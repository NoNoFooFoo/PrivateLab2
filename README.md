```markdown
# 🇮🇹 Italian Startup & Innovation Ecosystem Knowledge Graph

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/Playwright-Async-green.svg?logo=playwright)](https://playwright.dev/python/)
[![Groq AI](https://img.shields.io/badge/Groq%20AI-Llama%20%7C%20Qwen-purple.svg)](https://groq.com/)
[![ZenRows](https://img.shields.io/badge/Proxy-ZenRows%20CDP%20%2B%20Webshare-orange.svg)](https://www.zenrows.com/)
[![SQLite](https://img.shields.io/badge/Database-SQLite%20(WAL%20Mode)-blueviolet.svg?logo=sqlite)](https://www.sqlite.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Pipeline asincrona avanzata di **Web Scraping Stealth**, **Data Enrichment con LLM (Groq)** e costruzione di un **Grafo della Conoscenza (Knowledge Graph)** dell'ecosistema delle Startup e PMI Innovative italiane, basata sui dati ufficiali del **Registro delle Imprese (D.L. 179/2012)**.

---

## 📌 Indice dei Contenuti

- [Panoramica dell'Architettura](#-panoramica-dellarchitettura)
- [Caratteristiche Chiave](#-caratteristiche-chiave)
- [Modello dei Dati & Knowledge Graph](#-modello-dei-dati--knowledge-graph)
- [Stack Tecnologico](#-stack-tecnologico)
- [Struttura del Repository](#-struttura-del-repository)
- [Installazione e Setup](#-installazione-e-setup)
- [Configurazione (.env)](#-configurazione-env)
- [Guida all'Uso](#-guida-alluso)
- [Esportazione e Compatibilità Google Sheets](#-esportazione-e-compatibilità-google-sheets)
- [Roadmap](#-roadmap)

---

## 🏗 Panoramica dell'Architettura

Il sistema si articola su una **pipeline a due stadi disaccoppiati**, coordinata da un orchestratore centrale:

```
[ Registro Imprese CCIAA ]
           │
           ▼  (WAF Bypass / ZenRows Residential Proxy)
┌──────────────────────┐
│  Fase 1: HARVESTER   │ ──> Popola la tabella 'coda' (Stato: PENDING)
└──────────────────────┘
           │
           ▼
┌──────────────────────┐
│   Fase 2: WORKER     │ <── Lettura in-memory dell'XML camerale
└──────────┬───────────┘
           │
           ├─► Upload Logo Base64 su ImgBB (CDN Permanente)
           ├─► Interrogazione Unica Groq AI (Entity Extraction, Normalizzazione, Score)
           ├─► Deduplicazione Anagrafica a Vocaboli (Anti-Doppioni)
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│      KNOWLEDGE GRAPH RELAZIONALE (database.db in SQLite)     │
│  Tabelle Master + Satelliti + Nodi Condivisi + 'relazioni'   │
└──────────────────────────────────────────────────────────────┘
```

---

## ⚡ Caratteristiche Chiave

### 1. Acquisizione XML In-Memory Istantanea
- Neutralizza i problemi di salvataggio su disco remoto dei browser WebSocket/CDP (ZenRows).
- Esegue un `fetch` asincrono in-session direttamente nel contesto del browser con i cookie camerali già attivi, acquisendo l'XML ministeriale in meno di 200 ms.

### 2. Risoluzione delle 12 Anomalie del Tracciato XML Camerale
- **Canalizzazione Università (`TA2`) vs Associazioni (`TA1`)**: smista correttamente gli atenei e i centri di ricerca (`codice TA2`) nella tabella `universita` e le associazioni di categoria (`TA1`) in `associazioni`.
- **Tolleranza a Tag Vuoti/Mancanti**: gestione bilingue (`<it>` / `<en>`), fallback sul pitch per prodotti non descritti esplicitamente e gestione di startup senza blocco `<contatti>`.
- **Finanziamenti Multi-Round Flessibili**: supporto a round multipli (debito, equity, venture), gestione di consorzi con 3+ investitori e supporto a round riservati privi del tag `<investitori>`.

### 3. Baricentro Relazionale: Partita IVA `IT...`
- Ogni Partita IVA / Codice Fiscale viene categoricamente normalizzato con il prefisso **`IT`** (es. `IT01234567890`), evitando che Excel o SQLite troncino lo zero iniziale.
- Tutte le tabelle figlie e ogni arco del grafo archiviano il codice `IT...` come **Foreign Key univoca**, garantendo la totale assenza di ambiguità e una cancellazione idempotente in caso di ri-estrazione.

### 4. Groq AI Engine Monoprocesso & Dynamic Model Discovery
- **Una Sola Chiamata per Azienda**: risolve il vincolo del free tier (RPM/TPM) inviando un payload compatto ad alta densità (riduzione token dell'85%).
- **Discovery Dinamica dei Modelli via API**: interroga `/v1/models` per identificare i modelli disponibili e scendere in fallback automatico in caso di rate-limit (`HTTP 429`), senza dipendere da modelli hardcodati.
- **Zero Allucinazioni su Persone e Qualifiche**: gli slot ministeriali dell'XML rimangono rigorosamente anonimi (`persona_nome = NULL`, `stato = 'ANONIMO'`) a meno di una prova testuale inconfutabile nella visura.

### 5. Resilienza ZenRows a 180 Secondi
- Il worker intercetta la chiusura forzata della connessione CDP (limite di 3 minuti delle sessioni residenziali remote), ripristina la sessione WebSocket in 500 ms e riprende l'elaborazione del batch senza interrompere il processo.

### 6. Gestione Loghi su ImgBB CDN
- Converte la stringa `<logo><b64>` in un URL pubblico permanente (`https://i.ibb.co/.../logo.png`).
- Protegge il database dal gonfiamento e **risolve il limite di 50.000 caratteri per cella di Google Sheets**, consentendo l'uso nativo della formula `=IMAGE(logo_url)`.

