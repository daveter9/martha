# Design

## Doel
Home Assistant (Container) in Docker op een pc met Ubuntu Server 26.04 LTS ("resolute"),
amd64. De installatie moet volledig offline kunnen. In gebruik mag Home Assistant wel
internet gebruiken als dat er is, maar hij moet ook zonder internet blijven werken.

## Design constraints

| # | Constraint | Gevolg |
|---|---|---|
| C1 | **Installatie werkt volledig offline** op de doel-pc. | Alles (OS, packages, image) staat in de repo en op de USB-stick. Geen enkele installatie- of updatestap op de doel-pc mag netwerk nodig hebben. |
| C1a | **In gebruik mag HA internet gebruiken, maar heeft het niet nodig.** | Niets blokkeert uitgaand verkeer (geen firewall, `network_mode: host`). HA start en draait lokaal ook zonder internet; integraties die een cloud nodig hebben (weer, updatecheck, HACS, Nabu Casa e.d.) werken dan niet, dat is geaccepteerd. Achteraf een kabel insteken moet werken (zie *Bekabeld netwerk achteraf*). |
| C2 | **Binaries staan in de repo.** | ISO, Rufus, `.deb`'s en image-blobs gaan via Git LFS (`.gitattributes`). |
| C3 | **De voorbereidingsmachine is Windows, zonder Docker.** | `download.ps1` en `make-usb.ps1` zijn PowerShell 5.1. Het image wordt zonder Docker van de registry gehaald (registry-API, dan een OCI layout). apt-dependencies worden in PowerShell opgelost. |
| C4 | **Doel-OS is Ubuntu Server 26.04, amd64.** | De bundle is gekoppeld aan suite `resolute`; `install.sh` weigert een andere release. |
| C5 | **Twee installatieroutes**: (A) autoinstall vanaf USB, (B) `install.sh` op een bestaande, lege server-installatie. | Eén `install.sh` voor beide routes. Route A roept hem aan via een first-boot-service. |
| C6 | **Alleen de containers Home Assistant en TimescaleDB.** | TimescaleDB is sinds ADR-001 de opslag van Recorder en LTSS. Geen MQTT/Zigbee2MQTT enzovoort. Later toe te voegen in `host/docker-compose.yml` en `download.ps1`. |
| C7 | Rufus-stick in **ISO-mode, FAT32**. | Stick blijft beschrijfbaar voor `autoinstall.yaml` en de bundle. Geen bestand mag groter zijn dan 4 GB (grootste is nu ~1,3 GB `casper/*.squashfs`). |
| C8 | **Paden van de bundle zonder spaties** op de doel-pc. | Een apt `file:`-URI met spaties werkt niet. `install.sh` controleert dit. |
| C9 | Git-host is **GitHub** (`daveter9/martha`). **GitHub LFS staat max. 2 GB per bestand toe** (Free/Pro). | De ISO (2,8 GB) staat in delen van 1,5 GB in LFS (`*.iso.001`, `.002`) plus `*.iso.sha256` (officiële Ubuntu-checksum, geverifieerd op 2026-10-05). `0. install os/iso.ps1 -Join` zet hem weer in elkaar en controleert de hash; de `.iso` zelf is gitignored. |
| C10 | De **InRelease-bestanden moeten byte-identiek** blijven (GPG-handtekening). | `offline/apt/dists/** -text` in `.gitattributes`, dus geen regeleinde-conversie. |
| C11 | Scripts voor de doel-pc hebben **LF-regeleinden** nodig. | `eol=lf` voor `*.sh`, `*.service`, `*.yml`, `*.yaml` en `bundle.env`. |
| C12 | De Home Assistant-layers zijn **zstd**-gecomprimeerd. | Docker ≥ 23 nodig; docker.io 29 voldoet. |
| C13 | Op de Windows-machine is **Git for Windows** nodig (met Git LFS). | `make-usb.ps1` gebruikt de `openssl.exe` daaruit voor de wachtwoord-hash. |
| C14 | Werkafspraken en beslissingen staan **in de repo**, niet in een lokaal geheugen. | `CLAUDE.md` (werkafspraken) en dit bestand. |
| C15 | De **agent-laag** (Hermes in NemoClaw) is optioneel en **mag online installeren**. | C1 blijft gelden voor de HA-laag (`host/`, `offline/`), inclusief config-repo, back-up, staging en martha-gate. HA werkt volledig zonder agent. De agent-laag staat apart in `agent/`. |
| C16 | De agent heeft **nooit schrijfrechten op productie-HA**. | Geen productie-token, geen Docker-socket, geen host-shell in de sandbox. Alleen martha-gate (door ons geschreven) wijzigt productie. |
| C17 | Een wijziging gaat pas live na een **geslaagde test op staging en goedkeuring in de HA Companion-app**. | De goedkeuring loopt via productie-HA met een eenmalige nonce die de agent nooit ziet. Ook een rollback-verzoek van de agent vraagt goedkeuring. |
| C18 | Deploy en rollback raken **de database en niet-getrackte bestanden nooit**. | Alleen bestanden op de allowlist van de config-repo worden geschreven. Rollback is een revert-commit; de git-historie wordt nooit herschreven. |
| C19 | **Vóór elke deploy een volledige back-up**, en niets verdwijnt zonder retentiebeleid. | `martha-ha backup` maakt een consistente kopie van de hele config-map plus een `pg_dump` van de database (die staat sinds ADR-001 in PostgreSQL, niet meer in de config-map). Te weinig vrije schijf breekt de deploy af. |
| C20 | Prompts van de agent gaan **alleen naar een attested TEE-endpoint, nooit via een router**. | Phala direct op `inference.phala.com` of `tee.redpill.ai`, nooit `api.redpill.ai` of OpenRouter. Een lokale proxy controleert de attestatie vóór elk verzoek (zie [ADR-002](docs/adr/ADR-002-inference-provider.md)). |
| C21 | **HA installeert in gebruik geen Python-pakketten van internet.** | Requirements van custom integrations die niet in het HA-image zitten, staan als wheels in de bundle (`offline/wheels`). `install.sh` installeert ze met de `uv` van het image zelf, zonder netwerk, in `/opt/homeassistant/pydeps` (via `PYTHONPATH` in de container). Geen HACS. |
| C22 | **Geen SQLite meer voor de Recorder.** | De Recorder gebruikt PostgreSQL. `install.sh` hernoemt een oude `home-assistant_v2.db` naar `*.retired` en breekt af als HA toch een SQLite-database aanmaakt. |

