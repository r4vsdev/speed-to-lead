"""Salesforce adapter (REST API, OAuth 2.0 client credentials).

Mapping: company → Account, person → Contact, enquiry → Opportunity (+ contact role), form message → Task.
Unlike HubSpot search, SOQL reads its own writes at once, so no search-lag workaround is needed.
"""

import threading
import time
from datetime import date, timedelta

import httpx

from ..http import ApiError, request
from ..models import Lead

API_VERSION = "v62.0"


def soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Salesforce:
    name = "salesforce"

    def __init__(self, instance_url: str, client_id: str, client_secret: str, *,
                 stage: str = "Prospecting", close_in_days: int = 30,
                 client: httpx.Client | None = None, sleep=time.sleep, store=None):
        self.instance_url = instance_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.stage = stage
        self.close_in_days = close_in_days
        self.client = client or httpx.Client(timeout=15)
        self.sleep = sleep
        self.store = store
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self._account_lock = threading.Lock()

    # --- auth ---

    def _auth(self) -> str:
        with self._token_lock:
            if not self._token:
                resp = request(self.client, "POST", f"{self.instance_url}/services/oauth2/token", sleep=self.sleep, data={
                    "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret})
                body = resp.json()
                self._token = body["access_token"]
                self.instance_url = body.get("instance_url", self.instance_url).rstrip("/")
            return self._token

    def _call(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        url = f"{self.instance_url}/services/data/{API_VERSION}{path}"
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._auth()}"}
            try:
                resp = request(self.client, method, url, json=json, params=params, headers=headers, sleep=self.sleep)
                return resp.json() if resp.content else {}
            except ApiError as e:
                if e.status == 401 and attempt == 1:  # token expired: get a new one once
                    self._token = None
                    continue
                raise
        raise AssertionError("unreachable")

    def _query_one(self, soql: str) -> str | None:
        records = self._call("GET", "/query", params={"q": soql}).get("records", [])
        return records[0]["Id"] if records else None

    # --- CRM interface ---

    def upsert_contact(self, lead: Lead) -> tuple[str, bool]:
        fields = {k: v for k, v in {
            "Email": lead.email.lower(),
            "FirstName": lead.first_name,
            "LastName": lead.last_name or lead.first_name or lead.email.split("@")[0],  # LastName is required
            "Phone": lead.phone,
            "LeadSource": "Web",
        }.items() if v}
        existing = self._query_one(f"SELECT Id FROM Contact WHERE Email = {soql_quote(lead.email.lower())} LIMIT 1")
        if existing:
            fields.pop("LeadSource")  # keep the original source of a returning contact
            self._call("PATCH", f"/sobjects/Contact/{existing}", fields)
            return existing, False
        return self._call("POST", "/sobjects/Contact", fields)["id"], True

    def upsert_company(self, lead: Lead) -> str | None:
        if not lead.domain:
            return None
        with self._account_lock:
            known = self.store.company_id(self.name, lead.domain) if self.store else None
            account_id = known or self._query_one(
                f"SELECT Id FROM Account WHERE Website = {soql_quote(lead.domain)} "
                f"OR Website LIKE {soql_quote('%://' + lead.domain + '%')} "
                f"OR Website LIKE {soql_quote('%www.' + lead.domain + '%')} LIMIT 1")
            if not account_id:
                account_id = self._call("POST", "/sobjects/Account",
                                        {"Name": lead.company or lead.domain, "Website": lead.domain})["id"]
            if self.store and not known:
                self.store.remember_company(self.name, lead.domain, account_id)
            return account_id

    def link_contact_company(self, contact_id: str, company_id: str) -> None:
        self._call("PATCH", f"/sobjects/Contact/{contact_id}", {"AccountId": company_id})

    def create_deal(self, lead: Lead, contact_id: str, company_id: str | None) -> str:
        fields = {"Name": lead.deal_name, "StageName": self.stage, "LeadSource": "Web",
                  "CloseDate": (date.today() + timedelta(days=self.close_in_days)).isoformat()}
        if company_id:
            fields["AccountId"] = company_id
        opp_id = self._call("POST", "/sobjects/Opportunity", fields)["id"]
        self._call("POST", "/sobjects/OpportunityContactRole",
                   {"OpportunityId": opp_id, "ContactId": contact_id, "IsPrimary": True})
        return opp_id

    def add_note(self, lead: Lead, contact_id: str, deal_id: str) -> None:
        self._call("POST", "/sobjects/Task", {
            "WhoId": contact_id, "WhatId": deal_id, "Subject": f"Form message ({lead.source})",
            "Description": lead.message, "Status": "Completed", "ActivityDate": date.today().isoformat()})

    def record_qualification(self, result, q) -> None:
        if q.owner.salesforce_user_id:
            owner = {"OwnerId": q.owner.salesforce_user_id}
            self._call("PATCH", f"/sobjects/Contact/{result.contact_id}", owner)
            self._call("PATCH", f"/sobjects/Opportunity/{result.deal_id}", owner)
        self._call("POST", "/sobjects/Task", {
            "WhoId": result.contact_id, "WhatId": result.deal_id,
            "Subject": f"Lead score {q.score.points} ({q.score.grade}) → {q.owner.name}"[:255],
            "Description": q.summary(), "Status": "Completed", "ActivityDate": date.today().isoformat()})

    def record_url(self, result) -> str:
        return f"{self.instance_url}/lightning/r/Opportunity/{result.deal_id}/view"
