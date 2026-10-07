#!/usr/bin/env python3
"""martha-ha: version, back up, deploy and roll back the Home Assistant configuration.

The HA config directory is tracked in a git repository (/var/lib/martha/ha-config.git)
with an allowlist: only configuration files that are safe to version and to change are
tracked. The database, secrets, auth and integration credentials are never tracked and
never written by a deploy or rollback.

  martha-ha init                 create the config repo (idempotent) and commit the current state
  martha-ha sync [-m MSG]        commit the current production state (changes made in the UI)
  martha-ha status               show the current version, recent history and backups
  martha-ha backup [LABEL]       full, consistent backup: config dir plus a pg_dump of the database
  martha-ha check REV            validate REV with check_config in a throwaway container
  martha-ha deploy REV [-m MSG]  merge REV into production: check, backup, apply, health check
  martha-ha rollback [REV]       revert deploy REV (default: the most recent deploy)
  martha-ha rollback --to REV    put the tracked files back exactly as they were at REV
  martha-ha restore BACKUP       emergency: restore a full backup, database included (the current
                                 config and database are kept aside)
  martha-ha staging up [REV]     run staging HA with production + REV merged (default: production)
  martha-ha staging test REV     check_config plus a staging boot of REV; exit 1 on errors
  martha-ha staging down         stop staging HA (its own logins and data are kept)
  martha-ha setup-gate [--rotate]  interactive: HA tokens, notify service and approval
                                 automation for martha-gate (--rotate: new agent token)
  martha-ha proposals            list the agent's proposals and their state
  martha-ha serve                daemon for martha-gate on /run/martha/ha.sock (martha-ha.service)

Staging is a second HA container on an internal Docker network (no LAN, no internet),
reachable on the LAN at port 8124 through martha-staging-proxy. It has its own logins,
.storage and database; only the tracked files come from production and the proposal, and
secrets.yaml holds dummy values.

martha-gate (martha_gate.py, unprivileged) is the agent's only access. Everything that
needs root goes through 'martha-ha serve': proposals (commits on refs/proposals/<id>), staging
tests, and deploys that only start after the user approves a notification with a one-time
nonce in the Companion app.

Deploy and rollback never rewrite history: every change, including a rollback, is a new
commit on 'main'. A failing health check after a deploy rolls back automatically.
Uses only the Python standard library (Ubuntu 26.04: Python 3.14), git and docker.
"""
import argparse
import base64
import contextlib
import datetime
import fcntl
import getpass
import grp
import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import shutil
import signal
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HA_DIR = "/opt/homeassistant"
CONFIG = HA_DIR + "/config"
COMPOSE = HA_DIR + "/docker-compose.yml"
SERVICE = "homeassistant"
STATE = "/var/lib/martha"
GIT_DIR = STATE + "/ha-config.git"
BACKUPS = STATE + "/backups"
WORK = STATE + "/work"
LOCK = "/run/lock/martha-ha.lock"
ROOT_ENV = "/etc/martha/ha-root.env"   # admin token and notify service: root only (0600)
GATE_ENV = "/etc/martha/gate.env"      # tokens of martha-gate: root:martha-gate 0640
HA_URL = "http://127.0.0.1:8123"
STAGING = STATE + "/staging"
STAGING_CONFIG = STAGING + "/config"
STAGING_SERVICE = "homeassistant-staging"
STAGING_URL = "http://172.30.53.10:8123"   # fixed address, see host/docker-compose.yml
BRANCH = "main"
# Recorder and LTSS database (ADR-001): PostgreSQL in its own container.
DB_CONTAINER = "timescaledb"
DB_NAME = DB_USER = "homeassistant"
DB_ENV = HA_DIR + "/db.env"
DB_DUMP = "database/homeassistant.pgdump"   # path of the pg_dump inside a backup

# martha-gate (phase 3): the agent's only access. It runs unprivileged and asks this
# daemon (martha-ha serve, as root) over a Unix socket for everything else.
SOCKET = "/run/martha/ha.sock"
GATE_GROUP = "martha-gate"
GATE_PORT = 8765
GATE_PACKAGE = "packages/martha_gate.yaml"            # approval automation, see setup-gate
GATE_TEMPLATE = "/usr/local/lib/martha/martha_gate.yaml"
GATE_USER_NAME = "Martha gate"                        # non-admin HA user for read access
PROPOSALS = STATE + "/proposals"
MAX_FILES = 50
MAX_FILE_SIZE = 512 * 1024
MAX_REQUEST = 32 * 1024**2
# A proposal must not touch the approval path. Defence in depth: the user still reviews
# the diff before approving.
FORBIDDEN = ("MARTHA_APPROVE", "MARTHA_REJECT", "martha_gate", "call_service", "secrets.yaml")
OPEN_STATES = ("new", "testing", "tested", "failed", "submitted")

MIN_FREE = 5 * 1024**3   # a deploy or backup aborts below this much free disk space
KEEP_COUNT = 30          # backups: always keep the newest 30 ...
KEEP_DAYS = 7            # ... and every backup younger than 7 days
HEALTH_TIMEOUT = 300     # seconds for HA to come back after a restart
SETTLE = 30              # seconds to let integrations load before reading the log

# gitignore syntax: ignore everything, then allow what may be versioned and changed.
# Never tracked: secrets.yaml, *.db, logs, deps, custom_components (arbitrary code),
# .storage/auth*, http*, core.config_entries (credentials), core.restore_state, ...
# packages/martha_storage.yaml is managed by install.sh: it points the recorder and LTSS
# at the production database, which staging and the agent must never get.
# packages/martha_gate.yaml is managed by setup-gate: no deploy or rollback may remove the
# approval automation.
ALLOWLIST = """\
/*
!/configuration.yaml
!/automations.yaml
!/scripts.yaml
!/scenes.yaml
!/customize.yaml
!/packages/
!/blueprints/
!/dashboards/
!/themes/
!/.storage/
/.storage/*
!/.storage/lovelace
!/.storage/lovelace.*
!/.storage/lovelace_dashboards
!/.storage/lovelace_resources
!/.storage/input_boolean
!/.storage/input_button
!/.storage/input_datetime
!/.storage/input_number
!/.storage/input_select
!/.storage/input_text
!/.storage/counter
!/.storage/timer
!/.storage/schedule
!/.storage/core.area_registry
!/.storage/core.floor_registry
!/.storage/core.label_registry
/packages/martha_storage.yaml
/packages/martha_gate.yaml
secrets.yaml
*.db
*.db-*
"""

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


