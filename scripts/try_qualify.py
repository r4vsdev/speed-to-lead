"""Run enrichment + scoring + routing on sample leads, without any CRM. Uses LLM_* from .env if set.

    python scripts/try_qualify.py
"""

import os
import time

from dotenv import load_dotenv

from speed_to_lead.enrich import WebsiteEnricher
from speed_to_lead.llm import LLM
from speed_to_lead.models import Lead
from speed_to_lead.qualify import DEFAULT_ROUTING, Qualifier, Router

load_dotenv()
llm = LLM(os.environ["LLM_BASE_URL"], os.getenv("LLM_MODEL", "qwen3:8b"), os.getenv("LLM_API_KEY", ""),
          reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "")) if os.getenv("LLM_BASE_URL") else None
qualifier = Qualifier(Router(DEFAULT_ROUTING), enricher=WebsiteEnricher(), llm=llm)

SAMPLES = [
    Lead(email="maya.demo@example.com", first_name="Maya", company="PostHog (demo)", website="posthog.com",
         phone="+1 555 0100", message="We'd like a demo and pricing this week. Inbound leads wait hours before a rep sees them."),
    Lead(email="leo.demo@example.org", first_name="Leo", company="Webflow (demo)", website="webflow.com",
         message="Could you send pricing for connecting our site forms to HubSpot?"),
    Lead(email="sam.demo@gmail.com", first_name="Sam", message="just curious how this works"),
    Lead(email="pitch.demo@example.net", first_name="Pat", website="example.net",
         message="We offer SEO services and backlinks to rank your site, book a call http://x.test http://y.test"),
]

for lead in SAMPLES:
    t = time.monotonic()
    q = qualifier.run(lead)
    print(f"\n{lead.first_name} ({lead.domain or 'no site'}) → {q.score.points} {q.score.grade} → {q.owner.name} "
          f"[{time.monotonic() - t:.1f}s]")
    print("  " + q.summary().replace("\n", "\n  "))
