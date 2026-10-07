from dataclasses import dataclass
from typing import Protocol

from ..models import Lead


@dataclass
class SyncResult:
    contact_id: str
    company_id: str | None
    deal_id: str
    contact_created: bool


class CRM(Protocol):
    """What every CRM adapter must do. The sync logic only talks to this."""

    name: str

    def upsert_contact(self, lead: Lead) -> tuple[str, bool]:
        """Create or update the person by email. Returns (id, created)."""

    def upsert_company(self, lead: Lead) -> str | None:
        """Find the company by domain or create it. None when there is no company to record."""

    def create_deal(self, lead: Lead, contact_id: str, company_id: str | None) -> str:
        """Open a new deal linked to the contact (and company)."""

    def link_contact_company(self, contact_id: str, company_id: str) -> None:
        """Make the company the contact's employer."""

    def add_note(self, lead: Lead, contact_id: str, deal_id: str) -> None:
        """Save the form message on the contact and deal."""


def sync_lead(lead: Lead, crm: CRM) -> SyncResult:
    # Contact first: it is the write most likely to be rejected (e.g. an email the CRM won't accept),
    # and failing before the company is created leaves no orphan records behind.
    contact_id, created = crm.upsert_contact(lead)
    company_id = crm.upsert_company(lead)
    if company_id:
        crm.link_contact_company(contact_id, company_id)
    deal_id = crm.create_deal(lead, contact_id, company_id)
    if lead.message:
        crm.add_note(lead, contact_id, deal_id)
    return SyncResult(contact_id, company_id, deal_id, created)
