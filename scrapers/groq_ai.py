import json
import os
import re
import urllib.error
import urllib.request
from dotenv import load_dotenv

# Ancoraggio assoluto alla root del progetto
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

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
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Cache globale per interrogare /models una sola volta
_DISCOVERED_MODELS_CACHE = []

# ==============================================================================
# SELEZIONE DINAMICA DEI MODELLI GROQ (CON USER-AGENT E FALLBACK CERTIFICATI)
# ==============================================================================
def get_best_available_models(api_key: str) -> list:
    """Interroga dinamicamente Groq con User-Agent valido e seleziona i modelli attivi."""
    global _DISCOVERED_MODELS_CACHE
    if _DISCOVERED_MODELS_CACHE:
        return _DISCOVERED_MODELS_CACHE

    # Modelli di fallback certificati e funzionanti nel tuo account
    default_fallback = [
        "openai/gpt-oss-20b",           # Veloce, leggero, difficilissimo da mandare in 429
        "qwen/qwen3.8-27b",             # Eccellente per estrazioni JSON
        "openai/gpt-oss-120b",          # Altissima precisione per casi complessi
        "deepseek-r1-distill-llama-70b" # Di riserva
    ]

    if not api_key:
        return default_fallback

    try:
        req = urllib.request.Request(
            f"{GROQ_BASE_URL}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            all_models = [m.get("id", "") for m in data.get("data", []) if m.get("active", True)]

        # Esclude modelli non testuali
        chat_models = [
            m for m in all_models
            if not any(x in m.lower() for x in ["whisper", "vision", "guard", "embed", "safeguard"])
        ]

        # Priorità: modelli veloci a basso consumo token prima, modelli pesanti dopo
        priority_order = [
            "openai/gpt-oss-20b",
            "qwen/qwen3.8-27b",
            "openai/gpt-oss-120b",
            "deepseek-r1-distill-llama-70b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
        ]

        selected = []
        for p in priority_order:
            if p in chat_models and p not in selected:
                selected.append(p)

        # Se ne mancano, aggiunge gli altri disponibili
        for cm in chat_models:
            if cm not in selected:
                selected.append(cm)
            if len(selected) >= 4:
                break

        _DISCOVERED_MODELS_CACHE = selected if selected else default_fallback
        print(f"[GROQ] Modelli pronti all'uso: {_DISCOVERED_MODELS_CACHE}")
        return _DISCOVERED_MODELS_CACHE

    except Exception as e:
        print(f"[GROQ] Impossibile recuperare lista dinamica ({e}). Uso fallback certificati.")
        _DISCOVERED_MODELS_CACHE = default_fallback
        return _DISCOVERED_MODELS_CACHE


# ==============================================================================
# SYSTEM PROMPT RIGOROSO AD ALTA DENSITÀ
# ==============================================================================
SYSTEM_PROMPT = """Sei un analista senior specializzato nell'ecosistema delle startup innovative italiane.
Il tuo compito è analizzare la tabella TSV fornita ed estrarre i dati in formato JSON strutturato con la massima precisione e ZERO ALLUCINAZIONI.

REGOLE FERREE:

1. "nome_pulito":
   - Rimuovi TUTTE le desinenze societarie (SRL, S.R.L., SRLS, SPA, S.P.A., SOCIETÀ BENEFIT, BENEFIT, S.B., SOCIETÀ AGRICOLA, 'IN FORMA ABBREVIATA...', 'IN SIGLA...').
   - Estrai SOLO il BRAND commerciale puro dell'azienda in MAIUSCOLO (es. "DEVON", "WUOZ ITALIA", "D-MOD", "KEPLERA", "SUITE").

2. "prodotti" (CATEGORIA E BRAND/NOME):
   - Per ogni prodotto individua "categoria" (es. "LegalTech", "MedTech", "Manifattura 3D", "SaaS B2B", "AI & ML", "Marketing Automation").
   - Per "nome":
     * Se l'azienda cita un brand commerciale esplicito (es. "LATTICE", "LexHero", "DEVON AI", "OcuSuite", "Refertly") -> usa quel nome esatto.
     * Se NON c'è un brand esplicito, componi rigorosamente: [CATEGORIA] + [NOME AZIENDA] (es. "Piattaforma B2B REWIND", "AI Reliability PRINCIPLED INTELLIGENCE").
     * VIETATO usare segnaposti generici come "Soluzione", "Prodotto" o "Piattaforma Aziendale".

3. "persone" (ANAGRAFICA REALE - NOME E COGNOME):
   - Includi sempre il Legale Rappresentante normalizzato in formato "Nome Cognome" (Title Case, es. "Mario Rossi", MAI "ROSSI MARIO").
   - Estrai altre persone SOLO ed ESCLUSIVAMENTE se nel testo compaiono NOMI E COGNOMI umani reali ed espliciti.
   - VIETATO categoricamente estrarre atenei ("Di Bologna"), diciture di laurea ("Vecchio Ordinamento"), ruoli generici senza nome o frasi burocratiche.
   - Se nel testo non sono presenti nomi e cognomi oltre al legale rappresentante, NON inventarli.
   - Campi per ciascuna persona:
     * "nome": "Nome Cognome"
     * "ruolo": Ruolo aziendale effettivo
     * "livello_gerarchico": "C_LEVEL" (CEO, CTO, COO), "FOUNDER", "BOARD", "LEAD", "OPERATIONAL"
     * "titolo": Titolo di studio ("Dottorato di ricerca (PhD)", "Laurea magistrale", "Non specificato")
     * "eta": Fascia d'età o "Non dichiarata"
     * "is_founder": "SÌ" o "NO"

4. "qualifiche" (SLOT FORMALI DELL'XML CAMERA DI COMMERCIO):
   - Per ciascuna postazione dichiarata nella riga QUALIFICHE_XML_JSON crea un elemento corrispondente:
     * "ruolo": Ruolo dichiarato nello slot
     * "titolo_studio": Titolo formale dichiarato
     * "fascia_eta": Fascia anagrafica dichiarata
     * "persona_nome": Inserisci "Nome Cognome" SOLO se hai la certezza assoluta da testo o firma digitale che quella persona ricopre quel ruolo specifico; altrimenti inserisci null.
     * "stato_assegnazione": "ASSEGNATA" se associata con certezza a una persona reale nota, altrimenti obbligatoriamente "ANONIMO".

5. "team_score" e "team_score_rationale" (SCORE DELLA STARTUP):
   - "team_score": Valutazione complessiva della startup da 0 a 100 basata sulla compagine del team:
     * 0-35: Solo 1 persona o team generico non documentato
     * 36-65: Team standard con lauree magistrali e ruoli definiti
     * 66-85: Team solido con profili C-Level completi e presenza di Dottorati (PhD)
     * 86-100: Eccellenza scientifica (3+ Dottorati di ricerca PhD, ruoli STEM specializzati)
   - "team_score_rationale": Breve motivazione sintetica di 1 riga del punteggio assegnato alla startup.
"""


def clean_json_response(raw_text: str) -> dict:
    """Sanitizza il testo rimuovendo tag reasoning anche non chiusi, blocchi markdown e trailing commas."""
    text = raw_text.strip()

    # 1. Rimuove blocchi <think>...</think> o tag <think> troncati
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.DOTALL).strip()

    # 2. Rimuove blocchi markdown ```json ... ```
    if "```" in text:
        match_code = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match_code:
            text = match_code.group(1).strip()
        else:
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()

    # 3. Estrae solo il payload compreso tra la prima { e l'ultima }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    # 4. Rimuove le trailing commas che rompono json.loads
    text = re.sub(r",\s*([\]}])", r"\1", text)

    return json.loads(text)


