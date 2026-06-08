#!/usr/bin/env python3
"""Mock AnkiConnect HTTP server for bats tests.

Listens on a configurable port and returns canned JSON responses for the
actions the bootstrap script uses (`version`, `getActiveProfile`,
`importPackage`, `deckNames`). Each test programs the responses via a JSON
file passed in --responses; the file maps action name to response object
(or to a list-queue of objects consumed in order).

Logs every received request as one JSON line to --log (if given) so tests
can assert that the script made the calls in the expected order with the
expected params.

Lifecycle:
  - Prints "READY <port>\\n" to stdout when bound and accepting connections.
  - Runs until SIGTERM / SIGINT.

Usage from bats:
  python3 mock_ankiconnect.py --port 9876 --responses /tmp/r.json --log /tmp/l.ndjson &
  MOCK_PID=$!
  # bats reads stdout for "READY"
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    responses: dict = {}
    log_path: str = ""

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            req = json.loads(raw)
            action = req.get("action", "")
            params = req.get("params", {})
        except Exception:
            req = {"_invalid_json": raw}
            action = ""
            params = {}

        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({"action": action, "params": params, "raw": raw})
                    + "\n"
                )

        resp = self.responses.get(action)
        if resp is None:
            resp = {"result": None, "error": f"mock: no response programmed for {action!r}"}
        elif isinstance(resp, list):
            # Queue semantics: pop the first response on each call, leaving an
            # exhaustion error if the test programmed fewer responses than
            # the script made calls.
            if resp:
                resp = resp.pop(0)
            else:
                resp = {"result": None, "error": "mock: response queue exhausted"}

        body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kwargs):
        # Silence the access log. We have our own json log_path.
        return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--responses", type=str, default="")
    ap.add_argument("--log", type=str, default="")
    args = ap.parse_args()

    if args.responses:
        Handler.responses = json.loads(Path(args.responses).read_text(encoding="utf-8"))
    Handler.log_path = args.log

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"READY {args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
