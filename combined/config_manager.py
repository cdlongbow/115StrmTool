import json
import shutil
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Tuple

from logger import logger

CONFIG_FILE = Path("config.json")

PIN_RULES_SEP = " => "

DEFAULT_CONFIG: Dict[str, Any] = {
    "admin_host": "0.0.0.0",
    "admin_port": 8100,
    "emby": {
        "enabled": False,
        "emby_host": "http://192.168.2.100:8096",
        "proxy_host": "0.0.0.0",
        "proxy_port": 8097,
        "pin_rules": "",
        "external_player_url": False,
        "external_player_list": [],
    },
    "p115": {
        "enabled": False,
        "cookie": "",
        "redirect_host": "0.0.0.0",
        "redirect_port": 3333,
        "strm_url_prefix": "http://192.168.2.100:3333",
        "rmt_mediaext": "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v,webm",
        "download_mediaext": "srt,ssa,ass,sup,pgs,sub,idx",
        "auto_download_mediainfo": False,
        "overwrite_mode": "never",
        "paths": [
            {"from": "/电影", "to": "D:/strm/电影"},
            {"from": "/电视剧", "to": "D:/strm/电视剧"},
        ],
        "sync_cron": "",
        "sync_thread_count": 5,
        "database_path": "data/strm.db",
        "open_api": {"app_id": "", "app_key": "", "redirect_uri": ""},
    },
}


class ConfigManager:
    def __init__(self):
        self._lock = Lock()
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if CONFIG_FILE.exists():
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    self._config = _deep_merge(dict(DEFAULT_CONFIG), loaded)
                    logger.info("配置已加载: %s", CONFIG_FILE.resolve())
                except (json.JSONDecodeError, OSError) as e:
                    logger.error("配置文件加载失败，使用默认配置: %s", e)
                    self._config = dict(DEFAULT_CONFIG)
                    self._backup_corrupted()
            else:
                logger.info("配置文件不存在，使用默认配置")
                self._config = dict(DEFAULT_CONFIG)
        self._write()
        return self._deep_copy(self._config)

    def save(self) -> bool:
        return self._write()

    def update(self, new_config: Dict[str, Any]) -> bool:
        with self._lock:
            self._config = _deep_merge(self._config, new_config)
        return self._write()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return self._deep_copy(self._config)

    def _write(self) -> bool:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            logger.info("配置已保存: %s", CONFIG_FILE.resolve())
            return True
        except OSError as e:
            logger.error("配置保存失败: %s", e)
            return False

    def parse_pin_rules(self, raw: str) -> List[Tuple[str, str]]:
        result = []
        for line in (raw or "").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if PIN_RULES_SEP not in line:
                logger.warning("顶置规则格式错误: %s", line)
                continue
            parts = line.split(PIN_RULES_SEP, 1)
            path_prefix = parts[0].strip()
            target_url = parts[1].strip()
            if not path_prefix or not target_url:
                logger.warning("顶置规则路径或目标为空: %s", line)
                continue
            if not target_url.startswith(("http://", "https://")):
                logger.warning("顶置规则目标需以 http:// 或 https:// 开头: %s", line)
                continue
            result.append((path_prefix, target_url))
        return result

    def _backup_corrupted(self):
        backup_path = CONFIG_FILE.with_suffix(".json.bak")
        try:
            shutil.copy2(CONFIG_FILE, backup_path)
            logger.info("损坏的配置文件已备份: %s", backup_path)
        except OSError:
            pass
        CONFIG_FILE.unlink(missing_ok=True)

    def _deep_copy(self, d: dict) -> dict:
        return json.loads(json.dumps(d))


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


config_manager = ConfigManager()