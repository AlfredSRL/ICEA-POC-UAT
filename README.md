# ICEA-POC-UAT

Client UAT per la API privata di estrazione del repository server `ICEA-POC`. Il server restituisce un'estrazione canonica per chiamata. Questo client conserva in locale PDF, ground truth, risposte, scoring e report.

Il client non chiama mai Azure direttamente: usa soltanto `POST /v1/extractions` e `GET /openapi.json` con Bearer token. La API è alla versione `api_version: "2.0"`: ogni richiesta invia `file`, `profile` ed `extraction_option` (`A`, `B`, `C` o `D`). Le opzioni sono codici opachi: la corrispondenza con modello e strategia è configurazione riservata del server e non compare né nel client né nei report.

## Confini

- Campioni UAT e workbook GT sono forniti da ICEA, già compilati e approvati. Sono dati di ingresso: il client non li modifica né li ricostruisce.
- ICEA ha autorizzato il trattamento di questi dati e tutte le invocazioni necessarie. I costi delle chiamate sono gestiti dal team.
- `data/uat/` e `runtime/` sono ignorati da Git. Non fare commit di PDF, GT compilata, risposte API, Excel di confronto, `.env` o prompt con valori.
- I test versionati usano solo fixture sintetiche generate al volo. Nessun dato DEV o UAT è nel repository.
- Nessuna chiamata di rete senza `--execute` e conferma del numero di chiamate pianificate. Nessun retry automatico.

## Struttura

| Percorso | Contenuto |
|---|---|
| `src/icea_client/` | CLI `preflight`, `run`, `compare`, `all`, `check-api` |
| `src/icea_client/canonical/` | Copie **non modificate** dei moduli server (contratto, schema, normalizzazione, scoring, allineamento righe) con `MANIFEST.json` degli hash SHA-256 |
| `src/icea_client/layouts/default.json` | Mappatura celle del workbook GT (default: struttura GT UAT v1.8, fogli `UAT-NN`) |
| `data/uat/samples/` | PDF consegnati da ICEA (sola lettura nel container) |
| `data/uat/ground_truth/` | Workbook GT approvato da ICEA ed eventuale layout personalizzato (sola lettura) |
| `runtime/` | Risposte API, preflight, report ed Excel (scrittura) |

## Configurazione (Windows PowerShell)

```powershell
Copy-Item .env.example .env      # poi imposta ICEA_API_URL e ICEA_API_TOKEN
.\scripts\uat.ps1 -Command build
.\scripts\uat.ps1 -Command test  # test offline, container senza rete
```

Il client non dipende dal tipo di connessione: tra uso locale e remoto cambia solo `ICEA_API_URL` in `.env`, senza modifiche al codice né nuovi test.

| Server | `ICEA_API_URL` |
|---|---|
| Container locale sulla rete Docker condivisa | `http://icea-uat-api:8000` |
| Server POC tramite tunnel SSH su questa macchina | `http://host.docker.internal:8083` |

Il container client è collegato alla rete Docker `icea-api` (configurabile con `ICEA_API_DOCKER_NETWORK`). `scripts/uat.ps1` la crea se manca; senza wrapper serve una volta `docker network create icea-api`. Un container API locale collegato alla stessa rete si raggiunge per nome. Il server POC espone la API solo su `127.0.0.1:8083` del server: apri prima il tunnel SSH autorizzato sull'host con `ssh -N -L 8083:127.0.0.1:8083 <utente>@<host-server>`. Il client gira in Docker, quindi raggiunge il `127.0.0.1` dell'host tramite `host.docker.internal`. Non esporre la porta `8083` sulla rete.

Con `http` verso un host diverso da loopback, il client stampa un avviso: il token viaggia in chiaro. Va bene sulla rete Docker interna e nel tunnel SSH (`host.docker.internal` conta come loopback). Il token non compare in log, record o report. Il client non segue redirect, per non inoltrare il token ad altri host. Verifica la connessione con `check-api` prima di una run.

## Comandi

Il workbook GT si indica per nome di file, dentro `data/uat/ground_truth/`. Il nome è libero.

