#!/usr/bin/env python3
"""martha-gate: the agent's only door to Home Assistant (phase 3, DESIGN.md C16/C17).

Runs as the unprivileged system user martha-gate (martha-gate.service). It holds no
production write access: reads go to production HA with the token of a non-admin user,
everything else goes to the root daemon 'martha-ha serve' over /run/martha/ha.sock.

For the agent (header 'Authorization: Bearer <AGENT_TOKEN>'):
  GET  /ha/{states,config,services,history,logbook,events}[/...]   production, read only
  POST /ha/template {template}                                      render a template (via the daemon)
  GET  /ha/log?lines=N, /staging/log?lines=N                        container logs
  ANY  /staging/api/...                                             staging HA, full access
  GET  /config/files, /config/file?path=P, /config/history[?path=P&n=N]
  POST /proposals {title, description, files: {path: content|null}}
  GET  /proposals, /proposals/<id>
  POST /proposals/<id>/test, /proposals/<id>/submit, /proposals/<id>/withdraw
  POST /rollback-request {deploy, reason}
Without the agent token:
  GET  /p/<id>       diff page for the user (the id is 20 hex characters, not guessable)
  POST /p/<id>/resend  send the approval notification again (it cannot approve anything)
  POST /approval     from production HA only, with header X-Martha-Secret
  GET  /health
Python standard library only.
"""
import hmac
import html
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV_FILE = "/etc/martha/gate.env"
SOCKET = "/run/martha/ha.sock"
HA_URL = "http://127.0.0.1:8123"
STAGING_URL = "http://172.30.53.10:8123"
PORT = int(os.environ.get("MARTHA_GATE_PORT", "8765"))
MAX_BODY = 32 * 1024**2
HA_READ = ("states", "config", "services", "history", "logbook", "events")
PROPOSAL = r"([0-9a-f]{20})"


class GateError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = read_env(ENV_FILE)
for key in ("AGENT_TOKEN", "APPROVAL_SECRET", "GATE_HA_TOKEN", "STAGING_TOKEN"):
    if not ENV.get(key):
        sys.exit(f"martha-gate: {key} missing in {ENV_FILE}; run 'sudo martha-ha setup-gate'")


def daemon(cmd, **args):
    """One request to 'martha-ha serve'; long tasks answer at once (poll the proposal)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(900)   # 'create' waits for the lock while a deploy runs
            s.connect(SOCKET)
            s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
            line = s.makefile("rb").readline()
    except OSError as e:
        raise GateError(503, f"martha-ha daemon not reachable: {e}")
    resp = json.loads(line)
    if not resp.get("ok"):
        raise GateError(409, resp.get("error", "error"))
    return resp["result"]


def proxy(base, path, token, method="GET", body=None, query=""):
    url = base + path + (("?" + query) if query else "")
    req = urllib.request.Request(url, method=method, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "text/plain")
    except (urllib.error.URLError, OSError) as e:
        raise GateError(502, f"Home Assistant not reachable: {e}")


PAGE = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--add:#e6ffec;--del:#ffebe9;--hunk:#ddf4ff;--line:#d0d7de}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;
--add:#12361f;--del:#3d1418;--hunk:#0c2d6b;--line:#30363d}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0;padding:16px}}
main{{max-width:960px;margin:0 auto}}h1{{font-size:20px;margin:0 0 4px}}
.meta{{color:var(--muted);margin-bottom:12px}}p.desc{{white-space:pre-wrap}}
pre{{border:1px solid var(--line);border-radius:6px;overflow-x:auto;font:12px/1.5 ui-monospace,monospace;margin:0}}
pre span{{display:block;padding:0 8px;white-space:pre}}.a{{background:var(--add)}}.d{{background:var(--del)}}
.h{{background:var(--hunk)}}.f{{font-weight:bold;border-top:1px solid var(--line)}}
.notice{{margin:16px 0;padding:12px;border:1px solid var(--line);border-radius:6px}}
button{{font:inherit;padding:10px 16px;border-radius:6px;border:1px solid var(--line);
background:var(--hunk);color:var(--fg);margin-top:8px}}
</style></head><body><main>
<h1>{title}</h1><div class="meta">{kind} · status: <b>{state}</b> · {created}</div>
<p class="desc">{description}</p><p class="meta">Bestanden: {paths}</p>
<pre>{diff}</pre>
{notice}
</main></body></html>"""


def render(p, message=""):
    e = html.escape
    if p["state"] == "submitted":
        notice = ('<div class="notice">Toepassen of afwijzen doe je in de melding: houd hem lang '
                  'ingedrukt (iOS) of klap hem uit (Android). Is de melding weg?'
                  f'<form method="post" action="/p/{e(p["id"])}/resend">'
                  '<button type="submit">Stuur de melding opnieuw</button></form></div>')
    else:
        notice = ""
    if message:
        notice = f'<div class="notice"><b>{e(message)}</b></div>' + notice
    rows = []
    for line in p.get("diff", "").splitlines():
        cls = ("f" if line.startswith(("diff --git", "+++", "---", "index ")) else
               "h" if line.startswith("@@") else "a" if line.startswith("+") else
               "d" if line.startswith("-") else "")
        rows.append(f'<span class="{cls}">{e(line) or " "}</span>')
    return PAGE.format(notice=notice, 
        title=e(p["title"]), kind="terugdraaien" if p["kind"] == "rollback" else "wijziging",
        state=e(p["state"]), created=e(p["created"][:16].replace("T", " ")),
        description=e(p.get("description") or ""), paths=e(", ".join(p.get("paths", []))),
        diff="".join(rows) or "(geen verschillen)")


