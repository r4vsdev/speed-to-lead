import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from speed_to_lead.app import RateLimiter, create_app
from speed_to_lead.crm import sync_lead
from speed_to_lead.crm.fake import FakeCRM
from speed_to_lead.http import ApiError, request
from speed_to_lead.models import Lead
from speed_to_lead.notify import Notifier
from speed_to_lead.store import Store

LEAD = {"email": "Ana@Acme.io", "first_name": "Ana", "last_name": "Silva", "company": "Acme",
        "message": "Need a quote", "submission_id": "form-1"}


# --- models ---

def test_domain_from_website_then_work_email():
    assert Lead(email="a@x.com", website="https://www.Acme.io/about").domain == "acme.io"
    assert Lead(email="a@acme.io").domain == "acme.io"
    assert Lead(email="a@gmail.com").domain == ""


def test_key_uses_submission_id_else_content_hash():
    assert Lead(**LEAD).key == "sub:form-1"
    a = Lead(email="a@acme.io", message="hi")
    assert a.key == Lead(email="A@acme.io", message="hi").key
    assert a.key != Lead(email="a@acme.io", message="other").key


# --- sync logic ---

def test_sync_creates_company_contact_deal_and_note():
    crm = FakeCRM()
    r = sync_lead(Lead(**LEAD), crm)
    assert r.contact_created
    assert crm.companies[r.company_id]["domain"] == "acme.io"
    assert (r.contact_id, r.company_id) in crm.links
    assert crm.deals[r.deal_id]["contact"] == r.contact_id
    assert crm.notes[0]["body"] == "Need a quote"


def test_returning_person_updates_contact_and_reuses_company():
    crm = FakeCRM()
    first = sync_lead(Lead(**LEAD), crm)
    second = sync_lead(Lead(**{**LEAD, "phone": "+1 555", "submission_id": "form-2"}), crm)
    assert second.contact_id == first.contact_id and not second.contact_created
    assert second.company_id == first.company_id
    assert len(crm.contacts) == 1 and len(crm.companies) == 1
    assert len(crm.deals) == 2  # a new enquiry is a new deal
    assert crm.contacts[first.contact_id]["phone"] == "+1 555"


def test_free_email_without_website_gets_no_company():
    crm = FakeCRM()
    r = sync_lead(Lead(email="joe@gmail.com", first_name="Joe"), crm)
    assert r.company_id is None and not crm.links


# --- retries ---

@respx.mock
def test_retries_429_using_retry_after():
    route = respx.get("https://api.test/x").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    waits = []
    resp = request(httpx.Client(), "GET", "https://api.test/x", sleep=waits.append)
    assert resp.json() == {"ok": 1} and route.call_count == 3
    assert waits == [2.0, 1.0]


@respx.mock
def test_does_not_retry_bad_request():
    route = respx.get("https://api.test/x").mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(ApiError) as e:
        request(httpx.Client(), "GET", "https://api.test/x", sleep=lambda s: None)
    assert e.value.status == 400 and route.call_count == 1


@respx.mock
def test_gives_up_after_attempts():
    route = respx.get("https://api.test/x").mock(return_value=httpx.Response(500))
    with pytest.raises(ApiError):
        request(httpx.Client(), "GET", "https://api.test/x", attempts=3, sleep=lambda s: None)
    assert route.call_count == 3


# --- webhook ---

class Boom(FakeCRM):
    def create_deal(self, *a):
        raise RuntimeError("CRM down")


def make(crm=None, secret="", **kw):
    store = Store(":memory:")
    alerts = []
    notifier = Notifier()
    notifier.send = alerts.append
    crm = crm or FakeCRM()
    return TestClient(create_app(crm, store, notifier, secret, **kw)), store, crm, alerts


def test_webhook_syncs_and_times_the_lead():
    client, store, crm, alerts = make()
    r = client.post("/leads", json=LEAD)
    assert r.status_code == 202 and r.json()["status"] == "accepted"
    row = client.get("/leads/sub:form-1").json()
    assert row["status"] == "done" and row["deal_id"] and row["ms"] >= 0
    assert "New lead in dryrun" in alerts[0]
    assert client.get("/stats").json()["done"] == 1


def test_resent_webhook_is_ignored():
    client, store, crm, alerts = make()
    client.post("/leads", json=LEAD)
    r = client.post("/leads", json=LEAD)
    assert r.json()["status"] == "duplicate"
    assert len(crm.deals) == 1 and len(alerts) == 1


def test_secret_required_when_set():
    client, *_ = make(secret="s3cret")
    assert client.post("/leads", json=LEAD).status_code == 401
    assert client.post("/leads", json=LEAD, headers={"X-Webhook-Secret": "s3cret"}).status_code == 202


def test_lead_details_and_stats_need_the_secret():
    client, *_ = make(secret="s3cret")
    ok = {"X-Webhook-Secret": "s3cret"}
    client.post("/leads", json=LEAD, headers=ok)
    assert client.get("/leads/sub:form-1").status_code == 401
    assert client.get("/stats").status_code == 401
    assert client.get("/leads/sub:form-1", headers=ok).json()["status"] == "done"
    assert client.get("/health").json() == {"ok": True}  # no CRM name or data leaked


def test_real_crm_refuses_to_start_without_secret():
    class Real(FakeCRM):
        name = "hubspot"
    with pytest.raises(RuntimeError):
        make(crm=Real(), secret="")


def test_rate_limit_blocks_floods_and_secret_guessing():
    client, *_ = make(secret="s3cret", rate_limit=2)
    codes = [client.post("/leads", json={**LEAD, "submission_id": str(i)}, headers={"X-Webhook-Secret": "guess"}).status_code
             for i in range(3)]
    assert codes == [401, 401, 429]


def test_rate_limiter_window_expires():
    now = [0.0]
    rl = RateLimiter(1, window=60, clock=lambda: now[0])
    assert rl.allow("ip") and not rl.allow("ip")
    now[0] = 61
    assert rl.allow("ip")


def test_api_docs_are_not_exposed():
    client, *_ = make()
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_invalid_email_rejected():
    client, *_ = make()
    assert client.post("/leads", json={"email": "not-an-email"}).status_code == 422


def test_crm_failure_is_recorded_and_alerted():
    client, store, crm, alerts = make(crm=Boom())
    client.post("/leads", json=LEAD)
    row = store.get("sub:form-1")
    assert row["status"] == "failed" and "CRM down" in row["error"]
    assert alerts[0].startswith("⚠️")