def git(*args, check=True, **kw):
    cmd = ["git", "--git-dir", GIT_DIR, "--work-tree", CONFIG,
           "-c", "user.name=martha", "-c", "user.email=martha@localhost", *args]
    return run(cmd, check=check, **kw)


def rev(name):
    return git("rev-parse", "--verify", "--quiet", name + "^{commit}", check=False).stdout.strip()


def head():
    return rev(BRANCH)


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
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


@contextlib.contextmanager
def locked(wait=False):
    """One change at a time. The CLI fails at once; the daemon's workers wait their turn."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("another martha-ha command is running")
        yield


# --- repository -----------------------------------------------------------------

def init():
    if not os.path.isdir(CONFIG):
        raise Error(f"{CONFIG} does not exist; install Home Assistant first")
    os.makedirs(STATE, exist_ok=True)
    if not os.path.isfile(GIT_DIR + "/HEAD"):
        log(f"creating config repository {GIT_DIR}")
        run(["git", "--git-dir", GIT_DIR, "init", "--quiet", f"--initial-branch={BRANCH}"])
        os.chmod(GIT_DIR, 0o700)
    git("config", "core.worktree", CONFIG)
    git("config", "core.bare", "false")
    os.makedirs(GIT_DIR + "/info", exist_ok=True)
    with open(GIT_DIR + "/info/exclude", "w") as f:
        f.write(ALLOWLIST)
    sync("sync: initial production state" if not rev(BRANCH) else "sync: production state")


def sync(message="sync: production state"):
    """Commit the current production files, so changes made in the HA UI are never lost."""
    git("add", "--all")
    if git("diff", "--cached", "--quiet", check=False).returncode != 0 or not rev(BRANCH):
        git("commit", "--quiet", "--allow-empty", "-m", message)
        log(f"{message} ({head()[:10]})")
    return head()


def changed_paths(a, b):
    return [p for p in git("diff", "--name-only", a, b).stdout.splitlines() if p]


def tree_of(commit):
    return git("rev-parse", commit + "^{tree}").stdout.strip()


def commit_tree(tree, parent, message):
    return git("commit-tree", tree, "-p", parent, "-m", message).stdout.strip()


def merge_tree(base, ours, theirs=None):
    """Tree of merging 'theirs' into 'ours' (optionally with an explicit merge base)."""
    args = ["merge-tree", "--write-tree", "--no-messages"]
    if theirs is None:
        args += [base, ours]          # two-argument form: merge base computed by git
    else:
        args += ["--merge-base", base, ours, theirs]
    res = git(*args, check=False)
    if res.returncode == 1:
        conflicts = [l for l in res.stdout.splitlines()[1:] if l]
        raise Error("conflict with the current production config: " + ", ".join(conflicts))
    if res.returncode != 0:
        raise Error(f"git merge-tree failed: {res.stderr.strip()}")
    return res.stdout.splitlines()[0].strip()


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


def has_db():
    """True if this install keeps its recorder in PostgreSQL (ADR-001). A database that
    exists but is not running is an error: a backup without it would not be complete."""
    if not os.path.isfile(DB_ENV):
        return False
    res = run(["docker", "inspect", "-f", "{{.State.Running}}", DB_CONTAINER], check=False)
    if res.stdout.strip() != "true":
        raise Error(f"database container {DB_CONTAINER} is not running; start it first "
                    f"(docker compose -f {COMPOSE} up -d {DB_CONTAINER})")
    return True


def dump_db(dest):
    """Consistent pg_dump (custom format, uncompressed: the backup tar is zstd already)."""
    with open(dest, "wb") as f:
        res = db_exec(["pg_dump", "-Fc", "-Z0"], stdout=f, stderr=subprocess.PIPE)
    if res.returncode != 0:
        raise Error(f"pg_dump failed: {res.stderr.decode(errors='replace').strip()}")


def restore_db(dump):
    """Replace the database with 'dump'. The current database is kept aside under another
    name, so a restore never destroys data. Home Assistant must be stopped."""
    aside = f"{DB_NAME}_before_restore_{stamp().replace('-', '_')}"
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
    log(f"database restored; the previous database is kept as '{aside}' "
        f"(remove it with: DROP DATABASE {aside})")


def backup(label="manual"):
    """Full backup: the config dir plus a pg_dump of the database. SQLite databases are
    copied with the online backup API, so the copy is consistent while HA keeps running."""
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    db = has_db()
    db_size = int(psql(f"SELECT pg_database_size('{DB_NAME}')")) if db else 0
    check_free(BACKUPS, dir_size(CONFIG) + db_size)
    label = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
    path = f"{BACKUPS}/ha-{stamp()}-{label}.tar.zst"
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        dump = None
        if db:
            dump = os.path.join(tmp, "homeassistant.pgdump")
            dump_db(dump)
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
            if dump:
                tar.add(dump, arcname=DB_DUMP)
    os.chmod(path + ".partial", 0o600)
    os.replace(path + ".partial", path)
    log(f"backup {path} ({os.path.getsize(path) // 1024**2} MB)")
    prune()
    return path


def list_backups():
    with contextlib.suppress(FileNotFoundError):
        return sorted(f for f in os.listdir(BACKUPS) if f.endswith(".tar.zst"))
    return []


def prune():
    names = list_backups()
    cutoff = time.time() - KEEP_DAYS * 86400
    for name in names[:-KEEP_COUNT]:
        path = os.path.join(BACKUPS, name)
        if os.path.getmtime(path) < cutoff:
            os.remove(path)
            log(f"pruned old backup {name}")


def restore(name):
    path = name if os.path.isabs(name) else os.path.join(BACKUPS, name)
    if not os.path.isfile(path):
        raise Error(f"backup {path} not found (see 'martha-ha status')")
    backup("pre-restore")
    aside = f"{HA_DIR}/config.before-restore-{stamp()}"
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    compose("stop")
    try:
        with tempfile.TemporaryDirectory(dir=WORK) as tmp:
            dump = None
            with tarfile.open(path, "r:zst") as tar:
                members = tar.getmembers()
                config = [m for m in members if m.name == "config" or m.name.startswith("config/")]
                if not config:
                    raise Error(f"{path} holds no config directory")
                if any(m.name == DB_DUMP for m in members):
                    tar.extract(DB_DUMP, tmp, filter="data")
                    dump = os.path.join(tmp, DB_DUMP)
                os.rename(CONFIG, aside)
                tar.extractall(HA_DIR, members=config, filter="tar")
            if dump and has_db():
                restore_db(dump)
            elif os.path.isfile(DB_ENV):
                log("WARNING: this backup has no database dump; the database was left as it is")
    except BaseException:
        if os.path.isdir(aside):
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(CONFIG)
            os.rename(aside, CONFIG)
        raise
    finally:
        compose("start")
    log(f"restored {os.path.basename(path)}; the previous config is kept in {aside}")
    git("reset", "--quiet")   # index follows the restored files, history is kept
    sync(f"sync: restored backup {os.path.basename(path)}")
    ok, why = wait_healthy(time.time())
    if not ok:
        log(f"WARNING: {why}")


# --- Home Assistant -----------------------------------------------------------------

def compose(action, service=SERVICE):
    args = {"stop": ["stop", service], "start": ["up", "-d", service],
            "restart": ["restart", service]}[action]
    log(f"{action} {service}")
    run(["docker", "compose", "-f", COMPOSE, "--profile", "staging", *args])


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


def extract(treeish, dest):
    """Write the tracked files of 'treeish' (commit or tree) into 'dest'."""
    archive = subprocess.Popen(["git", "--git-dir", GIT_DIR, "archive", treeish],
                               stdout=subprocess.PIPE)
    with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
        tar.extractall(dest, filter="data")
    if archive.wait() != 0:
        raise Error(f"git archive {treeish} failed")


def check(commit):
    """Run HA's check_config on a copy of production with the tracked files of 'commit'.
    Network-less throwaway container, production is not touched."""
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        cfg = os.path.join(tmp, "config")
        shutil.copytree(CONFIG, cfg, symlinks=True, ignore=shutil.ignore_patterns(
            "*.db", "*.db-*", "*.log", "*.log.*", "deps", "tts"))
        for path in git("ls-files", "-z").stdout.split("\0"):
            if path:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(cfg, path))
        extract(commit, cfg)
        # PYTHONPATH as in production: custom integrations (LTSS) import their requirements
        # from the wheels install.sh put in pydeps.
        res = run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python3",
                   "-v", f"{cfg}:/config", "-v", f"{HA_DIR}/pydeps:/pydeps:ro",
                   "-e", "PYTHONPATH=/pydeps", ha_image(),
                   "-m", "homeassistant", "--script", "check_config", "--config", "/config"],
                  check=False)
    output = (res.stdout + res.stderr).strip()
    return res.returncode == 0, output


def activate(paths, storage):
    """Make HA pick up changed files: reload if possible, otherwise restart."""
    if storage:
        compose("start")
        return
    token = read_env(ROOT_ENV).get("HA_TOKEN")
    if token and paths and all(p.startswith(RELOADABLE) for p in paths):
        try:
            ha_api("POST", "/api/services/homeassistant/reload_all", token, timeout=120)
            log("reloaded Home Assistant configuration")
            return
        except (urllib.error.URLError, OSError) as e:
            log(f"reload failed ({e}); restarting instead")
    compose("restart")


# --- deploy / rollback --------------------------------------------------------------

def apply(build, message, verify=True, auto_rollback=True):
    """Common path for deploy and rollback. 'build(base)' returns the new tree for a
    production commit 'base'. Steps: sync, build, check_config, backup, write files,
    activate, health check, automatic rollback on failure."""
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    check_free(STATE)
    base = sync()
    tree = build(base)
    if tree == tree_of(base):
        log("nothing to change: production already has this configuration")
        return base
    paths = changed_paths(base, tree)
    storage = any(p.startswith(".storage/") for p in paths)
    candidate = commit_tree(tree, base, message)
    if verify:
        ok, out = check(candidate)
        if not ok:
            raise Error("check_config failed, nothing was changed:\n" + out)
        log("check_config passed")
    backup("pre-" + message.split(":", 1)[0])

    since = time.time()
    if storage:
        # HA writes .storage from memory on shutdown: stop first, then take the final state.
        compose("stop")
        base = sync()
        tree = build(base)
        paths = changed_paths(base, tree)
        candidate = commit_tree(tree, base, message)
    try:
        git("read-tree", "-u", "-m", base, candidate)
        git("update-ref", f"refs/heads/{BRANCH}", candidate, base)
    except Error:
        if storage:
            compose("start")
        raise
    log(f"{message} ({candidate[:10]}): {len(paths)} file(s) changed")
    activate(paths, storage)

    ok, why = wait_healthy(since)
    if ok:
        log("health check passed")
        return candidate
    log(f"health check FAILED: {why}")
    if not auto_rollback:
        raise Error(f"health check failed after rollback, check Home Assistant by hand: {why}")
    log("rolling back automatically")
    apply(lambda b: merge_tree(candidate, b, base),
          f"revert: automatic rollback ({candidate[:10]})", verify=False, auto_rollback=False)
    raise Error(f"deploy rolled back: {why}")


def resolve(ref):
    target = rev(ref)
    if not target:
        raise Error(f"unknown revision {ref}")
    return target


def deploy(ref, message=None):
    target = resolve(ref)
    subject = git("log", "-1", "--format=%s", target).stdout.strip()
    message = "deploy: " + (message or subject)
    return apply(lambda base: merge_tree(base, target), message)


def last_deploy():
    """Most recent deploy that has not been reverted yet (reverts name it as '(<sha10>)')."""
    reverted = set()
    for line in git("log", "--format=%H %s", BRANCH).stdout.splitlines():
        sha, subject = line.split(" ", 1)
        if subject.startswith("revert:"):
            reverted.update(re.findall(r"\(([0-9a-f]{10})\)", subject))
        elif subject.startswith("deploy:") and sha[:10] not in reverted:
            return sha
    raise Error("no deploy found to roll back")


def rollback(ref=None, to=None):
    if to:
        target = rev(to)
        if not target:
            raise Error(f"unknown revision {to}")
        tree = tree_of(target)
        return apply(lambda base: tree, f"revert: restore configuration of {target[:10]}")
    commit = rev(ref) if ref else last_deploy()
    if not commit:
        raise Error(f"unknown revision {ref}")
    parent = rev(commit + "^")
    subject = git("log", "-1", "--format=%s", commit).stdout.strip()
    # Undo only this commit, keep everything that happened after it (e.g. UI changes).
    return apply(lambda base: merge_tree(commit, base, parent),
                 f"revert: {subject} ({commit[:10]})")


# --- staging --------------------------------------------------------------------------

def dummy_secrets():
    """secrets.yaml for staging: the keys of production, with harmless values of the same
    kind, so the config loads without staging ever seeing a real secret."""
    lines = ["# Generated by martha-ha: dummy values, staging never gets real secrets."]
    with contextlib.suppress(FileNotFoundError):
        with open(os.path.join(CONFIG, "secrets.yaml")) as f:
            for line in f:
                m = re.match(r"^([A-Za-z0-9_]+):\s*(.*?)\s*$", line)
                if not m:
                    continue
                value = m.group(2)
                if re.fullmatch(r"-?\d+", value):
                    dummy = "0"
                elif re.fullmatch(r"-?\d*\.\d+", value):
                    dummy = "0.0"
                elif value.lower() in ("true", "false", "yes", "no", "on", "off"):
                    dummy = "false"
                else:
                    dummy = '"staging-dummy"'
                lines.append(f"{m.group(1)}: {dummy}")
    return "\n".join(lines) + "\n"


def staging_files(tree):
    """Refresh the tracked files in the staging config; everything else there (logins,
    .storage, database, the Companion app registration) belongs to staging and stays."""
    os.makedirs(STAGING_CONFIG, mode=0o755, exist_ok=True)
    listing = os.path.join(STAGING, "tracked")
    with contextlib.suppress(FileNotFoundError):
        with open(listing) as f:
            for path in f.read().splitlines():
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(STAGING_CONFIG, path))
    extract(tree, STAGING_CONFIG)
    with open(listing, "w") as f:
        f.write(git("ls-tree", "-r", "--name-only", tree).stdout)
    with open(os.path.join(STAGING_CONFIG, "secrets.yaml"), "w") as f:
        f.write(dummy_secrets())


def staging_up(ref=None):
    """Run staging with production plus 'ref' merged in. Returns (ok, why, tree)."""
    base = sync()
    tree = merge_tree(base, resolve(ref)) if ref else tree_of(base)
    with contextlib.suppress(Error):
        compose("stop", STAGING_SERVICE)   # staging writes .storage on shutdown
    staging_files(tree)
    since = time.time()
    compose("start", STAGING_SERVICE)
    ok, why = wait_healthy(since, STAGING_URL, STAGING_SERVICE)
    log(f"staging runs {ref or 'production'} ({tree[:10]}) on port 8124")
    return ok, why, tree


def staging_test(ref):
    """check_config on production + ref, then boot it on staging and read the log."""
    base = sync()
    candidate = commit_tree(merge_tree(base, resolve(ref)), base, f"test: {ref}")
    ok, out = check(candidate)
    if not ok:
        return False, "check_config failed:\n" + out
    log("check_config passed")
    ok, why, _ = staging_up(ref)
    if not ok:
        return False, "staging: " + why
    return True, "check_config and staging boot passed"


# --- proposals (phase 3) ----------------------------------------------------------------
# A proposal is a commit on refs/proposals/<id> on top of production, plus its metadata
# in PROPOSALS/<id>.json. Life cycle:
#   new -> testing -> tested | failed -> submitted -> approved -> deployed | failed-deploy
#                                                 \-> rejected
# A rollback request starts as 'submitted': it only needs the user's approval.

_meta_lock = threading.Lock()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def check_id(pid):
    if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{20}", pid):
        raise Error("invalid proposal id")
    return pid


def load_proposal(pid):
    try:
        with open(f"{PROPOSALS}/{check_id(pid)}.json") as f:
            return json.load(f)
    except FileNotFoundError:
        raise Error(f"unknown proposal {pid}")


def save_proposal(p):
    os.makedirs(PROPOSALS, mode=0o700, exist_ok=True)
    path = f"{PROPOSALS}/{p['id']}.json"
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(p, f, indent=1)
    os.replace(path + ".tmp", path)


def update_proposal(pid, expect=None, **fields):
    """Change a proposal's metadata; with 'expect', only from one of those states."""
    with _meta_lock:
        p = load_proposal(pid)
        if expect and p["state"] not in expect:
            raise Error(f"proposal {pid} is '{p['state']}', expected {' or '.join(expect)}")
        p.update(fields, updated=now_iso())
        save_proposal(p)
        return p


