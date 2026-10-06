#!/usr/bin/env bash
# Installs Docker (docker.io), Home Assistant and TimescaleDB on Ubuntu 26.04 from the
# offline bundle.
#
# Runs automatically on first boot after the autoinstall (martha-firstboot.service),
# but can also be run by hand on an existing Ubuntu 26.04 install, e.g. from a USB stick:
#   sudo bash /media/<user>/<usb>/martha/host/install.sh
# Idempotent: re-running with a newer bundle upgrades Docker, Home Assistant and TimescaleDB.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFLINE="$ROOT/offline"
HA_DIR=/opt/homeassistant
STATE_DIR=/var/lib/martha
KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg

log() { echo "[martha] $*"; }
die() { echo "[martha] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0)"
[ -f "$OFFLINE/bundle.env" ] || die "no offline bundle found at $OFFLINE (run 1. download/download.ps1 first)"
case "$OFFLINE" in *[[:space:]]*) die "bundle path must not contain spaces: $OFFLINE" ;; esac
# shellcheck source=/dev/null
. "$OFFLINE/bundle.env"
. /etc/os-release
[ "${VERSION_CODENAME:-}" = "${UBUNTU_SUITES%% *}" ] ||
    die "bundle is for Ubuntu ${UBUNTU_SUITES%% *}, this system is ${VERSION_CODENAME:-unknown}"

: "${TSDB_IMAGE:?bundle has no TimescaleDB image; run 1. download/download.ps1 again}"

log "bundle created $BUNDLE_CREATED, Home Assistant $HA_VERSION, TimescaleDB $TSDB_VERSION"

# --- 0. Wired network fallback ------------------------------------------------
# The PC is usually installed without a network. If netplan has no ethernet config
# at all (e.g. a manual install with "Continue without network"), add DHCP on every
# wired port, so a cable plugged in later just works. Existing config is left alone.
NETPLAN_LAN=/etc/netplan/90-martha-lan.yaml
if command -v netplan >/dev/null 2>&1; then
    ethernets="$(netplan get ethernets 2>/dev/null || true)"
    case "$ethernets" in
    "" | null | "{}")
        log "no wired network configured, adding DHCP on all ethernet ports ($NETPLAN_LAN)"
        (
            umask 077
            cat >"$NETPLAN_LAN" <<'EOF'
# Added by Martha install.sh: DHCP on every wired port, also when no cable was
# connected during installation.
network:
  version: 2
  ethernets:
    martha-lan:
      match:
        name: "e*"
      dhcp4: true
      # Don't block boot waiting for a network that may not be there.
      optional: true
EOF
        )
        # Takes effect now if possible, otherwise at the next boot.
        { netplan generate && networkctl reload; } >/dev/null 2>&1 ||
            log "WARNING: could not activate $NETPLAN_LAN now; it is used after a reboot"
        ;;
    esac
fi

# --- 1. Docker from the local (signed) partial mirror ------------------------
# apt runs with its own sources, lists and cache in a temp dir, so the system's
# apt configuration is left untouched. apt verifies InRelease against the Ubuntu
# archive keyring and every Packages/.deb hash, exactly as with the online archive.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/sources.d" "$work/lists/partial" "$work/cache/archives/partial"
cat >"$work/sources.d/martha-offline.sources" <<EOF
Types: deb
URIs: file:$OFFLINE/apt
Suites: $UBUNTU_SUITES
Components: $UBUNTU_COMPONENTS
Signed-By: $KEYRING
EOF
apt_opts=(
    -o "Dir::Etc::SourceList=/dev/null"
    -o "Dir::Etc::SourceParts=$work/sources.d"
    -o "Dir::State::Lists=$work/lists"
    -o "Dir::Cache=$work/cache"
    -o "Acquire::Languages=none"
    -o "Acquire::Check-Valid-Until=false"
    # The partial mirror only has Packages indexes; the system config also asks for
    # command-not-found and AppStream metadata, and the missing CNF files fail update.
    -o "Acquire::IndexTargets::deb::CNF::DefaultEnabled=false"
    -o "Acquire::IndexTargets::deb::DEP-11::DefaultEnabled=false"
    -o "APT::Sandbox::User=root"
    -o "DPkg::Lock::Timeout=600"
)
export DEBIAN_FRONTEND=noninteractive
log "installing $APT_PACKAGES from offline mirror"
apt-get "${apt_opts[@]}" update
# shellcheck disable=SC2086
apt-get "${apt_opts[@]}" install -y --no-install-recommends \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold $APT_PACKAGES

systemctl enable --now docker.service
for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 || die "docker daemon did not start"

