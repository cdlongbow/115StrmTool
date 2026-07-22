import base64
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from logger import logger

if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent

CONFIG_FILE = _BASE_DIR / "config.json"

PIN_RULES_SEP = " => "

_COOKIE_ENCRYPTED_PREFIX = "#ENC#"


# ── Pydantic 配置模型 ──


class EmbyConfig(BaseModel):
    enabled: bool = False
    emby_host: str = "http://192.168.2.100:8096"
    proxy_host: str = "0.0.0.0"
    proxy_port: int = 8097
    pin_rules: str = ""
    external_player_url: bool = False
    external_player_list: List[str] = Field(default_factory=list)
    redirect_mode: bool = False


class PathMapping(BaseModel):
    from_path: str = Field(default="", alias="from")
    to: str = ""
    enabled: bool = True

    model_config = {"populate_by_name": True}


class P115Config(BaseModel):
    enabled: bool = False
    cookie: str = ""
    redirect_host: str = "0.0.0.0"
    redirect_port: int = 3333
    strm_url_prefix: str = "http://192.168.2.100:3333"
    rmt_mediaext: str = "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v,webm"
    download_mediaext: str = "srt,ssa,ass,sup,pgs,sub,idx"
    auto_download_mediainfo: bool = False
    overwrite_mode: str = "never"
    cleanup_deleted_strm: bool = False
    paths: List[PathMapping] = Field(default_factory=list)

    @field_validator("overwrite_mode")
    @classmethod
    def _validate_overwrite_mode(cls, v: str) -> str:
        if v not in ("never", "always"):
            return "never"
        return v


class CheckinConfig(BaseModel):
    enabled: bool = False
    time_range: str = "06:00-09:00"

    @field_validator("time_range")
    @classmethod
    def _validate_time_range(cls, v: str) -> str:
        if not re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", v):
            return "06:00-09:00"
        return v


class RootConfig(BaseModel):
    admin_host: str = "0.0.0.0"
    admin_port: int = 8100
    emby: EmbyConfig = Field(default_factory=EmbyConfig)
    p115: P115Config = Field(default_factory=P115Config)
    checkin: CheckinConfig = Field(default_factory=CheckinConfig)


def _validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """通过 Pydantic 模型校验并修复配置，保留未知字段防止数据丢失"""
    try:
        validated = RootConfig(**raw)
        known = validated.model_dump(by_alias=True, exclude_unset=True)
        merged = _deep_merge(raw, known)
        return merged
    except Exception as e:
        logger.warning("配置校验失败，使用默认值: %s", e)
        return RootConfig().model_dump(by_alias=True)


def _get_encryption_key() -> bytes:
    """
    获取持久化的加密密钥，首次自动生成 32 字节随机密钥保存到 config.key
    密钥文件与 config.json 同目录，两者缺一则无法解密
    """
    key_file = _BASE_DIR / "config.key"
    if not key_file.exists():
        key = os.urandom(32)
        key_file.write_bytes(key)
    else:
        key = key_file.read_bytes()
    return key


def _encrypt_cookie(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    key = _get_encryption_key()
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", key, salt, 100000)
    data = plaintext.encode("utf-8")
    encrypted = bytes([b ^ derived[i % len(derived)] for i, b in enumerate(data)])
    return _COOKIE_ENCRYPTED_PREFIX + base64.b64encode(salt + encrypted).decode("ascii")


def _decrypt_cookie(encoded: str) -> str:
    if not encoded or not encoded.startswith(_COOKIE_ENCRYPTED_PREFIX):
        return encoded
    key = _get_encryption_key()
    raw = base64.b64decode(encoded[len(_COOKIE_ENCRYPTED_PREFIX):])
    salt = raw[:16]
    encrypted = raw[16:]
    derived = hashlib.pbkdf2_hmac("sha256", key, salt, 100000)
    decrypted = bytes([b ^ derived[i % len(derived)] for i, b in enumerate(encrypted)])
    return decrypted.decode("utf-8")

DEFAULT_CONFIG: Dict[str, Any] = RootConfig().model_dump(by_alias=True)


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
                    self._config = _validate_config(self._config)
                    cookie = self._config.get("p115", {}).get("cookie", "")
                    if cookie and cookie.startswith(_COOKIE_ENCRYPTED_PREFIX):
                        try:
                            self._config["p115"]["cookie"] = _decrypt_cookie(cookie)
                        except Exception as e:
                            logger.warning("Cookie 解密失败，保留原始值: %s", e)
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
            self._config = _validate_config(self._config)
        return self._write()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return self._deep_copy(self._config)

    def _write(self) -> bool:
        try:
            config_to_write = self._deep_copy(self._config)
            cookie = config_to_write.get("p115", {}).get("cookie", "")
            if cookie:
                config_to_write["p115"]["cookie"] = _encrypt_cookie(cookie)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config_to_write, f, ensure_ascii=False, indent=2)
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
        except OSError as e:
            logger.warning("备份损坏配置文件失败: %s", e)
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