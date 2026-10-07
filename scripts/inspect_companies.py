"""List HubSpot companies for the demo domains (debug helper)."""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(".env")
h = {"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}"}
props = ["domain", "name", "createdate", "hs_object_source", "hs_object_source_label"]
for dom in sys.argv[1:] or ["example.com", "example.org"]:
    body = {"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": dom}]}],
            "properties": props, "limit": 20}
    r = httpx.post("https://api.hubapi.com/crm/v3/objects/companies/search", headers=h, json=body)
    for x in r.json().get("results", []):
        p = x["properties"]
        print(dom, x["id"], p.get("name"), p.get("hs_object_source_label") or p.get("hs_object_source"), p["createdate"])
