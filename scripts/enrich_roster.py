#!/usr/bin/env python3
"""Add GroupMe sender ids and name aliases to a roster CSV, using a GroupMe history export.

Input CSV: name,squad (one person per row).  Output CSV: name,squad,sender_ids,aliases
(each ';'-separated). Matching is by exact display name; every sender id that has used that name
is kept, and every other name those sender ids have used becomes an alias, so the PII filter
scrubs all of them.

Only names and ids are written; no message text. The output is local-only (config/roster.local.csv
is git-ignored). Prints counts, never names.

Usage:
    python scripts/enrich_roster.py ~/Downloads/collab_roster.csv data/groupme_<id>.jsonl config/roster.local.csv
"""

import csv
import json
import sys
from collections import defaultdict


def main(roster_csv, export_jsonl, out_csv):
    ids_by_name, names_by_id = defaultdict(set), defaultdict(set)
    with open(export_jsonl, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("sender_type") == "bot" or row.get("system"):
                continue
            ids_by_name[row["name"]].add(row["sender_id"])
            names_by_id[row["sender_id"]].add(row["name"])

    out, unmatched = [], 0
    with open(roster_csv, newline="", encoding="utf-8") as f:
        rows = [[c.strip() for c in r] for r in csv.reader(f) if any(c.strip() for c in r)]
    if rows and rows[0][0].lower() in ("name", "member"):
        rows = rows[1:]
    for name, squad, *_ in rows:
        ids = sorted(ids_by_name.get(name, ()))
        aliases = sorted({n for i in ids for n in names_by_id[i]} - {name})
        unmatched += not ids
        out.append([name, squad, ";".join(ids), ";".join(aliases)])

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out)
    print(f"{len(out)} people, {unmatched} with no sender id found, "
          f"{sum(1 for r in out if r[3])} with aliases -> {out_csv}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
