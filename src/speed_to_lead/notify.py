"""Tell the sales rep a lead arrived. Telegram if configured, else the log."""

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = "", client: httpx.Client | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=10)

    def send(self, text: str) -> None:
        if not (self.bot_token and self.chat_id):
            log.info("ALERT %s", text)
            return
        try:
            self.client.post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                             json={"chat_id": self.chat_id, "text": text})
        except httpx.HTTPError as e:
            # An alert failing must never lose the lead: it is already in the CRM.
            log.warning("alert failed: %s", e)
