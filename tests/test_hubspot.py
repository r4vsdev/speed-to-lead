import json

import httpx
import respx

from speed_to_lead.crm import sync_lead
from speed_to_lead.crm.hubspot import BASE, HubSpot
from speed_to_lead.models import Lead

LEAD = Lead(email="ana@acme.io", first_name="Ana", company="Acme", message="Quote please")


def hs():
    return HubSpot("test-token", sleep=lambda s: None)


def search(obj, results):
    return respx.post(f"{BASE}/crm/v3/objects/{obj}/search").mock(
        return_value=httpx.Response(200, json={"results": results}))


@respx.mock
def test_new_lead_creates_everything_with_associations():
    search("companies", [])
    search("contacts", [])
    company = respx.post(f"{BASE}/crm/v3/objects/companies").mock(return_value=httpx.Response(201, json={"id": "10"}))
    contact = respx.post(f"{BASE}/crm/v3/objects/contacts").mock(return_value=httpx.Response(201, json={"id": "20"}))
    link = respx.put(f"{BASE}/crm/v4/objects/contacts/20/associations/default/companies/10").mock(
        return_value=httpx.Response(200, json={}))
    deal = respx.post(f"{BASE}/crm/v3/objects/deals").mock(return_value=httpx.Response(201, json={"id": "30"}))
    note = respx.post(f"{BASE}/crm/v3/objects/notes").mock(return_value=httpx.Response(201, json={"id": "40"}))

    r = sync_lead(LEAD, hs())

    assert (r.company_id, r.contact_id, r.deal_id, r.contact_created) == ("10", "20", "30", True)
    assert json.loads(company.calls[0].request.content)["properties"] == {"domain": "acme.io", "name": "Acme"}
    assert contact.calls[0].request.headers["Authorization"] == "Bearer test-token"
    body = json.loads(deal.calls[0].request.content)
    assert body["properties"]["dealname"] == "Acme – inbound (website)"
    assert {a["to"]["id"] for a in body["associations"]} == {"20", "10"}
    assert link.called and note.called


@respx.mock
def test_existing_contact_and_company_are_reused():
    search("companies", [{"id": "10"}])
    search("contacts", [{"id": "20"}])
    patch = respx.patch(f"{BASE}/crm/v3/objects/contacts/20").mock(return_value=httpx.Response(200, json={"id": "20"}))
    create = respx.post(f"{BASE}/crm/v3/objects/contacts")
    respx.put(f"{BASE}/crm/v4/objects/contacts/20/associations/default/companies/10").mock(
        return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/crm/v3/objects/deals").mock(return_value=httpx.Response(201, json={"id": "30"}))
    respx.post(f"{BASE}/crm/v3/objects/notes").mock(return_value=httpx.Response(201, json={"id": "40"}))

    r = sync_lead(LEAD, hs())

    assert r.contact_id == "20" and not r.contact_created
    assert patch.called and not create.called


@respx.mock
def test_search_lag_conflict_falls_back_to_update():
    """Contact created seconds ago: search misses it, create returns 409 with the existing id."""
    search("contacts", [])
    respx.post(f"{BASE}/crm/v3/objects/contacts").mock(return_value=httpx.Response(
        409, json={"message": "Contact already exists. Existing ID: 77"}))
    patch = respx.patch(f"{BASE}/crm/v3/objects/contacts/77").mock(return_value=httpx.Response(200, json={}))

    cid, created = hs().upsert_contact(LEAD)

    assert (cid, created) == ("77", False) and patch.called


@respx.mock
def test_company_created_seconds_ago_is_not_duplicated():
    """Search can't see a company created moments ago; the local record must stop a second create."""
    from speed_to_lead.store import Store
    search("companies", [])  # search index still empty
    create = respx.post(f"{BASE}/crm/v3/objects/companies").mock(return_value=httpx.Response(201, json={"id": "10"}))
    crm = HubSpot("t", sleep=lambda s: None, store=Store(":memory:"))
    assert crm.upsert_company(LEAD) == "10"
    assert crm.upsert_company(LEAD) == "10"
    assert create.call_count == 1


@respx.mock
def test_rate_limit_is_retried():
    respx.post(f"{BASE}/crm/v3/objects/companies/search").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"results": [{"id": "10"}]})])
    assert hs().upsert_company(LEAD) == "10"
