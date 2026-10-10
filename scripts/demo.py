"""Send fake form leads to a running server and print the timing.

    python scripts/demo.py [base_url]
"""

import os
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
HEADERS = {"X-Webhook-Secret": os.getenv("WEBHOOK_SECRET", "")}
RUN = uuid.uuid4().hex[:6]

LEADS = [
    # Hot: work email, live website, asks for a demo this week → Account Executive
    {"first_name": "Ines", "last_name": "Costa (demo)", "email": "ines.demo@example.com", "company": "PostHog (demo)",
     "website": "posthog.com", "phone": "+1 555 0100",
     "message": "We'd like a demo and pricing this week. Inbound leads wait hours before a rep sees them."},
    # Vague, personal email → nurture
    {"first_name": "Sam", "last_name": "Reyes (demo)", "email": "sam.demo.lead@gmail.com",
     "message": "just curious how this works"},
    # Vendor pitch → flagged as spam even if the AI rates it "medium"
    {"first_name": "Pat", "last_name": "Vendor (demo)", "email": "pat.demo@example.net", "website": "example.net",
     "message": "We offer SEO services and backlinks to rank your site, book a call http://x.test http://y.test"},
    # Same person as the first lead, new enquiry: contact and company must be reused, not duplicated
    {"first_name": "Ines", "last_name": "Costa (demo)", "email": "ines.demo@example.com", "company": "PostHog (demo)",
     "website": "posthog.com", "message": "Follow-up: we also want the alert in Slack."},
]

with httpx.Client(timeout=30) as c:
    keys = []
    for i, lead in enumerate(LEADS):
        lead = {**lead, "source": "demo", "submission_id": f"demo-{RUN}-{i}"}
        r = c.post(f"{BASE}/leads", json=lead, headers=HEADERS)
        print(r.status_code, r.json())
        keys.append(r.json()["key"])
        time.sleep(1)
    # resend the first one: must be ignored
    r = c.post(f"{BASE}/leads", json={**LEADS[0], "source": "demo", "submission_id": f"demo-{RUN}-0"}, headers=HEADERS)
    print("resend:", r.json()["status"])

    deadline = time.time() + 120
    for key in keys:
        while True:
            row = c.get(f"{BASE}/leads/{key}", headers=HEADERS).json()
            if (row["status"] == "failed" or row["grade"]) or time.time() > deadline:
                break
            time.sleep(0.5)
        print(f"{key}: {row['status']} in {row['ms']} ms · contact {row['contact_id']} "
              f"(new={bool(row['contact_created'])}) · company {row['company_id']} · deal {row['deal_id']}"
              f"{' · ' + row['error'] if row['error'] else ''}")
        print(f"   score {row['score']} ({row['grade']}) → {row['owner']} · industry {row['industry']} "
              f"· scored and routed in {row['qualified_ms']} ms")
    print("stats:", c.get(f"{BASE}/stats", headers=HEADERS).json())