## Architectuurbeslissingen

### Docker: `docker.io` uit Ubuntu (niet Docker CE, niet Podman)
Ubuntu 26.04 levert `docker.io` 29.1.3 en `docker-compose-v2` 2.40.3 (gecontroleerd op
2026-10-05). Dat is actueel genoeg. Podman was het alternatief geweest als docker.io te
oud was. Voordeel: de packages komen uit hetzelfde Ubuntu-archief, met dezelfde signing keys.

### Offline packages: gedeeltelijke, *gesigneerde* Ubuntu-mirror
`download.ps1` haalt de originele `InRelease`- en `Packages.gz`-bestanden op van
`resolute`, `resolute-updates` en `resolute-security` (main en universe). Daarnaast
alleen de `.deb`'s van `docker.io` en `docker-compose-v2` plus hun volledige
dependency-closure (Depends en Pre-Depends, geen Recommends), en wel de hoogste versie
over alle pockets, net als apt.
Op de doel-pc gebruikt `install.sh` die map als apt-bron, met
`Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg`. **apt controleert dus
zelf** de handtekening, de index-hashes en de `.deb`-hashes. De Windows-machine hoeft
niet vertrouwd te worden: GPG-verificatie gebeurt op de doel-pc met diens eigen keyring.
apt draait met tijdelijke sources, lists en cache, zodat de apt-configuratie van het
systeem niet verandert. `Check-Valid-Until=false`, zodat een oudere bundle blijft werken.
De mirror bevat alleen `Packages`-indexen. De index-targets voor command-not-found (`CNF`)
en AppStream (`DEP-11`) staan daarom uit; anders faalt `apt-get update` op Ubuntu Server
(gevonden bij de eerste echte installatie, 2026-10-06).

*Bekend risico:* de closure is berekend zonder te weten wat er al op de doel-pc staat.
apt upgradet een geïnstalleerde dependency alleen als een versie-eis dat afdwingt. Is
die nieuwere versie strikt gekoppeld aan een package dat niet in de bundle zit (bijvoorbeeld
`libsystemd0` en `systemd`), dan faalt `apt-get install` met een duidelijke melding. De
oplossing is dat package toevoegen via `download.ps1 -Packages docker.io,docker-compose-v2,avahi-daemon,<extra>`.
Met de 26.04.1-ISO en de bundle van 2026-10-05 is dat in de praktijk nog niet getest
op echte hardware.