```powershell
# 1. Controlli offline su PDF e GT (tutti i campioni o una selezione)
.\scripts\uat.ps1 -Command preflight -GroundTruth UAT_ICEA_v1.8.xlsx

# 2. Piano e budget, senza rete (dry-run predefinito)
.\scripts\uat.ps1 -Command run -GroundTruth UAT_ICEA_v1.8.xlsx -Samples UAT-01,UAT-02

# 3. Chiamate reali, confermando il numero di chiamate mostrato al punto 2
.\scripts\uat.ps1 -Command run -GroundTruth UAT_ICEA_v1.8.xlsx -Samples UAT-01 -Execute -ApproveCalls 4

# 4. Confronto offline, report JSON/Markdown ed Excel
.\scripts\uat.ps1 -Command compare -GroundTruth UAT_ICEA_v1.8.xlsx

# Schema della API (una GET autenticata, nessun PDF, nessuna chiamata Azure)
.\scripts\uat.ps1 -Command check-api
```

`all` esegue preflight, run e compare in sequenza. Anche `all` resta in dry-run senza `-Execute`. Senza wrapper: `docker compose run --rm client <comando> --ground-truth ... --samples ...`.

### `preflight`

Verifica presenza univoca del PDF in `data/uat/samples/`, SHA-256, numero di pagine, profilo e limite di 20 MiB della API. Sulla GT applica gli stessi controlli dello scorer server: percorsi del contratto, classe, regola, stato, valore atteso, gruppo, pagina. Segnala anche ID riga duplicati. La console mostra conteggi e codici, mai valori. Il report completo va in `runtime/preflight/`.

| Codice | Effetto |
|---|---|
| `pdf_missing` / `pdf_not_unique` | Blocca il campione; le righe GT vengono comunque controllate con hash e pagine registrati. |
| `sha256_mismatch`, `page_count_mismatch`, `profile_mismatch` | Bloccano il campione. |
| `legacy_profile_rejected_by_api` | Profili storici `DISPOSIZIONE-CONTO-LAVORO` ed `ELENCO-TERZISTI`: la API risponde `422`, quindi il client blocca prima. |
| `unknown_profile` | Profilo non canonico: blocca. |
| `unknown_field_path`, `class_mismatch`, `rule_mismatch`, `missing_group`, `missing_expected_value`, … | Anomalie GT: righe non valutabili, nessuna correzione. |
| `absent_with_value` | Riga `Assente` con un valore: esclusa dallo score. |
| `Ambiguo` (`explicit_ambiguities`) | Conteggiate a parte, escluse dallo score come `ambiguous_in_ground_truth`; non bloccano. |

La colonna di validazione è solo informativa: l'approvazione ICEA è già acquisita. Senza `-Samples`, i campioni con un profilo non accettato dalla API sono esclusi ed elencati; se richiesti in modo esplicito, bloccano.
### `run`

- Richiede una lista esplicita di sample ID. Non esiste un'opzione per tutti i campioni.
- Per campione esegue fino a quattro chiamate, una per opzione di estrazione `A`, `B`, `C`, `D` (`-Methods` per un sottoinsieme). Una richiesta alla volta.
- **Budget per campione: 4 chiamate API.** Il consumo interno (modello, Document Intelligence) è noto solo al server.
- Senza `-Execute` mostra solo il piano. Con `-Execute`, `-ApproveCalls` deve coincidere con le chiamate pianificate, altrimenti la run è rifiutata.
- Una combinazione già completata con lo stesso hash PDF, profilo, opzione, versione API e URL API viene saltata. `-Force` la ripete in modo esplicito. Le combinazioni fallite restano richiamabili. Le run salvate con la API `1.0` non sono più usate né dal piano né da `compare`.
- Ogni chiamata salva in `runtime/runs/<sample>/<metodo>/<timestamp>/`:
  - `response.json`: status HTTP e corpo JSON completo, anche per gli errori;
  - `record.json`: hash PDF, profilo, opzione di estrazione, versione API, run ID API, tempo pipeline (dalla API), tempo HTTP end-to-end, errore e timestamp.
- I dettagli tecnici (modello, strategia, prompt, token, tempi interni) restano solo negli artefatti server `runtime/api/runs/<run_id>/`: per un'indagine basta il run ID API.
- `429 busy`, `502`, timeout, errori di rete, `api_version` diversa da `2.0` e risposte con identità incoerente (hash, profilo, filename) sono registrati come fallimenti. Non vengono ritentati né sostituiti da valori vuoti.

### `compare`

Lavora solo offline sulle risposte salvate e sulla GT locale. Per ogni campione e metodo usa l'ultima chiamata definitiva con hash PDF e profilo della GT: una risposta riuscita o un fallimento della run (per esempio schema non valido, `502`). Un fallimento resta il fallimento dell'intera run: non viene sostituito da una risposta più vecchia. I fallimenti transitori (`429 busy`, timeout, rete) sono usati solo se non c'è altro. Le risposte archiviate sono rivalidate offline con lo schema canonico corrente, senza riscriverle. Scoring e allineamento delle righe sono quelli del server, senza modifiche:

