import argparse
import asyncio
from datetime import datetime
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
import pandas as pd
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
    from scrapers.groq_ai import analyze_dossier_with_groq
except ModuleNotFoundError:
    from groq_ai import analyze_dossier_with_groq

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
WORKER_OUTPUT_DIR = os.getenv(
    "WORKER_OUTPUT_DIR", os.path.join(PROJECT_ROOT, "output")
)

ZENROWS_API_KEYS = get_zenrows_api_keys()
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "").strip()

INSTITUTIONAL_KEYWORDS = [
    "invitalia", "polo nazionale", "trasferimento tecnologico", "regione",
    "camera di commercio", "ministero", "mimit", "mise", "cdp",
    "cassa depositi", "fondo nazionale", "unione europea", "commissione europea",
    "infocamere", "finlombarda", "fises", "sviluppo campania"
]

CERTIFIER_KEYWORDS = [
    "aruba pec", "namirial", "infocert", "actalis", "intesigroup", "postecom", "trust technologies"
]


def log(level: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level.upper()}] {msg}", flush=True)


def clean_val(val, default="NON RILEVATO"):
    if not val or pd.isna(val):
        return default
    val_str = str(val).replace("\r", " ").replace("\n", " ").strip()
    val_str = re.sub(r"\s+", " ", val_str)
    return val_str if val_str else default


def format_piva(val: str) -> str:
    """Formatta rigorosamente la Partita IVA / CF con il prefisso 'IT'

    per evitare la perdita dello zero iniziale nei database e nei fogli di calcolo.
    """
    if not val or val == "NON RILEVATO":
        return "NON RILEVATO"
    v = str(val).strip().upper()
    if "HTTP" in v or "WWW" in v:
        return "NON RILEVATO"
    clean = re.sub(r"[^A-Z0-9]", "", v)
    if clean == "NONRILEVATO":
        return "NON RILEVATO"
    if clean.startswith("IT") and len(clean) in [13, 18]:
        clean = clean[2:]
    if len(clean) == 11 and clean.isdigit():
        return f"IT{clean}"
    if len(clean) == 16 and clean.isalnum():
        return f"IT{clean}"
    return "NON RILEVATO"


def format_date_str(val: str) -> str:
    if not val or val == "NON RILEVATO":
        return "NON RILEVATO"
    v = str(val).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return v


def is_institutional_entity(name: str) -> bool:
    if not name:
        return False
    nl = name.lower()
    return any(kw in nl for kw in INSTITUTIONAL_KEYWORDS)


def is_certifier_entity(name: str) -> bool:
    if not name:
        return False
    nl = name.lower()
    return any(kw in nl for kw in CERTIFIER_KEYWORDS)


def slugify(text: str) -> str:
    if not text:
        return ""
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[-\s]+", "-", slug)


def normalize_person_name(name: str) -> str:
    if not name or name in ["NON RILEVATO", "NON COMUNICATO"]:
        return ""
    clean = re.sub(r"[^\w\s]", "", name).strip()
    words = clean.split()
    if len(words) >= 2:
        return " ".join(w.capitalize() for w in words)
    return clean.title()


def are_same_person(name1: str, name2: str) -> bool:
    if not name1 or not name2:
        return False
    tokens1 = set(re.findall(r"\b\w+\b", name1.lower()))
    tokens2 = set(re.findall(r"\b\w+\b", name2.lower()))
    if not tokens1 or not tokens2:
        return False
    return tokens1 == tokens2 or tokens1.issubset(tokens2) or tokens2.issubset(tokens1)


def get_bilingual_text(parent, path_it: str, path_en: str, default="NON RILEVATO") -> str:
    """Legge in modo robusto prima il valore italiano e, se vuoto o assente, passa all'inglese."""
    if parent is None:
        return default
    it_val = parent.findtext(path_it)
    if it_val and it_val.strip():
        return it_val.strip()
    en_val = parent.findtext(path_en)
    if en_val and en_val.strip():
        return en_val.strip()
    return default


async def get_text(locator, default="NON RILEVATO"):
    try:
        if await locator.count() > 0:
            text = await locator.first.inner_text(timeout=2000)
            return clean_val(text, default)
    except Exception:
        pass
    return default