# avahi-daemon announces <hostname>.local. Keep it off the Docker bridge, otherwise
# it also publishes 172.17.0.1 and clients may pick that unreachable address.
AVAHI_CONF=/etc/avahi/avahi-daemon.conf
if [ -f "$AVAHI_CONF" ]; then
    if grep -q '^#\?deny-interfaces=' "$AVAHI_CONF"; then
        sed -i 's/^#\?deny-interfaces=.*/deny-interfaces=docker0/' "$AVAHI_CONF"
    else
        sed -i 's/^\[server\]$/[server]\ndeny-interfaces=docker0/' "$AVAHI_CONF"
    fi
    systemctl enable avahi-daemon.service
    systemctl restart avahi-daemon.service
fi

# --- 2. Container images -----------------------------------------------------------
load_image() { # <ref> <oci-layout-dir>
    local ref="$1" dir="$2" out loaded
    if docker image inspect "$ref" >/dev/null 2>&1; then
        log "image $ref already present"
        return
    fi
    log "loading $ref (this takes a few minutes)"
    out="$(tar -C "$dir" -cf - . | docker load)"
    echo "$out"
    if ! docker image inspect "$ref" >/dev/null 2>&1; then
        # Older loaders ignore the OCI name annotation; tag whatever was loaded.
        loaded="$(sed -n -e 's/^Loaded image ID: //p' -e 's/^Loaded image: //p' <<<"$out" | head -n1)"
        [ -n "$loaded" ] || die "docker load did not report an image"
        docker tag "$loaded" "$ref"
    fi
}
load_image "$HA_IMAGE" "$OFFLINE/images/homeassistant"
load_image "$TSDB_IMAGE" "$OFFLINE/images/timescaledb"

# --- 3. Compose project ---------------------------------------------------------
COMPOSE=(docker compose -f "$HA_DIR/docker-compose.yml")
CONF="$HA_DIR/config"
# Home Assistant is stopped while its config, Python packages and database change.
if [ -f "$HA_DIR/docker-compose.yml" ] && [ -f "$HA_DIR/.env" ]; then
    "${COMPOSE[@]}" stop homeassistant >/dev/null 2>&1 || true
fi

tz="$(timedatectl show -p Timezone --value 2>/dev/null || echo Etc/UTC)"
install -d -m 0755 "$HA_DIR" "$CONF" "$HA_DIR/postgres"
install -m 0644 "$ROOT/host/docker-compose.yml" "$HA_DIR/docker-compose.yml"
printf 'HA_IMAGE=%s\nTSDB_IMAGE=%s\nTZ=%s\n' "$HA_IMAGE" "$TSDB_IMAGE" "$tz" >"$HA_DIR/.env"

# The database password is generated once and kept in db.env (read by the timescaledb
# container) and in secrets.yaml (read by Home Assistant).
DB_ENV="$HA_DIR/db.env"
if [ ! -s "$DB_ENV" ]; then
    [ -z "$(ls -A "$HA_DIR/postgres")" ] ||
        die "$HA_DIR/postgres holds a database, but $DB_ENV with its password is missing"
    (
        umask 077
        printf 'POSTGRES_PASSWORD=%s\n' "$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')" >"$DB_ENV"
    )
fi
db_pw="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$DB_ENV")"
[ -n "$db_pw" ] || die "no POSTGRES_PASSWORD in $DB_ENV"

