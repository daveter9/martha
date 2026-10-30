# Plan: agent en staging eruit, alleen prod, met back-ups per commit

Status: **gevalideerd met de gebruiker op 2026-10-30, nog niet gebouwd.** Bewust uitgesteld,
omdat de gebruiker op dat moment HA aan het uitbreiden was. Pak dit op in een schone sessie.
Na het bouwen gaat de inhoud naar DESIGN.md en de readme, en verdwijnt dit bestand.

## Aanleiding
De Hermes-agent (NemoClaw, Telegram, Phala), martha-gate, staging en de goedkeuring via de
Companion-app zijn niet meer nodig. Alleen productie blijft. Wel moet de config altijd
gebackupt zijn, zodat de gebruiker naar de staat van elke commit kan terugzetten.

## Beslissingen van de gebruiker (één voor één gevraagd)
| # | Vraag | Keuze |
|---|---|---|
| 1 | Wat zit er in de back-up? | Config vaak, database minder vaak, los van elkaar terug te zetten. |
| 2 | Waar staan de back-ups? | **Alleen lokaal** op martha's eigen schijf. In DESIGN.md vastleggen dat dit niet beschermt tegen schijfuitval. |
| 3 | Wanneer een config-back-up? | **Vóór elke edit/deploy**, gekoppeld aan de repo-commit, zodat altijd terug te zetten is. |
| 4 | Ook wijzigingen in de HA-interface? | Nee, de gebruiker past nooit iets met de hand aan. Er is dus geen timer voor de config. |
| 5 | Wanneer een database-back-up? | Elke nacht (systemd-timer), plus extra vóór een deploy die de database raakt (`host/db/*.sql` of een setup-script). |
| 6 | Retentie | Config-back-ups **altijd** bewaren; database-dumps **30 dagen**. Onder 5 GB vrij breekt een back-up af (zoals nu). |
| 7 | Mechanisme | **Snapshots van de hele config-map** (`tar.zst`). De interne config-repo `/var/lib/martha/ha-config.git` verdwijnt. |
| 8 | Hoe komt een aanpassing op martha? | **Nieuw `deploy.ps1` op Windows** (zie hieronder). martha doet geen `git pull` (C1). |
| 9 | Documentatie van de agent | **Alles verwijderen**, de git-historie bewaart het. In DESIGN.md één regel dat de agent-laag op 2026-10-30 geschrapt is. |
| 10 | Wat er al op martha staat | **Alles weg, back-ups houden** (zie *Opruimen op martha*). |

## Ontwerp

### Weg uit de repo
- `agent/` (README, VOORTGANG, install-agent.sh, policy, skills).
- `docs/phala.md`, `docs/adr/ADR-002-inference-provider.md`.
- `host/gate/martha_gate.py`, `host/gate/staging_proxy.py`.
- `host/martha-gate.service`, `host/martha-ha.service` (de daemon), `host/martha-staging-proxy.service`.
- `host/ha/packages/martha_gate.yaml`.
- Uit `host/gate/martha_ha.py`: staging, voorstellen, goedkeuring/nonce, notify, `serve`/dispatch,
  `setup-gate`, de config-git (`init`, `sync`, `deploy` via merge-tree, `rollback`), `files`/`history`.
- DESIGN.md: C15, C16, C17 en C20, het hoofdstuk *Agent (Hermes)*, en de opmerkingen over staging
  (o.a. bij de P1-meter). C18 en C19 herschrijven naar het nieuwe back-upmodel. Het open punt over
  de attestatieproxy vervalt.
- readme.md: de secties *Staging* en *Goedkeuren op je telefoon*, de agent-rij in *Inhoud*, en de
  paden van proposals, staging, `gate.env` en `ha-config.git`. CLAUDE.md: de agent-fase-afspraak,
  C15 bij offline installeren, en "niet via Telegram".

### Back-ups (`martha-ha`, alleen lokaal in `/var/lib/martha/backups`)
- **Config:** `config-<YYYY-MM-DD_HHMM>-<commit>.tar.zst`, de hele config-map (dus ook `secrets.yaml`,
  `.storage`). Gemaakt vóór elke deploy, en nooit weggegooid.
- **Database:** `db-<YYYY-MM-DD_HHMM>[-<commit>].pgdump` (custom format), elke nacht via
  `martha-backup-db.timer`, plus vóór een deploy die de database raakt. Gaan na 30 dagen weg.
