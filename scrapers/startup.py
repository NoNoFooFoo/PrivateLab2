import argparse
import asyncio
import os
import sqlite3
import sys

# Ancoraggio percorsi alla cartella scrapers e alla root del progetto
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from harvester import load_piva_candidates, run_harvester_standalone
from worker import run_worker_standalone


def log(level: str, message: str):
    from datetime import datetime

    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level.upper()}] {message}", flush=True)


async def run_piva_pipeline_standalone(
        limit: int,
        delay_sec: float,
        piva_zip: str,
        tipo: str = "STARTUP",
):
    from harvester import DB_PATH as HARVESTER_DB_PATH, ZENROWS_API_KEY

    if not ZENROWS_API_KEY:
        raise RuntimeError("ZENROWS_API_KEY mancante: il ciclo richiede ZenRows.")
    if not os.path.isfile(piva_zip):
        raise FileNotFoundError(f"Archivio ministeriale non trovato: {piva_zip}")

    candidates = load_piva_candidates(piva_zip, -1, "ALL")
    if not candidates:
        log("WARN", "[PIPELINE PIVA] Nessun CF nuovo trovato nel CSV ministeriale.")
        return 0

    target = len(candidates) if limit == -1 else max(limit, 0)
    attempt_limit = len(candidates) if limit == -1 else min(len(candidates), max(target * 3, target))
    log(
            "INFO",
            f"[PIPELINE PIVA] CF nuovi={len(candidates)}; obiettivo={target}; "
            f"tentativi massimi={attempt_limit}; routing=ZenRows.",
    )

    processed = 0
    attempted = 0
    for candidate in candidates[:attempt_limit]:
        if processed >= target:
            break
        attempted += 1
        cf = candidate["cf"]
        log(
                "INFO",
                f"[PIPELINE PIVA] Tentativo {attempted}/{attempt_limit}: "
                f"{candidate['name']} [CF finale {cf[-4:]}].",
        )

        try:
            await run_harvester_standalone(
                  regione="ALL",
                    limit=1,
                    delay_sec=delay_sec,
                    proxy_mode="zenrows",
                    tipo=tipo,
                    piva_zip=piva_zip,
                    piva_cf=cf,
            )
        except Exception as error:
            log("ERROR", f"[PIPELINE PIVA] Harvester interrotto: {type(error).__name__}: {error}")
            break

        with sqlite3.connect(HARVESTER_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            matching = conn.execute(
                    "SELECT url, denominazione, regione, stato FROM coda "
                    "WHERE cf = ? AND UPPER(tipo) = ? ORDER BY rowid DESC",
                    (cf, tipo.upper()),
            ).fetchall()

        pending = [row for row in matching if row["stato"].upper() == "PENDING"]
        if not pending:
            if any(row["stato"].upper() == "COMPLETED" for row in matching):
                log("INFO", f"[PIPELINE PIVA] CF {cf[-4:]} già completato; nessun nuovo lavoro.")
            else:
                log("WARN", f"[PIPELINE PIVA] Nessun dossier accodato per {candidate['name']}; passo al CF successivo.")
            continue
        if len(pending) > 1:
            log("ERROR", f"[PIPELINE PIVA] Trovati {len(pending)} URL pending per lo stesso CF; interrompo per evitare duplicati.")
            break

        url = pending[0]["url"]
        log("INFO", f"[PIPELINE PIVA] Link verificato; avvio Worker sul solo URL della startup.")
        try:
            await run_worker_standalone(
                    limit=1,
                    delay_sec=delay_sec,
                    proxy_mode="zenrows",
                    tipo_filter=tipo,
                    only_url=url,
            )
        except Exception as error:
            log("ERROR", f"[PIPELINE PIVA] Worker interrotto: {type(error).__name__}: {error}")
            break

        with sqlite3.connect(HARVESTER_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            queue_row = conn.execute(
                    "SELECT stato, cf FROM coda WHERE url = ?", (url,)
            ).fetchone()
            startup_row = conn.execute(
                    "SELECT cf, url_scheda, xml_raw FROM startup WHERE cf = ?", (cf,)
            ).fetchone()

        if (
                queue_row
                and queue_row["stato"].upper() == "COMPLETED"
                and startup_row
                and startup_row["cf"] == cf
                and startup_row["url_scheda"] == url
                and startup_row["xml_raw"]
                and "<dichiarazione" in startup_row["xml_raw"]
        ):
            processed += 1
            log("SUCCESS", f"[PIPELINE PIVA] Completata {processed}/{target}: {candidate['name']} [{cf}]; XML e URL verificati.")
        else:
            state = queue_row["stato"] if queue_row else "MISSING"
            log("ERROR", f"[PIPELINE PIVA] Verifica post-Worker fallita per CF {cf[-4:]} (coda={state}, XML/CF/URL non coerenti).")

        await asyncio.sleep(max(delay_sec, 0.5))

    if processed == target:
        log("SUCCESS", f"[PIPELINE PIVA] Ciclo completato: {processed} startup verificate su {attempted} candidate tentate.")
    else:
        log("WARN", f"[PIPELINE PIVA] Ciclo incompleto: {processed}/{target} startup verificate dopo {attempted} tentativi.")
    return processed

if __name__ == "__main__":
  parser = argparse.ArgumentParser(
      description="Pipeline Master Registro Imprese (Harvester & Worker)"
  )
  parser.add_argument(
      "--action",
      choices=["harvest", "worker", "both", "piva"],
      default="harvest",
      help="Azione da eseguire: harvest (Fase 1), worker (Fase 2) o both (Pipeline completa)",
  )
  parser.add_argument(
      "--regione",
      default="ALL",
      help="Codice numerico regione 0-19 oppure ALL per tutte le 20 regioni",
  )
  parser.add_argument(
      "--limit",
      type=int,
      default=10,
      help="Limite massimo di schede da elaborare",
  )
  parser.add_argument(
      "--delay",
      type=float,
      default=2.0,
      help="Secondi di pausa tra le richieste",
  )
  parser.add_argument(
      "--proxy",
      default="zenrows,webshare",
      help="Modalità routing proxy (es. zenrows,webshare o direct)",
  )
  parser.add_argument(
      "--tipo",
      default="STARTUP",
      choices=["STARTUP", "PMI", "ALL", "startup", "pmi", "all"],
      help="Target societario da estrarre (STARTUP o PMI)",
  )
  parser.add_argument(
      "--piva-zip",
      default=os.path.join(PROJECT_ROOT, "scrapers", "elenco_startup_ministero", "startup (1).zip"),
      help="Archivio ZIP ministeriale usato dall'azione piva",
  )
  args = parser.parse_args()

  tipo_target = args.tipo.upper()

  if args.action == "piva":
    asyncio.run(
        run_piva_pipeline_standalone(
            args.limit, args.delay, args.piva_zip, tipo_target
        )
    )

  # 1. Esecuzione Fase 1: Harvester
  elif args.action == "harvest":
    asyncio.run(
        run_harvester_standalone(
            regione=args.regione,
            limit=args.limit,
            delay_sec=args.delay,
            proxy_mode=args.proxy,
            tipo=tipo_target,
        )
    )

  # 2. Esecuzione Fase 2: Worker
  elif args.action == "worker":
    asyncio.run(
        run_worker_standalone(
            limit=args.limit,
            delay_sec=args.delay,
            proxy_mode=args.proxy,
            tipo_filter=tipo_target,
        )
    )

  # 3. Esecuzione Sequenziale Completa: Harvest -> Worker
  elif args.action == "both":
    print(
        f"\n=== [PIPELINE MASTER] AVVIO FASE 1: HARVEST LINK [{tipo_target}]"
        " ==="
    )
    asyncio.run(
        run_harvester_standalone(
            regione=args.regione,
            limit=args.limit,
            delay_sec=args.delay,
            proxy_mode=args.proxy,
            tipo=tipo_target,
        )
    )
    print(
        "\n=== [PIPELINE MASTER] FASE 1 COMPLETATA. AVVIO FASE 2: WORKER SCHEDE"
        f" [{tipo_target}] ==="
    )
    asyncio.run(
        run_worker_standalone(
            limit=args.limit,
            delay_sec=args.delay,
            proxy_mode=args.proxy,
            tipo_filter=tipo_target,
        )
    )
    print("\n=== [PIPELINE MASTER] PROCESSO TERMINATO CON SUCCESSO ===")
