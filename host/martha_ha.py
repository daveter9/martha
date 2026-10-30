#!/usr/bin/env python3
"""martha-ha: back up and restore the Home Assistant configuration and database.

Backups are local only, in /var/lib/martha/backups (they do not protect against a failing
disk). Config and database are backed up and restored separately:

  martha-ha backup [LABEL]       snapshot of the whole config dir (config-<time>-<LABEL>.tar.zst);
                                 deploy.ps1 uses the repo commit as label. Kept forever.
  martha-ha backup-db [LABEL]    pg_dump of the database (db-<time>[-<LABEL>].pgdump), nightly
                                 through martha-backup-db.timer. Removed after 30 days.
  martha-ha restore COMMIT|FILE  put the config dir back from a backup (the newest one with
                                 that commit or label); the current dir is kept aside
  martha-ha restore-db FILE      put the database back from a dump (the current database is
                                 kept aside under another name)
  martha-ha status               the latest backups and the free disk space
  martha-ha check                check_config on the current config, in a throwaway container
  martha-ha token                interactive: log in and create the admin token 'martha-root'
                                 in /etc/martha/ha-root.env, used by the setup scripts

The setup scripts (host/ha/setup_*.py) write their packages with put_files(): check_config on
a copy first, then write, reload or restart, a health check on the log, and on errors the
previous files are put back. Uses only the Python standard library (Ubuntu 26.04: Python 3.14)
and docker.
"""
import argparse
import base64
import contextlib
import datetime
import fcntl
import getpass
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HA_DIR = "/opt/homeassistant"
CONFIG = HA_DIR + "/config"
COMPOSE = HA_DIR + "/docker-compose.yml"
SERVICE = "homeassistant"
STATE = "/var/lib/martha"
BACKUPS = STATE + "/backups"
WORK = STATE + "/work"
LOCK = "/run/lock/martha-ha.lock"
ROOT_ENV = "/etc/martha/ha-root.env"   # admin token of the setup scripts: root only (0600)
HA_URL = "http://127.0.0.1:8123"
# Recorder and LTSS database (ADR-001): PostgreSQL in its own container.
DB_CONTAINER = "timescaledb"
DB_NAME = DB_USER = "homeassistant"
DB_ENV = HA_DIR + "/db.env"
OLD_DB_DUMP = "database/homeassistant.pgdump"   # in a backup of the old format (ha-*.tar.zst)

MIN_FREE = 5 * 1024**3   # a backup aborts below this much free disk space
DB_KEEP_DAYS = 30        # database dumps; config backups are kept forever
HEALTH_TIMEOUT = 300     # seconds for HA to come back after a restart
SETTLE = 30              # seconds to let integrations load before reading the log

# Files HA can reload without a restart (homeassistant.reload_all).
RELOADABLE = ("automations.yaml", "scripts.yaml", "scenes.yaml", "blueprints/")
# Log lines after a restart or reload that mean (part of) the configuration did not load.
# check_config does not catch everything: invalid automation triggers, for example, are
# only reported when HA sets them up ("... failed to setup triggers and has been disabled").
FATAL_LOG = ("Invalid config", "Error loading /config", "recovery mode", "safe mode",
             "has been disabled", "could not be validated")


class Error(Exception):
    pass


def log(msg):
    print(f"[martha-ha] {msg}", flush=True)


def run(cmd, check=True, capture=True, **kw):
    res = subprocess.run(cmd, text=True, capture_output=capture, **kw)
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip() if capture else ""
        raise Error(f"{' '.join(cmd)} failed (exit {res.returncode}) {detail}")
    return res


def read_env(path):
    env = {}
    with contextlib.suppress(FileNotFoundError):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    return env


def ha_image():
    image = read_env(HA_DIR + "/.env").get("HA_IMAGE")
    if not image:
        raise Error(f"HA_IMAGE not set in {HA_DIR}/.env")
    return image


def stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def clean_label(label):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]


