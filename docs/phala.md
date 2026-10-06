# Phala opzetten voor Hermes

Deze gids voert [ADR-001](adr/001-inference-provider.md) uit: Hermes gebruikt GLM 5.3 Flash
rechtstreeks bij Phala Confidential AI, op een attested endpoint, achter een lokale proxy
die de attestatie controleert.

| Instelling | Waarde |
|---|---|
| Base URL | `https://inference.phala.com/v1` |
| Model | `z-ai/glm-5.3-flash` |
| Key-variabele | `PHALA_API_KEY` |
| Verplicht in elk verzoek | `"provider": {"aci_verified": true, "zdr": true}` |
| Nooit gebruiken | `api.redpill.ai`, OpenRouter |

We kiezen `inference.phala.com` uit de twee toegestane domeinen, omdat Phala's eigen
documentatie en modelpagina dat domein gebruiken. `tee.redpill.ai` is hetzelfde
gateway (dezelfde attestatie) en werkt als alternatief.

De commando's hieronder zijn voor **Git Bash** op Windows (`curl`, `openssl` en `python`
zijn daar beschikbaar). Op martha werken ze ongewijzigd.

## Stap 1: account en tegoed

1. Maak een account op [cloud.phala.com](https://cloud.phala.com).
2. Private AI betaal je uit de **Balance van de workspace**. Dat is dezelfde Balance als voor
   CVM's en GPU's; er is geen apart tegoed en geen minimum.
   - Gebruik de workspace alleen voor Hermes, zodat de Balance echt de bovengrens is.
   - Waardeer op met **~$15** (startwaarde uit de ADR). Dat is ruim genoeg voor de bouwfase
     (~$16 per maand bij 10 taken per dag).
   - Zet automatisch opwaarderen uit als het dashboard dat aanbiedt. Anders is het geen harde cap.

## Stap 2: API-key

1. Ga in het dashboard naar **Private AI → API Keys** en maak een key, bijvoorbeeld
   `martha-hermes`.
2. **Spend-limiet per key:** kijk bij het aanmaken of er een limiet of maandbudget in te
   stellen is. De huidige Phala-documentatie noemt er geen. Zie je hem wel, stel dan ~$10 per
   maand in en noteer het in de ADR (maatregel 4).
3. Bewaar de key in je wachtwoordmanager. **Niet** in de repo, niet in een bestand op
   Windows. Op martha komt de key later via `install-agent.sh` (zie DESIGN.md, *Agent*).

## Stap 3: endpoint en model controleren (zonder key)

```bash
curl -s https://inference.phala.com/v1/models | grep -o '"id":"z-ai/glm-5.3-flash"'
curl -s "https://inference.phala.com/v1/models?zdr=true" | grep -o '"id":"z-ai/glm-5.3-flash"'
```

Beide moeten `"id":"z-ai/glm-5.3-flash"` tonen. De tweede bevestigt dat het model
*zero data retention* ondersteunt.

## Stap 4: eerste verzoek

Zet de key alleen in de shell, zonder dat hij in de history komt:

```bash
read -rsp "PHALA_API_KEY: " PHALA_API_KEY; echo; export PHALA_API_KEY
```

Stuur een verzoek met afgedwongen attestatie en zonder dataretentie:

```bash
curl -s -D headers.txt https://inference.phala.com/v1/chat/completions \
  -H "Authorization: Bearer $PHALA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-ai/glm-5.3-flash",
    "messages": [{"role": "user", "content": "Zeg alleen: hallo martha"}],
    "provider": {"aci_verified": true, "zdr": true}
  }'
grep -i '^x-receipt-id\|^x-aci-' headers.txt
```

Verwacht: een antwoord van het model en de headers `x-receipt-id`, `x-aci-identity` en
`x-aci-keyset-digest`.

`aci_verified: true` betekent dat de gateway het verzoek alleen doorstuurt naar een upstream
die hij binnen de TEE heeft geverifieerd. Is die er niet, dan krijg je **503 en wordt de
prompt niet verstuurd**; er is geen stille terugval naar plaintext. Dat is precies wat we
willen. Hermes stuurt dit veld zelf niet mee, dus de proxy moet het toevoegen (stap 6).

## Stap 5: handmatig verifiëren

Deze stap laat zien wat de proxy straks bij elke verbinding doet. Doe hem eenmalig met de
hand, zodat je weet dat het klopt.

### 5a. Attestatie, TLS-sleutel en keyset

```bash
NONCE=$(openssl rand -hex 32)
curl -s "https://inference.phala.com/v1/aci/attestation?nonce=$NONCE" > attestation.json

# SPKI-hash van het certificaat dat je nu echt van inference.phala.com krijgt
SPKI=$(openssl s_client -connect inference.phala.com:443 -servername inference.phala.com </dev/null 2>/dev/null \
  | openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256 -r | cut -d' ' -f1)

python - "$SPKI" attestation.json <<'EOF'
import hashlib, json, sys
spki, path = sys.argv[1], sys.argv[2]
d = json.load(open(path))
a = d["attestation"]
ks = a["workload_keyset"]
# JCS: sorted keys, no whitespace (sufficient here: the keyset has no floats)
jcs = json.dumps(ks, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
checks = {
    "api_version is aci/1": d["api_version"] == "aci/1",
    "tee_type is tdx": a["tee_type"] == "tdx",
    "keyset digest matches": d["workload_keyset_digest"] == "sha256:" + hashlib.sha256(jcs).hexdigest(),
    "live TLS key is in attestation": any(k["domain"] == "inference.phala.com" and k["spki_sha256"] == spki
                                          for k in ks["tls_public_keys"]),
    "downstream TLS binding matches": a["evidence"]["downstream_tls_binding"] == {
        "domain": "inference.phala.com", "spki_sha256": spki},
}
for name, ok in checks.items():
    print(("OK   " if ok else "FAIL ") + name)
print("gateway source:", a["source_provenance"]["repo_url"], a["source_provenance"]["repo_commit"])
sys.exit(0 if all(checks.values()) else 1)
EOF
```

Alle regels moeten `OK` zijn. De regel *live TLS key is in attestation* is de kern uit de
ADR: de TLS-verbinding die je opbouwt, eindigt in de attested gateway-CVM.

Vergelijk ook `x-aci-keyset-digest` uit `headers.txt` (stap 4) met `workload_keyset_digest`
in `attestation.json`. Die moeten gelijk zijn.

Wat dit script **niet** doet: de TDX-quote zelf controleren (handtekening via de certificaatketen
van Intel, de measurements, en de koppeling van `report_data` aan de nonce en de keyset).
Dat is werk voor de proxy of voor Phala's officiële verifier ([`@phala/aci-verifier`](https://docs.phala.com/phala-cloud/confidential-ai/verify/typescript-sdk.md)).

### 5b. Receipt van het verzoek

```bash
RID=$(grep -i '^x-receipt-id' headers.txt | cut -d' ' -f2 | tr -d '\r')
curl -s "https://inference.phala.com/v1/aci/receipts/$RID" \
  -H "Authorization: Bearer $PHALA_API_KEY" > receipt.json
```

Controleer in `receipt.json` dat het `upstream.verified`-event `"result": "verified"` heeft.
Een geldige handtekening alleen is niet genoeg: het gaat erom dat de gateway de upstream
(de model-CVM) heeft geverifieerd. De volledige set van acht controles (handtekening,
request- en response-hash, sessie) staat in het
[Private Inference API Cookbook](https://hackmd.io/@eLrG7p-xQKu7_FayKVbxYA/phala-private-inference-api-cookbook).

> Stap 5b is niet getest bij het schrijven van deze gids (er was geen key). Wijkt de
> structuur van de receipt af, pas deze gids dan aan.

Ruim daarna op: `rm headers.txt attestation.json receipt.json; unset PHALA_API_KEY`.

## Stap 6: attestatieproxy (open punt)

De ADR wil een lokale proxy, bijvoorbeeld [teep](https://github.com/13rac1/teep), tussen
Hermes en Phala. Die proxy verifieert de attestatie, weigert bij twijfel (fail-closed), voegt
`provider.aci_verified` en `zdr` toe en is het enige wat de key kent.

**teep is daar nu (stand 2026-10-06) nog niet geschikt voor:**

- teep's `phalacloud`-provider gebruikt standaard `https://api.redpill.ai`, het domein dat de
  ADR uitsluit (`internal/config/config.go`).
- Hij haalt de attestatie op via het oude pad `/v1/attestation/report`. `inference.phala.com`
  geeft daar het ACI/1-formaat terug, en het gateway-formaat is in teep's Phala-provider
  "not yet supported" (`internal/provider/phalacloud/phalacloud.go`).
- teep verifieert ACI/1 wel, maar alleen voor Venice (factor `aci_key_custody`). Venice
  draait op dezelfde Phala-stack, dus de code bestaat al. Alleen de Phala-provider gebruikt
  hem nog niet.

Dit is een architectuurkeuze en staat daarom als open punt in DESIGN.md. Mogelijke routes:

1. **teep uitbreiden** (upstream PR): de Phala-provider op `inference.phala.com` laten
   werken met de ACI/1-verificatie die er voor Venice al is.
2. **Eigen kleine proxy** op basis van [`@phala/aci-verifier`](https://docs.phala.com/phala-cloud/confidential-ai/verify/typescript-sdk.md)
   (Node). Die verifieert een verse attestatie met nonce en pint de TLS-verbinding op de
   attested SPKI. De proxy voegt alleen de key en het `provider`-blok toe.
3. **Tijdelijk zonder proxy**: alleen `aci_verified` plus periodiek stap 5. Dat is zwakker:
   niemand controleert dan per verbinding of TLS in de enclave eindigt.

Ook nog te beslissen: **waar** de proxy draait. Hermes zit in de OpenShell-sandbox en praat
met `inference.local`; OpenShell beheert de key. Draait de proxy op de host, dan moet de
inference-route van OpenShell naar de proxy wijzen en niet meer naar Phala, en houdt de
proxy de key.

## Stap 7: koppelen aan Hermes (later, agent-fase)

Pas zodra de proxy er is. Dan krijgt OpenShell als inference-endpoint de proxy (niet
`inference.phala.com`) en als model `z-ai/glm-5.3-flash`. Deze gids vullen we in die fase aan.

## Stap 8: in gebruik

- **Budget:** controleer de Balance wekelijks in de bouwfase. Is hij op, dan stopt Hermes
  met een fout; HA zelf merkt niets (C15).
- **Configs:** de ADR vraagt om `ha core check` vóór herstart. Die CLI bestaat niet bij
  HA Container. Hier doet de staging-HA dat werk (C17): een voorstel gaat pas live als
  staging de config heeft geladen.
- **Uitval:** er is geen automatische failover. NEAR AI Cloud (GLM 5.3 Flash) is de
  handmatige fallback uit de ADR; die moet nog worden uitgewerkt (maatregel 6).

## Stand van de maatregelen uit ADR-001

| Maatregel | Stand |
|---|---|
| Attestatie-verifiërende proxy | Open, zie stap 6 |
| Endpoint vastzetten | `inference.phala.com`, deze gids |
| Prepaid tegoed ~$15 | Stap 1 |
| Spend-limiet per key | Nagaan bij het aanmaken van de key, stap 2 |
| Config valideren voor herstart | Via staging (C17), stap 8 |
| NEAR AI-fallback documenteren | Open |
