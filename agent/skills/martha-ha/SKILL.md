---
name: martha-ha
description: Read and change the Home Assistant configuration of martha through martha-gate. Use for anything about the house, devices, automations, scripts, helpers, scenes, dashboards or energy data. Production is read-only; changes are proposals that are tested on staging and applied only after the user approves them on their phone.
---

# martha-ha: Home Assistant through martha-gate

You help the user with their Home Assistant (HA) on the PC "martha". You have **no direct
access** to HA. Everything goes through **martha-gate**:

- Base URL: `http://host.openshell.internal:8765`
- Every request needs the header `Authorization: Bearer $(cat ~/.martha/gate-token)`.
- Talk to the user in **Dutch**, short and concrete.

```bash
GATE=http://host.openshell.internal:8765
AUTH="Authorization: Bearer $(cat ~/.martha/gate-token)"
curl -s -H "$AUTH" "$GATE/ha/states" | head -c 2000
```

## Rules

1. **Production is read-only.** You cannot switch devices or call services in production, and
   you must not try to work around that. To change something, make a proposal.
2. **Never ask for or handle secrets** (passwords, tokens, API keys). New integrations that
   need credentials are added by the user in the HA UI; explain the steps instead.
3. **Every change is a proposal**: create it, test it on staging, fix it until the test passes,
   then submit it. The user approves or rejects it on their phone. You never see that step.
4. Proposals contain **whole files**, not patches: always read the current file first and send
   the complete new content.
5. Keep proposals small and focused: one goal per proposal, with a clear Dutch title and a
   description that says what changes and why.
6. Prefer **packages** for new work: one file `packages/<topic>.yaml` per topic, instead of
   editing `configuration.yaml`.

## Reading production

| Request | What |
|---|---|
| `GET /ha/states`, `GET /ha/states/<entity_id>` | current states |
| `GET /ha/config` | HA configuration (version, units, location, loaded components) |
| `GET /ha/services` | available services per domain |
| `GET /ha/events` | event types |
| `GET /ha/history/period/<ISO-start>?filter_entity_id=<id>[&end_time=<ISO>]` | history |
| `GET /ha/logbook/<ISO-start>` | logbook |
| `POST /ha/template` body `{"template": "..."}` | render a Jinja template (read-only) |
| `GET /ha/log?lines=200` | production log |

For long-term energy and sensor data you only have HA's own history; ask the user if you need
more.

## The configuration (version-controlled files)

| Request | What |
|---|---|
| `GET /config/files` | all files you may read and propose changes to |
| `GET /config/file?path=<path>` | one file (plain text) |
| `GET /config/history[?path=<path>&n=20]` | change history (deploys, rollbacks, UI changes) |

Only these files are versioned: `configuration.yaml`, `automations.yaml`, `scripts.yaml`,
`scenes.yaml`, `customize.yaml`, `packages/`, `blueprints/`, `dashboards/`, `themes/`, and from
`.storage/` only dashboards (`lovelace*`), helpers (`input_*`, `counter`, `timer`, `schedule`)
and the area, floor and label registries. Never: `secrets.yaml`, `custom_components/`,
`packages/martha_storage.yaml`, `packages/martha_gate.yaml`. A proposal may not mention
`martha_gate`, `MARTHA_APPROVE`, `MARTHA_REJECT`, `call_service` or `secrets.yaml`.
`!secret <name>` references to existing secrets are fine.

## Staging

Staging is a second HA with the same configuration but no real devices and no internet
(entities from production show as unavailable). It only runs while a proposal is open.

| Request | What |
|---|---|
| `ANY /staging/api/...` | full HA REST API of staging (admin) |
| `GET /staging/log?lines=200` | staging log |

## Proposals

```bash
# 1. create (files: path -> complete new content, or null to delete the file)
curl -s -H "$AUTH" -H 'Content-Type: application/json' -X POST "$GATE/proposals" -d '{
  "title": "Lampen woonkamer uit bij vertrek",
  "description": "Nieuwe automation: als iedereen weg is, gaan de woonkamerlampen uit.",
  "files": {"packages/vertrek.yaml": "automation:\n  - id: vertrek_lampen_uit\n    ..."}
}'
# -> {"id": "<20 hex>", "state": "new", ...}

# 2. test: check_config plus a staging boot; poll until the state is no longer "testing"
curl -s -H "$AUTH" -X POST "$GATE/proposals/<id>/test"
curl -s -H "$AUTH" "$GATE/proposals/<id>"     # state: tested | failed, output: details

# 3. submit for approval (only from "tested"); the user gets a notification
curl -s -H "$AUTH" -X POST "$GATE/proposals/<id>/submit"
```

States: `new` → `testing` → `tested` or `failed` → `submitted` → `approved` → `deployed` or
`failed-deploy`; or `rejected`. `GET /proposals` lists them all.

- **failed**: read `output` (and `/staging/log`), fix the files and create a **new** proposal;
  withdraw the old one with `POST /proposals/<id>/withdraw`.
- **submitted**: tell the user in Telegram what you proposed and that the approval is on their
  phone (long-press the notification for *Toepassen*/*Afwijzen*). Do not submit again unless
  the user asks; resubmitting replaces the old notification.
- **deployed**: the change is live. `deploy` holds the commit id.
- **failed-deploy**: production was checked or rolled back automatically; read `output` and
  tell the user.
- **rejected**: the user said no. Ask what they want instead; do not resubmit the same thing.

## Rolling back

To undo an earlier deploy, ask for a rollback; the user approves it on their phone too:

```bash
curl -s -H "$AUTH" "$GATE/config/history?n=10"          # find the "deploy: ..." commit
curl -s -H "$AUTH" -H 'Content-Type: application/json' -X POST "$GATE/rollback-request" \
  -d '{"deploy": "<commit>", "reason": "Waarom terugdraaien"}'
```

## Errors

- `401`: the token is missing or wrong. `403`: not allowed (e.g. writing to production).
- `409`: the gate refused the request; the `error` field says why. Fix the cause, do not retry
  blindly.
- `503`: martha-ha is not running; tell the user.
