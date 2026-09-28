import asyncio
from datetime import datetime
import json
import os
import re
import signal
import sqlite3
import sys
import time
import traceback
from typing import Optional, Set
import urllib.error
import urllib.request

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
import pandas as pd

# ==============================================================================
# 1. CARICAMENTO AMBIENTE E PERCORSI ASSOLUTI
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(SCRIPT_DIR)

for env_candidate in [
    os.path.join(PROJECT_ROOT, ".env"),
    os.path.join(PROJECT_ROOT, "env.taziovettori"),
]:
  if os.path.exists(env_candidate):
    load_dotenv(env_candidate)
    break
else:
  load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
WEBSHARE_API_KEY = os.getenv("WEBSHARE_API_KEY", "").strip()
ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "").strip()
DEFAULT_GS_WEBHOOK = os.getenv("GS_WEBHOOK_URL", "").strip()

DB_PATH = os.getenv("DB_PATH", os.path.join(PROJECT_ROOT, "data", "database.db"))
if not os.path.isabs(DB_PATH):
  DB_PATH = os.path.join(PROJECT_ROOT, DB_PATH)

os.makedirs(os.path.dirname(DB_PATH) or "data", exist_ok=True)
os.makedirs(os.path.join(PROJECT_ROOT, "output"), exist_ok=True)
os.makedirs(os.path.join(PROJECT_ROOT, "scrapers"), exist_ok=True)

app = FastAPI(title="PrivateLab OSINT - Master Engine", version="8.5 Enterprise")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================================================================
# 2. STATO MOTORE E PROCESSI SUBPROCESS (TRI-STATE)
# ==============================================================================
ACTIVE_PROCESS: Optional[asyncio.subprocess.Process] = None
ENGINE_STATE = "STOPPED"  # Valori ammessi: "STOPPED", "RUNNING", "PAUSED"
CURRENT_TASK_NAME = "In attesa"

# Mappa Ufficiale Registro Imprese CCIAA (20 Regioni: Codici 0 - 19)
MAPPA_REGIONI_CCIAA = {
    "0": "ABRUZZO",
    "1": "BASILICATA",
    "2": "CALABRIA",
    "3": "CAMPANIA",
    "4": "EMILIA-ROMAGNA",
    "5": "FRIULI-VENEZIA GIULIA",
    "6": "LAZIO",
    "7": "LIGURIA",
    "8": "LOMBARDIA",
    "9": "MARCHE",
    "10": "MOLISE",
    "11": "PIEMONTE",
    "12": "PUGLIA",
    "13": "SARDEGNA",
    "14": "SICILIA",
    "15": "TOSCANA",
    "16": "TRENTINO-ALTO ADIGE",
    "17": "UMBRIA",
    "18": "VALLE D'AOSTA",
    "19": "VENETO",
}

# Elenco univoco delle 15 tabelle del database relazionale
TABELLE_SISTEMA = [
    "coda",
    "startup",
    "pmi",
    "brevetti",
    "marchi",
    "prodotti",
    "persone",
    "qualifiche",
    "finanziamenti",
    "investitori",
    "incubatori",
    "enti",
    "universita",
    "associazioni",
    "relazioni",
]

# Mapping ufficiale per Backup Integrale SQLite <-> Excel (15 Fogli) & GSheets
TAB_MAPPING = {
    "startup": ("🚀 Startup", "Startup"),
    "pmi": ("🏢 PMI", "PMI"),
    "brevetti": ("📜 Brevetti", "Brevetti"),
    "marchi": ("™️ Marchi", "Marchi"),
    "prodotti": ("📦 Prodotti", "Prodotti"),
    "persone": ("👥 Persone", "Persone"),
    "qualifiche": ("🎓 Qualifiche", "Qualifiche"),
    "finanziamenti": ("💰 Finanziamenti", "Finanziamenti"),
    "investitori": ("💼 Investitori", "Investitori"),
    "incubatori": ("🏭 Incubatori", "Incubatori"),
    "enti": ("🏛️ Enti", "Enti"),
    "universita": ("🏫 Universita", "Universita"),
    "associazioni": ("🤝 Associazioni", "Associazioni"),
    "coda": ("⏳ Coda", "Coda"),
    "relazioni": ("🕸️ Relazioni", "Relazioni"),
}


from collections import deque

# ==============================================================================
# 3. PUB/SUB LOG STREAMER CON STORICO IN MEMORIA
# ==============================================================================
class LogBroadcaster:

  def __init__(self):
    self.subscribers: Set[asyncio.Queue] = set()
    self.history: deque = deque(
        maxlen=1000
    )  # Mantiene gli ultimi 1000 eventi in memoria

  async def subscribe(self) -> asyncio.Queue:
    q = asyncio.Queue()
    self.subscribers.add(q)
    return q

  def unsubscribe(self, q: asyncio.Queue):
    self.subscribers.discard(q)

  async def broadcast(self, level: str, message: str, details: str = ""):
    timestamp = datetime.now().strftime("%H:%M:%S")
    clean_msg = re.sub(r"^\[(INFO|SUCCESS|WARN|ERROR)\]\s*", "", message)
    entry = {
        "time": timestamp,
        "level": level.upper(),
        "msg": clean_msg,
        "details": details,
    }
    self.history.append(entry)
    payload = json.dumps(entry)

    for q in list(self.subscribers):
      try:
        q.put_nowait(payload)
      except Exception:
        pass


broadcaster = LogBroadcaster()


async def broadcast_log(level: str, message: str, details: str = ""):
  await broadcaster.broadcast(level, message, details)


# Endpoint per prelevare lo storico completo dei log in formato testo o JSON
@app.get("/api/logs")
def get_logs_history():
  return {
      "total": len(broadcaster.history),
      "logs": list(broadcaster.history),
      "raw_text": "\n".join([
          f"[{l['time']}] [{l['level']}] {l['msg']}"
          for l in broadcaster.history
      ]),
  }


# ==============================================================================
# 4. GESTIONE DATABASE SQLITE
# ==============================================================================
def get_db_connection():
  conn = sqlite3.connect(DB_PATH, timeout=30.0)
  conn.execute("PRAGMA journal_mode = WAL;")
  conn.execute("PRAGMA busy_timeout = 30000;")
  conn.execute("PRAGMA foreign_keys = ON;")
  return conn


