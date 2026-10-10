"""Webhook receiver: POST /leads from any website form."""

import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import dashboard
from .crm import CRM, sync_lead
from .enrich import WebsiteEnricher
from .llm import LLM
from .models import Lead
from .notify import Notifier, lead_blocks
from .qualify import DEFAULT_ROUTING, Qualifier, Router
from .store import Store

log = logging.getLogger("speed_to_lead")


def make_crm(name: str, store: Store) -> CRM:
    if name == "hubspot":
        from .crm.hubspot import HubSpot
        return HubSpot(os.environ["HUBSPOT_TOKEN"], store=store, portal_id=os.getenv("HUBSPOT_PORTAL_ID", ""))
    if name == "salesforce":
        from .crm.salesforce import Salesforce
        return Salesforce(os.environ["SALESFORCE_INSTANCE_URL"], os.environ["SALESFORCE_CLIENT_ID"],
                          os.environ["SALESFORCE_CLIENT_SECRET"], store=store)
    if name == "dryrun":
        from .crm.fake import FakeCRM
        return FakeCRM()
    raise ValueError(f"unknown CRM {name!r}")


class RateLimiter:
    """At most `limit` requests per client IP per `window` seconds (in memory, per process)."""

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


def create_app(crm: CRM, store: Store, notifier: Notifier, secret: str, *,
               rate_limit: int = 30, limiter: RateLimiter | None = None,
               qualifier: Qualifier | None = None) -> FastAPI:
    if not secret and crm.name != "dryrun":
        # Without a secret anyone who finds the URL could write into the real CRM.
        raise RuntimeError("WEBHOOK_SECRET must be set when writing to a real CRM")
    limiter = limiter or RateLimiter(rate_limit)
    # No interactive API docs: they would advertise the endpoints to anyone.
    app = FastAPI(title="speed-to-lead", docs_url=None, redoc_url=None, openapi_url=None)

    def check_secret(x_webhook_secret: str = Header(default="")) -> None:
        if secret and not hmac.compare_digest(x_webhook_secret.encode(), secret.encode()):
            raise HTTPException(401, "bad secret")

    def check_rate(request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        if not limiter.allow(ip):
            raise HTTPException(429, "too many requests", headers={"Retry-After": str(int(limiter.window))})

    qualifier = qualifier or Qualifier(Router(DEFAULT_ROUTING, store))

    def process(lead: Lead) -> None:
        # Phase 1: the lead reaches the CRM first, in seconds, whatever happens next.
        try:
            result = sync_lead(lead, crm)
        except Exception as e:  # noqa: BLE001 - record any failure; the lead stays in the log for a retry
            log.exception("lead %s failed", lead.key)
            store.fail(lead.key, f"{type(e).__name__}: {e}")
            notifier.send(f"⚠️ Lead NOT saved to {crm.name}: {lead.email} ({type(e).__name__}). Check the log.")
            return
        ms = store.finish(lead.key, result)
        who = f"{lead.first_name} {lead.last_name}".strip() or lead.email
        plain = f"New lead in {crm.name} in {ms / 1000:.1f}s: {who}{' · ' + lead.company if lead.company else ''}"

        # Phase 2: enrich, score, route. A failure here never undoes phase 1: the rep still gets the alert.
        try:
            q = qualifier.run(lead)
            crm.record_qualification(result, q)
            store.qualify(lead.key, q)
        except Exception:  # noqa: BLE001
            log.exception("qualifying lead %s failed", lead.key)
            notifier.send(plain + f" · deal {result.deal_id} (not scored: see log)")
            return
        link = crm.record_url(result)
        notifier.send(f"{plain} · score {q.score.points} ({q.score.grade}) → {q.owner.name}"
                      + (f"\n{q.enrichment.summary}" if q.enrichment.summary else "") + (f"\n{link}" if link else ""),
                      slack_blocks=lead_blocks(who=who, company=lead.company, crm=crm.name, crm_ms=ms, q=q, link=link))

    def check_dashboard(credentials: HTTPBasicCredentials | None = Depends(HTTPBasic(auto_error=False))) -> None:
        # Browser login prompt; any username, the password is the webhook secret.
        given = credentials.password if credentials else ""
        if secret and not hmac.compare_digest(given.encode(), secret.encode()):
            raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="lead stats"'})

    @app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(check_rate), Depends(check_dashboard)])
    def dashboard_page():
        return HTMLResponse(dashboard.render(dashboard.collect(store.db), crm.name),
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    # Rate limit runs before the secret check, so guessing the secret is throttled too.
    @app.post("/leads", status_code=202, dependencies=[Depends(check_rate), Depends(check_secret)])
    def receive(lead: Lead, background: BackgroundTasks):
        if not store.claim(lead.key, lead.email, crm.name, time.time(), lead.source, lead.domain):
            return {"status": "duplicate", "key": lead.key}
        background.add_task(process, lead)
        return {"status": "accepted", "key": lead.key}

    # Lead details and stats contain personal data: same secret required.
    @app.get("/leads/{key}", dependencies=[Depends(check_secret)])
    def status(key: str):
        row = store.get(key)
        if not row:
            raise HTTPException(404)
        return row

    @app.get("/stats", dependencies=[Depends(check_secret)])
    def stats():
        return store.stats()

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main_app() -> FastAPI:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO; the Telegram URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(os.getenv("DB_PATH", "data/leads.db"))
    llm = None
    if os.getenv("LLM_BASE_URL"):
        llm = LLM(os.environ["LLM_BASE_URL"], os.getenv("LLM_MODEL", "qwen3:8b"), os.getenv("LLM_API_KEY", ""),
                  reasoning_effort=os.getenv("LLM_REASONING_EFFORT", ""))
    qualifier = Qualifier(
        Router.from_file(os.getenv("ROUTING_FILE", ""), store),
        enricher=WebsiteEnricher() if os.getenv("ENRICH_WEBSITES", "1") == "1" else None,
        llm=llm,
    )
    return create_app(
        crm=make_crm(os.getenv("CRM", "dryrun"), store),
        store=store,
        notifier=Notifier(os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""),
                          os.getenv("SLACK_WEBHOOK_URL", "")),
        secret=os.getenv("WEBHOOK_SECRET", ""),
        rate_limit=int(os.getenv("RATE_LIMIT_PER_MIN", "30")),
        qualifier=qualifier,
    )