@contextlib.contextmanager
def locked():
    """One change at a time; a second command waits its turn."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("waiting for another martha-ha command to finish")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


# --- backups ----------------------------------------------------------------------

def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def check_free(path, extra=0):
    free = shutil.disk_usage(path).free
    if free < MIN_FREE + extra:
        raise Error(f"only {free // 1024**2} MB free on {path}; need at least "
                    f"{(MIN_FREE + extra) // 1024**2} MB. Nothing was changed.")


def db_exec(args, db=DB_NAME, **kw):
    """Run a PostgreSQL client tool in the database container, over TCP with the password
    from db.env (as install.sh does)."""
    password = read_env(DB_ENV).get("POSTGRES_PASSWORD")
    if not password:
        raise Error(f"no POSTGRES_PASSWORD in {DB_ENV}")
    cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={password}", DB_CONTAINER,
           *args, "-h", "127.0.0.1", "-U", DB_USER, "-d", db]
    return subprocess.run(cmd, **kw)


def psql(sql, db=DB_NAME):
    res = db_exec(["psql", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql], db=db,
                  text=True, capture_output=True)
    if res.returncode != 0:
        raise Error(f"psql failed: {res.stderr.strip()}")
    return res.stdout.strip()


def require_db():
    if not os.path.isfile(DB_ENV):
        raise Error(f"no database on this install ({DB_ENV} is missing)")
    res = run(["docker", "inspect", "-f", "{{.State.Running}}", DB_CONTAINER], check=False)
    if res.stdout.strip() != "true":
        raise Error(f"database container {DB_CONTAINER} is not running; start it first "
                    f"(docker compose -f {COMPOSE} up -d {DB_CONTAINER})")


def backup(label="manual"):
    """Snapshot of the whole config dir, secrets.yaml and .storage included. SQLite databases
    are copied with the online backup API, so the copy is consistent while HA keeps running."""
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    check_free(BACKUPS, dir_size(CONFIG))
    path = f"{BACKUPS}/config-{stamp()}-{clean_label(label)}.tar.zst"
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        snapshots = {}
        for name in sorted(os.listdir(CONFIG)):
            if name.endswith(".db") and os.path.isfile(os.path.join(CONFIG, name)):
                dst = os.path.join(tmp, name)
                src = sqlite3.connect(f"file:{os.path.join(CONFIG, name)}?mode=ro", uri=True)
                out = sqlite3.connect(dst)
                try:
                    src.backup(out)
                finally:
                    out.close()
                    src.close()
                snapshots[name] = dst

        def skip(info):
            rel = info.name.split("/", 1)[1] if "/" in info.name else ""
            if rel == "deps" or rel.startswith("deps/"):
                return None   # pip cache, rebuilt by HA
            for db in snapshots:
                if rel in (db, db + "-wal", db + "-shm", db + "-journal"):
                    return None   # replaced by the consistent snapshot below
            return info

        with tarfile.open(path + ".partial", "w:zst") as tar:
            tar.add(CONFIG, arcname="config", filter=skip)
            for name, snap in snapshots.items():
                tar.add(snap, arcname=f"config/{name}")
    os.chmod(path + ".partial", 0o600)
    os.replace(path + ".partial", path)
    log(f"config backup {path} ({os.path.getsize(path) // 1024} KB)")
    return path


def backup_db(label=None):
    """Consistent pg_dump (custom format, compressed)."""
    require_db()
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    check_free(BACKUPS, int(psql(f"SELECT pg_database_size('{DB_NAME}')")))
    suffix = f"-{clean_label(label)}" if label else ""
    path = f"{BACKUPS}/db-{stamp()}{suffix}.pgdump"
    fd = os.open(path + ".partial", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        res = db_exec(["pg_dump", "-Fc"], stdout=f, stderr=subprocess.PIPE)
    if res.returncode != 0:
        os.remove(path + ".partial")
        raise Error(f"pg_dump failed: {res.stderr.decode(errors='replace').strip()}")
    os.replace(path + ".partial", path)
    log(f"database backup {path} ({os.path.getsize(path) // 1024**2} MB)")
    prune_db()
    return path


def list_backups(prefix, suffix):
    with contextlib.suppress(FileNotFoundError):
        return sorted(f for f in os.listdir(BACKUPS) if f.startswith(prefix) and f.endswith(suffix))
    return []


def prune_db():
    cutoff = time.time() - DB_KEEP_DAYS * 86400
    for name in list_backups("db-", ".pgdump"):
        path = os.path.join(BACKUPS, name)
        if os.path.getmtime(path) < cutoff:
            os.remove(path)
            log(f"removed database backup {name} (older than {DB_KEEP_DAYS} days)")


def find_config_backup(name):
    """A path, a file name in BACKUPS, or a commit/label: the newest backup that has it."""
    for path in (name, os.path.join(BACKUPS, name)):
        if os.path.isfile(path):
            return path
    label = clean_label(name)
    hits = []
    for f in list_backups("config-", ".tar.zst"):
        m = re.fullmatch(r"config-\d{4}-\d\d-\d\d_\d{6}-(.+)\.tar\.zst", f)
        # A short commit in the name matches a longer one given, and the other way round.
        if m and label and (m.group(1).startswith(label) or label.startswith(m.group(1))):
            hits.append(f)
    if not hits:
        raise Error(f"no config backup for {name!r} (see 'martha-ha status')")
    return os.path.join(BACKUPS, hits[-1])


def restore(name):
    """Put the config dir back from a backup; the database is not touched. Reads the old
    format (ha-*.tar.zst, config plus a database dump) too, and then takes only the config."""
    path = find_config_backup(name)
    aside = f"{HA_DIR}/config.before-restore-{stamp()}"
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    with tarfile.open(path, "r:zst") as tar:
        config = [m for m in tar.getmembers() if m.name == "config" or m.name.startswith("config/")]
        if not config:
            raise Error(f"{path} holds no config directory")
        compose("stop")
        try:
            os.rename(CONFIG, aside)
            tar.extractall(HA_DIR, members=config, filter="tar")
        except BaseException:
            if os.path.isdir(aside):
                with contextlib.suppress(FileNotFoundError):
                    shutil.rmtree(CONFIG)
                os.rename(aside, CONFIG)
            raise
        finally:
            since = time.time()
            compose("start")
    log(f"restored the config of {os.path.basename(path)}; the previous config is kept in {aside}")
    ok, why = wait_healthy(since)
    if not ok:
        raise Error(f"Home Assistant is not healthy after the restore: {why}")
    log("health check passed")


def restore_db(name):
    """Replace the database with a dump (db-*.pgdump, or the dump inside an old ha-*.tar.zst).
    The current database is kept aside under another name, so a restore never destroys data.
    Home Assistant is stopped meanwhile."""
    require_db()
    path = next((p for p in (name, os.path.join(BACKUPS, name)) if os.path.isfile(p)), None)
    if not path:
        raise Error(f"database backup {name} not found (see 'martha-ha status')")
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        dump = path
        if path.endswith(".tar.zst"):
            with tarfile.open(path, "r:zst") as tar:
                if OLD_DB_DUMP not in tar.getnames():
                    raise Error(f"{path} holds no database dump")
                tar.extract(OLD_DB_DUMP, tmp, filter="data")
            dump = os.path.join(tmp, OLD_DB_DUMP)
        aside = f"{DB_NAME}_before_restore_{stamp().replace('-', '_')}"
        compose("stop")
        try:
            psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                 f"WHERE datname = '{DB_NAME}' AND pid <> pg_backend_pid()", db="postgres")
            psql(f"ALTER DATABASE {DB_NAME} RENAME TO {aside}", db="postgres")
            try:
                psql(f"CREATE DATABASE {DB_NAME} OWNER {DB_USER}", db="postgres")
                psql("CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT timescaledb_pre_restore()")
                with open(dump, "rb") as f:
                    res = db_exec(["pg_restore", "--no-owner", "--exit-on-error"], stdin=f,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                psql("SELECT timescaledb_post_restore()")
                if res.returncode != 0:
                    raise Error(f"pg_restore failed: {res.stderr.decode(errors='replace').strip()}")
            except BaseException:
                with contextlib.suppress(Error):
                    psql(f"DROP DATABASE IF EXISTS {DB_NAME}", db="postgres")
                    psql(f"ALTER DATABASE {aside} RENAME TO {DB_NAME}", db="postgres")
                raise
        finally:
            compose("start")
    log(f"database restored from {os.path.basename(path)}; the previous database is kept as "
        f"'{aside}' (remove it with: DROP DATABASE {aside})")


def status():
    deployed = ""
    with contextlib.suppress(FileNotFoundError):
        with open(STATE + "/deployed") as f:
            deployed = f.read().strip()
    print(f"deployed commit: {deployed or 'unknown'}")
    for title, names in (("config", list_backups("config-", ".tar.zst") + list_backups("ha-", ".tar.zst")),
                         ("database", list_backups("db-", ".pgdump"))):
        names.sort(key=lambda n: os.path.getmtime(os.path.join(BACKUPS, n)))
        print(f"{title} backups: {len(names)}")
        for name in names[-5:]:
            size = os.path.getsize(os.path.join(BACKUPS, name))
            print(f"  {name}  {size // 1024**2} MB" if size >= 1024**2 else f"  {name}  {size // 1024} KB")
    free = shutil.disk_usage(STATE).free
    print(f"free on {STATE}: {free // 1024**3} GB (backups stop below {MIN_FREE // 1024**3} GB)")


# --- Home Assistant -----------------------------------------------------------------

def compose(action, service=SERVICE):
    args = {"stop": ["stop", service], "start": ["up", "-d", service],
            "restart": ["restart", service]}[action]
    log(f"{action} {service}")
    run(["docker", "compose", "-f", COMPOSE, *args])


def ha_api(method, path, token=None, body=None, timeout=10, url=HA_URL, form=False):
    """Call the HA REST API; returns the decoded JSON answer (or None)."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if form:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif method == "POST":
        data = json.dumps(body or {}).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else None


