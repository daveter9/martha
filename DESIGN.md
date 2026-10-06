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
| C6 | **Alleen de Home Assistant-container.** | Geen MQTT/Zigbee2MQTT enzovoort. Later toe te voegen in `host/docker-compose.yml` en `download.ps1`. |
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
| C19 | **Vóór elke deploy een volledige back-up**, en niets verdwijnt zonder retentiebeleid. | `martha-ha backup` maakt een consistente kopie van de hele config-map, inclusief database. Te weinig vrije schijf breekt de deploy af. |

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
  - Back-ups zijn `tar.zst` (Python 3.14 `tarfile`), met SQLite-databases via de online
    backup-API. Retentie: de nieuwste 30 en alles van de laatste 7 dagen; onder 5 GB vrij breekt hij af.
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
- **Nieuwe integraties** (config flows met credentials) vallen buiten de agent: die stelt hij
  voor en legt hij uit, de gebruiker voegt ze toe in de UI.

## Open punten
- Offline OS-updates (zie hierboven).
- Back-up buiten martha (nu staan de back-ups op dezelfde schijf).
