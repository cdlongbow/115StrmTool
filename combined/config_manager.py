import base64
import hashlib
import hmac
import json
import os
import re
import shutil
from enum import Enum
from pathlib import Path
from threading import RLock
from time import strftime, time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from _paths import _BASE_DIR, CONFIG_FILE, CONFIG_KEY_FILE, CONFIG_BACKUP_DIR  # noqa: F401
from exceptions import ServiceError
from logger import logger

MAX_CONFIG_BACKUPS = 10
CONFIG_BACKUP_MIN_INTERVAL = 3600.0

PIN_RULES_SEP = " => "

_COOKIE_ENCRYPTED_PREFIX = "#ENC#"
_COOKIE_ENCRYPTED_V2_PREFIX = "#ENC#v2:"
COOKIE_MASK = "********"
_SECRET_FIELDS: List[Tuple[str, str]] = [("p115", "cookie"), ("webdav", "password")]


def _set_posix_permissions(path: Path, mode: int) -> None:
    """
    在 POSIX 平台收紧文件或目录权限

    :param path (Path): 目标路径
    :param mode (int): 八进制权限值
    """
    if os.name != "nt":
        os.chmod(path, mode)


def mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    生成对外展示用的配置副本，将 115 Cookie 替换为固定掩码

    :param config (Dict): 完整配置字典

    :return Dict: 脱敏后的配置副本，不影响原字典
    """
    data = json.loads(json.dumps(config))
    cookie = data.get("p115", {}).get("cookie")
    if cookie:
        data["p115"]["cookie"] = COOKIE_MASK
    webdav = data.get("webdav")
    if isinstance(webdav, dict) and webdav.get("password"):
        webdav["password"] = COOKIE_MASK
    if data.get("admin_token"):
        data["admin_token"] = COOKIE_MASK
    return data


class PlaybackMode(str, Enum):
    """
    播放模式：Emby 前置代理处理媒体路由的三态互斥枚举

    Attributes:
        DISABLED: 关闭，媒体请求透传 Emby 原响应（通用反向代理）
        REDIRECT: 302 直链，下发 302 让播放端直连 115 CDN
        STREAM_PROXY: 流代理，由工具向 115 CDN 取流回传播放端，仅响应私有网段来源
    """

    DISABLED = "disabled"
    REDIRECT = "redirect"
    STREAM_PROXY = "stream_proxy"


PLAYBACK_MODE_VALUES = (
    PlaybackMode.DISABLED.value,
    PlaybackMode.REDIRECT.value,
    PlaybackMode.STREAM_PROXY.value,
)


class EmbyConfig(BaseModel):
    enabled: bool = False
    emby_host: str = "http://192.168.2.100:8096"
    proxy_host: str = "0.0.0.0"
    proxy_port: int = 8097
    pin_rules: str = ""
    external_player_url: bool = False
    external_player_list: List[str] = Field(default_factory=list)
    playback_mode: PlaybackMode = PlaybackMode.DISABLED

    @field_validator("playback_mode", mode="before")
    @classmethod
    def _normalize_playback_mode(cls, v: Any) -> str:
        if isinstance(v, str):
            low = v.strip().lower()
            if low in PLAYBACK_MODE_VALUES:
                return low
            logger.warning("playback_mode 取值非法，回落 disabled: %s", v)
            return PlaybackMode.DISABLED.value
        if isinstance(v, bool):
            return PlaybackMode.REDIRECT.value if v else PlaybackMode.DISABLED.value
        logger.warning("playback_mode 类型非预期，回落 disabled: %r", v)
        return PlaybackMode.DISABLED.value


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
    use_rust: bool = False
    fast_scan: bool = True
    same_playback: bool = False
    same_playback_dir: str = "/多端播放"
    paths: List[PathMapping] = Field(default_factory=list)

    @field_validator("strm_url_prefix", mode="before")
    @classmethod
    def _normalize_strm_url_prefix(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        if v is None:
            return ""
        return v

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


class WebdavBackupConfig(BaseModel):
    """
    WebDAV 远端备份配置
    """

    enabled: bool = Field(default=False, description="远端备份总开关")
    url: str = Field(default="", description="WebDAV 根地址，http(s) 开头")
    username: str = Field(default="", description="WebDAV 账号")
    password: str = Field(default="", description="WebDAV 密码，落盘加密、展示掩码")
    remote_subdir: str = Field(default="115strmtool-backups", description="远端备份子目录，逐级创建")
    keep_count: int = Field(default=10, description="远端保留档案份数")

    @field_validator("keep_count")
    @classmethod
    def _validate_keep_count(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            return 10
        return v


class RootConfig(BaseModel):
    admin_host: str = "127.0.0.1"
    admin_port: int = 8100
    admin_token: str = Field(default="", description="管理接口访问令牌，空值时启动自动生成")
    emby: EmbyConfig = Field(default_factory=EmbyConfig)
    p115: P115Config = Field(default_factory=P115Config)
    checkin: CheckinConfig = Field(default_factory=CheckinConfig)
    webdav: WebdavBackupConfig = Field(default_factory=WebdavBackupConfig)
    media_diag: bool = Field(
        default=False,
        description="播放诊断日志开关（管理面板可切换，默认关闭；保存后立即生效）",
    )


def _migrate_legacy_emby_fields(raw: Dict[str, Any]) -> None:
    """
    就地迁移 Emby 段的旧 redirect_mode 布尔字段为 playback_mode 枚举值

    旧键无论是否映射均从字典中移除，避免旧键在配置中持续残留

    :param raw (Dict): 即将进入模型校验的原始配置字典，就地修改
    """
    emby = raw.get("emby")
    if not isinstance(emby, dict) or "redirect_mode" not in emby:
        return
    legacy = emby.pop("redirect_mode")
    if "playback_mode" not in emby:
        emby["playback_mode"] = (
            PlaybackMode.REDIRECT.value if legacy else PlaybackMode.DISABLED.value
        )
    logger.info("配置迁移: emby.redirect_mode(%r) -> emby.playback_mode=%s", legacy, emby["playback_mode"])


def _validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """通过 Pydantic 模型校验并修复配置，保留未知字段防止数据丢失

    :param raw (Dict): 原始配置字典

    :return Dict: 校验并合并后的配置字典
    """
    try:
        _migrate_legacy_emby_fields(raw)
        validated = RootConfig(**raw)
        known = validated.model_dump(by_alias=True, exclude_unset=True)
        merged = _deep_merge(raw, known)
        return merged
    except Exception as e:
        logger.warning("配置校验失败，使用默认值: %s", e)
        return RootConfig().model_dump(by_alias=True)


def _validate_config_strict(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    严格解析并校验配置，校验失败时抛出异常

    :param raw (Dict): 原始配置字典

    :return Dict: 校验并合并后的配置字典

    :raises ValueError: 配置顶层或字段结构非法
    """
    if not isinstance(raw, dict):
        raise ValueError("配置顶层必须是 JSON 对象")
    _migrate_legacy_emby_fields(raw)
    validated = RootConfig(**raw)
    known = validated.model_dump(by_alias=True, exclude_unset=True)
    return _deep_merge(raw, known)


