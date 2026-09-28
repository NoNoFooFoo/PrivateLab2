import argparse
import asyncio
import os
import sys

# Ancoraggio percorsi alla cartella scrapers e alla root del progetto
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from harvester import run_harvester_standalone
from worker import run_worker_standalone

if __name__ == "__main__":
  parser = argparse.ArgumentParser(
      description="Pipeline Master Registro Imprese (Harvester & Worker)"
  )
  parser.add_argument(
      "--action",
      choices=["harvest", "worker", "both"],
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
  args = parser.parse_args()

  tipo_target = args.tipo.upper()

  # 1. Esecuzione Fase 1: Harvester
  if args.action == "harvest":
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
