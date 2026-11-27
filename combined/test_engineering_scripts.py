"""
工程脚本测试：校验本地 wheel 安装策略与中文文件名 SHA256 输出
"""
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "install_local_wheels.py"
SPEC = importlib.util.spec_from_file_location("install_local_wheels", SCRIPT_PATH)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

CHECKSUM_SPEC = importlib.util.spec_from_file_location(
    "write_sha256", SCRIPTS_DIR / "write_sha256.py"
)
assert CHECKSUM_SPEC and CHECKSUM_SPEC.loader
write_sha256 = importlib.util.module_from_spec(CHECKSUM_SPEC)
CHECKSUM_SPEC.loader.exec_module(write_sha256)

COVERAGE_SPEC = importlib.util.spec_from_file_location(
    "check_coverage", SCRIPTS_DIR / "check_coverage.py"
)
assert COVERAGE_SPEC and COVERAGE_SPEC.loader
check_coverage = importlib.util.module_from_spec(COVERAGE_SPEC)
COVERAGE_SPEC.loader.exec_module(check_coverage)


def _coverage_file(tmp_path: Path, total: float, module_percent: float) -> Path:
    """写入最小 coverage JSON 夹具"""
    path = tmp_path / "coverage.json"
    payload = {
        "totals": {"percent_covered": total},
        "files": {
            name: {"summary": {"percent_covered": module_percent}}
            for name in check_coverage.THRESHOLDS
            if name != "TOTAL"
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_incompatible_required_wheel_is_skipped(tmp_path, monkeypatch):
    """跨平台 wheel 应跳过且不调用 pip"""
    (tmp_path / "demo-1.0-py3-none-win_amd64.whl").touch()
    supported = next(iter(installer.parse_wheel_filename("demo-1.0-py3-none-any.whl")[3]))
    monkeypatch.setattr(installer, "sys_tags", lambda: {supported})
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("跨平台 wheel 不应安装"),
    )
    assert installer.install_local_wheels(tmp_path) == 0


def test_optional_wheel_failure_is_skipped(tmp_path, monkeypatch):
    """可选 wheel 安装失败应跳过"""
    (tmp_path / "full_strm_sync-0.1.6-py3-none-any.whl").touch()
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "failed"),
    )
    assert installer.install_local_wheels(tmp_path) == 0


def test_required_wheel_failure_aborts(tmp_path, monkeypatch):
    """当前平台必需 wheel 安装失败应中止"""
    (tmp_path / "p115client-1.0-py3-none-any.whl").touch()
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "failed"),
    )
    assert installer.install_local_wheels(tmp_path) == 1


def test_required_wheel_success_continues(tmp_path, monkeypatch):
    """当前平台必需 wheel 安装成功应继续"""
    (tmp_path / "p115client-1.0-py3-none-any.whl").touch()
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    assert installer.install_local_wheels(tmp_path) == 0


def test_write_sha256_supports_chinese_filename(tmp_path):
    """中文 exe 文件名应能以 UTF-8 写入标准 SHA256 校验文件"""
    target = tmp_path / "115网盘STRM生成与302工具.exe"
    payload = b"frozen-binary"
    target.write_bytes(payload)
    checksum_path = write_sha256.write_sha256(target)
    assert checksum_path == target.with_name(target.name + ".sha256")
    assert checksum_path.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n"
    )


def test_check_coverage_accepts_thresholds(tmp_path, monkeypatch):
    """达到全部阈值时覆盖率门禁应通过"""
    path = _coverage_file(tmp_path, 80.0, 100.0)
    monkeypatch.setattr(check_coverage.sys, "argv", ["check_coverage.py", str(path)])
    assert check_coverage.main() == 0


def test_check_coverage_rejects_below_threshold(tmp_path, monkeypatch):
    """任一关键模块低于阈值时覆盖率门禁应失败"""
    path = _coverage_file(tmp_path, 80.0, 10.0)
    monkeypatch.setattr(check_coverage.sys, "argv", ["check_coverage.py", str(path)])
    assert check_coverage.main() == 1
