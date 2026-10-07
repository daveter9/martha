#!/usr/bin/env bash
# Installs the Hermes agent on martha: NVIDIA NemoClaw (NemoHermes CLI, OpenShell gateway and
# sandbox) with Hermes, inference at Phala (ADR-002), Telegram, the martha-gate network policy
# and the martha-ha skill. Needs internet (C15); Home Assistant keeps working without the agent.
#
# Run as the admin user (not root), on martha, after 'sudo martha-ha setup-gate':
#   bash agent/install-agent.sh
# It asks for the Phala API key, the Telegram bot token and your Telegram user ID; they are
# handed to NemoClaw/OpenShell, which keeps them outside the sandbox. Nothing is written to
# this repository. Re-running updates the policy, the skill and the gate token.
set -euo pipefail

NEMOCLAW_VERSION="${NEMOCLAW_VERSION:-v0.0.124}"   # tested on martha, 2026-10-07
SANDBOX="${SANDBOX:-hermes}"
PHALA_URL="https://inference.phala.com/v1"
MODEL="z-ai/glm-5.3-flash"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/nemoclaw-install.log"

log() { echo "[martha-agent] $*"; }
die() { echo "[martha-agent] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as your own user, not as root (NemoClaw installs per user)"
command -v systemctl >/dev/null || die "systemd is required"
systemctl is-active --quiet martha-gate.service ||
    die "martha-gate is not running; run 'sudo martha-ha setup-gate' first"

# --- 1. Host preparation (sudo) ------------------------------------------------------------
# NemoClaw drives Docker as this user and runs the OpenShell gateway as a user service, which
# must keep running after logout and start at boot (lingering).
if ! id -nG | tr ' ' '\n' | grep -qx docker; then
    log "adding $USER to the docker group"
    sudo usermod -aG docker "$USER"
fi
sudo loginctl enable-linger "$USER"
command -v strings >/dev/null || sudo apt-get install -y binutils

# --- 2. Secrets (never stored here) ----------------------------------------------------------
if [ -z "${COMPATIBLE_API_KEY:-}" ]; then
    read -rsp "Phala API key (docs/phala.md, step 2): " COMPATIBLE_API_KEY; echo
fi
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    read -rsp "Telegram bot token (agent/README.md, step 2): " TELEGRAM_BOT_TOKEN; echo
fi
if [ -z "${TELEGRAM_ALLOWED_IDS:-}" ]; then
    read -rp "Your Telegram user ID (a number): " TELEGRAM_ALLOWED_IDS
fi
[[ "$TELEGRAM_ALLOWED_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "Telegram user ID must be a number"
export COMPATIBLE_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_IDS

# --- 3. NemoClaw with Hermes -------------------------------------------------------------------
# Inference goes straight to Phala's attested endpoint (C20, ADR-002); OpenShell holds the key
# and the sandbox only talks to inference.local. Strictest policy tier, no web search.
export NEMOCLAW_INSTALL_TAG="$NEMOCLAW_VERSION" NEMOCLAW_AGENT=hermes \
    NEMOCLAW_NON_INTERACTIVE=1 NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 \
    NEMOCLAW_SANDBOX_NAME="$SANDBOX" NEMOCLAW_PROVIDER=custom \
    NEMOCLAW_ENDPOINT_URL="$PHALA_URL" NEMOCLAW_MODEL="$MODEL" \
    NEMOCLAW_POLICY_TIER=restricted NEMOCLAW_WEB_SEARCH_PROVIDER=none
log "installing NemoClaw $NEMOCLAW_VERSION with Hermes (log: $LOG)"
installer="$(mktemp)"
trap 'rm -f "$installer"' EXIT
curl -fsSL -o "$installer" https://www.nvidia.com/nemoclaw.sh
# 'sg docker' gives this run the docker group without logging in again.
sg docker -c "bash '$installer'" </dev/null >"$LOG" 2>&1 ||
    die "NemoClaw installer failed; see $LOG"
unset COMPATIBLE_API_KEY TELEGRAM_BOT_TOKEN

export PATH="$HOME/.local/bin:$PATH"
nh() { sg docker -c "nemohermes $SANDBOX $*"; }

# --- 4. martha-gate: policy, token and skill --------------------------------------------------
log "allowing the sandbox to reach martha-gate"
nh policy add --from-file "$HERE/policy/martha-gate.yaml" --yes
log "installing the martha-ha skill"
nh skill install "$HERE/skills/martha-ha"
log "handing the agent its martha-gate token"
sudo sed -n 's/^AGENT_TOKEN=//p' /etc/martha/gate.env |
    sg docker -c "nemohermes $SANDBOX exec -- sh -c 'umask 077; mkdir -p ~/.martha && cat > ~/.martha/gate-token'"

nh status
log "done: talk to your bot in Telegram"
