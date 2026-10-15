"""Offline loopback trip dashboard and claim preview."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..core.common import ClaimError, failure, success
from ..core.store import Store

STATIC = Path(__file__).parent / "static"


def make_server(root: Path, config: dict, host: str, port: int):
    if host not in {"127.0.0.1", "localhost"}:
        raise ClaimError("UNSAFE_BIND", "Only loopback hosts are supported")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def safe(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in hosts:
                raise ClaimError("INVALID_HOST", "Unrecognized Host")
            if self.headers.get("Origin") and self.headers["Origin"] not in {
                "http://" + host for host in hosts
            }:
                raise ClaimError("INVALID_ORIGIN", "Cross-origin access is blocked")

        def send(self, body: bytes, mime: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def response(self, data, status=200):
            self.send(
                json.dumps(data, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                status,
            )

        def do_GET(self):
            store = None
            try:
                self.safe()
                url = urlparse(self.path)
                query = parse_qs(url.query)
                routes = {
                    "/": ("index.html", "text/html"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/style.css": ("style.css", "text/css"),
                }
                if url.path in routes:
                    name, mime = routes[url.path]
                    self.send((STATIC / name).read_bytes(), mime + "; charset=utf-8")
                    return
                if url.path == "/favicon.ico":
                    self.send(b"", "image/x-icon", 204)
                    return
                store = Store(root, config)
                if url.path == "/api/dashboard":
                    year = int(query.get("year", ["2026"])[0])
                    result = {
                        "trips": store.trips(),
                        "tiers": list(store.policy["tiers"]),
                        "claims": store.claims(),
                        "stats": store.stats(year),
                        "check": store.check(),
                    }
                elif url.path == "/api/trip":
                    identifier = int(query["id"][0])
                    result = {
                        "trip": store.trip(identifier),
                        "expenses": store.expenses(identifier),
                        "preview": store.preview([identifier], unclaimed=True),
                    }
                elif url.path == "/api/claim":
                    result = store.claim(query["code"][0])
                elif url.path == "/api/download":
                    claim = store.claim(query["code"][0])
                    format = query["format"][0]
                    target = Path(claim["snapshot"]["artifacts"][format])
                    if not target.is_relative_to(root / "claims") or target.is_symlink():
                        raise ClaimError("UNSAFE_PATH", "Invalid artifact path")
                    mime = {
                        "pdf": "application/pdf",
                        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "docx": (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        "zip": "application/zip",
                    }[format]
                    self.send(target.read_bytes(), mime)
                    return
                else:
                    raise ClaimError("NOT_FOUND", "Route not found", 3)
                self.response(success(result))
            except Exception as exc:
                payload, code = failure(exc)
                self.response(payload, 404 if code == 3 else 400)
            finally:
                if store:
                    store.close()

        def do_POST(self):
            store = None
            try:
                self.safe()
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ClaimError("INVALID_CONTENT_TYPE", "JSON requests are required")
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 1024 * 1024:
                    raise ClaimError("INVALID_BODY", "Body must be at most 1 MiB")
                body = json.loads(self.rfile.read(length))
                store = Store(root, config)
                actions = {
                    "/api/trip": store.add_trip,
                    "/api/expense": store.add_expense,
                    "/api/review": store.review_expense,
                    "/api/build": store.build_claim,
                    "/api/status": store.claim_status,
                    "/api/import-receipts": store.import_receipts,
                }
                if self.path not in actions:
                    raise ClaimError("NOT_FOUND", "Route not found", 3)
                self.response(success(actions[self.path](**body)))
            except Exception as exc:
                payload, code = failure(exc)
                self.response(payload, 404 if code == 3 else 400)
            finally:
                if store:
                    store.close()

    return ThreadingHTTPServer((host, port), Handler)


def serve(root: Path, config: dict, host: str, port: int, quiet: bool = False):
    server = make_server(root, config, host, port)
    if not quiet:
        print(f"Expense review: http://{host}:{server.server_port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
