#!/usr/bin/env bash
# Deploys a copy of host/ to martha; run by deploy.ps1 (Windows) as root:
#   sudo bash ~/martha-deploy/host/deploy.sh <commit> <db:0|1> [setup ...]
# 1. installs martha-ha from this copy;
# 2. backs up the config (martha-ha backup <commit>), and the database too if <db> is 1;
# 3. runs install.sh --host-only (host files, database schema, services; no apt, no images);
# 4. runs each setup script given (setup_p1, setup_solar, setup_solar --inverter, setup_heat,
#    dashboards);
# 5. on an error in 3 or 4: puts the config of the backup from step 2 back;
# 6. writes <commit> to /var/lib/martha/deployed.
set -euo pipefail

HOST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR=/var/lib/martha

log() { echo "[martha-deploy] $*"; }
die() { echo "[martha-deploy] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ $# -ge 2 ] || die "usage: deploy.sh <commit> <db:0|1> [setup ...]"
commit="$1" db="$2"
shift 2
[[ "$commit" =~ ^[0-9a-f]{7,40}$ ]] || die "invalid commit $commit"

install -d -m 0755 /usr/local/lib/martha
install -m 0755 "$HOST/martha_ha.py" /usr/local/lib/martha/martha_ha.py
ln -sf /usr/local/lib/martha/martha_ha.py /usr/local/sbin/martha-ha

martha-ha backup "$commit"
if [ "$db" = 1 ]; then
    martha-ha backup-db "$commit"
fi

for s in "$@"; do
    case "$s" in
    setup_p1 | setup_solar | setup_solar_inverter | setup_heat | dashboards) ;;
    *) die "unknown setup step $s" ;;
    esac
done

# 'set -e' does not apply inside an if condition, so every step checks its own result.
run_steps() {
    bash "$HOST/install.sh" --host-only || return 1
    local s
    for s in "$@"; do
        if [ "$s" = setup_solar_inverter ]; then
            log "running setup_solar.py --inverter"
            python3 /usr/local/lib/martha/setup_solar.py --inverter || return 1
        else
            log "running $s.py"
            python3 "/usr/local/lib/martha/$s.py" || return 1
        fi
    done
}

if ! run_steps "$@"; then
    log "deploy of $commit FAILED; putting the config of the backup of $commit back"
    martha-ha restore "$commit" || log "WARNING: restore failed too; check Home Assistant by hand"
    exit 1
fi

echo "$commit" >"$STATE_DIR/deployed"
log "deployed $commit"
