"""Telegram: kirim pesan + listener command (long-polling, cuma chat ID kamu)."""
from __future__ import annotations

import time
from typing import Callable

import requests

_STRIP = "*_[]`"


def clean(text: str) -> str:
    """Pesan dikirim tanpa parse_mode, tapi tetap buang karakter markdown nyasar."""
    return "".join(c for c in text if c not in _STRIP)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.enabled = bool(token and chat_id)
        self._last_send = 0.0

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        # Telegram nge-limit ~1 pesan/detik per chat.
        wait = 1.1 - (time.time() - self._last_send)
        if wait > 0:
            time.sleep(wait)
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
            try:
                requests.post(self._url("sendMessage"), timeout=10,
                              json={"chat_id": self.chat_id, "text": clean(chunk)})
            except requests.RequestException:
                pass
        self._last_send = time.time()

    def listen(self, handlers: dict[str, Callable[[list[str]], str]],
               stop: Callable[[], bool] = lambda: False) -> None:
        if not self.enabled:
            return
        offset = None
        while not stop():
            try:
                r = requests.get(self._url("getUpdates"), timeout=40,
                                 params={"timeout": 30, "offset": offset})
                updates = r.json().get("result", [])
            except (requests.RequestException, ValueError):
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                    continue          # chat lain diabaikan
                parts = (msg.get("text") or "").strip().split()
                if not parts or not parts[0].startswith("/"):
                    continue
                cmd = parts[0][1:].split("@")[0].lower()
                fn = handlers.get(cmd) or handlers.get("help")
                try:
                    reply = fn(parts[1:]) if fn else "?"
                except Exception as e:  # noqa: BLE001
                    reply = f"error: {e}"
                self.send(reply)
