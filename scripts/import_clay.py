"""Push a Clay CSV export into HubSpot as companies with an ICP note.

    python scripts/import_clay.py clay/accounts-enriched.csv            # dry run: shows what would happen
    python scripts/import_clay.py clay/accounts-enriched.csv --write    # writes to HubSpot
    options: --min-tier A|B|C (default B)
"""

import argparse
import os

from dotenv import load_dotenv

from speed_to_lead.clay_import import import_accounts, read_accounts
from speed_to_lead.crm.hubspot import HubSpot
from speed_to_lead.store import Store

load_dotenv(".env")
p = argparse.ArgumentParser()
p.add_argument("csv")
p.add_argument("--write", action="store_true")
p.add_argument("--min-tier", default="B", choices=["A", "B", "C"])
args = p.parse_args()

store = Store(os.getenv("DB_PATH", "data/leads.db"))
hs = HubSpot(os.environ["HUBSPOT_TOKEN"], store=store)
for line in import_accounts(read_accounts(args.csv), hs, store, min_tier=args.min_tier, write=args.write):
    print(line)
if not args.write:
    print("\nDry run. Add --write to send to HubSpot.")