def wait_healthy(since, url=HA_URL, service=SERVICE):
    """HA answers HTTP again and its log shows no configuration errors since 'since'."""
    deadline = time.time() + HEALTH_TIMEOUT
    while True:
        try:
            with urllib.request.urlopen(url + "/manifest.json", timeout=5) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        if time.time() > deadline:
            return False, "Home Assistant did not answer within 5 minutes"
        time.sleep(5)
    time.sleep(SETTLE)
    since_iso = datetime.datetime.fromtimestamp(since, datetime.timezone.utc).isoformat()
    res = run(["docker", "logs", "--since", since_iso, service], check=False)
    bad = [l for l in (res.stdout + res.stderr).splitlines() if any(p in l for p in FATAL_LOG)]
    if bad:
        return False, "configuration errors in the log:\n  " + "\n  ".join(bad[:10])
    return True, "ok"


def check(files=None):
    """HA's check_config on a copy of the config dir, with 'files' ({path: content or None})
    written into the copy. Network-less throwaway container; production is not touched."""
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        cfg = os.path.join(tmp, "config")
        shutil.copytree(CONFIG, cfg, symlinks=True, ignore=shutil.ignore_patterns(
            "*.db", "*.db-*", "*.log", "*.log.*", "deps", "tts"))
        for path, content in (files or {}).items():
            dest = os.path.join(cfg, path)
            if content is None:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(dest)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w") as f:
                    f.write(content)
        # PYTHONPATH as in production: custom integrations (LTSS) import their requirements
        # from the wheels install.sh put in pydeps.
        res = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python3",
                   "-v", f"{cfg}:/config", "-v", f"{HA_DIR}/pydeps:/pydeps:ro",
                   "-e", "PYTHONPATH=/pydeps", ha_image(),
                   "-m", "homeassistant", "--script", "check_config", "--config", "/config"],
                  check=False)
    output = (res.stdout + res.stderr).strip()
    return res.returncode == 0, output


