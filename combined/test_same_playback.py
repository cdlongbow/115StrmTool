"""
多端播放（same_playback）流程测试

同一文件已有其他 UA 的回源缓存时，应复制副本换取独立下载地址，
并在取地址完成后调度延迟清理；无并发或复制失败时回退原文件
"""
import asyncio
import time
from unittest.mock import MagicMock, patch

import httpx

API_PATH = "/api/v1/plugin/P115StrmHelper/redirect_url"
PICKCODE = "3" * 17
COPY_PC = "9" * 17
UA_FIRST = "Emby/iOS"
UA_SECOND = "Emby/Android"


def _build_service(same_playback):
    wrapper = MagicMock()
    wrapper.get_download_url_with_ua.return_value = (
        "https://cdn.115.com/file/movie.mp4?t=1800000000",
        "movie.mp4",
        int(time.time()) + 3600,
    )
    wrapper.copy_pickcode.return_value = COPY_PC
    with patch.dict("sys.modules", {"p115_client_wrapper": MagicMock()}):
        from redirect_service import RedirectService
        svc = RedirectService(
            wrapper,
            same_playback=same_playback,
            same_playback_dir="/多端播放",
        )
    svc._copy_dir_pid = 123
    return svc


def _request_seq(svc, user_agents, settle=0.0):
    """
    在同一事件循环内依序发起多个不同 UA 的跳转请求

    :param svc: 被测服务
    :param user_agents: 依序使用的 UA 列表
    :param settle: 最后一次响应后额外等待的秒数，用于等待清理任务落地

    :return List: 各请求的响应对象列表
    """
    async def _run():
        with patch("redirect_service.COPY_CLEANUP_DELAY", 0.01):
            transport = httpx.ASGITransport(app=svc.create_app())
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test",
            ) as client:
                responses = []
                for ua in user_agents:
                    responses.append(
                        await client.get(
                            API_PATH,
                            params={"pickcode": PICKCODE},
                            headers={"user-agent": ua},
                        )
                    )
                if settle > 0:
                    await asyncio.sleep(settle)
                return responses

    return asyncio.run(_run())


def test_first_ua_no_copy():
    svc = _build_service(True)
    r1 = _request_seq(svc, [UA_FIRST])[0]
    assert r1.status_code == 302
    svc._client.copy_pickcode.assert_not_called()


def test_second_ua_uses_copy_and_cleans_up():
    svc = _build_service(True)
    r1, r2 = _request_seq(svc, [UA_FIRST, UA_SECOND], settle=0.1)
    assert (r1.status_code, r2.status_code) == (302, 302)
    svc._client.copy_pickcode.assert_called_once()
    fetch_pc = svc._client.get_download_url_with_ua.call_args_list[-1].args[0]
    assert fetch_pc == COPY_PC, "第二个 UA 应以副本 pickcode 请求下载地址"
    cleanup_pcs = [c.args[0] for c in svc._client.delete_file.call_args_list]
    assert COPY_PC in cleanup_pcs, "副本应在取址后被调度删除"


def test_copy_failure_falls_back_to_original():
    svc = _build_service(True)
    svc._client.copy_pickcode.return_value = None
    _, r2 = _request_seq(svc, [UA_FIRST, UA_SECOND])
    assert r2.status_code == 302
    fetch_pc = svc._client.get_download_url_with_ua.call_args_list[-1].args[0]
    assert fetch_pc == PICKCODE, "复制失败应回退到原文件"
    svc._client.delete_file.assert_not_called()


def test_disabled_same_playback_never_copies():
    svc = _build_service(False)
    _request_seq(svc, [UA_FIRST, UA_SECOND])
    svc._client.copy_pickcode.assert_not_called()
    fetch_pc = svc._client.get_download_url_with_ua.call_args_list[-1].args[0]
    assert fetch_pc == PICKCODE


def test_copy_uses_stable_dir_pid():
    svc = _build_service(True)
    svc._copy_dir_pid = None
    svc._client.ensure_folder.return_value = 777
    _, r2 = _request_seq(svc, [UA_FIRST, UA_SECOND])
    assert r2.status_code == 302
    pid = svc._client.copy_pickcode.call_args.args[1]
    assert pid == 777
    assert svc._copy_dir_pid == 777, "目录 ID 应缓存复用"
