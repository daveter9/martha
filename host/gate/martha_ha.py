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

Staging is a second HA container on an internal Docker network (no LAN, no internet),
reachable on the LAN at port 8124 through martha-staging-proxy. It has its own logins,
.storage and database; only the tracked files come from production and the proposal, and
secrets.yaml holds dummy values.

Deploy and rollback never rewrite history: every change, including a rollback, is a new
commit on 'main'. A failing health check after a deploy rolls back automatically.
Uses only the Python standard library (Ubuntu 26.04: Python 3.14), git and docker.
"""
import argparse
import contextlib
import datetime
import fcntl
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
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
ENV_FILE = "/etc/martha/gate.env"
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
def locked():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def ha_api(method, path, token, timeout=10):
    req = urllib.request.Request(HA_URL + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


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
    token = read_env(ENV_FILE).get("HA_TOKEN")
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
    a = p.parse_args(argv)

    if os.geteuid() != 0:
        print("martha-ha must run as root", file=sys.stderr)
        return 2
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
