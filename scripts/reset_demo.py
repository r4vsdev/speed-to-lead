"""Archive the demo records in HubSpot (recycle bin, restorable for 90 days) and clear the local log.

Only touches records with demo domains/emails or "(demo)" in the name.
"""

import os
import pathlib

import httpx
from dotenv import load_dotenv

load_dotenv(".env")
API = "https://api.hubapi.com/crm/v3/objects"
h = {"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}"}
DOMAINS = ["example.com", "example.org", "northwind-demo.example", "brightdesk-demo.example"]


def find(obj, prop, op, value):
    body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": op, "value": value}]}], "limit": 100}
    return [x["id"] for x in httpx.post(f"{API}/{obj}/search", headers=h, json=body).json().get("results", [])]


targets = {"companies": set(), "contacts": set(), "deals": set()}
for d in DOMAINS:
    targets["companies"] |= set(find("companies", "domain", "EQ", d))
    targets["contacts"] |= set(find("contacts", "email", "CONTAINS_TOKEN", f"*@{d}"))
targets["deals"] |= set(find("deals", "dealname", "CONTAINS_TOKEN", "(demo)"))
for obj, ids in targets.items():
    for i in ids:
        httpx.delete(f"{API}/{obj}/{i}", headers=h).raise_for_status()
    print(f"archived {len(ids)} {obj}")

db = pathlib.Path(os.getenv("DB_PATH", "data/leads.db"))
if db.exists():
    db.unlink()
    print("cleared", db)