### Container-image: OCI layout, op versie vastgepind
`download.ps1` haalt alleen het `linux/amd64`-manifest van
`ghcr.io/home-assistant/home-assistant` op en controleert alle blobs op digest.
`stable` wordt omgezet naar het echte versienummer (label
`org.opencontainers.image.version`), dat in `offline/bundle.env` komt te staan. Het
geheel wordt opgeslagen als OCI image layout (map, geen tar), zodat blobs die over
versies heen gelijk blijven niet dubbel in LFS komen. `install.sh` laadt het met
`tar | docker load`. Compose heeft `pull_policy: never`.
Het TimescaleDB-image (Docker Hub) gaat op dezelfde manier, naar `offline/images/timescaledb`,
met een vaste tag (`-TimescaleVersion`). Het token wordt per registry opgehaald via de
standaard `WWW-Authenticate`-challenge.

### Home Assistant-container
`network_mode: host` (nodig voor discovery: mDNS, SSDP en DHCP), `privileged: true`
(USB-sticks, Bluetooth), `/run/dbus` read-only. De config staat in
`/opt/homeassistant/config`. `restart: unless-stopped`, dus Docker start HA bij elke boot.

### Vindbaar als `martha.local`: avahi-daemon
`avahi-daemon` staat in de bundle (naast `docker.io` en `docker-compose-v2`) en wordt door
`install.sh` geïnstalleerd en aangezet, in beide routes. Daarmee is de pc via mDNS te vinden
als `<hostnaam>.local`, standaard `martha.local`, ook zonder DNS-server of internet.
`install.sh` zet `deny-interfaces=docker0` in `/etc/avahi/avahi-daemon.conf`, anders publiceert
avahi ook het onbereikbare adres van de Docker-bridge (172.17.0.1). Home Assistant draait met
host-networking een eigen mDNS-stack (zeroconf) naast avahi; beide delen poort 5353, zoals op HA OS.

### Route A: autoinstall
`autoinstall.yaml` staat in de root van de USB-stick (Ubuntu 24.04+ leest die vanzelf).
Omdat `autoinstall` niet op de kernel-commandline staat, **vraagt de installer om
bevestiging** voordat er een schijf gewist wordt: een vangnet tegen een stick die per
ongeluk in de verkeerde pc zit. Verder:
- LVM over de hele grootste schijf (`sizing-policy: all`).
- DHCP op elke `e*`-netwerkkaart, `optional: true`, zodat booten zonder netwerk niet blijft wachten.
  Subiquity schrijft deze `match`-config letterlijk naar de netplan van het doelsysteem, dus ook
  een kabel die pas later wordt ingestoken krijgt via DHCP een adres.
- `apt.fallback: offline-install`, en geen geoip, codecs, drivers of installer-refresh.
- OpenSSH-server (zit in de ISO-pool). Met een SSH-key geldt key-only login, anders een wachtwoord.
- Wachtwoord als SHA-512 crypt-hash (`openssl` uit Git for Windows). Het wachtwoord zelf komt niet op de stick.
- `late-commands` kopiëren de bundle naar `/opt/martha` en zetten `martha-firstboot.service` aan.
- `shutdown: poweroff`, zodat je de stick eruit haalt voordat de pc opnieuw opstart.

Docker kan niet tijdens de installatie (in de chroot) geïnstalleerd en gestart worden.
Daarom gebeurt dat bij de eerste boot. De service draait eenmalig (marker in
`/var/lib/martha/installed`) en toont de voortgang op de console.

### Route B: bestaande lege server
Dezelfde `install.sh`, op twee manieren bij de server gebracht. Vereisten: Ubuntu Server
26.04 amd64 met de standaard `ubuntu-archive-keyring`.
- **Via SSH** (`2. ssh/install-ssh.ps1`): de ingebouwde OpenSSH-client van Windows (geen
  extra tools, alleen LAN, geen internet). Het script controleert eerst release en
  architectuur, kopieert daarna met `scp` naar `~/martha-bundle` (in de home, niet in
  `/tmp`, dat een tmpfs in RAM kan zijn) en draait `install.sh` met `ssh -t`, zodat
  sudo om een wachtwoord kan vragen. Daarna ruimt het de bundle op, want het image zit dan al in Docker.
  Remote commando's bevatten geen dubbele quotes: Windows PowerShell 5.1 verminkt die bij het aanroepen van native programma's.