- Te weinig vrije schijf (< 5 GB) breekt de back-up af, en daarmee de deploy.
- `restore` kan het oude formaat (config + `database/homeassistant.pgdump` in één tar) nog lezen.

### Commando's
- `martha-ha backup <label>`: alleen de config (het label is de commit).
- `martha-ha backup-db [label]`: een pg_dump.
- `martha-ha restore <commit|back-up>`: zet alleen de config-map terug. De huidige map gaat opzij
  als `config.before-restore-<tijd>`. HA wordt gestopt en weer gestart.
- `martha-ha restore-db <dump>`: zet de database terug (het bestaande mechanisme: oude database
  hernoemen naar `homeassistant_before_restore_<tijd>`, `timescaledb_pre_restore()`/`post_restore()`).
- `martha-ha status`: laatste back-ups en vrije ruimte.
- `martha-ha token` (vervangt `setup-gate`): logt interactief in en maakt het root-token
  `martha-root` in `/etc/martha/ha-root.env`, dat de setup-scripts gebruiken. Zonder gate-gebruiker,
  staging-token of notify-service. Dit is een eigen keuze van Claude: het is `setup-gate` zonder
  de gate-onderdelen.
- De bewaking blijft: `check_config` in een wegwerpcontainer, een health-check op de log na de
  herstart, en bij fouten automatisch de config van de back-up van die deploy terugzetten.

### `deploy.ps1` (Windows, PowerShell 5.1)
1. Weigert bij niet-gecommitte of niet-gepushte wijzigingen, en neemt de korte hash van `HEAD`.
2. Kopieert alleen `host/` met scp naar martha (geen `offline/`).
3. Op martha: `martha-ha backup <commit>`, en `backup-db <commit>` als de diff sinds de vorige
   deploy `host/db/` of een setup-script raakt. De vorige deploy-commit staat op martha, bv. in
   `/var/lib/martha/deployed`.
4. Draait `install.sh` in een modus zonder apt en zonder images te laden (alleen host-bestanden,
   schema, services), of het setup-script dat nodig is.
5. Gaat het mis, dan zet hij de config van die back-up terug.
Remote commando's zonder dubbele quotes (PS 5.1, zie DESIGN.md *Route B*).

De setup-scripts (`setup_p1/solar/heat.py`, `dashboards.py`) gebruiken nu `m.backup`, `m.deploy`
en `m.ROOT_ENV`. Ze schrijven hun packages voortaan direct, met de bewaking hierboven in plaats van
een commit in de config-git, en verwijzen naar `martha-ha token` in plaats van `setup-gate`.

### Opruimen op martha (door `install.sh`, idempotent)
- De services `martha-gate`, `martha-ha` en `martha-staging-proxy` stoppen, uitzetten en verwijderen.
  De staging-container en het netwerk `martha-staging` weghalen.
- De user en groep `martha-gate`, `/etc/martha/gate.env`, `/var/lib/martha/staging` en
  `/var/lib/martha/proposals` verwijderen.
- `packages/martha_gate.yaml` verwijderen (gevolgd door een herstart van HA), en de HA-gebruiker
  "Martha gate" verwijderen (via de websocket met het root-token).
- `ha-config.git` in een laatste back-up zetten (`ha-config-git-<tijd>.tar.zst`) en dan verwijderen.
- Bestaande back-ups blijven staan. Het oude formaat valt onder de config-retentie (altijd).
- `ha-root.env` blijft staan; alleen `NOTIFY_SERVICE` mag eruit.

## Werkwijze bij het bouwen
- Conventional commits, logisch opgesplitst, en na elke commit pushen (CLAUDE.md). Bijvoorbeeld:
  1. `feat(host): config backups per commit and nightly db dump`
  2. `feat: deploy.ps1`
  3. `refactor(ha): setup scripts without config repo`
  4. `chore: remove agent, gate and staging` (code + cleanup in install.sh)
  5. `docs: design and readme for prod only`
- Zet daarna alles op martha (CLAUDE.md: meteen naar productie) en test daar: een deploy maakt een
  back-up met de commit, `restore <commit>` werkt, de timer draait, en het opruimen is gelukt.
- Verifieer lokaal alleen met wat native op Windows draait (`bash -n` in Git Bash, PowerShell),
  niet in WSL.
- Haal dit bestand weg zodra alles in DESIGN.md en de readme staat.
