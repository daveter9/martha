# Werkafspraken voor Claude

- **Vastleggen in de repo.** Afspraken, voorkeuren en beslissingen gaan in deze repo,
  niet in een lokaal geheugen: werkafspraken in dit bestand, design constraints en
  architectuurbeslissingen in [DESIGN.md](DESIGN.md). Nieuwe constraints die je
  tegenkomt neem je daar meteen op.
- **Taal.** Overleg en documentatie in het Nederlands; code en commentaar in scripts in het Engels.
- **Architectuurvragen** stel je aan de gebruiker, in plaats van zelf te kiezen.
- **Niet testen in WSL.** Gebruik de WSL-distro's van de gebruiker niet om iets te
  testen, ook niet read-only of als simulatie. Verifieer met wat native op Windows
  draait (PowerShell, `bash -n` in Git Bash). Vraag het eerst als je een andere
  Linux-omgeving wilt gebruiken.
- **Offline installeren is een harde eis.** Installeren en updaten op de doel-pc mag geen
  netwerk nodig hebben. Home Assistant mag in gebruik wel internet gebruiken, maar moet
  ook zonder internet werken (zie C1 en C1a in DESIGN.md).