- **Via USB** (`make-usb.ps1 -BundleOnly`), daarna handmatig mounten en `install.sh` starten.

### Bekabeld netwerk achteraf
Internet of LAN kan later bijkomen. Daarom moet elke bekabelde poort DHCP doen, ook als er
tijdens de installatie geen kabel in zat. Route A regelt dat via de `network`-sectie van de
autoinstall. Voor route B voegt `install.sh` `/etc/netplan/90-martha-lan.yaml` toe (DHCP op
`e*`, `optional: true`), maar **alleen als `netplan get ethernets` leeg is**. Een bestaande
(bijvoorbeeld statische) configuratie blijft dus ongemoeid. Activeren gaat met `netplan generate`
plus `networkctl reload`, niet met `netplan apply`, zodat een SSH-sessie niet wegvalt.

### Updates
Een nieuwe bundle (`download.ps1`) plus `install.sh` op de doel-pc. Dat is idempotent:
`apt-get install` brengt Docker naar de bundleversie, het nieuwe image wordt geladen en
compose maakt de container opnieuw aan. **Niet** inbegrepen: offline security-updates
van het hele OS. Dat zou een volledigere mirror vragen en is een open punt.

### Opslag van meetdata (ADR-001)
Besluit en afwegingen: [docs/adr/ADR-001-opslag-energie-en-sensordata.md](docs/adr/ADR-001-opslag-energie-en-sensordata.md).
HA leest en stuurt alle apparaten via zijn integraties. Recorder en LTSS schrijven naar
één PostgreSQL-server met TimescaleDB.

- **Container `timescaledb`:** image `timescale/timescaledb:<versie>-pg18` (Alpine, met de
  Timescale-licentie voor compressie en continuous aggregates; de `-ha`-variant met PostGIS
  is onnodig groot). Database en gebruiker `homeassistant`. Alleen op `127.0.0.1:5432`;
  analyses vanaf een andere pc gaan via een SSH-tunnel
  (`ssh -L 5432:127.0.0.1:5432 david@martha.local`). Data in `/opt/homeassistant/postgres`
  (de ouder van `PGDATA`, zoals PostgreSQL 18 aanraadt), op de SSD van martha.
  Telemetrie staat uit, `timescaledb-tune` krijgt 1 GB RAM.
- **Wachtwoord:** één keer gegenereerd door `install.sh`, in `/opt/homeassistant/db.env`
  (0600, voor de container) en als `martha_db_url` in `secrets.yaml` (voor HA).
- **HA-config:** `packages/martha_storage.yaml` (Recorder en LTSS). `install.sh` zet hem
  alleen neer als hij ontbreekt, zodat eigen wijzigingen (de include-lijst) blijven staan.
  `configuration.yaml` krijgt `homeassistant: packages: !include_dir_named packages`. Bij een
  verse installatie schrijft `install.sh` HA's standaard-`configuration.yaml` zelf, zodat HA
  vanaf de eerste start PostgreSQL gebruikt.
- **LTSS** (v2.1.1) staat in de bundle, niet via HACS (C1). Zijn requirements `psycopg2-binary`
  en `geoalchemy2` zitten niet in het HA-image (dat heeft wel `psycopg2` en `sqlalchemy`) en
  komen als wheels mee (C21). `download.ps1` haalt van `psycopg2-binary` de musllinux-wheels
  voor alle CPython-versies; `uv` kiest de wheel die past bij de Python van het image. Die map
  wordt bij elke `install.sh` opnieuw opgebouwd, dus een HA-update met een nieuwere Python werkt
  zolang de wheel voor die versie in de bundle zit. *Gevolg:* via `PYTHONPATH` overschaduwt
  `psycopg2-binary` de `psycopg2` uit het image, ook voor de Recorder. Het is dezelfde
  bibliotheek, met een eigen libpq.
