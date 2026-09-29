import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


APIFY_API_KEY = os.getenv("APIFY_API_KEY", "").strip()
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "apify/web-scraper").strip()
APIFY_API_BASE = "https://api.apify.com/v2"
PORTAL_HOME = "https://startup.registroimprese.it/isin/home"

PAGE_FUNCTION = r"""async function pageFunction(context) {
    const { page, request } = context;
    const { cf, tipo } = request.userData;
    const normalizedCf = String(cf || '').replace(/[^A-Z0-9]/gi, '').toUpperCase();
    const outcome = (name) => ({ outcome: name, cf: normalizedCf });
    const waitResponse = (predicate, timeoutMs) => new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            page.removeListener('response', listener);
            reject(new Error('response_timeout'));
        }, timeoutMs);
        const listener = response => {
            try {
                if (!predicate(response)) return;
                clearTimeout(timer);
                page.removeListener('response', listener);
                resolve(response);
            } catch (_) {}
        };
        page.on('response', listener);
    });
    const classifyText = text => {
        const normalized = String(text || '').replace(/\s+/g, ' ').toLowerCase();
        if (/captcha|access denied|verifica di sicurezza|request rejected|accesso bloccato|\bforbidden\b/.test(normalized)) return 'challenge';
        if (/richiesta non valida|invalid request/.test(normalized)) return 'invalid_request';
        return '';
    };
    const waitForTypeAck = async (fieldName) => {
        const checked = await page.$eval(`input[name="${fieldName}"]`, input => input.checked);
        if (checked) return true;
        const responsePromise = waitResponse(response => response.url().includes(`${fieldName.split(':')[0]}-chkFld`), 10000);
        await page.$eval(`input[name="${fieldName}"]`, input => {
            input.closest('.field')?.querySelector('.ui.checkbox')?.click();
        });
        const response = await responsePromise;
        return response.status() >= 200 && response.status() < 300;
    };

    try {
        await page.goto(request.url, { waitUntil: 'domcontentloaded', timeout: 35000 });
        const startupField = 'startupChk:chkFld';
        const pmiField = 'pmiChk:chkFld';
        const wantedTypeField = String(tipo || 'STARTUP').toUpperCase() === 'PMI' ? pmiField : startupField;
        if (!await waitForTypeAck(wantedTypeField)) return outcome('invalid_request');

        const searchSelector = 'input[name="parolaChiaveFld"]';
        await page.waitForSelector(searchSelector, { timeout: 10000 });
        await page.focus(searchSelector);
        await page.keyboard.type(normalizedCf);
        const fieldAckPromise = waitResponse(response => response.url().includes('parolaChiaveFld'), 10000);
        await page.keyboard.press('Tab');
        const fieldAck = await fieldAckPromise;
        if (fieldAck.status() < 200 || fieldAck.status() >= 300) return outcome('http_error');

        const searchResponsePromise = waitResponse(
            response => response.request().method() === 'POST' && response.url().includes('searchBtn'),
            15000
        );
        await page.click('a.searchBtnVetrina');
        const searchResponse = await searchResponsePromise;
        const responseText = await searchResponse.text().catch(() => '');
        const bodyOutcome = classifyText(responseText);
        if (bodyOutcome) return outcome(bodyOutcome);
        if (searchResponse.status() < 200 || searchResponse.status() >= 300) return outcome('http_error');

        await page.waitForFunction(cfValue => {
            const text = document.body?.innerText || '';
            const cards = Array.from(document.querySelectorAll('.searchCompanyCard'));
            const found = cards.some(card => {
                const matches = [...card.innerText.matchAll(/Codice\s*fiscale\s*([A-Z0-9]{11,16})/gi)];
                return matches.some(match => match[1].replace(/[^A-Z0-9]/gi, '').toUpperCase() === cfValue);
            });
            return found || /richiesta non valida|invalid request|captcha|access denied|request rejected/i.test(text);
        }, { timeout: 12000 }, normalizedCf).catch(() => null);

        const searchState = await page.evaluate(cfValue => {
            const text = document.body?.innerText || '';
            const cards = Array.from(document.querySelectorAll('.searchCompanyCard'));
            const card = cards.find(candidate => {
                const matches = [...candidate.innerText.matchAll(/Codice\s*fiscale\s*([A-Z0-9]{11,16})/gi)];
                return matches.some(match => match[1].replace(/[^A-Z0-9]/gi, '').toUpperCase() === cfValue);
            });
            return {
                cardId: card?.id || '',
                name: card?.querySelector('h5 a, h3 span, h5')?.textContent?.trim() || '',
                error: /richiesta non valida|invalid request/i.test(text) ? 'invalid_request' :
                    (/captcha|access denied|request rejected|verifica di sicurezza/i.test(text) ? 'challenge' : '')
            };
        }, normalizedCf);
        if (searchState.error) return outcome(searchState.error);
        if (!searchState.cardId) return outcome('unknown');

        const detailResponsePromise = waitResponse(
            response => response.url().includes('buttonLink') || response.url().includes('dettaglioStartup'),
            12000
        );
        await page.$eval(`#${searchState.cardId}`, card => {
            const link = card.querySelector('a.link, .button');
            if (!link) throw new Error('detail_link_missing');
            link.click();
        });
        const detailResponse = await detailResponsePromise;
        const detailResponseText = await detailResponse.text().catch(() => '');
        const redirect = detailResponseText.match(/<redirect><!\[CDATA\[(.*?)\]\]><\/redirect>/s);
        let detailUrl = page.url();
        if (redirect) detailUrl = new URL(redirect[1].replace(/&amp;/g, '&'), request.url).href;
        if (!detailUrl.includes('dettaglioStartup')) return outcome('unknown');
        if (!page.url().includes('dettaglioStartup')) {
            await page.goto(detailUrl, { waitUntil: 'domcontentloaded', timeout: 35000 });
        }
        await page.waitForFunction(() =>
            !!document.querySelector('#companyNameForGA, #downloadPnl a[href], h2.roundedtop span'),
            { timeout: 15000 }
        );

        const detailState = await page.evaluate(() => {
            const text = document.body?.innerText || '';
            return {
                challenge: /captcha|access denied|request rejected|verifica di sicurezza/i.test(text),
                invalid: /richiesta non valida|invalid request/i.test(text),
                html: document.documentElement.outerHTML
            };
        });
        if (detailState.challenge) return outcome('challenge');
        if (detailState.invalid) return outcome('invalid_request');

        const bindings = await page.evaluate(() => {
            const scripts = Array.from(document.scripts, script => script.textContent || '').join('\n');
            const matches = [...scripts.matchAll(/Wicket\.Ajax\.ajax\((\{[^)]*\})\)/g)];
            for (const match of matches) {
                try {
                    const binding = JSON.parse(match[1]);
                    if (binding.u?.includes('downloadXmlLnk')) return binding;
                } catch (_) {}
            }
            return null;
        });
        if (!bindings?.u) return outcome('unknown');

        const xmlResult = await page.evaluate(async binding => {
            const base = window.Wicket?.Ajax?.baseUrl?.replaceAll('&amp;', '&') || location.pathname + location.search;
            const response = await fetch(binding.u, {
                method: (binding.m || 'GET').toUpperCase(),
                credentials: 'same-origin',
                headers: {
                    'Wicket-Ajax': 'true',
                    'Wicket-Ajax-BaseURL': base,
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });
            return { status: response.status, text: await response.text() };
        }, bindings);
        if (xmlResult.status < 200 || xmlResult.status >= 300) return outcome('http_error');
        let xml = xmlResult.text;
        if (!/<(?:[\w.-]+:)?dichiarazione(?:\s|>)/i.test(xml)) {
            const xmlRedirect = xml.match(/<redirect><!\[CDATA\[(.*?)\]\]><\/redirect>/s);
            if (xmlRedirect) {
                const downloadUrl = new URL(xmlRedirect[1].replace(/&amp;/g, '&'), detailUrl).href;
                const downloaded = await page.evaluate(async url => {
                    const response = await fetch(url, { credentials: 'same-origin' });
                    return { status: response.status, text: await response.text() };
                }, downloadUrl);
                if (downloaded.status >= 200 && downloaded.status < 300) xml = downloaded.text;
            }
        }
        if (!/<(?:[\w.-]+:)?dichiarazione(?:\s|>)/i.test(xml)) return outcome('unknown');

        return {
            outcome: 'found',
            cf: normalizedCf,
            name: searchState.name,
            detailUrl,
            html: detailState.html,
            xml
        };
    } catch (error) {
        const text = String(error?.message || error);
        const marker = classifyText(text);
        return outcome(marker || 'unknown');
    }
}"""