def init_db_schema():
  conn = get_db_connection()
  cur = conn.cursor()

  cur.execute("""
        CREATE TABLE IF NOT EXISTS coda (
            url TEXT PRIMARY KEY,
            denominazione TEXT,
            tipo TEXT DEFAULT 'STARTUP',
            regione TEXT,
            stato TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS startup (
            cf TEXT PRIMARY KEY,
            denominazione TEXT,
            denominazione_pulita TEXT,
            societa_benefit TEXT DEFAULT 'NO',
            comune TEXT,
            provincia TEXT,
            regione TEXT,
            forma_giuridica TEXT,
            modalita_costituzione TEXT,
            sito_internet TEXT,
            siti_web_esterni TEXT,
            email_ufficiale TEXT,
            pec_aziendale TEXT DEFAULT 'NON RILEVATO',
            url_scheda TEXT,
            codice_ateco TEXT,
            descrizione_ateco TEXT,
            macro_settore TEXT,
            macro_settore_codice TEXT,
            macro_settore_dettagliato TEXT,
            tag_settoriali TEXT,
            dt_costituzione TEXT,
            dt_iscrizione_startup TEXT,
            dt_aggiornamento_ri TEXT,
            data_compilazione_dichiarazione TEXT,
            classe_produzione_valore TEXT,
            classe_produzione_codice TEXT,
            classe_addetti_valore TEXT,
            classe_addetti_codice TEXT,
            classe_capitale_valore TEXT,
            classe_capitale_codice TEXT,
            prev_femminile TEXT,
            prev_giovanile TEXT,
            prev_straniera TEXT,
            req_spese_rs TEXT,
            req_team_qualificato TEXT,
            req_brevetti TEXT,
            legale_rappresentante TEXT,
            data_firma TEXT,
            ente_certificatore TEXT,
            codice_dichiarazione_xml TEXT,
            logo_url TEXT,
            logo_b64 TEXT,
            video_pitch_url TEXT,
            linkedin_url TEXT,
            facebook_url TEXT,
            twitter_x_url TEXT,
            instagram_url TEXT,
            youtube_channel_url TEXT,
            tiktok_url TEXT,
            completezza_profilo TEXT,
            stadio_startup_codice TEXT,
            stadio_startup_testo TEXT,
            stadio_prodotto_codice TEXT,
            stadio_prodotto_testo TEXT,
            stadio_team_codice TEXT,
            stadio_team_testo TEXT,
            pitch_presentazione_it TEXT,
            pitch_presentazione_en TEXT,
            descrizione_prodotto_it TEXT,
            descrizione_prodotto_en TEXT,
            business_model_descrizione TEXT,
            concorrenza_descrizione TEXT,
            innovazione_descrizione TEXT,
            canale_diretto TEXT DEFAULT 'NO',
            canale_gdo TEXT DEFAULT 'NO',
            canale_ecommerce TEXT DEFAULT 'NO',
            canale_agenti TEXT DEFAULT 'NO',
            mercato_italia_regioni TEXT,
            mercato_estero_aree TEXT,
            interesse_clienti TEXT DEFAULT 'NO',
            interesse_investitori TEXT DEFAULT 'NO',
            interesse_incubatori TEXT DEFAULT 'NO',
            interesse_partner_universitari TEXT DEFAULT 'NO',
            interesse_partner_imprenditoriali TEXT DEFAULT 'NO',
            interesse_figure_tecniche TEXT DEFAULT 'NO',
            incubatore_accelerato TEXT DEFAULT 'NO',
            incubatore_nome TEXT,
            incubatore_cf TEXT,
            ha_relazioni_universitarie TEXT DEFAULT 'NO',
            ha_proprieta_intellettuale TEXT DEFAULT 'NO',
            kpi_num_brevetti INTEGER DEFAULT 0,
            kpi_num_marchi INTEGER DEFAULT 0,
            kpi_num_prodotti INTEGER DEFAULT 0,
            finanza_ha_round TEXT DEFAULT 'NO',
            finanza_totale_raccolto_euro REAL DEFAULT 0,
            finanza_num_round INTEGER DEFAULT 0,
            finanza_tipologie_elenco TEXT,
            finanza_cronologia_testo TEXT,
            team_num_membri INTEGER DEFAULT 0,
            team_num_dottorati_phd INTEGER DEFAULT 0,
            team_groq_score INTEGER DEFAULT 0,
            team_groq_score_rationale TEXT,
            spese_rs_dichiarazione_ri TEXT,
            team_curricula_ri TEXT,
            relazioni_ricerca_ri TEXT,
            privative_dichiarazione_ri TEXT
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS pmi (
            cf TEXT PRIMARY KEY,
            denominazione TEXT,
            denominazione_pulita TEXT,
            societa_benefit TEXT DEFAULT 'NO',
            comune TEXT,
            provincia TEXT,
            regione TEXT,
            forma_giuridica TEXT,
            modalita_costituzione TEXT,
            sito_internet TEXT,
            email_ufficiale TEXT,
            pec_aziendale TEXT,
            codice_ateco TEXT,
            descrizione_ateco TEXT,
            macro_settore_dettagliato TEXT,
            dt_costituzione TEXT,
            dt_iscrizione_startup TEXT,
            dt_aggiornamento_ri TEXT,
            classe_produzione_valore TEXT,
            classe_produzione_codice TEXT,
            classe_addetti_valore TEXT,
            classe_addetti_codice TEXT,
            classe_capitale_valore TEXT,
            classe_capitale_codice TEXT,
            prev_femminile TEXT,
            prev_giovanile TEXT,
            prev_straniera TEXT,
            req_spese_rs TEXT,
            req_team_qualificato TEXT,
            req_brevetti TEXT,
            kpi_num_brevetti INTEGER DEFAULT 0,
            team_groq_score INTEGER DEFAULT 0,
            team_groq_score_rationale TEXT,
            pitch_presentazione_it TEXT,
            descrizione_prodotto_it TEXT,
            finanza_totale_raccolto_euro REAL DEFAULT 0
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS brevetti (
            id TEXT PRIMARY KEY,
            cf TEXT,
            titolo TEXT,
            numero TEXT,
            tipo TEXT,
            titolarita TEXT,
            data TEXT,
            ente TEXT DEFAULT 'NON DEFINITO',
            area TEXT DEFAULT 'NAZIONALE',
            verif TEXT DEFAULT 'NON VERIFICABILE'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS marchi (
            id TEXT PRIMARY KEY,
            cf TEXT,
            denominazione TEXT,
            stato TEXT,
            estensione TEXT,
            prodotto TEXT
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS prodotti (
            id TEXT PRIMARY KEY,
            cf TEXT,
            nome TEXT,
            categoria TEXT DEFAULT 'GENERALE',
            stadio TEXT,
            descrizione TEXT
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS persone (
            id TEXT PRIMARY KEY,
            cf TEXT,
            nome TEXT,
            ruolo TEXT,
            livello TEXT DEFAULT 'OPERATIONAL',
            titolo TEXT,
            eta TEXT,
            is_founder TEXT DEFAULT 'NO'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS qualifiche (
            id TEXT PRIMARY KEY,
            cf TEXT,
            ruolo TEXT,
            titolo TEXT,
            eta TEXT,
            descrizione_ruolo TEXT,
            persona TEXT,
            stato TEXT DEFAULT 'ANONIMO'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS finanziamenti (
            id TEXT PRIMARY KEY,
            cf TEXT,
            tipo_cod TEXT,
            tipo_decod TEXT,
            ammontare REAL DEFAULT 0,
            data_annuncio TEXT,
            data_chiusura TEXT,
            investitori TEXT
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS investitori (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT DEFAULT 'VENTURE_CAPITAL_ANGEL'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS incubatori (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            cf TEXT DEFAULT 'NON RILEVATO',
            categoria TEXT DEFAULT 'ACCELERATORE_CERTIFICATO'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS enti (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT DEFAULT 'ENTE_PUBBLICO'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS universita (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT DEFAULT 'CENTRO_RICERCA'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS associazioni (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT DEFAULT 'ASSOCIAZIONE'
        );
    """)

  cur.execute("""
        CREATE TABLE IF NOT EXISTS relazioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sorgente TEXT,
            sorgente_tipo TEXT,
            destinazione TEXT,
            destinazione_tipo TEXT,
            tipo TEXT,
            categoria TEXT,
            peso REAL DEFAULT 1.0,
            metadati TEXT
        );
    """)

  for table, column in [
      ("startup", "siti_web_esterni"),
      ("startup", "innovazione_descrizione"),
      ("qualifiche", "descrizione_ruolo"),
  ]:
    cur.execute(f"PRAGMA table_info({table})")
    existing_cols = {row[1] for row in cur.fetchall()}
    if column not in existing_cols:
      cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

  conn.commit()
  conn.close()


