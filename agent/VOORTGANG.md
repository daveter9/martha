# Voortgang Hermes-agent

Stand van zaken, zodat het werk later kan worden hervat. Het ontwerp staat in
[DESIGN.md](../DESIGN.md) (C15–C19 en *Agent (Hermes)*).

## Klaar (getest op martha)

| Fase | Wat | Waar |
|---|---|---|
| 0 | Constraints en architectuur vastgelegd | `DESIGN.md`, `CLAUDE.md` |
| 1 | `martha-ha`: config in git, back-up, deploy, automatische rollback, `rollback`, `restore` | `host/gate/martha_ha.py` |
| 2 | Staging-HA op een internal netwerk, poort 8124 via `martha-staging-proxy` | `host/docker-compose.yml`, `host/gate/staging_proxy.py` |
| – | Back-up met `pg_dump` en restore van de database (C19), 2026-10-07 | `host/gate/martha_ha.py` |
| 3 | `martha-gate` en goedkeuring in de Companion-app, 2026-10-07 | `host/gate/martha_gate.py`, `martha_ha.py` (`serve`, `setup-gate`), `host/ha/packages/martha_gate.yaml` |
| – | Voorbereidingsgids (Telegram, API-key) | `agent/README.md` |

Het ontwerp van fase 3, zoals gebouwd, staat in DESIGN.md onder *martha-gate*.
Getest op martha (2026-10-07):
- **Toegang:** zonder token of met een fout token geeft de gate 401. Elke schrijfactie op
  `/ha/` geeft 403, `..`-paden (ook gecodeerd) geven 400, en `/approval` zonder secret geeft 403.
- **Lezen:** states, config, services, events, logbook, history, template, logs, staging-API en
  de config-endpoints werken.
- **Voorstellen die geweigerd worden:** `secrets.yaml`, `packages/martha_gate.yaml`,
  `martha_storage.yaml`, `.storage/auth`, `custom_components/`, `..`-paden, ongeldige
  `.storage`-JSON, verboden markers, en een voorstel zonder wijziging.
- **Telefoon:**
  - de melding komt binnen, en een tik opent de diff-pagina;
  - "Stuur de melding opnieuw" werkt;
  - **Toepassen** deployt (input_boolean `fase3_test`);
  - **Afwijzen** van een rollback-verzoek verandert niets;
  - **Toepassen** van een rollback-verzoek draait de deploy terug.
- **Nonce:** een tweede gebruik, een fout nonce, een onbekend id en Toepassen na Afwijzen worden
  geweigerd. De agent ziet de nonce-hash niet.

Stand bij de gebruiker:
- Productie en staging zijn geonboard (opnieuw, na de herinstallatie van 2026-10-07). De
  Companion-app is gekoppeld en `setup-gate` is gedraaid.
- De Telegram-bot is aangemaakt en gestart. Het token staat in de wachtwoordmanager van de
  gebruiker; het user-ID kent de gebruiker en komt niet in de repo.
- **Provider:** Phala (ADR-002). De API-key is aangemaakt en werkt (getest op 2026-10-07, zie
  `docs/phala.md`). De key staat niet in de repo; de gebruiker levert hem aan bij `install-agent.sh`.

## Fase 4: bezig (onderbroken op 2026-10-07)

**Al gedaan op martha** (met de hand, als `david`):
- `david` zit in de groep `docker` en heeft lingering aan; `binutils` was er al.
- NemoClaw **v0.0.124** (de `lkg`-tag) is geïnstalleerd met Hermes, zonder vragen: zie de
  `export`-regel in `agent/install-agent.sh`. Het resultaat:
  - Node 22 via nvm, `nemohermes` en `openshell` (0.0.116) in `~/.local/bin`;
  - de gateway als user-service `nemoclaw-openshell-gateway.service` (poort 8080);
  - de log staat in `~/nemoclaw-install.log`.
- **Sandbox `hermes`** draait:
  - model `z-ai/glm-5.3-flash` via de provider `custom` op `https://inference.phala.com/v1`;
  - de Phala-key staat in de credentials van OpenShell, niet in de sandbox;
  - policy-tier `restricted`, met alleen het preset `pypi`;
  - de onboarding meldt "gateway, dashboard, and inference route are healthy";
  - dashboard op `127.0.0.1:18789` en de OpenAI-API op `127.0.0.1:8642`, alleen lokaal
    (bereikbaar via een SSH-tunnel).
