# Hermes-agent: voorbereiden

De agent-laag is optioneel. Home Assistant werkt ook zonder agent. Ontwerp en
beveiligingskeuzes staan in [DESIGN.md](../DESIGN.md#agent-hermes-ha-configureren-zonder-schrijfrechten-op-productie).
Deze gids beschrijft wat je **vooraf** regelt. Pas daarna installeer je de agent op martha.

> `install-agent.sh` (NemoClaw + Hermes) volgt nog; deze gids wordt dan aangevuld.

## 1. Staging en de Companion-app

Zie [Staging: wijzigingen eerst uitproberen](../readme.md#staging-wijzigingen-eerst-uitproberen)
in de readme:
- Onboard staging op `http://martha.local:8124`.
- Voeg staging toe als tweede server in de Companion-app.
- Zorg dat de app ook op productie (`http://martha.local:8123`) is ingelogd. De meldingen
  met **Toepassen/Afwijzen** komen via productie binnen.

## 2. Telegram-bot

Via Telegram praat je met Hermes en stuurt hij je verbetertips.

1. **Bot aanmaken**
   1. Zoek in Telegram **@BotFather** (met het blauwe vinkje) en open de chat.
   2. Stuur `/newbot`.
   3. Kies een **naam**, bijvoorbeeld `Martha Hermes`.
   4. Kies een **gebruikersnaam**. Die moet eindigen op `bot` en uniek zijn, bijvoorbeeld `martha_hermes_bot`.
   5. BotFather stuurt een **token** zoals `123456789:AAH...`. Bewaar het in je
      wachtwoordmanager. **Zet het nooit in deze repo of in een chat.** Wie het token
      heeft, bestuurt de bot.
2. **Bot afschermen** (in BotFather)
   - Stuur `/setjoingroups`, kies je bot en kies **Disable**. Dan kan niemand hem aan een groep toevoegen.
   - Hermes reageert alleen op de user-ID's die je bij de installatie opgeeft (stap 3).
     Berichten van anderen negeert hij.
3. **Je user-ID opzoeken**
   - Zoek **@userinfobot**, open de chat en druk op Start. Het **Id** dat hij terugstuurt
     (een getal) is je user-ID. Dat is niet geheim.
4. **De bot openen**
   - Open de chat met je eigen bot en druk op **Start**. Een bot kan je pas berichten
     sturen nadat jij hem eerst een bericht hebt gestuurd.

Token kwijt of uitgelekt? Stuur `/revoke` aan @BotFather en kies je bot. Je krijgt een
nieuw token en het oude werkt niet meer. Daarna installeer je de agent opnieuw met het nieuwe token.

## 3. Inference-provider en API-key

Hermes gebruikt GLM 5.3 Flash bij **Phala Confidential AI**, rechtstreeks op een attested
TEE-endpoint ([ADR-002](../docs/adr/ADR-002-inference-provider.md), C20). Routers als
OpenRouter zijn uitgesloten: die zien je prompts.

Volg [docs/phala.md](../docs/phala.md), stap 1 t/m 4: account, tegoed (~$15, zonder automatisch
opwaarderen), een API-key alleen voor martha, en een testverzoek. Bewaar de key in je
wachtwoordmanager. Hij komt nooit in deze repo; je typt hem bij de installatie zelf in op martha.

Key kwijt of uitgelekt? Trek hem in bij Phala, maak een nieuwe aan en installeer de agent opnieuw.

## 4. martha-gate

Zie [Goedkeuren op je telefoon](../readme.md#goedkeuren-op-je-telefoon-martha-gate) in de
readme: draai eenmalig `ssh -t david@martha.local sudo martha-ha setup-gate` en controleer
dat de testmelding binnenkomt.

## Checklist vóór de installatie

- [ ] Staging is onboard en staat in de Companion-app.
- [ ] De Companion-app is ingelogd op productie.
- [ ] Je hebt het Telegram-bottoken (in je wachtwoordmanager).
- [ ] Je weet je Telegram-user-ID.
- [ ] Je hebt de bot zelf een keer gestart.
- [ ] Je hebt een Phala-account met tegoed en een API-key, en het testverzoek uit `docs/phala.md` werkt.
- [ ] `martha-ha setup-gate` is gedraaid en de testmelding kwam binnen.
