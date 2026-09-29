import asyncio
import concurrent.futures
import json
import os
import random
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse
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

WEBSHARE_API_KEY = os.getenv("WEBSHARE_API_KEY", "").strip()
PROXY_FILE = os.path.join(PROJECT_ROOT, "data", "proxies.txt")
os.makedirs(os.path.dirname(PROXY_FILE), exist_ok=True)

TOR_SOCKS5 = "socks5://127.0.0.1:9050"
CONNECTIVITY_TEST_URL = "https://api.ipify.org"


def get_zenrows_api_keys() -> tuple:
  return tuple(dict.fromkeys(
      key for key in (
          os.getenv("ZENROWS_API_KEY", "").strip(),
          os.getenv("ZENROWS_API_KEY_2", "").strip(),
      ) if key
  ))


async def connect_zenrows_browser(playwright, timeout: float = 15.0):
  api_keys = get_zenrows_api_keys()
  if not api_keys:
    raise RuntimeError("Nessuna chiave ZenRows configurata.")

  last_error = None
  for index, api_key in enumerate(api_keys, start=1):
    endpoint = f"wss://browser.zenrows.com?apikey={api_key}&proxy_country=it"
    try:
      browser = await asyncio.wait_for(
          playwright.chromium.connect_over_cdp(endpoint), timeout=timeout
      )
      if index > 1:
        print(f"[SUCCESS] [ZenRows] Connessione riuscita con chiave fallback #{index}.")
      return browser
    except Exception as error:
      last_error = error
      if index < len(api_keys):
        print(
            f"[WARN] [ZenRows] Chiave #{index} non disponibile; "
            f"provo la chiave fallback #{index + 1}."
        )

  raise RuntimeError(
      f"Connessione ZenRows fallita con tutte le {len(api_keys)} chiavi configurate."
  ) from last_error


def parse_proxy_dict(proxy_str: str):
  """Formatta qualsiasi proxy per Playwright.

  Restituisce None se vuoto o non valido (evita crash di Playwright).
  """
  if not proxy_str or not isinstance(proxy_str, str):
    return None

  proxy_str = proxy_str.strip()
  if not proxy_str:
    return None

  if not any(
      proxy_str.startswith(proto)
      for proto in ("http://", "https://", "socks5://")
  ):
    proxy_str = f"http://{proxy_str}"

  try:
    parsed = urlparse(proxy_str)
    if not parsed.hostname:
      return None

    port = f":{parsed.port}" if parsed.port else ""
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}{port}"}
    if parsed.username:
      cfg["username"] = parsed.username
    if parsed.password:
      cfg["password"] = parsed.password
    return cfg
  except Exception:
    return None


def is_tor_running(host="127.0.0.1", port=9050, timeout=1.0) -> bool:
  """Verifica se il demone Tor SOCKS5 è attivo sulla porta locale 9050."""
  try:
    with socket.create_connection((host, port), timeout=timeout):
      return True
  except (socket.timeout, ConnectionRefusedError, OSError):
    return False


def test_proxy_live(proxy_url: str, timeout: float = 2.5) -> bool:
  """Test rapido di connettività HTTPS in uscita tramite proxy."""
  try:
    handler = urllib.request.ProxyHandler(
        {"http": proxy_url, "https": proxy_url}
    )
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(
        CONNECTIVITY_TEST_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0"
            )
        },
    )
    with opener.open(req, timeout=timeout) as resp:
      return resp.status == 200
  except Exception:
    return False


def fetch_webshare_proxies(api_key: str) -> list:
  """Scarica i proxy dedicati dall'API di Webshare v2."""
  if not api_key:
    return []

  proxies = []
  print("[INFO] [ProxyManager] Connessione a Webshare API v2...")

  endpoints = [
      "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100&country_code_in=IT,DE,FR,ES,NL,GB",
      "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100",
      "https://proxy.webshare.io/api/v2/proxy/list/?mode=backbone&page=1&page_size=100",
  ]

  for url in endpoints:
    try:
      req = urllib.request.Request(
          url,
          headers={
              "Authorization": f"Token {api_key}",
              "User-Agent": "PrivateLab-ProxyManager/2.0",
          },
      )
      with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("results", []):
          if item.get("valid", True):
            u = item.get("username")
            p = item.get("password")
            ip = item.get("proxy_address") or "p.webshare.io"
            port = item.get("port") or 80
            if u and p:
              proxies.append(f"http://{u}:{p}@{ip}:{port}")
            else:
              proxies.append(f"http://{ip}:{port}")

        if proxies:
          print(
              f"[SUCCESS] [ProxyManager] Scaricati {len(proxies)} proxy da"
              " Webshare!"
          )
          break
    except urllib.error.HTTPError as e:
      print(f"[WARN] [ProxyManager] Errore Webshare API (HTTP {e.code})")
    except Exception as e:
      print(f"[WARN] [ProxyManager] Connessione Webshare fallita: {e}")

  if proxies:
    try:
      with open(PROXY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(proxies))
    except Exception:
      pass

  return proxies