init_db_schema()


# ==============================================================================
# 5. CONTROLLO PROCESSI SUBPROCESS (RUN / PAUSE / STOP)
# ==============================================================================
async def kill_active_process():
  global ACTIVE_PROCESS, ENGINE_STATE
  if ACTIVE_PROCESS and ACTIVE_PROCESS.returncode is None:
    try:
      if sys.platform != "win32":
        pgid = os.getpgid(ACTIVE_PROCESS.pid)
        if ENGINE_STATE == "PAUSED":
          os.killpg(pgid, signal.SIGCONT)
        os.killpg(pgid, signal.SIGTERM)
        await asyncio.sleep(0.4)
        if ACTIVE_PROCESS.returncode is None:
          os.killpg(pgid, signal.SIGKILL)
      else:
        ACTIVE_PROCESS.terminate()
    except (ProcessLookupError, PermissionError):
      pass
    ACTIVE_PROCESS = None
  ENGINE_STATE = "STOPPED"


async def run_external_script(script_name: str, args: list, task_label: str):
  global ACTIVE_PROCESS, CURRENT_TASK_NAME, ENGINE_STATE

  script_path = os.path.join(PROJECT_ROOT, "scrapers", script_name)
  if not os.path.exists(script_path):
    script_path = os.path.join(PROJECT_ROOT, script_name)
    if not os.path.exists(script_path):
      await broadcast_log("ERROR", f"File script '{script_name}' non trovato!")
      ENGINE_STATE = "STOPPED"
      return

  CURRENT_TASK_NAME = task_label
  ENGINE_STATE = "RUNNING"
  await broadcast_log(
      "INFO", f"Motore avviato [PLAY]: {script_name} {' '.join(args)}"
  )

  try:
    cmd = [sys.executable, "-u", script_path] + args
    sub_env = dict(os.environ)
    sub_env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}:{os.path.join(PROJECT_ROOT, 'scrapers')}"
    )

    kwargs = {"env": sub_env}
    if sys.platform != "win32":
      kwargs["start_new_session"] = True

    ACTIVE_PROCESS = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kwargs,
    )

    while True:
      line = await ACTIVE_PROCESS.stdout.readline()
      if not line:
        break
      text_line = line.decode("utf-8", errors="replace").rstrip()
      if text_line:
        level = "INFO"
        low = text_line.lower()
        if "success" in low or "salvato" in low or "dossier" in low:
          level = "SUCCESS"
        elif "warn" in low or "attenzione" in low or "skip" in low:
          level = "WARN"
        elif "error" in low or "traceback" in low or "exception" in low:
          level = "ERROR"
        await broadcast_log(level, text_line)

    await ACTIVE_PROCESS.wait()
    await broadcast_log(
        "INFO", f"Processo terminato (Exit Code: {ACTIVE_PROCESS.returncode})"
    )
  except asyncio.CancelledError:
    await kill_active_process()
    await broadcast_log("WARN", "Processo interrotto dall'utente.")
  except Exception as e:
    await broadcast_log(
        "ERROR", f"Errore runtime motore: {e}", traceback.format_exc()
    )
  finally:
    ACTIVE_PROCESS = None
    ENGINE_STATE = "STOPPED"
    CURRENT_TASK_NAME = "In attesa"


# ==============================================================================
# 6. ROUTE FILE STATICI E STREAM SSE
# ==============================================================================
@app.get("/", response_class=FileResponse)
async def serve_index():
  index_path = os.path.join(PROJECT_ROOT, "index.html")
  if os.path.exists(index_path):
    return FileResponse(index_path)
  return HTMLResponse("<h2>index.html non trovato</h2>", status_code=404)


@app.get("/style.css", response_class=FileResponse)
async def serve_css():
  css_path = os.path.join(PROJECT_ROOT, "style.css")
  if os.path.exists(css_path):
    # Cache control reattivo per evitare disallineamenti di stile
    return FileResponse(
        css_path,
        media_type="text/css",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
  return JSONResponse(
      status_code=404, content={"error": "style.css non trovato"}
  )


@app.get("/api/stream")
@app.get("/api/stream-logs")
async def sse_logs(request: Request):
  q = await broadcaster.subscribe()

  async def event_generator():
    try:
      while True:
        if await request.is_disconnected():
          break
        try:
          raw_payload = await asyncio.wait_for(q.get(), timeout=2.0)
          yield f"data: {raw_payload}\n\n"
        except asyncio.TimeoutError:
          yield ": ping\n\n"
    finally:
      broadcaster.unsubscribe(q)

  return StreamingResponse(event_generator(), media_type="text/event-stream")


# ==============================================================================
# 7. 1-CLICK TEST DELLE VERE API ESTERNE (GROQ & WEBSHARE)
# ==============================================================================
@app.get("/api/test/groq")
@app.post("/api/test/groq")
async def test_groq_api():
  """Verifica la validità della chiave e l'accessibilità dell'API Groq AI."""
  if not GROQ_API_KEY:
    await broadcast_log(
        "ERROR", "[TEST API] GROQ_API_KEY mancante nel file .env!"
    )
    return {
        "groq_configured": False,
        "worker": "ERROR",
        "success": False,
        "message": "GROQ_API_KEY non trovata nel file .env!",
    }

  t0 = time.time()

  def _ping_groq():
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/models",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "PrivateLab-Tester/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
      return json.loads(resp.read().decode("utf-8"))

  try:
    data = await asyncio.to_thread(_ping_groq)
    latency = int((time.time() - t0) * 1000)
    models = len(data.get("data", []))
    msg = f"API Groq attiva ({models} modelli pronti)"
    await broadcast_log("SUCCESS", f"[TEST GROQ] {msg} [{latency}ms]")
    return {
        "groq_configured": True,
        "worker": "READY",
        "success": True,
        "latency_ms": latency,
        "models_count": models,
        "message": msg,
    }
  except Exception as e:
    msg = f"Errore connessione Groq API: {e}"
    await broadcast_log("ERROR", f"[TEST GROQ] {msg}")
    return {
        "groq_configured": True,
        "worker": "ERROR",
        "success": False,
        "message": msg,
    }


@app.get("/api/test/webshare")
@app.post("/api/test/webshare")
async def test_webshare_api():
  """Verifica la validità dell'API Webshare con il parametro obbligatorio mode=direct."""
  t0 = time.time()
  results = {
      "webshare_configured": bool(WEBSHARE_API_KEY),
      "target_reachable": False,
      "success": False,
      "latency_ms": 0,
      "message": "",
  }

  def _ping_webshare():
    if WEBSHARE_API_KEY:
      # Parametro mode=direct OBBLIGATORIO per evitare HTTP 400 Bad Request
      url = "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=10"
      req = urllib.request.Request(
          url,
          headers={
              "Authorization": f"Token {WEBSHARE_API_KEY}",
              "User-Agent": "PrivateLab-Tester/1.0",
          },
      )
      with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))
    else:
      # Fallback su endpoint target
      req = urllib.request.Request(
          "https://startup.registroimprese.it/isin/home",
          headers={"User-Agent": "Mozilla/5.0"},
      )
      with urllib.request.urlopen(req, timeout=5) as resp:
        return {"count": 1 if resp.status in (200, 302) else 0}

  try:
    data = await asyncio.to_thread(_ping_webshare)
    results["latency_ms"] = int((time.time() - t0) * 1000)
    results["target_reachable"] = True
    results["success"] = True
    if WEBSHARE_API_KEY:
      cnt = data.get("count", len(data.get("results", [])))
      results["message"] = f"Pool Webshare operativo ({cnt} proxy disponibili)"
    else:
      results["message"] = (
          "Connessione diretta attiva (target CCIAA raggiungibile)"
      )
    await broadcast_log(
        "SUCCESS",
        f"[TEST WEBSHARE] {results['message']} [{results['latency_ms']}ms]",
    )
  except Exception as e:
    results["latency_ms"] = int((time.time() - t0) * 1000)
    results["success"] = False
    results["message"] = f"Errore routing Webshare: {e}"
    await broadcast_log("ERROR", f"[TEST WEBSHARE] {results['message']}")

  return results