def _get_encryption_key() -> bytes:
    """
    获取持久化的加密密钥，首次自动生成 32 字节随机密钥保存到 config.key
    密钥文件与 config.json 同目录，两者缺一则无法解密
    """
    key_file = CONFIG_KEY_FILE
    if not key_file.exists():
        key = os.urandom(32)
        try:
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = key_file.read_bytes()
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(key)
    else:
        key = key_file.read_bytes()
    _set_posix_permissions(key_file, 0o600)
    return key


def _derive_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", key, salt, 100000)


def _xor_bytes(data: bytes, derived: bytes) -> bytes:
    return bytes([b ^ derived[i % len(derived)] for i, b in enumerate(data)])


def _encrypt_cookie(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    key = _get_encryption_key()
    salt = os.urandom(16)
    derived = _derive_key(key, salt)
    encrypted = _xor_bytes(plaintext.encode("utf-8"), derived)
    mac = hmac.new(derived, salt + encrypted, hashlib.sha256).digest()
    payload = base64.b64encode(salt + encrypted + mac).decode("ascii")
    return _COOKIE_ENCRYPTED_V2_PREFIX + payload


def _decrypt_cookie(encoded: str, key: Optional[bytes] = None) -> str:
    if not encoded or not encoded.startswith(_COOKIE_ENCRYPTED_PREFIX):
        return encoded
    key = key if key is not None else _get_encryption_key()
    if encoded.startswith(_COOKIE_ENCRYPTED_V2_PREFIX):
        payload = encoded[len(_COOKIE_ENCRYPTED_V2_PREFIX):]
        raw = base64.b64decode(payload, validate=True)
        if len(raw) <= 48:
            raise ValueError("加密数据长度非法")
        salt = raw[:16]
        encrypted = raw[16:-32]
        mac = raw[-32:]
        derived = _derive_key(key, salt)
        expected = hmac.new(derived, salt + encrypted, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError("密钥错误或加密数据损坏")
        return _xor_bytes(encrypted, derived).decode("utf-8")
    raw = base64.b64decode(encoded[len(_COOKIE_ENCRYPTED_PREFIX):], validate=True)
    if len(raw) <= 16:
        raise ValueError("加密数据长度非法")
    salt = raw[:16]
    encrypted = raw[16:]
    derived = _derive_key(key, salt)
    return _xor_bytes(encrypted, derived).decode("utf-8")


def _encrypt_secrets(config: Dict[str, Any]) -> None:
    """
    按秘密字段表就地加密配置字典中的全部敏感值（空值跳过）

    :param config (Dict): 即将写盘的配置字典，就地修改
    """
    for section, field in _SECRET_FIELDS:
        target = config.get(section)
        if not isinstance(target, dict):
            continue
        value = target.get(field)
        if value and not value.startswith(_COOKIE_ENCRYPTED_PREFIX):
            target[field] = _encrypt_cookie(value)


def _decrypt_secrets(config: Dict[str, Any]) -> None:
    """
    按秘密字段表就地解密配置字典中的全部敏感值，无 #ENC# 前缀按明文兼容

    :param config (Dict): 刚从磁盘读取的配置字典，就地修改
    """
    for section, field in _SECRET_FIELDS:
        target = config.get(section)
        if not isinstance(target, dict):
            continue
        value = target.get(field)
        if value and value.startswith(_COOKIE_ENCRYPTED_PREFIX):
            try:
                target[field] = _decrypt_cookie(value)
            except Exception as e:
                logger.warning("秘密字段解密失败，保留原始值: %s.%s - %s", section, field, e)


def _decrypt_secrets_strict(config: Dict[str, Any], key: Optional[bytes]) -> None:
    """
    使用指定密钥试解密全部秘密字段，任一失败即抛出异常

    :param config (Dict): 待校验配置字典，就地解密
    :param key (bytes): 拟采用密钥，缺失时拒绝解密加密字段

    :raises ValueError: 密钥缺失或秘密字段解密失败
    """
    errors = []
    for section, field in _SECRET_FIELDS:
        target = config.get(section)
        if not isinstance(target, dict):
            continue
        value = target.get(field)
        if not value or not value.startswith(_COOKIE_ENCRYPTED_PREFIX):
            continue
        if key is None:
            errors.append(f"{section}.{field} 已加密但缺少密钥")
            continue
        try:
            target[field] = _decrypt_cookie(value, key=key)
        except Exception as exc:
            errors.append(f"{section}.{field} 解密失败：{exc}")
    if errors:
        raise ValueError("；".join(errors))


DEFAULT_CONFIG: Dict[str, Any] = RootConfig().model_dump(by_alias=True)


class ConfigManager:
    def __init__(self):
        self._lock = RLock()
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
                    _decrypt_secrets(self._config)
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
            previous = self._deep_copy(self._config)
            self._config = _deep_merge(self._config, new_config)
            self._config = _validate_config(self._config)
            if not self._write():
                self._config = previous
                return False
            return True

    def update_or_raise(self, new_config: Dict[str, Any]) -> None:
        """
        更新配置并在写盘失败时抛出服务异常

        :param new_config (Dict): 待合并的配置字典

        :raises ServiceError: 配置保存失败
        """
        if not self.update(new_config):
            raise ServiceError("配置保存失败，请检查数据目录权限或磁盘空间")

    def get_flag(self, key: str, default: bool = False) -> bool:
        """读取顶层布尔开关；热路径上避免复制整份配置。

        诊断日志等高频判断直接读进程内配置单例，管理面板保存后无需
        重启或重建服务即可生效；各服务运行在同一进程的线程中。

        :param key (str): 顶层配置键
        :param default (bool): 缺失时的返回值
        :return bool: 开关状态
        """
        with self._lock:
            return bool(self._config.get(key, default))

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return self._deep_copy(self._config)

    def export_disk_text(self) -> Optional[str]:
        """
        读取磁盘当前配置用于导出（Cookie 保持密文形态，管理令牌剔除）

        导出的文件常被另存或分享，明文 admin_token 不能随之外带；
        导入方首次启动会自动生成新令牌，无需保留旧值。

        :return str: 配置文件内容，文件不存在或读取失败返回 None
        """
        try:
            content = CONFIG_FILE.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict) and data.pop("admin_token", None) is not None:
                return json.dumps(data, ensure_ascii=False, indent=2)
            return content
        except (OSError, ValueError):
            return None

    def _write(self) -> bool:
        tmp_path: Optional[Path] = None
        try:
            with self._lock:
                config_to_write = self._deep_copy(self._config)
                _encrypt_secrets(config_to_write)
                # 原子写：进程唯一临时文件 + 替换目标；整体持锁防止并发写交叉
                tmp_path = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.{os.getpid()}.tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(config_to_write, f, ensure_ascii=False, indent=2)
                _set_posix_permissions(tmp_path, 0o600)
                os.replace(tmp_path, CONFIG_FILE)
            logger.info("配置已保存: %s", CONFIG_FILE.resolve())
            self._maybe_backup()
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.error("配置保存失败: %s", e)
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def _maybe_backup(self):
        """
        配置写盘成功后做滚动快照，保留最近 MAX_CONFIG_BACKUPS 份

        距上一份不足 CONFIG_BACKUP_MIN_INTERVAL 秒则跳过，避免高频写
        把有效历史轮换掉；快照内容沿用磁盘形态（Cookie 为密文）
        """
        try:
            CONFIG_BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
            _set_posix_permissions(CONFIG_BACKUP_DIR, 0o700)
            backups = sorted(CONFIG_BACKUP_DIR.glob("config-*.json"))
            now = time()
            if backups and now - backups[-1].stat().st_mtime < CONFIG_BACKUP_MIN_INTERVAL:
                return
            dest = CONFIG_BACKUP_DIR / f"config-{strftime('%Y%m%d-%H%M%S')}.json"
            shutil.copy2(CONFIG_FILE, dest)
            _set_posix_permissions(dest, 0o600)
            stale = sorted(CONFIG_BACKUP_DIR.glob("config-*.json"))[:-MAX_CONFIG_BACKUPS]
            for old in stale:
                old.unlink(missing_ok=True)
            logger.info("配置快照已备份: %s", dest.name)
        except OSError as e:
            logger.warning("配置快照备份失败: %s", e)

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