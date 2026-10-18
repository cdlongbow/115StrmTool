"""
WebDAV 远端配置备份模块

档案打包/自检/上传/清理/列表/下载/恢复；依赖纪律：httpx（已捆绑）+标准库，
协议错误统一收口 ServiceError 供 API 层转译
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urljoin
from xml.etree import ElementTree

import httpx

from app_metadata import APP_VERSION
from config_manager import CONFIG_BACKUP_DIR, CONFIG_FILE, config_manager
from exceptions import BadRequestError, NotFoundError, ServiceError
from logger import logger

_BASE_DIR = CONFIG_FILE.parent

BACKUP_NAME_PREFIX = "115strmtool-backup-"
BACKUP_NAME_SUFFIX = ".zip"
BACKUP_NAME_RE = re.compile(r"^115strmtool-backup-\d{8}-\d{6}(-\d{1,2})?\.zip$")
CHECKIN_STATE_FILE = _BASE_DIR / "checkin_state.json"
BACKUP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_PROBE_NAME = "115strmtool-write-probe.tmp"
_PROPFIND_BODY = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:getcontentlength/><d:getlastmodified/><d:resourcetype/>"
    "</d:prop></d:propfind>"
).encode("utf-8")
_MANIFEST_NAME = "manifest.json"
_REQUIRED_MEMBERS = ("config.json", "config.key", _MANIFEST_NAME)


def _friendly_http_error(resp: httpx.Response, action: str) -> str:
    """
    将 WebDAV 失败状态码转成可定位根因的中文说明

    :param resp (httpx.Response): 非成功响应
    :param action (str): 操作描述

    :return str: 错误说明
    """
    code = resp.status_code
    if code == 401:
        return "认证失败：账号或密码错误（HTTP 401）"
    if code == 403:
        return "无写入权限：服务器拒绝访问（HTTP 403）"
    return "{0}失败：服务器返回 HTTP {1}".format(action, code)


def _parse_int_lenient(value: Any) -> int:
    """
    宽容解析数字字段，非整文本向零取整，识别失败按 0 降级

    :param value (Any): 待解析值

    :return int: 解析结果
    """
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


class WebdavClient:
    """
    WebDAV 最小客户端，仅实现备份闭环所需的六个操作

    transport 参数供测试注入 MockTransport 替换真实网络
    """

    def __init__(
        self,
        base_url,
        username="",
        password="",
        transport=None,
    ):
        """
        :param base_url (str): WebDAV 根地址
        :param username (str): 账号，与密码皆空时匿名访问
        :param password (str): 密码
        :param transport (httpx.BaseTransport): 注入的 httpx transport，测试替换真实网络
        """
        if not base_url.startswith(("http://", "https://")):
            raise ServiceError("WebDAV 地址必须以 http:// 或 https:// 开头")
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        auth = (username, password) if (username or password) else None
        self._client = httpx.Client(
            auth=auth,
            follow_redirects=True,
            timeout=BACKUP_TIMEOUT,
            transport=transport,
        )

    def close(self):
        """
        关闭底层连接池

        :return None:
        """
        self._client.close()

    def _url(self, rel):
        """
        拼接并转义根相对路径

        :param rel (str): 根下相对路径

        :return str: 完整 URL
        """
        return urljoin(self._base, quote(rel.lstrip("/"), safe="/"))

    def _request(self, method, rel, **kwargs):
        """
        发起请求，网络层异常归类为不可达 ServiceError

        :param method (str): HTTP 方法（含 WEBDAV 扩展）
        :param rel (str): 相对路径
        :param kwargs (Dict): 透传 httpx 参数

        :return httpx.Response: 原始响应，状态码由调用侧判定

        :raises ServiceError: 网络层失败
        """
        try:
            return self._client.request(method, self._url(rel), **kwargs)
        except httpx.RequestError as exc:
            raise ServiceError("网络不可达：" + str(exc))

    def test_connection(self, subdir):
        """
        连接测试：建目录、写探针、删探针、读列表，失败归类根因返回

        :param subdir (str): 远端备份子目录

        :return Tuple: (是否通过, 结论或根因)
        """
        try:
            self.ensure_dir(subdir)
            dir_part = subdir.strip("/")
            probe_rel = (dir_part + "/" if dir_part else "") + _PROBE_NAME
            resp = self._request("PUT", probe_rel, content=b"probe")
            if resp.status_code not in (200, 201, 204):
                return False, _friendly_http_error(resp, "写入探针（目标目录不可写）")
            self.delete(probe_rel)
            self.list_archives(subdir)
        except ServiceError as exc:
            return False, str(exc)
        return True, "连接正常，目录可读写"

    def ensure_dir(self, dir_rel):
        """
        逐级 MKCOL 创建远端目录，已存在的层级宽容跳过

        :param dir_rel (str): 目录相对路径，空值直接返回

        :raises ServiceError: 某层创建被拒绝或网络失败
        """
        parts = [p for p in (dir_rel or "").strip("/").split("/") if p and p != ".."]
        for i in range(len(parts)):
            target = "/".join(parts[: i + 1]) + "/"
            resp = self._request("MKCOL", target)
            if resp.status_code not in (201, 204, 405):
                raise ServiceError(_friendly_http_error(resp, "创建远端目录"))

    def list_archives(self, dir_rel):
        """
        PROPFIND Depth 1 解析 multistatus，返回匹配的档案条目

        :param dir_rel (str): 目录相对路径

        :return List: 每项含 name/size/modified，按 name 升序

        :raises ServiceError: 状态码非 207/200 或 XML 无法解析
        """
        target = (dir_rel or "").strip("/") + "/"
        resp = self._request(
            "PROPFIND",
            target,
            content=_PROPFIND_BODY,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        if resp.status_code not in (200, 207):
            raise ServiceError(_friendly_http_error(resp, "读取远端列表"))
        try:
            root = ElementTree.fromstring(resp.text)
        except ElementTree.ParseError as exc:
            raise ServiceError("读取远端列表：响应不是 multistatus XML：" + str(exc))
        if not root.tag.rsplit("}", 1)[-1] == "multistatus":
            raise ServiceError("读取远端列表：响应根节点不是 multistatus")
        items = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "response":
                continue
            href = ""
            size = 0
            modified = ""
            for info in node.iter():
                tag = info.tag.rsplit("}", 1)[-1]
                if tag == "href":
                    href = (info.text or "").strip()
                elif tag == "getcontentlength":
                    size = _parse_int_lenient(info.text)
                elif tag == "getlastmodified":
                    modified = (info.text or "").strip()
            if not href or href.endswith("/"):
                continue
            nm = unquote(href.rstrip("/")).rstrip("/").rsplit("/", 1)[-1]
            if not BACKUP_NAME_RE.match(nm):
                continue
            items.append({"name": nm, "size": size, "modified": modified})
        return sorted(items, key=lambda one: one["name"])

    def put(self, rel, data):
        """
        上传单个对象

        :param rel (str): 相对路径
        :param data (bytes): 内容字节

        :raises ServiceError: 上传被拒绝或网络失败
        """
        resp = self._request("PUT", rel, content=data)
        if resp.status_code not in (200, 201, 204):
            raise ServiceError(_friendly_http_error(resp, "上传"))

    def get(self, rel):
        """
        下载单个对象

        :param rel (str): 相对路径

        :return bytes: 对象内容

        :raises NotFoundError: 远端对象不存在
        :raises ServiceError: 下载被拒绝或网络失败
        """
        resp = self._request("GET", rel)
        if resp.status_code == 404:
            raise NotFoundError("远端对象不存在")
        if resp.status_code != 200:
            raise ServiceError(_friendly_http_error(resp, "下载"))
        return resp.content

    def delete(self, rel):
        """
        删除单个对象，远端已不存在视为成功

        :param rel (str): 相对路径

        :raises ServiceError: 删除被拒绝或网络失败
        """
        resp = self._request("DELETE", rel)
        if resp.status_code not in (200, 204, 404, 405):
            raise ServiceError(_friendly_http_error(resp, "删除"))


class BackupService:
    """
    备份/恢复闭环服务：档案构建、自检、上传、清理、恢复，任务互斥
    """

    def __init__(self):
        """
        初始化互斥锁与最近一次任务结果缓存
        """
        self._task_lock = threading.Lock()
        self._last_result: Dict[str, Any] = {}
        self._transport: Optional[httpx.BaseTransport] = None

    def last_result(self) -> Dict[str, Any]:
        """
        返回最近一次备份/恢复任务结果摘要

        :return Dict: 含 kind/ok/time/name/size/error 组合的摘要
        """
        return dict(self._last_result)

    def _record(self, kind: str, ok: bool, **extra: Any) -> Dict[str, Any]:
        """
        记录任务结果缓存

        :param kind (str): backup 或 restore
        :param ok (bool): 是否成功
        :param extra (Dict): 附加字段

        :return Dict: 摘要
        """
        self._last_result = {
            "kind": kind, "ok": ok, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **extra
        }
        return self.last_result()

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        """
        SHA-256 十六进制摘要

        :param data (bytes): 输入字节

        :return str: 摘要串
        """
        return hashlib.sha256(data).hexdigest()

    def _build_client(self) -> WebdavClient:
        """
        从配置构造 WebDAV 客户端，前置校验启用与地址

        :return WebdavClient: 客户端实例

        :raises BadRequestError: 未启用、未配置 URL
        """
        cfg = config_manager.get().get("webdav", {})
        if not cfg.get("enabled"):
            raise BadRequestError("远端备份未启用，请先开启并保存配置")
        url = (cfg.get("url") or "").strip()
        if not url:
            raise BadRequestError("未配置 WebDAV 根地址")
        return WebdavClient(
            url,
            username=cfg.get("username") or "",
            password=cfg.get("password") or "",
            transport=self._transport,
        )

    @staticmethod
    def _remote_dir(cfg: Optional[Dict[str, Any]] = None) -> str:
        """
        取远端子目录配置

        :param cfg (Dict): 可选 webdav 配置字典，缺省现读配置

        :return Optional: 子目录串
        """
        cfg = cfg or config_manager.get().get("webdav", {})
        return (cfg.get("remote_subdir") or "").strip("/")

    @staticmethod
    def _keep_count(cfg: Optional[Dict[str, Any]] = None) -> int:
        """
        取远端保留份数配置

        :param cfg (Dict): 可选 webdav 配置字典

        :return int: 小于 1 按 10 处理
        """
        cfg = cfg or config_manager.get().get("webdav", {})
        return _parse_int_lenient(cfg.get("keep_count", 10)) or 10

    def _create_archive(self, staging_dir: Path) -> Path:
        """
        把配置四件套与本地快照打包成含清单的 zip 档案

        :param staging_dir (Path): 暂存目录

        :return Path: 生成的档案路径

        :raises ServiceError: 必备成员（config.key）缺失
        """
        if not (_BASE_DIR / "config.key").exists():
            raise ServiceError("config.key 不存在，无法生成可恢复的档案")
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = staging_dir / f"{BACKUP_NAME_PREFIX}{ts}{BACKUP_NAME_SUFFIX}"
        members: Dict[str, Path] = {
            "config.json": CONFIG_FILE,
            "config.key": _BASE_DIR / "config.key",
        }
        if CHECKIN_STATE_FILE.exists():
            members["checkin_state.json"] = CHECKIN_STATE_FILE
        for snap in sorted(CONFIG_BACKUP_DIR.glob("config-*.json")):
            members[f"backups/{snap.name}"] = snap
        manifest = {
            "app_version": APP_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "members": [],
        }
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname in sorted(members):
                data = members[arcname].read_bytes()
                zf.writestr(arcname, data)
                manifest["members"].append(
                    {"path": arcname, "size": len(data), "sha256": self._sha256_bytes(data)}
                )
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        return archive_path

    def _verify_archive(self, archive_path: Path) -> Tuple[bool, str]:
        """
        完整自检档案：ZIP 完整性、路径安全、必备成员、清单逐条大小与摘要核对

        :param archive_path (Path): 档案路径

        :return Tuple: (是否通过, 失败原因)
        """
        try:
            with zipfile.ZipFile(archive_path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    return False, f"ZIP损坏条目: {bad}"
                names = zf.namelist()
                for name in names:
                    if name.startswith("/") or ".." in Path(name).parts or ":" in name:
                        return False, f"含危险路径条目: {name}"
                missing = [m for m in _REQUIRED_MEMBERS if m not in names]
                if missing:
                    return False, "缺少必备成员: " + ", ".join(missing)
                try:
                    manifest = json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    return False, "manifest.json 无法解析: " + str(exc)
                declared = {
                    item.get("path"): item for item in manifest.get("members", [])
                    if isinstance(item, dict)
                }
                if not declared:
                    return False, "manifest.json 成员清单为空"
                for name, record in declared.items():
                    if name == _MANIFEST_NAME or name not in names:
                        return False, f"清单成员在实际档案中缺失: {name}"
                    data = zf.read(name)
                    if len(data) != _parse_int_lenient(record.get("size")):
                        return False, f"大小不符: {name}"
                    if self._sha256_bytes(data) != (record.get("sha256") or ""):
                        return False, f"摘要不符: {name}"
                rest = set(names) - set(declared) - {_MANIFEST_NAME}
                if rest:
                    return False, "档案含清单外文件: " + ", ".join(sorted(rest))
        except (zipfile.BadZipFile, OSError) as exc:
            return False, "档案读取失败: " + str(exc)
        extra_check = self._manifest_time_ok(manifest)
        if extra_check:
            return False, extra_check
        return True, ""

    @staticmethod
    def _manifest_time_ok(manifest: Dict[str, Any]) -> str:
        """
        清单格式轻校验：生成时间字段可解析

        :param manifest (Dict): 已解析清单

        :return str: 问题描述，空串表示通过
        """
        created = manifest.get("created_at")
        if not isinstance(created, str) or not created:
            return "manifest.json 缺少 created_at"
        return ""

    def _remote_rel(self, subdir: str, name: str) -> str:
        """
        拼接远端对象相对路径

        :param subdir (str): 子目录
        :param name (str): 档案名

        :return str: 相对路径
        """
        return f"{subdir}/{name}" if subdir else name

    def _resolve_remote_name(self, client: WebdavClient, subdir: str, base_name: str) -> str:
        """
        同名冲突时追加 -N 序号后缀（命名正则允许的 2-99）

        :param client (WebdavClient): 客户端
        :param subdir (str): 子目录
        :param base_name (str): 期望档案名

        :return str: 无冲突的档案名

        :raises ServiceError: 同秒撞名超过 99 个
        """
        existing = {item["name"] for item in client.list_archives(subdir)}
        name = base_name.rsplit("/", 1)[-1]
        if name not in existing:
            return name
        stem = name[: -len(BACKUP_NAME_SUFFIX)]
        for suffix in range(2, 100):
            candidate = f"{stem}-{suffix}{BACKUP_NAME_SUFFIX}"
            if candidate not in existing:
                return candidate
        raise ServiceError("同秒备份过于频繁，无法生成唯一档案名")

    def run_backup(self) -> Dict[str, Any]:
        """
        执行一次完整备份：打包、自检、上传、读回核对、远端滚动清理

        :return Dict: 任务结果摘要（name/size/pruned）

        :raises BadRequestError: 未启用或未配置地址
        :raises ServiceError: 忙、自检、上传或核对失败
        """
        if not self._task_lock.acquire(blocking=False):
            raise ServiceError("已有备份或恢复任务进行中，请稍候", 409)
        client: Optional[WebdavClient] = None
        staging: Optional[Path] = None
        try:
            client = self._build_client()
            cfg = config_manager.get().get("webdav", {})
            subdir = self._remote_dir(cfg)
            client.ensure_dir(subdir)
            staging = Path(tempfile.mkdtemp(prefix="115strm-backup-"))
            archive = self._create_archive(staging)
            ok, reason = self._verify_archive(archive)
            if not ok:
                raise ServiceError("档案自检失败：" + reason)
            name = self._resolve_remote_name(client, subdir, archive.name)
            rel = self._remote_rel(subdir, name)
            data = archive.read_bytes()
            client.put(rel, data)
            uploaded = client.get(rel)
            if len(uploaded) != len(data):
                raise ServiceError("上传后远端大小与本地不一致，本轮未做滚动清理")
            pruned = self._prune(client, subdir, self._keep_count(cfg))
            self._record("backup", True, name=name, size=len(data), pruned=pruned)
            return self.last_result()
        except ServiceError as exc:
            self._record("backup", False, error=str(exc))
            raise
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if client is not None:
                client.close()
            self._task_lock.release()

    def _prune(self, client: WebdavClient, subdir: str, keep: int) -> int:
        """
        删除超出保留份数的最旧档案；单文件失败仅记日志等下轮补偿

        :param client (WebdavClient): 客户端
        :param subdir (str): 子目录
        :param keep (int): 保留份数

        :return int: 实际删除数
        """
        items = client.list_archives(subdir)
        if len(items) <= keep:
            return 0
        removed = 0
        for old in items[: len(items) - keep]:
            try:
                client.delete(self._remote_rel(subdir, old["name"]))
                removed += 1
            except ServiceError as exc:
                logger.warning("清理远端旧档案失败（下轮补偿）: %s - %s", old["name"], exc)
        return removed

    def list_remote(self) -> List[Dict[str, Any]]:
        """
        查看远端档案列表

        :return List: 档案条目列表

        :raises BadRequestError: 未启用或未配置地址
        :raises ServiceError: 列表读取失败
        """
        client = self._build_client()
        try:
            client.ensure_dir(self._remote_dir())
            return client.list_archives(self._remote_dir())
        finally:
            client.close()

    def download_remote(self, name: str) -> Tuple[str, bytes]:
        """
        下载单个远端档案，名称必须通过白名单校验

        :param name (str): 档案名

        :return Tuple: (档案名, 内容字节)

        :raises BadRequestError: 名称非法
        :raises NotFoundError: 档案不存在
        :raises ServiceError: 下载失败
        """
        if not BACKUP_NAME_RE.match(name):
            raise BadRequestError("非法的档案名称")
        client = self._build_client()
        try:
            rel = self._remote_rel(self._remote_dir(), name)
            return name, client.get(rel)
        finally:
            client.close()

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """
        同目录临时文件加 os.replace 原子写

        :param path (Path): 目标路径
        :param data (bytes): 内容

        :return None:
        """
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def restore_backup(self, name: str) -> Dict[str, Any]:
        """
        从远端档案恢复：下载、全量校验、guard 快照、原子覆盖、热重载

        :param name (str): 档案名，必须通过白名单校验

        :return Dict: 任务结果摘要（name/guard）

        :raises BadRequestError: 名称非法、未启用或未配置地址
        :raises ServiceError: 校验失败保持现状，或覆盖失败时 guard 还原
        """
        if not BACKUP_NAME_RE.match(name):
            raise BadRequestError("非法的档案名称")
        if not self._task_lock.acquire(blocking=False):
            raise ServiceError("已有备份或恢复任务进行中，请稍候", 409)
        client: Optional[WebdavClient] = None
        staging: Optional[Path] = None
        try:
            client = self._build_client()
            data = client.get(self._remote_rel(self._remote_dir(), name))
            staging = Path(tempfile.mkdtemp(prefix="115strm-restore-"))
            tmp_archive = staging / name
            tmp_archive.write_bytes(data)
            ok, reason = self._verify_archive(tmp_archive)
            if not ok:
                raise ServiceError("档案校验失败，已保持当前配置：" + reason)
            key_file = _BASE_DIR / "config.key"
            with zipfile.ZipFile(tmp_archive) as zf:
                names = zf.namelist()
                new_config = zf.read("config.json")
                new_key = zf.read("config.key")
                new_state = zf.read("checkin_state.json") if "checkin_state.json" in names else None
            # 与 ConfigManager._write 持同一把锁：防止恢复覆盖与面板保存并发交叉，
            # 也保证随后的 load 读到的就是本轮恢复内容（RLock 同线程可重入）
            with config_manager._lock:
                pre_json = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else None
                pre_key = key_file.read_bytes() if key_file.exists() else None
                guard_base = "restore-guard-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                CONFIG_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                if pre_json is not None:
                    (CONFIG_BACKUP_DIR / f"{guard_base}.json").write_bytes(pre_json)
                if pre_key is not None:
                    (CONFIG_BACKUP_DIR / f"{guard_base}.key").write_bytes(pre_key)
                try:
                    self._atomic_write_bytes(CONFIG_FILE, new_config)
                    self._atomic_write_bytes(key_file, new_key)
                    if new_state is not None:
                        self._atomic_write_bytes(CHECKIN_STATE_FILE, new_state)
                except OSError as exc:
                    self._undo_pre(pre_json, pre_key, key_file)
                    raise ServiceError("恢复中途失败，已还原到恢复前配置：" + str(exc))
            config_manager.load()
            self._rotate_guards()
            self._record("restore", True, name=name, guard=guard_base)
            return self.last_result()
        except ServiceError as exc:
            self._record("restore", False, name=name, error=str(exc))
            raise
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if client is not None:
                client.close()
            self._task_lock.release()

    @staticmethod
    def _undo_pre(pre_json: Optional[bytes], pre_key: Optional[bytes], key_file: Path) -> None:
        """
        用恢复前内存副本把配置文件与密钥写回（还原兜底）

        :param pre_json-pre_key (bytes): 恢复前 config.json 与 config.key 内容，None 表示原本不存在
        :param key_file (Path): 密钥路径

        :return None:
        """
        if pre_json is not None:
            BackupService._atomic_write_bytes(CONFIG_FILE, pre_json)
        else:
            CONFIG_FILE.unlink(missing_ok=True)
        if pre_key is not None:
            BackupService._atomic_write_bytes(key_file, pre_key)
        else:
            key_file.unlink(missing_ok=True)

    @staticmethod
    def _rotate_guards() -> None:
        """
        guard 快照仅保留最近 5 组，旧组成对清理

        :return None:
        """
        guards = sorted(CONFIG_BACKUP_DIR.glob("restore-guard-*.json"))
        for old in guards[:-5]:
            old.unlink(missing_ok=True)
            (CONFIG_BACKUP_DIR / (old.stem + ".key")).unlink(missing_ok=True)


backup_service = BackupService()
