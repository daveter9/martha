# Werkafspraken voor Claude

- **Vastleggen in de repo.** Afspraken, voorkeuren en beslissingen gaan in deze repo,
  niet in een lokaal geheugen: werkafspraken in dit bestand, design constraints en
  architectuurbeslissingen in [DESIGN.md](DESIGN.md). Nieuwe constraints die je
  tegenkomt neem je daar meteen op.
- **Commits.** Conventional commits, logisch opgesplitst. Push na elke commit meteen.
  Kijk vóór elke push (`git log origin/master..HEAD`) of er lokaal nieuwe commits zijn
  bijgekomen die niet van jou zijn; meld die eerst, in plaats van ze ongezien mee te pushen.
- **Meteen naar productie.** Na elke aanpassing zet je die direct op martha (prod), niet
  alleen in de repo: committen, pushen en dan `deploy.ps1` (zie *Back-ups en deploy* in
  DESIGN.md). Dat maakt eerst een config-back-up met de commit (C19), kopieert `host/` met
  `scp`, draait `install.sh --host-only` en de setup-scripts die bij de diff horen, en zet bij
  een fout de config terug. Over `ssh david@martha.local` (key-login). sudo vraagt een
  wachtwoord; dat komt nooit in de repo.
- **Taal.** Overleg en documentatie in het Nederlands; code en commentaar in scripts in het Engels.
- **Architectuurvragen** stel je aan de gebruiker, in plaats van zelf te kiezen.
- **Vragen stel je in de sessie, één tegelijk.** Stelt de gebruiker een
  aanpassing voor, dan bouw je die meteen, zonder eerst om bevestiging te vragen.
- **Alleen wat gevraagd is.** Voeg in HA niets toe waar de gebruiker niet om vroeg (C24 in
  DESIGN.md); stel extra's voor in plaats van ze te bouwen.
- **Niet testen in WSL.** Gebruik de WSL-distro's van de gebruiker niet om iets te
  testen, ook niet read-only of als simulatie. Verifieer met wat native op Windows
  draait (PowerShell, `bash -n` in Git Bash). Vraag het eerst als je een andere
  Linux-omgeving wilt gebruiken.
- **Offline installeren is een harde eis.** Installeren en updaten op de doel-pc mag geen
  netwerk nodig hebben. Home Assistant mag in gebruik wel internet gebruiken, maar moet
  ook zonder internet werken (zie C1 en C1a in DESIGN.md).