def activate(paths):
    """Make HA pick up changed files: reload if possible, otherwise restart."""
    token = read_env(ROOT_ENV).get("HA_TOKEN")
    if token and paths and all(p.startswith(RELOADABLE) for p in paths):
        try:
            ha_api("POST", "/api/services/homeassistant/reload_all", token, timeout=120)
            log("reloaded Home Assistant configuration")
            return
        except (urllib.error.URLError, OSError) as e:
            log(f"reload failed ({e}); restarting instead")
    compose("restart")


def read_file(path):
    with contextlib.suppress(FileNotFoundError):
        with open(os.path.join(CONFIG, path)) as f:
            return f.read()
    return None


def write_files(files):
    for path, content in files.items():
        dest = os.path.join(CONFIG, path)
        if content is None:
            with contextlib.suppress(FileNotFoundError):
                os.remove(dest)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".tmp", "w") as f:
            f.write(content)
        os.chmod(dest + ".tmp", 0o644)
        os.replace(dest + ".tmp", dest)


def put_files(files, message):
    """Write config files ({path relative to the config dir: content, or None to remove})
    under supervision: check_config on a copy first, then write, reload or restart, and a
    health check on the log. On errors the previous files are put back. True when something
    changed. The caller makes the backup (martha-ha backup) beforehand."""
    with locked():
        changed = {p: c for p, c in files.items() if read_file(p) != c}
        if not changed:
            log(f"{message}: nothing to change")
            return False
        ok, out = check(changed)
        if not ok:
            raise Error("check_config failed, nothing was changed:\n" + out)
        log("check_config passed")
        old = {p: read_file(p) for p in changed}
        since = time.time()
        write_files(changed)
        log(f"{message}: {', '.join(sorted(changed))}")
        activate(list(changed))
        ok, why = wait_healthy(since)
        if ok:
            log("health check passed")
            return True
        log(f"health check FAILED: {why}; putting the previous files back")
        write_files(old)
        since = time.time()
        compose("restart")
        ok2, why2 = wait_healthy(since)
        if not ok2:
            raise Error(f"health check failed, also after putting the files back; check Home "
                        f"Assistant by hand ({why2})")
        raise Error(f"{message} was undone: {why}")


