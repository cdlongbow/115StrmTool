"""
Renovate 工程配置回归测试
"""

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "renovate.json"
WHEELS_DIR = ROOT / "combined" / "wheels"
REQUIREMENTS_INPUTS = [
    "combined/requirements.txt",
    "combined/requirements-dev.in",
]
LOCK_FILES = [
    "combined/requirements.lock",
    "combined/requirements-dev.lock",
]
WORKFLOW_FILES = [
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
]


def _load_config() -> Dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheel_package_names() -> List[str]:
    names = set()
    for wheel in WHEELS_DIR.glob("*.whl"):
        names.add(_normalize_package_name(wheel.name.split("-", 1)[0]))
    return sorted(names)


def _pattern_matches(pattern: str, package_name: str) -> bool:
    normalized_name = _normalize_package_name(package_name)
    if pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], normalized_name) is not None
    return _normalize_package_name(pattern) == normalized_name


def test_renovate_uses_recommended_baseline_and_weekly_schedule() -> None:
    """
    验证 Renovate 使用官方推荐基线并按周调度
    """
    config = _load_config()

    assert config["$schema"] == "https://docs.renovatebot.com/renovate-schema.json"
    assert "config:recommended" in config["extends"]
    assert set(config["enabledManagers"]) == {"pip_requirements", "github-actions"}
    assert config["schedule"] == ["before 6am on monday"]
    assert config["timezone"] == "Asia/Shanghai"


def test_renovate_manager_covers_requirements_inputs_and_excludes_locks() -> None:
    """
    验证 requirements 输入管理器覆盖 txt 与 in 并排除锁文件
    """
    config = _load_config()
    patterns = config["pip_requirements"]["managerFilePatterns"]
    compiled = [re.compile(pattern[1:-1]) for pattern in patterns]

    for package_file in REQUIREMENTS_INPUTS:
        assert any(regex.search(package_file) for regex in compiled), package_file
    for lock_file in LOCK_FILES:
        assert not any(regex.search(lock_file) for regex in compiled), lock_file
        assert lock_file in config["ignorePaths"]


def test_renovate_groups_pypi_and_github_actions_updates() -> None:
    """
    验证 PyPI 与 GitHub Actions 更新分别分组
    """
    rules = _load_config()["packageRules"]

    assert any(
        rule.get("matchManagers") == ["pip_requirements"]
        and rule.get("matchDatasources") == ["pypi"]
        and rule.get("groupName") == "Python dependencies"
        and rule.get("groupSlug") == "python"
        for rule in rules
    )
    assert any(
        rule.get("matchManagers") == ["github-actions"]
        and rule.get("groupName") == "GitHub Actions"
        and rule.get("groupSlug") == "github-actions"
        for rule in rules
    )


def test_renovate_disables_all_vendored_wheel_packages() -> None:
    """
    验证仓库 wheels 中的全部 SDK 包均禁止自动升级
    """
    rules = _load_config()["packageRules"]
    disabled_patterns = [
        pattern for rule in rules if rule.get("enabled") is False for pattern in rule.get("matchPackageNames", [])
    ]
    wheel_packages = _wheel_package_names()

    assert wheel_packages
    missing = [
        package
        for package in wheel_packages
        if not any(_pattern_matches(pattern, package) for pattern in disabled_patterns)
    ]
    assert not missing, f"vendored wheel packages are not disabled: {missing}"

    required_packages = {
        "p115client",
        "p115cipher",
        "p115pickcode",
        "python-httpfile",
        "python-http-request",
        "python-dictattr",
        "python-concurrenttools",
        "call-sleep",
        "full-strm-sync",
    }
    assert required_packages.issubset(set(wheel_packages))

    for package in (
        "fastapi",
        "pydantic",
        "uvicorn",
        "httpx",
        "websockets",
        "qrcode",
        "pillow",
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "ruff",
        "pyinstaller",
    ):
        assert not any(_pattern_matches(pattern, package) for pattern in disabled_patterns)


