#!/usr/bin/env python3
"""Download the full message history of a GroupMe group to a JSON Lines file.

Usage:
    export GROUPME_TOKEN=...            # from https://dev.groupme.com
    python scripts/retrieve_groupme_history.py --list-groups
    python scripts/retrieve_groupme_history.py --group-id 12345678

Messages are fetched newest-to-oldest (GroupMe's pagination order) and written
oldest-first, one JSON object per line, so the file is easy to stream later.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://api.groupme.com/v3"
PAGE_SIZE = 100  # GroupMe's maximum
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"


def get_token(cli_token):
    token = cli_token or os.environ.get("GROUPME_TOKEN")
    if not token:
        sys.exit("No token. Set GROUPME_TOKEN or pass --token.")
    return token


def api_get(path, token, params=None, retries=5):
    """GET with simple backoff on rate limits / transient errors."""
    headers = {"X-Access-Token": token}
    for attempt in range(retries):
        resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=30)
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(2**attempt)
            continue
        return resp
    resp.raise_for_status()
    return resp


def list_groups(token):
    groups = []
    page = 1
    while True:
        resp = api_get("/groups", token, {"page": page, "per_page": 50})
        resp.raise_for_status()
        batch = resp.json()["response"]
        if not batch:
            return groups
        groups.extend(batch)
        page += 1


def fetch_history(group_id, token):
    """Return every message in the group, oldest first."""
    messages = []
    before_id = None
    while True:
        params = {"limit": PAGE_SIZE}
        if before_id:
            params["before_id"] = before_id
        resp = api_get(f"/groups/{group_id}/messages", token, params)
        if resp.status_code == 304:  # no more messages
            break
        resp.raise_for_status()
        batch = resp.json()["response"]["messages"]
        if not batch:
            break
        messages.extend(batch)
        before_id = batch[-1]["id"]
        print(f"\rFetched {len(messages)} messages...", end="", file=sys.stderr, flush=True)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(0.2)  # be polite
    print(file=sys.stderr)
    messages.sort(key=lambda m: m["created_at"])
    return messages


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token", help="GroupMe access token (default: $GROUPME_TOKEN)")
    parser.add_argument("--group-id", help="ID of the group to download")
    parser.add_argument("--list-groups", action="store_true", help="List your groups and exit")
    parser.add_argument("--output", type=Path, help="Output file (default: data/groupme_<group_id>.jsonl)")
    args = parser.parse_args()

    token = get_token(args.token)

    if args.list_groups:
        for g in list_groups(token):
            print(f"{g['id']}\t{g['name']}\t{g['messages']['count']} messages")
        return

    if not args.group_id:
        parser.error("--group-id is required (use --list-groups to find it)")

    messages = fetch_history(args.group_id, token)
    output = args.output or DEFAULT_OUTPUT_DIR / f"groupme_{args.group_id}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    print(f"Wrote {len(messages)} messages to {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