class Handler(BaseHTTPRequestHandler):
    server_version = "martha-gate"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def send(self, status, body, ctype="application/json", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise GateError(413, "request too large")
        return self.rfile.read(n) if n else b""

    def json_body(self):
        try:
            data = json.loads(self.body() or b"{}")
        except ValueError:
            raise GateError(400, "body must be JSON")
        if not isinstance(data, dict):
            raise GateError(400, "body must be a JSON object")
        return data

    def authorized(self, header, expected, prefix=""):
        value = self.headers.get(header, "")
        return value.startswith(prefix) and hmac.compare_digest(
            value[len(prefix):].encode(), expected.encode())

    def handle_any(self, method):
        try:
            self.route(method)
        except GateError as e:
            self.send(e.status, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = lambda self: self.handle_any("GET")          # noqa: E731
    do_POST = lambda self: self.handle_any("POST")        # noqa: E731
    do_PUT = lambda self: self.handle_any("PUT")          # noqa: E731
    do_PATCH = lambda self: self.handle_any("PATCH")      # noqa: E731
    do_DELETE = lambda self: self.handle_any("DELETE")    # noqa: E731

    def route(self, method):
        raw, _, query = self.path.partition("?")
        segments = raw.split("/")[1:]
        for seg in segments:
            dec = urllib.parse.unquote(seg)
            if dec in (".", "..") or "/" in dec or "\\" in dec:
                raise GateError(400, "invalid path")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}

        # --- without the agent token
        if method == "GET" and raw == "/health":
            return self.send(200, {"ok": True})
        m = re.fullmatch(r"/p/" + PROPOSAL + r"(/resend)?", raw)
        if m and (method, bool(m.group(2))) in (("GET", False), ("POST", True)):
            pid, message = m.group(1), ""
            if m.group(2):
                try:
                    daemon("resend", id=pid)
                    message = "De melding is opnieuw verstuurd."
                except GateError as e:
                    message = f"Niet verstuurd: {e}"
            try:
                p = daemon("show", id=pid)
            except GateError:
                return self.send(404, b"Onbekend voorstel", "text/plain; charset=utf-8")
            return self.send(200, render(p, message).encode(), "text/html; charset=utf-8", {
                "Content-Security-Policy":
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
                "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
        if method == "POST" and raw == "/approval":
            if not self.authorized("X-Martha-Secret", ENV["APPROVAL_SECRET"]):
                raise GateError(403, "forbidden")
            daemon("approval", action=self.json_body().get("action"))
            return self.send(200, {"ok": True})

        # --- the agent
        if not self.authorized("Authorization", ENV["AGENT_TOKEN"], "Bearer "):
            raise GateError(401, "unauthorized")

        if segments and segments[0] == "ha":
            rest = "/".join(segments[1:])
            if method == "GET" and rest == "log":
                return self.send(200, daemon("logs", target="production", lines=q.get("lines", 200)).encode(),
                                 "text/plain; charset=utf-8")
            if method == "GET" and len(segments) > 1 and segments[1] in HA_READ:
                return self.send(*proxy(HA_URL, "/api/" + rest, ENV["GATE_HA_TOKEN"], query=query))
            if method == "POST" and rest == "template":
                text = daemon("template", template=self.json_body().get("template"))
                return self.send(200, text.encode(), "text/plain; charset=utf-8")
            raise GateError(403, "production is read only for the agent")

        if segments and segments[0] == "staging":
            if method == "GET" and segments[1:] == ["log"]:
                return self.send(200, daemon("logs", target="staging", lines=q.get("lines", 200)).encode(),
                                 "text/plain; charset=utf-8")
            if len(segments) > 1 and segments[1] == "api":
                body = self.body() if method in ("POST", "PUT", "PATCH") else None
                return self.send(*proxy(STAGING_URL, raw[len("/staging"):], ENV["STAGING_TOKEN"],
                                        method, body, query))

        if method == "GET" and raw == "/config/files":
            return self.send(200, daemon("files"))
        if method == "GET" and raw == "/config/file":
            return self.send(200, daemon("file", path=q.get("path", "")).encode(), "text/plain; charset=utf-8")
        if method == "GET" and raw == "/config/history":
            return self.send(200, daemon("history", path=q.get("path"), n=q.get("n", 20)))

        if raw == "/proposals":
            if method == "GET":
                return self.send(200, daemon("list"))
            if method == "POST":
                b = self.json_body()
                return self.send(201, daemon("create", title=b.get("title"), files=b.get("files"),
                                             description=b.get("description", "")))
        m = re.fullmatch(r"/proposals/" + PROPOSAL + r"(?:/(test|submit|withdraw))?", raw)
        if m:
            pid, action = m.groups()
            if method == "GET" and not action:
                return self.send(200, daemon("show", id=pid))
            if method == "POST" and action:
                return self.send(202, daemon(action, id=pid))
        if method == "POST" and raw == "/rollback-request":
            b = self.json_body()
            return self.send(202, daemon("rollback_request", deploy=b.get("deploy"),
                                         reason=b.get("reason", "")))
        raise GateError(404, "not found")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"martha-gate listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