def upload_logo_to_imgbb(b64_str: str, api_key: str) -> str:
    if not b64_str or b64_str in ["NON RILEVATO", ""]:
        return "NON RILEVATO"
    if not api_key:
        return "NON RILEVATO"
    try:
        clean_b64 = b64_str.strip()
        if "base64," in clean_b64:
            clean_b64 = clean_b64.split("base64,")[-1]
        data = urllib.parse.urlencode({"key": api_key, "image": clean_b64}).encode("utf-8")
        req = urllib.request.Request("https://api.imgbb.com/1/upload", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            if res_json.get("success"):
                return res_json["data"]["url"]
    except Exception as e:
        log("WARN", f"Upload ImgBB fallito: {e}")
    return "NON RILEVATO"


def ensure_schema(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS enti (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS universita (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS associazioni (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS investitori (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            categoria TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS incubatori (
            id TEXT PRIMARY KEY,
            denominazione TEXT,
            cf TEXT,
            categoria TEXT
        )
    """)

    for t in ["startup", "pmi"]:
        cur.execute(f"PRAGMA table_info({t})")
        existing_cols = [row[1] for row in cur.fetchall()]
        if existing_cols:
            if "xml_raw" not in existing_cols:
                try:
                    cur.execute(f"ALTER TABLE {t} ADD COLUMN xml_raw TEXT")
                except Exception:
                    pass
            if "logo_url" not in existing_cols:
                try:
                    cur.execute(f"ALTER TABLE {t} ADD COLUMN logo_url TEXT")
                except Exception:
                    pass
            if t == "startup":
                for column in ["siti_web_esterni", "innovazione_descrizione"]:
                    if column not in existing_cols:
                        cur.execute(f"ALTER TABLE startup ADD COLUMN {column} TEXT")
                        existing_cols.append(column)
    cur.execute("PRAGMA table_info(qualifiche)")
    qualification_cols = [row[1] for row in cur.fetchall()]
    if qualification_cols and "descrizione_ruolo" not in qualification_cols:
        cur.execute("ALTER TABLE qualifiche ADD COLUMN descrizione_ruolo TEXT")
    conn.commit()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    ensure_schema(conn)
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


def classify_patent_meta(num_reg: str):
    nr = num_reg.strip().upper()
    if nr.startswith("EP") or "EP" in nr:
        return "EPO (European Patent Office)", "Europeo", "Verificabile via API (EPO OPS)"
    elif nr.startswith("WO") or nr.startswith("PCT/"):
        return "WIPO", "Internazionale (PCT)", "Verificabile via API (PATENTSCOPE)"
    elif nr.startswith("D") or re.match(r"^\d{4}/\d{3,6}$", nr):
        return "SIAE (Sezione OLAF)", "Nazionale (Diritto d'Autore)", "Non verificabile (Registro Chiuso)"
    return "UIBM (MIMIT)", "Nazionale (Italia)", "Verificabile (UIBM/EPO)"


def parse_xml_payload(xml_content: str):
    out = {
        "xml_raw": xml_content or "",
        "xml_codice": "NON RILEVATO",
        "data_compilazione": "NON RILEVATO",
        "cf_dichiarato": "NON RILEVATO",
        "denominazione_dichiarata": "NON RILEVATO",
        "logo_b64": "NON RILEVATO",
        "email": "NON RILEVATO",
        "linkedin": "NON RILEVATO",
        "facebook": "NON RILEVATO",
        "video_url": "NON RILEVATO",
        "siti_web_esterni": [],
        "tags": [],
        "macro_settore_cod": "NON RILEVATO",
        "macro_settore_decod": "NON RILEVATO",
        "stadio_cod": "NON RILEVATO",
        "stadio_decod": "NON RILEVATO",
        "presentazione_it": "NON RILEVATO",
        "presentazione_en": "NON RILEVATO",
        "prodotto_stato_cod": "NON RILEVATO",
        "prodotto_stato_decod": "NON RILEVATO",
        "prodotto_desc_it": "NON RILEVATO",
        "prodotto_desc_en": "NON RILEVATO",
        "team_stato_cod": "NON RILEVATO",
        "team_stato_decod": "NON RILEVATO",
        "canale_diretto": "NO",
        "canale_gdo": "NO",
        "canale_ecommerce": "NO",
        "canale_agenti": "NO",
        "mercato_italia": "NON RILEVATO",
        "mercato_estero": "NON RILEVATO",
        "business_model": "NON RILEVATO",
        "concorrenza": "NON RILEVATO",
        "innovazione_desc": "NON RILEVATO",
        "has_pi": "NO",
        "brevetto_xml": None,
        "incubatore_accelerato": "NO",
        "incubatore_nome": "NON RILEVATO",
        "incubatore_cf": "NON RILEVATO",
        "associazioni": [],
        "universita": [],
        "enti": [],
        "interessi": [],
        "soggetti_xml": [],
        "finanziamenti": [],
    }

    if not xml_content:
        return out

    try:
        root = ET.fromstring(xml_content)
        out["xml_codice"] = root.findtext(".//info-dichiarazione/codice") or "NON RILEVATO"
        dt_raw = root.findtext(".//info-schema/data-compilazione") or ""
        out["data_compilazione"] = format_date_str(dt_raw)

        out["cf_dichiarato"] = format_piva(root.findtext(".//impresa/codice-fiscale"))
        out["denominazione_dichiarata"] = clean_val(root.findtext(".//impresa/denominazione"))
        out["logo_b64"] = clean_val(root.findtext(".//informazioni/logo/b64"))

        contatti = root.find(".//contatti")
        if contatti is not None:
            em = contatti.findtext("email")
            if em and "@" in em:
                out["email"] = em.strip().lower()
            out["linkedin"] = clean_val(contatti.findtext("linkedin"))
            out["facebook"] = clean_val(contatti.findtext("facebook"))

        pres_video = root.find(".//presentazione/video/url")
        out["video_url"] = get_bilingual_text(pres_video, "it", "en")

        for s in root.findall(".//riferimenti-esterni/riferimento-esterno/sito-web-esterno"):
            if s.text and s.text.strip():
                out["siti_web_esterni"].append(s.text.strip())

        for t in root.findall(".//settore/tag"):
            if t.text and t.text.strip():
                out["tags"].append(t.text.strip())

        ms = root.find(".//settore/macro-settore")
        if ms is not None:
            out["macro_settore_cod"] = ms.findtext("cod") or "NON RILEVATO"
            out["macro_settore_decod"] = get_bilingual_text(ms, "decod/it", "decod/en")
        elif out["tags"]:
            out["macro_settore_decod"] = ", ".join(out["tags"])

        stadio = root.find(".//informazioni/stadio")
        if stadio is not None:
            out["stadio_cod"] = stadio.findtext("cod") or "NON RILEVATO"
            out["stadio_decod"] = get_bilingual_text(stadio, "decod/it", "decod/en")

        pres = root.find(".//presentazione/descrizione")
        out["presentazione_it"] = get_bilingual_text(pres, "it", "en")
        out["presentazione_en"] = get_bilingual_text(pres, "en", "it")

        prod = root.find(".//prodotto")
        if prod is not None:
            st_prod = prod.find(".//stato-prodotto")
            out["prodotto_stato_cod"] = prod.findtext("stato-prodotto/cod") or "NON RILEVATO"
            out["prodotto_stato_decod"] = get_bilingual_text(st_prod, "decod/it", "decod/en")

            desc_prod = prod.find(".//descrizione-prodotto")
            out["prodotto_desc_it"] = get_bilingual_text(desc_prod, "it", "en")
            out["prodotto_desc_en"] = get_bilingual_text(desc_prod, "en", "it")

        # Fallback Prodotto se descrizione-prodotto manca (es. Rewind, Principled)
        if out["prodotto_desc_it"] == "NON RILEVATO" and out["presentazione_it"] != "NON RILEVATO":
            out["prodotto_desc_it"] = out["presentazione_it"]

        tm = root.find(".//informazioni/team")
        if tm is not None:
            st_tm = tm.find(".//stato-team")
            out["team_stato_cod"] = tm.findtext("stato-team/cod") or "NON RILEVATO"
            out["team_stato_decod"] = get_bilingual_text(st_tm, "decod/it", "decod/en")

        for sog in root.findall(".//team/soggetto"):
            occ = get_bilingual_text(sog, "occupazione-startup-team/it", "occupazione-startup-team/en", "")
            tit_cod = sog.findtext("livello-titolo-studio/cod") or ""
            tit_dec = get_bilingual_text(sog, "livello-titolo-studio/decod/it", "livello-titolo-studio/decod/en", "")
            eta_cod = sog.findtext("fascia-eta/cod") or ""
            eta_dec = get_bilingual_text(sog, "fascia-eta/decod/it", "fascia-eta/decod/en", "")
            desc_r = get_bilingual_text(sog, "descrizione-ruolo-team/it", "descrizione-ruolo-team/en", "")

            out["soggetti_xml"].append({
                "occupazione": occ.strip(),
                "titolo_cod": tit_cod.strip(),
                "titolo_studio": tit_dec.strip(),
                "fascia_eta_cod": eta_cod.strip(),
                "fascia_eta": eta_dec.strip(),
                "descrizione_ruolo": desc_r.strip(),
            })

        for cv in root.findall(".//mercato/canale-di-vendita"):
            cod_c = cv.findtext("cod") or ""
            if "C1" in cod_c:
                out["canale_diretto"] = "SÌ"
            if "C2" in cod_c:
                out["canale_gdo"] = "SÌ"
            if "C3" in cod_c:
                out["canale_ecommerce"] = "SÌ"
            if "C4" in cod_c:
                out["canale_agenti"] = "SÌ"

        reg_it = [r.findtext("decod/it").strip() for r in root.findall(".//mercato/area-geografica-italia") if r.findtext("decod/it")]
        if reg_it:
            out["mercato_italia"] = ", ".join(reg_it)

        est_ar = [e.findtext("decod/it").strip() for e in root.findall(".//mercato/area-geografica-estero") if e.findtext("decod/it")]
        if est_ar:
            out["mercato_estero"] = ", ".join(est_ar)

        bm = root.find(".//business-model/descrizione-business-model")
        out["business_model"] = get_bilingual_text(bm, "it", "en")

        cc = root.find(".//concorrenza/descrizione-concorrenza")
        out["concorrenza"] = get_bilingual_text(cc, "it", "en")

        inv_d = root.find(".//innovazione/descrizione-proprieta-intellettuale")
        out["innovazione_desc"] = get_bilingual_text(inv_d, "it", "en")

        if root.findtext(".//innovazione/proprieta-intellettuale") == "true":
            out["has_pi"] = "SÌ"

        br_node = root.find(".//innovazione/brevetto")
        if br_node is not None:
            tit_node = br_node.find(".//titolo-proprieta-intellettuale")
            desc_node = br_node.find(".//descrizione-titolo-proprieta-intellettuale")
            out["brevetto_xml"] = {
                "numero": br_node.findtext("codice-proprieta-intellettuale") or "NON RILEVATO",
                "titolarita": get_bilingual_text(tit_node, "decod/it", "decod/en", "titolare"),
                "tipo": get_bilingual_text(desc_node, "decod/it", "decod/en", "Brevetto per invenzione industriale"),
            }

        inc = root.find(".//incubatori")
        if inc is not None and inc.findtext("incubatore-accelerato") == "true":
            out["incubatore_accelerato"] = "SÌ"
            out["incubatore_nome"] = inc.findtext("incubatore/denominazione-incubatore") or "Incubatore certificato"
            out["incubatore_cf"] = format_piva(inc.findtext("incubatore/cfisc-incubatore"))

        # Routing deterministico: TA2 = Università/Ricerca | TA1 = Associazioni
        for ass in root.findall(".//associazione/tipologiaAssociazione"):
            nome_ass = ass.findtext("nome")
            cod_tipo = ass.findtext("tipo-associazione/cod") or ""
            tipo_desc = get_bilingual_text(ass.find(".//tipo-associazione"), "decod/it", "decod/en", "associazione")

            if nome_ass:
                nome_pulito = nome_ass.strip()
                if (
                    cod_tipo == "TA2"
                    or "universit" in nome_pulito.lower()
                    or "politecnico" in nome_pulito.lower()
                    or "cnr" in nome_pulito.lower()
                ):
                    out["universita"].append(nome_pulito)
                else:
                    out["associazioni"].append({"nome": nome_pulito, "tipo": tipo_desc.strip()})

        for inte in root.findall(".//interessi/interessato-a/cod"):
            if inte.text and inte.text.strip():
                out["interessi"].append(inte.text.strip())

        # Finanziamenti multi-round
        for fin in root.findall(".//finanze/finanza"):
            amm_raw = fin.findtext("ammontare") or "0"
            try:
                amm_val = float(amm_raw)
            except ValueError:
                amm_val = 0.0

            investitori = [
                inv.findtext("nome-investitore").strip()
                for inv in fin.findall(".//investitore")
                if inv.findtext("nome-investitore")
            ]

            tipo_node = fin.find(".//tipo")
            tipo_desc = get_bilingual_text(tipo_node, "decod/it", "decod/en", "Finanziamento")

            out["finanziamenti"].append({
                "tipo_cod": fin.findtext("tipo/cod") or "F",
                "tipo_decod": tipo_desc,
                "ammontare": amm_val,
                "data_annuncio": format_date_str(fin.findtext("data-annuncio") or ""),
                "data_chiusura": format_date_str(fin.findtext("data-chiusura") or ""),
                "investitori": investitori,
            })

            for inv_nome in investitori:
                if is_institutional_entity(inv_nome):
                    out["enti"].append({"nome": inv_nome, "categoria": "SOSTEGNO_PUBBLICO"})

    except Exception as e:
        log("WARN", f"Parsing XML: {e}")

    return out


async def fetch_xml_in_session(page, timeout_ms=6000) -> str:
    """Try the XML URL, Wicket methods in-browser, then native browser download."""
    def is_declaration(text):
        return bool(re.search(r"<(?:[\w.-]+:)?dichiarazione(?:\s|>)", text or ""))

    def log_response(stage, status, content_type, url, text):
        path = urllib.parse.urlsplit(url or "").path.split(";", 1)[0]
        tags = re.findall(r"<(?:[\w.-]+:)?([\w.-]+)(?:\s|>)", text or "")[:5]
        markers = [
            marker for marker, pattern in [
                ("declaration", r"<(?:[\w.-]+:)?dichiarazione(?:\s|>)"),
                ("redirect", r"<(?:[\w.-]+:)?redirect\b"),
                ("ajax-response", r"<(?:[\w.-]+:)?ajax-response\b"),
                ("exception", r"exception|error|rejected|forbidden"),
            ] if re.search(pattern, text or "", re.IGNORECASE)
        ]
        log(
            "INFO",
            f"[XML:{stage}] HTTP {status}; content-type={content_type or 'assente'}; "
            f"bytes={len(text or '')}; root-tags={','.join(tags) or 'nessuno'}; "
            f"markers={','.join(markers) or 'nessuno'}; path={path or 'non disponibile'}.",
        )

    def extract_redirect(text, base_url):
        match = re.search(
            r"<(?:[\w.-]+:)?redirect\b[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</(?:[\w.-]+:)?redirect>",
            text or "", re.DOTALL,
        )
        if not match:
            return ""
        return urllib.parse.urljoin(
            base_url, match.group(1).replace("&amp;", "&")
        )

    def extract_evaluate_url(text, base_url):
        try:
            root = ET.fromstring(text or "")
        except ET.ParseError:
            return ""
        script = next(
            ("".join(node.itertext()) for node in root.iter()
             if node.tag.rsplit("}", 1)[-1] == "evaluate"),
            "",
        )
        patterns = [
            r"(?:window\.)?location(?:\.href)?\s*=\s*(['\"])(.*?)\1",
            r"window\.open\(\s*(['\"])(.*?)\1",
            r"(?:window\.)?location\.(?:assign|replace)\(\s*(['\"])(.*?)\1",
        ]
        for pattern in patterns:
            match = re.search(pattern, script, re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            target = urllib.parse.urljoin(
                base_url, match.group(2).replace("&amp;", "&")
            )
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme == "https" and parsed.netloc.lower() == "startup.registroimprese.it":
                return target
        return ""

    async def fetch_browser_url(url, stage):
        result = await page.evaluate("""async ({url, timeout}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            try {
                const response = await fetch(url, {
                    credentials: 'same-origin', signal: controller.signal
                });
                return {
                    status: response.status,
                    contentType: response.headers.get('content-type') || '',
                    url: response.url,
                    text: await response.text()
                };
            } catch (error) {
                return {error: String(error), text: ''};
            } finally {
                clearTimeout(timer);
            }
        }""", {"url": url, "timeout": timeout_ms})
        if result.get("status") is not None:
            log_response(
                stage, result["status"], result.get("contentType"),
                result.get("url"), result.get("text"),
            )
        elif result.get("error"):
            log("WARN", f"[XML:{stage}] {result['error']}")
        return result

    xml_btn = page.locator("#downloadPnl a:has-text('XML'), a:has-text('XML')").first
    if await xml_btn.count() == 0:
        log("ERROR", "[XML] Controllo XML assente nel DOM (#downloadPnl / anchor XML).")
        return None

    xml_href = await xml_btn.get_attribute("href") or ""
    if xml_href and not xml_href.lower().startswith("javascript:"):
        try:
            result = await page.evaluate("""async ({url, timeout}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout);
                try {
                    const response = await fetch(url, {
                        credentials: 'same-origin', signal: controller.signal
                    });
                    return {
                        status: response.status,
                        contentType: response.headers.get('content-type') || '',
                        url: response.url,
                        text: await response.text()
                    };
                } catch (error) {
                    return {error: String(error), text: ''};
                } finally {
                    clearTimeout(timer);
                }
            }""", {"url": xml_href, "timeout": timeout_ms})
            if result.get("status") is not None:
                log_response(
                    "url-diretto", result["status"], result.get("contentType"),
                    result.get("url"), result.get("text"),
                )
            if is_declaration(result.get("text")):
                return result["text"]
            if result.get("error"):
                log("WARN", f"[XML:url-diretto] {result['error']}")
        except Exception as error:
            log("WARN", f"[XML:url-diretto] {type(error).__name__}: {error}")
    else:
        log("INFO", "[XML:url-diretto] href non scaricabile; provo il binding Wicket.")

    page_html = ""
    try:
        page_html = await page.content()
    except Exception as error:
        log("WARN", f"[XML] DOM non leggibile per binding Wicket: {type(error).__name__}: {error}")

    wicket_binding = None
    for config_text in re.findall(r"Wicket\.Ajax\.ajax\((\{[^)]*\})\)", page_html):
        try:
            binding = json.loads(config_text)
        except json.JSONDecodeError:
            continue
        if "downloadXmlLnk" in binding.get("u", ""):
            wicket_binding = binding
            break

    if not wicket_binding:
        log("WARN", "[XML] Binding Wicket downloadXmlLnk assente; proseguo col download nativo.")
        wicket_url = ""
        wicket_methods = []
    else:
        wicket_url = wicket_binding["u"].replace("&amp;", "&")
        wicket_method = str(wicket_binding.get("m", "GET")).upper()
        if wicket_method not in {"GET", "POST"}:
            log("WARN", "[XML:binding] Metodo Wicket inatteso; uso GET.")
            wicket_method = "GET"
        wicket_methods = [wicket_method, "POST" if wicket_method == "GET" else "GET"]

    for method in wicket_methods:
        try:
            result = await page.evaluate("""async ({url, method, timeout}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout);
                try {
                    const base = (window.Wicket && window.Wicket.Ajax && window.Wicket.Ajax.baseUrl)
                        ? window.Wicket.Ajax.baseUrl.replaceAll('&amp;', '&')
                        : location.pathname + location.search;
                    const response = await fetch(url, {
                        method,
                        credentials: 'same-origin',
                        signal: controller.signal,
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
                } catch (error) {
                    return {error: String(error), text: '', url: location.href};
                } finally {
                    clearTimeout(timer);
                }
            }""", {"url": wicket_url, "method": method, "timeout": timeout_ms})
            if result.get("status") is not None:
                log_response(
                    f"wicket-{method}", result["status"], result.get("contentType"),
                    result.get("url"), result.get("text"),
                )
            if is_declaration(result.get("text")):
                return result["text"]
            if result.get("error"):
                log("WARN", f"[XML:wicket-{method}] {result['error']}")

            evaluate_url = extract_evaluate_url(
                result.get("text", ""), result.get("url", page.url)
            )
            if evaluate_url:
                log("INFO", f"[XML:wicket-{method}] evaluate contiene un URL same-origin; provo il download browser.")
                evaluated = await fetch_browser_url(
                    evaluate_url, f"evaluate-wicket-{method}"
                )
                if is_declaration(evaluated.get("text")):
                    return evaluated["text"]

            redirect_url = extract_redirect(
                result.get("text", ""), result.get("url", page.url)
            )
            if redirect_url:
                redirected = await page.evaluate("""async ({url, timeout}) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeout);
                    try {
                        const response = await fetch(url, {
                            credentials: 'same-origin', signal: controller.signal
                        });
                        return {
                            status: response.status,
                            contentType: response.headers.get('content-type') || '',
                            url: response.url,
                            text: await response.text()
                        };
                    } catch (error) {
                        return {error: String(error), text: ''};
                    } finally {
                        clearTimeout(timer);
                    }
                }""", {"url": redirect_url, "timeout": timeout_ms})
                if redirected.get("status") is not None:
                    log_response(
                        f"redirect-wicket-{method}", redirected["status"],
                        redirected.get("contentType"), redirected.get("url"),
                        redirected.get("text"),
                    )
                if is_declaration(redirected.get("text")):
                    return redirected["text"]
                if redirected.get("error"):
                    log("WARN", f"[XML:redirect-wicket-{method}] {redirected['error']}")
        except Exception as error:
            log("WARN", f"[XML:wicket-{method}] {type(error).__name__}")

    try:
        if not await xml_btn.is_visible():
            await page.locator("#downloadPnl").click(force=True, timeout=3000)
            await xml_btn.wait_for(state="visible", timeout=3000)
        async with page.expect_download(timeout=timeout_ms) as download_info:
            await xml_btn.click(force=True, timeout=timeout_ms)
        download = await download_info.value
        failure = await download.failure()
        if failure:
            log("WARN", "[XML:download-nativo] Download segnalato come fallito.")
        else:
            temp_fd, xml_path = tempfile.mkstemp(prefix="privatelab-xml-", suffix=".xml")
            os.close(temp_fd)
            try:
                await download.save_as(xml_path)
                with open(xml_path, "r", encoding="utf-8", errors="replace") as xml_file:
                    xml_text = xml_file.read()
                log("INFO", f"[XML:download-nativo] bytes={len(xml_text)}.")
                if is_declaration(xml_text):
                    return xml_text
                log("WARN", "[XML:download-nativo] File scaricato ma non è una dichiarazione XML.")
            finally:
                try:
                    os.remove(xml_path)
                except OSError:
                    pass
    except Exception as error:
        log("WARN", f"[XML:download-nativo] {type(error).__name__}")

    log("ERROR", "[XML] Esauriti tutti i metodi; dossier non completo.")
    return None


async def extract_complete_company(page, card_url: str, fallback_name: str = ""):
    if "startup.registroimprese.it" not in card_url:
        raise ValueError(f"URL non appartenente al Registro Imprese: {card_url}")

    await page.goto(card_url, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(1.5)
    await neutralize_f5_shield(page)

    full_text = await page.inner_text("body")

    # Acquisizione rapida XML in memoria (senza timeout disco ZenRows)
    xml_content = None
    for attempt in range(2):
        xml_content = await fetch_xml_in_session(page)
        if xml_content:
            break
        if attempt == 0:
            log("WARN", "[XML] Primo tentativo incompleto; ricarico una volta la scheda e riprovo.")
            await page.goto(card_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.0)
            await neutralize_f5_shield(page)
    if not xml_content:
        log("ERROR", "XML non disponibile; il dossier non verrà salvato come completo.")
        raise RuntimeError("Download XML della scheda non riuscito")
    xml_data = parse_xml_payload(xml_content)

    anag = page.locator(".ui.grid.no-vertical-column-padding .eleven.wide.column")
    c_count = await anag.count()

    denominazione = clean_val(await anag.nth(0).inner_text()) if c_count > 0 else "NON RILEVATO"
    if denominazione == "NON RILEVATO":
        denominazione = await get_text(page.locator("#companyNameForGA, h2.roundedtop span"))
    if (not denominazione or denominazione == "NON RILEVATO") and xml_data["denominazione_dichiarata"] != "NON RILEVATO":
        denominazione = xml_data["denominazione_dichiarata"]
    if (not denominazione or denominazione == "NON RILEVATO") and fallback_name:
        denominazione = fallback_name.strip().upper()

    comune = clean_val(await anag.nth(1).inner_text()) if c_count > 1 else "NON RILEVATO"

    raw_cf = clean_val(await anag.nth(2).inner_text()) if c_count > 2 else "NON RILEVATO"
    if raw_cf == "NON RILEVATO":
        raw_cf = xml_data["cf_dichiarato"]
    if raw_cf == "NON RILEVATO":
        m_piva = re.search(r"Codice\s*Fiscale\D*(\d{11}|\b[A-Z0-9]{11,16}\b)", full_text, re.IGNORECASE)
        if m_piva:
            raw_cf = m_piva.group(1).strip()

    # Partita IVA / CF rigorosamente prefissata con 'IT'
    cf_piva = format_piva(raw_cf)

    forma_giuridica = clean_val(await anag.nth(3).inner_text()) if c_count > 3 else "NON RILEVATO"
    impresa_costituita = clean_val(await anag.nth(4).inner_text()) if c_count > 4 else "NON RILEVATO"

    sito_internet = clean_val(await anag.nth(5).inner_text()) if c_count > 5 else "NON RILEVATO"
    if sito_internet == "NON RILEVATO" and xml_data["siti_web_esterni"]:
        sito_internet = xml_data["siti_web_esterni"][0]

    codice_ateco = clean_val(await anag.nth(6).locator("b").inner_text()) if c_count > 6 else "NON RILEVATO"
    macro_settore = clean_val(await anag.nth(7).inner_text()) if c_count > 7 else "NON RILEVATO"
    if macro_settore == "NON RILEVATO" and xml_data["macro_settore_decod"] != "NON RILEVATO":
        macro_settore = xml_data["macro_settore_decod"]

    desc_ateco = "NON RILEVATO"
    try:
        raw_d = await page.locator("#codiceatecoLbl").get_attribute("data-content")
        if raw_d:
            desc_ateco = clean_val(raw_d)
    except Exception:
        pass

    dt_cost = await get_text(page.locator("#mainDiv .row.roundedbottom .eight.wide.column span"))
    dt_sec = await get_text(page.locator("#mainDiv .row.roundedbottom .six.wide.column span"))

    aggiornamento_ri = "NON RILEVATO"
    m_agg = re.search(r"Aggiornamento al\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", full_text)
    if m_agg:
        aggiornamento_ri = m_agg.group(1)

    # Griglia Classi Dimensionali da visura
    classe_prod_val = "non disponibile"
    classe_prod_cod = "?"
    classe_add_val = "non disponibile"
    classe_add_cod = "?"
    classe_cap_val = "non disponibile"
    classe_cap_cod = "?"

    classi_grid = page.locator(".ui.celled.three.column.grid:has-text('classe di produzione') .column")
    if await classi_grid.count() >= 3:
        try:
            prod_col = classi_grid.nth(0)
            classe_prod_val = await get_text(prod_col.locator("div:has-text('classe di produzione') ~ div span, span").first, "non disponibile")
            classe_prod_cod = await get_text(prod_col.locator("span[style*='font-size'], b span").first, "?")

            add_col = classi_grid.nth(1)
            classe_add_val = await get_text(add_col.locator("div:has-text('classe di addetti') ~ div span, span").first, "non disponibile")
            classe_add_cod = await get_text(add_col.locator("span[style*='font-size'], b span").first, "?")

            cap_col = classi_grid.nth(2)
            classe_cap_val = await get_text(cap_col.locator("div:has-text('classe di capitale') ~ div span, span").first, "non disponibile")
            classe_cap_cod = await get_text(cap_col.locator("span[style*='font-size'], b span").first, "?")
        except Exception:
            pass

    spese_rs = await get_text(page.locator("#mostPresentazioneAttivitaRi span").first)
    team_curricula = await get_text(page.locator("#mostTeamRi span").first)
    rel_ricerca = await get_text(page.locator("#mostFinanzaIncubatoriRi span").first)
    privative = await get_text(page.locator("#mostInnovazioneRi span").first)

    req_rs = "SÌ" if ("SPESE" in spese_rs.upper() or "RICERCA" in spese_rs.upper()) else "NO"
    req_team = "SÌ" if ("DOTTORATO" in team_curricula.upper() or "LAUREA" in team_curricula.upper()) else "NO"
    req_brev = "SÌ" if ("BREVETTO" in privative.upper() or "SIAE" in privative.upper() or xml_data["has_pi"] == "SÌ") else "NO"

    legale_rappresentante = "NON RILEVATO"
    data_firma = "NON RILEVATO"
    m_f = re.search(r"dal\s+legale rappresentante\s+(.*?)\s+il\s+([0-9]{2}/[0-9]{2}/[0-9]{4})", full_text, re.IGNORECASE)
    if m_f:
        legale_rappresentante = clean_val(m_f.group(1))
        data_firma = m_f.group(2).strip()

    ente_certificatore = "NON RILEVATO"
    try:
        rc = await page.locator("#dettagliFirma").get_attribute("data-html")
        if rc:
            mc = re.search(r"Certificato da\s+([^<]+)", rc)
            if mc:
                ente_certificatore = clean_val(mc.group(1))
                if is_certifier_entity(ente_certificatore):
                    xml_data["enti"].append({"nome": ente_certificatore, "categoria": "CERTIFICATORE_DIGITALE"})
    except Exception:
        pass

    email_ufficiale = xml_data["email"]
    pec_aziendale = "NON RILEVATO"
    if email_ufficiale != "NON RILEVATO" and ("pec." in email_ufficiale.lower() or "legalmail" in email_ufficiale.lower()):
        pec_aziendale = email_ufficiale
        email_ufficiale = "NON COMUNICATA"

    if email_ufficiale in ["NON RILEVATO", "NON COMUNICATA"]:
        try:
            mail_locs = await page.locator("a[href^='mailto:']:not([href*='?subject']):not([class*='jssocials'])").all()
            for ml in mail_locs:
                href = await ml.get_attribute("href") or ""
                em = href.replace("mailto:", "").split("?")[0].strip()
                if "@" in em and "." in em:
                    if "pec" in em.lower() or "legalmail" in em.lower():
                        pec_aziendale = em
                    else:
                        email_ufficiale = em
                        break
        except Exception:
            pass

    pitch_it = xml_data["presentazione_it"] if xml_data["presentazione_it"] != "NON RILEVATO" else await get_text(page.locator("#presentazioneAnchor .twocolumntext"))
    prod_desc_it = xml_data["prodotto_desc_it"] if xml_data["prodotto_desc_it"] != "NON RILEVATO" else await get_text(page.locator("#prodottoAnchor .twocolumntext"))

    # CHIAMATA UNICA A GROQ AI (Brand, Prodotti, Persone, Qualifiche, Score)
    log("INFO", f"🤖 Groq AI monoprocesso per: {denominazione}...")
    ai_result = {}
    try:
        ai_result = await asyncio.to_thread(
            analyze_dossier_with_groq,
            denominazione,
            legale_rappresentante,
            team_curricula,
            pitch_it,
            prod_desc_it,
            xml_data["soggetti_xml"],
        )
    except Exception as e:
        log("WARN", f"Groq AI fallback: {e}")

    nome_pulito = ai_result.get("nome_pulito", "").strip().upper()
    if not nome_pulito or nome_pulito == "NON RILEVATO":
        clean = re.sub(
            r"\b(SOCIETA'|SOCIETA|A|RESPONSABILITA'|RESPONSABILITA|LIMITATA|BENEFIT|AGRICOLA|SRLS|SRL|SPA|S\.B\.|SB)\b",
            "",
            denominazione,
            flags=re.IGNORECASE,
        )
        nome_pulito = re.sub(r"[^\w\s-]", "", clean).strip().upper()

    forma_giuridica = forma_giuridica.strip().upper()
    is_sb = "SÌ" if ("S.B." in denominazione.upper() or "BENEFIT" in denominazione.upper()) else "NO"

    # Prodotti con categorizzazione e fallback sintetico
    prodotti_list = []
    groq_prods = ai_result.get("prodotti", [])
    if groq_prods and isinstance(groq_prods, list):
        for p in groq_prods:
            p_name = p.get("nome", "").strip()
            p_cat = p.get("categoria", "Software / Servizi").strip()
            if p_name:
                prodotti_list.append({
                    "nome": p_name,
                    "categoria": p_cat,
                    "stadio": p.get("stadio") or xml_data["prodotto_stato_decod"],
                    "descrizione": p.get("descrizione") or prod_desc_it[:250] + "...",
                })
    if not prodotti_list:
        log("WARN", f"Nessun prodotto esplicito estratto per {denominazione}.")

    # DEDUPLICAZIONE RIGOROSA DELLE PERSONE REALI (Nomi e Cognomi)
    people_list = []
    seen_names = []

    # 1. Inserisce prima il Legale Rappresentante normalizzato
    if legale_rappresentante and legale_rappresentante != "NON RILEVATO":
        lr_norm = normalize_person_name(legale_rappresentante)
        if lr_norm:
            people_list.append({
                "nome": lr_norm,
                "ruolo": "Legale Rappresentante / Amministratore",
                "livello": "C_LEVEL",
                "titolo": "Laurea magistrale" if "LAUREA" in team_curricula.upper() else "Non specificato",
                "eta": "Non dichiarata",
                "is_founder": "SÌ",
            })
            seen_names.append(lr_norm)

    # 2. Inserisce le persone estratte da Groq solo se non già presenti
    groq_people = ai_result.get("persone", [])
    if groq_people and isinstance(groq_people, list):
        for per in groq_people:
            raw_p_name = per.get("nome", "").strip()
            norm_p_name = normalize_person_name(raw_p_name)
            if norm_p_name and len(norm_p_name.split()) in [2, 3, 4]:
                if not any(are_same_person(norm_p_name, existing) for existing in seen_names):
                    people_list.append({
                        "nome": norm_p_name,
                        "ruolo": per.get("ruolo", "Socio / Fondatore").strip(),
                        "livello": per.get("livello_gerarchico", "OPERATIONAL").strip().upper(),
                        "titolo": per.get("titolo", "Non specificato").strip(),
                        "eta": per.get("eta", "Non dichiarata").strip(),
                        "is_founder": per.get("is_founder", "SÌ").strip(),
                    })
                    seen_names.append(norm_p_name)

    # QUALIFICHE BLINDATE SUGLI SLOT CANONICI DELL'XML
    qualifiche_list = []
    groq_assigned_quals = ai_result.get("qualifiche", [])

    if xml_data["soggetti_xml"]:
        # Se l'XML ha soggetti, gli slot sono esattamente quelli
        for idx, sx in enumerate(xml_data["soggetti_xml"]):
            assigned_person = None
            assigned_status = "ANONIMO"

            # Cerca se Groq ha associato con certezza un nome a questo slot
            if idx < len(groq_assigned_quals):
                g_q = groq_assigned_quals[idx]
                g_cand_name = normalize_person_name(g_q.get("persona_nome", ""))
                if g_cand_name and any(are_same_person(g_cand_name, p["nome"]) for p in people_list):
                    assigned_person = g_cand_name
                    assigned_status = "ASSEGNATA"

            qualifiche_list.append({
                "ruolo": sx["occupazione"] or "Specialista / Tecnico",
                "titolo_studio": sx["titolo_studio"] or "Non specificato",
                "fascia_eta": sx["fascia_eta"] or "Non dichiarata",
                "descrizione_ruolo": sx["descrizione_ruolo"],
                "persona_nome": assigned_person,
                "stato_assegnazione": assigned_status,
            })
    elif groq_assigned_quals:
        # Fallback se l'XML non ha soggetti (Wuoz, Lookinglass)
        for q in groq_assigned_quals:
            g_cand_name = normalize_person_name(q.get("persona_nome", ""))
            qualifiche_list.append({
                "ruolo": q.get("ruolo", "Specialista"),
                "titolo_studio": q.get("titolo_studio", "Non specificato"),
                "fascia_eta": q.get("fascia_eta", "Non dichiarata"),
                "persona_nome": g_cand_name if g_cand_name else None,
                "stato_assegnazione": "ASSEGNATA" if g_cand_name else "ANONIMO",
            })

    # Brevetti
    brevetti_list = []
    if xml_data["brevetto_xml"]:
        num_br = xml_data["brevetto_xml"]["numero"]
        ente, area, verif = classify_patent_meta(num_br)
        brevetti_list.append({
            "titolo": "Brevetto Registrato Ufficiale",
            "numero": num_br,
            "tipo": xml_data["brevetto_xml"]["tipo"],
            "titolarita": xml_data["brevetto_xml"]["titolarita"],
            "data": "",
            "ente": ente,
            "area": area,
            "verif": verif,
        })

    brevetti_trovati = re.findall(
        r"\b(EP\s*\d{7}|WO\s*\d{4}/\d{6}|PCT/[A-Z]{2}\d{4}/\d{6}|IT\d{6,15}|D\d{6,10}|10\d{12,14})\b",
        privative,
        re.IGNORECASE,
    )
    numeri_gia_inseriti = set(b["numero"].replace(" ", "") for b in brevetti_list)
    for bt in set(brevetti_trovati):
        clean_num = bt.replace(" ", "")
        if clean_num not in numeri_gia_inseriti:
            ente, area, verif = classify_patent_meta(clean_num)
            brevetti_list.append({
                "titolo": "Privativa Industriale / Brevetto",
                "numero": bt.strip(),
                "tipo": "Software SIAE" if "D" in bt.upper() else "Brevetto per Invenzione Industriale",
                "titolarita": "Titolare",
                "data": "",
                "ente": ente,
                "area": area,
                "verif": verif,
            })
            numeri_gia_inseriti.add(clean_num)

    marchi_list = []
    m_marchi = re.findall(r"(\b[A-Z0-9a-z_-]+(?:™|®))", full_text)
    for mk in set(m_marchi):
        marchi_list.append({
            "denominazione": mk,
            "stato": "Registrato (®)" if "®" in mk else "Depositato",
            "estensione": "Nazionale / Internazionale",
            "prodotto": mk.replace("™", "").replace("®", "").strip(),
        })

    tot_fin_euro = sum(r["ammontare"] for r in xml_data["finanziamenti"])

    # Upload Logo su ImgBB API
    logo_url = "NON RILEVATO"
    logo_b64_db = "NON RILEVATO"
    if xml_data["logo_b64"] != "NON RILEVATO":
        log("INFO", f"📤 Caricamento logo su ImgBB per {denominazione}...")
        logo_url = await asyncio.to_thread(upload_logo_to_imgbb, xml_data["logo_b64"], IMGBB_API_KEY)
        logo_b64_db = "Disponibile (Caricato su ImgBB)" if logo_url != "NON RILEVATO" else "Disponibile"

    xml_raw_saved = xml_data["xml_raw"]
    if xml_raw_saved and "<b64>" in xml_raw_saved:
        xml_raw_saved = re.sub(r"<b64>.*?</b64>", "<b64>[ARCHIVED_IN_IMG_BB]</b64>", xml_raw_saved, flags=re.DOTALL)

    num_phds = sum(
        1 for s in xml_data["soggetti_xml"]
        if "LTS6" in s.get("titolo_cod", "") or "dottorato" in s.get("titolo_studio", "").lower()
    )
    if num_phds == 0:
        num_phds = sum(
            1 for p in people_list
            if "phd" in p.get("titolo", "").lower() or "dottorato" in p.get("titolo", "").lower()
        )

    master_record = {
        "cf": cf_piva,
        "denominazione": denominazione,
        "denominazione_pulita": nome_pulito,
        "societa_benefit": is_sb,
        "comune": comune,
        "provincia": comune.split("(")[-1].replace(")", "").strip() if "(" in comune else "",
        "regione": "",
        "forma_giuridica": forma_giuridica,
        "modalita_costituzione": impresa_costituita,
        "sito_internet": sito_internet,
        "siti_web_esterni": json.dumps(xml_data["siti_web_esterni"], ensure_ascii=False),
        "email_ufficiale": email_ufficiale,
        "pec_aziendale": pec_aziendale,
        "url_scheda": card_url,
        "codice_ateco": codice_ateco,
        "descrizione_ateco": desc_ateco,
        "macro_settore": macro_settore,
        "macro_settore_codice": xml_data["macro_settore_cod"],
        "macro_settore_dettagliato": xml_data["macro_settore_decod"],
        "tag_settoriali": ", ".join(xml_data["tags"]) if xml_data["tags"] else "NON RILEVATO",
        "dt_costituzione": dt_cost,
        "dt_iscrizione_startup": dt_sec,
        "dt_aggiornamento_ri": aggiornamento_ri,
        "data_compilazione_dichiarazione": xml_data["data_compilazione"],
        "classe_produzione_valore": classe_prod_val,
        "classe_produzione_codice": classe_prod_cod,
        "classe_addetti_valore": classe_add_val,
        "classe_addetti_codice": classe_add_cod,
        "classe_capitale_valore": classe_cap_val,
        "classe_capitale_codice": classe_cap_cod,
        "prev_femminile": "Non disponibile",
        "prev_giovanile": "Non disponibile",
        "prev_straniera": "Non disponibile",
        "req_spese_rs": req_rs,
        "req_team_qualificato": req_team,
        "req_brevetti": req_brev,
        "legale_rappresentante": legale_rappresentante,
        "data_firma": data_firma,
        "ente_certificatore": ente_certificatore,
        "codice_dichiarazione_xml": xml_data["xml_codice"],
        "logo_url": logo_url,
        "logo_b64": logo_b64_db,
        "xml_raw": xml_raw_saved,
        "video_pitch_url": xml_data["video_url"],
        "linkedin_url": xml_data["linkedin"],
        "facebook_url": xml_data["facebook"],
        "twitter_x_url": "NON RILEVATO",
        "instagram_url": "NON RILEVATO",
        "youtube_channel_url": "NON RILEVATO",
        "tiktok_url": "NON RILEVATO",
        "completezza_profilo": "NON RILEVATO",
        "stadio_startup_codice": xml_data["stadio_cod"],
        "stadio_startup_testo": xml_data["stadio_decod"],
        "stadio_prodotto_codice": xml_data["prodotto_stato_cod"],
        "stadio_prodotto_testo": xml_data["prodotto_stato_decod"],
        "stadio_team_codice": xml_data["team_stato_cod"],
        "stadio_team_testo": xml_data["team_stato_decod"],
        "pitch_presentazione_it": pitch_it,
        "pitch_presentazione_en": xml_data["presentazione_en"],
        "descrizione_prodotto_it": prod_desc_it,
        "descrizione_prodotto_en": xml_data["prodotto_desc_en"],
        "business_model_descrizione": xml_data["business_model"],
        "concorrenza_descrizione": xml_data["concorrenza"],
        "innovazione_descrizione": xml_data["innovazione_desc"],
        "canale_diretto": xml_data["canale_diretto"],
        "canale_gdo": xml_data["canale_gdo"],
        "canale_ecommerce": xml_data["canale_ecommerce"],
        "canale_agenti": xml_data["canale_agenti"],
        "mercato_italia_regioni": xml_data["mercato_italia"],
        "mercato_estero_aree": xml_data["mercato_estero"],
        "interesse_clienti": "SÌ" if "I1" in xml_data["interessi"] else "NO",
        "interesse_investitori": "SÌ" if "I2" in xml_data["interessi"] else "NO",
        "interesse_incubatori": "SÌ" if "I3" in xml_data["interessi"] else "NO",
        "interesse_partner_universitari": "SÌ" if "I4" in xml_data["interessi"] else "NO",
        "interesse_partner_imprenditoriali": "SÌ" if "I5" in xml_data["interessi"] else "NO",
        "interesse_figure_tecniche": "SÌ" if "I6" in xml_data["interessi"] else "NO",
        "incubatore_accelerato": xml_data["incubatore_accelerato"],
        "incubatore_nome": xml_data["incubatore_nome"],
        "incubatore_cf": format_piva(xml_data["incubatore_cf"]),
        "ha_relazioni_universitarie": "SÌ" if xml_data["universita"] else "NO",
        "ha_proprieta_intellettuale": "SÌ" if (brevetti_list or marchi_list or xml_data["has_pi"] == "SÌ") else "NO",
        "kpi_num_brevetti": len(brevetti_list),
        "kpi_num_marchi": len(marchi_list),
        "kpi_num_prodotti": len(prodotti_list),
        "finanza_ha_round": "SÌ" if xml_data["finanziamenti"] else "NO",
        "finanza_totale_raccolto_euro": tot_fin_euro,
        "finanza_num_round": len(xml_data["finanziamenti"]),
        "finanza_tipologie_elenco": ", ".join(set(f["tipo_decod"] for f in xml_data["finanziamenti"])) if xml_data["finanziamenti"] else "NESSUNO",
        "finanza_cronologia_testo": f"{len(xml_data['finanziamenti'])} round registrati" if xml_data["finanziamenti"] else "Nessun finanziamento dichiarato",
        "team_num_membri": len(people_list),
        "team_num_dottorati_phd": num_phds,
        "team_groq_score": ai_result.get("team_score", 50),
        "team_groq_score_rationale": ai_result.get("team_score_rationale", ""),
        "spese_rs_dichiarazione_ri": spese_rs,
        "team_curricula_ri": team_curricula,
        "relazioni_ricerca_ri": rel_ricerca,
        "privative_dichiarazione_ri": privative,
    }

    return {
        "master": master_record,
        "brevetti": brevetti_list,
        "marchi": marchi_list,
        "prodotti": prodotti_list,
        "finanziamenti": xml_data["finanziamenti"],
        "team": people_list,
        "qualifiche": qualifiche_list,
        "associazioni": xml_data["associazioni"],
        "universita": xml_data["universita"],
        "enti": xml_data["enti"],
        "incubatore": {
            "accelerato": xml_data["incubatore_accelerato"],
            "nome": xml_data["incubatore_nome"],
            "cf": xml_data["incubatore_cf"],
        },
    }


def get_next_id(cur, table_name: str, id_col: str, prefix: str) -> str:
    try:
        res = cur.execute(
            f"SELECT COALESCE(MAX(CAST(SUBSTR({id_col}, {len(prefix)+1}) AS INTEGER)), 0) FROM {table_name}"
        ).fetchone()[0]
        return f"{prefix}{res + 1:05d}"
    except Exception:
        return f"{prefix}00001"


def save_transaction(conn, payload, regione: str, tipo: str = "STARTUP"):
    cur = conn.cursor()
    m = payload["master"]
    m["regione"] = regione.upper() if regione else "ITALIA"
    cf = m["cf"]

    if not re.fullmatch(r"IT(?:\d{11}|[A-Z0-9]{16})", cf or ""):
        raise ValueError(f"Codice fiscale/partita IVA non valido: {cf!r}")

    # Supporto duale: PMI mantenuta integra per sviluppi futuri
    target_table = "pmi" if str(tipo).upper() == "PMI" else "startup"
    entity_prefix = "PMI" if target_table == "pmi" else "STARTUP"

    try:
        # Pulizia preliminare sul Codice Fiscale normalizzato
        try:
            company_id = f"{entity_prefix}:{cf}"
            cur.execute(
                """
                DELETE FROM relazioni
                WHERE sorgente = ? OR destinazione = ?
                   OR sorgente IN (SELECT 'PERSONA:' || id FROM persone WHERE cf = ?)
                   OR sorgente IN (SELECT 'FINANZIAMENTO:' || id FROM finanziamenti WHERE cf = ?)
                """,
                (company_id, company_id, cf, cf),
            )
        except sqlite3.OperationalError:
            pass

        for t in ["brevetti", "marchi", "prodotti", "finanziamenti", "persone", "qualifiche"]:
            try:
                cur.execute(f"DELETE FROM {t} WHERE cf = ?", (cf,))
            except sqlite3.OperationalError:
                pass

        # Inserimento Master
        cur.execute(f"PRAGMA table_info({target_table})")
        cols_presenti = set(row[1] for row in cur.fetchall())
        dati_master = {k: v for k, v in m.items() if k in cols_presenti}

        keys = list(dati_master.keys())
        columns = ", ".join(keys)
        placeholders = ", ".join([f":{k}" for k in keys])
        cur.execute(
            f"INSERT OR REPLACE INTO {target_table} ({columns}) VALUES ({placeholders})",
            dati_master,
        )

        # Brevetti
        for b in payload.get("brevetti", []):
            b_id = get_next_id(cur, "brevetti", "id", "BRV")
            cur.execute(
                """
                INSERT INTO brevetti (id, cf, titolo, numero, tipo, titolarita, data, ente, area, verif)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    b_id, cf, b.get("titolo", "Brevetto"), b.get("numero", "NON RILEVATO"),
                    b.get("tipo", "Invenzione"), b.get("titolarita", "Titolare"),
                    b.get("data", ""), b.get("ente", "UIBM"), b.get("area", "Nazionale"),
                    b.get("verif", "Verificabile"),
                ),
            )

        # Marchi
        for mk in payload.get("marchi", []):
            m_id = get_next_id(cur, "marchi", "id", "MRC")
            cur.execute(
                """
                INSERT INTO marchi (id, cf, denominazione, stato, estensione, prodotto)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (m_id, cf, mk.get("denominazione", "Marchio"), mk.get("stato", "Registrato"), mk.get("estensione", "Nazionale"), mk.get("prodotto", "")),
            )

        # Prodotti
        for p in payload.get("prodotti", []):
            p_id = get_next_id(cur, "prodotti", "id", "PRD")
            cur.execute(
                """
                INSERT INTO prodotti (id, cf, nome, categoria, stadio, descrizione)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (p_id, cf, p.get("nome", "Prodotto"), p.get("categoria", "Software"), p.get("stadio", "Sviluppo"), p.get("descrizione", "")),
            )

        # Persone
        for per in payload.get("team", []):
            prs_id = get_next_id(cur, "persone", "id", "PRS")
            nome_p = per.get("nome", "Membro").strip()
            cur.execute(
                """
                INSERT INTO persone (id, cf, nome, ruolo, livello, titolo, eta, is_founder)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (prs_id, cf, nome_p, per.get("ruolo", "Specialista"), per.get("livello", "OPERATIONAL"), per.get("titolo", "Non specificato"), per.get("eta", "Non dichiarata"), per.get("is_founder", "NO")),
            )
            cur.execute(
                """
                INSERT INTO relazioni (sorgente, sorgente_tipo, destinazione, destinazione_tipo, tipo, categoria, peso, metadati)
                VALUES (?, 'PERSONA', ?, ?, 'MEMBRO_TEAM', 'HIERARCHY', 1.0, ?)
                """,
                (f"PERSONA:{prs_id}", f"{entity_prefix}:{cf}", entity_prefix, json.dumps({"cf": cf, "ruolo": per.get("ruolo", ""), "livello": per.get("livello", "OPERATIONAL")})),
            )

        # Qualifiche (Slot dell'XML con ancoraggio al CF della startup)
        for q in payload.get("qualifiche", []):
            q_id = get_next_id(cur, "qualifiche", "id", "QLF")
            cur.execute(
                """
                INSERT INTO qualifiche (id, cf, ruolo, titolo, eta, descrizione_ruolo, persona, stato)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (q_id, cf, q.get("ruolo", "Specialista"), q.get("titolo_studio", "Laurea"), q.get("fascia_eta", "N/D"), q.get("descrizione_ruolo", ""), q.get("persona_nome"), q.get("stato_assegnazione", "ANONIMO")),
            )

        # Finanziamenti & Investitori (Multi-round con gestione nodi condivisi)
        for f in payload.get("finanziamenti", []):
            f_id = get_next_id(cur, "finanziamenti", "id", "FIN")
            inv_str = ", ".join(f.get("investitori", []))
            cur.execute(
                """
                INSERT INTO finanziamenti (id, cf, tipo_cod, tipo_decod, ammontare, data_annuncio, data_chiusura, investitori)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f_id, cf, f.get("tipo_cod", "F"), f.get("tipo_decod", "Finanziamento"), f.get("ammontare", 0), f.get("data_annuncio", ""), f.get("data_chiusura", ""), inv_str),
            )
            cur.execute(
                """
                INSERT INTO relazioni (sorgente, sorgente_tipo, destinazione, destinazione_tipo, tipo, categoria, peso, metadati)
                VALUES (?, 'FINANZIAMENTO', ?, ?, 'FINANZIA', 'FLOW', ?, ?)
                """,
                (f"FINANZIAMENTO:{f_id}", f"{entity_prefix}:{cf}", entity_prefix, f.get("ammontare", 0), json.dumps({"cf": cf, "tipo": f.get("tipo_decod", ""), "data": f.get("data_annuncio", "")})),
            )

            for inv in f.get("investitori", []):
                inv_slug = slugify(inv)
                node_id = f"INVESTITORE:{inv_slug}"
                cur.execute("INSERT OR IGNORE INTO investitori (id, denominazione, categoria) VALUES (?, ?, 'VENTURE_CAPITAL_ANGEL')", (node_id, inv))
                cur.execute(
                    """
                    INSERT INTO relazioni (sorgente, sorgente_tipo, destinazione, destinazione_tipo, tipo, categoria, peso, metadati)
                    VALUES (?, 'INVESTITORE', ?, ?, 'HA_INVESTITO_IN', 'FLOW', ?, ?)
                    """,
                    (node_id, f"{entity_prefix}:{cf}", entity_prefix, f.get("ammontare", 0), json.dumps({"cf": cf, "fin_id": f_id, "azienda": m.get("denominazione_pulita", "")})),
                )

        # Incubatori (con Codice Fiscale dell'incubatore se disponibile)
        inc = payload.get("incubatore", {})
        if inc.get("accelerato") == "SÌ" and inc.get("nome", "NON RILEVATO") != "NON RILEVATO":
            inc_id = f"INCUBATORE:{inc['cf']}" if inc.get("cf") != "NON RILEVATO" else f"INCUBATORE:{slugify(inc['nome'])}"
            cur.execute("INSERT OR IGNORE INTO incubatori (id, denominazione, cf, categoria) VALUES (?, ?, ?, 'ACCELERATORE_CERTIFICATO')", (inc_id, inc["nome"], inc.get("cf", "NON RILEVATO")))
            cur.execute(
                """
                INSERT INTO relazioni (sorgente, sorgente_tipo, destinazione, destinazione_tipo, tipo, categoria, peso, metadati)
                VALUES (?, 'INCUBATORE', ?, ?, 'ACCELERATA_DA', 'NETWORK', 1.0, ?)
                """,
                (inc_id, f"{entity_prefix}:{cf}", entity_prefix, json.dumps({"cf": cf, "incubatore_cf": inc.get("cf", "")})),
            )

        # Università (TA2 - Nodo condiviso collegato alla P.IVA)
        for u in payload.get("universita", []):
            u_id = f"UNIVERSITA:{slugify(u)}"
            cur.execute("INSERT OR IGNORE INTO universita (id, denominazione, categoria) VALUES (?, ?, 'CENTRO_RICERCA')", (u_id, u))
            cur.execute(
                """
                INSERT INTO relazioni (sorgente, sorgente_tipo, destinazione, destinazione_tipo, tipo, categoria, peso, metadati)
                VALUES (?, 'UNIVERSITA', ?, ?, 'PARTNER_ACCADEMICO', 'NETWORK', 1.0, ?)
                """,
                (u_id, f"{entity_prefix}:{cf}", entity_prefix, json.dumps({"cf": cf})),
            )

        # Enti Istituzionali & Certificatori
        for en in payload.get("enti", []):
            en_nome = en.get("nome", en) if isinstance(en, dict) else en
            en_cat = en.get("categoria", "SOSTEGNO_PUBBLICO") if isinstance(en, dict) else "SOSTEGNO_PUBBLICO"
            en_rel = "SOSTENUTA_DA" if en_cat == "SOSTEGNO_PUBBLICO" else "CERTIFICATA_DA"

            en_id = f"ENTE:{slugify(en_nome)}"
            cur.execute("INSERT OR IGNORE INTO enti (id, denominazione, categoria) VALUES (?, ?, ?)", (en_id, en_nome, en_cat))
            cur.execute(
                """
                INSERT INTO relazioni (sorgente, sorgente_tipo, destinazione, destinazione_tipo, tipo, categoria, peso, metadati)
                VALUES (?, 'ENTE', ?, ?, ?, 'NETWORK', 1.0, ?)
                """,
                (en_id, f"{entity_prefix}:{cf}", entity_prefix, en_rel, json.dumps({"cf": cf})),
            )

        # Associazioni (TA1)
        for a in payload.get("associazioni", []):
            ass_id = f"ASSOCIAZIONE:{slugify(a['nome'])}"
            cur.execute("INSERT OR IGNORE INTO associazioni (id, denominazione, categoria) VALUES (?, ?, ?)", (ass_id, a["nome"], a.get("tipo", "Associazione")))
            cur.execute(
                """
                INSERT INTO relazioni (sorgente, sorgente_tipo, destinazione, destinazione_tipo, tipo, categoria, peso, metadati)
                VALUES (?, 'ASSOCIAZIONE', ?, ?, 'ASSOCIATA_A', 'NETWORK', 1.0, ?)
                """,
                (ass_id, f"{entity_prefix}:{cf}", entity_prefix, json.dumps({"cf": cf})),
            )

        try:
            cur.execute("UPDATE coda SET stato = 'COMPLETED' WHERE url = ?", (m["url_scheda"],))
        except Exception:
            pass

        conn.commit()

    except Exception as e:
        conn.rollback()
        raise e


async def create_browser_and_page(p, use_zenrows, proxy_cfg):
    browser = None
    if use_zenrows:
        try:
            log("INFO", "🌐 Connessione a Browser Residenziale ZenRows (Italia)...")
            browser = await connect_zenrows_browser(p)
            page = await browser.new_page()
            return browser, page
        except Exception as e:
            log("ERROR", f"Connessione ZenRows fallita ({type(e).__name__}); nessun fallback diretto.")
            raise RuntimeError("ZenRows richiesto ma non disponibile") from e

    browser = await p.chromium.launch(
        headless=True,
        proxy=proxy_cfg,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    page = await browser.new_page()
    return browser, page


async def run_worker_standalone(
    limit: int,
    delay_sec: float,
    proxy_mode: str,
    tipo_filter: str = "STARTUP",
    only_url: str = None,
):
    conn = get_db()
    cur = conn.cursor()

    if "zenrows" in (proxy_mode or "").lower() and not ZENROWS_API_KEYS:
        log("ERROR", "ZenRows richiesto ma ZENROWS_API_KEY non configurata.")
        conn.close()
        return

    try:
        limit_value = limit if limit != -1 else 1000
        if only_url:
            query = (
                "SELECT url, denominazione, regione, tipo FROM coda "
                "WHERE stato = 'PENDING' AND url = ? LIMIT 1"
            )
            pending = cur.execute(query, (only_url,)).fetchall()
        elif tipo_filter.upper() == "ALL":
            query = "SELECT url, denominazione, regione, tipo FROM coda WHERE stato = 'PENDING' ORDER BY created_at ASC, rowid ASC LIMIT ?"
            pending = cur.execute(query, (limit_value,)).fetchall()
        else:
            query = "SELECT url, denominazione, regione, tipo FROM coda WHERE stato = 'PENDING' AND UPPER(tipo) = ? ORDER BY created_at ASC, rowid ASC LIMIT ?"
            pending = cur.execute(query, (tipo_filter.upper(), limit_value)).fetchall()
    except Exception:
        pending = []

    if not pending:
        log("WARN", f"Nessun link PENDING per '{tipo_filter.upper()}' trovato in coda.")
        conn.close()
        return

    log(
        "INFO",
        f"=== AVVIO DEEP WORKER: {len(pending)} schede [{tipo_filter.upper()}] da elaborare con Groq (Route: {proxy_mode}) ===",
    )

    use_zenrows = "zenrows" in (proxy_mode or "").lower()
    proxy_cfg = None if use_zenrows else get_playwright_proxy_config(proxy_mode)

    async with async_playwright() as p:
        browser, page = await create_browser_and_page(p, use_zenrows, proxy_cfg)

        processed = 0
        for card_url, raw_name, reg, rec_tipo in pending:
            try:
                if (
                    "startup.registroimprese.it" not in card_url
                    or "dettaglioStartup" not in card_url
                ):
                    log("WARN", f"URL non camerale scartato: {card_url}")
                    cur.execute("UPDATE coda SET stato = 'FAILED' WHERE url = ?", (card_url,))
                    conn.commit()
                    continue

                log(
                    "INFO",
                    f"Elaborazione dossier [{processed+1}/{len(pending)}] [{rec_tipo}]: {raw_name}...",
                )

                # Auto-recovery se scade il timeout WebSocket a 180s
                try:
                    data_payload = await extract_complete_company(page, card_url, fallback_name=raw_name)
                except Exception as conn_err:
                    if "closed" in str(conn_err).lower() or "target" in str(conn_err).lower():
                        log("WARN", "🔄 Sessione browser interrotta (timeout). Ripristino sessione CDP...")
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        browser, page = await create_browser_and_page(p, use_zenrows, proxy_cfg)
                        data_payload = await extract_complete_company(page, card_url, fallback_name=raw_name)
                    else:
                        raise conn_err

                save_transaction(conn, data_payload, reg, tipo=rec_tipo)
                processed += 1
                m = data_payload["master"]
                log(
                    "SUCCESS",
                    f"Dossier #{processed} [{rec_tipo}] salvato: {m['denominazione_pulita']} [{m['cf']}] "
                    f"| Logo: {m['logo_url']} | PhD: {m['team_num_dottorati_phd']} | Round: {m['finanza_num_round']}",
                )
                await asyncio.sleep(random.uniform(delay_sec * 0.8, delay_sec * 1.2))

            except Exception as e:
                log("ERROR", f"Errore su {raw_name}: {e}")
                try:
                    cur.execute("UPDATE coda SET stato = 'FAILED' WHERE url = ?", (card_url,))
                    conn.commit()
                except Exception:
                    pass

        try:
            await browser.close()
        except Exception:
            pass

    conn.close()

    # Esportazione dataset in output
    try:
        conn = get_db()
        df_s = pd.read_sql_query("SELECT * FROM startup", conn)
        df_p = pd.read_sql_query("SELECT * FROM pmi", conn)
        conn.close()

        out_dir = WORKER_OUTPUT_DIR
        os.makedirs(out_dir, exist_ok=True)

        if not df_s.empty:
            df_s.to_csv(
                os.path.join(out_dir, "startup_estratte.csv"),
                index=False,
                sep=";",
                encoding="utf-8-sig",
            )
            df_s.to_excel(os.path.join(out_dir, "startup_estratte.xlsx"), index=False)
        if not df_p.empty:
            df_p.to_csv(
                os.path.join(out_dir, "pmi_estratte.csv"),
                index=False,
                sep=";",
                encoding="utf-8-sig",
            )
            df_p.to_excel(os.path.join(out_dir, "pmi_estratte.xlsx"), index=False)

        log("SUCCESS", "Dataset di output aggiornati con successo.")
    except Exception as e:
        log("WARN", f"Export output non riuscito: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deep Worker Groq AI & ImgBB")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--delay", type=float, default=2.5)
    parser.add_argument("--proxy", default="zenrows,webshare")
    parser.add_argument(
        "--tipo",
        default="STARTUP",
        choices=["STARTUP", "PMI", "ALL", "startup", "pmi", "all"],
    )
    args = parser.parse_args()

    asyncio.run(
        run_worker_standalone(args.limit, args.delay, args.proxy, args.tipo)
    )