def public(p):
    """What martha-gate (and so the agent) may see: never the nonce hash."""
    return {k: v for k, v in p.items() if k != "nonce_sha256"}


def all_proposals():
    with contextlib.suppress(FileNotFoundError):
        names = sorted(os.listdir(PROPOSALS))
        out = []
        for name in names:
            if re.fullmatch(r"[0-9a-f]{20}\.json", name):
                with contextlib.suppress(Error, ValueError):
                    out.append(load_proposal(name[:-5]))
        return sorted(out, key=lambda p: p["created"])
    return []


def check_path(path):
    if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
            or "\0" in path or posixpath.normpath(path) != path or path.split("/")[0] == ".."):
        raise Error(f"invalid path {path!r}")
    return path


def validate_files(files):
    if not isinstance(files, dict) or not files:
        raise Error("'files' must be an object {path: content or null}")
    if len(files) > MAX_FILES:
        raise Error(f"at most {MAX_FILES} files per proposal")
    for path, content in files.items():
        check_path(path)
        if path == GATE_PACKAGE or path.endswith("secrets.yaml"):
            raise Error(f"{path} may not be changed")
        # info/exclude is the allowlist: an ignored path is not versioned, so not proposable.
        if git("check-ignore", "--no-index", "-q", "--", path, check=False).returncode == 0:
            raise Error(f"{path} is outside the allowlist of the config repository")
        if content is None:
            continue
        if not isinstance(content, str):
            raise Error(f"content of {path} must be a string or null")
        if len(content.encode()) > MAX_FILE_SIZE:
            raise Error(f"{path} is larger than {MAX_FILE_SIZE // 1024} KB")
        for marker in FORBIDDEN:
            if marker.lower() in content.lower():
                raise Error(f"{path} mentions '{marker}', which is reserved for the approval path")
        if path.startswith(".storage/"):
            try:
                json.loads(content)
            except ValueError as e:
                raise Error(f"{path} is not valid JSON: {e}")


