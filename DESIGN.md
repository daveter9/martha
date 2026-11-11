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
| C18 | Een deploy **raakt de database nooit, behalve via het idempotente schema**. | `deploy.ps1` schrijft host-bestanden en de setup-scripts schrijven hun packages, helpers en dashboards; de data blijft staan. Raakt een deploy `host/db/` of een setup-script, dan komt er eerst ook een `pg_dump`. Config en database worden los van elkaar teruggezet (`martha-ha restore`, `restore-db`). |
| C19 | **Vóór elke deploy een back-up van de config, gekoppeld aan de commit**, en niets verdwijnt zonder retentiebeleid (keuze van de gebruiker, 2026-10-30). | `martha-ha backup <commit>` maakt een snapshot van de hele config-map; `martha-ha restore <commit>` zet die terug. Config-back-ups blijven **altijd**; database-dumps (elke nacht, plus vóór een deploy die de database raakt) **30 dagen**. Onder 5 GB vrij breekt een back-up af, en daarmee de deploy. De back-ups staan **alleen lokaal** op martha's eigen schijf: ze beschermen **niet tegen schijfuitval**. |
| C21 | **HA installeert in gebruik geen Python-pakketten van internet.** | Requirements van custom integrations die niet in het HA-image zitten, staan als wheels in de bundle (`offline/wheels`). `install.sh` installeert ze met de `uv` van het image zelf, zonder netwerk, in `/opt/homeassistant/pydeps` (via `PYTHONPATH` in de container). Geen HACS. |
| C22 | **Geen SQLite meer voor de Recorder.** | De Recorder gebruikt PostgreSQL. `install.sh` hernoemt een oude `home-assistant_v2.db` naar `*.retired` en breekt af als HA toch een SQLite-database aanmaakt. |
| C23 | **De repo is publiek: code van anderen zonder licentie komt er niet in.** | Zo'n code (nu: Grott) haalt `download.ps1` alleen naar de lokale bundle, en staat in `.gitignore`. Code met een licentie (LTSS, MIT) mag wel in de repo. |
| C24 | **In HA staat alleen wat de gebruiker bewust heeft toegevoegd** (keuze van de gebruiker, 2026-10-19). | Geen `default_config`: `configuration.yaml` noemt alleen `frontend`, `energy`, `history`, `logbook`, `mobile_app` en `webhook`. Geen ontdekking (zeroconf, ssdp, dhcp, usb, Bluetooth), cloud, spraak, media, Sun of go2rtc. Integraties uit de onboarding (Shopping list, Radio Browser, Google Translate), de via het netwerk gevonden Sonos One, het Kaart-dashboard, de voorbeeld-blueprints (lege mappen met `.keep`, anders zet HA ze terug) en de standaardruimtes zijn verwijderd. Een integratie die een gevraagde functie nodig heeft (Met.no voor de buitentemperatuur, Nord Pool voor de marktprijs) blijft. **Uitzondering** (keuze van de gebruiker, 2026-10-30): HomeKit Device voor de thermostaat laadt zeroconf (mDNS) mee; zie *Thermostaat*. |
| C25 | **Dashboards per domein, niet per bron** (keuze van de gebruiker, 2026-10-19). | Drie dashboards: **Elektriciteit** (`/huis-elektriciteit`: de stroommeter en de tabs van de zonnepanelen), **Verwarming** (`/huis-verwarming`: gas en de vergelijking met de warmtepomp) en **Tapwater** (`/huis-tapwater`: gas voor warm water en de instellingen van de herkenning). HA eist een streepje in de URL. `host/ha/dashboards.py` bouwt ze alle drie opnieuw uit wat geïnstalleerd is; elk setup-script roept het aan, en de kaarten staan in `views()` naast de sensoren in elk script. Een nieuw onderdeel krijgt een plek in een domein, niet een eigen dashboard. **Progressive disclosure** (verzoek van de gebruiker): geen tabbalk; een dashboard met meer pagina's opent op een *overzicht* (titel, verbruik nu en vandaag, een knop per pagina) en de pagina's zijn subviews met een terugpijl, net zoals Thuis naar de dashboards leidt. Ook een dashboard met één pagina (Tapwater) opent op een overzicht. Op Thuis (drie kolommen: knop met daaronder *Nu* en *Vandaag*, elk met verbruik en kosten onder elkaar, bv. 0,17 kW / 0,05€/u) en op elk overzicht (dezelfde blokken naast elkaar; onderaan het blok *Nu* klein hoe lang de meting erachter geleden is: stroom = laatste telegram, gas = laatste verandering van de gasmeter, want die verandert alleen bij verbruik) staan het verbruik **en de kosten** nu en vandaag (`heat_home()`, contractprijzen zonder vaste kosten; stroom nu = vermogen x tarief van nu, in €/u). De blokken heten *Nu*, *Dag*, *Week* en *Maand* (verzoek van de gebruiker, 2026-10-30); op het overzicht van Elektriciteit staan ze als **één tabel** met de periodes als kolommen, de eenheden één keer vooraan de rij (`kWh`, `€`) en linksboven hoe lang de meting achter *Nu* geleden is, gecentreerd (`now_table()`, `TABLES`); Verwarming en Tapwater net zo, met `m³` en `€` en alleen *Nu* en *Dag*; de eerste sectie van een pagina heet *Deze dag*. De overzichten tonen daarnaast ook *Week* (vanaf maandag) en *Maand* (verzoek van de gebruiker, 2026-10-30; eerst alleen Elektriciteit, daarna ook Verwarming en Tapwater): stroom = meterstanden nu min aan het begin van de periode, met de tarieven van nu; gas = de som van de dagen in `heat_1d`; Thuis blijft bij *Nu* en *Dag*. Onder de blokken van Elektriciteit staat de stroom nu, waar vandaan en waar heen, in een **eigen kaart** (`host/ha/www/martha-power-flow.js`, keuze van de gebruiker, 2026-10-30): HA's power-sankey toont altijd *Niet getraceerd verbruik* en heeft geen optie om dat te verbergen, en kantelt op een telefoon naar boven-naar-beneden. De eigen kaart toont alleen net en zonnepanelen → woning en teruglevering, **altijd van links naar rechts** (ook op een telefoon). Zolang de omvormer niet gekoppeld is, toont hij bij teruglevering toch de zonnepanelen, met de teruglevering als ondergrens (*Zonnepanelen ≥ …*, *Woning ≥ …*) en een regel dat alleen de teruglevering bekend is (verzoek van de gebruiker, 2026-11-11: de panelen leveren sinds die dag). `install.sh` zet hem in `/config/www/martha/`; `dashboards.write_resource()` registreert hem als Lovelace-resource met een hash in de URL, zodat browsers een nieuwe versie laden. HA serveert `/local` alleen als `www` bestond bij het starten: de eerste keer is een herstart nodig. De titel van Thuis is de locatienaam van HA, zodat het adres niet in de repo staat. De bovenbalk van HA blijft staan (keuze van de gebruiker, 2026-10-19): verbergen kan alleen met de plugin kiosk-mode, en dat is niet gewenst. De pagina **Gebruik woning** (vervangt *Meter*, verzoek van de gebruiker, 2026-10-30) toont alleen het vermogen over 24 uur en per uur, dag, week, maand en jaar een grafiek van het gebruik en van de kosten; het gebruik is één balk per periode met dal en normaal gestapeld (statistics-graph `bar-stack`). Deze pagina wijkt af van de vaste indeling hieronder: geen totalen en geen *Details*. `write_energy()` vult nog steeds HA's Energie-instellingen: het net per tarief met het vermogen nu, en de zonnepanelen zodra de omvormer gekoppeld is. Naast elke paginatitel staat een terugknop (naar het overzicht, of vanaf een overzicht of een dashboard met één pagina naar Thuis); daarvoor zijn alle pagina's sections-views (`titled()`). **Vaste indeling** (verzoek van de gebruiker): elke pagina begint met een titel die zegt wat erop staat (`HEADINGS`); een pagina met gebruik en kosten volgt `dashboards.page()`: *Vandaag* (eerst wat er nu gebeurt), dan *Deze week*, *Deze maand*, *Dit jaar* (hoe verder naar beneden, hoe langer het bereik), elk met de totalen van die kalenderperiode en een grafiek, en *Details* onderaan. Een periodekiezer is er niet meer. **Startscherm** (`/lovelace`, het standaardpaneel voor iedereen via `default_panel`; sinds HA 2026 telt dat alleen als `lovelace` een eigen dashboardvermelding is, die HA bij een herstart maakt uit de oude standaardconfig, dus de eerste keer herstart `write_start` HA): per dashboard een knop met daaronder het verbruik **nu** en **vandaag** (`heat_home()`: stroom = meterstanden nu min middernacht; gas nu = gemiddelde van het afgelopen kwartier in m³/u, omdat de gasmeter per 5 minuten meldt; gas vandaag uit `heat_1d`); HA's gegenereerde Thuis-scherm (weer, apparaten) is niet meer de start. **Zijbalk** van de eigenaar: Energie, Activiteit, Geschiedenis, Media en Thuis verborgen (`hiddenPanels`; per gebruiker in HA). Die integraties blijven geladen: de grafieken hebben `history` nodig. |
| C26 | **Gas en kosten apart voor verwarming en tapwater** (verzoek van de gebruiker, 2026-10-19). | Per minuut staat het tapwaterdeel in `heat_1m` (`cv_cost_dhw`, `wp_cost_dhw`, ook tegen marktprijs, en `wp_kwh_dhw`); verwarming is het totaal min tapwater. CV-ketel: het gas voor warm water maal de gasprijs. Warmtepomp: warm water / COP bij de tapwatertemperatuur; het zonne-overschot en dus de stroomkosten worden naar verhouding van de kWh verdeeld. **Vaste kosten** (gasaansluiting, onderhoud) horen bij geen van beide: totaal = verwarming + tapwater + vast. Zichtbaar op Verwarming → *Kosten*, op Tapwater, per dag (`heat_split_days`, laatste 14 dagen) en per jaar (`year_split`). De nieuwe sensoren hebben pas statistieken vanaf 2026-10-19; de dagtabel rekent uit `heat_1d` en toont ook de dagen ervoor. |

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
  **`max_connections = 100`** (als `-c` in `docker-compose.yml`, dus ook voor een bestaande
  database). `timescaledb-tune` zet bij 1 GB 25, en HA alleen houdt er al zoveel open
  (Recorder, LTSS en de pool van de SQL-sensoren). Daarna weigerde PostgreSQL elke nieuwe
  verbinding, ook `psql`, `pg_dump` (back-ups) en de setup-scripts (gevonden op martha,
  2026-10-19, met 25 open verbindingen van HA).
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
  - `ltss_1m` (onbeperkt sinds 2026-10-08, keuze van de gebruiker; compressie na 30 dagen;
    een oude retentie van 2 jaar haalt het script weg) en `ltss_1h` (onbeperkt, gebouwd op
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

### P1-meter (DSMR)
De slimme meter is een Landis+Gyr E360 (DSMR 5.0, `/XMX5LGF…`). Hij stuurt elke seconde een
telegram via een FTDI-P1-kabel (`/dev/ttyUSB0`). HA leest hem met de ingebouwde DSMR-integratie.
Die wordt neergezet door `host/ha/setup_p1.py` (handmatig, als root, na `martha-ha token`), via de
config-flow-API en de websocket met het root-token:
- **Update-interval van 10 s.** De meter stuurt elke seconde, maar elke seconde is te veel voor
  Recorder en LTSS. Met 10 s heeft `ltss_1m` tot 6 samples per minuut voor gemiddelde, minimum
  en maximum.
- **Alle sensoren aan**, ook die HA standaard uitzet: spanning, stroom en vermogen per fase,
  stroomuitvallen, spanningsdips en -pieken, en het tijdstip. Samen 17 sensoren.
- **Herstart in plaats van reload.** Een optiewijziging en het aanzetten van entities plannen
  allebei een reload van de integratie. Overlappende DSMR-reloads laten de oude entities staan
  ("unique ID … already exists"), en die sensoren werken dan niet meer bij (gevonden op martha,
  2026-10-08). Het script herstart HA daarom één keer, en alleen als er iets veranderde.
- **Opslag** via LTSS (domein `sensor`). Per minuut staan ze voor altijd in `ltss_1m` (zie
  *Opslag van meetdata*). Alleen numerieke states komen in de aggregates. Het actieve tarief
  (`normal`/`low`) en het tijdstip zijn tekst, dus die staan alleen 30 dagen ruw in `ltss`.
- **Niet beschikbaar in HA**: het tekstbericht (`0-0:96.13.0`), het storingslogboek
  (`1-0:99.97.0`) en het tijdstip van de gasmeting. De equipment-ID's zijn de serienummers
  van de devices. Teruglevering in kWh is `unknown` zolang de teller op 0 staat (DSMR-gedrag).
- **Poort:** in de container bestaat `/dev/serial/by-id` niet (privileged maakt bij de start
  een eigen `/dev`). Het script gebruikt daarom het `ttyUSB`-pad van de enige USB-seriële
  adapter. *Bekend risico:* komt er een tweede USB-seriële stick bij (Zigbee), dan kan de
  nummering wisselen. Dan is een vast pad nodig, bijvoorbeeld een udev-symlink plus een
  mount van `/dev/serial`.
- **Dashboard** (C25: pagina *Gebruik woning* op Elektriciteit, pagina *Gas* op Verwarming; storage-modus, in `.storage/lovelace.*`):
  history-graphs (24 uur, uit de Recorder) en statistics-graphs (uur tot en met jaar, uit de
  long-term statistics). Spanning, stroom, fasen en storingen staan niet meer op een pagina. Het minuutdetail van langer dan 10 dagen geleden staat alleen in
  TimescaleDB; HA zelf toont dat niet.
- **Gas per 5 minuten (24 uur):** een lijngrafiek (statistics-graph, `period: 5minute`,
  `change`) uit HA's eigen 5-minutenstatistieken, die net zo lang bewaard blijven als
  `purge_keep_days`. De gasmeter geeft zijn stand elke 5 minuten door; per minuut zou dus vooral
  nullen geven. Een eerdere versie als tabel per minuut (SQL-sensor) is op verzoek van de
  gebruiker vervangen (2026-10-09).
- **Gas verdeeld (afgelopen uur)**, bovenaan de pagina *Gas* (verzoek van de gebruiker,
  2026-10-30): het gas per melding van de gasmeter als traplijn in m³/h, zoals het gas in de
  thermostaatgrafiek. Het vlak onder de lijn heeft de kleur van wat de tapwaterherkenning
  (`heat_gas`) koos: paars voor verwarming, cyaan voor tapwater (ook *los*), zodat je ziet wat
  de herkenning kiest. Eigen kaart
  `host/ha/www/martha-gas-split.js`; de reeks komt uit `heat_1m` via `heat_gas_recent()` als
  attribuut `reeks` van de SQL-sensor `heat_gas_recent`. De pagina *Gas* staat ook op
  Tapwater, zodat je er via beide dashboards komt. Een tik op de grafiek opent de subview
  *Gas verdeeld* (`gas-verdeeld`, op beide dashboards) met de grafiek groot en, net als HA's
  grafieken, interactief: zoomen met scrollen of twee vingers, slepen door de tijd, de waarden
  bij aanwijzen of tikken, knoppen voor in- en uitzoomen en *Nu*. Oudere stukken komen uit de
  geschiedenis in de recorder van de template-sensor `heat_gas_split`, een kopie van de reeks
  (de recorder slaat de attributen van een SQL-sensor niet op). Elke toestand bevat het laatste
  uur; de nieuwste per interval telt, dus terug tot `purge_keep_days` (10 dagen), en niet verder dan
  2026-10-30, toen de sensor er kwam.

### Zonnepanelen
Eisen, beslissingen (Z1–Z10) en ontwerp: [docs/zonnepanelen.md](docs/zonnepanelen.md). In het
kort: een Growatt MIN 3000TL-XH met een ShineLink op de LAN, sinds 2026-11-11 lokaal gelezen door
de ingebouwde **Modbus-integratie** van HA (Modbus TCP op de stick, poort 502, vast IP; keuze van
de gebruiker, zie Z6). De stick stuurt daarnaast gewoon naar de Growatt-cloud. Eerder gepland:
**Grott als systemd-service**
op martha. Dat is geen container, dus C6 blijft gelden; de broncode gaat mee in de bundle (C1),
maar staat niet in git (C23). Grott stuurt naar een webhook van HA. Zonder internet stuurt
`martha_grott.py` de datalogger door naar een lokale grottserver (alleen op localhost), omdat
Grott in proxy-modus anders niets verwerkt (C1a). Alle berekeningen zitten in TimescaleDB
(`host/db/solar.sql`, tabel `solar_1m` per minuut) en worden in HA getoond via SQL-sensoren en
HA-statistieken. Tarieven en investering zijn helpers in HA, en LTSS bewaart hun historie.
Martha heeft een vast IP-adres nodig (DHCP-reservering), omdat de stick geen `.local` kent.
- **Live op de pagina Zonnepanelen** (verzoek van de gebruiker, 2026-11-11): onder *Deze dag*
  eerst *Teruglevering nu* en *Verbruik nu* van de P1-meter (en *Opwek nu* zodra de omvormer
  gekoppeld is) en het vermogen over 24 uur; de tellers *Teruglevering normaal* en *dal* staan
  bij de totalen en grafieken per dag, week, maand en jaar. Zo is de pagina ook zonder omvormer
  bruikbaar. `setup_solar.views()` krijgt daarvoor de P1-entities mee.

### Thermostaat (Honeywell Lyric T6, HomeKit)
De kamerthermostaat is een Honeywell Lyric T6 met Apple HomeKit. Home Assistant praat er
**lokaal** mee via de ingebouwde integratie HomeKit Device (`homekit_controller`), zonder de
cloud van Honeywell/Resideo (C1a); die cloud-integratie vraagt internet en een eigen
API-sleutel. `host/ha/setup_thermostat.py --code <code>` koppelt hem via de config-flow-API.
De koppelcode staat op de sticker en komt niet in de repo (die is publiek, C23).
- HomeKit Device vindt accessoires via mDNS en laadt daarvoor **zeroconf**; dat is een bewuste
  uitzondering op C24 (keuze van de gebruiker, 2026-10-30). Ontdekte apparaten worden niet
  toegevoegd.
- Een HomeKit-accessoire heeft één eigenaar: hij mag niet ook in Apple Woning staan.
- De thermostaat moet op het hoofdnetwerk zitten (niet het gastnetwerk), anders ziet martha
  hem niet via mDNS. Hij meldt zich als `Tstat-XXXXXX._hap._tcp` (model `YT4710WF1003`).
  Vlak na het laden van zeroconf, of nadat hij net op het netwerk kwam, geeft de flow soms nog
  `no_devices`; het script probeert het daarom een paar keer.
- **Gekoppeld op 2026-10-30** (na verbinden met de wifi via de app Honeywell Home):
  `climate.tstat_34040c_lyric_t6_thermostat`.
- Op het overzicht van **Verwarming** staat onder de tabel **één grafiek** van het laatste
  **uur** (verzoek van de gebruiker, 2026-10-30; tijdas en tijden per kwartier): links de *Huidige temperatuur* en de *Doeltemperatuur* (°C),
  op een **tweede y-as rechts** het gas voor verwarming nu (`heat_home_space_now`, m³/h,
  gemiddelde van het laatste kwartier). HA's history-graph kan een climate-entity en een sensor
  niet combineren en heeft één as, dus is dit een **eigen kaart**
  (`host/ha/www/martha-thermostat-graph.js`, net als de stroomkaart, C25), door `install.sh` in
  `/config/www/martha/` gezet en door `dashboards.write_resource()` geregistreerd. De kaart haalt
  de geschiedenis via de websocket (`history/history_during_period`, de Recorder), voegt nieuwe
  states live toe, tekent **elke minuut** opnieuw (dus schuift hij op, ook zonder nieuwe meting;
  de laatste waarde loopt door tot nu) en haalt de geschiedenis elke 15 minuten opnieuw op.
  HomeKit Device krijgt veranderingen als melding van de thermostaat en vraagt daarnaast elke
  minuut de waarden op; HA schrijft alleen een nieuwe state als er iets veranderde.
  LTSS bewaart `climate` al (`martha_storage.yaml`).

### CV-ketel tegen warmtepomp
Ontwerp, rekenmodel en beslissingen (W1–W9): [docs/warmtepomp.md](docs/warmtepomp.md). In het
kort: dezelfde opzet als de zonnepanelen. `host/db/heat.sql` rekent per minuut (`heat_1m`) uit
wat het gas van de P1-meter met de CV-ketel kost, en wat dezelfde warmte zou kosten met een
volledig elektrische warmtepomp (COP uit een Carnot-model op een stooklijn, de buitentemperatuur
van de weer-entity), elk met de contractprijs en met de marktprijs (Nord Pool, en EnergyZero voor
gas via een REST-sensor). Warm water wordt herkend in de 5-minutendata van de gasmeter. Alle aannames zijn helpers (`input_number.heat_*`); hun
tekst en standaardwaarde staan in de tabel `heat_param`. Het dashboard Verwarming (C25) toont de
berekening van één minuut met zijn getallen en elke aanname met hoe hij te verfijnen is.
De jaarprognose gebruikt het gemeten warmteverlies en een referentiejaar (2019, Open-Meteo,
CC BY 4.0; data met licentie mag in de repo, C23).

### Back-ups en deploy (alleen productie)
De agent-laag (Hermes in NemoClaw, martha-gate, staging en goedkeuren in de Companion-app) is
op 2026-10-30 geschrapt (keuze van de gebruiker); de git-historie bewaart het ontwerp. Er is
alleen productie.
- **Een deploy gaat met `deploy.ps1`** op Windows (PowerShell 5.1); martha doet geen `git pull`
  (C1). Het script:
  1. weigert bij niet-gecommitte of niet-gepushte wijzigingen, en neemt de korte hash van `HEAD`
     (10 tekens);
  2. leest de vorige deploy-commit uit `/var/lib/martha/deployed`, en kiest uit de diff sindsdien
     of er ook een database-dump komt (`host/db/` of een setup-script gewijzigd) en welke
     setup-scripts draaien. Is de vorige deploy onbekend, dan wel een dump en geen scripts,
     tenzij `-Setup` ze noemt. `setup_solar` draait met `--inverter` als `/etc/martha/inverter`
     bestaat (het IP-adres van de ShineLink). De dashboards draaien alleen apart als er geen ander script draait (elk script
     schrijft ze aan het eind);
  3. kopieert alleen `host/` met `scp` naar `~/martha-deploy` (geen `offline/`);
  4. draait `host/deploy.sh` met sudo in een sessie met terminal (`ssh -tt`). De andere
     ssh-aanroepen krijgen `-n`, zodat alleen sudo stdin leest. `deploy.sh` installeert eerst
     `martha-ha`, maakt `martha-ha backup <commit>` (en `backup-db <commit>`), draait
     `install.sh --host-only` en de setup-scripts, en zet bij een fout de config terug met
     `martha-ha restore <commit>`. Daarna schrijft hij de commit naar `deployed`.
- **`install.sh --host-only`**: geen apt, geen images, geen LTSS, wheels of Grott uit de bundle,
  `.env` blijft staan en HA wordt niet gestopt. Wel de host-bestanden, het schema, de services en
  het opruimen hieronder. Een nieuwe versie van Docker, HA of TimescaleDB gaat via de volledige
  `install.sh` (route B).
- **`martha-ha`** (`host/martha_ha.py`, Python-stdlib):
  - `backup [label]`: `tar.zst` (Python 3.14 `tarfile`) van de hele config-map, ook `secrets.yaml`
    en `.storage`, zonder `deps`; SQLite-bestanden via de online backup-API. Bestandsnaam
    `config-<YYYY-MM-DD_HHMMSS>-<label>.tar.zst`, ongeveer 90 KB.
  - `backup-db [label]`: `pg_dump -Fc` als `db-<tijd>[-<label>].pgdump` (ongeveer 32 MB op
    2026-10-30), elke nacht om 03:30 via `martha-backup-db.timer` (label `nightly`).
  - `restore <commit|label|bestand>`: de nieuwste config-back-up met die commit of dat label (een
    korte en een lange hash passen op elkaar). HA stopt, de huidige map gaat opzij als
    `config.before-restore-<tijd>`, de back-up wordt uitgepakt, HA start en de health-check
    draait. Leest ook het oude formaat `ha-*.tar.zst`, en neemt daaruit alleen de config.
  - `restore-db <dump>`: hernoemt de huidige database naar `homeassistant_before_restore_<tijd>`
    en laadt de dump in een nieuwe, met `timescaledb_pre_restore()`/`post_restore()` (getest op
    martha, 2026-10-07: hypertables, continuous aggregates en policies komen terug). Haalt de dump
    ook uit een oude `ha-*.tar.zst`.
  - `token`: logt interactief in via de login-flow (`/auth/login_flow`) en maakt het long-lived
    token `martha-root` in `/etc/martha/ha-root.env` (0600), dat de setup-scripts gebruiken. Dit
    vervangt `setup-gate` (zonder gate-gebruiker, staging-token of notify-service).
  - `check` (`check_config` op de huidige config) en `status` (laatste back-ups, vrije ruimte).
    Elke wijzigende opdracht neemt een flock; een tweede wacht op zijn beurt.
- **De setup-scripts schrijven hun packages met `put_files()`**: eerst `check_config` in een
  wegwerpcontainer (`--network none`) op een kopie met de nieuwe bestanden, dan schrijven,
  herladen (alleen automations, scripts, scenes, blueprints) of herstarten, en de health-check op
  de log. `check_config` vangt niet alles: een ongeldige automation-trigger meldt HA pas bij het
  opstarten ("has been disabled"), daarom leest de health-check de log op `FATAL_LOG`. Faalt hij,
  dan komen de vorige bestanden terug en herstart HA. Elk script maakt vooraf zelf een back-up
  (`before-p1`, `before-solar`, `before-heat`).
- **`packages/martha_storage.yaml`** (ADR-001) is van `install.sh` (alleen neergezet als hij
  ontbreekt; `setup_solar.py` past alleen de LTSS-include aan). `check_config` krijgt
  `/opt/homeassistant/pydeps` mee, zodat LTSS te importeren is.
- **Opruimen van de agent-laag** door `install.sh` (idempotent, uitgevoerd op 2026-10-30): de
  services `martha-gate`, `martha-ha` (de daemon) en `martha-staging-proxy`, de staging-container
  en het netwerk `martha-staging`, user en groep `martha-gate`, `gate.env`, `staging/`,
  `proposals/` en `NOTIFY_SERVICE` in `ha-root.env`. `martha-ha cleanup-legacy` zet
  `ha-config.git` als `ha-config-git-<tijd>.tar.zst` bij de back-ups en verwijdert hem, haalt
  `packages/martha_gate.yaml` (met een herstart) en `martha_gate_secret` weg, en verwijdert de
  HA-gebruiker "Martha gate". NemoClaw en OpenShell (geïnstalleerd als `david`) ruimt
  `install.sh` niet op. Bestaande back-ups blijven staan.

## Open punten
- **Zonnepanelen** (vanaf 2026-10-23): ontwerp staat (zie *Zonnepanelen*), bouw in 6 fasen,
  met de omvormer zo laat mogelijk (fase 5). Fase 1–4 zijn gebouwd (2026-10-12): het
  databaseschema is getest op martha (teruggedraaid), en op 2026-10-13 staat het live:
  prijzen, helpers en dashboard `/zonne-energie`. Fase 5 is voorbereid (2026-10-13): Grott met terugval is getest met een
  nagebootste datalogger, maar nog niet met de echte stick.
  Open: de contracttarieven, de gegevens van de installatie en het interval van de ShineLAN-X.
- **Warmtepomp** (2026-10-19): gebouwd en live, met de gegevens van de gebruiker (zie *CV-ketel
  tegen warmtepomp*). Open: een betrouwbaar gemeten warmteverlies (pas na een maand stookseizoen;
  tot die tijd geijkt op ~800 m³ per jaar) en het exacte jaarverbruik.
- **`check_config` geeft exit 0 bij een ongeldige template-optie** (gevonden op 2026-10-13,
  HA 2026.9.4). Hij logt alleen `ERROR:homeassistant.config:Invalid config for 'template' …`.
  `check()` in `martha_ha.py` kijkt alleen naar de exitcode en laat zo'n fout dus door. Na de
  herstart vangt de health-check het wel ("Invalid config" staat in `FATAL_LOG`), en komen de
  vorige bestanden terug. Te overwegen: `check()` laten afkeuren op `Invalid config` in de
  uitvoer.
- Offline OS-updates (zie hierboven).
- Back-up buiten martha (nu staan de back-ups op dezelfde schijf, zie C19).
- **De health-check mist fouten bij het opzetten van een platform.** "Error while setting up
  sql platform" (bij de eerdere gastabel) kwam niet door de health-check (gevonden op 2026-10-09). `FATAL_LOG` in
  `martha_ha.py` herkent het patroon niet. Het toevoegen moet nog worden afgewogen: een
  integratie die tijdelijk een apparaat mist, logt hetzelfde en zou dan worden teruggedraaid.
- LTSS is een community-integratie zonder onderhoudsgarantie (laatste release 2024-12).
  Terugvaloptie volgens ADR-001: `mqtt_statestream` → Telegraf → Timescale.
- **ADR-001 staat op martha** (verse installatie, 2026-10-07): schema toegepast, de Recorder
  schrijft in PostgreSQL en LTSS in `ltss`; `ltss_1m` en `ltss_1h` en hun policies bestaan.
  Met echte P1-data (2026-10-08) vult `ltss_1m` zich per minuut met gemiddelde, min, max en
  laatste waarde; `ltss_1h` is nog niet bekeken.
