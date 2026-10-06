# Voortgang Hermes-agent

Stand van zaken, zodat het werk later kan worden hervat. Het ontwerp staat in
[DESIGN.md](../DESIGN.md) (C15–C19 en *Agent (Hermes)*).

## Klaar (getest op martha, 2026-10-06)

| Fase | Wat | Waar |
|---|---|---|
| 0 | Constraints en architectuur vastgelegd | `DESIGN.md`, `CLAUDE.md` |
| 1 | `martha-ha`: config in git, back-up, deploy, automatische rollback, `rollback`, `restore` | `host/gate/martha_ha.py` |
| 2 | Staging-HA op een internal netwerk, poort 8124 via `martha-staging-proxy` | `host/docker-compose.yml`, `host/gate/staging_proxy.py` |
| – | Voorbereidingsgids (Telegram, API-key) | `agent/README.md` |

Wat de gebruiker al gedaan heeft:
- Staging is geonboard en staat in de Companion-app.
- De Telegram-bot is aangemaakt en gestart. Het token staat in de wachtwoordmanager van de
  gebruiker; het user-ID kent de gebruiker en komt niet in de repo.
- Nog open: de keuze van een inference-provider en een API-key.

## Volgende stap: fase 3 (martha-gate en goedkeuring), nog niet begonnen in code

Uitgewerkt ontwerp:

**Twee processen, zodat de gate die met de agent praat geen root heeft:**
- `martha-ha serve`, als root (`martha-ha.service`):
  - Unix-socket `/run/martha/ha.sock`, `root:martha-gate 0660`.
  - JSON per regel. Alleen deze commando's:
    - `create`, `list`, `show`, `test` en `submit`;
    - `approve` en `reject`, die een nonce vereisen;
    - `rollback_request`;
    - `logs`.
  - Lange taken (`test`, `approve`) draaien in een achtergrondthread op de bestaande
    flock. `locked()` moet daarvoor kunnen wachten.
- `martha_gate.py`, als systeemgebruiker `martha-gate`:
  - HTTP op `:8765`, met een stevig gesandboxte unit: `NoNewPrivileges`,
    `ProtectSystem=strict` en `ReadWritePaths=/run/martha`.
  - Voor de agent (Bearer `AGENT_TOKEN`):
    - `GET /ha/{states,config,services,history,logbook,events}` en `POST /ha/template`.
      Deze lopen naar productie met het token van een **niet-admin** HA-gebruiker "Martha gate".
    - `/ha/log` en `/staging/log` lopen via de daemon.
    - `/staging/api/*` is een volledige proxy met een staging-token.
    - `GET /config/files|file|history`. De gate leest de repo zelf (groep `martha-gate`,
      `core.sharedRepository=group`, `-c safe.directory=*`).
    - `POST /proposals`, `GET /proposals[/<id>]`, `POST /proposals/<id>/test|submit` en
      `POST /rollback-request`.
  - Zonder agent-token:
    - `GET /p/<id>`: een HTML-diffpagina. Het id is 20 hex-tekens, dus niet te raden.
    - `POST /approval`: alleen met de header `X-Martha-Secret`.

**Voorstellen:**
- Een voorstel is JSON `{title, description, files: {pad: inhoud|null}}`. Dat is
  hele bestanden, geen patches.
- Elk pad moet buiten `info/exclude` vallen (`git check-ignore --no-index`). Niet
  toegestaan: `packages/martha_gate.yaml`, meer dan 50 bestanden, of bestanden groter dan
  512 KB. `.storage/*` moet geldige JSON zijn.
- De daemon maakt de commit met een tijdelijke `GIT_INDEX_FILE` en zet hem op `refs/proposals/<id>`.
- De metadata staat in `/var/lib/martha/proposals/<id>.json`, mode 0600.
- De states zijn `new`, `testing`, `tested`, `failed`, `submitted`, `approved`,
  `deployed`, `failed-deploy` en `rejected`.

**Goedkeuring:**
- `submit` maakt een nonce aan (`token_urlsafe`; alleen de sha256 wordt bewaard). Daarna
  volgt een melding via `notify.<NOTIFY_SERVICE>` met deze acties:
  - `MARTHA_APPROVE_<id>_<nonce>` (Toepassen);
  - `MARTHA_REJECT_<id>_<nonce>` (Afwijzen);
  - een URI naar `http://<host>.local:8765/p/<id>`.
