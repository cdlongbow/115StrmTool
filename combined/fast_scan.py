"""
快速整树扫描：以"递归文件列表 + 递归目录表"两条分页端点流合成文件属性，
请求数由逐目录遍历的 O(目录数) 降为根级常数；任一环节不可用时抛出
FastScanUnavailable，由调用方逐根降级回 DFS 遍历

端点形态参考 115-station fast115.go：
- webapi /files?show_dir=0 一次返回子树内全部文件（1150/页）
- proapi /app/chrome/downfolders 一次返回子树内全部目录（5000/页）
- 目录 pickcode 由首文件页条目的 pc 反推动不动点后本地换算，零额外请求
"""
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

from logger import logger

FILES_PAGE_SIZE = 1150
DIRS_PAGE_SIZE = 5000
MAX_DIRS_PAGES = 500


class FastScanUnavailable(Exception):
    """
    快速扫描不可用（端点失败、响应异常、pickcode 不可推导、目录映射缺失），
    调用方捕获后应降级为逐目录 DFS 遍历
    """


def _build_path_getter(
    root_cid: int, dir_map: Dict[str, Tuple[str, str]]
) -> Callable[[str], Optional[str]]:
    """
    构建"目录 cid -> (相对根目录的路径，不含文件名)"的带缓存函数

    :param root_cid (int): 同步根目录 cid
    :param dir_map (Dict): 目录 cid 到 (目录名, 父目录 cid) 的映射

    :return Callable: 输入目录 cid，返回相对路径；映射缺失返回 None
    """
    root = str(root_cid)
    cache: Dict[str, Optional[str]] = {root: ""}

    def path_of(cid: str) -> Optional[str]:
        if cid in cache:
            return cache[cid]
        parts: List[str] = []
        cursor = cid
        while cursor != root:
            node = dir_map.get(cursor)
            if node is None:
                cache[cid] = None
                return None
            name, parent = node
            parts.append(name)
            cursor = parent
        cache[cid] = "/" + "/".join(reversed(parts)) if parts else ""
        return cache[cid]

    return path_of


