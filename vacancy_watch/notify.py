"""Where matches go. Telegram sends them with 👍/👎 buttons and collects the taps as labels;
without Telegram settings matches are only printed and logged."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import requests

from .config import Config

log = logging.getLogger("vacancy_watch")
OnLabel = Callable[[str, bool], None]


class Notifier:
    def send(self, text: str, job_id: str) -> None:
        log.info("🔔 " + text.replace("\n", "\n   "))

    def poll(self, on_label: OnLabel, timeout: int = 0) -> bool:
        """Collect feedback, waiting up to timeout seconds. False if this notifier cannot poll."""
        return False

    def wait(self, seconds: float, on_label: OnLabel) -> None:
        """Sleep between cycles, collecting feedback meanwhile."""
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            if not self.poll(on_label, int(min(50, left))):
                time.sleep(min(60, left))


def telegram(token: str, method: str, **kw) -> dict:
    return requests.post(f"https://api.telegram.org/bot{token}/{method}", json=kw,
                         timeout=kw.get("timeout", 0) + 30).json()


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat: str):
        self.token, self.chat, self.offset = token, chat, 0

    def send(self, text: str, job_id: str) -> None:
        r = telegram(self.token, "sendMessage", chat_id=self.chat, text=text[:4000], reply_markup={
            "inline_keyboard": [[{"text": "👍 good match", "callback_data": f"{job_id}:1"},
                                 {"text": "👎 bad match", "callback_data": f"{job_id}:0"}]]})
        if not r.get("ok"):
            log.info(f"Telegram sendMessage failed: {r.get('description')}")

    def poll(self, on_label: OnLabel, timeout: int = 0) -> bool:
        try:
            ups = telegram(self.token, "getUpdates", offset=self.offset, timeout=timeout,
                           allowed_updates=["callback_query"])
            for u in ups.get("result", []):
                self.offset = u["update_id"] + 1
                q = u.get("callback_query") or {}
                chat = str(q.get("message", {}).get("chat", {}).get("id"))
                if chat == self.chat and re.fullmatch(r"[0-9a-f]{12}:[01]", q.get("data", "")):
                    job_id, good = q["data"].split(":")
                    on_label(job_id, good == "1")
                    telegram(self.token, "answerCallbackQuery", callback_query_id=q["id"],
                             text="saved 👍" if good == "1" else "saved 👎")
            return "result" in ups
        except (requests.RequestException, ValueError):
            return False


def make_notifier(cfg: Config) -> Notifier:
    if cfg.tg_token and cfg.tg_chat:
        return TelegramNotifier(cfg.tg_token, cfg.tg_chat)
    log.info("Telegram not configured (TG_TOKEN, TG_CHAT): matches are only printed and logged")
    return Notifier()
