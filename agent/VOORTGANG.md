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

## Daarna
- **Fase 4:** `agent/install-agent.sh`, met NemoClaw/Hermes (online), de `daemon.json`-fix
  voor cgroupns, de swap tot 8 GB, `agent/policy/martha.yaml` en de skill `agent/skills/martha-ha/`
  (de endpoints van martha-gate, met het agent-token uit `/etc/martha/gate.env`).
  - Eerst beslissen: de attestatieproxy voor Phala (`docs/phala.md`, stap 6) en waar die draait.
- **Fase 5:** proactieve tips (een geplande Hermes-taak).

## Werkwijze bij hervatten
- Testen op martha gaat over SSH (`david@martha.local`; de SSH-key werkt). sudo vraagt een
  wachtwoord: geef het vanuit Git Bash met `printf '%s\n' ... | ssh ... "sudo -S -p '' ..."`.
  PowerShell plakt er een CRLF achter, waardoor sudo faalt.
- `martha-ha` en `martha-gate` op martha zijn bijgewerkt naar de versie in deze repo
  (`install.sh` plus handmatig `install` van de laatste `martha_ha.py` en `martha_gate.py`).
- Staging staat uit; hij start vanzelf bij de test van een voorstel.
- Testen zonder telefoon: roep als root `martha_ha.submit()` aan met een vervangen
  `notify()`, zodat je de goedkeuringsacties zelf naar `/approval` kunt sturen.
