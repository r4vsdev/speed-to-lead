import json

import httpx
import respx

from speed_to_lead.crm import sync_lead
from speed_to_lead.crm.salesforce import API_VERSION, Salesforce, soql_quote
from speed_to_lead.models import Lead
from speed_to_lead.store import Store

INST = "https://acme-dev.my.salesforce.com"
DATA = f"{INST}/services/data/{API_VERSION}"
LEAD = Lead(email="ana@acme.io", first_name="Ana", company="Acme", message="Quote please")


def sf(store=None):
    return Salesforce(INST, "id", "secret", sleep=lambda s: None, store=store)


def token(value="tok1"):
    return respx.post(f"{INST}/services/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": value, "instance_url": INST}))


def query(results_by_object: dict):
    def handler(request):
        q = request.url.params["q"]
        obj = q.split(" FROM ")[1].split()[0]
        return httpx.Response(200, json={"records": [{"Id": i} for i in results_by_object.get(obj, [])]})
    return respx.get(f"{DATA}/query").mock(side_effect=handler)


def created(obj, new_id):
    return respx.post(f"{DATA}/sobjects/{obj}").mock(return_value=httpx.Response(201, json={"id": new_id}))


def test_soql_quote_escapes():
    assert soql_quote("O'Brien\\x") == "'O\\'Brien\\\\x'"


@respx.mock
def test_new_lead_creates_account_contact_opportunity_role_and_task():
    tok = token()
    query({})
    account = created("Account", "001A")
    contact = created("Contact", "003C")
    link = respx.patch(f"{DATA}/sobjects/Contact/003C").mock(return_value=httpx.Response(204))
    opp = created("Opportunity", "006O")
    role = created("OpportunityContactRole", "00KR")
    task = created("Task", "00TT")

    r = sync_lead(LEAD, sf())

    assert (r.contact_id, r.company_id, r.deal_id, r.contact_created) == ("003C", "001A", "006O", True)
    assert tok.call_count == 1  # token reused across calls
    assert json.loads(account.calls[0].request.content) == {"Name": "Acme", "Website": "acme.io"}
    c = json.loads(contact.calls[0].request.content)
    assert c["LastName"] == "Ana" and c["Email"] == "ana@acme.io"  # LastName is required: falls back
    assert json.loads(link.calls[0].request.content) == {"AccountId": "001A"}
    o = json.loads(opp.calls[0].request.content)
    assert o["AccountId"] == "001A" and o["StageName"] == "Prospecting" and o["CloseDate"]
    assert json.loads(role.calls[0].request.content)["ContactId"] == "003C"
    assert json.loads(task.calls[0].request.content)["WhoId"] == "003C"
    assert contact.calls[0].request.headers["Authorization"] == "Bearer tok1"


@respx.mock
def test_returning_contact_is_updated_not_duplicated():
    token()
    query({"Contact": ["003X"], "Account": ["001X"]})
    patch = respx.patch(f"{DATA}/sobjects/Contact/003X").mock(return_value=httpx.Response(204))
    create = respx.post(f"{DATA}/sobjects/Contact")

    cid, was_created = sf().upsert_contact(LEAD)

    assert (cid, was_created) == ("003X", False)
    assert not create.called and "LeadSource" not in json.loads(patch.calls[0].request.content)


@respx.mock
def test_account_remembered_locally():
    token()
    q = query({})
    account = created("Account", "001A")
    crm = sf(store=Store(":memory:"))
    assert crm.upsert_company(LEAD) == crm.upsert_company(LEAD) == "001A"
    assert account.call_count == 1 and q.call_count == 1


@respx.mock
def test_expired_token_is_refreshed_once():
    tok = respx.post(f"{INST}/services/oauth2/token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "old", "instance_url": INST}),
        httpx.Response(200, json={"access_token": "new", "instance_url": INST})])
    respx.get(f"{DATA}/query").mock(side_effect=[
        httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}]),
        httpx.Response(200, json={"records": [{"Id": "003X"}]})])
    respx.patch(f"{DATA}/sobjects/Contact/003X").mock(return_value=httpx.Response(204))

    assert sf().upsert_contact(LEAD) == ("003X", False)
    assert tok.call_count == 2
