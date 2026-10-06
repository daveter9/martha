# ADR-002: Inference-provider voor Hermes (HA-configuratieagent)

- **Datum:** 2026-10-06
- **Status:** Geaccepteerd
- **Beslissing in één zin:** Hermes gebruikt GLM 5.3 Flash rechtstreeks bij Phala
  (TEE-endpoint), zonder OpenRouter ertussen.

Uitvoering: [Phala opzetten](../phala.md).

## Context

Hermes Agent draait zelf-gehost naast Home Assistant en bouwt en wijzigt HA-configuraties
via MCP. Daarvoor is een externe LLM-provider nodig. De prompts bevatten de volledige
thuisconfiguratie: apparaten, entiteiten, energiedata en routines.

Eisen:

- **TEE:** inference draait in een hardware-enclave (Intel TDX + NVIDIA confidential GPU)
  met verifieerbare attestatie.
- **End-to-end:** geen partij buiten de enclave ziet queries in plaintext, ook geen router
  of gateway-operator.
- **Kwaliteit:** weinig hallucinatie; verzonnen entiteiten of service-opties breken configs.
- **Kosten:** zo laag mogelijk, met een harde bovengrens op spend.

## Beslissing

| Onderdeel | Keuze |
|---|---|
| Provider | Phala Confidential AI, direct (OpenAI-compatibele API) |
| Endpoint | `tee.redpill.ai` of `inference.phala.com` (attested TEE-domeinen); **niet** `api.redpill.ai` |
| Model | GLM 5.3 Flash (Z.ai), TEE-deployment via Phala |
| Router | Geen; OpenRouter valt af |
| Verificatie | Lokale attestatie-verifiërende proxy tussen Hermes en Phala |
| Budget | Prepaid tegoed als harde cap, plus per-key limiet indien beschikbaar |

### Onderbouwing: Phala direct, zonder OpenRouter

Via OpenRouter is de eis "end-to-end" niet haalbaar: de TLS-verbinding eindigt bij
OpenRouter, dat de prompt in plaintext leest en doorstuurt. De TEE beschermt dan alleen
het laatste stuk, niet de router.

Phala direct haalt de eis wel, onder voorwaarden:

- **Twee hops RA-TLS.** De eerste TLS-hop eindigt in de dstack-gateway-CVM, de tweede in
  de model-CVM; Phala stelt dat er geen plaintext-tussenpersoon op de host is (Phala).
- **Juiste domein.** Afgedwongen attested serving geldt voor `tee.redpill.ai` en
  `inference.phala.com`; `api.redpill.ai` viel daar bij een externe audit buiten (audit).
- **Zelf verifiëren.** Alleen een controle dat de TLS-sleutel van het endpoint in de
  attestatie staat bewijst dat de verbinding in de enclave eindigt (cookbook). Hermes doet
  dat niet zelf, dus een lokale proxy doet het.

Precies geformuleerd is dit *end-to-enclave*: de prompt wordt ontsleuteld binnen de
attested Phala-gateway en model-CVM. Geen operator of host kan meelezen zolang de
attestatie klopt.

### Onderbouwing: GLM 5.3 Flash

GLM 5.3 Flash wint op hallucinatie met grote marge en is per taak ook goedkoper dan het
enige andere sterke, goedkope TEE-alternatief.

| Model (Phala TEE) | Input / output / cache-read ($ per 1M) | AA Intelligence | AA non-hallucination | Snelheid (tok/s) |
|---|---|---|---|---|
| GLM 5.3 Flash | 0,15 / 0,50 / 0,03 | 41,8 | 72,4% | 22 |
| DeepSeek V4.1 Flash | 0,30 / 1,20 / 0,006 | 39,5 (Max) | 3,5% (Max) | 55 |

Geverifieerd: prijzen zijn Phala-lijstprijzen zonder tijdelijke kortingen; benchmarks zijn
Artificial Analysis; snelheid is de OpenRouter-meting van Phala als provider en kan direct
anders zijn.

De non-hallucination-score is doorslaggevend. Bij configwerk is "weet ik niet, ik
controleer het" beter dan een verzonnen attribuut dat pas faalt bij de herstart van HA.

**Kosteninschatting** (geschat, niet gemeten). Aannames per taak: 25 agent-calls van ~25k
context, 70% cache-hit, ~25k output inclusief reasoning.

| Fase | Taken | GLM 5.3 Flash |
|---|---|---|
| Per taak | 1 | ~$0,05 (zonder cache ~$0,11) |
| Bouwfase | 10 per dag | ~$16 per maand |
| Daarna | 30 per maand | ~$1,60 per maand |

## Overwogen alternatieven

| Alternatief | Reden afgewezen |
|---|---|
| OpenRouter met Phala gepind | Router ziet plaintext; geen end-to-end |
| DeepSeek V4.1 Flash (Phala) | Non-hallucination 3,5%; output 2,4× duurder |
| NEAR AI Cloud, GLM 5.3 Flash | Ook TEE; afgewezen om één provider aan te houden. Blijft de fallback |
| Nemotron 3.5 Lightning (Phala) | Goedkoopst ($0,07 / $0,20), maar 3B actief; waarschijnlijk te zwak voor multi-step configwerk (inschatting) |
| Lokaal model op de HA-host | Mini-pc zonder GPU haalt niet de kwaliteit en snelheid voor agentwerk |

## Consequenties

**Positief**

- Geen derde partij buiten de enclave ziet de thuisconfiguratie in plaintext.
- Elke sessie is cryptografisch te controleren via attestatie.
- Laagste kosten per taak van de geschikte TEE-modellen.

**Negatief**

- Geen automatische failover: valt Phala uit, dan staat Hermes stil. Gemeten uptime via
  OpenRouter: 99,69%.
- Geen OpenRouter-guardrails meer voor budget per periode.
- Traag: 22 tok/s betekent naar schatting 15–20 minuten per configtaak.
- Restvertrouwen blijft bij Intel en NVIDIA (hardware) en bij de gemeten Phala-code.

## Maatregelen

- [ ] Attestatie-verifiërende proxy (bijvoorbeeld teep) op de HA-host; Hermes praat alleen
      met die proxy.
- [ ] Endpoint vastzetten op `tee.redpill.ai` of `inference.phala.com`.
- [ ] Prepaid tegoed klein houden (startwaarde ~$15) als harde cap.
- [ ] Nagaan of Phala per API-key een spend-limiet met maandelijkse reset biedt (oudere
      RedPill-documentatie noemt een limiet per key; huidige stand niet geverifieerd).
- [ ] Elke gegenereerde config valideren met `ha core check` vóór herstart.
- [ ] NEAR AI Cloud met GLM 5.3 Flash als handmatige fallback documenteren.

## Bronnen

- [Phala: Confidential AI](https://docs.phala.com/phala-cloud/confidential-ai/overview) (twee-hop RA-TLS)
- [Phala TEE-modelcatalogus](https://phala.com/models)
- [RedPill (Phala) modellen en prijzen](https://redpill.ai/models)
- [Private Inference API Cookbook](https://hackmd.io/@eLrG7p-xQKu7_FayKVbxYA/phala-private-inference-api-cookbook) (TLS-verificatie)
- Externe security-audit Venice/Phala-stack
- GLM 5.3 Flash op OpenRouter (benchmarks, tok/s per provider)
- DeepSeek V4.1 Flash op OpenRouter
- Phala als provider op OpenRouter
- NEAR AI Cloud private inference
- [teep: TEE-attestatieproxy](https://github.com/13rac1/teep)
