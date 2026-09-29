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

Il repository contiene una dashboard web e una pipeline CLI. La dashboard usa il backend FastAPI per leggere SQLite e avviare l'orchestratore come processo separato. La pipeline può lavorare in due fasi (`harvest` e `worker`) oppure elaborare i codici fiscali estratti dall'archivio ministeriale (`piva`):

```
┌────────────────────┐     HTTP / SSE      ┌───────────────────────┐
│ Dashboard HTML/CSS │ ◄─────────────────► │ FastAPI: app.py       │
│ JavaScript vanilla │                     │ API, SQLite, controlli│
└────────────────────┘                     └───────────┬───────────┘
                                                       │ subprocess
                                                       ▼
                                           ┌───────────────────────┐
                                           │ scrapers/startup.py   │
                                           │ harvest / worker /    │
                                           │ both / piva           │
                                           └───────────┬───────────┘
                                                       │
                        ┌──────────────────────────────┴────────────────────────┐
                        ▼                                                       ▼
             ┌─────────────────────┐                               ┌─────────────────────┐
             │ Harvester: ricerca  │                               │ Worker: XML, Groq,  │
             │ schede e coda       │                               │ SQLite ed export    │
             └──────────┬──────────┘                               └──────────┬──────────┘
                        └──────────────────────────┬──────────────────────────┘
                                                   ▼
                                     SQLite (data/database.db) + output/
```

La modalità `piva` parte dai codici fiscali contenuti nello ZIP ministeriale; le modalità `harvest`, `worker` e `both` consentono invece di usare direttamente la ricerca regionale e la coda.

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

* **Linguaggio**: Python 3.10+ (asincrono con `asyncio`)
* **Backend**: FastAPI e Uvicorn
* **Interfaccia**: HTML/CSS e JavaScript vanilla; Apache ECharts e Lucide via CDN
* **Browser Automation**: Playwright (Async API)
* **WAF Bypass & Proxies**: ZenRows (Browser Residenziale via CDP over WebSocket) + Webshare API v2
* **Intelligenza Artificiale**: Groq API (SDK/REST, modelli Llama 3.3 / Qwen)
* **Image Hosting**: ImgBB REST API
* **Database**: SQLite3 con `PRAGMA journal_mode = WAL`
* **Data Processing & Export**: Pandas, OpenPyXL, ElementTree

---

## 📁 Struttura del Repository

```text
├── app.py                      # Backend FastAPI e API della dashboard
├── index.html                  # Dashboard e logica JavaScript
├── style.css                   # Stili della dashboard
├── data/
│   ├── database.db             # Creato/aggiornato all'avvio (percorso predefinito)
│   └── proxies.txt             # Cache locale dei proxy
├── output/
│   ├── startup_estratte.csv    # Export startup
│   ├── startup_estratte.xlsx   # Export startup
│   ├── pmi_estratte.csv        # Export PMI, se presenti
│   └── pmi_estratte.xlsx       # Export PMI, se presenti
├── scrapers/
│   ├── startup.py              # Orchestratore CLI: harvest, worker, both, piva
│   ├── harvester.py            # Ricerca delle schede e popolamento della coda
│   ├── worker.py               # Estrazione XML, arricchimento e persistenza
│   ├── groq_ai.py              # Integrazione Groq
│   ├── proxy_manager.py        # Gestione proxy
│   ├── ocr_service.py          # Servizio OCR opzionale
│   └── elenco_startup_ministero/ # Archivio ministeriale usato dalla modalità piva
├── requirements.txt            # Dipendenze Python
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

### Avvio della dashboard
Avvia il backend dalla root del repository:

```bash
python app.py
```

La dashboard è disponibile su `http://localhost:8000`. In alternativa, con Uvicorn:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

---

## 🔑 Configurazione (.env)

Configura le variabili in un file `.env` nella root del progetto. In assenza di `.env`, il codice cerca anche `env.taziovettori`. `DB_PATH` può essere assoluto o relativo alla root del repository; il valore predefinito è `data/database.db`.