def fetch_and_test_free_public_proxies(max_candidates: int = 25) -> list:
  """Scarica ed esamina proxy pubblici di riserva."""
  print("[INFO] [ProxyManager] Ricerca proxy gratuiti di riserva...")
  sources = [
      (
          "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=2000&country=IT,DE,FR,ES,NL,GB&ssl=yes&anonymity=all"
      ),
      "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
  ]

  candidates = set()
  for s in sources:
    try:
      req = urllib.request.Request(s, headers={"User-Agent": "Mozilla/5.0"})
      with urllib.request.urlopen(req, timeout=4) as resp:
        txt = resp.read().decode("utf-8", errors="ignore")
        matches = re.findall(
            r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}\b", txt
        )
        for m in matches[:30]:
          candidates.add(f"http://{m}")
      if len(candidates) >= max_candidates:
        break
    except Exception:
      pass

  candidate_list = list(candidates)[:max_candidates]
  if not candidate_list:
    return []

  verified = []
  executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
  try:
    future_to_proxy = {
        executor.submit(test_proxy_live, p, 2.0): p for p in candidate_list
    }
    for future in concurrent.futures.as_completed(
        future_to_proxy, timeout=5.0
    ):
      p = future_to_proxy[future]
      try:
        if future.result():
          verified.append(p)
          if len(verified) >= 2:
            break
      except Exception:
        pass
  except Exception:
    pass
  finally:
    executor.shutdown(wait=False)

  return verified


class ProxyManager:

  def __init__(self):
    self.webshare_pool = []
    self.free_verified_pool = []
    self.current_idx = 0
    self.initialize_pools()

  def initialize_pools(self):
    # 1. Pool Primario da Webshare API
    if WEBSHARE_API_KEY:
      self.webshare_pool = fetch_webshare_proxies(WEBSHARE_API_KEY)
      if self.webshare_pool:
        random.shuffle(self.webshare_pool)
        return

    # 2. Cache locale proxies.txt
    if os.path.exists(PROXY_FILE):
      try:
        with open(PROXY_FILE, "r", encoding="utf-8") as f:
          lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
          if lines:
            print(
                f"[INFO] [ProxyManager] Caricati {len(lines)} proxy da cache"
                f" {PROXY_FILE}"
            )
            self.webshare_pool = lines
            return
      except Exception:
        pass

  def get_proxy(self) -> str:
    """Restituisce il proxy disponibile con priorità a cascata (Webshare -> Tor -> Free -> Direct)."""
    if self.webshare_pool:
      p = self.webshare_pool[self.current_idx % len(self.webshare_pool)]
      self.current_idx = (self.current_idx + 1) % len(self.webshare_pool)
      return p

    if is_tor_running():
      print("[INFO] [ProxyManager] Attivazione fallback su Tor (9050)...")
      return TOR_SOCKS5

    if self.free_verified_pool:
      return self.free_verified_pool.pop(0)
    else:
      self.free_verified_pool = fetch_and_test_free_public_proxies()
      if self.free_verified_pool:
        return self.free_verified_pool.pop(0)

    print(
        "[WARN] [ProxyManager] Nessun proxy attivo. Connessione diretta locale."
    )
    return ""

  def mark_failed(self, proxy: str):
    """Rimuove in sicurezza un proxy non funzionante dal pool."""
    try:
      if proxy in self.webshare_pool:
        self.webshare_pool.remove(proxy)
    except ValueError:
      pass


# Pool inizializzato solo quando viene richiesta esplicitamente una route Webshare.
proxy_pool = None


def get_playwright_proxy_config(proxy_mode: str):
  """Funzione ponte universale per Playwright (usata da harvester.py e worker.py).

  Restituisce il dict {'server': ..., 'username': ..., 'password': ...} oppure
  None se connessione diretta.
  """
  mode = (proxy_mode or "direct").lower()

  # Connessione diretta esplicita
  if mode == "direct":
    return None

  # Priorità Webshare
  if "webshare" in mode:
    global proxy_pool
    if proxy_pool is None:
      proxy_pool = ProxyManager()
    raw_p = proxy_pool.get_proxy()
    return parse_proxy_dict(raw_p)

  # Fallback Tor SOCKS5
  if "tor" in mode:
    return parse_proxy_dict(TOR_SOCKS5)

  return None