# Alias di compatibilità
@app.get("/api/test/harvester")
@app.post("/api/test/harvester")
async def alias_test_harvester():
  return await test_webshare_api()


@app.get("/api/test/worker")
@app.post("/api/test/worker")
async def alias_test_worker():
  return await test_groq_api()


# ==============================================================================
# 8. CONTROLLI OPERATIVI (PLAY, PAUSE, STOP)
# ==============================================================================
@app.post("/api/action/run")
async def trigger_run(
    bg: BackgroundTasks,
    action: str = Query("both"),
    module: str = Query("startup"),
    limit: int = Query(50),
    delay: float = Query(2.0),
    region: Optional[str] = Query(None),
    regione: Optional[str] = Query(None),
    proxy: str = Query("zenrows,webshare"),
):
  global ACTIVE_PROCESS, ENGINE_STATE
  if ACTIVE_PROCESS and ACTIVE_PROCESS.returncode is None:
    if ENGINE_STATE == "PAUSED" and sys.platform != "win32":
      try:
        os.killpg(os.getpgid(ACTIVE_PROCESS.pid), signal.SIGCONT)
        ENGINE_STATE = "RUNNING"
        await broadcast_log("INFO", "Ripresa esecuzione motore da pausa.")
        return {
            "status": "ok",
            "state": "RUNNING",
            "message": "Motore ripreso",
        }
      except Exception:
        pass
    return JSONResponse(
        status_code=400,
        content={"status": "error", "message": "Un job è già in esecuzione!"},
    )

  raw_reg = (region or regione or "ALL").strip()
  target_region = raw_reg
  for cod, nom in MAPPA_REGIONI_CCIAA.items():
    if raw_reg.upper() == nom:
      target_region = cod
      break

  target_module = "STARTUP"
  action_clean = action.lower().strip()

  if action_clean == "both":
    script_file = "startup.py"
    args = [
        "--action",
        "both",
        "--regione",
        target_region,
        "--limit",
        str(limit),
        "--delay",
        str(delay),
        "--proxy",
        proxy.strip() or "direct",
        "--tipo",
        target_module,
    ]
    task_label = f"HARVESTER + WORKER ({target_module})"
  elif action_clean == "harvest":
    script_file = (
        "harvester.py"
        if os.path.exists(
            os.path.join(PROJECT_ROOT, "scrapers", "harvester.py")
        )
        else "startup.py"
    )
    args = [
        "--regione",
        target_region,
        "--limit",
        str(limit),
        "--delay",
        str(delay),
        "--proxy",
        proxy.strip() or "direct",
        "--tipo",
        target_module,
    ]
    task_label = f"HARVESTER ({target_module})"
  else:
    script_file = (
        "worker.py"
        if os.path.exists(os.path.join(PROJECT_ROOT, "scrapers", "worker.py"))
        else "startup.py"
    )
    args = [
        "--limit",
        str(limit),
        "--delay",
        str(delay),
        "--proxy",
        proxy.strip() or "direct",
        "--tipo",
        target_module,
    ]
    task_label = f"WORKER ({target_module})"

  bg.add_task(run_external_script, script_file, args, task_label)
  return {
      "status": "ok",
      "state": "RUNNING",
      "message": f"Avviato {task_label} (Regione: {target_region})",
  }


@app.post("/api/action/pause")
async def trigger_pause():
  global ACTIVE_PROCESS, ENGINE_STATE
  if ACTIVE_PROCESS and ACTIVE_PROCESS.returncode is None:
    if sys.platform != "win32":
      try:
        pgid = os.getpgid(ACTIVE_PROCESS.pid)
        if ENGINE_STATE == "RUNNING":
          os.killpg(pgid, signal.SIGSTOP)
          ENGINE_STATE = "PAUSED"
          await broadcast_log("WARN", "[PAUSE] Motore messo in pausa.")
          return {
              "status": "ok",
              "state": "PAUSED",
              "message": "Motore in pausa",
          }
        elif ENGINE_STATE == "PAUSED":
          os.killpg(pgid, signal.SIGCONT)
          ENGINE_STATE = "RUNNING"
          await broadcast_log("INFO", "[PLAY] Motore ripreso dalla pausa.")
          return {
              "status": "ok",
              "state": "RUNNING",
              "message": "Motore ripreso",
          }
      except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    else:
      return {
          "status": "warn",
          "message": "Pausa non supportata su piattaforma Windows",
      }

  return {
      "status": "ok",
      "state": ENGINE_STATE,
      "message": "Nessun processo attivo da mettere in pausa",
  }


@app.post("/api/action/stop")
@app.post("/api/stop")
async def trigger_stop():
  global ACTIVE_PROCESS, ENGINE_STATE
  if ACTIVE_PROCESS and ACTIVE_PROCESS.returncode is None:
    await kill_active_process()
    await broadcast_log("WARN", "[STOP] Motore arrestato dall'operatore.")
    return {"status": "ok", "state": "STOPPED", "message": "Motore arrestato"}
  ENGINE_STATE = "STOPPED"
  return {
      "status": "ok",
      "state": "STOPPED",
      "message": "Nessun processo attivo",
  }


