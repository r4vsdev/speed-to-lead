"""Tell the sales rep a lead arrived: Slack and/or Telegram if configured, else the log."""

import logging

import httpx

log = logging.getLogger(__name__)


def slack_escape(text: str) -> str:
    """Form text is written by strangers: '<!channel>' or '<https://evil|click>' must show as plain text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = "", slack_webhook: str = "",
                 client: httpx.Client | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.slack_webhook = slack_webhook
        self.client = client or httpx.Client(timeout=10)

    def send(self, text: str, slack_blocks: list | None = None) -> None:
        """text: plain version (Telegram, log, Slack fallback). slack_blocks: rich Slack layout."""
        sent = False
        if self.slack_webhook:
            payload = {"text": slack_escape(text)}
            if slack_blocks:
                payload["blocks"] = slack_blocks
            sent |= self._post(self.slack_webhook, payload, "Slack")
        if self.bot_token and self.chat_id:
            sent |= self._post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                               {"chat_id": self.chat_id, "text": text}, "Telegram")
        if not sent:
            log.info("ALERT %s", text)

    def _post(self, url: str, payload: dict, channel: str) -> bool:
        try:
            self.client.post(url, json=payload).raise_for_status()
            return True
        except httpx.HTTPError as e:
            # An alert failing must never lose the lead: it is already in the CRM.
            # Log the channel name only: both URLs contain secrets.
            log.warning("%s alert failed: %s", channel, type(e).__name__)
            return False


def lead_blocks(*, who: str, company: str, crm: str, crm_ms: int, q, link: str = "") -> list:
    """Slack Block Kit message for a qualified lead. All form-derived text is escaped."""
    s, e, o = q.score, q.enrichment, q.owner
    icon = {"A": "🔥", "B": "👍", "C": "🧊", "spam": "🚫"}[s.grade]
    title = f"{icon} *{slack_escape(who)}*" + (f" · {slack_escape(company)}" if company else "")
    owner = o.slack or slack_escape(o.name)  # o.slack is a mention from our own config, not from the form
    fields = [f"*Score*\n{s.points}/100 (grade {s.grade})", f"*Owner*\n{owner}",
              f"*In {crm}*\n{crm_ms / 1000:.1f}s", f"*Industry*\n{slack_escape(e.industry or '—')}"]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": title}},
              {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]}]
    if e.summary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{slack_escape(e.summary)}_"}})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": slack_escape("Why: " + "; ".join(s.reasons))[:2900]}]})
    if link:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": f"Open in {crm}"}, "url": link}]})
    return blocks