- gruppi e liste annidate;
- assenze attese come match quando il modello non emette il campo;
- booleani `true`/`false` e normalizzazioni tipizzate;
- esiti `matched`, `partial`, `incorrect`, `omitted`, `unresolved_group` e GT non valutabile; le collisioni di cardinalità e le liste GT seguono le regole della sezione Profili;
- corrispondenze esatte riservate prima sull'intero vettore, così un'occorrenza extra non fa slittare le altre;
- gruppi con contenuto identico abbinati in ordine di documento; gruppi diversi in pareggio restano `unresolved_group`;
- `partial` esteso (frammento significativo, parola o codice in comune, caratteri simili da OCR, un carattere errato), mai per date, numeri e booleani;
- per `FATTURA-VENDITA` e `DDT-VENDITA`, `net_weight`, `gross_weight` e `measurement_value` della stessa riga sono intercambiabili: il valore corretto in un campo alternativo è `valore alternativo accettato` e conta come match;
- per Paese e provincia RMDF valgono sia il valore stampato sia la forma normalizzata approvata;
- numeri GT letti con le 15 cifre significative mostrate da Excel; date `gg/mm/aa` lette come `20aa`;
- righe `Ambiguo` e `Assente` con valore escluse dallo score;
- regole client (`src/icea_client/client_rules.py`), applicate a copie in memoria di GT e risposte: un campo booleano del profilo che il modello non emette affatto vale `false`, se la GT lo valuta (`ambiguous` e `illegible` emessi restano tali); su ogni altro campo il valore `false` significa campo vuoto, quindi una riga GT `Presente` con `false` è un'assenza attesa (nessuna estrazione o `not_present` = match) e un'occorrenza del modello con valore `false` vale `not_present` (esempio: `tc.buyer.buying_on_behalf_of`). Nell'Excel i booleani sono scritti `true` e `false`.

Ogni report registra l'hash della GT, il commit e gli SHA-256 dei moduli canonici, l'SHA-256 dei sorgenti client e il run ID API di ogni risposta. La API v2 non espone più l'hash del prompt: l'equivalenza della configurazione di un'opzione va verificata sugli artefatti server.

Lo schema canonico vendorizzato richiede ancora `strategy`; il client valida l'estrazione v2 con una sottoclasse che la vieta (`PublicExtraction` in `runner.py`), senza toccare i moduli copiati. Il costruttore delle righe vendorizzato usa le proprie quattro chiavi metodo solo come posizioni, rinominate subito in `A`–`D`.

Output in `runtime/reports/`: `field_comparison_data.json`, `comparison_summary.json`, `comparison_summary.md`, `Confronto_estrazioni_UAT.xlsx`. Prima di sovrascriverli, i report precedenti vengono archiviati in `runtime/reports/history/`. `ground_truth_history.jsonl` registra l'hash della GT di ogni confronto. Se la GT cambia, basta ripetere `compare`: le risposte storiche non vengono riscritte.

L'Excel contiene:

- foglio `Sintesi`: conteggi su tutte le righe valutabili e per profilo; tempi medi e mediani di pipeline e HTTP; grafico a colonne in pila Match % + Partial % con asse 0–100%; barre Match % per profilo con valori; dispersione match vs tempo pipeline medio;
- `Errori per campo`: errori per profilo, campo e soluzione (partial, incorrect, omitted, gruppi irrisolti, campi invalidi), ordinati per totale;
- `Errori`: una riga per occorrenza con almeno una soluzione non in match pieno, con esito, raw, normalizzato ed evidenza per soluzione;
- righe `Ambiguo`, righe non valutabili e run fallite (schema non valido, errore API) sono escluse dalle metriche di errore; il valore alternativo accettato è mostrato come match;
- un foglio per campione: filename, run ID, hash, tempi, righe GT e occorrenze extra, regola, valori GT raw e normalizzati, quattro valori modello, quattro esiti colorati, dettagli (raw/pagina/gruppo/indice) e anomalie GT.

Dopo la scrittura, il workbook viene verificato in modo strutturale: nomi e ordine dei fogli, righe dei fogli errori, tre grafici e loro assi, percentuali della sintesi, tempi e celle GT numeriche tipizzate. I testi che iniziano con `=` restano testo, mai formule.