# ==============================================================================
# 9. TELEMETRIA E STATISTICHE GLOBALI
# ==============================================================================
@app.get("/api/stats")
@app.get("/api/status")
def get_global_stats():
  try:
    conn = get_db_connection()
    cur = conn.cursor()

    def get_cnt(table):
      try:
        return cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
      except Exception:
        return 0

    s_count = get_cnt("startup")
    p_count = get_cnt("pmi")

    try:
      rounds = cur.execute(
          "SELECT COUNT(*), COALESCE(SUM(ammontare), 0) FROM finanziamenti"
      ).fetchone()
      totale_fin = rounds[1] or 0
      num_r = rounds[0] or 0
    except Exception:
      totale_fin, num_r = 0, 0

    brevetti = get_cnt("brevetti")
    marchi = get_cnt("marchi")
    prodotti = get_cnt("prodotti")
    persone = get_cnt("persone")
    qualifiche = get_cnt("qualifiche")
    investitori = get_cnt("investitori")
    incubatori = get_cnt("incubatori")
    enti = get_cnt("enti")
    universita = get_cnt("universita")
    associazioni = get_cnt("associazioni")
    relazioni = get_cnt("relazioni")
    coda_pending = cur.execute(
        "SELECT COUNT(*) FROM coda WHERE stato = 'PENDING'"
    ).fetchone()[0]

    conn.close()

    is_running = ACTIVE_PROCESS is not None and ACTIVE_PROCESS.returncode is None

    return {
        "engine_state": ENGINE_STATE,  # "RUNNING", "PAUSED", "STOPPED"
        "running": is_running and ENGINE_STATE == "RUNNING",
        "is_running": is_running,
        "is_paused": ENGINE_STATE == "PAUSED",
        "task_name": CURRENT_TASK_NAME,
        "startup": s_count,
        "pmi": p_count,
        "totale_finanza_euro": totale_fin,
        "num_round": num_r,
        "brevetti": brevetti,
        "marchi": marchi,
        "prodotti": prodotti,
        "persone": persone,
        "qualifiche": qualifiche,
        "investitori": investitori,
        "incubatori": incubatori,
        "enti": enti,
        "universita": universita,
        "associazioni": associazioni,
        "relazioni": relazioni,
        "coda_pending": coda_pending,
    }
  except Exception:
    return {
        "engine_state": "STOPPED",
        "running": False,
        "is_running": False,
        "is_paused": False,
        "task_name": "In attesa",
        "startup": 0,
        "pmi": 0,
        "totale_finanza_euro": 0,
        "num_round": 0,
        "brevetti": 0,
        "marchi": 0,
        "prodotti": 0,
        "persone": 0,
        "qualifiche": 0,
        "investitori": 0,
        "incubatori": 0,
        "enti": 0,
        "universita": 0,
        "associazioni": 0,
        "relazioni": 0,
        "coda_pending": 0,
    }


# ==============================================================================
# 10. RICERCA E TABELLA MASTER (STARTUP / PMI CON 20 REGIONI)
# ==============================================================================
@app.get("/api/startup")
@app.get("/api/aziende")
def search_startup(
    tipo: str = "STARTUP",
    q: Optional[str] = Query(None),
    regione: Optional[str] = Query(None),
    provincia: Optional[str] = Query(None),
    ateco: Optional[str] = Query(None),
    page: int = 1,
    page_size: int = 50,
):
  target_table = "pmi" if tipo.upper() == "PMI" else "startup"
  offset = (page - 1) * page_size
  conn = get_db_connection()
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()

  try:
    cur.execute(f"SELECT 1 FROM {target_table} LIMIT 1")
  except sqlite3.OperationalError:
    conn.close()
    return {"total": 0, "page": page, "page_size": page_size, "items": []}

  query = f"SELECT * FROM {target_table} WHERE 1=1"
  params = []

  if q:
    query += (
        " AND (denominazione LIKE ? OR denominazione_pulita LIKE ? OR cf LIKE"
        " ? OR comune LIKE ?)"
    )
    params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])

  if regione and regione.upper() not in ["ALL", "TUTTE", ""]:
    reg_clean = regione.strip().upper()
    if reg_clean in MAPPA_REGIONI_CCIAA:
      reg_clean = MAPPA_REGIONI_CCIAA[reg_clean]
    query += " AND (UPPER(regione) = ? OR UPPER(regione) LIKE ?)"
    params.extend([reg_clean, f"%{reg_clean}%"])

  if provincia and provincia.upper() not in ["ALL", "TUTTE", ""]:
    prov_clean = provincia.strip().upper()
    query += " AND (UPPER(provincia) = ? OR UPPER(comune) LIKE ?)"
    params.extend([prov_clean, f"%({prov_clean})%"])

  if ateco:
    query += " AND codice_ateco LIKE ?"
    params.append(f"{ateco}%")

  try:
    total = cur.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()[0]
    query += " ORDER BY rowid DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])
    rows = cur.execute(query, params).fetchall()

    items = []
    for r in rows:
      d = dict(r)
      if "cf" in d and "codice_fiscale_piva" not in d:
        d["codice_fiscale_piva"] = d["cf"]
      items.append(d)
  except Exception:
    total = 0
    items = []
  finally:
    conn.close()

  return {
      "total": total,
      "page": page,
      "page_size": page_size,
      "items": items,
  }


