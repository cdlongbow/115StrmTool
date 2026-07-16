from json import dump as json_dump, load as json_load
from pathlib import Path
from random import uniform
from re import fullmatch as re_fullmatch
from threading import Lock, Thread
from time import sleep
from typing import Dict, Optional, Tuple
import sys

from logger import logger

if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent

CHECKIN_STATE_FILE = _BASE_DIR / "checkin_state.json"
CHECKIN_POLL_INTERVAL = 60
CHECKIN_DEFAULT_WINDOW = "06:00-09:00"
CHECKIN_MAX_RETRIES = 3
CHECKIN_RETRY_DELAY = 3


def run_p115_checkin_once(client) -> Tuple[bool, str]:
    """
    执行 115 单次签到

    :param client: P115ClientWrapper 实例
    :return: (是否成功, 说明文案)
    """
    if not client:
        return False, "客户端未初始化"
    try:
        logger.info("【115 签到】查询今日签到状态...")
        status_resp = client.user_points_sign()
        if isinstance(status_resp, dict):
            data = status_resp.get("data") or {}
            if data.get("is_sign_today") == 1:
                return True, "今日已签到，无需重复签到"

        logger.info("【115 签到】执行签到...")
        for attempt in range(1, CHECKIN_MAX_RETRIES + 1):
            try:
                resp = client.user_points_sign_post()
                if isinstance(resp, dict):
                    d2 = resp.get("data") or {}
                    cd = d2.get("continuous_day", 0)
                    pn = d2.get("points_num", 0)
                    detail = f"签到成功，连续签到 {cd} 天，获得 {pn} 积分"
                    logger.info("【115 签到】%s", detail)
                    return True, detail
            except Exception as e:
                logger.warning("【115 签到】第 %d/%d 次失败: %s",
                               attempt, CHECKIN_MAX_RETRIES, e)
                if attempt < CHECKIN_MAX_RETRIES:
                    sleep(CHECKIN_RETRY_DELAY)
        return False, "签到失败，已达最大重试次数"
    except Exception as e:
        logger.error("【115 签到】异常: %s", e, exc_info=True)
        return False, str(e)


class CheckinScheduler:
    def __init__(self):
        self._lock = Lock()
        self._state: Dict = self._load_state()
        self._client = None
        self._thread: Optional[Thread] = None
        self._running = False

    def set_client(self, client) -> None:
        self._client = client

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("签到调度器已启动")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.error("签到调度异常", exc_info=True)
            for _ in range(CHECKIN_POLL_INTERVAL):
                if not self._running:
                    return
                sleep(1)

    def _tick(self) -> None:
        from config_manager import config_manager
        cfg = config_manager.get().get("checkin", {})
        enabled = cfg.get("enabled", False)
        if not enabled or not self._client:
            return

        from datetime import date, datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")

        last_done = self._state.get("last_done_date") or ""
        next_ts = self._state.get("next_run_ts")

        if last_done == today_str:
            if next_ts is not None:
                nr = datetime.fromtimestamp(next_ts, tz=tz)
                if nr.date() > now.date():
                    return
            tomorrow_d = (now + timedelta(days=1)).date()
            self._set_next_run(self._random_epoch(tomorrow_d, cfg))
            return

        if next_ts is None:
            nxt = self._pick_next(now, cfg)
            self._save_state_field("next_run_ts", nxt)
            logger.debug("【115 签到】已安排下次执行 %s",
                         datetime.fromtimestamp(nxt, tz=tz).strftime("%Y-%m-%d %H:%M"))
            next_ts = nxt

        if now.timestamp() < next_ts:
            return

        ok, detail = run_p115_checkin_once(self._client)
        if ok:
            self._save_state_field("last_done_date", today_str)
            self._save_state_field("last_detail", detail)
            tomorrow_d = (now + timedelta(days=1)).date()
            self._set_next_run(self._random_epoch(tomorrow_d, cfg))
        else:
            self._save_state_field("next_run_ts", None)
            self._save_state_field("last_detail", detail)

    def manual_checkin(self) -> Tuple[bool, str]:
        if not self._client:
            return False, "客户端未初始化"
        return run_p115_checkin_once(self._client)

    def get_status(self) -> Dict:
        from config_manager import config_manager
        cfg = config_manager.get().get("checkin", {})
        enabled = cfg.get("enabled", False)
        time_range = cfg.get("time_range", CHECKIN_DEFAULT_WINDOW)

        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")
        last_done = self._state.get("last_done_date") or ""
        next_ts = self._state.get("next_run_ts")
        next_time = ""
        if next_ts:
            next_time = datetime.fromtimestamp(next_ts, tz=tz).strftime("%Y-%m-%d %H:%M")
        return {
            "enabled": enabled,
            "signed_today": last_done == today_str,
            "last_done": last_done,
            "next_run": next_time,
            "last_detail": self._state.get("last_detail") or "",
            "time_range": time_range,
        }

    @staticmethod
    def _parse_window(time_range: str, d) -> Tuple:
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        s = (time_range or CHECKIN_DEFAULT_WINDOW).strip()
        m = re_fullmatch(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", s)
        if not m:
            h1, m1, h2, m2 = 6, 0, 9, 0
        else:
            h1, m1, h2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        start = datetime(d.year, d.month, d.day, h1, m1, 0, tzinfo=tz)
        end = datetime(d.year, d.month, d.day, h2, m2, 0, tzinfo=tz)
        return start, end

    def _random_epoch(self, d, cfg: Dict) -> float:
        tr = cfg.get("time_range", CHECKIN_DEFAULT_WINDOW)
        ws, we = self._parse_window(tr, d)
        return uniform(ws.timestamp(), we.timestamp())

    def _pick_next(self, now, cfg: Dict) -> float:
        from datetime import timedelta
        tr = cfg.get("time_range", CHECKIN_DEFAULT_WINDOW)
        today = now.date()
        ws, we = self._parse_window(tr, today)
        tomorrow = today + timedelta(days=1)
        wsn, wen = self._parse_window(tr, tomorrow)

        if now < ws:
            return uniform(ws.timestamp(), we.timestamp())
        if now < we:
            return uniform(max(now.timestamp(), ws.timestamp()), we.timestamp())
        return uniform(wsn.timestamp(), wen.timestamp())

    def _set_next_run(self, epoch: float) -> None:
        self._save_state_field("next_run_ts", epoch)

    def _save_state_field(self, key: str, value) -> None:
        with self._lock:
            self._state[key] = value
            self._write_state()

    def _load_state(self) -> Dict:
        if CHECKIN_STATE_FILE.exists():
            try:
                with open(CHECKIN_STATE_FILE, "r", encoding="utf-8") as f:
                    return json_load(f)
            except Exception:
                pass
        return {}

    def _write_state(self) -> None:
        try:
            with open(CHECKIN_STATE_FILE, "w", encoding="utf-8") as f:
                json_dump(self._state, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("签到状态保存失败: %s", e)


checkin_scheduler = CheckinScheduler()