## Layout del workbook GT

Il layout predefinito replica la struttura della GT UAT v1.8:

- metadati cercati per etichetta nelle righe 1–9: `Profilo`, `Pagine`, filename, `SHA-256` (anche la testata su una sola riga delle schede `SELEZIONE-TERZISTI`: `File` in `L4`, SHA-256 in `N4`);
- righe dati dalla riga 10;
- colonne: `A` ID riga, `C` field_path, `D` classe, `F` pagina, `G` group_id, `H` raw, `I` normalizzato, `J` stato, `K` regola, `M` validazione;
- fogli campione con nome `UAT-NN`.

Se il workbook ICEA differisce, copia `src/icea_client/layouts/default.json` in `data/uat/ground_truth/` e adatta il pattern dei fogli, le etichette o le celle (`{"cell": "D4"}`) e le colonne. Poi passa `-Layout nome.json`. Se un metadato non viene trovato, il parser si ferma con un errore e non tenta di indovinare. Il layout mappa solo le posizioni: i valori attesi non vengono mai modificati.

## Allineamento con il server

`src/icea_client/canonical/` contiene copie byte per byte di `contracts.py`, `schemas.py`, `normalization.py`, `evaluation.py`, `ground_truth.py`, `metrics.py` ed `export_field_comparison.py` del server. `MANIFEST.json` riporta commit e hash. Preflight e test falliscono se una copia diverge dal manifest. Per aggiornarle:

```powershell
.\scripts\sync_canonical.ps1 -ServerRepo C:\Users\stefa\Desktop\ICEA\codes\ICEA-POC -Commit <hash>
.\scripts\uat.ps1 -Command test
```

La copia attuale proviene dal commit `f7b795a` del branch `deploy`, letto con `git show` (vedi `MANIFEST.json`). Gli eventuali adattamenti del client stanno nei moduli di `src/icea_client/`, mai nelle copie.

## Profili

La API accetta solo i sei codici canonici: `TC`, `FATTURA-VENDITA`, `DDT-VENDITA`, `FATTURA-ACQUISTO-RMDF` (14 campi), `DDT-ACQUISTO-RMDF` (15 campi), `SELEZIONE-TERZISTI` (15 campi, prefisso `subcontractor_selection.`). I codici storici `DISPOSIZIONE-CONTO-LAVORO` ed `ELENCO-TERZISTI` ricevono `422`.

`transport_document.handwritten_order_or_lot` è stato eliminato dal contratto (`REMOVED_FIELD_PATHS`, server `a29f324`): la API rifiuta le risposte che lo contengono (`502 schema`), il preflight segnala le righe GT con quel percorso come `unknown_field_path`, e `compare` scarta in memoria le sue occorrenze nelle risposte archiviate registrandole in `removed_paths`, senza riscriverle. L'unico campo lotto resta `transport_document.lines[].lot_numbers[]`, con un lotto per riga GT.

Per `SELEZIONE-TERZISTI` esistono solo `quantity_input_kg` e `quantity_output_kg`; `quantity_kg` è un percorso sconosciuto. Una quantità senza direzione è riportata identica su entrambi i campi (stessi pagina, gruppo ed evidenza); con direzione esplicita il valore copiato nell'altro campo è un errore. Le cardinalità `0..1 per riga`, `0..1 per voce` e `0..1 per lotto/riga` ammettono un valore per gruppo. Più valori del modello nello stesso gruppo (collisione) restano isolati in `field_issues` dalla API; nello score sono `partial` se almeno un valore coincide anche in parte con la GT, altrimenti `incorrect` (non più `invalid_field`).

Una cella GT può elencare più valori come `'a'; 'b'`. Excel nasconde l'apostrofo iniziale, quindi il testo letto è `a'; 'b'`: lo scorer canonico lo riconosce. Stessi valori e stesso numero: `matched`; almeno un valore in comune o parziale: `partial`; nessuno: `incorrect`. Il separatore deve essere esattamente apostrofo, punto e virgola, spazio, apostrofo; un apostrofo dentro un valore va raddoppiato.

## Prima delle chiamate reali

1. `preflight` senza blocchi sui campioni selezionati;
2. container API in esecuzione e raggiungibile (`check-api`), oppure URL remoto `https`;
3. per ripetere campioni già eseguiti con una nuova versione del server, usare `-Force`: senza, le combinazioni già completate vengono saltate.