def iter_files_fast(
    client_wrapper: Any,
    root_cid: int,
    root_pan_path: str = "",
    cancellation: Optional[Event] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    """
    快速整树遍历同步根，产出与 _iter_files_115 同构的文件属性字典列表

    拉取阶段整体执行：先翻完递归文件页（借首条 pc 推导不动点并换算根
    目录 pickcode），再翻递归目录表，两者在内存合成完整网盘路径后返回。
    任一环节失败抛 FastScanUnavailable，由调用方降级；拉取中途收到取消
    信号则返回已获得的部分条目（调用方取消守卫保证后续不会误删）。

    :param client_wrapper (Any): P115ClientWrapper 实例，需暴露 scan_get_json
    :param root_cid (int): 同步根目录 cid
    :param root_pan_path (str): 同步根网盘完整路径，用于拼接文件绝对路径
    :param cancellation (Event): 取消信号，置位后停止分页
    :param progress (Callable): 进度回调 (已收文件数, 声明总数)

    :return List: 文件属性字典列表，键含 name/size/sha1/pickcode/path/is_dir

    :raises FastScanUnavailable: 快速扫描任一环节不可用
    """
    entries, total = _fetch_all_files(client_wrapper, root_cid, cancellation, progress)
    if cancellation is not None and cancellation.is_set():
        return []
    dir_pickcode = _derive_root_pickcode(root_cid, entries)
    dir_map = _fetch_dir_map(client_wrapper, dir_pickcode, root_cid)
    path_of = _build_path_getter(root_cid, dir_map)

    root_prefix = root_pan_path.rstrip("/")
    results: List[Dict[str, Any]] = []
    for entry in entries:
        pickcode = str(entry.get("pc") or "")
        if not pickcode or pickcode.startswith("f"):
            continue
        parent = str(entry.get("cid") or "")
        rel = path_of(parent)
        if rel is None:
            raise FastScanUnavailable(
                f"目录映射缺失 cid={parent}，文件 {pickcode} 无法拼路径"
            )
        name = str(entry.get("n") or "")
        results.append({
            "name": name,
            "size": _parse_size(entry.get("s")),
            "sha1": str(entry.get("sha1") or ""),
            "pickcode": pickcode,
            "path": f"{root_prefix}{rel}/{name}" if name else "",
            "is_dir": False,
        })
    logger.info(
        "快速扫描完成: root_cid=%s 文件=%d 目录=%d",
        root_cid, len(results), len(dir_map),
    )
    return results


def _fetch_all_files(
    client_wrapper: Any,
    root_cid: int,
    cancellation: Optional[Event],
    progress: Optional[Callable[[int, int], None]],
) -> Tuple[List[Dict[str, Any]], int]:
    """
    翻完递归文件列表全部分页

    :param client_wrapper (Any): P115ClientWrapper 实例
    :param root_cid (int): 同步根目录 cid
    :param cancellation (Event): 取消信号
    :param progress (Callable): 进度回调 (已收, 总数)

    :return Tuple: (全部文件条目, 声明总数)

    :raises FastScanUnavailable: 请求失败或结构异常
    """
    collected: List[Dict[str, Any]] = []
    offset = 0
    total = -1
    while True:
        if cancellation is not None and cancellation.is_set():
            return collected, max(total, len(collected))
        data = _scan_get(
            client_wrapper,
            "files",
            {
                "aid": "1",
                "cid": str(root_cid),
                "show_dir": "0",
                "o": "user_ptime",
                "asc": "0",
                "offset": str(offset),
                "limit": str(FILES_PAGE_SIZE),
                "format": "json",
            },
        )
        page = data.get("data")
        if not isinstance(page, list):
            raise FastScanUnavailable(f"递归文件列表结构异常: data={type(page).__name__}")
        try:
            total = int(data.get("count") or 0)
        except (TypeError, ValueError):
            raise FastScanUnavailable("递归文件列表 count 字段异常")
        if not page:
            if 0 < total > len(collected):
                logger.warning(
                    "快速扫描空页提前结束: 已收 %d/%d", len(collected), total,
                )
            break
        collected.extend(item for item in page if isinstance(item, dict))
        offset += len(page)
        if progress is not None:
            progress(len(collected), total)
        if total and len(collected) >= total:
            break
    return collected, max(total, len(collected))


def _fetch_dir_map(
    client_wrapper: Any,
    dir_pickcode: str,
    root_cid: int,
) -> Dict[str, Tuple[str, str]]:
    """
    翻完递归目录表，构建 目录cid -> (目录名, 父目录cid) 映射

    :param client_wrapper (Any): P115ClientWrapper 实例
    :param dir_pickcode (str): 同步根目录 pickcode
    :param root_cid (int): 同步根目录 cid

    :return Dict: 目录映射（含根节点自身占位）

    :raises FastScanUnavailable: 请求失败、结构异常或页数超限
    """
    dir_map: Dict[str, Tuple[str, str]] = {str(root_cid): ("", "")}
    for page_no in range(1, MAX_DIRS_PAGES + 1):
        data = _scan_get(
            client_wrapper,
            "downfolders",
            {
                "pickcode": dir_pickcode,
                "page": str(page_no),
                "per_page": str(DIRS_PAGE_SIZE),
            },
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise FastScanUnavailable(f"目录表结构异常: data={type(payload).__name__}")
        listing = payload.get("list")
        if not isinstance(listing, list):
            raise FastScanUnavailable("目录表 list 字段异常")
        for item in listing:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("fid") or "")
            if not fid or fid == str(root_cid):
                continue
            dir_map[fid] = (str(item.get("fn") or ""), str(item.get("pid") or ""))
        if not payload.get("has_next_page"):
            return dir_map
    raise FastScanUnavailable(f"目录表页数超过上限 {MAX_DIRS_PAGES}")


def _derive_root_pickcode(root_cid: int, entries: List[Dict[str, Any]]) -> str:
    """
    由任一文件条目的 pickcode 反推动不动点，本地换算同步根目录 pickcode

    :param root_cid (int): 同步根目录 cid
    :param entries (List): 递归文件列表条目

    :return str: 同步根目录 pickcode

    :raises FastScanUnavailable: 无可用条目或换算库异常
    """
    from p115pickcode import get_stable_point, id_to_pickcode

    for entry in entries:
        pc = str(entry.get("pc") or "")
        if not pc or pc.startswith("f"):
            continue
        try:
            stable_point = get_stable_point(pc)
            return id_to_pickcode(root_cid, stable_point, prefix="fa")
        except Exception as e:
            logger.debug("跳过不可用 pickcode 条目: %s", e)
    raise FastScanUnavailable("首文件页中无可用 pickcode，无法推导目录 pickcode")


def _scan_get(client_wrapper: Any, endpoint: str, params: Dict[str, str]) -> Dict[str, Any]:
    """
    经 wrapper 的统一入口发起扫描类 GET 请求并校验 state

    :param client_wrapper (Any): P115ClientWrapper 实例
    :param endpoint (str): 端点名（files / downfolders）
    :param params (Dict): 查询参数

    :return Dict: 校验 state 为真的响应体

    :raises FastScanUnavailable: 请求异常、HTTP 非 200、405 或 state 为假
    """
    try:
        data = client_wrapper.scan_get_json(endpoint, params)
    except FastScanUnavailable:
        raise
    except Exception as e:
        raise FastScanUnavailable(f"{endpoint} 请求失败: {e}") from e
    if not isinstance(data, dict):
        raise FastScanUnavailable(f"{endpoint} 响应结构异常")
    state = data.get("state")
    if state is False or state == "false":
        raise FastScanUnavailable(f"{endpoint} 被拒绝: {str(data.get('error') or data)[:120]}")
    return data


def _parse_size(raw: Any) -> int:
    """
    解析 115 文件大小字段（可能为字符串或数字）

    :param raw (Any): 原始 size 值

    :return int: 字节数，解析失败为 0
    """
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0