- Telegram is nog **niet** toegevoegd.

**Gevonden:**
- martha heeft 30 GB RAM en 8 GB swap; extra swap is niet nodig.
- De cgroupns-fix in `daemon.json` hoeft niet meer (zie DESIGN.md).
- `nemoclaw.sh` via `curl | bash … </dev/null` doet niets, omdat bash het script dan van
  `/dev/null` leest. Download het eerst naar een bestand.

**Concepten in de repo, nog niet getest:**
- `agent/install-agent.sh`: legt de stappen vast. Het geheel is nog niet gedraaid; vooral
  `sg docker`, `skill install`, `policy add --from-file` en `exec` moeten nog worden nagelopen.
- `agent/policy/martha-gate.yaml`: de sandbox mag alleen naar `host.openshell.internal:8765`.
  Na te gaan:
  - werkt `host.openshell.internal` voor een dienst op de host met gewoon HTTP?
  - kloppen de binary-paden van curl en python in de sandbox? Zie `openshell term` bij een
    geblokkeerd verzoek.
- `agent/skills/martha-ha/SKILL.md`: hoe Hermes de gate gebruikt. Het token verwacht hij in
  `~/.martha/gate-token` in de sandbox.

**Volgende stappen** (op martha, als `david`; eerst `export PATH="$HOME/.local/bin:$PATH"`):
1. `nemohermes hermes policy add --from-file agent/policy/martha-gate.yaml --dry-run`, en
   daarna met `--yes`. Kopieer `agent/` eerst naar martha.
2. `nemohermes hermes skill install agent/skills/martha-ha`.
3. Zet het gate-token in de sandbox. Zoek uit hoe (`nemohermes help`; `exec` of upload).
   Het token is `AGENT_TOKEN` uit `/etc/martha/gate.env`.
4. Test vanuit de sandbox (`nemohermes hermes connect`):
   `curl -H "Authorization: Bearer …" http://host.openshell.internal:8765/ha/states`.
   Controleer ook dat `martha.local:8123` vanuit de sandbox **niet** bereikbaar is.
5. Telegram toevoegen met `TELEGRAM_BOT_TOKEN` en `TELEGRAM_ALLOWED_IDS` in de omgeving, dan
   `nemohermes hermes channels add telegram`.
6. End-to-end: in Telegram een kleine wijziging vragen, bijvoorbeeld een helper. Daarna volgt
   een voorstel, een staging-test, de melding op de telefoon, Toepassen, en het resultaat.
7. `install-agent.sh` bijwerken naar wat echt werkte, de gidsen aanvullen (`agent/README.md`,
   `docs/phala.md` stap 7), en committen.
8. Uitzoeken of `"provider": {"aci_verified": true, "zdr": true}` mee kan (`docs/phala.md`,
   stap 7). Niet blokkerend: Phala dwingt de verificatie al af.

## Daarna
- **Fase 5:** proactieve tips (een geplande Hermes-taak).

## Werkwijze bij hervatten
- **Geheimen** (sudo-wachtwoord, Phala-key, Telegram-token en user-ID) staan lokaal op de
  Windows-pc in `.secrets/martha.env`. Die map is gitignored en gaat nooit naar GitHub. Lees
  het bestand en vraag de gebruiker er niet opnieuw om.
- Testen op martha gaat over SSH (`david@martha.local`; de SSH-key werkt). sudo vraagt een
  wachtwoord: geef het vanuit Git Bash met `printf '%s\n' ... | ssh ... "sudo -S -p '' ..."`.
  PowerShell plakt er een CRLF achter, waardoor sudo faalt.
- `martha-ha` en `martha-gate` op martha zijn bijgewerkt naar de versie in deze repo
  (`install.sh` plus handmatig `install` van de laatste `martha_ha.py` en `martha_gate.py`).
- Staging staat uit; hij start vanzelf bij de test van een voorstel.
- Testen zonder telefoon: roep als root `martha_ha.submit()` aan met een vervangen
  `notify()`, zodat je de goedkeuringsacties zelf naar `/approval` kunt sturen.