def test_renovate_regenerates_locks_with_uv_after_requirements_updates() -> None:
    """
    验证 requirements 输入更新后通过 uv 重生成两把锁文件
    """
    config = _load_config()
    task_rules = [rule for rule in config["packageRules"] if "postUpgradeTasks" in rule]

    assert len(task_rules) == 1
    rule = task_rules[0]
    assert rule["matchManagers"] == ["pip_requirements"]
    assert set(rule["matchFileNames"]) == set(REQUIREMENTS_INPUTS)

    tasks = rule["postUpgradeTasks"]
    assert tasks["executionMode"] == "branch"
    assert tasks["installTools"] == {"uv": {}}
    assert set(tasks["fileFilters"]) == set(LOCK_FILES)
    assert len(tasks["commands"]) == 2

    for source_file, lock_file in zip(REQUIREMENTS_INPUTS, LOCK_FILES):
        command = next(command for command in tasks["commands"] if f" {source_file} " in command)
        assert command.startswith("uv pip compile ")
        assert f"--output-file {lock_file}" in command
        for flag in (
            "--universal",
            "--python-version 3.12",
            "--generate-hashes",
            "--find-links combined/wheels",
            "--emit-find-links",
        ):
            assert flag in command
        assert "--upgrade" not in command


def test_engineering_workflows_run_root_config_tests() -> None:
    """
    验证 CI 与发布流程均运行根目录工程配置测试
    """
    command = "run: python -m pytest scripts/test_engineering_scripts.py -q"

    for workflow_file in WORKFLOW_FILES:
        assert command in (ROOT / workflow_file).read_text(encoding="utf-8"), workflow_file


def test_frozen_smoke_checks_web_api_health_and_version(monkeypatch) -> None:
    """
    冻结 exe 冒烟应覆盖 Web、API、健康检查与版本号
    """
    script_path = ROOT / "scripts" / "smoke_frozen_exe.py"
    spec = importlib.util.spec_from_file_location("smoke_frozen_exe", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests = []

    class FakeResponse:
        def __init__(self, payload: Any) -> None:
            self.payload = payload
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def fake_request(url: str, token: str = ""):
        requests.append((url, token))
        if url.endswith("/"):
            return FakeResponse({"html": "115网盘STRM生成与302工具"})
        if url.endswith("/admin/api/status"):
            return FakeResponse({"version": "20261127"})
        if url.endswith("/health/live"):
            return FakeResponse({"status": "ok"})
        if url.endswith("/health/ready"):
            return FakeResponse({"status": "ready"})
        raise AssertionError(url)

    monkeypatch.setattr(module, "_request", fake_request)

    module._check_endpoints("http://127.0.0.1:8100", "admin-token", "20261127", 1.0)

    assert [url for url, _token in requests] == [
        "http://127.0.0.1:8100/",
        "http://127.0.0.1:8100/admin/api/status",
        "http://127.0.0.1:8100/health/live",
        "http://127.0.0.1:8100/health/ready",
    ]
    assert requests[1][1] == "admin-token"
    assert requests[2][1] == ""
    assert requests[3][1] == ""


def test_inline_js_check_uses_temp_file_and_timeout(tmp_path, monkeypatch) -> None:
    """
    内联 JS 检查应通过临时文件调用 node 并设置超时
    """
    script_path = ROOT / "scripts" / "check_inline_js.py"
    spec = importlib.util.spec_from_file_location("check_inline_js", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    html_path = tmp_path / "index.html"
    html_path.write_text("<script>const value = 1;</script>", encoding="utf-8")
    observed = {}

    def fake_run(command, **_kwargs):
        observed["command"] = command
        observed["timeout"] = _kwargs["timeout"]
        observed["source"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.check_inline_js(html_path) == 0
    assert observed["command"][:2] == ["node", "--check"]
    assert observed["timeout"] == 30
    assert observed["source"] == "const value = 1;"


def test_inline_js_check_times_out_cleanly(tmp_path, monkeypatch) -> None:
    """
    node 语法检查超时时返回失败而不是挂起
    """
    script_path = ROOT / "scripts" / "check_inline_js.py"
    spec = importlib.util.spec_from_file_location("check_inline_js", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    html_path = tmp_path / "index.html"
    html_path.write_text("<script>const value = 1;</script>", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("node", 30)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.check_inline_js(html_path) == 1