# --- websocket and tokens ---------------------------------------------------------

class WebSocket:
    """Minimal client for HA's websocket API (stdlib only): text frames, no extensions."""

    def __init__(self, url, token):
        u = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: {u.netloc}\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self._read()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise Error(f"websocket handshake failed: {head.splitlines()[0].decode()}")
        self.recv()   # auth_required
        self.send({"type": "auth", "access_token": token})
        if self.recv().get("type") != "auth_ok":
            raise Error("websocket authentication failed")
        self.id = 0

    def _read(self):
        data = self.sock.recv(65536)
        if not data:
            raise Error("websocket closed by Home Assistant")
        return data

    def _exact(self, n):
        while len(self.buf) < n:
            self.buf += self._read()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _frame(self, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, obj):
        self._frame(0x1, json.dumps(obj).encode())

    def recv(self):
        message = b""
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._exact(2), "big")
            elif n == 127:
                n = int.from_bytes(self._exact(8), "big")
            mask = self._exact(4) if b1 & 0x80 else None
            payload = self._exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise Error("websocket closed by Home Assistant")
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode in (0x0, 0x1):
                message += payload
                if b0 & 0x80:
                    return json.loads(message)

    def call(self, type_, **kw):
        self.id += 1
        self.send({"id": self.id, "type": type_, **kw})
        while True:
            msg = self.recv()
            if msg.get("id") == self.id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise Error(f"{type_}: {msg.get('error', {}).get('message', msg)}")
                return msg.get("result")

    def close(self):
        with contextlib.suppress(OSError):
            self._frame(0x8, b"")
            self.sock.close()