```ini
# Database Path
DB_PATH=data/database.db

# Groq Cloud API Key
GROQ_API_KEY=gsk_...

# ZenRows API Key (per connessioni CDP e bypass WAF)
ZENROWS_API_KEY=...
ZENROWS_API_KEY_2=... # Fallback se la prima chiave non riesce a connettersi

# ImgBB API Key (per hosting loghi CDN)
IMGBB_API_KEY=...

# Webshare Proxy Token (opzionale)
WEBSHARE_API_KEY=...

# URL webhook Google Apps Script (opzionale, per la sincronizzazione Sheets)
GS_WEBHOOK_URL=...
```

Groq e ImgBB servono rispettivamente per l'arricchimento AI e l'hosting dei loghi; Webshare è un proxy alternativo. ZenRows prova `ZENROWS_API_KEY` e, se la connessione fallisce, passa a `ZENROWS_API_KEY_2`. La funzione Play della dashboard richiede almeno una delle due chiavi e l'archivio ministeriale nel percorso `scrapers/elenco_startup_ministero/startup (1).zip` (oppure il percorso passato con `--piva-zip` da CLI).

---

## 💻 Guida all'Uso

### Dashboard
La dashboard avvia la modalità `piva`: legge i CF dallo ZIP ministeriale, cerca ciascuna scheda con l'harvester, la elabora con il worker e verifica il risultato. I controlli Play/Pause/Stop agiscono sul processo avviato dal backend.

### Pipeline CLI
Esegui i comandi dalla root del repository. L'orchestratore si trova in `scrapers/startup.py`:

```bash
# Pipeline completa: ricerca regionale seguita dall'elaborazione della coda
python scrapers/startup.py --action both --regione 8 --limit 20 --delay 2.5 --proxy zenrows

# Pipeline basata sui CF nell'archivio ministeriale
python scrapers/startup.py --action piva --limit 20 --delay 2.5 --proxy zenrows

# Tutte le regioni (per la modalità harvest/both)
python scrapers/startup.py --action both --regione ALL --limit 50 --delay 3.0 --proxy zenrows
```

Le azioni CLI disponibili sono `harvest`, `worker`, `both` e `piva`; i parametri principali sono `--regione`, `--limit`, `--delay`, `--proxy`, `--tipo` e `--piva-zip`. Usare `python scrapers/startup.py --help` per i valori ammessi.

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

Al termine dell'elaborazione, il worker esporta le tabelle master startup e PMI (quando presenti) in `output/`, in formato CSV `utf-8-sig` e XLSX. La dashboard offre inoltre export del database in Excel e CSV e un endpoint di sincronizzazione Google Sheets configurabile con `GS_WEBHOOK_URL`.

### Visualizzazione dei Loghi in Google Sheets
Quando è disponibile `logo_url`, è possibile mostrare il logo in una cella di Google Sheets con:

```text
=IMAGE(V2)
```
*(dove `V2` è la cella contenente il valore della colonna `logo_url`)*

---

## 🗺 Stato e limiti noti

- La dashboard e la visualizzazione del grafo sono già presenti; il grafo usa Apache ECharts.
- Il backend crea/aggiorna lo schema SQLite all'avvio e usa il database configurato da `DB_PATH`.
- L'interfaccia Play è vincolata alla modalità P.IVA e al relativo ZIP ministeriale; la scansione per regione è disponibile via CLI.
- Il controllo del processo e lo storico dei log sono in memoria nel backend: eseguire una sola istanza per mantenere coerenti stato e controlli.
- Le API non implementano autenticazione e il server di sviluppo ascolta su tutte le interfacce. Non esporre la dashboard a reti non fidate senza aggiungere autenticazione, limitare CORS e configurare un deployment appropriato.

---

## 📄 Licenza

Distribuito sotto licenza **MIT**. Consulta il file `LICENSE` per ulteriori dettagli.
```
