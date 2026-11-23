import os
import sys
from pathlib import Path


def _resolve_base_dir() -> Path:
    """
    解析应用数据根目录：优先环境变量 APP_DATA_DIR，其次按打包/源码模式回退

    :return Path: 应用数据根目录
    """
    env_dir = os.environ.get("APP_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


_BASE_DIR = _resolve_base_dir()

CONFIG_FILE = _BASE_DIR / "config.json"
CONFIG_KEY_FILE = _BASE_DIR / "config.key"
CONFIG_BACKUP_DIR = _BASE_DIR / "config_backups"
CHECKIN_STATE_FILE = _BASE_DIR / "checkin_state.json"
LOG_DIR = _BASE_DIR / "logs"
DB_PATH = _BASE_DIR / "data" / "strm.db"
