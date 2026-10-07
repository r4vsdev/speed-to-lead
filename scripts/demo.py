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
    {"first_name": "Maya", "last_name": "Fernandes", "email": "maya.demo@example.com", "company": "Northwind (demo)",
     "website": "example.com", "phone": "+1 555 0100", "message": "We need help routing inbound leads."},
    {"first_name": "Leo", "last_name": "Park", "email": "leo.demo@example.org", "company": "Brightdesk (demo)",
     "message": "Can you connect our Webflow forms to the CRM?"},
    # Same person as the first lead, new enquiry: contact and company must be reused, not duplicated
    {"first_name": "Maya", "last_name": "Fernandes", "email": "maya.demo@example.com", "company": "Northwind (demo)",
     "message": "Follow-up: also want a Slack alert."},
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

    deadline = time.time() + 60
    for key in keys:
        while True:
            row = c.get(f"{BASE}/leads/{key}").json()
            if row["status"] != "processing" or time.time() > deadline:
                break
            time.sleep(0.5)
        print(f"{key}: {row['status']} in {row['ms']} ms · contact {row['contact_id']} "
              f"(new={bool(row['contact_created'])}) · company {row['company_id']} · deal {row['deal_id']}"
              f"{' · ' + row['error'] if row['error'] else ''}")
    print("stats:", c.get(f"{BASE}/stats").json())
