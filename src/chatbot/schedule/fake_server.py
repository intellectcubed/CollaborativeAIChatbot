"""A stand-in for the calendar HTTP service, backed by MockCalendar. For development without the real service.

    python -m chatbot.schedule.fake_server --port 8080

It speaks the same GET protocol (action=get_schedule_day | addShift | noCrew | rollback | list_backups) and
returns the same JSON shapes. Every night from 2026-10-01 to 2026-12-31 starts with squads 34, 35, 42, 43 and 54
all scheduled 1800-0600 (weekend days 0600-1800 too), so any removal in that range has something to remove.
State lives in memory and resets when it restarts.
"""

import argparse
import json
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from chatbot.schedule.client import CalendarError
from chatbot.schedule.mock import MockCalendar

SQUADS = (34, 35, 42, 43, 54)


def seeded_calendar(start=date(2026, 10, 1), end=date(2026, 12, 31)) -> MockCalendar:
    cal = MockCalendar()
    d = start
    while d <= end:
        for squad in SQUADS:
            cal.seed(d, squad, "1800", "0600")
            if d.weekday() >= 5:
                cal.seed(d, squad, "0600", "1800")
        d += timedelta(days=1)
    cal.calls.clear()
    return cal


def make_handler(cal: MockCalendar):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            action, raw_date = q.get("action", ""), q.get("date", "")
            try:
                d = datetime.strptime(raw_date, "%Y%m%d").date()
                if action == "get_schedule_day":
                    day = cal.get_day(d)
                    return self._send({"success": True, "action": action, "date": raw_date, "day_schedule": day.model_dump_json()})
                if action in ("addShift", "noCrew"):
                    fn = cal.add_shift if action == "addShift" else cal.no_crew
                    r = fn(d, q["shift_start"], q["shift_end"], int(q["squad"]), preview=q.get("preview", "False") == "True")
                    out = {"success": True, "action": action, "date": raw_date}
                    if r.change_id:
                        out["changeId"] = r.change_id
                    else:
                        out["preview"] = True
                    return self._send(out)
                if action == "rollback":
                    r = cal.rollback(d, q["change_id"])
                    return self._send({"success": True, "message": r.message, "changeId": r.change_id, "date": raw_date, "snapshot_removed": True})
                if action == "list_backups":
                    return self._send({"success": True, "action": action, "date": raw_date,
                                       "backups": [{"id": b.id, "created_at": "", "description": "", "command": ""} for b in cal.list_backups(d)]})
                return self._send({"success": False, "message": f"unknown action {action!r}"}, 400)
            except (CalendarError, KeyError, ValueError) as exc:
                return self._send({"success": False, "message": type(exc).__name__}, 200)

    return Handler


def serve(port: int, cal: MockCalendar | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(cal or seeded_calendar()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"fake calendar service on :{args.port}", flush=True)
    serve(args.port).serve_forever()