# --- 4. TimescaleDB ---------------------------------------------------------------
log "starting TimescaleDB"
"${COMPOSE[@]}" up -d timescaledb
for _ in $(seq 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' timescaledb 2>/dev/null)" = healthy ] && break
    sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' timescaledb 2>/dev/null)" = healthy ] ||
    die "TimescaleDB did not become healthy (docker logs timescaledb)"
# Tables, retention and aggregates for LTSS; idempotent, so applied on every run.
log "applying database schema (host/db/timescale.sql)"
docker exec -i -e PGPASSWORD="$db_pw" -e PGOPTIONS='-c client_min_messages=warning' timescaledb \
    psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U homeassistant -d homeassistant \
    <"$ROOT/host/db/timescale.sql" >/dev/null

# --- 5. Home Assistant configuration ------------------------------------------------
# A fresh install gets HA's own default configuration, written here because HA only
# writes it when configuration.yaml is missing, and it must use PostgreSQL from the start.
if [ ! -f "$CONF/configuration.yaml" ]; then
    log "writing default configuration.yaml"
    cat >"$CONF/configuration.yaml" <<'EOF'
# Loads default set of integrations. Do not remove.
default_config:

# Load frontend themes from the themes folder
frontend:
  themes: !include_dir_merge_named themes

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml

homeassistant:
  packages: !include_dir_named packages
EOF
    for f in automations.yaml scenes.yaml; do [ -e "$CONF/$f" ] || echo '[]' >"$CONF/$f"; done
    [ -e "$CONF/scripts.yaml" ] || : >"$CONF/scripts.yaml"
fi
# Existing configuration: make sure packages/ is loaded.
if ! grep -qE '^[[:space:]]+packages:' "$CONF/configuration.yaml"; then
    if grep -qE '^homeassistant:[[:space:]]*$' "$CONF/configuration.yaml"; then
        sed -i '/^homeassistant:[[:space:]]*$/a\  packages: !include_dir_named packages' "$CONF/configuration.yaml"
    elif grep -q '^homeassistant:' "$CONF/configuration.yaml"; then
        die "add 'packages: !include_dir_named packages' under homeassistant: in $CONF/configuration.yaml and run again"
    else
        printf '\nhomeassistant:\n  packages: !include_dir_named packages\n' >>"$CONF/configuration.yaml"
    fi
fi
install -d -m 0755 "$CONF/packages"
[ -e "$CONF/packages/martha_storage.yaml" ] ||
    install -m 0644 "$ROOT/host/ha/packages/martha_storage.yaml" "$CONF/packages/martha_storage.yaml"

# secrets.yaml: set martha_db_url, leave everything else as it is.
SECRETS="$CONF/secrets.yaml"
db_url="postgresql://homeassistant:$db_pw@127.0.0.1:5432/homeassistant"
[ -e "$SECRETS" ] || : >"$SECRETS"
chmod 0600 "$SECRETS"
if grep -q '^martha_db_url:' "$SECRETS"; then
    sed -i "s|^martha_db_url:.*|martha_db_url: \"$db_url\"|" "$SECRETS"
else
    printf 'martha_db_url: "%s"\n' "$db_url" >>"$SECRETS"
fi

# LTSS from the bundle (not HACS, which needs internet); always the bundled version.
rm -rf "$CONF/custom_components/ltss"
install -d -m 0755 "$CONF/custom_components"
cp -r "$OFFLINE/custom_components/ltss" "$CONF/custom_components/ltss"

# LTSS's requirements that the HA image lacks, installed from the bundled wheels with the
# image's own uv and Python, without network. Rebuilt on every run, so it always matches
# the Python of the current image. The container gets them via PYTHONPATH.
log "installing LTSS requirements from wheels ($LTSS_WHEELS)"
rm -rf "$HA_DIR/pydeps"
install -d -m 0755 "$HA_DIR/pydeps"
# shellcheck disable=SC2086
docker run --rm --network none --entrypoint uv \
    -v "$OFFLINE/wheels:/wheels:ro" -v "$HA_DIR/pydeps:/pydeps" "$HA_IMAGE" \
    pip install --quiet --no-index --find-links /wheels --no-deps --target /pydeps $LTSS_WHEELS

# PostgreSQL only: retire the SQLite database of the recorder, so it is not used again.
for f in "$CONF"/home-assistant_v2.db "$CONF"/home-assistant_v2.db-wal "$CONF"/home-assistant_v2.db-shm; do
    if [ -e "$f" ]; then
        log "recorder now uses PostgreSQL; moving ${f##*/} to ${f##*/}.retired"
        mv -f "$f" "$f.retired"
    fi
done

# --- 6. Start Home Assistant ----------------------------------------------------------
log "starting Home Assistant"
"${COMPOSE[@]}" up -d --remove-orphans

# The recorder creates its tables at startup; an SQLite file means the package with the
# PostgreSQL db_url was not loaded.
log "waiting for the recorder to use PostgreSQL"
recorder_ok=
for _ in $(seq 90); do
    if [ -e "$CONF/home-assistant_v2.db" ]; then
        die "Home Assistant created an SQLite database; check that packages/martha_storage.yaml is loaded"
    fi
    if [ "$(docker exec -e PGPASSWORD="$db_pw" timescaledb psql -X -tA -h 127.0.0.1 -U homeassistant -d homeassistant \
        -c "SELECT to_regclass('public.recorder_runs') IS NOT NULL" 2>/dev/null)" = t ]; then
        recorder_ok=1
        break
    fi
    sleep 2
done
[ -n "$recorder_ok" ] ||
    log "WARNING: no recorder tables in PostgreSQL yet; check: docker logs homeassistant"

install -d -m 0755 "$STATE_DIR"
cp "$OFFLINE/bundle.env" "$STATE_DIR/installed"

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "done: Home Assistant $HA_VERSION is starting on http://$(hostname).local:8123 (http://${ip:-<ip-of-this-pc>}:8123)"
log "TimescaleDB $TSDB_VERSION listens on 127.0.0.1:5432 (database and user homeassistant, password in $DB_ENV)"