def build_single_startup_tsv(
    raw_name: str,
    legal_rep: str,
    team_curricula_ri: str,
    pitch_it: str,
    prod_desc_it: str,
    soggetti_xml: list,
) -> str:
    """Costruisce una tabella TSV compatta e pulita per il prompt LLM."""

    def clean_t(txt, max_len):
        if not txt:
            return "NON DICHIARATO"
        cleaned = txt.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned[:max_len]

    rows = [
        f"RAGIONE_SOCIALE\t{clean_t(raw_name, 250)}",
        f"LEGALE_RAPPRESENTANTE\t{clean_t(legal_rep, 150)}",
        f"CURRICULA_CCIAA\t{clean_t(team_curricula_ri, 2500)}",
        f"DESCRIZIONE_PRODOTTI\t{clean_t(prod_desc_it, 1500)}",
        f"PITCH_AZIENDA\t{clean_t(pitch_it, 1500)}",
        f"QUALIFICHE_XML_JSON\t{json.dumps(soggetti_xml, ensure_ascii=False) if soggetti_xml else 'NESSUNA'}",
    ]
    return "\n".join(rows)


def analyze_dossier_with_groq(
    raw_name: str,
    legal_rep: str,
    team_curricula_ri: str,
    pitch_it: str,
    prod_desc_it: str,
    soggetti_xml: list,
) -> dict:
    """Invia il dossier a Groq AI selezionando dinamicamente i modelli migliori

    con gestione automatica dei fallback e del rate-limiting.
    """
    if not GROQ_API_KEY:
        raise ValueError("ERRORE: GROQ_API_KEY non trovata! Verifica il file .env.")

    tsv_payload = build_single_startup_tsv(
        raw_name,
        legal_rep,
        team_curricula_ri,
        pitch_it,
        prod_desc_it,
        soggetti_xml,
    )

    prompt = f"""Analizza la seguente tabella TSV della startup ed estrai i dati nel formato JSON richiesto:

{tsv_payload}

Rispondi ESCLUSIVAMENTE con la seguente struttura JSON valida:
{{
  "nome_pulito": "BRAND PURO",
  "team_score": 85,
  "team_score_rationale": "Motivazione sintetica del punteggio...",
  "prodotti": [
    {{
      "nome": "Nome Brand o Categoria + Nome Azienda",
      "categoria": "Categoria Software/Hardware",
      "stadio": "Sul mercato",
      "descrizione": "Descrizione sintetica..."
    }}
  ],
  "persone": [
    {{
      "nome": "Nome Cognome",
      "ruolo": "CEO",
      "livello_gerarchico": "C_LEVEL",
      "titolo": "Dottorato di ricerca (PhD)",
      "eta": "30-34",
      "is_founder": "SÌ"
    }}
  ],
  "qualifiche": [
    {{
      "ruolo": "Mansione",
      "titolo_studio": "Titolo Studio",
      "fascia_eta": "Fascia Eta",
      "persona_nome": "Nome Cognome oppure null",
      "stato_assegnazione": "ASSEGNATA o ANONIMO"
    }}
  ]
}}"""

    models_to_try = get_best_available_models(GROQ_API_KEY)
    last_err = None

    for m in models_to_try:
        payload = {
            "model": m,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 2500,
            "response_format": {"type": "json_object"},
        }

        req = urllib.request.Request(
            f"{GROQ_BASE_URL}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                content = res["choices"][0]["message"]["content"]
                return clean_json_response(content)
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            print(f"[GROQ] Modello '{m}' non disponibile (HTTP {e.code}: {err_msg[:90]}). Provo il successivo...")
            last_err = e
            continue
        except Exception as e:
            print(f"[GROQ] Errore con modello '{m}': {e}. Provo il successivo...")
            last_err = e
            continue

    if last_err:
        raise last_err
    return {}
