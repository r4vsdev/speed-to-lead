"""Score the lead, then route it to an owner. Every point has a reason the rep can read."""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .enrich import Enrichment, WebsiteEnricher
from .models import FREE_MAIL, Lead

log = logging.getLogger(__name__)

BUYING_WORDS = re.compile(r"\b(pricing|price|quote|demo|proposal|budget|buy|purchase|contract|call|meeting|"
                          r"this week|asap|urgent|start|hire|onboard)\b", re.I)
SPAM_WORDS = re.compile(r"\b(seo services?|backlinks?|guest post|crypto|casino|loan offer|rank (you|your site))\b", re.I)
LINK = re.compile(r"https?://|www\.", re.I)


@dataclass
class Score:
    points: int
    grade: str                     # A | B | C | spam
    reasons: list[str] = field(default_factory=list)


def score(lead: Lead, e: Enrichment, target_industries: list[str]) -> Score:
    pts, why = 20, []

    def add(n: int, reason: str):
        nonlocal pts
        pts += n
        why.append(f"{n:+d} {reason}")

    email_domain = lead.email.split("@")[1].lower()
    if email_domain in FREE_MAIL:
        add(-10, "personal email")
    else:
        add(15, "work email")
    if e.reachable:
        add(10, "company website is live")
    if BUYING_WORDS.search(lead.message):
        add(20, "message asks to buy or talk")
    if len(lead.message) >= 60:
        add(5, "detailed message")
    if lead.phone:
        add(10, "left a phone number")
    if e.intent == "high":
        add(20, "AI: high buying intent")
    elif e.intent == "medium":
        add(10, "AI: medium buying intent")
    if e.industry and e.industry in target_industries:
        add(10, f"target industry ({e.industry})")
    for s in ("hiring", "books demos"):
        if s in e.signals:
            add(5, f"website: {s}")
    spam = bool(SPAM_WORDS.search(lead.message)) or len(LINK.findall(lead.message)) >= 2
    if spam:
        add(-60, "looks like a sales pitch or spam")
    pts = max(0, min(100, pts))
    grade = "spam" if spam else "A" if pts >= 70 else "B" if pts >= 45 else "C"
    return Score(pts, grade, why)


@dataclass
class Owner:
    key: str
    name: str
    hubspot_owner_id: str = ""
    salesforce_user_id: str = ""
    slack: str = ""                # Slack mention, e.g. <@U012ABC>


DEFAULT_ROUTING = {
    "target_industries": ["software", "marketing agency", "ecommerce", "professional services"],
    "owners": {
        "ae": {"name": "Account Executive"},
        "sdr1": {"name": "SDR 1"},
        "sdr2": {"name": "SDR 2"},
        "nurture": {"name": "Marketing nurture"},
    },
    # First matching rule wins. "assign" is one owner, or a list shared round-robin.
    "rules": [
        {"when": {"grade": ["spam"]}, "assign": "nurture"},
        {"when": {"min_score": 70}, "assign": "ae"},
        {"when": {"grade": ["B"]}, "assign": ["sdr1", "sdr2"]},
        {"assign": "nurture"},
    ],
}


class Router:
    def __init__(self, config: dict, store=None):
        self.config = config
        self.owners = {k: Owner(k, **v) for k, v in config["owners"].items()}
        self.store = store
        for rule in config["rules"]:
            for key in rule["assign"] if isinstance(rule["assign"], list) else [rule["assign"]]:
                if key not in self.owners:
                    raise ValueError(f"routing rule assigns unknown owner {key!r}")

    @classmethod
    def from_file(cls, path: str, store=None) -> "Router":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")) if path else DEFAULT_ROUTING, store)

    @property
    def target_industries(self) -> list[str]:
        return self.config.get("target_industries", [])

    def route(self, s: Score, e: Enrichment) -> tuple[Owner, str]:
        for i, rule in enumerate(self.config["rules"]):
            w = rule.get("when", {})
            if "grade" in w and s.grade not in w["grade"]:
                continue
            if "min_score" in w and s.points < w["min_score"]:
                continue
            if "industry" in w and e.industry not in w["industry"]:
                continue
            assign = rule["assign"]
            if isinstance(assign, list):
                n = self.store.next_turn(f"rule{i}") if self.store else 0
                return self.owners[assign[n % len(assign)]], f"rule {i + 1}, round-robin"
            return self.owners[assign], f"rule {i + 1}"
        raise ValueError("no routing rule matched: add a catch-all rule without 'when'")


@dataclass
class Qualification:
    enrichment: Enrichment
    score: Score
    owner: Owner
    rule: str
    ms: int

    def summary(self) -> str:
        """Plain-text block for the CRM note."""
        e = self.enrichment
        lines = [f"Lead score {self.score.points}/100 (grade {self.score.grade}) → routed to {self.owner.name} "
                 f"({self.rule})", "Why: " + "; ".join(self.score.reasons)]
        if e.reachable:
            lines.append("Website: " + " — ".join(p for p in (e.title, e.description) if p))
        if e.ai:
            lines.append(f"AI: {e.industry} · {e.company_type} · intent {e.intent or '?'}. {e.summary}")
        if e.error:
            lines.append(f"Enrichment skipped: {e.error}")
        return "\n".join(lines)


class Qualifier:
    """Enrich → score → route. Each step degrades instead of failing: the lead always gets a score and owner."""

    def __init__(self, router: Router, enricher: WebsiteEnricher | None = None, llm=None, clock=time.monotonic):
        self.router, self.enricher, self.llm, self.clock = router, enricher, llm, clock

    def run(self, lead: Lead) -> Qualification:
        start = self.clock()
        e = Enrichment(domain=lead.domain)
        if self.enricher and lead.domain:
            e = self.enricher.enrich(lead.domain)
        if self.llm and (lead.message or e.reachable):
            try:
                self.llm.classify(lead, e)
            except Exception as err:  # noqa: BLE001 - AI down or slow: the rules score still works
                log.warning("AI enrichment failed for %s: %s", lead.key, err)
                e.error = (e.error + "; " if e.error else "") + f"AI unavailable ({type(err).__name__})"
        s = score(lead, e, self.router.target_industries)
        owner, rule = self.router.route(s, e)
        return Qualification(e, s, owner, rule, int((self.clock() - start) * 1000))
