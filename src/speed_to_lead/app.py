"""Webhook receiver: POST /leads from any website form."""

import hmac
import logging
import os
import time

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from .crm import CRM, sync_lead
from .models import Lead
from .notify import Notifier
from .store import Store

log = logging.getLogger("speed_to_lead")


def make_crm(name: str, store: Store) -> CRM:
    if name == "hubspot":
        from .crm.hubspot import HubSpot
        return HubSpot(os.environ["HUBSPOT_TOKEN"], store=store)
    if name == "salesforce":
        from .crm.salesforce import Salesforce
        return Salesforce(os.environ["SALESFORCE_INSTANCE_URL"], os.environ["SALESFORCE_CLIENT_ID"],
                          os.environ["SALESFORCE_CLIENT_SECRET"], store=store)
    if name == "dryrun":
        from .crm.fake import FakeCRM
        return FakeCRM()
    raise ValueError(f"unknown CRM {name!r}")


def create_app(crm: CRM, store: Store, notifier: Notifier, secret: str = "") -> FastAPI:
    app = FastAPI(title="speed-to-lead")

    def process(lead: Lead) -> None:
        try:
            result = sync_lead(lead, crm)
        except Exception as e:  # noqa: BLE001 - record any failure; the lead stays in the log for a retry
            log.exception("lead %s failed", lead.key)
            store.fail(lead.key, f"{type(e).__name__}: {e}")
            notifier.send(f"⚠️ Lead NOT saved to {crm.name}: {lead.email} ({type(e).__name__}). Check the log.")
            return
        ms = store.finish(lead.key, result)
        who = f"{lead.first_name} {lead.last_name}".strip() or lead.email
        notifier.send(f"New lead in {crm.name} in {ms / 1000:.1f}s: {who}"
                      f"{' · ' + lead.company if lead.company else ''} · deal {result.deal_id}")

    @app.post("/leads", status_code=202)
    def receive(lead: Lead, background: BackgroundTasks, x_webhook_secret: str = Header(default="")):
        if secret and not hmac.compare_digest(x_webhook_secret, secret):
            raise HTTPException(401, "bad secret")
        if not store.claim(lead.key, lead.email, crm.name, time.time()):
            return {"status": "duplicate", "key": lead.key}
        background.add_task(process, lead)
        return {"status": "accepted", "key": lead.key}

    @app.get("/leads/{key}")
    def status(key: str):
        row = store.get(key)
        if not row:
            raise HTTPException(404)
        return row

    @app.get("/stats")
    def stats():
        return store.stats()

    @app.get("/health")
    def health():
        return {"ok": True, "crm": crm.name}

    return app


def main_app() -> FastAPI:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = Store(os.getenv("DB_PATH", "data/leads.db"))
    return create_app(
        crm=make_crm(os.getenv("CRM", "dryrun"), store),
        store=store,
        notifier=Notifier(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")),
        secret=os.getenv("WEBHOOK_SECRET", ""),
    )