- **Schema** (`host/db/timescale.sql`, idempotent, bij elke `install.sh`):
  - `ltss`: hypertable met chunks van een dag, zelf aangemaakt met precies de kolommen en
    indexnamen van LTSS, zodat de aggregates al bestaan voordat LTSS verbindt. Compressie na
    7 dagen, retentie 30 dagen. Een trigger haalt alleen-UI-attributen weg (`icon`,
    `entity_picture`, `options` enz.), omdat LTSS geen `ignore_attributes` kent.
  - `ltss_1m` (2 jaar, compressie na 30 dagen) en `ltss_1h` (onbeperkt, gebouwd op
    `ltss_1m`): alleen numerieke states, met `value_avg`, `value_min`, `value_max`,
    `value_last`, `samples`, `state_class` en `unit`. Tellers gebruiken `value_last`,
    momentane waarden `value_avg`. Het gemiddelde is per sample, niet tijdgewogen.
  - Policies gebruiken `if_not_exists`: een ander interval in het script verandert een
    bestaande policy niet. Een andere aggregate-query vraagt drop en opnieuw aanmaken.
- **Granulariteit:** throttlen gebeurt bij de bron (opties van de integratie, zoals het
  DSMR-update-interval). Filteren gebeurt met de include/exclude-lijst van LTSS. De Recorder
  blijft ongefilterd, want het energiedashboard heeft zijn statistieken nodig.
- **Gaten:** HA schrijft alleen bij een wijziging, dus vul gaten met de vorige waarde:

  ```sql
  SELECT time_bucket_gapfill('1 minute', bucket) AS t,
         locf(last(value_avg, bucket)) AS watt
  FROM ltss_1m
  WHERE entity_id = 'sensor.power_consumption'
    AND bucket > now() - INTERVAL '1 day' AND bucket < now()
  GROUP BY t ORDER BY t;
  ```

### Agent (Hermes): HA configureren zonder schrijfrechten op productie
Doel: een Hermes-agent (Nous Research) die HA echt configureert (automations, scripts,
helpers, dashboards, packages), via Telegram met de gebruiker praat en zelf verbetertips stuurt.

```
Telefoon (Telegram)  <->  Hermes in OpenShell-sandbox (NemoClaw)
                              | alleen HTTP naar martha-gate (L7-policy)
                              v
                         martha-gate (host, systemd, eigen user)
                           |-- read-only proxy naar productie-HA (alleen GET)
                           |-- config-repo (voorstellen = git-patches)
                           |-- staging-HA (internal Docker-netwerk)
                           '-- deploy/rollback na goedkeuring
Telefoon (HA Companion) --[Toepassen/Afwijzen]--> productie-HA --rest_command--> martha-gate
```

- **Isolatie:** NVIDIA NemoClaw met OpenShell. Netwerk deny-by-default met L7-inspectie,
  Landlock en seccomp. De inference-key en het Telegram-token beheert OpenShell; de agent
  praat met `inference.local`. De gebruiker levert de key zelf aan bij `install-agent.sh`.
  Ubuntu 26.04 is bij NVIDIA "tested with limitations"; k3s in Docker vraagt
  `"default-cgroupns-mode": "host"` in `/etc/docker/daemon.json`.
- **Inference:** GLM 5.3 Flash (`z-ai/glm-5.3-flash`) rechtstreeks bij Phala Confidential AI,
  achter een lokale attestatie-verifiërende proxy. Beslissing en onderbouwing in
  [ADR-002](docs/adr/ADR-002-inference-provider.md), opzetten in [docs/phala.md](docs/phala.md).
- **Geheugen:** martha heeft nu 7,1 GB RAM (NemoClaw-minimum 8 GB of 8 GB swap). Tot de
  upgrade naar 32 GB vergroot `install-agent.sh` de swap en draait staging alleen zolang er
  een voorstel openstaat.
- **Config-repo** (`/var/lib/martha/ha-config.git`) met een allowlist: `configuration.yaml`,
  `packages/`, `automations.yaml`, `scripts.yaml`, `scenes.yaml`, `blueprints/`, YAML-dashboards,
  en uit `.storage` alleen dashboards, helpers en de area-, floor- en label-registry.
  Nooit getrackt: `secrets.yaml`, de database, `.storage/auth*`, `http*`,
  `core.config_entries` (credentials), `core.restore_state`, logs.
  Vóór elke deploy commit `martha-ha sync` de productiestaat, zodat wijzigingen via de UI nooit verloren gaan.
  Ook `custom_components/` wordt niet getrackt: de agent kan zo geen code in productie zetten.
