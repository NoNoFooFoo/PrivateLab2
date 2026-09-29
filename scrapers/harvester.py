import argparse
import asyncio
import csv
from datetime import datetime
import io
import json
import os
import random
import re
import sqlite3
import sys
import urllib.parse
import zipfile
from dotenv import load_dotenv
from playwright.async_api import async_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

for env_candidate in [
    os.path.join(PROJECT_ROOT, ".env"),
    os.path.join(PROJECT_ROOT, "env.taziovettori"),
]:
  if os.path.exists(env_candidate):
    load_dotenv(env_candidate)
    break
else:
  load_dotenv()

try:
  from scrapers.proxy_manager import (
      connect_zenrows_browser,
      get_playwright_proxy_config,
      get_zenrows_api_keys,
  )
except ModuleNotFoundError:
  from proxy_manager import (
      connect_zenrows_browser,
      get_playwright_proxy_config,
      get_zenrows_api_keys,
  )

DB_PATH = os.getenv("DB_PATH", os.path.join(PROJECT_ROOT, "data", "database.db"))
if not os.path.isabs(DB_PATH):
  DB_PATH = os.path.join(PROJECT_ROOT, DB_PATH)

ZENROWS_API_KEYS = get_zenrows_api_keys()

# Mappa Ufficiale Registro Imprese CCIAA (0 - 19)
MAPPA_REGIONI = {
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]


def log(level: str, msg: str):
  ts = datetime.now().strftime("%H:%M:%S")
  print(f"[{ts}] [{level.upper()}] {msg}", flush=True)


def clean_val(val, default="NON RILEVATO"):
  if not val:
    return default
  val_str = str(val).replace("\r", " ").replace("\n", " ").strip()
  val_str = re.sub(r"\s+", " ", val_str)
  return val_str if val_str else default


