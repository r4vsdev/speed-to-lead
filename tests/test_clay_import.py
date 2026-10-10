import json

import httpx
import respx

from speed_to_lead.clay_import import import_accounts, read_accounts
from speed_to_lead.crm.hubspot import BASE, HubSpot
from speed_to_lead.store import Store

CSV = """﻿Domain,Name,Industry,Employee Count,Country,Description,Url,Sells To,Sales Motion,Has Free Plan,Why Fast Leads Matter,ICP Score,ICP Tier
www.Acme.io,Acme,Software Development,"1,124",US,Lead tools,https://www.linkedin.com/company/acme,B2B,hybrid,true,Trials need fast follow-up,95,A
small.io,Small,Other,12,BR,,,both,self-serve,false,,40,C
"""


def write_csv(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(CSV, encoding="utf-8")
    return str(path)


def test_reads_clay_export(tmp_path):
    a, b = read_accounts(write_csv(tmp_path))
    assert (a.domain, a.employees, a.score, a.tier) == ("acme.io", "1124", 95, "A")
    assert "Clay ICP score 95/100 (tier A)" in a.note() and "hybrid" in a.note()
    assert b.tier == "C"


def test_dry_run_writes_nothing(tmp_path):
    with respx.mock(assert_all_called=False) as mock:
        lines = import_accounts(read_accounts(write_csv(tmp_path)), HubSpot("t"), Store(":memory:"))
        assert not mock.calls
    assert lines[0].startswith("would acme.io") and lines[1].startswith("skip  small.io")


@respx.mock
def test_write_creates_company_and_note_once(tmp_path):
    respx.post(f"{BASE}/crm/v3/objects/companies/search").mock(return_value=httpx.Response(200, json={"results": []}))
    create = respx.post(f"{BASE}/crm/v3/objects/companies").mock(return_value=httpx.Response(201, json={"id": "10"}))
    patch = respx.patch(f"{BASE}/crm/v3/objects/companies/10").mock(return_value=httpx.Response(200, json={}))
    note = respx.post(f"{BASE}/crm/v3/objects/notes").mock(return_value=httpx.Response(201, json={"id": "1"}))
    store = Store(":memory:")
    hs = HubSpot("t", sleep=lambda s: None, store=store)
    accounts = read_accounts(write_csv(tmp_path))

    first = import_accounts(accounts, hs, store, write=True)
    assert first[0] == "done  acme.io: company 10 · 95 (A)"
    props = json.loads(patch.calls[0].request.content)["properties"]
    assert props["numberofemployees"] == "1124" and props["country"] == "US"
    body = json.loads(note.calls[0].request.content)
    assert body["associations"][0]["types"][0]["associationTypeId"] == 190

    again = import_accounts(accounts, hs, store, write=True)  # rerun: nothing new
    assert again[0].startswith("same  acme.io") and create.call_count == 1 and note.call_count == 1
