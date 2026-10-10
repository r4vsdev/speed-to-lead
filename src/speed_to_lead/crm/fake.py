"""In-memory CRM: used by the tests and by CRM=dryrun (writes nothing anywhere)."""

import itertools

from ..models import Lead


class FakeCRM:
    name = "dryrun"

    def __init__(self):
        self._ids = itertools.count(1)
        self.contacts: dict[str, dict] = {}  # id -> record
        self.companies: dict[str, dict] = {}
        self.deals: dict[str, dict] = {}
        self.notes: list[dict] = []
        self.links: set[tuple[str, str]] = set()

    def _id(self) -> str:
        return str(next(self._ids))

    def upsert_contact(self, lead: Lead) -> tuple[str, bool]:
        email = lead.email.lower()
        for cid, c in self.contacts.items():
            if c["email"] == email:
                c.update({k: v for k, v in _contact_fields(lead).items() if v})
                return cid, False
        cid = self._id()
        self.contacts[cid] = _contact_fields(lead)
        return cid, True

    def upsert_company(self, lead: Lead) -> str | None:
        if not lead.domain:
            return None
        for cid, c in self.companies.items():
            if c["domain"] == lead.domain:
                return cid
        cid = self._id()
        self.companies[cid] = {"domain": lead.domain, "name": lead.company or lead.domain}
        return cid

    def create_deal(self, lead: Lead, contact_id: str, company_id: str | None) -> str:
        did = self._id()
        self.deals[did] = {"name": lead.deal_name, "contact": contact_id, "company": company_id}
        return did

    def link_contact_company(self, contact_id: str, company_id: str) -> None:
        self.links.add((contact_id, company_id))

    def add_note(self, lead: Lead, contact_id: str, deal_id: str) -> None:
        self.notes.append({"contact": contact_id, "deal": deal_id, "body": lead.message})

    def record_qualification(self, result, q) -> None:
        self.deals[result.deal_id]["owner"] = q.owner.name
        self.notes.append({"contact": result.contact_id, "deal": result.deal_id, "body": q.summary()})

    def record_url(self, result) -> str:
        return ""


def _contact_fields(lead: Lead) -> dict:
    return {
        "email": lead.email.lower(),
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "phone": lead.phone,
        "company": lead.company,
    }
