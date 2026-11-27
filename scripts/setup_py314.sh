#!/usr/bin/env bash
# 沙箱重置后一键重建 Python 3.14 + 真实 SDK 环境
# 配方来源: .monkeycode/MEMORY.md「沙箱重建后的 Python 环境重建配方」
# 用法: bash scripts/setup_py314.sh [venv目录] [--test]
#   venv目录  默认 /tmp/opencode/py314-venv
#   --test    装完后在 combined/ 下跑全量 pytest
set -euo pipefail

VENV_DIR="/tmp/opencode/py314-venv"
RUN_TEST=0
for arg in "$@"; do
    case "$arg" in
        --test) RUN_TEST=1 ;;
        *) VENV_DIR="$arg" ;;
    esac
done

# 定位仓库根，保证相对路径稳定
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 安装 uv 到系统（--break-system-packages 规避 PEP 668 限制）
pip install --break-system-packages -q uv

# 下载并安装 uv 托管的 CPython 3.14
uv python install 3.14

# 创建 venv（--clear 允许覆盖已存在的 venv，保证脚本可重复执行）；
# 目录内无 uv，后续一律用系统 uv + --python
uv venv "$VENV_DIR" --clear --python 3.14

# 安装项目依赖与测试工具，优先使用仓库锁包；full_strm_sync 无 cp314 wheel 属预期跳过
uv pip install --python "$VENV_DIR/bin/python" \
    --find-links "$REPO_ROOT/combined/wheels" \
    -r combined/requirements.txt \
    pytest pytest-asyncio pip

"$VENV_DIR/bin/python" -c "import sys; assert sys.version_info[:2] == (3, 14), sys.version"
"$VENV_DIR/bin/python" -m pip check

echo
echo "Python 3.14 环境已就绪: $VENV_DIR"
echo "激活环境: source $VENV_DIR/bin/activate"
echo "运行测试: cd combined && $VENV_DIR/bin/python -m pytest -q"

if [ "$RUN_TEST" -eq 1 ]; then
    echo
    echo "开始运行全量测试..."
    (cd combined && "$VENV_DIR/bin/python" -m pytest -q)
fi