# ==============================================================================
# 11. TABELLE CONDIVISE DATA LAKE (AZIENDA_NOME IN EVIDENZA)
# ==============================================================================
@app.get("/api/entita/{tipo}")
def list_entita(tipo: str):
  target = tipo.lower().strip()
  conn = get_db_connection()
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()

  try:
    if target == "brevetti":
      rows = cur.execute("""
                SELECT COALESCE(s.denominazione_pulita, p.denominazione_pulita, b.cf) as azienda_nome,
                       b.titolo, b.numero, b.tipo, b.data, b.ente, b.area, b.verif, b.id
                FROM brevetti b
                LEFT JOIN startup s ON b.cf = s.cf
                LEFT JOIN pmi p ON b.cf = p.cf
                ORDER BY b.rowid DESC LIMIT 500
            """).fetchall()
    elif target == "marchi":
      rows = cur.execute("""
                SELECT COALESCE(s.denominazione_pulita, p.denominazione_pulita, m.cf) as azienda_nome,
                       m.denominazione, m.stato, m.estensione, m.prodotto, m.id
                FROM marchi m
                LEFT JOIN startup s ON m.cf = s.cf
                LEFT JOIN pmi p ON m.cf = p.cf
                ORDER BY m.rowid DESC LIMIT 500
            """).fetchall()
    elif target == "prodotti":
      rows = cur.execute("""
                SELECT COALESCE(s.denominazione_pulita, p.denominazione_pulita, pr.cf) as azienda_nome,
                       pr.nome, pr.categoria, pr.stadio, pr.descrizione, pr.id
                FROM prodotti pr
                LEFT JOIN startup s ON pr.cf = s.cf
                LEFT JOIN pmi p ON pr.cf = p.cf
                ORDER BY pr.rowid DESC LIMIT 500
            """).fetchall()
    elif target == "persone":
      rows = cur.execute("""
                SELECT COALESCE(s.denominazione_pulita, p.denominazione_pulita, t.cf) as azienda_nome,
                       t.nome, t.ruolo, t.livello, t.titolo, t.eta, t.is_founder, t.id
                FROM persone t
                LEFT JOIN startup s ON t.cf = s.cf
                LEFT JOIN pmi p ON t.cf = p.cf
                ORDER BY CASE t.livello WHEN 'C_LEVEL' THEN 1 WHEN 'FOUNDER' THEN 2 WHEN 'BOARD' THEN 3 ELSE 4 END ASC
                LIMIT 500
            """).fetchall()
    elif target == "qualifiche":
      rows = cur.execute("""
                SELECT COALESCE(s.denominazione_pulita, p.denominazione_pulita, q.cf) as azienda_nome,
                       q.ruolo, q.titolo, q.eta, q.descrizione_ruolo, q.persona, q.stato, q.id
                FROM qualifiche q
                LEFT JOIN startup s ON q.cf = s.cf
                LEFT JOIN pmi p ON q.cf = p.cf
                ORDER BY q.rowid DESC LIMIT 500
            """).fetchall()
    elif target == "finanziamenti":
      rows = cur.execute("""
                SELECT COALESCE(s.denominazione_pulita, p.denominazione_pulita, f.cf) as azienda_nome,
                       COALESCE(f.tipo_decod, f.tipo_cod, 'Round') as tipo, f.ammontare, f.data_annuncio, f.investitori, f.id
                FROM finanziamenti f
                LEFT JOIN startup s ON f.cf = s.cf
                LEFT JOIN pmi p ON f.cf = p.cf
                ORDER BY f.ammontare DESC LIMIT 500
            """).fetchall()
    elif target in [
        "investitori",
        "incubatori",
        "enti",
        "universita",
        "associazioni",
    ]:
      rows = cur.execute(
          f"SELECT denominazione, categoria, id FROM {target} LIMIT 500"
      ).fetchall()
    elif target == "coda":
      rows = cur.execute(
          "SELECT denominazione, url, tipo, regione, stato, created_at FROM"
          " coda ORDER BY rowid DESC LIMIT 500"
      ).fetchall()
    elif target == "relazioni":
      rows = cur.execute(
          "SELECT sorgente, tipo, destinazione, categoria, peso, id FROM"
          " relazioni ORDER BY id DESC LIMIT 500"
      ).fetchall()
    else:
      rows = []
  except Exception:
    rows = []
  finally:
    conn.close()

  return {"items": [dict(r) for r in rows]}


# ==============================================================================
# 12. REPORT AZIENDALE UFFICIALE (DOSSIER CAMERALE PDF-LIKE)
# ==============================================================================
@app.get("/api/dettaglio/{cf}")
def get_dossier_aziendale(cf: str):
  """Restituisce il set integrale di dati per comporre il Report Camerale Unioncamere."""
  conn = get_db_connection()
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()

  cf_clean = cf.strip().upper()
  cf_variations = [
      cf_clean,
      cf_clean.replace("IT", ""),
      f"IT{cf_clean.replace('IT', '')}",
  ]
  placeholders = ",".join(["?"] * len(cf_variations))

  try:
    master = cur.execute(
        f"SELECT * FROM startup WHERE cf IN ({placeholders})", cf_variations
    ).fetchone()
    if not master:
      master = cur.execute(
          f"SELECT * FROM pmi WHERE cf IN ({placeholders})", cf_variations
      ).fetchone()

    if not master:
      conn.close()
      return JSONResponse(
          status_code=404,
          content={"error": f"Nessun dossier trovato per CF: {cf}"},
      )

    actual_cf = master["cf"]

    rounds = cur.execute(
        "SELECT * FROM finanziamenti WHERE cf = ? ORDER BY ammontare DESC",
        (actual_cf,),
    ).fetchall()
    persone = cur.execute(
        """
            SELECT * FROM persone WHERE cf = ? 
            ORDER BY CASE livello WHEN 'C_LEVEL' THEN 1 WHEN 'FOUNDER' THEN 2 WHEN 'BOARD' THEN 3 ELSE 4 END ASC
        """,
        (actual_cf,),
    ).fetchall()
    qualifiche = cur.execute(
        "SELECT * FROM qualifiche WHERE cf = ?", (actual_cf,)
    ).fetchall()
    brevetti = cur.execute(
        "SELECT * FROM brevetti WHERE cf = ?", (actual_cf,)
    ).fetchall()
    marchi = cur.execute(
        "SELECT * FROM marchi WHERE cf = ?", (actual_cf,)
    ).fetchall()
    prodotti = cur.execute(
        "SELECT * FROM prodotti WHERE cf = ?", (actual_cf,)
    ).fetchall()

    relazioni = cur.execute(
        """
            SELECT * FROM relazioni 
            WHERE sorgente IN (?, ?) OR destinazione IN (?, ?)
        """,
        (
            f"STARTUP:{actual_cf}",
            f"PMI:{actual_cf}",
            f"STARTUP:{actual_cf}",
            f"PMI:{actual_cf}",
        ),
    ).fetchall()

  except Exception as e:
    conn.close()
    return JSONResponse(status_code=500, content={"error": str(e)})
  finally:
    conn.close()

  m_dict = dict(master)

  return {
      "report_meta": {
          "tipo_documento": (
              "REPORT DI INTELLIGENCE AZIENDALE // REGISTRO IMPRESE CCIAA"
          ),
          "data_emissione": datetime.now().strftime("%d/%m/%Y %H:%M"),
          "stato_archivio": (
              "VERIFICATO" if m_dict.get("cf") else "NON DISPONIBILE"
          ),
      },
      "master": m_dict,
      "scorecard": {
          "groq_score": m_dict.get("team_groq_score", 50),
          "groq_rationale": m_dict.get("team_groq_score_rationale", ""),
          "req_spese_rs": m_dict.get("req_spese_rs", "NO"),
          "req_team_qualificato": m_dict.get("req_team_qualificato", "NO"),
          "req_brevetti": m_dict.get("req_brevetti", "NO"),
      },
      "team": [dict(p) for p in persone],
      "qualifiche": [dict(q) for q in qualifiche],
      "brevetti": [dict(b) for b in brevetti],
      "marchi": [dict(m) for m in marchi],
      "prodotti": [dict(pr) for pr in prodotti],
      "rounds": [dict(r) for r in rounds],
      "relazioni": [dict(rel) for rel in relazioni],
  }