- **`martha-ha`** (`host/gate/martha_ha.py`, Python-stdlib, door `install.sh` geïnstalleerd):
  - Een deploy wordt één commit op `main`. git doet de merge met `merge-tree --write-tree`,
    zonder werkkopie; een conflict met de productiestaat breekt af voordat er iets verandert.
  - Bestanden schrijven gaat met `read-tree -u -m`, dus alleen de getrackte bestanden.
    Hangt er `.storage` aan de wijziging, dan stopt HA eerst, omdat HA `.storage` bij het
    afsluiten vanuit het geheugen overschrijft.
  - `check_config` draait in een wegwerpcontainer (`--network none`) op een kopie. Dat
    vangt niet alles: een ongeldige automation-trigger meldt HA pas bij het opstarten
    ("has been disabled"). Daarom leest de health-check na de deploy de log, en volgt bij
    zulke meldingen een automatische rollback (getest op martha, 2026-10-06).
  - Back-ups zijn `tar.zst` (Python 3.14 `tarfile`): de config-map, SQLite-databases via de
    online backup-API, en een `pg_dump` (custom format) van PostgreSQL als
    `database/homeassistant.pgdump`. `restore` zet de oude database opzij onder een andere naam
    (`homeassistant_before_restore_<tijd>`) en laadt de dump in een nieuwe database, met
    `timescaledb_pre_restore()`/`post_restore()` (getest op martha, 2026-10-07: hypertables,
    continuous aggregates en policies komen terug). Retentie: de nieuwste 30 en alles van de laatste 7 dagen; onder 5 GB vrij breekt hij af.
- **Staging-HA:** hetzelfde image, naast productie, op een `internal` Docker-netwerk (geen LAN,
  geen internet), niet privileged, database in het geheugen, dummy-secrets. Op het LAN
  bereikbaar via `http://martha.local:8124` (TCP-forward door martha-gate), met een eigen
  login, zodat de gebruiker in de Companion-app tussen staging en productie kan wisselen.
  Staging bewijst dat de config laadt; echte apparaten testen kan alleen in productie, en
  daarvoor is er de rollback.
  - Vast adres `172.30.53.10` op netwerk `martha-staging` (`172.30.53.0/24`, `internal`).
    Een internal netwerk kan geen poort publiceren, maar de host bereikt de container wel.
    `martha-staging-proxy.service` (DynamicUser, geen Docker-toegang) forwardt daarom LAN-poort
    8124 naar staging. Getest op martha (2026-10-06): de host bereikt staging; staging bereikt
    de router, het internet en productie-HA niet.
  - Staging krijgt geen registries of `config_entries` van productie, alleen de getrackte
    bestanden. Daarmee zijn er ook geen credentials in staging. Entities uit productie bestaan
    in staging dus niet; dashboards tonen ze als "niet beschikbaar".
  - `martha-ha staging test` = `check_config` plus staging opstarten met de logcontrole van de
    health-check. Een ongeldige automation-trigger wordt zo vóór productie afgekeurd.
  - `packages/martha_storage.yaml` (ADR-001) staat **niet** in de config-repo (uitgezonderd in
    de allowlist): hij wijst naar de productiedatabase en laadt LTSS, en dat blijft van
    `install.sh`. Staging en de agent krijgen hem dus nooit; staging houdt zijn eigen
    Recorder-database. `check_config` krijgt `/opt/homeassistant/pydeps` mee, zodat LTSS
    (in de kopie van productie) te importeren is.
- **Nieuwe integraties** (config flows met credentials) vallen buiten de agent: die stelt hij
  voor en legt hij uit, de gebruiker voegt ze toe in de UI.

## Open punten
- Offline OS-updates (zie hierboven).
- Back-up buiten martha (nu staan de back-ups op dezelfde schijf).
- LTSS is een community-integratie zonder onderhoudsgarantie (laatste release 2024-12).
  Terugvaloptie volgens ADR-001: `mqtt_statestream` → Telegraf → Timescale.
- **ADR-001 staat op martha** (verse installatie, 2026-10-07): schema toegepast, de Recorder
  schrijft in PostgreSQL en LTSS in `ltss`; `ltss_1m` en `ltss_1h` en hun policies bestaan.
  Nog niet bekeken: of de aggregates met echte sensordata kloppen.
- Attestatieproxy voor Phala: teep verifieert het ACI/1-formaat van `inference.phala.com`
  nog niet (zie [docs/phala.md](docs/phala.md#stap-6-attestatieproxy-open-punt)). Ook nog
  open: waar de proxy draait ten opzichte van de OpenShell-sandbox.
