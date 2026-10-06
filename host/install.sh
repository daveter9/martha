#!/usr/bin/env bash
# Installs Docker (docker.io) and Home Assistant on Ubuntu 26.04 from the offline bundle.
#
# Runs automatically on first boot after the autoinstall (martha-firstboot.service),
# but can also be run by hand on an existing Ubuntu 26.04 install, e.g. from a USB stick:
#   sudo bash /media/<user>/<usb>/martha/host/install.sh
# Idempotent: re-running with a newer bundle upgrades Docker and Home Assistant.
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

log "bundle created $BUNDLE_CREATED, Home Assistant $HA_VERSION"

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

# --- 2. Home Assistant image ---------------------------------------------------
if docker image inspect "$HA_IMAGE" >/dev/null 2>&1; then
    log "image $HA_IMAGE already present"
else
    log "loading $HA_IMAGE (this takes a few minutes)"
    out="$(tar -C "$OFFLINE/images/homeassistant" -cf - . | docker load)"
    echo "$out"
    if ! docker image inspect "$HA_IMAGE" >/dev/null 2>&1; then
        # Older loaders ignore the OCI name annotation; tag whatever was loaded.
        loaded="$(sed -n -e 's/^Loaded image ID: //p' -e 's/^Loaded image: //p' <<<"$out" | head -n1)"
        [ -n "$loaded" ] || die "docker load did not report an image"
        docker tag "$loaded" "$HA_IMAGE"
    fi
fi

# --- 3. Compose project ---------------------------------------------------------
tz="$(timedatectl show -p Timezone --value 2>/dev/null || echo Etc/UTC)"
install -d -m 0755 "$HA_DIR" "$HA_DIR/config"
install -m 0644 "$ROOT/host/docker-compose.yml" "$HA_DIR/docker-compose.yml"
printf 'HA_IMAGE=%s\nTZ=%s\n' "$HA_IMAGE" "$tz" >"$HA_DIR/.env"
log "starting Home Assistant"
docker compose -f "$HA_DIR/docker-compose.yml" up -d --remove-orphans

install -d -m 0755 "$STATE_DIR"
cp "$OFFLINE/bundle.env" "$STATE_DIR/installed"

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "done: Home Assistant $HA_VERSION is starting on http://$(hostname).local:8123 (http://${ip:-<ip-of-this-pc>}:8123)"