def ha_login(url, username, password):
    """Log in through HA's login flow, as the frontend does; returns an access token."""
    client_id = url + "/"
    try:
        flow = ha_api("POST", "/auth/login_flow", url=url, body={
            "client_id": client_id, "handler": ["homeassistant", None], "redirect_uri": client_id})
        step = ha_api("POST", f"/auth/login_flow/{flow['flow_id']}", url=url, body={
            "client_id": client_id, "username": username, "password": password})
        while step.get("type") == "form" and step.get("step_id") == "mfa":
            code = input("  two-factor code: ").strip()
            step = ha_api("POST", f"/auth/login_flow/{flow['flow_id']}", url=url,
                          body={"client_id": client_id, "code": code})
        if step.get("type") != "create_entry":
            raise Error(f"login failed: {step.get('errors') or step.get('reason') or step}")
        tokens = ha_api("POST", "/auth/token", url=url, form=True, body={
            "grant_type": "authorization_code", "code": step["result"], "client_id": client_id})
    except urllib.error.HTTPError as e:
        raise Error(f"login failed: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, OSError) as e:
        raise Error(f"cannot reach Home Assistant at {url}: {e}")
    return tokens["access_token"]


def long_lived_token(url, access_token, name):
    """A new long-lived token 'name' for the logged-in user; an older one is revoked."""
    ws = WebSocket(url, access_token)
    try:
        for t in ws.call("auth/refresh_tokens"):
            if t.get("client_name") == name:
                ws.call("auth/delete_refresh_token", refresh_token_id=t["id"])
        return ws.call("auth/long_lived_access_token", client_name=name, lifespan=3650)
    finally:
        ws.close()


def write_env(path, values, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Written by 'martha-ha token'. Do not share.\n")
        f.writelines(f"{k}={v}\n" for k, v in values.items())
    os.chmod(path + ".tmp", mode)
    os.replace(path + ".tmp", path)


def token():
    """Interactive: log in as an administrator and create the token 'martha-root'."""
    print(f"Log in to Home Assistant ({HA_URL}) with an administrator account:")
    username = input("  username: ").strip()
    password = getpass.getpass("  password: ")
    root_token = long_lived_token(HA_URL, ha_login(HA_URL, username, password), "martha-root")
    write_env(ROOT_ENV, {"HA_TOKEN": root_token})
    log(f"created long-lived token 'martha-root' in {ROOT_ENV}")


# --- clean-up of the agent layer (removed on 2026-10-30) -----------------------------

LEGACY_GIT = STATE + "/ha-config.git"
LEGACY_PACKAGE = "packages/martha_gate.yaml"
LEGACY_SECRET = "martha_gate_secret"
LEGACY_USER = "Martha gate"


def cleanup_legacy():
    """Idempotent, called by install.sh: archive and remove the old config repository, and
    remove the approval package, its secret and the HA user of martha-gate."""
    if os.path.isdir(LEGACY_GIT):
        os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
        path = f"{BACKUPS}/ha-config-git-{stamp()}.tar.zst"
        with tarfile.open(path + ".partial", "w:zst") as tar:
            tar.add(LEGACY_GIT, arcname="ha-config.git")
        os.chmod(path + ".partial", 0o600)
        os.replace(path + ".partial", path)
        shutil.rmtree(LEGACY_GIT)
        log(f"archived the old config repository as {path} and removed it")

    # First the package, then the secret it refers to.
    if read_file(LEGACY_PACKAGE) is not None:
        write_files({LEGACY_PACKAGE: None})
        log(f"removed {LEGACY_PACKAGE}")
        since = time.time()
        compose("restart")
        ok, why = wait_healthy(since)
        if not ok:
            raise Error(f"Home Assistant is not healthy after removing {LEGACY_PACKAGE}: {why}")
    secrets_text = read_file("secrets.yaml") or ""
    if re.search(rf"^{LEGACY_SECRET}:", secrets_text, re.M):
        kept = [l for l in secrets_text.splitlines() if not l.startswith(LEGACY_SECRET + ":")]
        path = os.path.join(CONFIG, "secrets.yaml")
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))
        os.replace(path + ".tmp", path)
        log(f"removed {LEGACY_SECRET} from secrets.yaml")

    admin = read_env(ROOT_ENV).get("HA_TOKEN")
    if not admin:
        log(f"no HA_TOKEN in {ROOT_ENV}; the HA user '{LEGACY_USER}' (if any) is left alone")
        return
    try:
        ws = WebSocket(HA_URL, admin)
    except (Error, OSError) as e:
        log(f"WARNING: cannot reach Home Assistant ({e}); run install.sh again to remove "
            f"the HA user '{LEGACY_USER}'")
        return
    try:
        for u in ws.call("config/auth/list"):
            if u.get("name") == LEGACY_USER:
                ws.call("config/auth/delete", user_id=u["id"])
                log(f"removed the HA user '{LEGACY_USER}'")
    finally:
        ws.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="martha-ha", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("backup"); s.add_argument("label", nargs="?", default="manual")
    s = sub.add_parser("backup-db"); s.add_argument("label", nargs="?")
    s = sub.add_parser("restore"); s.add_argument("backup")
    s = sub.add_parser("restore-db"); s.add_argument("backup")
    sub.add_parser("status")
    sub.add_parser("check")
    sub.add_parser("token")
    sub.add_parser("cleanup-legacy")
    a = p.parse_args(argv)

    if os.geteuid() != 0:
        print("martha-ha must run as root", file=sys.stderr)
        return 2
    try:
        if a.cmd == "status":
            status()   # read only: works while another command runs
            return 0
        with locked():
            if a.cmd == "backup":
                backup(a.label)
            elif a.cmd == "backup-db":
                backup_db(a.label)
            elif a.cmd == "restore":
                restore(a.backup)
            elif a.cmd == "restore-db":
                restore_db(a.backup)
            elif a.cmd == "check":
                ok, out = check()
                print(out)
                return 0 if ok else 1
            elif a.cmd == "token":
                token()
            elif a.cmd == "cleanup-legacy":
                cleanup_legacy()
    except Error as e:
        log(f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
