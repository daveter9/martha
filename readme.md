# Martha
Martha installeren en configureren: een pc met Ubuntu Server 26.04 waarop Home Assistant
in een Docker-container draait. De installatie werkt **volledig offline**. Alles wat de
doel-pc nodig heeft staat in deze repo. Komt er later internet bij, dan mag Home Assistant
dat gebruiken; zonder internet blijft hij werken, alleen cloud-integraties niet.

- Design constraints en architectuurbeslissingen staan in [DESIGN.md](DESIGN.md).
- Werkafspraken voor Claude staan in [CLAUDE.md](CLAUDE.md).

Er zijn twee installatieroutes:

| Route | Wanneer | Wat gebeurt er |
|---|---|---|
| **A: Autoinstall** | Nieuwe of te wissen pc | USB erin, `yes` typen. Ubuntu, Docker en Home Assistant komen er volledig automatisch op. **Wist de schijf.** |
| **B: Bestaande server** | Er draait al een (lege) Ubuntu Server 26.04 | Eén commando over SSH vanaf Windows, of de bundle via USB en dan `install.sh`. De schijf blijft intact. |

## Inhoud

| Map | Wat | Waar draait het |
|---|---|---|
| `0. install os/` | Ubuntu Server 26.04 ISO (in delen, samenvoegen met `iso.ps1 -Join`) en Rufus | Windows |
| `1. download/download.ps1` | Vult `offline/` met Docker-packages, de images van Home Assistant en TimescaleDB, en LTSS met zijn wheels | Windows, **met** internet |
| `2. usb/make-usb.ps1` | Zet de bundle (en voor route A `autoinstall.yaml`) op een USB-stick | Windows |
| `2. ssh/install-ssh.ps1` | Route B over SSH: kopieert de bundle en draait `install.sh` op de server | Windows, LAN naar de server |
| `host/` | `install.sh`, `docker-compose.yml`, firstboot-service, databaseschema (`db/`) en HA-package (`ha/`) | Doel-pc |
| `offline/` | Gegenereerde bundle (signed apt-mirror, OCI-images, LTSS, wheels) | Doel-pc |
| `docs/adr/` | Architectuurbeslissingen (ADR's) | - |

## Stap 1: bundle verversen (optioneel, met internet)

`offline/` staat al in de repo. Alleen voor een nieuwere Home Assistant of nieuwere Docker-packages:

```powershell
powershell -ExecutionPolicy Bypass -File ".\1. download\download.ps1"                       # laatste stable
powershell -ExecutionPolicy Bypass -File ".\1. download\download.ps1" -HaVersion 2026.10.1  # vaste versie
```

Het script haalt de Ubuntu-indexen en `.deb`'s op en controleert hun SHA256. Het
Home Assistant-image komt rechtstreeks uit `ghcr.io` (Docker is niet nodig) en wordt
op digest gecontroleerd. Daarna `git add -A` en committen; de binaries gaan via Git LFS
(eenmalig per pc: `git lfs install`).

Nieuwe Ubuntu-ISO? Zet hem in `0. install os\` en zet de officiële regel uit
`SHA256SUMS` in `<iso>.sha256`. Draai daarna `iso.ps1 -Split -Iso <naam>` en pas de
standaardnaam in `iso.ps1` en deze gids aan.

---

## Route A: Autoinstall

### A1. USB-stick maken (Windows)

Je hebt een USB-stick van minimaal 8 GB nodig. **Alles op de stick wordt gewist.**

0. Na een verse clone staat de ISO in delen in de repo (GitHub LFS staat max. 2 GB per
   bestand toe). Voeg ze samen; het script controleert de officiële SHA256:

   ```powershell
   git lfs pull
   powershell -ExecutionPolicy Bypass -File ".\0. install os\iso.ps1" -Join
   ```
1. Start `0. install os\rufus-4.15.exe`.
2. **Device**: kies de USB-stick.
3. **Boot selection**: SELECT, dan `0. install os\ubuntu-26.04.1-live-server-amd64.iso`.
4. **Partition scheme**: `GPT`, **Target system**: `UEFI (non CSM)`.
   (Heeft de doel-pc alleen legacy BIOS, kies dan `MBR`.)
5. **File system**: `FAT32`. Laat *Persistent partition* op 0.
6. START. Kies bij de vraag *ISOHybrid image detected* voor **Write in ISO Image mode
   (Recommended)**, niet DD-mode: alleen in ISO-mode blijft de stick beschrijfbaar.
7. Wacht tot de status *READY* is en sluit Rufus. Noteer de stationsletter, bijvoorbeeld `E:`.
8. Zet de autoinstall en de bundle erop:

   ```powershell
   powershell -ExecutionPolicy Bypass -File ".\2. usb\make-usb.ps1" -Drive E:
   ```

   Het script vraagt om het wachtwoord voor de beheerdersaccount op de doel-pc.
   Optionele parameters:

   | Parameter | Standaard | |
   |---|---|---|
   | `-Hostname` | `martha` | |
   | `-Username` | `david` | beheerder met sudo |
   | `-SshKeyFile` | geen | bijvoorbeeld `$HOME\.ssh\id_ed25519.pub`; dan alleen SSH-login met een key |
   | `-Timezone` | `Europe/Amsterdam` | |
   | `-Keyboard` | `us` | bijvoorbeeld `nl` |
   | `-Locale` | `en_US.UTF-8` | |
9. Werp de stick veilig uit.

### A2. De doel-pc installeren (offline)

> ⚠️ De autoinstall **wist de grootste schijf** van de doel-pc volledig.

1. Sluit een netwerkkabel aan (mag ook later). Internet is niet nodig.
2. Steek de USB-stick erin en boot ervan (meestal via F12, F11, F8 of Esc voor het
   bootmenu). Secure Boot mag aan blijven.
3. Kies *Try or Install Ubuntu Server*.
4. De installer vindt `autoinstall.yaml` en vraagt **"Continue with autoinstall? (yes|no)"**.
   Typ `yes` en Enter.
5. De installatie loopt zonder verdere vragen. Daarna **schakelt de pc zichzelf uit**.
6. Haal de USB-stick eruit en zet de pc aan.
7. Bij de eerste boot installeert `martha-firstboot.service` Docker en laadt het
   Home Assistant-image. Dat duurt een paar minuten en de voortgang staat op het scherm.
   Als het klaar is staat er: `Home Assistant ... is starting on http://<ip>:8123`.
8. Ga verder bij [Na de installatie](#na-de-installatie).

Lukt de first-boot-installatie niet? Bekijk dan `journalctl -u martha-firstboot.service`
en draai hem opnieuw met `sudo bash /opt/martha/host/install.sh`.

---

## Route B: Bestaande lege server

Vereiste: **Ubuntu Server 26.04 amd64**, met een gebruiker die sudo mag. Docker en
Home Assistant hoeven er nog niet op te staan. Internet is niet nodig.

### B1. (Als Ubuntu er nog niet op staat) Ubuntu handmatig installeren

1. Maak een stick met Rufus zoals in [A1](#a1-usb-stick-maken-windows), stap 1 t/m 7,
   en zet daarna **alleen de bundle** erop (zonder `autoinstall.yaml`):

   ```powershell
   powershell -ExecutionPolicy Bypass -File ".\2. usb\make-usb.ps1" -Drive E: -BundleOnly
   ```

   Zo dient dezelfde stick als installer **en** als bundle voor stap B3.
2. Boot ervan en doorloop de installer zoals je wilt. Offline let je op:
   - *Network*: geen verbinding is prima, kies *Continue without network* als dat gevraagd wordt.
     `install.sh` zet daarna DHCP aan op alle netwerkpoorten (als er nog geen bekabelde
     configuratie is), zodat een kabel die je later insteekt vanzelf werkt.
   - *Ubuntu archive mirror*: de mirror-test faalt offline; ga door (*Continue*).
   - Kies **Ubuntu Server** (niet *minimized*) en vink **Install OpenSSH server** aan als je SSH wilt.
   - Featured server snaps: niets selecteren.
3. Reboot na de installatie en haal de stick er even uit.

Daarna installeer je Docker en Home Assistant op een van twee manieren: **B2 via SSH**
(aanbevolen als de server in het netwerk hangt) of **B3 via USB**.

### B2. Installeren via SSH (vanaf Windows)

Vereisten: de server is bereikbaar over het LAN (internet is niet nodig), er draait
OpenSSH-server, en de gebruiker mag sudo. Op Windows gebruik je de ingebouwde
OpenSSH-client (*Instellingen > Systeem > Optionele onderdelen > OpenSSH Client*).

```powershell
powershell -ExecutionPolicy Bypass -File ".\2. ssh\install-ssh.ps1" -Target david@martha.local
```

Het script:
1. controleert over SSH dat de server Ubuntu 26.04 amd64 is (voordat er ~1,1 GB gekopieerd wordt);
2. kopieert `host\` en `offline\` met `scp` naar `~/martha-bundle` op de server;
3. draait daar `sudo bash ~/martha-bundle/host/install.sh` in een interactieve sessie (sudo vraagt om je wachtwoord);
4. ruimt `~/martha-bundle` op (houd hem met `-KeepBundle`).

Zonder SSH-key vraagt SSH bij elke stap om het wachtwoord, dus drie keer plus één keer
voor sudo. Met een key (`-IdentityFile $HOME\.ssh\id_ed25519`) vraagt alleen sudo nog.
Andere poort: `-Port 2222`.

### B3. Installeren via USB-stick

Zet de bundle op een stick (elk bestandssysteem; de stick uit B1 heeft hem al):

```powershell
powershell -ExecutionPolicy Bypass -File ".\2. usb\make-usb.ps1" -Drive E: -BundleOnly
```

Steek de stick in de server en mount hem:

```bash
lsblk -f                                  # zoek de partitie van de stick, bijvoorbeeld sdb1 (FAT32/vfat)
sudo mkdir -p /mnt/usb
sudo mount -o ro /dev/sdb1 /mnt/usb
sudo bash /mnt/usb/martha/host/install.sh
sudo umount /mnt/usb
```

`install.sh` installeert Docker uit de offline mirror, laadt het image en start Home
Assistant. Aan het eind staat er: `Home Assistant ... is starting on http://<ip>:8123`.
Het pad naar de bundle mag geen spaties bevatten.

---

## Na de installatie

Open `http://martha.local:8123` in een browser en doorloop de onboarding van Home Assistant.
`install.sh` installeert `avahi-daemon`, zodat de pc via mDNS als `<hostnaam>.local` te vinden is
(Windows 10+, macOS, iOS en de meeste Linux-desktops ondersteunen dat). Lukt dat niet, gebruik dan
het IP-adres: `http://<ip-van-de-pc>:8123`.

```bash
sudo docker ps                                          # draaien de containers 'homeassistant' en 'timescaledb'?
sudo docker logs -f homeassistant                       # logs van Home Assistant
sudo docker exec -it timescaledb psql -U homeassistant   # SQL op de database (Recorder en LTSS)
sudo docker compose -f /opt/homeassistant/docker-compose.yml restart
sudo usermod -aG docker $USER                           # optioneel: docker zonder sudo (opnieuw inloggen)
```

| Pad | Inhoud |
|---|---|
| `/opt/homeassistant/config` | Home Assistant-configuratie: **dit is wat je back-upt** |
| `/opt/homeassistant/docker-compose.yml`, `.env` | Compose-project (image-versies, tijdzone) |
| `/opt/homeassistant/postgres` | PostgreSQL/TimescaleDB-data (Recorder en LTSS): **ook back-uppen**, met `pg_dump` |
| `/opt/homeassistant/db.env` | Databasewachtwoord (ook als `martha_db_url` in `config/secrets.yaml`) |
| `/opt/homeassistant/pydeps` | Python-pakketten voor LTSS, door `install.sh` uit de wheels gebouwd |
| `/opt/martha` | Kopie van de bundle (alleen bij route A) |
| `/var/lib/martha/installed` | `bundle.env` van de laatste installatie |

## Updaten (offline, beide routes)

1. Draai op Windows `1. download\download.ps1` ([stap 1](#stap-1-bundle-verversen-optioneel-met-internet)).
2. Maak een back-up van `/opt/homeassistant/config` en van de database:
   `sudo docker exec timescaledb pg_dump -U homeassistant -Fc homeassistant > ha-db.dump`.
3. Installeer opnieuw zoals in [B2 (SSH)](#b2-installeren-via-ssh-vanaf-windows) of
   [B3 (USB)](#b3-installeren-via-usb-stick). Dat werkt ook voor een pc die met route A is geïnstalleerd.

Tip voor route A: de first-boot-installatie volg je ook over SSH met
`ssh david@martha.local journalctl -fu martha-firstboot.service`.

`install.sh` werkt Docker bij naar de versie in de bundle, laadt het nieuwe image en
maakt de container opnieuw aan met dezelfde `config`-map.