- Het beschermde package `packages/martha_gate.yaml` (template in `host/ha/`) bevat een
  automation op `mobile_app_notification_action`. Die gaat via
  `rest_command.martha_gate_approval` naar `http://127.0.0.1:8765/approval`, met de header
  `X-Martha-Secret: !secret martha_gate_secret`.
- De gate splitst de actie met `split("_", 3)` en geeft hem aan de daemon. De daemon
  controleert de nonce, die maar één keer bruikbaar is, en deployt (bij een rollback-verzoek:
  `rollback(<deploy-sha>)`). Daarna volgt een resultaatmelding.

**`martha-ha setup-gate`** (interactief, de gebruiker draait het zelf):
1. Het vraagt de HA-login van productie en logt in via de login-flow
   (`/auth/login_flow` en `/auth/token`). Via een minimale stdlib-websocketclient maakt het
   daarna:
   - een long-lived token "martha-root", in `/etc/martha/ha-root.env` (0600);
   - de niet-admin gebruiker "Martha gate" (`config/auth/create` en
     `config/auth_provider/homeassistant/create`, `local_only`) met een eigen token, in
     `/etc/martha/gate.env` (`0640 root:martha-gate`).
2. Hetzelfde voor staging: een staging-token in `gate.env`.
3. Het kiest de `notify.mobile_app_*`-service uit `/api/services`.
4. Het genereert `AGENT_TOKEN` en `APPROVAL_SECRET`. Bij een nieuwe run blijven die
   behouden, tenzij je `--rotate` meegeeft.
5. Het zet `martha_gate_secret` in de productie-`secrets.yaml`.
6. Het deployt `packages/martha_gate.yaml` plus, indien nodig,
   `homeassistant: packages: !include_dir_named packages` in `configuration.yaml`. Staat er
   al een `homeassistant:`-blok, dan breekt het af met instructies.
7. Het zet `martha-gate.service` aan en stuurt een testmelding.

`activate()` leest `HA_TOKEN` voortaan uit `/etc/martha/ha-root.env`, niet meer uit `gate.env`.

**Test fase 3:**
- Met `curl` en het agent-token:
  - een voorstel maken, testen en indienen;
  - een voorstel op `secrets.yaml` of `packages/martha_gate.yaml` moet geweigerd worden.
- Op de telefoon:
  - de melding komt binnen;
  - Afwijzen verandert niets;
  - Toepassen deployt.
- Een nonce een tweede keer gebruiken moet falen.
- Een rollback-verzoek werkt.

## Let op bij hervatten: parallel werk op master
Op master staat ook werk uit een andere sessie: TimescaleDB (ADR-001) en een ADR over de
inference-provider (Phala, `docs/phala.md`). Controleer daarom eerst:
- **Back-ups:** de recorder staat nu in PostgreSQL (`/opt/homeassistant/postgres`). De
  back-up van `martha-ha` dekt alleen SQLite en de config-map. Om C19 (geen dataverlies)
  waar te maken, moet `martha-ha backup` ook een `pg_dump` maken, of een consistente
  kopie van de database.
- **Provider:** of de provider-keuze uit ADR-002 de open vraag hierboven al beantwoordt.

## Daarna
- **Fase 4:** `agent/install-agent.sh`, met NemoClaw/Hermes (online), de `daemon.json`-fix
  voor cgroupns, de swap tot 8 GB, `agent/policy/martha.yaml` en de skill `agent/skills/martha-ha/`.
- **Fase 5:** proactieve tips (een geplande Hermes-taak).

## Werkwijze bij hervatten
- Testen op martha gaat over SSH (`david@martha.local`; de SSH-key werkt). sudo vraagt een
  wachtwoord: geef het vanuit Git Bash met `printf '%s\n' ... | ssh ... "sudo -S -p '' ..."`.
  PowerShell plakt er een CRLF achter, waardoor sudo faalt.
- `martha-ha` op martha is met de hand bijgewerkt naar de versie in deze repo.
- Staging draait op dit moment met de productieconfig.