---

## 📊 Modello dei Dati & Knowledge Graph

Il database SQLite (`database.db`) è strutturato come un vero e proprio **Property Graph relazionale**:

```
                       ┌──────────────┐
                       │ INVESTITORI  │
                       └──────┬───────┘
                              │ [HA_INVESTITO_IN] (peso: ammontare €)
                              ▼
┌──────────────┐      ┌───────────────┐      ┌─────────────┐
│  UNIVERSITA  │ ───► │    STARTUP    │ ◄─── │ INCUBATORI  │
└──────────────┘      │ (Anchor: IT…) │      └─────────────┘
 [PARTNER_ACCADEMICO] └───────┬───────┘      [ACCELERATA_DA]
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
    ┌──────────┐        ┌───────────┐       ┌───────────┐
    │ PERSONE  │        │ PRODOTTI  │       │ BREVETTI  │
    └──────────┘        └───────────┘       └───────────┘
   [MEMBRO_TEAM]       (1:N via CF)        (1:N via CF)
```

### Tabelle Principali
* **`startup`**: Tabella master con oltre 85 colonne anagrafiche, dimensionali, canali di vendita, stadi di sviluppo, punteggi AI (`team_groq_score`) e il tracciato `xml_raw` originale.
* **`relazioni`**: Tabella degli archi del grafo (`sorgente`, `sorgente_tipo`, `destinazione`, `destinazione_tipo`, `tipo`, `categoria`, `peso`, `metadati`).
* **`persone`** vs **`qualifiche`**: Disaccoppia l'anagrafica reale delle persone fisiche (Nome e Cognome) dagli slot di qualifica formali depositati nell'XML (`LTS6` per Dottorato di Ricerca, fasce d'età `FE`).
* **Nodi Condivisi (`investitori`, `universita`, `enti`, `incubatori`, `associazioni`)**: Nodi unici di rete che aggregano più startup nel proprio portafoglio o ecosistema.

---

## 🛠 Stack Tecnologico

* **Linguaggio**: Python 3.10+ (Asincrono con `asyncio`)
* **Browser Automation**: Playwright (Async API)
* **WAF Bypass & Proxies**: ZenRows (Browser Residenziale via CDP over WebSocket) + Webshare API v2
* **Intelligenza Artificiale**: Groq API (SDK/REST, modelli Llama 3.3 / Qwen)
* **Image Hosting**: ImgBB REST API
* **Database**: SQLite3 con `PRAGMA journal_mode = WAL`
* **Data Processing & Export**: Pandas, OpenPyXL, ElementTree

---

## 📁 Struttura del Repository

```text
├── data/
│   ├── database.db             # Database SQLite principale (WAL mode)
│   └── ...
├── output/
│   ├── startup_estratte.csv    # Export tabellare CSV (separatore ';', UTF-8 BOM)
│   └── startup_estratte.xlsx   # Export Microsoft Excel pronto per Google Sheets
├── scrapers/
│   ├── harvester.py            # Fase 1: Scansione stealth ed estrazione link
│   ├── worker.py               # Fase 2: Deep extraction, Groq AI e Knowledge Graph
│   ├── groq_ai.py              # Modulo LLM con prompt engineering e dynamic discovery
│   └── proxy_manager.py        # Gestione proxy Webshare e route ZenRows
├── startup.py                  # Orchestratore master (CLI sia per Fase 1 che per Fase 2)
├── requirements.txt            # Dipendenze Python
├── .env.example                # Template per le chiavi d'ambiente
└── README.md
```

---

## 🚀 Installazione e Setup

### 1. Clonazione del repository
```bash
git clone https://github.com/tuo-username/tuo-repo-startup.git
cd tuo-repo-startup
```

### 2. Configurazione dell'ambiente virtuale
```bash
python3 -m venv venv
source venv/bin/activate  # Su Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

---

## 🔑 Configurazione (.env)

Copia il file di esempio e configura le tue credenziali nel file `.env`:

```ini
# Database Path
DB_PATH=data/database.db

# Groq Cloud API Key
GROQ_API_KEY=gsk_...

# ZenRows API Key (per connessioni CDP e bypass WAF)
ZENROWS_API_KEY=...

# ImgBB API Key (per hosting loghi CDN)
IMGBB_API_KEY=...

# Webshare Proxy Token (opzionale se si usa ZenRows)
WEBSHARE_API_KEY=...
```

---

## 💻 Guida all'Uso

### Pipeline Completa (Orchestratore Master)
Esegue automaticamente sia la Fase 1 (Harvesting dei link) che la Fase 2 (Deep Worker):

```bash
# Esegui scansione per una specifica regione (es. 8 = Lombardia, target 20 startup)
python startup.py --action both --regione 8 --limit 20 --delay 2.5 --proxy zenrows

# Esegui scansione su tutte le regioni italiane
python startup.py --action both --regione ALL --limit 50 --delay 3.0 --proxy zenrows
```

### Esecuzione Modulare

#### 1. Solo Harvester (Fase 1)
Estrae le schede camerali e popola la tabella `coda` con stato `PENDING`:
```bash
python scrapers/harvester.py --regione 8 --limit 10 --delay 2.0 --proxy zenrows --tipo STARTUP
```

#### 2. Solo Deep Worker (Fase 2)
Elabora la coda `PENDING`, estrae l'XML in memoria, chiama Groq AI e costruisce il grafo:
```bash
python scrapers/worker.py --limit 10 --delay 2.0 --proxy zenrows --tipo STARTUP
```

---

## 📈 Esportazione e Compatibilità Google Sheets

Al termine di ogni esecuzione del worker, i dati vengono automaticamente salvati nella cartella `output/` sia in formato `.csv` (codifica `utf-8-sig` compatibile con Excel italiano) sia in formato `.xlsx`.

### Visualizzazione dei Loghi in Google Sheets
Grazie all'integrazione di ImgBB, è possibile visualizzare l'anteprima grafica del logo direttamente all'interno delle celle di Google Sheets inserendo la formula:

```text
=IMAGE(V2)
```
*(dove `V2` è la cella contenente il valore della colonna `logo_url`)*

---

## 🗺 Roadmap

- [x] Neutralizzazione F5 BIG-IP WAF e sessioni Wicket.
- [x] Lettura asincrona dell'XML camerale in-session via JavaScript context.
- [x] Integrazione API ImgBB per l'hosting dei loghi in CDN.
- [x] Named Entity Recognition con Groq AI a chiamata singola.
- [x] Costruzione del Knowledge Graph su SQLite (Nodi + Archi pesati).
- [x] Deduplicazione anagrafica a vocaboli e risoluzione dello zero iniziale con prefisso `IT`.
- [ ] Estensione dell'estrazione avanzata a 85 colonne alla categoria **PMI Innovative** *(Work in progress)*.
- [ ] Frontend Dashboard interattivo con visualizzazione del grafo tramite Cytoscape.js / D3.js.

---

## 📄 Licenza

Distribuito sotto licenza **MIT**. Consulta il file `LICENSE` per ulteriori dettagli.
```