class ApifyFallbackError(RuntimeError):
    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__(f"Apify fallback non completato ({outcome}).")


def build_apify_input(cf: str, tipo: str = "STARTUP") -> dict:
    normalized_cf = "".join(char for char in str(cf).upper() if char.isalnum())
    if normalized_cf.startswith("IT"):
        normalized_cf = normalized_cf[2:]
    if len(normalized_cf) not in {11, 16}:
        raise ValueError("CF/P.IVA non valido per il fallback Apify.")

    return {
        "startUrls": [{
            "url": PORTAL_HOME,
            "userData": {"cf": normalized_cf, "tipo": tipo.upper()},
        }],
        "pageFunction": PAGE_FUNCTION,
        "proxyConfiguration": {"useApifyProxy": False},
        "maxPagesPerCrawl": 1,
        "maxConcurrency": 1,
        "maxRequestRetries": 0,
        "pageLoadTimeoutSecs": 45,
        "pageFunctionTimeoutSecs": 120,
    }


def _api_request(
    path: str,
    payload: dict | None = None,
    timeout: int = 150,
    method: str | None = None,
) -> dict:
    url = f"{APIFY_API_BASE}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {APIFY_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method or ("POST" if payload is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as error:
        raise ApifyFallbackError(f"api_http_{error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ApifyFallbackError(type(error).__name__.lower()) from None


def _run_apify_candidate(cf: str, tipo: str) -> dict:
    if not APIFY_API_KEY:
        raise ApifyFallbackError("api_key_missing")
    actor_id = urllib.parse.quote(APIFY_ACTOR_ID.replace("/", "~"), safe="~")
    run_response = _api_request(
        f"/acts/{actor_id}/runs?waitForFinish=120",
        build_apify_input(cf, tipo),
        timeout=150,
    )
    run_data = run_response.get("data", {})
    run_id = run_data.get("id")
    dataset_id = run_data.get("defaultDatasetId")
    if not run_id:
        raise ApifyFallbackError("run_id_missing")

    deadline = time.monotonic() + 180
    while run_data.get("status") in {"READY", "RUNNING"} and time.monotonic() < deadline:
        time.sleep(3)
        run_data = _api_request(f"/actor-runs/{run_id}", timeout=20).get("data", {})
        dataset_id = dataset_id or run_data.get("defaultDatasetId")

    if run_data.get("status") != "SUCCEEDED" or not dataset_id:
        raise ApifyFallbackError(f"run_{str(run_data.get('status', 'unknown')).lower()}")

    try:
        items = _api_request(
            f"/datasets/{dataset_id}/items?clean=true&format=json&limit=1",
            timeout=30,
        )
    finally:
        try:
            _api_request(
                f"/datasets/{dataset_id}",
                timeout=20,
                method="DELETE",
            )
        except ApifyFallbackError:
            pass
    if not isinstance(items, list) or not items:
        raise ApifyFallbackError("dataset_empty")
    result = items[0]
    if result.get("outcome") != "found":
        raise ApifyFallbackError(str(result.get("outcome") or "unknown"))
    if result.get("cf") != "".join(char for char in cf.upper() if char.isalnum()).removeprefix("IT"):
        raise ApifyFallbackError("cf_mismatch")
    if not result.get("detailUrl") or not result.get("html") or not result.get("xml"):
        raise ApifyFallbackError("detail_payload_incomplete")
    return result


async def run_apify_piva_candidate(cf: str, tipo: str = "STARTUP") -> dict:
    return await asyncio.to_thread(_run_apify_candidate, cf, tipo)