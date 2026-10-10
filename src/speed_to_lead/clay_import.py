"""Clay table (CSV export) → HubSpot companies, each with an ICP note.

Clay's free plan has no CRM sync, so the table is exported as CSV and pushed with the same HubSpot adapter the
webhook uses. Safe to rerun: a company whose score was already imported is skipped.
"""

import csv
from dataclasses import dataclass

from .crm.hubspot import HubSpot
from .models import Lead


@dataclass
class Account:
    domain: str
    name: str
    score: int
    tier: str
    industry: str = ""
    employees: str = ""
    country: str = ""
    description: str = ""
    linkedin: str = ""
    sells_to: str = ""
    sales_motion: str = ""
    free_plan: str = ""
    why: str = ""

    def note(self) -> str:
        return (f"Clay ICP score {self.score}/100 (tier {self.tier})\n"
                f"Sells to: {self.sells_to or '?'} · sales motion: {self.sales_motion or '?'} · "
                f"free plan: {self.free_plan or '?'} · {self.employees or '?'} employees · {self.industry or '?'}\n"
                f"Why fast lead response matters to them (AI research): {self.why or '-'}")


def read_accounts(path: str) -> list[Account]:
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            domain = (r.get("Domain") or "").strip().lower().removeprefix("www.")
            if not domain:
                continue
            try:
                score = int(float(r.get("ICP Score") or 0))
            except ValueError:
                score = 0
            out.append(Account(
                domain=domain, name=(r.get("Name") or domain).strip(), score=score,
                tier=(r.get("ICP Tier") or "").strip(), industry=(r.get("Industry") or "").strip(),
                employees=(r.get("Employee Count") or "").replace(",", "").strip(),
                country=(r.get("Country") or "").strip(), description=(r.get("Description") or "").strip()[:1000],
                linkedin=(r.get("Url") or "").strip(), sells_to=(r.get("Sells To") or "").strip(),
                sales_motion=(r.get("Sales Motion") or "").strip(), free_plan=(r.get("Has Free Plan") or "").strip(),
                why=(r.get("Why Fast Leads Matter") or "").strip()[:500]))
    return out


def import_accounts(accounts: list[Account], hs: HubSpot, store, *, min_tier: str = "B", write: bool = False) -> list[str]:
    """Push accounts at or above min_tier. Returns one log line per account. Nothing is written unless write=True."""
    order = {"A": 1, "B": 2, "C": 3}
    log = []
    for a in accounts:
        if order.get(a.tier, 9) > order.get(min_tier, 2):
            log.append(f"skip  {a.domain}: tier {a.tier or '?'} below {min_tier}")
            continue
        if store.clay_imported(a.domain, a.score):
            log.append(f"same  {a.domain}: score {a.score} already imported")
            continue
        if not write:
            log.append(f"would {a.domain}: {a.name} · {a.score} ({a.tier})")
            continue
        company_id = hs.upsert_company(Lead(email=f"import@{a.domain}", company=a.name, website=a.domain))
        props = {k: v for k, v in {
            "name": a.name,
            "numberofemployees": a.employees if a.employees.isdigit() else "",
            "country": a.country,
            "description": a.description,
            "linkedin_company_page": a.linkedin,
        }.items() if v}
        hs.update_company(company_id, props)
        hs.add_company_note(company_id, a.note())
        store.mark_clay_imported(a.domain, a.score, company_id)
        log.append(f"done  {a.domain}: company {company_id} · {a.score} ({a.tier})")
    return log
