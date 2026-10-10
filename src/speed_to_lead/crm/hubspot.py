"""HubSpot adapter (CRM v3 objects API, private app token)."""

import re
import threading
import time

import httpx

from ..http import ApiError, request
from ..models import Lead

BASE = "https://api.hubapi.com"

# HubSpot-defined association type ids
DEAL_TO_CONTACT = 3
DEAL_TO_COMPANY = 5
CONTACT_TO_COMPANY = 279
NOTE_TO_CONTACT = 202
NOTE_TO_DEAL = 214
NOTE_TO_COMPANY = 190


class HubSpot:
    name = "hubspot"

    def __init__(self, token: str, *, pipeline: str = "default", stage: str = "appointmentscheduled",
                 client: httpx.Client | None = None, sleep=time.sleep, store=None, portal_id: str = ""):
        self.portal_id = portal_id  # only used to build links to records
        # store: remembers domain -> company id. HubSpot's search index lags a few seconds behind writes and
        # company domains aren't unique, so two leads from one company seconds apart would otherwise create two.
        self.store = store
        self._company_lock = threading.Lock()
        self.pipeline = pipeline
        self.stage = stage
        self.sleep = sleep
        self.client = client or httpx.Client(timeout=15)
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        resp = request(self.client, method, BASE + path, json=json, headers=self.headers, sleep=self.sleep)
        return resp.json() if resp.content else {}

    def _search_one(self, obj: str, prop: str, value: str) -> str | None:
        body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}],
                "properties": [prop], "limit": 1}
        results = self._call("POST", f"/crm/v3/objects/{obj}/search", body).get("results", [])
        return results[0]["id"] if results else None

    def upsert_contact(self, lead: Lead) -> tuple[str, bool]:
        props = {k: v for k, v in {
            "email": lead.email.lower(),
            "firstname": lead.first_name,
            "lastname": lead.last_name,
            "phone": lead.phone,
            "company": lead.company,
            "website": lead.website,
        }.items() if v}
        existing = self._search_one("contacts", "email", lead.email.lower())
        if existing is None:
            try:
                return self._call("POST", "/crm/v3/objects/contacts", {"properties": props})["id"], True
            except ApiError as e:
                # The search index lags a few seconds behind writes. HubSpot keeps emails unique and
                # answers 409 "Contact already exists. Existing ID: 123" — update that one instead.
                m = re.search(r"Existing ID: (\d+)", e.body)
                if e.status != 409 or not m:
                    raise
                existing = m.group(1)
        self._call("PATCH", f"/crm/v3/objects/contacts/{existing}", {"properties": props})
        return existing, False

    def upsert_company(self, lead: Lead) -> str | None:
        if not lead.domain:
            return None
        with self._company_lock:  # two leads from one company at once must not both create it
            known = self.store.company_id(self.name, lead.domain) if self.store else None
            company_id = known or self._search_one("companies", "domain", lead.domain)
            if not company_id:
                props = {"domain": lead.domain, "name": lead.company or lead.domain}
                company_id = self._call("POST", "/crm/v3/objects/companies", {"properties": props})["id"]
            if self.store and not known:
                self.store.remember_company(self.name, lead.domain, company_id)
            return company_id

    def link_contact_company(self, contact_id: str, company_id: str) -> None:
        self._call("PUT", f"/crm/v4/objects/contacts/{contact_id}/associations/default/companies/{company_id}")

    def create_deal(self, lead: Lead, contact_id: str, company_id: str | None) -> str:
        assoc = [_assoc(contact_id, DEAL_TO_CONTACT)]
        if company_id:
            assoc.append(_assoc(company_id, DEAL_TO_COMPANY))
        body = {"properties": {"dealname": lead.deal_name, "pipeline": self.pipeline, "dealstage": self.stage},
                "associations": assoc}
        return self._call("POST", "/crm/v3/objects/deals", body)["id"]

    def add_note(self, lead: Lead, contact_id: str, deal_id: str) -> None:
        body = {"properties": {"hs_note_body": f"Form message ({lead.source}):\n{lead.message}",
                               "hs_timestamp": str(int(time.time() * 1000))},
                "associations": [_assoc(contact_id, NOTE_TO_CONTACT), _assoc(deal_id, NOTE_TO_DEAL)]}
        self._call("POST", "/crm/v3/objects/notes", body)

    def record_qualification(self, result, q) -> None:
        if q.owner.hubspot_owner_id:
            owner = {"properties": {"hubspot_owner_id": q.owner.hubspot_owner_id}}
            self._call("PATCH", f"/crm/v3/objects/contacts/{result.contact_id}", owner)
            self._call("PATCH", f"/crm/v3/objects/deals/{result.deal_id}", owner)
        body = {"properties": {"hs_note_body": q.summary(), "hs_timestamp": str(int(time.time() * 1000))},
                "associations": [_assoc(result.contact_id, NOTE_TO_CONTACT), _assoc(result.deal_id, NOTE_TO_DEAL)]}
        self._call("POST", "/crm/v3/objects/notes", body)

    def update_company(self, company_id: str, props: dict) -> None:
        self._call("PATCH", f"/crm/v3/objects/companies/{company_id}", {"properties": props})

    def add_company_note(self, company_id: str, body: str) -> None:
        self._call("POST", "/crm/v3/objects/notes", {
            "properties": {"hs_note_body": body, "hs_timestamp": str(int(time.time() * 1000))},
            "associations": [_assoc(company_id, NOTE_TO_COMPANY)]})

    def record_url(self, result) -> str:
        return f"https://app.hubspot.com/contacts/{self.portal_id}/record/0-3/{result.deal_id}" if self.portal_id else ""


def _assoc(to_id: str, type_id: int) -> dict:
    return {"to": {"id": to_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}
