"""
前端行为测试入口：通过 Node.js 执行管理面板脚本回归用例
"""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_behavior():
    """前端脚本应通过令牌重试、轮询与会话隔离等行为测试"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 不可用")
    script = Path(__file__).resolve().parents[1] / "scripts" / "test_frontend.js"
    result = subprocess.run(
        [node, "--test", str(script)],
        cwd=str(script.parents[1]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
