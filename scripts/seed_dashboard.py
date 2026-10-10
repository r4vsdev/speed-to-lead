"""Fill a separate database with synthetic leads so the stats page has something to show.

Scores and owners come from the real scoring and routing code; companies, messages and timings are made up.

    python scripts/seed_dashboard.py [data/demo-dashboard.db] [count]
    DB_PATH=data/demo-dashboard.db uvicorn speed_to_lead.app:main_app --factory --port 8000   → /dashboard
"""

import random
import sys
import time
from pathlib import Path

from speed_to_lead.enrich import Enrichment
from speed_to_lead.models import Lead
from speed_to_lead.qualify import DEFAULT_ROUTING, Router, score
from speed_to_lead.store import Store

path = sys.argv[1] if len(sys.argv) > 1 else "data/demo-dashboard.db"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 160
Path(path).unlink(missing_ok=True)
store = Store(path)
router = Router(DEFAULT_ROUTING, store)
rnd = random.Random(7)

SOURCES = [("website", 5), ("pricing page", 3), ("webinar", 2), ("google ads", 2), ("linkedin", 1)]
INDUSTRIES = [("software", 5), ("marketing agency", 3), ("ecommerce", 2), ("professional services", 2),
              ("finance", 1), ("healthcare", 1), ("other", 2)]
MESSAGES = [
    "Can we book a demo this week? Our reps answer web leads the next morning.",
    "Please send pricing for 5 seats.",
    "How does this connect to HubSpot?",
    "just looking",
    "We need a quote for routing leads to 3 regional teams, budget approved for Q4.",
    "Do you have a student discount?",
    "We offer SEO services and backlinks, book a call",
]


def pick(pairs):
    return rnd.choices([p for p, _ in pairs], weights=[w for _, w in pairs])[0]


now = time.time()
for i in range(count):
    days_ago = min(13, int(rnd.expovariate(1 / 5)))  # more leads recently
    received = now - days_ago * 86400 - rnd.uniform(0, 10 * 3600)
    free = rnd.random() < 0.25
    domain = f"company{i}.example"
    lead = Lead(email=f"lead{i}@{'gmail.com' if free else domain}", message=rnd.choice(MESSAGES),
                phone="+1 555 0100" if rnd.random() < 0.4 else "", source=pick(SOURCES))
    e = Enrichment(domain="" if free else domain, reachable=not free and rnd.random() < 0.9,
                   industry=pick(INDUSTRIES), intent=rnd.choice(["high", "medium", "low", "low"]), ai=True)
    s = score(lead, e, router.target_industries)
    owner, _ = router.route(s, e)
    failed = rnd.random() < 0.02
    ms = int(rnd.lognormvariate(8.25, 0.18))                  # ~3.8 s median, like the measured HubSpot runs
    qualified = ms + int(rnd.lognormvariate(7.6, 0.5))        # + website fetch and local AI
    store.claim(f"seed-{i}", lead.email, "hubspot", received, lead.source, e.domain)
    store.db.execute(
        "update leads set status=?, ms=?, finished_at=?, score=?, grade=?, owner=?, industry=?, ai=1, qualified_ms=?, "
        "error=? where key=?",
        ("failed" if failed else "done", None if failed else ms, received + ms / 1000,
         None if failed else s.points, None if failed else s.grade, None if failed else owner.name,
         None if failed else e.industry, None if failed else qualified,
         "ApiError: HTTP 400 (demo)" if failed else None, f"seed-{i}"))
store.db.commit()
print(f"{count} synthetic leads → {path}")