def get_db():
  conn = sqlite3.connect(DB_PATH, timeout=30.0)
  conn.execute("PRAGMA journal_mode = WAL;")
  conn.execute("""
        CREATE TABLE IF NOT EXISTS coda (
            url TEXT PRIMARY KEY,
            denominazione TEXT,
          cf TEXT,
            tipo TEXT DEFAULT 'STARTUP',
            regione TEXT,
            stato TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
  cur = conn.execute("PRAGMA table_info(coda)")
  if "cf" not in {row[1] for row in cur.fetchall()}:
    conn.execute("ALTER TABLE coda ADD COLUMN cf TEXT")
  conn.commit()
  return conn


async def neutralize_f5_shield(page):
  try:
    await page.add_style_tag(content="""
            iframe[id*='TSBrPFrame'], iframe[name*='TSBrPFrame'] {
                pointer-events: none !important;
                visibility: hidden !important;
                z-index: -9999 !important;
            }
        """)
  except Exception:
    pass


async def wait_for_search_page_change(
  page, selector: str, previous_cards: list, timeout_ms: int = 20000
):
  await page.wait_for_function(
      """({selector, previous}) => {
          const cards = Array.from(document.querySelectorAll(selector));
          if (!cards.length) return false;
          const current = cards.map(card => card.innerHTML);
          return JSON.stringify(current) !== JSON.stringify(previous);
      }""",
        arg={"selector": selector, "previous": previous_cards},
      timeout=timeout_ms,
  )


async def find_card_by_cf(cards, cf_digits: str):
  for idx in range(await cards.count()):
    card = cards.nth(idx)
    card_text = await card.inner_text()
    match_cf = re.search(
        r"Codice fiscale\s*([A-Z0-9]{11,16})", card_text, re.IGNORECASE
    )
    if match_cf and re.sub(r"[^A-Z0-9]", "", match_cf.group(1).upper()) == cf_digits:
      return card
  return None


async def search_card_by_piva(page, search_input, search_button, cf_digits: str):
  try:
    async with page.expect_response(
        lambda response: "parolaChiaveFld" in response.url, timeout=6000
    ) as response_info:
      await search_input.fill(cf_digits)
      await search_input.press("Tab")
    response = await response_info.value
    log(
        "INFO",
        f"[PIVA] Campo CF aggiornato via Wicket: HTTP {response.status} "
        f"({response.headers.get('content-type', 'tipo sconosciuto')}).",
    )
  except Exception as error:
    log(
        "WARN",
        f"[PIVA] ACK Wicket del CF {cf_digits[-4:]} non ricevuto: "
        f"{type(error).__name__}: {error}; scarto questo tentativo.",
    )
    return None

  if not response.ok:
    log("WARN", f"[PIVA] ACK Wicket del campo CF non riuscito: HTTP {response.status}.")
    return None

  log("INFO", "[PIVA] Invio ricerca con il binding click Wicket.")
  try:
    async with page.expect_response(
        lambda candidate: "searchBtn" in candidate.url, timeout=10000
    ) as response_info:
      await search_button.click(force=True)
    response = await response_info.value
  except Exception as error:
    log(
      "WARN",
      f"[PIVA] Risposta Wicket della ricerca non ricevuta: "
      f"{type(error).__name__}: {error}; scarto questo tentativo.",
    )
    return None

  content_type = response.headers.get("content-type", "tipo sconosciuto")
  log(
      "INFO",
      f"[PIVA] Ricerca Wicket: HTTP {response.status} ({content_type}).",
  )
  if not response.ok:
    log("WARN", f"[PIVA] Ricerca Wicket fallita: HTTP {response.status}.")
    return None
  if "xml" not in content_type.lower():
    log(
        "WARN",
        "[PIVA] Risposta Wicket inattesa per la ricerca "
        f"(HTTP {response.status}, content-type={content_type}); "
        "interrompo questo tentativo senza inviare altri submit.",
    )
    return None

  cards = page.locator(".searchCompanyCard")
  try:
    await page.wait_for_function(
        """({selector, cf}) => Array.from(document.querySelectorAll(selector)).some(card => {
          const match = card.innerText.match(/Codice fiscale\\s*([A-Z0-9]{11,16})/i);
          return match && match[1].replace(/[^A-Z0-9]/gi, '').toUpperCase() === cf;
        })""",
        arg={"selector": ".searchCompanyCard", "cf": cf_digits},
        timeout=8000,
    )
  except Exception:
    log("WARN", f"[PIVA] Nessuna card con CF esatto {cf_digits[-4:]} dopo la risposta Wicket.")
    return None

  match_card = await find_card_by_cf(cards, cf_digits)
  if not match_card:
    log("WARN", f"[PIVA] CF {cf_digits[-4:]} non presente nelle card aggiornate.")
    return None

  log("SUCCESS", "[PIVA] CF cercato verificato con click Wicket.")
  return match_card


# ==============================================================================
# INVIO RICERCA (METODO ORIGINALE COLLAUDATO + PROTEZIONE CONTRO TIMEOUT)
# ==============================================================================
async def execute_search_sequence(
  page,
  current_regione: str,
  data_val: str,
  tipo_clean: str,
  retry_attempt: int = 0,
) -> bool:
  CARD_SELECTOR = ".searchCompanyCard"

  # 1. Caricamento Home Portale
  await page.goto(
      "https://startup.registroimprese.it/isin/home",
      wait_until="domcontentloaded",
      timeout=35000,
  )
  await asyncio.sleep(1.5)

  # Controllo anti-blocco immediato
  page_title = await page.title()
  page_text = await page.inner_text("body")
  if "rejected" in page_text.lower() or "forbidden" in page_title.lower():
    log("ERROR", f"Accesso bloccato dal WAF F5 (Titolo: {page_title}). Verifica proxy residenziali.")
    if retry_attempt < 2:
      await asyncio.sleep(2 * (retry_attempt + 1))
      return await execute_search_sequence(
          page, current_regione, data_val, tipo_clean, retry_attempt + 1
      )
    return False

  await neutralize_f5_shield(page)

  # 2. Cookie Banner (Dispatch originale)
  try:
    btn = page.locator("button:has-text('Accetta'), .cookie-consent button").first
    if await btn.count() > 0:
      await btn.dispatch_event("click")
      await asyncio.sleep(0.3)
  except Exception:
    pass

  # 3. Selezione Target (STARTUP o PMI)
  try:
    target_box = page.locator(f".field:has-text('{tipo_clean}') .ui.checkbox").first
    if await target_box.count() > 0:
      cls_box = await target_box.get_attribute("class") or ""
      if "checked" not in cls_box:
        await target_box.dispatch_event("click")
        await asyncio.sleep(0.3)
  except Exception:
    pass

  # 4. Apertura Ricerca Avanzata (Dispatch originale)
  adv_lnk = page.locator("#filtroAvanzatoLnk, a:has-text('ricerca avanzata')").first
  if await adv_lnk.count() > 0:
    await adv_lnk.dispatch_event("click")
    await asyncio.sleep(0.5)

  # 5. Selezione Regione (Dispatch originale)
  dropdown = page.locator("#valueRegione .ui.dropdown, #id2, #id3e").first
  if await dropdown.count() > 0:
    await dropdown.dispatch_event("click")
    await asyncio.sleep(0.4)

    sel_item = f"#valueRegione .item[data-value='{data_val}']"
    item = page.locator(sel_item).first
    if await item.count() > 0:
      await item.dispatch_event("click")
      log("SUCCESS", f"Regione '{current_regione}' [data-value={data_val}] impostata.")
    else:
      inp = page.locator("#valueRegione input.search").first
      if await inp.count() > 0:
        await inp.fill(current_regione)
        await page.keyboard.press("Enter")
    await asyncio.sleep(0.3)

  # 6. Invio Ricerca Wicket (Selettore protetto con fallback pulito)
  log("INFO", f"Invio ricerca per {current_regione} (ID: {data_val})...")
  await neutralize_f5_shield(page)

  # Cerca prima l'elemento diretto, poi il fallback generico senza causare eccezioni
  cerca_btn = page.locator("a.searchBtnVetrina, a#id21, a#id5f").first
  if await cerca_btn.count() > 0:
    await cerca_btn.dispatch_event("click")
  else:
    # Se il pulsante non ha quella classe specifica, premi Enter sul form
    await page.keyboard.press("Enter")

  # Attesa risultati con timeout calibrato
  try:
    await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    return True
  except Exception:
    # Fallback con click nativo se il dispatch non ha risposto entro 20s
    try:
      btn_retry = page.locator("a.searchBtnVetrina, a#id21").first
      if await btn_retry.count() > 0 and await btn_retry.is_visible():
        await btn_retry.click(force=True, timeout=5000)
        await page.wait_for_selector(CARD_SELECTOR, timeout=20000)
        return True
    except Exception:
      pass

  log("WARN", f"Nessuna scheda comparsa per {current_regione} dopo l'invio ricerca.")
  if retry_attempt < 2:
    log("WARN", f"Nuovo tentativo ricerca per {current_regione} ({retry_attempt + 1}/2).")
    await asyncio.sleep(2 * (retry_attempt + 1))
    return await execute_search_sequence(
        page, current_regione, data_val, tipo_clean, retry_attempt + 1
    )
  return False


# ==============================================================================
# ESTRAZIONE LINK CAMERALE CON TOKEN WICKET
# ==============================================================================
async def extract_camerale_url(page, card, row_num: int, ajax_map: dict, current_page_num: int) -> str:
  ajax_binding = ajax_map.get(row_num)
  card_name = clean_val(
      await card.locator("h5 a, h3 span, h5").first.inner_text(timeout=2500), ""
  )

  # Method 1: authenticated in-page request using the binding's declared method.
  if ajax_binding:
    ajax_endpoint, ajax_method = ajax_binding
    ajax_method = ajax_method if ajax_method in {"GET", "POST"} else "GET"
    try:
      result = await page.evaluate("""async ({endpoint, method, pageNum}) => {
          try {
                const base = (window.Wicket && window.Wicket.Ajax && window.Wicket.Ajax.baseUrl)
                  ? window.Wicket.Ajax.baseUrl.replaceAll('&amp;', '&')
                  : `search?${pageNum}`;
                const response = await window.fetch(endpoint, {
                  method,
                  credentials: 'same-origin',
                  headers: {
                    'Wicket-Ajax': 'true',
                    'Wicket-Ajax-BaseURL': base,
                    'X-Requested-With': 'XMLHttpRequest'
                  }
                });
                return {
                  status: response.status,
                  contentType: response.headers.get('content-type') || '',
                  url: response.url,
                  text: await response.text()
                };
          } catch (err) {
              return {error: String(err), text: ''};
          }
            }""", {
              "endpoint": ajax_endpoint,
              "method": ajax_method,
              "pageNum": current_page_num,
            })
      if result.get("status") is not None:
        log(
            "INFO",
            f"[LINK] Binding Wicket {ajax_method}: HTTP {result['status']} "
            f"({result.get('contentType') or 'content-type assente'}).",
        )
      if result.get("error"):
        log("WARN", f"[LINK] Fetch in pagina fallita: {result['error']}")
      target_url = _detail_url_from_response(
          result.get("text", ""), result.get("url", page.url)
      )
      if target_url:
        log("SUCCESS", "[LINK] URL scheda recuperata dal binding Wicket.")
        return target_url
      log("WARN", "[LINK] Binding Wicket senza redirect a dettaglioStartup.")
    except Exception as error:
      log("WARN", f"[LINK] Fetch in pagina fallita: {type(error).__name__}: {error}")

  else:
    log("WARN", f"[LINK] Binding buttonLink non trovato per la card {card_name!r}.")

  # Method 2: click the rendered SCOPRI control in a disposable ZenRows page.
  probe_page = None
  try:
    if page.url.startswith("http"):
      probe_page = await page.context.new_page()
      await probe_page.goto(page.url, wait_until="domcontentloaded", timeout=30000)
      probe_cards = probe_page.locator(".searchCompanyCard")
      probe_card = None
      for idx in range(await probe_cards.count()):
        candidate_card = probe_cards.nth(idx)
        candidate_name = clean_val(
            await candidate_card.locator("h5 a, h3 span, h5").first.inner_text(timeout=2500),
            "",
        )
        if candidate_name.casefold() == card_name.casefold():
          probe_card = candidate_card
          break
      if probe_card:
        btn = probe_card.locator(
            "div.button:has-text('SCOPRI'), h5 a.link, a.link"
        ).first
        async with probe_page.expect_response(
            lambda response: "buttonLink" in response.url
            or "dettaglioStartup" in response.url,
            timeout=10000,
        ) as resp_info:
          await btn.click(force=True, timeout=5000)
        resp = await resp_info.value
        log(
            "INFO",
            f"[LINK] Click SCOPRI: HTTP {resp.status} "
            f"({resp.headers.get('content-type', 'content-type assente')}).",
        )
        try:
          response_text = await resp.text()
        except Exception as error:
          log("WARN", f"[LINK] Body Wicket non leggibile dopo click: {error}")
          response_text = ""
        target_url = _detail_url_from_response(response_text, resp.url)
        if not target_url and "dettaglioStartup" in probe_page.url:
          target_url = probe_page.url
        if target_url:
          log("SUCCESS", "[LINK] URL scheda recuperata con click SCOPRI.")
          return target_url
      else:
        log("WARN", f"[LINK] Card {card_name!r} non trovata nella pagina di fallback.")
  except Exception as error:
    log("WARN", f"[LINK] Click SCOPRI fallito: {type(error).__name__}: {error}")
  finally:
    if probe_page:
      try:
        await probe_page.close()
      except Exception:
        pass

  # Method 3: a real detail href, if a portal revision exposes one.
  try:
    card_html = await card.inner_html()
    m_static = re.search(r"(\.?\/?dettaglioStartup\?[^'\"<>\s]+)", card_html)
    if m_static:
      raw_rel = m_static.group(1).replace("&amp;", "&").replace("./", "")
      target_url = urllib.parse.urljoin("https://startup.registroimprese.it/isin/", raw_rel)
      log("SUCCESS", "[LINK] URL scheda recuperata dal markup statico.")
      return target_url
  except Exception as error:
    log("WARN", f"[LINK] Lettura href statico fallita: {type(error).__name__}: {error}")

  log("ERROR", f"[LINK] Esauriti i fallback per {card_name!r}.")
  return None


def _detail_url_from_response(response_text: str, base_url: str) -> str:
  redirect = re.search(
      r"<redirect><!\[CDATA\[(.*?)\]\]></redirect>", response_text or "", re.DOTALL
  )
  if redirect:
    raw_url = redirect.group(1).replace("&amp;", "&").replace("./", "")
    return urllib.parse.urljoin(
        "https://startup.registroimprese.it/isin/", raw_url
    )
  match = re.search(
      r"(?:https?://startup\.registroimprese\.it/isin/)?(?:\.?/)?dettaglioStartup\?[^'\"<>\s]+",
      response_text or "",
  )
  if match:
    return urllib.parse.urljoin(base_url, match.group(0).replace("&amp;", "&"))
  return ""


# ==============================================================================
# SCANSIONE REGIONE MASTER
# ==============================================================================
async def harvest_region(
    page,
    current_regione: str,
    data_val: str,
    limit: int,
    delay_sec: float,
    tipo: str = "STARTUP",
) -> int:
  collected = 0
  conn = get_db()
  cur = conn.cursor()

  CARD_SELECTOR = ".searchCompanyCard"
  tipo_clean = tipo.upper().strip()
  log("INFO", f"=== Scansione [{tipo_clean}] Regione: {current_regione} (ID: {data_val}) ===")

  # 1. Esecuzione sequenza di ricerca originale protetta
  ok = await execute_search_sequence(page, current_regione, data_val, tipo_clean)
  if not ok:
    conn.close()
    return 0

  await asyncio.sleep(1.0)

  current_page_num = 1
  pagination_restarts = 0
  while True:
    if page.is_closed():
      break

    await neutralize_f5_shield(page)
    cards = page.locator(CARD_SELECTOR)
    cards_count = await cards.count()

    if cards_count == 0:
      log("INFO", f"Nessuna scheda presente per {current_regione}.")
      break

    log("INFO", f"[{current_regione} - Pagina {current_page_num}] Trovate {cards_count} schede.")

    # Mappa degli endpoint AJAX Wicket della pagina corrente
    page_html = await page.content()
    ajax_map = {}
    for config_text in re.findall(r"Wicket\.Ajax\.ajax\((\{[^)]*\})\)", page_html):
      try:
        binding = json.loads(config_text)
      except json.JSONDecodeError:
        continue
      match_row = re.search(r"resultRow-(\d+)", binding.get("u", ""))
      if not match_row:
        continue
      row_idx = int(match_row.group(1))
      endpoint = binding["u"]
      ajax_method = str(binding.get("m", "GET")).upper()
      if ajax_method not in {"GET", "POST"}:
        ajax_method = "GET"
      if "buttonLink" in endpoint:
        ajax_map[row_idx] = (endpoint, ajax_method)
      elif row_idx not in ajax_map:
        ajax_map[row_idx] = (endpoint, ajax_method)

    for i in range(cards_count):
      if limit != -1 and collected >= limit:
        break
      if page.is_closed():
        break

      try:
        card = cards.nth(i)
        name = clean_val(await card.locator("h5 a, h3 span, h5").first.inner_text(timeout=2500))

        cur.execute(
            """
              SELECT 1 FROM coda
              WHERE denominazione = ? COLLATE NOCASE
                AND UPPER(tipo) = ? AND UPPER(regione) = ?
              LIMIT 1
            """,
            (name, tipo_clean, current_regione.upper()),
        )
        if cur.fetchone():
          log("INFO", f"Target già presente in coda, ignorato: {name}")
          continue

        await card.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.3 * delay_sec, 0.6 * delay_sec))

        row_num = i + 1
        camerale_url = await extract_camerale_url(page, card, row_num, ajax_map, current_page_num)

        if camerale_url and "dettaglioStartup" in camerale_url:
          clean_url = re.sub(r"dettaglioStartup\?\d+&", "dettaglioStartup?", camerale_url)
          cur.execute(
              """
                INSERT OR IGNORE INTO coda (url, denominazione, tipo, regione, stato)
                VALUES (?, ?, ?, ?, 'PENDING')
              """,
              (clean_url, name, tipo_clean, current_regione),
          )
          if cur.rowcount:
            conn.commit()
            collected += 1
            log("SUCCESS", f"Target #{collected} [{tipo_clean}] salvato in coda: {name}")
          else:
            log("INFO", f"Target già presente in coda, ignorato: {name}")
        else:
          log("WARN", f"Dossier non disponibile per: {name} (Link non recuperato)")

        await asyncio.sleep(random.uniform(0.4 * delay_sec, 0.8 * delay_sec))

      except Exception as e:
        log("ERROR", f"Errore su scheda #{i+1}: {e}")

    if limit != -1 and collected >= limit:
      break
    # Wicket retains the old cards during AJAX navigation, so wait for their content to change.
    try:
      next_btn = page.locator("a[rel='next'], a[title='Go to next page']").first
      if not page.is_closed() and await next_btn.count() > 0 and await next_btn.is_visible():
        previous_cards = await cards.evaluate_all(
            "elements => elements.map(element => element.innerHTML)"
        )
        next_page_num = current_page_num + 1
        log("INFO", f"[{current_regione}] Richiesta Pagina {next_page_num}...")
        await next_btn.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.8, 1.4))
        async with page.expect_response(
          lambda response: (
            "navigatorTop-next" in response.url
            or "navigatorBottom-next" in response.url
          ),
            timeout=20000,
        ) as response_info:
          await next_btn.click(force=True, timeout=5000)
        response = await response_info.value
        if not response.ok:
          raise RuntimeError(f"Wicket ha risposto HTTP {response.status}")
        await wait_for_search_page_change(page, CARD_SELECTOR, previous_cards)
        current_page_num = next_page_num
        pagination_restarts = 0
      else:
        log("INFO", f"Regione {current_regione} completata: nessuna pagina successiva.")
        break
    except Exception as e:
      log("WARN", f"Paginazione interrotta in {current_regione} dopo la pagina {current_page_num}: {e}")
      if pagination_restarts >= 2:
        break
      pagination_restarts += 1
      log("WARN", f"Riavvio ricerca per recuperare la paginazione (tentativo {pagination_restarts}/2).")
      try:
        if not await execute_search_sequence(page, current_regione, data_val, tipo_clean):
          break
      except Exception as restart_error:
        log("ERROR", f"Ripristino ricerca fallito: {restart_error}")
        break
      current_page_num = 1
      continue

  conn.close()
  return collected


def load_piva_candidates(
  zip_path: str,
  limit: int,
  regione: str = "ALL",
  piva_cf: str = None,
) -> list:
  with sqlite3.connect(DB_PATH) as conn:
    startup_table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'startup'"
    ).fetchone()

  if not startup_table_exists:
    from app import DB_PATH as APP_DB_PATH, init_db_schema

    if os.path.abspath(APP_DB_PATH) != os.path.abspath(DB_PATH):
      raise RuntimeError(
          "Lo schema Startup non esiste e app e Harvester puntano a database diversi."
      )
    init_db_schema()

  with zipfile.ZipFile(zip_path) as archive:
    csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
    if not csv_names:
      raise ValueError("Nello ZIP non è presente un CSV con la lista startup.")
    csv_name = next((name for name in csv_names if "startup" in name.lower()), csv_names[0])
    csv_text = archive.read(csv_name).decode("cp1252")

  reader = csv.reader(io.StringIO(csv_text), delimiter="|")
  headers = next(reader, None)
  if not headers:
    raise ValueError("Il CSV della lista startup è vuoto.")
  normalized_headers = [re.sub(r"\s+", " ", header.strip().lower()) for header in headers]
  piva_idx = next(
      (
          idx for idx, header in enumerate(normalized_headers)
          if header == "codice fiscale" or "partita iva" in header or header in {"p.iva", "piva"}
      ),
      None,
  )
  name_idx = next(
      (
          idx for idx, header in enumerate(normalized_headers)
          if header.startswith("denominazione") or header.startswith("ragione sociale")
      ),
      None,
  )
  region_idx = next(
      (idx for idx, header in enumerate(normalized_headers) if header == "regione"),
      None,
  )
  if piva_idx is None or name_idx is None:
    raise ValueError("Il CSV deve contenere denominazione e codice fiscale/P.IVA.")

  region_filter = (regione or "ALL").strip().upper().replace("-", " ")
  if region_filter not in {"", "ALL", "TUTTE", "NAZIONALE"}:
    if region_idx is None:
      raise ValueError("Il CSV non contiene la colonna Regione richiesta dal filtro.")
    region_filter = MAPPA_REGIONI.get(region_filter, region_filter).replace("-", " ")

  target_cf = re.sub(r"[^A-Z0-9]", "", (piva_cf or "").strip().upper())
  if target_cf.startswith("IT"):
    target_cf = target_cf[2:]

  conn = get_db()
  cur = conn.cursor()
  existing_cfs = {
      re.sub(r"[^A-Z0-9]", "", str(row[0]).upper())
      for row in cur.execute("SELECT cf FROM startup WHERE cf IS NOT NULL")
  }
  existing_cfs.update(
      re.sub(r"[^A-Z0-9]", "", str(row[0]).upper())
      for row in cur.execute(
          "SELECT cf FROM coda WHERE UPPER(tipo) = 'STARTUP' "
          "AND UPPER(stato) IN ('PENDING', 'COMPLETED') AND cf IS NOT NULL"
      )
      if row[0]
  )
  seen_cfs = set()
  total_rows = 0
  invalid_rows = 0
  processed_rows = 0
  duplicate_rows = 0
  queued_rows = 0
  candidates = []
  for row in reader:
    if not row:
      continue
    total_rows += 1
    if len(row) <= max(piva_idx, name_idx):
      invalid_rows += 1
      continue
    region = row[region_idx].strip() if region_idx is not None and len(row) > region_idx else ""
    if region_filter and region_filter not in {"ALL", "TUTTE", "NAZIONALE"}:
      if region.upper().replace("-", " ") != region_filter:
        continue
    raw_cf = re.sub(r"[^A-Z0-9]", "", row[piva_idx].strip().upper())
    if raw_cf.startswith("IT"):
      raw_cf = raw_cf[2:]
    if not ((len(raw_cf) == 11 and raw_cf.isdigit()) or (len(raw_cf) == 16 and raw_cf.isalnum())):
      invalid_rows += 1
      continue
    if target_cf and raw_cf != target_cf:
      continue
    cf = f"IT{raw_cf}"
    if cf in seen_cfs:
      duplicate_rows += 1
      continue
    seen_cfs.add(cf)
    if cf in existing_cfs:
      processed_rows += 1
      continue
    name = re.sub(r"\s+", " ", row[name_idx].strip())
    candidates.append({"cf": cf, "name": name, "region": region or "ITALIA"})
    queued_rows += 1
    if limit != -1 and len(candidates) >= limit:
      break
  conn.close()
  log(
      "INFO",
      f"[PIVA] CSV righe lette={total_rows}; CF invalidi={invalid_rows}; "
      f"già processati/in coda={processed_rows}; duplicati CSV={duplicate_rows}; "
      f"nuovi candidati selezionati={len(candidates)}.",
  )
  return candidates


async def harvest_piva_list(
    page,
    zip_path: str,
    limit: int,
    delay_sec: float,
    tipo: str,
    regione: str = "ALL",
    piva_cf: str = None,
) -> int:
  candidates = load_piva_candidates(zip_path, limit, regione, piva_cf)
  if not candidates:
    log("WARN", "Nessuna P.IVA nuova da cercare nel CSV.")
    return 0

  log("INFO", f"=== Ricerca per P.IVA: {len(candidates)} startup candidate ===")
  search_input = page.locator('input[name="parolaChiaveFld"]').first
  search_button = page.locator("a.searchBtnVetrina").first
  form_ready = False
  for attempt in range(2):
    await page.goto(
        "https://startup.registroimprese.it/isin/home",
        wait_until="domcontentloaded",
        timeout=35000,
    )
    await neutralize_f5_shield(page)
    try:
      await search_input.wait_for(state="attached", timeout=10000)
      await search_button.wait_for(state="attached", timeout=5000)
      form_ready = True
      break
    except Exception:
      if attempt == 0:
        log("WARN", "Form P.IVA non ancora presente; ricarico una volta la pagina.")

  if not form_ready:
    page_html = await page.content()
    log(
        "ERROR",
        "Form Wicket P.IVA assente dopo due caricamenti "
        f"(titolo={await page.title()!r}, html={len(page_html)} byte).",
    )
    return 0

  target_box = page.locator(f".field:has-text('{tipo}') .ui.checkbox").first
  if await target_box.count() and "checked" not in (await target_box.get_attribute("class") or ""):
    await target_box.dispatch_event("click")

  conn = get_db()
  cur = conn.cursor()
  collected = 0
  try:
    for candidate in candidates:
      if page.is_closed() or (limit != -1 and collected >= limit):
        break
      cf_digits = candidate["cf"][2:]
      try:
        match_card = await search_card_by_piva(
            page, search_input, search_button, cf_digits
        )
        if match_card is None:
          log("WARN", f"Nessuna card verificabile per {candidate['name']} [{candidate['cf']}]; passo al CF successivo.")
          await asyncio.sleep(max(delay_sec, 0.5))
          continue

        name = clean_val(
            await match_card.locator("h5 a, h3 span, h5").first.inner_text(timeout=2500)
        )
        page_html = await page.content()
        ajax_map = {}
        for config_text in re.findall(r"Wicket\.Ajax\.ajax\((\{[^)]*\})\)", page_html):
          try:
            binding = json.loads(config_text)
          except json.JSONDecodeError:
            continue
          if "buttonLink" not in binding.get("u", ""):
            continue
          row_match = re.search(r"resultRow-(\d+)", binding["u"])
          if row_match:
            row_idx = int(row_match.group(1))
            ajax_method = str(binding.get("m", "GET")).upper()
            ajax_map[row_idx] = (binding["u"], ajax_method if ajax_method in {"GET", "POST"} else "GET")

        row_num = 1
        camerale_url = await extract_camerale_url(
            page, match_card, row_num, ajax_map, 1
        )
        if not camerale_url or "dettaglioStartup" not in camerale_url:
          log("WARN", f"Link dossier non recuperato per {name}.")
          await asyncio.sleep(max(delay_sec, 0.5))
          continue

        clean_url = re.sub(r"dettaglioStartup\?\d+&", "dettaglioStartup?", camerale_url)
        cur.execute(
            """
              INSERT OR IGNORE INTO coda (url, denominazione, cf, tipo, regione, stato)
              VALUES (?, ?, ?, ?, ?, 'PENDING')
            """,
            (clean_url, name, candidate["cf"], tipo, candidate["region"]),
        )
        inserted = cur.rowcount == 1
        if not cur.rowcount:
          cur.execute(
              "UPDATE coda SET cf = ?, stato = 'PENDING' "
              "WHERE url = ? AND UPPER(stato) = 'FAILED'",
              (candidate["cf"], clean_url),
          )
          if not cur.rowcount:
            cur.execute(
                "UPDATE coda SET cf = ? WHERE url = ? "
                "AND (cf IS NULL OR cf = '') AND UPPER(stato) IN ('PENDING', 'COMPLETED')",
                (candidate["cf"], clean_url),
            )
        if cur.rowcount:
          conn.commit()
          collected += 1
          action = "accodato" if inserted else "agganciato alla coda esistente"
          log("SUCCESS", f"P.IVA verificata; target #{collected} {action}: {name} [{candidate['cf']}]")
        else:
          log("INFO", f"Dossier già presente in coda: {name}")
      except Exception as error:
        log("WARN", f"Ricerca P.IVA non completata per {candidate['name']}: {error}")
        raise RuntimeError(
            f"Ricerca P.IVA interrotta al primo errore per {candidate['cf']}: {error}"
        ) from error
      await asyncio.sleep(max(delay_sec, 0.5))
  finally:
    conn.close()
  return collected


# ==============================================================================
# RUNNER STANDALONE CON ZENROWS PROXY NATIVO
# ==============================================================================
async def run_harvester_standalone(
    regione: str,
    limit: int,
    delay_sec: float,
    proxy_mode: str,
    tipo: str,
    piva_zip: str = None,
    piva_cf: str = None,
):
  log(
      "INFO",
      f"=== AVVIO HARVESTER STEALTH (Tipo: {tipo.upper()}, Target: {limit},"
      f" Regione: {regione}, Route: {proxy_mode}) ===",
  )

  if piva_zip and not ZENROWS_API_KEYS:
    log("ERROR", "[PIVA PIPELINE] ZENROWS_API_KEY mancante: la modalità P.IVA richiede ZenRows.")
    raise RuntimeError("ZENROWS_API_KEY mancante per la pipeline P.IVA")

  use_zenrows = "zenrows" in proxy_mode.lower() and bool(ZENROWS_API_KEYS)
  if piva_zip and not use_zenrows:
    log("ERROR", "[PIVA PIPELINE] Routing non valido: richiesto ZenRows.")
    raise RuntimeError("La pipeline P.IVA accetta esclusivamente il routing ZenRows")
  proxy_cfg = None if use_zenrows else get_playwright_proxy_config(proxy_mode)

  async with async_playwright() as p:
    browser = None

    if use_zenrows:
      connection_attempts = 3 if piva_zip else 1
      for attempt in range(1, connection_attempts + 1):
        try:
          if attempt == 1:
            log("INFO", "🌐 Connessione a Browser Residenziale ZenRows (WAF Bypass)...")
          else:
            log("INFO", f"[PIVA PIPELINE] Nuovo tentativo ZenRows {attempt}/{connection_attempts}.")
          browser = await connect_zenrows_browser(p)
          # USA DIRETTAMENTE IL BROWSER NATIVO SENZA CREARE NEW_CONTEXT CHE DISATTIVA IL PROXY
          page = await browser.new_page()
          log("SUCCESS", "✅ Connessione ZenRows stabilita.")
          break
        except Exception as e:
          if browser:
            try:
              await browser.close()
            except Exception:
              pass
            browser = None
          if piva_zip and attempt < connection_attempts:
            log(
                "WARN",
                f"[PIVA PIPELINE] Connessione ZenRows fallita ({type(e).__name__}); "
                f"ritento {attempt + 1}/{connection_attempts}.",
            )
            await asyncio.sleep(attempt)
            continue
          if piva_zip:
            log("ERROR", f"[PIVA PIPELINE] Connessione ZenRows fallita; nessun fallback diretto: {type(e).__name__}.")
            raise RuntimeError("Connessione ZenRows non disponibile") from e
          log("WARN", f"ZenRows non disponibile ({e}). Fallback su locale...")
          browser = None
          break

    if not browser:
      if piva_zip:
        log("ERROR", "[PIVA PIPELINE] Browser ZenRows non disponibile; interrompo senza connessione diretta.")
        raise RuntimeError("Browser ZenRows non disponibile")
      try:
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy_cfg,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="it-IT",
            timezone_id="Europe/Rome",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)
        page = await context.new_page()
      except Exception as err_launch:
        log("ERROR", f"Errore avvio Chromium: {err_launch}")
        return

    totale = 0
    try:
      if piva_zip:
        totale = await harvest_piva_list(
            page, piva_zip, limit, delay_sec, tipo.upper(), "ALL", piva_cf
        )
      else:
        codice_regione = str(regione).strip()
        if codice_regione in ["", "ALL", "TUTTE", "NAZIONALE"]:
          target_codes = [str(i) for i in range(20)]
        elif codice_regione in MAPPA_REGIONI:
          target_codes = [codice_regione]
        else:
          target_codes = [
              k for k, v in MAPPA_REGIONI.items()
              if v.replace("-", " ") == codice_regione.upper().replace("-", " ")
          ]
          if not target_codes:
            target_codes = [str(i) for i in range(20)]
        for code in target_codes:
          if page.is_closed():
            break
          nome_regione = MAPPA_REGIONI[code]
          if limit != -1 and totale >= limit:
            break
          rimanenti = limit - totale if limit != -1 else -1
          c = await harvest_region(page, nome_regione, code, rimanenti, delay_sec, tipo=tipo)
          totale += c
    finally:
      if browser:
        try:
          await browser.close()
        except Exception:
          pass

  if totale == 0 or (limit != -1 and totale < limit):
    target = f"/{limit}" if limit != -1 else ""
    log("WARN", f"=== HARVESTER INCOMPLETO: {totale}{target} link reali salvati in coda ===")
  else:
    log("SUCCESS", f"=== HARVESTER COMPLETATO: {totale} link reali salvati in coda ===")


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description="Harvester Stealth Registro Imprese")
  parser.add_argument("--regione", default="ALL")
  parser.add_argument("--limit", type=int, default=10)
  parser.add_argument("--delay", type=float, default=1.0)
  parser.add_argument("--proxy", default="zenrows,webshare")
  parser.add_argument("--piva-zip", help="ZIP con CSV startup e colonna codice fiscale/P.IVA")
  parser.add_argument("--piva-cf", help="P.IVA/CF esatto da cercare dal CSV")
  parser.add_argument("--tipo", default="STARTUP", choices=["STARTUP", "PMI", "startup", "pmi"])
  args = parser.parse_args()

  asyncio.run(
        run_harvester_standalone(
          args.regione, args.limit, args.delay, args.proxy, args.tipo, args.piva_zip, args.piva_cf
        )
  )