def check_text(value, name, limit, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise Error(f"'{name}' must be a string of at most {limit} characters")
    return value.strip()


def create_proposal(title, files, description=""):
    title = check_text(title, "title", 120).splitlines()[0]
    description = check_text(description, "description", 4000, required=False)
    validate_files(files)
    with locked(wait=True):
        base = sync()
        os.makedirs(WORK, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORK) as tmp:
            env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
            git("read-tree", base, env=env)
            for path, content in files.items():
                if content is None:
                    git("update-index", "--force-remove", "--", path, env=env)
                else:
                    blob = git("hash-object", "-w", "--stdin", input=content).stdout.strip()
                    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            tree = git("write-tree", env=env).stdout.strip()
        if tree == tree_of(base):
            raise Error("the proposal changes nothing")
        pid = secrets.token_hex(10)
        commit = commit_tree(tree, base, f"proposal {pid}: {title}")
        git("update-ref", f"refs/proposals/{pid}", commit)
    p = {"id": pid, "kind": "change", "title": title, "description": description,
         "state": "new", "created": now_iso(), "updated": now_iso(), "base": base,
         "commit": commit, "paths": changed_paths(base, commit), "output": "", "deploy": None}
    save_proposal(p)
    log(f"proposal {pid} created: {title}")
    return public(p)


def rollback_request(deploy_ref, reason=""):
    """The agent asks to undo a deploy; it becomes a proposal that only needs approval."""
    reason = check_text(reason, "reason", 4000, required=False)
    if not isinstance(deploy_ref, str) or not re.fullmatch(r"[0-9a-f]{7,40}", deploy_ref):
        raise Error("'deploy' must be a commit id")
    target = rev(deploy_ref)
    if not target or git("merge-base", "--is-ancestor", target, BRANCH, check=False).returncode != 0:
        raise Error(f"{deploy_ref} is not a commit on production")
    subject = git("log", "-1", "--format=%s", target).stdout.strip()
    if not subject.startswith("deploy:"):
        raise Error(f"{target[:10]} is not a deploy ({subject})")
    pid = secrets.token_hex(10)
    p = {"id": pid, "kind": "rollback", "title": f"Terugdraaien: {subject[len('deploy:'):].strip()}",
         "description": reason, "state": "new", "created": now_iso(), "updated": now_iso(),
         "base": target, "commit": target, "target": target,
         "paths": changed_paths(target + "^", target), "output": "", "deploy": None}
    save_proposal(p)
    log(f"rollback request {pid} for {target[:10]}")
    return submit(pid, expect=("new",))


def proposal_diff(p):
    if p["kind"] == "rollback":
        return git("diff", "--no-color", p["target"], p["target"] + "^").stdout
    return git("diff", "--no-color", p["base"], p["commit"]).stdout


def show_proposal(pid):
    p = load_proposal(pid)
    return dict(public(p), diff=proposal_diff(p)[:2 * 1024**2])


# Long tasks (staging test, deploy) run in worker threads; requests answer at once and the
# agent polls the proposal's state. Workers are joined before the daemon exits.
_workers = []


def background(fn, *args):
    t = threading.Thread(target=fn, args=args)
    _workers.append(t)
    t.start()
    _workers[:] = [w for w in _workers if w.is_alive()]


def test_proposal(pid):
    p = update_proposal(pid, expect=("new", "tested", "failed"), state="testing", output="")

    def work():
        try:
            with locked(wait=True):
                ok, why = staging_test(f"refs/proposals/{pid}")
            update_proposal(pid, state="tested" if ok else "failed", output=why[-20000:])
        except Exception as e:   # noqa: BLE001 - every failure must end the 'testing' state
            update_proposal(pid, state="failed", output=f"error: {e}")
        log(f"proposal {pid} test: {load_proposal(pid)['state']}")
    background(work)
    return public(p)


def gate_url(pid):
    return f"http://{socket.gethostname()}.local:{GATE_PORT}/p/{pid}"


def notify(title, message, data=None):
    env = read_env(ROOT_ENV)
    service, token = env.get("NOTIFY_SERVICE"), env.get("HA_TOKEN")
    if not service or not token:
        raise Error("no notify service or HA token; run 'martha-ha setup-gate' first")
    body = {"title": title, "message": message}
    if data:
        body["data"] = data
    try:
        ha_api("POST", f"/api/services/notify/{service}", token, body, timeout=30)
    except (urllib.error.URLError, OSError) as e:
        raise Error(f"notification via notify.{service} failed: {e}")


def submit(pid, expect=("tested", "submitted")):
    """Ask the user for approval: a notification with a one-time nonce in its actions.
    Only the nonce's hash is stored; the agent never sees the nonce. Submitting again sends
    a new notification with a new nonce, so the buttons of the old one stop working."""
    nonce = secrets.token_hex(16)
    p = update_proposal(pid, expect=expect, state="submitted",
                        nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest())
    files = ", ".join(p["paths"][:5]) + (" ..." if len(p["paths"]) > 5 else "")
    try:
        notify(f"Martha: {p['title']}", f"{p['description'][:300]}\n{files}".strip(), {
            "tag": f"martha-{pid}",
            # A plain tap opens the diff page (iOS: url, Android: clickAction); the buttons
            # appear on a long press (iOS) or by expanding the notification (Android).
            "url": gate_url(pid), "clickAction": gate_url(pid),
            "actions": [
                {"action": f"MARTHA_APPROVE_{pid}_{nonce}", "title": "Toepassen"},
                {"action": f"MARTHA_REJECT_{pid}_{nonce}", "title": "Afwijzen"},
                {"action": "URI", "title": "Bekijken", "uri": gate_url(pid)},
            ]})
    except Error:
        update_proposal(pid, state=expect[0], nonce_sha256=None)
        raise
    log(f"proposal {pid} submitted for approval")
    return public(p)


RESEND_INTERVAL = 30   # seconds between two resends from the diff page


def resend(pid):
    """From the diff page (no login): send the approval notification again. iOS removes a
    notification once it is tapped, so after reading the diff the user asks for a new one.
    It can only notify the user's own phone; the new nonce replaces the old one."""
    p = load_proposal(pid)
    if p["state"] != "submitted":
        raise Error(f"proposal {pid} is '{p['state']}', not waiting for approval")
    age = time.time() - datetime.datetime.fromisoformat(p["updated"]).timestamp()
    if age < RESEND_INTERVAL:
        raise Error(f"wait {int(RESEND_INTERVAL - age) + 1} seconds before sending it again")
    return submit(pid, expect=("submitted",))


def stop_idle_staging():
    """Staging only runs while a proposal is open (martha has little RAM)."""
    if not any(p["state"] in OPEN_STATES for p in all_proposals()):
        with contextlib.suppress(Error):
            compose("stop", STAGING_SERVICE)


def approval(action):
    """Handle a tapped notification action: MARTHA_<APPROVE|REJECT>_<id>_<nonce>."""
    parts = action.split("_", 3) if isinstance(action, str) else []
    if len(parts) != 4 or parts[0] != "MARTHA" or parts[1] not in ("APPROVE", "REJECT"):
        raise Error("invalid approval action")
    _, verdict, pid, nonce = parts
    with _meta_lock:
        p = load_proposal(pid)
        stored = p.get("nonce_sha256") or ""
        given = hashlib.sha256(nonce.encode()).hexdigest()
        if p["state"] != "submitted" or not hmac.compare_digest(stored, given):
            raise Error("this approval is not valid (anymore)")
        p.update(state="approved" if verdict == "APPROVE" else "rejected",
                 nonce_sha256=None, updated=now_iso())   # a nonce works once
        save_proposal(p)
    log(f"proposal {pid} {p['state']} by the user")
    tag = {"tag": f"martha-{pid}"}
    if verdict == "REJECT":
        with contextlib.suppress(Error):
            notify("Martha: afgewezen", p["title"], tag)
        with contextlib.suppress(Error), locked(wait=True):
            stop_idle_staging()
        return public(p)

    def work():
        try:
            with locked(wait=True):
                if p["kind"] == "rollback":
                    sha = rollback(p["target"])
                else:
                    sha = deploy(f"refs/proposals/{pid}", p["title"])
                stop_idle_staging()
            update_proposal(pid, state="deployed", deploy=sha)
            message = f"Toegepast: {p['title']}"
        except Exception as e:   # noqa: BLE001 - every failure must end the 'approved' state
            update_proposal(pid, state="failed-deploy", output=f"error: {e}"[-20000:])
            message = f"Mislukt, er is niets veranderd of het is teruggedraaid: {p['title']}\n{e}"[:1000]
        with contextlib.suppress(Error):
            notify("Martha", message, tag)
    background(work)
    return public(p)


def reject_open(pid):
    """Withdraw a proposal that is not submitted yet (agent side)."""
    return public(update_proposal(pid, expect=("new", "tested", "failed"), state="rejected"))


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def ha_logs(target="production", lines=200):
    service = {"production": SERVICE, "staging": STAGING_SERVICE}.get(target)
    if not service:
        raise Error("target must be production or staging")
    lines = max(1, min(int(lines), 2000))
    res = run(["docker", "logs", "--tail", str(lines), service], check=False)
    return ANSI.sub("", res.stdout + res.stderr)


def render_template(template):
    """HA's /api/template needs an admin; rendering cannot change anything, so the daemon
    does it with the root token on behalf of the gate."""
    template = check_text(template, "template", 20000)
    token = read_env(ROOT_ENV).get("HA_TOKEN")
    if not token:
        raise Error("no HA token; run 'martha-ha setup-gate' first")
    req = urllib.request.Request(HA_URL + "/api/template", method="POST",
                                 data=json.dumps({"template": template}).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        raise Error(f"template error: {e.read().decode(errors='replace')[:2000]}")
    except (urllib.error.URLError, OSError) as e:
        raise Error(f"Home Assistant not reachable: {e}")


def repo_files():
    return [p for p in git("ls-tree", "-r", "--name-only", BRANCH).stdout.splitlines() if p]


def repo_file(path):
    check_path(path)
    res = git("show", f"{BRANCH}:{path}", check=False)
    if res.returncode != 0:
        raise Error(f"{path} is not in the configuration")
    return res.stdout


def repo_history(path=None, n=20):
    n = max(1, min(int(n), 200))
    args = ["log", f"-{n}", "--format=%H%x09%aI%x09%s", BRANCH]
    if path:
        args += ["--", check_path(path)]
    out = []
    for line in git(*args).stdout.splitlines():
        sha, date, subject = line.split("\t", 2)
        out.append({"commit": sha, "date": date, "subject": subject})
    return out


# --- daemon (martha-ha serve) -----------------------------------------------------------

def dispatch(req):
    cmd, a = req.get("cmd"), req
    if cmd == "create":
        return create_proposal(a.get("title"), a.get("files"), a.get("description", ""))
    if cmd == "list":
        return [public(p) for p in all_proposals()]
    if cmd == "show":
        return show_proposal(a.get("id"))
    if cmd == "test":
        return test_proposal(check_id(a.get("id")))
    if cmd == "submit":
        return submit(check_id(a.get("id")))
    if cmd == "resend":
        return resend(check_id(a.get("id")))
    if cmd == "withdraw":
        return reject_open(check_id(a.get("id")))
    if cmd == "approval":
        return approval(a.get("action"))
    if cmd == "rollback_request":
        return rollback_request(a.get("deploy"), a.get("reason", ""))
    if cmd == "logs":
        return ha_logs(a.get("target", "production"), a.get("lines", 200))
    if cmd == "template":
        return render_template(a.get("template"))
    if cmd == "files":
        return repo_files()
    if cmd == "file":
        return repo_file(a.get("path"))
    if cmd == "history":
        return repo_history(a.get("path"), a.get("n", 20))
    raise Error(f"unknown command {cmd!r}")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline(MAX_REQUEST))
            if not isinstance(req, dict):
                raise Error("request must be a JSON object")
            resp = {"ok": True, "result": dispatch(req)}
        except (Error, ValueError, TypeError) as e:
            resp = {"ok": False, "error": str(e)}
        except Exception:   # noqa: BLE001 - keep the daemon alive, log the bug
            traceback.print_exc()
            resp = {"ok": False, "error": "internal error, see journalctl -u martha-ha"}
        self.wfile.write((json.dumps(resp) + "\n").encode())


def recover():
    """States that were in progress when the daemon stopped."""
    for p in all_proposals():
        if p["state"] == "testing":
            update_proposal(p["id"], state="failed", output="interrupted: martha-ha restarted")
        elif p["state"] == "approved":
            update_proposal(p["id"], state="failed-deploy",
                            output="interrupted: martha-ha restarted; check 'martha-ha status'")


def serve():
    os.makedirs(os.path.dirname(SOCKET), mode=0o755, exist_ok=True)
    os.makedirs(PROPOSALS, mode=0o700, exist_ok=True)
    recover()
    with contextlib.suppress(FileNotFoundError):
        os.remove(SOCKET)
    old = os.umask(0o077)
    try:
        server = socketserver.ThreadingUnixStreamServer(SOCKET, Handler)
    finally:
        os.umask(old)
    os.chown(SOCKET, 0, grp.getgrnam(GATE_GROUP).gr_gid)
    os.chmod(SOCKET, 0o660)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    log(f"listening on {SOCKET}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        for t in list(_workers):
            t.join()   # let a running test or deploy finish (TimeoutStopSec in the unit)
    return 0


# --- setup-gate ---------------------------------------------------------------------------

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


def gate_user_token(admin_token):
    """(Re)create the non-admin, local-only HA user that martha-gate reads production with."""
    ws = WebSocket(HA_URL, admin_token)
    try:
        for u in ws.call("config/auth/list"):
            if u.get("name") == GATE_USER_NAME:
                ws.call("config/auth/delete", user_id=u["id"])
        user = ws.call("config/auth/create", name=GATE_USER_NAME,
                       group_ids=["system-users"], local_only=True)["user"]
        password = secrets.token_urlsafe(32)
        ws.call("config/auth_provider/homeassistant/create", user_id=user["id"],
                username="martha-gate", password=password)
    finally:
        ws.close()
    return long_lived_token(HA_URL, ha_login(HA_URL, "martha-gate", password), "martha-gate")


def write_env(path, values, mode, gid=0):
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Written by 'martha-ha setup-gate'. Do not share.\n")
        f.writelines(f"{k}={v}\n" for k, v in values.items())
    os.chown(path + ".tmp", 0, gid)
    os.chmod(path + ".tmp", mode)
    os.replace(path + ".tmp", path)


def set_secret(key, value):
    path = os.path.join(CONFIG, "secrets.yaml")
    lines = []
    with contextlib.suppress(FileNotFoundError):
        with open(path) as f:
            lines = [l for l in f.read().splitlines() if not l.startswith(key + ":")]
    lines.append(f'{key}: "{value}"')
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)


def ask_login(what):
    print(f"Log in to {what} (an administrator account):")
    username = input("  username: ").strip()
    return username, getpass.getpass("  password: ")


def pick_notify_service(token):
    services = ha_api("GET", "/api/services", token)
    names = sorted(name for d in services if d["domain"] == "notify"
                   for name in d["services"] if name.startswith("mobile_app_"))
    if not names:
        raise Error("no Companion app found: log in to production with the app first")
    if len(names) == 1:
        return names[0]
    for i, name in enumerate(names, 1):
        print(f"  {i}. notify.{name}")
    choice = input("Which phone gets the approval notifications? [1] ").strip() or "1"
    return names[int(choice) - 1]


def setup_gate(rotate=False):
    """Interactive: tokens, notify service, approval automation, martha-gate service."""
    gid = grp.getgrnam(GATE_GROUP).gr_gid
    os.makedirs("/etc/martha", mode=0o755, exist_ok=True)
    old = read_env(GATE_ENV)

    user, password = ask_login(f"production Home Assistant ({HA_URL})")
    admin = ha_login(HA_URL, user, password)
    root_token = long_lived_token(HA_URL, admin, "martha-root")
    log("created long-lived token 'martha-root' for martha-ha")
    gate_token = gate_user_token(admin)
    log(f"created non-admin user '{GATE_USER_NAME}' with its own token")
    service = pick_notify_service(root_token)

    try:
        with urllib.request.urlopen(STAGING_URL + "/manifest.json", timeout=5):
            pass
    except (urllib.error.URLError, OSError):
        ok, why, _ = staging_up()
        if not ok:
            raise Error(f"staging does not run: {why}")
    user, password = ask_login(f"staging Home Assistant (http://{socket.gethostname()}.local:8124)")
    staging_token = long_lived_token(STAGING_URL, ha_login(STAGING_URL, user, password), "martha-gate")
    log("created long-lived token 'martha-gate' on staging")

    agent_token = old.get("AGENT_TOKEN") if not rotate else None
    approval_secret = old.get("APPROVAL_SECRET") if not rotate else None
    agent_token = agent_token or secrets.token_urlsafe(32)
    approval_secret = approval_secret or secrets.token_urlsafe(32)
    write_env(ROOT_ENV, {"HA_TOKEN": root_token, "NOTIFY_SERVICE": service}, 0o600)
    write_env(GATE_ENV, {"GATE_HA_TOKEN": gate_token, "STAGING_TOKEN": staging_token,
                         "AGENT_TOKEN": agent_token, "APPROVAL_SECRET": approval_secret}, 0o640, gid)
    log(f"wrote {ROOT_ENV} and {GATE_ENV}")

    # The approval automation: not in the config repo, so no deploy or rollback removes it.
    backup("pre-setup-gate")
    set_secret("martha_gate_secret", approval_secret)
    package = os.path.join(CONFIG, GATE_PACKAGE)
    os.makedirs(os.path.dirname(package), exist_ok=True)
    shutil.copyfile(GATE_TEMPLATE, package)
    os.chmod(package, 0o644)
    ok, out = check(head())
    if not ok:
        os.remove(package)
        raise Error("check_config failed with the approval package, it was removed again:\n" + out)
    since = time.time()
    compose("restart")
    ok, why = wait_healthy(since)
    if not ok:
        raise Error(f"Home Assistant is not healthy after adding {GATE_PACKAGE}: {why}")
    log(f"{GATE_PACKAGE} is active")

    run(["systemctl", "enable", "martha-gate.service"])
    run(["systemctl", "restart", "martha-gate.service"])
    notify("Martha", "martha-gate is ingesteld. Goedkeuringen komen voortaan hier binnen.")
    log(f"martha-gate runs on port {GATE_PORT}; test notification sent to notify.{service}")
    log(f"the agent's token is AGENT_TOKEN in {GATE_ENV}")


def list_proposals():
    for p in all_proposals():
        print(f"{p['id']}  {p['updated'][:16]}  {p['state']:<13}  {p['kind']:<8}  {p['title']}")


def status():
    if not rev(BRANCH):
        print("config repository not initialised (run 'martha-ha init')")
        return
    print(f"production: {head()[:10]}")
    print(git("log", "-15", "--format=  %h %ad %s", "--date=format:%Y-%m-%d %H:%M", BRANCH).stdout, end="")
    dirty = git("status", "--porcelain").stdout.strip()
    print("uncommitted UI changes: " + ("yes (martha-ha sync)" if dirty else "none"))
    names = list_backups()
    print(f"backups in {BACKUPS}: {len(names)}")
    for name in names[-5:]:
        print(f"  {name}  {os.path.getsize(os.path.join(BACKUPS, name)) // 1024**2} MB")


def main(argv=None):
    p = argparse.ArgumentParser(prog="martha-ha", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("sync"); s.add_argument("-m", "--message", default="sync: production state")
    sub.add_parser("status")
    s = sub.add_parser("backup"); s.add_argument("label", nargs="?", default="manual")
    s = sub.add_parser("check"); s.add_argument("rev")
    s = sub.add_parser("deploy"); s.add_argument("rev"); s.add_argument("-m", "--message")
    s = sub.add_parser("rollback"); s.add_argument("rev", nargs="?"); s.add_argument("--to")
    s = sub.add_parser("restore"); s.add_argument("backup")
    s = sub.add_parser("staging")
    s.add_argument("action", choices=["up", "test", "down"]); s.add_argument("rev", nargs="?")
    sub.add_parser("serve")
    s = sub.add_parser("setup-gate"); s.add_argument("--rotate", action="store_true")
    sub.add_parser("proposals")
    a = p.parse_args(argv)

    if os.geteuid() != 0:
        print("martha-ha must run as root", file=sys.stderr)
        return 2
    if a.cmd == "serve":
        return serve()   # takes the lock per task, not for its whole life
    if a.cmd == "proposals":
        list_proposals()   # read only: works while a deploy runs
        return 0
    try:
        with locked():
            if a.cmd == "init":
                init()
            elif a.cmd == "status":
                status()
            elif a.cmd == "sync":
                sync(a.message)
            elif a.cmd == "backup":
                backup(a.label)
            elif a.cmd == "check":
                ok, out = check(rev(a.rev) or a.rev)
                print(out)
                return 0 if ok else 1
            elif a.cmd == "deploy":
                deploy(a.rev, a.message)
            elif a.cmd == "rollback":
                rollback(a.rev, a.to)
            elif a.cmd == "restore":
                restore(a.backup)
            elif a.cmd == "setup-gate":
                setup_gate(a.rotate)
            elif a.cmd == "staging":
                if a.action == "down":
                    compose("stop", STAGING_SERVICE)
                elif a.action == "up":
                    ok, why, _ = staging_up(a.rev)
                    log(f"staging health: {why}")
                    return 0 if ok else 1
                else:
                    if not a.rev:
                        raise Error("staging test needs a revision")
                    ok, why = staging_test(a.rev)
                    log(("PASSED: " if ok else "FAILED: ") + why)
                    return 0 if ok else 1
    except Error as e:
        log(f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