# ==============================================================================
# 13. GRAFO RELAZIONALE CON TUTTE LE ENTITÀ (12+ CATEGORIE)
# ==============================================================================
@app.get("/api/grafo/data")
def get_grafo_completo():
  """Genera nodi ed archi coprendo integralmente TUTTE le entità censite nel sistema."""
  conn = get_db_connection()
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()

  nodes = []
  node_set = set()
  links = []

  categories = [
      {"name": "Startup", "itemStyle": {"color": "#0052ff"}},
      {"name": "PMI", "itemStyle": {"color": "#6366f1"}},
      {"name": "Founder / Team", "itemStyle": {"color": "#ec4899"}},
      {"name": "Qualifiche XML", "itemStyle": {"color": "#8b5cf6"}},
      {"name": "Prodotti", "itemStyle": {"color": "#0ea5e9"}},
      {"name": "Brevetti", "itemStyle": {"color": "#ea580c"}},
      {"name": "Marchi", "itemStyle": {"color": "#d97706"}},
      {"name": "Finanziamenti", "itemStyle": {"color": "#10b981"}},
      {"name": "Investitori", "itemStyle": {"color": "#059669"}},
      {"name": "Incubatori", "itemStyle": {"color": "#f59e0b"}},
      {"name": "Enti", "itemStyle": {"color": "#a855f7"}},
      {"name": "Università", "itemStyle": {"color": "#06b6d4"}},
      {"name": "Associazioni", "itemStyle": {"color": "#64748b"}},
  ]

  try:
    # 0. Startup
    for s in cur.execute(
        "SELECT cf, denominazione_pulita, finanza_totale_raccolto_euro,"
        " kpi_num_brevetti, macro_settore_dettagliato FROM startup"
    ).fetchall():
      nid = f"STARTUP:{s['cf']}"
      fin_val = float(s["finanza_totale_raccolto_euro"] or 0)
      sub = (
          f"€ {fin_val/1e6:.2f}M"
          if fin_val > 0
          else (s["macro_settore_dettagliato"] or "Startup")[:18]
      )
      nodes.append({
          "id": nid,
          "name": s["denominazione_pulita"] or s["cf"],
          "sub": sub,
          "category": 0,
          "symbolSize": 32 if fin_val > 500000 else 24,
      })
      node_set.add(nid)

    # 1. PMI
    for p in cur.execute(
        "SELECT cf, denominazione_pulita, finanza_totale_raccolto_euro FROM pmi"
    ).fetchall():
      nid = f"PMI:{p['cf']}"
      fin_val = float(p["finanza_totale_raccolto_euro"] or 0)
      nodes.append({
          "id": nid,
          "name": p["denominazione_pulita"] or p["cf"],
          "sub": f"€ {fin_val/1e6:.2f}M" if fin_val > 0 else "PMI Innovativa",
          "category": 1,
          "symbolSize": 26,
      })
      node_set.add(nid)

    # 2. Persone (Team e Founder)
    for per in cur.execute(
        "SELECT id, cf, nome, ruolo FROM persone"
    ).fetchall():
      nid = f"PERSONA:{per['id']}"
      if nid not in node_set:
        nodes.append({
            "id": nid,
            "name": per["nome"] or "Persona",
            "sub": per["ruolo"] or "Team",
            "category": 2,
            "symbolSize": 18,
        })
        node_set.add(nid)

    # 3. Qualifiche XML
    for q in cur.execute(
        "SELECT id, cf, ruolo, titolo FROM qualifiche"
    ).fetchall():
      nid = f"QUALIFICA:{q['id']}"
      if nid not in node_set:
        nodes.append({
            "id": nid,
            "name": q["ruolo"] or "Qualifica",
            "sub": q["titolo"] or "Competenza",
            "category": 3,
            "symbolSize": 14,
        })
        node_set.add(nid)
      # Link strutturale alla società
      parent_id = (
          f"STARTUP:{q['cf']}"
          if f"STARTUP:{q['cf']}" in node_set
          else f"PMI:{q['cf']}"
      )
      if parent_id in node_set:
        links.append({
            "source": nid,
            "target": parent_id,
            "rel_type": "QUALIFICA_DICHIARATA",
            "lineStyle": {"width": 1.0, "opacity": 0.5},
        })

    # 4. Prodotti
    for pr in cur.execute(
        "SELECT id, cf, nome, categoria FROM prodotti"
    ).fetchall():
      nid = f"PRODOTTO:{pr['id']}"
      if nid not in node_set:
        nodes.append({
            "id": nid,
            "name": pr["nome"] or "Prodotto",
            "sub": pr["categoria"] or "Soluzione",
            "category": 4,
            "symbolSize": 16,
        })
        node_set.add(nid)
      parent_id = (
          f"STARTUP:{pr['cf']}"
          if f"STARTUP:{pr['cf']}" in node_set
          else f"PMI:{pr['cf']}"
      )
      if parent_id in node_set:
        links.append({
            "source": nid,
            "target": parent_id,
            "rel_type": "SVILUPPA_PRODOTTO",
            "lineStyle": {"width": 1.2, "opacity": 0.6},
        })

    # 5. Brevetti
    for b in cur.execute(
        "SELECT id, cf, titolo, numero FROM brevetti"
    ).fetchall():
      nid = f"BREVETTO:{b['id']}"
      if nid not in node_set:
        nodes.append({
            "id": nid,
            "name": (b["titolo"] or "Brevetto")[:18],
            "sub": b["numero"] or "Privativa",
            "category": 5,
            "symbolSize": 16,
        })
        node_set.add(nid)
      parent_id = (
          f"STARTUP:{b['cf']}"
          if f"STARTUP:{b['cf']}" in node_set
          else f"PMI:{b['cf']}"
      )
      if parent_id in node_set:
        links.append({
            "source": nid,
            "target": parent_id,
            "rel_type": "DETIENE_BREVETTO",
            "lineStyle": {"width": 1.5, "opacity": 0.7},
        })

    # 6. Marchi
    for m in cur.execute(
        "SELECT id, cf, denominazione, stato FROM marchi"
    ).fetchall():
      nid = f"MARCHIO:{m['id']}"
      if nid not in node_set:
        nodes.append({
            "id": nid,
            "name": (m["denominazione"] or "Marchio")[:18],
            "sub": m["stato"] or "Brand",
            "category": 6,
            "symbolSize": 14,
        })
        node_set.add(nid)
      parent_id = (
          f"STARTUP:{m['cf']}"
          if f"STARTUP:{m['cf']}" in node_set
          else f"PMI:{m['cf']}"
      )
      if parent_id in node_set:
        links.append({
            "source": nid,
            "target": parent_id,
            "rel_type": "DETIENE_MARCHIO",
            "lineStyle": {"width": 1.0, "opacity": 0.5},
        })

    # 7. Finanziamenti
    for fn in cur.execute(
        "SELECT id, cf, ammontare, tipo_decod FROM finanziamenti"
    ).fetchall():
      nid = f"FINANZIAMENTO:{fn['id']}"
      amm_val = float(fn["ammontare"] or 0)
      if nid not in node_set:
        nodes.append({
            "id": nid,
            "name": f"€ {amm_val:,.0f}" if amm_val > 0 else "Round",
            "sub": fn["tipo_decod"] or "Finanza",
            "category": 7,
            "symbolSize": 18 if amm_val > 100000 else 14,
        })
        node_set.add(nid)

    # Helper per nodi d'ecosistema (8-12)
    def add_sec_nodes(table, cat_idx, sz):
      try:
        for r in cur.execute(
            f"SELECT id, denominazione, categoria FROM {table}"
        ).fetchall():
          raw_id = str(r["id"])
          nid = f"{table.upper()}:{raw_id}" if ":" not in raw_id else raw_id
          if nid not in node_set:
            nodes.append({
                "id": nid,
                "name": (r["denominazione"] or "")[:22],
                "sub": (r["categoria"] or "")[:18],
                "category": cat_idx,
                "symbolSize": sz,
            })
            node_set.add(nid)
      except Exception:
        pass

    add_sec_nodes("investitori", 8, 22)
    add_sec_nodes("incubatori", 9, 20)
    add_sec_nodes("enti", 10, 18)
    add_sec_nodes("universita", 11, 20)
    add_sec_nodes("associazioni", 12, 16)

    # Archi memorizzati nella tabella relazioni
    for ed in cur.execute(
        "SELECT sorgente, destinazione, tipo, peso FROM relazioni"
    ).fetchall():
      s_id = str(ed["sorgente"])
      d_id = str(ed["destinazione"])
      if s_id in node_set and d_id in node_set:
        w = float(ed["peso"] or 1.0)
        links.append({
            "source": s_id,
            "target": d_id,
            "rel_type": ed["tipo"] or "COLLEGAMENTO",
            "lineStyle": {
                "width": 3.0 if w > 500000 else (2.0 if w > 1 else 1.2),
                "opacity": 0.65,
            },
        })

  except Exception as e:
    print(f"Errore generazione grafo completo: {e}")
  finally:
    conn.close()

  return {"nodes": nodes, "links": links, "categories": categories}


# ==============================================================================
# 14. EXPORT FILE & BACKUP INTEGRALE 1:1
# ==============================================================================
@app.get("/api/download/db")
def download_database_file():
  if not os.path.exists(DB_PATH):
    return JSONResponse(status_code=404, content={"message": "DB non trovato"})
  ts = datetime.now().strftime("%Y%m%d_%H%M")
  return FileResponse(
      DB_PATH,
      media_type="application/x-sqlite3",
      filename=f"database_privatelab_{ts}.db",
  )


@app.get("/api/download/excel")
@app.get("/api/export/excel")
def download_excel_datalake():
  conn = get_db_connection()
  path = os.path.join(PROJECT_ROOT, "output", "backup_integrale_15_tabelle.xlsx")

  try:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
      for tbl, (emoji_name, clean_name) in TAB_MAPPING.items():
        try:
          df = pd.read_sql_query(f"SELECT * FROM {tbl}", conn)
        except Exception:
          df = pd.DataFrame()
        df.to_excel(writer, sheet_name=clean_name[:31], index=False)
  except Exception as e:
    conn.close()
    return JSONResponse(
        status_code=500, content={"error": f"Errore Excel: {e}"}
    )
  finally:
    conn.close()

  return FileResponse(
      path,
      media_type=(
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
      ),
      filename="backup_integrale_15_tabelle.xlsx",
  )


@app.get("/api/download/csv")
@app.get("/api/export/csv")
def download_csv(
    tipo: str = Query("STARTUP"),
    q: Optional[str] = Query(None),
    regione: Optional[str] = Query(None),
    provincia: Optional[str] = Query(None),
):
  target_table = "pmi" if tipo.upper() == "PMI" else "startup"
  query = f"SELECT * FROM {target_table} WHERE 1=1"
  params = []

  if q:
    query += (
        " AND (denominazione LIKE ? OR denominazione_pulita LIKE ? OR cf LIKE"
        " ? OR comune LIKE ?)"
    )
    params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])

  if regione and regione.upper() not in ["ALL", "TUTTE", ""]:
    reg_clean = regione.strip().upper()
    if reg_clean in MAPPA_REGIONI_CCIAA:
      reg_clean = MAPPA_REGIONI_CCIAA[reg_clean]
    query += " AND (UPPER(regione) = ? OR UPPER(regione) LIKE ?)"
    params.extend([reg_clean, f"%{reg_clean}%"])

  if provincia and provincia.upper() not in ["ALL", "TUTTE", ""]:
    prov_clean = provincia.strip().upper()
    query += " AND (UPPER(provincia) = ? OR UPPER(comune) LIKE ?)"
    params.extend([prov_clean, f"%({prov_clean})%"])

  conn = get_db_connection()
  try:
    df = pd.read_sql_query(query, conn, params=params)
  except Exception:
    df = pd.DataFrame()
  finally:
    conn.close()

  if df.empty:
    return JSONResponse(status_code=404, content={"errore": "Nessun dato"})
  filename = f"{target_table}_estratte.csv"
  path = os.path.join(PROJECT_ROOT, "output", filename)
  df.to_csv(path, index=False, sep=";", encoding="utf-8-sig")
  return FileResponse(
      path, media_type="text/csv", filename=filename
  )


@app.post("/api/sync-sheets")
@app.post("/api/sync-gsheets")
async def sync_google_sheets(request: Request):
  try:
    try:
      body = await request.json()
    except Exception:
      body = {}

    target_url = (
        body.get("webhook_url")
        or request.query_params.get("webhook_url")
        or DEFAULT_GS_WEBHOOK
    ).strip()

    if not target_url:
      return JSONResponse(
          status_code=400, content={"message": "URL Webhook non configurato!"}
      )

    conn = get_db_connection()
    sheets_payload = {}
    total_records = 0

    for tbl, (emoji_name, clean_name) in TAB_MAPPING.items():
      try:
        df = pd.read_sql_query(f"SELECT * FROM {tbl}", conn)
      except Exception:
        df = pd.DataFrame()
      recs = df.fillna("").to_dict(orient="records")
      total_records += len(recs)
      sheets_payload[clean_name] = recs
      sheets_payload[emoji_name] = recs

    conn.close()

    payload = json.dumps({
        "action": "mirror_sync",
        "clear_all": True,
        "sheets": sheets_payload,
    }).encode("utf-8")

    def _sync():
      req = urllib.request.Request(
          target_url,
          data=payload,
          headers={"Content-Type": "application/json"},
      )
      with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()

    await asyncio.to_thread(_sync)
    msg = f"Sincronizzazione completata: {total_records} record specchiati sui 15 fogli di Google Sheets."
    await broadcast_log("SUCCESS", msg)
    return {"status": "ok", "message": msg}

  except Exception as e:
    await broadcast_log("ERROR", f"Errore Google Sheets: {e}")
    return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/action/clean-db")
@app.post("/api/database/clean")
async def clean_database():
  try:
    conn = get_db_connection()
    conn.execute("PRAGMA foreign_keys = OFF;")
    for t in TABELLE_SISTEMA:
      try:
        conn.execute(f"DELETE FROM {t};")
      except sqlite3.OperationalError:
        pass
    conn.commit()
    try:
      conn.execute("VACUUM;")
    except Exception:
      pass
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.close()

    msg = "Database locale azzerato con successo: 0 record residui."
    await broadcast_log("WARN", msg)
    return {"status": "ok", "message": msg}
  except Exception as e:
    return JSONResponse(status_code=500, content={"error": str(e)})


# ==============================================================================
# 15. AVVIO SERVER FASTAPI
# ==============================================================================
if __name__ == "__main__":
  import uvicorn

  uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
