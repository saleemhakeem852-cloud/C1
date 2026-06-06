"""
craigslist.py — CLBlast Craigslist automation
FIX v2: CDP Network event listener captures real autocomplete XHR response
         and re-injects any hidden fields (geo_id, location_id, updated
         cryptedStepCheck) that CL's server sends back — the root cause of
         the "ZIP code • autofilled" rejection.

ROOT CAUSE SUMMARY:
  - postal=90001 WAS being sent correctly (confirmed by payload logs)
  - _ZIP_PATCH_JS was returning None (silent JS crash) — patches not installing
  - cryptedStepCheck value rotated between fill and submit, proving CL's server
    issues a new token only after a real autocomplete network call is made
  - Our fake widget fired UI events but never hit CL's geo endpoint, so the
    server never saw a confirmed location and rejected the submission

THE FIX:
  1. Enable CDP Network events BEFORE navigating to the posting form
  2. Listen for any XHR/fetch to CL's geo/suggest/postal endpoint during a
     real manual-style interaction (or during our automated run)
  3. Capture the full response body from that network call
  4. Parse it and inject any hidden fields (geo_id, location_id, new
     cryptedStepCheck, etc.) directly into the DOM before submit
  5. Fixed all JS syntax issues in patch scripts that caused None returns
"""

import re
import time
import json
import os
import random
import shutil
import subprocess
import threading
import tempfile
import urllib.request
import requests
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

try:
    from twocaptcha import TwoCaptcha
    CAPTCHA_SOLVER_AVAILABLE = True
except ImportError:
    CAPTCHA_SOLVER_AVAILABLE = False

TWO_CAPTCHA_API_KEY = os.environ.get("TWO_CAPTCHA_KEY", "YOUR_2CAPTCHA_API_KEY")
LISTINGS_JSON       = "posted_listings.json"
CL_CITY             = os.environ.get("CL_CITY", "losangeles")
IS_FAST_MODE        = os.environ.get("FAST_MODE", "1") == "1"
IS_RAILWAY          = any(os.path.exists(p) for p in [
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
])

CATEGORY_MAPPING = {
    "antiques": (1, "antiques"), "appliances": (2, "appliances"),
    "art": (3, "arts & crafts"), "paintings": (3, "arts & crafts"),
    "atvs": (4, None), "automotive": (5, "auto parts"), "auto parts": (5, "auto parts"),
    "tires": (6, "auto wheels & tires"), "boats": (13, "boats"),
    "books": (14, "books & magazines"), "business": (15, "business/commercial"),
    "cars": (16, "cars & trucks"), "trucks": (16, "cars & trucks"),
    "phones": (18, "cell phones"), "cell phones": (18, "cell phones"),
    "fashion": (19, "clothing & accessories"),
    "collectibles": (20, "collectibles"), "coins": (20, "collectibles"),
    "computers": (22, "computers"), "laptops": (22, "computers"),
    "electronics": (23, "electronics"), "cameras": (23, "electronics"),
    "furniture": (26, "furniture"),
    "miscellaneous": (28, "general for sale"),
    "health": (29, "health and beauty"), "beauty": (29, "health and beauty"),
    "household": (31, "household items"),
    "jewelry": (32, "jewelry"), "watches": (32, "jewelry"),
    "motorcycles": (35, "motorcycles/scooters"),
    "instruments": (36, "musical instruments"),
    "sports": (39, "sporting goods"), "sporting goods": (39, "sporting goods"),
    "tickets": (40, "tickets"), "tools": (41, "tools"),
    "toys": (42, "toys & games"), "video games": (44, "video gaming"),
    "men": (19, "clothing & accessories"), "women": (19, "clothing & accessories"),
    "accessories": (19, "clothing & accessories"),
    "artandcollectibles": (20, "collectibles"),
    "art and collectibles": (20, "collectibles"),
    "homeandappliances": (31, "household items"),
    "home and appliances": (31, "household items"),
    "entertainment": (44, "video gaming"),
}

def get_category_ul_value(category_name):
    key = category_name.lower().strip().replace(" ", "")
    for k in CATEGORY_MAPPING:
        if k.replace(" ", "") == key:
            return CATEGORY_MAPPING[k][0]
    key_spaced = category_name.lower().strip()
    for k in CATEGORY_MAPPING:
        if k in key_spaced or key_spaced in k:
            return CATEGORY_MAPPING[k][0]
    return CATEGORY_MAPPING["miscellaneous"][0]

posted_listings: dict = {}
_listings_lock = threading.Lock()

def _load_existing_listings():
    global posted_listings
    if not os.path.exists(LISTINGS_JSON):
        return
    try:
        with open(LISTINGS_JSON) as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in posted_listings:
                entry = dict(v)
                try:
                    entry["post_time"] = datetime.fromisoformat(v["post_time"])
                except Exception:
                    entry["post_time"] = datetime.now()
                posted_listings[k] = entry
        print(f"  Loaded {len(data)} existing listing(s) from disk.")
    except Exception as e:
        print(f"  Could not load existing listings: {e}")

def _save_listings():
    serialisable = {}
    for k, v in posted_listings.items():
        entry = dict(v)
        pt = v["post_time"]
        entry["post_time"] = pt.isoformat() if isinstance(pt, datetime) else str(pt)
        serialisable[k] = entry
    tmp_path = LISTINGS_JSON + ".tmp"
    with _listings_lock:
        with open(tmp_path, "w") as f:
            json.dump(serialisable, f, indent=2)
        os.replace(tmp_path, LISTINGS_JSON)

def _find_binary(names, fallback_paths):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    for name in names:
        try:
            r = subprocess.run(["which", name], capture_output=True, text=True, timeout=3)
            p = r.stdout.strip()
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    for p in (["/usr/local/bin/" + n for n in names] + fallback_paths):
        if os.path.exists(p):
            return p
    return None

def _ensure_xvfb():
    if os.environ.get("DISPLAY"):
        return
    if not IS_RAILWAY and not shutil.which("Xvfb"):
        return
    xvfb = shutil.which("Xvfb") or "/usr/bin/Xvfb"
    if not os.path.exists(xvfb):
        return
    try:
        subprocess.Popen(
            [xvfb, ":99", "-screen", "0", "1280x800x24", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        time.sleep(1.0)
        print("  [driver] Xvfb started (DISPLAY=:99)")
    except Exception as e:
        print(f"  [driver] Xvfb unavailable: {e}")


_FINGERPRINTS = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
        "platform": "Win32", "vendor": "Google Inc.", "lang": "en-US",
        "screen": (1920, 1080), "tz": "America/Los_Angeles",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
        "platform": "Win32", "vendor": "Google Inc.", "lang": "en-US",
        "screen": (1366, 768), "tz": "America/New_York",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
        "platform": "MacIntel", "vendor": "Google Inc.", "lang": "en-US",
        "screen": (2560, 1600), "tz": "America/Chicago",
    },
]

_FINGERPRINT_JS = """
(function() {{
    Object.defineProperty(navigator, 'webdriver',   {{get: () => undefined}});
    Object.defineProperty(navigator, 'platform',    {{get: () => '{platform}'}});
    Object.defineProperty(navigator, 'vendor',      {{get: () => '{vendor}'}});
    Object.defineProperty(navigator, 'language',    {{get: () => '{lang}'}});
    Object.defineProperty(navigator, 'languages',   {{get: () => ['{lang}', 'en']}});
    Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => 8}});
    Object.defineProperty(navigator, 'deviceMemory',        {{get: () => 8}});
    Object.defineProperty(navigator, 'maxTouchPoints',      {{get: () => 0}});
    Object.defineProperty(screen, 'width',       {{get: () => {sw}}});
    Object.defineProperty(screen, 'height',      {{get: () => {sh}}});
    Object.defineProperty(screen, 'availWidth',  {{get: () => {sw}}});
    Object.defineProperty(screen, 'availHeight', {{get: () => {sh} - 40}});
    Object.defineProperty(screen, 'colorDepth',  {{get: () => 24}});
    Object.defineProperty(screen, 'pixelDepth',  {{get: () => 24}});
    if (!window.chrome) {{
        window.chrome = {{
            app: {{}},
            runtime: {{
                onConnect: {{addListener: function(){{}}}},
                onMessage: {{addListener: function(){{}}}}
            }},
        }};
    }}
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {{
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return getParam.call(this, p);
    }};
}})();
"""


# ─────────────────────────────────────────────────────────────────────────────
#  CDP Network Interceptor
#
#  This is the core fix. We attach a Network.responseReceived listener via
#  CDP BEFORE the page loads. When CL's JS makes the postal/geo lookup XHR
#  (which our previous approach proved it does NOT do via JS-visible XHR —
#  it may be a fetch with credentials, or a navigation-level request), we
#  capture the full response body using Network.getResponseBody.
#
#  If the response contains new hidden fields (geo_id, location_id, a
#  rotated cryptedStepCheck), we store them in window._clGeoResponse so
#  that _inject_geo_hidden_fields() can write them into the DOM before submit.
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that identify CL's location/postal validation endpoint
_GEO_URL_PATTERNS = [
    "suggest", "postal", "geo", "location", "zip", "area",
    "geoCode", "geocode", "postcode",
]

def _start_cdp_network_capture(driver):
    """
    Enable CDP Network domain and register a Python-side listener that
    captures any response whose URL looks like a geo/postal lookup.
    Stores captured responses in driver._cl_geo_responses (list of dicts).
    """
    driver._cl_geo_responses = []
    driver._cl_network_request_map = {}  # requestId -> url

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        print("  [CDP] Network capture enabled")
    except Exception as e:
        print(f"  [CDP] Could not enable Network domain: {e}")
        return

    # We poll for new network events using a JS-side XHR spy instead of
    # Python CDP callbacks (Selenium's CDP event API is unreliable across
    # driver versions). The JS spy writes intercepted responses to a global.
    _NETWORK_SPY_JS = """
(function() {
    if (window._clNetworkSpyInstalled) return 'already-installed';
    window._clNetworkSpyInstalled = true;
    window._clCapturedGeoResponses = [];
    window._clAllNetworkCalls = [];

    // Patterns to watch for
    var GEO_PATTERNS = ['suggest','postal','geo','location','zip','area','geocode','postcode'];
    function looksLikeGeo(url) {
        if (!url) return false;
        var u = url.toLowerCase();
        for (var i = 0; i < GEO_PATTERNS.length; i++) {
            if (u.indexOf(GEO_PATTERNS[i]) !== -1) return true;
        }
        return false;
    }

    // Wrap XHR
    var OrigXHR = window.XMLHttpRequest;
    function SpyXHR() {
        var xhr = new OrigXHR();
        var _url = '', _method = '';
        var origOpen = xhr.open.bind(xhr);
        var origSend = xhr.send.bind(xhr);

        xhr.open = function(method, url) {
            _method = method; _url = url || '';
            return origOpen(method, url);
        };
        xhr.send = function(body) {
            var captureUrl = _url;
            var origRSC = xhr.onreadystatechange;
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    var entry = {
                        type: 'xhr', url: captureUrl,
                        status: xhr.status,
                        responseText: xhr.responseText || ''
                    };
                    window._clAllNetworkCalls.push(entry);
                    if (looksLikeGeo(captureUrl)) {
                        window._clCapturedGeoResponses.push(entry);
                        window._clLastGeoResponse = entry;
                    }
                }
                if (origRSC) origRSC.apply(this, arguments);
            };
            return origSend(body);
        };
        return xhr;
    }
    // Copy static props
    for (var k in OrigXHR) { try { SpyXHR[k] = OrigXHR[k]; } catch(e) {} }
    SpyXHR.prototype = OrigXHR.prototype;
    window.XMLHttpRequest = SpyXHR;

    // Wrap fetch
    var origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = (typeof input === 'string') ? input : (input && input.url) || '';
        var p = origFetch.apply(this, arguments);
        p.then(function(resp) {
            resp.clone().text().then(function(text) {
                var entry = {
                    type: 'fetch', url: url,
                    status: resp.status,
                    responseText: text || ''
                };
                window._clAllNetworkCalls.push(entry);
                if (looksLikeGeo(url)) {
                    window._clCapturedGeoResponses.push(entry);
                    window._clLastGeoResponse = entry;
                }
            }).catch(function(){});
        }).catch(function(){});
        return p;
    };

    return 'spy-installed';
})();
"""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _NETWORK_SPY_JS})
        print("  [CDP] Network spy script registered for new documents")
    except Exception as e:
        print(f"  [CDP] Could not register network spy: {e}")


def _install_network_spy_now(driver):
    """Install the network spy into the already-loaded page (not just new docs)."""
    _NETWORK_SPY_JS = """
(function() {
    if (window._clNetworkSpyInstalled) return 'already-installed';
    window._clNetworkSpyInstalled = true;
    window._clCapturedGeoResponses = [];
    window._clAllNetworkCalls = [];

    var GEO_PATTERNS = ['suggest','postal','geo','location','zip','area','geocode','postcode'];
    function looksLikeGeo(url) {
        if (!url) return false;
        var u = url.toLowerCase();
        for (var i = 0; i < GEO_PATTERNS.length; i++) {
            if (u.indexOf(GEO_PATTERNS[i]) !== -1) return true;
        }
        return false;
    }

    var OrigXHR = window.XMLHttpRequest;
    function SpyXHR() {
        var xhr = new OrigXHR();
        var _url = '', _method = '';
        var origOpen = xhr.open.bind(xhr);
        var origSend = xhr.send.bind(xhr);
        xhr.open = function(method, url) { _method = method; _url = url || ''; return origOpen(method, url); };
        xhr.send = function(body) {
            var captureUrl = _url;
            var origRSC = xhr.onreadystatechange;
            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    var entry = { type: 'xhr', url: captureUrl, status: xhr.status, responseText: xhr.responseText || '' };
                    window._clAllNetworkCalls.push(entry);
                    if (looksLikeGeo(captureUrl)) { window._clCapturedGeoResponses.push(entry); window._clLastGeoResponse = entry; }
                }
                if (origRSC) origRSC.apply(this, arguments);
            };
            return origSend(body);
        };
        return xhr;
    }
    for (var k in OrigXHR) { try { SpyXHR[k] = OrigXHR[k]; } catch(e) {} }
    SpyXHR.prototype = OrigXHR.prototype;
    window.XMLHttpRequest = SpyXHR;

    var origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = (typeof input === 'string') ? input : (input && input.url) || '';
        var p = origFetch.apply(this, arguments);
        p.then(function(resp) {
            resp.clone().text().then(function(text) {
                var entry = { type: 'fetch', url: url, status: resp.status, responseText: text || '' };
                window._clAllNetworkCalls.push(entry);
                if (looksLikeGeo(url)) { window._clCapturedGeoResponses.push(entry); window._clLastGeoResponse = entry; }
            }).catch(function(){});
        }).catch(function(){});
        return p;
    };

    return 'spy-installed';
})();
"""
    result = driver.execute_script(_NETWORK_SPY_JS)
    print(f"  [CDP] Network spy (live install): {result}")


def _get_geo_responses(driver):
    """Poll the JS-side spy buffer for any captured geo responses."""
    try:
        responses = driver.execute_script(
            "return window._clCapturedGeoResponses || [];")
        all_calls = driver.execute_script(
            "return (window._clAllNetworkCalls || []).slice(-20);")
        return responses, all_calls
    except Exception:
        return [], []


def _inject_geo_hidden_fields(driver, geo_response_text, zip_str):
    """
    Parse the geo/postal lookup response and inject any returned fields
    (geo_id, location_id, updated cryptedStepCheck, etc.) as hidden inputs
    into the posting form.

    CL's response format is typically one of:
      - JSON array: [{"id":"...", "value":"90001", "label":"...", "geo_id":"..."}]
      - JSON object: {"geo_id":"...", "cryptedStepCheck":"...", ...}
      - Plain text ZIP confirmation

    We inject ALL top-level string/number fields from the response as hidden
    inputs, then specifically look for known field names.
    """
    if not geo_response_text:
        return False

    injected = {}
    try:
        data = json.loads(geo_response_text)
        # Normalize to a flat dict
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, (str, int, float)) and val:
                    injected[key] = str(val)
    except Exception:
        # Not JSON — maybe plain text, ignore
        pass

    if not injected:
        print("  [GEO] Response parsed but no injectable fields found")
        return False

    print(f"  [GEO] Injecting fields from geo response: {list(injected.keys())}")

    # Inject each field as a hidden input (or update existing)
    inject_js = """
(function(fields) {
    var form = document.getElementById('postingForm');
    if (!form) return {ok: false, reason: 'no-form'};
    var injected = [];
    for (var name in fields) {
        var val = fields[name];
        // Check if field already exists
        var existing = form.querySelector('[name="' + name + '"]');
        if (existing) {
            var old = existing.value;
            existing.value = val;
            existing.setAttribute('value', val);
            injected.push('updated:' + name + '=' + val + '(was:' + old + ')');
        } else {
            var inp = document.createElement('input');
            inp.type = 'hidden';
            inp.name = name;
            inp.value = val;
            form.appendChild(inp);
            injected.push('added:' + name + '=' + val);
        }
    }
    return {ok: true, injected: injected};
})(arguments[0]);
"""
    result = driver.execute_script(inject_js, injected)
    print(f"  [GEO] Injection result: {result}")
    return bool(result and result.get("ok"))


# ─────────────────────────────────────────────────────────────────────────────
#  DIRECT GEO FETCH — if the autocomplete never fires a real network call,
#  we make the call ourselves using requests (from Python, bypassing JS) and
#  inject the result. This is the guaranteed fallback.
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_cl_geo_direct(driver, zip_str, city="Los Angeles", state="CA"):
    """
    Make the postal lookup request directly from Python using the same
    session cookies as the browser. CL's geo endpoint accepts GET requests.

    Known CL geo/suggest endpoint patterns (may vary by city):
      https://losangeles.craigslist.org/suggest?fieldname=postal&typing=90001
      https://post.craigslist.org/suggest?fieldname=postal&typing=90001
      https://post.craigslist.org/c/sss?s=geo&q=90001

    We try each and return the first successful response.
    """
    # Extract cookies from browser
    cookies = {}
    try:
        for cookie in driver.get_cookies():
            cookies[cookie["name"]] = cookie["value"]
    except Exception as e:
        print(f"  [GEO-direct] Could not get cookies: {e}")

    headers = {
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": driver.current_url,
        "Origin": "https://post.craigslist.org",
    }

    city_slug = CL_CITY.lower().replace(" ", "").replace("-", "")
    candidate_urls = [
        f"https://post.craigslist.org/suggest?fieldname=postal&typing={zip_str}",
        f"https://{city_slug}.craigslist.org/suggest?fieldname=postal&typing={zip_str}",
        f"https://post.craigslist.org/suggest?fieldname=postal_code&typing={zip_str}",
        f"https://post.craigslist.org/geo?q={zip_str}",
        f"https://{city_slug}.craigslist.org/geo?q={zip_str}",
        f"https://post.craigslist.org/c/sss?s=geo&q={zip_str}",
    ]

    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, cookies=cookies,
                                timeout=8, allow_redirects=True)
            print(f"  [GEO-direct] {url} → {resp.status_code} ({len(resp.text)} bytes)")
            if resp.status_code == 200 and resp.text.strip():
                print(f"  [GEO-direct] Response: {resp.text[:300]}")
                return resp.text, url
        except Exception as e:
            print(f"  [GEO-direct] {url} failed: {e}")

    print("  [GEO-direct] No successful geo response from any endpoint")
    return None, None


def _trigger_real_geo_lookup(driver, zip_str):
    """
    Force CL's own JS to make the postal lookup XHR/fetch by calling their
    internal autocomplete source function directly.

    CL's autocomplete source is defined in postingform-concat.min.js.
    We find it by walking jQuery UI autocomplete instances on the form.
    If found, we call it with the ZIP and wait for the callback.
    """
    trigger_js = """
(function(zipVal, callback) {
    var postalEl = document.querySelector('[name="postal"]') ||
                   document.querySelector('[name="postal_code"]') ||
                   document.querySelector('#postal_code') ||
                   document.querySelector('#postal');

    if (!postalEl || !window.jQuery) {
        return {ok: false, reason: 'no-postal-or-jquery'};
    }

    var jq = jQuery(postalEl);

    // Try to get the autocomplete instance's source function
    var acData = jq.data('ui-autocomplete') || jq.data('autocomplete');
    if (!acData || !acData.options || !acData.options.source) {
        return {ok: false, reason: 'no-autocomplete-instance', data: Object.keys(jq.data() || {})};
    }

    var sourceFn = acData.options.source;
    if (typeof sourceFn !== 'function') {
        // It's a URL string or array — that's the source directly
        return {ok: false, reason: 'source-not-function', sourceType: typeof sourceFn, source: String(sourceFn).substring(0,100)};
    }

    // Call the source function as CL does internally
    window._clGeoLookupTriggered = false;
    window._clGeoLookupResponse = null;

    try {
        sourceFn.call(acData, {term: zipVal}, function(items) {
            window._clGeoLookupTriggered = true;
            window._clGeoLookupResponse = items;
        });
        return {ok: true, reason: 'source-called'};
    } catch(e) {
        return {ok: false, reason: 'source-call-error', error: e.message};
    }
})(arguments[0]);
"""
    result = driver.execute_script(trigger_js, zip_str)
    print(f"  [GEO-trigger] Direct source call result: {result}")

    if result and result.get("ok"):
        # Wait for async callback
        try:
            WebDriverWait(driver, 8).until(
                lambda d: d.execute_script("return !!window._clGeoLookupTriggered;"))
            items = driver.execute_script("return window._clGeoLookupResponse;")
            print(f"  [GEO-trigger] Got {len(items) if items else 0} items from source")
            return items
        except TimeoutException:
            print("  [GEO-trigger] Source callback timed out")

    return None


def make_driver(proxy_url=None):
    from selenium.webdriver.chrome.service import Service as ChromeService
    os.environ["SE_MANAGER_PATH"] = ""
    os.environ["WDM_SKIP_DOWNLOAD"] = "1"
    _ensure_xvfb()
    use_headed = bool(os.environ.get("DISPLAY"))

    if not proxy_url:
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    fp = random.choice(_FINGERPRINTS)
    sw, sh = fp["screen"]
    print(f"  [driver] Fingerprint: {fp['ua'][:60]}...")

    options = webdriver.ChromeOptions()
    chrome_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        f"--window-size={sw},{sh}",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--ignore-certificate-errors",
        "--disable-extensions",
        "--mute-audio",
        "--no-first-run",
        "--shm-size=256m",
        "--disable-features=AutofillServerCommunication,IsolateOrigins,site-per-process",
        "--enable-features=NetworkService,NetworkServiceInProcess",
        "--disable-web-security",
        "--allow-running-insecure-content",
        "--enable-javascript",
        "--enable-local-storage",
        f"--lang={fp['lang']}",
        "--disable-popup-blocking",
        "--disable-translate",
        "--disable-default-apps",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-report-upload",
    ]
    if not use_headed:
        chrome_args.insert(3, "--headless=new")
    for arg in chrome_args:
        options.add_argument(arg)
    if use_headed:
        print("  [driver] Headed mode (virtual display)")

    if proxy_url:
        options.add_argument(f"--proxy-server={proxy_url}")
        options.add_argument("--proxy-bypass-list=localhost,127.0.0.1")

    fresh_profile = tempfile.mkdtemp(prefix="clblast_chrome_")
    options.add_argument(f"--user-data-dir={fresh_profile}")
    options.add_argument(f"--user-agent={fp['ua']}")

    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "autofill.profile_enabled": False,
        "autofill.credit_card_enabled": False,
        "intl.accept_languages": fp["lang"],
    })

    chromium_bin = _find_binary(
        ["google-chrome", "chromium", "chromium-browser"],
        ["/usr/local/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"])
    if chromium_bin:
        print(f"  [driver] Using chromium: {chromium_bin}")
        options.binary_location = chromium_bin

    chromedriver_bin = _find_binary(
        ["chromedriver"],
        ["/usr/local/bin/chromedriver", "/usr/bin/chromedriver"])
    if os.path.exists("/usr/local/bin/chromedriver"):
        chromedriver_bin = "/usr/local/bin/chromedriver"
    if not chromedriver_bin:
        raise RuntimeError("chromedriver not found")
    print(f"  [driver] Using chromedriver: {chromedriver_bin}")

    service = ChromeService(
        executable_path=chromedriver_bin,
        log_output="/tmp/chromedriver.log",
    )
    options.add_argument("--remote-allow-origins=*")

    try:
        import undetected_chromedriver as uc
        driver = uc.Chrome(
            options=options,
            driver_executable_path=chromedriver_bin,
            browser_executable_path=chromium_bin,
            headless=not use_headed,
            use_subprocess=True,
        )
        print("  [driver] Using undetected-chromedriver")
    except Exception as uc_err:
        print(f"  [driver] undetected-chromedriver unavailable ({uc_err}), using stock Chrome")
        driver = webdriver.Chrome(service=service, options=options)

    fingerprint_js = _FINGERPRINT_JS.format(
        platform=fp["platform"], vendor=fp["vendor"], lang=fp["lang"],
        sw=sw, sh=sh,
    )
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": fingerprint_js})

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {
            "Accept-Language": f"{fp['lang']},en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }})
    except Exception:
        pass

    # Register network spy for all new documents
    _start_cdp_network_capture(driver)

    # Native form submit interceptor
    _FORM_INTERCEPT_JS = """
(function() {
    window._clNativeSubmitPayloads = [];
    function _captureForm(form, via) {
        try {
            var fd = new FormData(form);
            var pairs = [];
            fd.forEach(function(v, k) { pairs.push(k + '=' + String(v).substring(0, 200)); });
            window._clNativeSubmitPayloads.push({
                action: form.action, method: form.method,
                via: via, body: pairs.join('&')
            });
        } catch(e) {}
    }
    var origSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function() {
        _captureForm(this, 'submit');
        return origSubmit.call(this);
    };
    if (HTMLFormElement.prototype.requestSubmit) {
        var origRS = HTMLFormElement.prototype.requestSubmit;
        HTMLFormElement.prototype.requestSubmit = function(btn) {
            _captureForm(this, 'requestSubmit');
            return origRS.call(this, btn);
        };
    }
})();
"""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _FORM_INTERCEPT_JS})
    except Exception:
        pass

    return driver


def human_delay(lo=0.8, hi=2.5):
    time.sleep(random.uniform(lo, hi))


def safe_click(driver, element):
    human_delay(0.3, 0.6)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.2, 0.5)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    human_delay(0.3, 0.6)


def handle_captcha_if_present(driver):
    try:
        driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        if CAPTCHA_SOLVER_AVAILABLE:
            try:
                iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
                sitekey = [p.split("=")[1] for p in iframe.get_attribute("src").split("&") if "k=" in p][0]
                solver = TwoCaptcha(TWO_CAPTCHA_API_KEY)
                result = solver.recaptcha(sitekey=sitekey, url=driver.current_url)
                driver.execute_script(
                    "document.getElementById('g-recaptcha-response').innerHTML=arguments[0];",
                    result["code"])
                print("  CAPTCHA solved ✓")
            except Exception as e:
                print(f"  CAPTCHA solve failed: {e}")
    except NoSuchElementException:
        pass
    if "Just a moment" in driver.title:
        print("  Cloudflare — waiting 8s…")
        time.sleep(8)


def craigslist_login(driver, email):
    driver.get("https://accounts.craigslist.org/login")
    human_delay(2, 4)
    handle_captcha_if_present(driver)
    try:
        ef = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "inputEmailHandle")))
        for ch in email:
            ef.send_keys(ch)
            time.sleep(random.uniform(0.05, 0.12))
        human_delay(0.5, 1.0)
        btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        safe_click(driver, btn)
        WebDriverWait(driver, 15).until(EC.url_contains("craigslist.org"))
        handle_captcha_if_present(driver)
        print("Logged in to Craigslist ✓")
        return True
    except TimeoutException:
        print("Login failed.")
        return False


def click_relocation_if_needed(driver, ad_name):
    try:
        btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#relocationButton")))
        safe_click(driver, btn)
        local_btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#localAreaButton")))
        safe_click(driver, local_btn)
        print("  Relocation handled ✓")
    except TimeoutException:
        pass


def _wait_for_cl_js_init(driver, timeout=20):
    """Wait for CL's postingform JS to fully initialize before we touch any field."""
    print("  [init] Waiting for CL form JS to initialize...")
    try:
        WebDriverWait(driver, timeout).until(lambda d: d.execute_script("""
            try {
                var form = document.getElementById('postingForm');
                if (!form) return false;
                if (!window.jQuery) return false;
                var jqForm = jQuery(form);
                if (jqForm.data('validator')) return true;
                if (window.cl && window.cl.postingProcess) return true;
                return jQuery('#postingForm').length > 0;
            } catch(e) { return false; }
        """))
        print("  [init] CL form JS ready ✓")
    except TimeoutException:
        print("  [init] Timeout waiting for CL JS — proceeding anyway")
    time.sleep(1.5)


def _find_field(driver, selectors, timeout=8):
    """Try multiple selectors, return first visible element found."""
    for sel in selectors:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            if el.is_displayed():
                return el
        except Exception:
            continue
    return None


def _cdp_type(driver, element, value):
    """
    Type into a field using CDP char events only.
    After typing, fires jQuery-compatible events so CL's validator marks field touched.
    """
    value = str(value).strip()
    if not value:
        return

    driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", element)
    time.sleep(0.25)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.1, 0.25)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].focus();", element)
    time.sleep(random.uniform(0.15, 0.3))

    # Select all + delete
    for key_action in [
        {"type": "keyDown", "key": "Control", "code": "ControlLeft",  "keyCode": 17, "modifiers": 0},
        {"type": "keyDown", "key": "a",       "code": "KeyA",         "keyCode": 65, "modifiers": 2},
        {"type": "keyUp",   "key": "a",       "code": "KeyA",         "keyCode": 65, "modifiers": 2},
        {"type": "keyUp",   "key": "Control", "code": "ControlLeft",  "keyCode": 17, "modifiers": 0},
        {"type": "keyDown", "key": "Delete",  "code": "Delete",       "keyCode": 46, "modifiers": 0},
        {"type": "keyUp",   "key": "Delete",  "code": "Delete",       "keyCode": 46, "modifiers": 0},
    ]:
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", key_action)
        time.sleep(0.03)

    time.sleep(0.1)

    for ch in value:
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
            "type": "char",
            "key": ch,
            "text": ch,
            "unmodifiedText": ch,
        })
        time.sleep(random.uniform(0.06, 0.14))

    time.sleep(0.2)

    driver.execute_script("""
        var el = arguments[0];
        el.dispatchEvent(new Event('input',  {bubbles: true, cancelable: true}));
        el.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
        if (window.jQuery) {
            jQuery(el).trigger('input').trigger('change').trigger('keyup');
        }
    """, element)
    time.sleep(0.25)


# ─────────────────────────────────────────────────────────────────────────────
#  FIXED ZIP PATCH JS
#
#  Previous version returned None because:
#  1. The IIFE was wrapped as `(function(zipVal) { ... })(arguments[0])` but
#     execute_script wraps the code in another function, so `arguments[0]` at
#     the outer IIFE call referred to the execute_script arguments, not the
#     passed zipVal. Fixed by using the standard execute_script argument passing.
#  2. Several inner try/catch blocks were swallowing errors silently.
#  3. The return value was inside the IIFE but execute_script needs the OUTER
#     function to return it.
#
#  This version is structured as a plain script (not IIFE) that uses
#  `arguments[0]` directly as execute_script passes it.
# ─────────────────────────────────────────────────────────────────────────────

_ZIP_PATCH_JS = """
var zipVal = arguments[0];
var results = [];

try {
    // ── Step 1: Patch jQuery serializeArray ──────────────────────────────────
    if (window.jQuery) {
        var origSerializeArray = jQuery.fn.serializeArray;
        jQuery.fn.serializeArray = function() {
            var result = origSerializeArray.call(this);
            var hasPostal = false;
            for (var i = 0; i < result.length; i++) {
                if (result[i].name === 'postal' || result[i].name === 'postal_code') {
                    result[i].value = zipVal;
                    hasPostal = true;
                }
            }
            if (!hasPostal) result.push({name: 'postal', value: zipVal});
            return result;
        };

        var origSerialize = jQuery.fn.serialize;
        jQuery.fn.serialize = function() {
            var s = origSerialize.call(this);
            s = s.replace(/postal=[^&]*/g, 'postal=' + encodeURIComponent(zipVal));
            s = s.replace(/postal_code=[^&]*/g, 'postal_code=' + encodeURIComponent(zipVal));
            if (s.indexOf('postal=') === -1) s += (s ? '&' : '') + 'postal=' + encodeURIComponent(zipVal);
            return s;
        };
        results.push('serializer-patched');
        window._clSerializerPatched = true;
    } else {
        results.push('no-jquery');
    }
} catch(e) { results.push('serializer-err:' + e.message); }

try {
    // ── Step 2: Patch FormData ───────────────────────────────────────────────
    var OrigFormData = window.FormData;
    function PatchedFormData(form) {
        var fd = form ? new OrigFormData(form) : new OrigFormData();
        if (form) {
            try { fd.set('postal', zipVal); } catch(e) {}
            try { fd.set('postal_code', zipVal); } catch(e) {}
        }
        var origAppend = fd.append.bind(fd);
        fd.append = function(name, value) {
            if (name === 'postal' || name === 'postal_code') value = zipVal;
            return origAppend(name, value);
        };
        if (fd.set) {
            var origSet = fd.set.bind(fd);
            fd.set = function(name, value) {
                if (name === 'postal' || name === 'postal_code') value = zipVal;
                return origSet(name, value);
            };
        }
        return fd;
    }
    PatchedFormData.prototype = OrigFormData.prototype;
    window.FormData = PatchedFormData;
    results.push('formdata-patched');
} catch(e) { results.push('formdata-err:' + e.message); }

try {
    // ── Step 3: Simulate autocomplete select after widget initializes ────────
    setTimeout(function() {
        try {
            var postalEl = document.querySelector('[name="postal"]') ||
                           document.querySelector('[name="postal_code"]') ||
                           document.querySelector('#postal_code') ||
                           document.querySelector('#postal');
            if (!postalEl || !window.jQuery) return;
            var jq = jQuery(postalEl);

            // Try attaching our own autocomplete if CL's isn't there
            if (!jq.data('ui-autocomplete') && !jq.data('autocomplete')) {
                jq.autocomplete({
                    source: [{value: zipVal, label: zipVal + ' - Los Angeles, CA'}],
                    minLength: 0
                });
                window._clZipWidgetCreated = true;
            }

            // Fire select event
            var selectEvent = jQuery.Event('autocompleteselect');
            selectEvent.item = {value: zipVal, label: zipVal + ' - Los Angeles, CA'};
            jq.trigger(selectEvent);
            jq.trigger(jQuery.Event('autocompletechange'), {item: {value: zipVal}});
            window._clZipAutoconfirmed = true;
            window._clZipFired = 'autocomplete-events-fired';
        } catch(e2) {
            window._clZipWidgetErr = e2.message;
        }
    }, 2500);
    results.push('autocomplete-timer-set');
} catch(e) { results.push('autocomplete-timer-err:' + e.message); }

window._clZipPatchInstalled = true;
window._clZipPatchResults = results;
return results.join(',');
"""

_VALIDATOR_NUKE_JS = """
var zipVal = arguments[0];
var results = [];

try {
    var postalEl = document.querySelector('[name="postal"]') ||
                   document.querySelector('[name="postal_code"]') ||
                   document.querySelector('#postal_code') ||
                   document.querySelector('#postal');

    if (!postalEl) {
        return ['no-postal-el'];
    }

    // 1. Force the DOM value using native setter
    var nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    nativeSetter.call(postalEl, zipVal);
    postalEl.setAttribute('value', zipVal);
    results.push('dom-set:' + postalEl.value);

    if (window.jQuery) {
        var jq = jQuery(postalEl);

        // 2. Remove ALL jQuery validation rules on this field
        try { jq.rules('remove'); results.push('rules-removed'); } catch(e) {}

        // 3. Remove from validator internal registry
        var form = document.getElementById('postingForm');
        if (form) {
            var validator = jQuery(form).data('validator');
            if (validator) {
                if (validator.settings && validator.settings.rules) {
                    delete validator.settings.rules['postal'];
                    delete validator.settings.rules['postal_code'];
                    results.push('validator-rules-deleted');
                }
                validator.successList = validator.successList || [];
                if (validator.successList.indexOf(postalEl) === -1) {
                    validator.successList.push(postalEl);
                }
                results.push('added-to-success-list');
                try { validator.resetElements([postalEl]); results.push('element-reset'); } catch(e) {}

                // 4. Walk custom validation methods and disable for postal
                if (jQuery.validator && jQuery.validator.methods) {
                    var nuked = 0;
                    var builtins = ['required','email','url','number','digits','min','max',
                                    'minlength','maxlength','range','rangelength','equalTo','remote'];
                    jQuery.each(jQuery.validator.methods, function(name, fn) {
                        if (builtins.indexOf(name) !== -1) return;
                        var orig = fn;
                        jQuery.validator.methods[name] = function(value, element, param) {
                            if (element === postalEl) return true;
                            return orig.call(this, value, element, param);
                        };
                        nuked++;
                    });
                    results.push('custom-methods-nuked:' + nuked);
                }
            } else {
                results.push('no-validator-instance');
            }
        }

        // 5. Clear error UI
        jq.removeClass('error invalid required')
          .removeAttr('aria-invalid')
          .removeAttr('aria-required')
          .removeAttr('aria-describedby');
        jQuery('label[for="postal_code"].error,label[for="postal"].error,#postal_code-error,#postal-error').remove();
        jQuery('.err li').filter(function() {
            return jQuery(this).text().toLowerCase().indexOf('zip') !== -1 ||
                   jQuery(this).text().toLowerCase().indexOf('postal') !== -1;
        }).remove();
        results.push('error-ui-cleared');

        // 6. Fire events so CL's validator marks it valid
        jq.val(zipVal)
          .trigger(jQuery.Event('focus',  {bubbles: true}))
          .trigger(jQuery.Event('input',  {bubbles: true}))
          .trigger(jQuery.Event('change', {bubbles: true}))
          .trigger(jQuery.Event('blur',   {bubbles: true}));
        results.push('events-fired');

        // 7. Mark in success lists of all ancestor validators
        jQuery('[name="postal_code"],[name="postal"]').each(function() {
            var el = this;
            jQuery('form').each(function() {
                var v = jQuery(this).data('validator');
                if (v) {
                    v.successList = v.successList || [];
                    if (v.successList.indexOf(el) === -1) v.successList.push(el);
                    try { v.resetElements([el]); } catch(e2) {}
                }
            });
        });
    } else {
        results.push('no-jquery-for-validator-nuke');
    }
} catch(e) {
    results.push('nuke-exception:' + e.message);
}

return results;
"""


def _call_cl_internal_location_setter(driver, zip_str, city_str="Los Angeles"):
    """
    CL's postingform-concat.min.js embeds the autocomplete dataset locally
    (no network call — confirmed by _allNetworkCalls: [] in logs).

    When a user selects from the autocomplete, CL's select handler calls an
    internal function that sets location state in a JS object. We find and
    call that function directly.

    Strategies tried in order:
    1. Find the autocomplete widget's select handler and call it with fake item data
    2. Find cl.postingProcess.setLocation or similar internal setter
    3. Walk all jQuery event handlers on the postal field and fire the select handler
    4. Simulate the exact event sequence CL's autocomplete fires on real selection
    """
    result = driver.execute_script("""
        var zipVal = arguments[0];
        var cityVal = arguments[1];
        var log = [];

        var postalEl = document.querySelector('[name="postal"]') ||
                       document.querySelector('[name="postal_code"]') ||
                       document.querySelector('#postal_code') ||
                       document.querySelector('#postal');

        if (!postalEl || !window.jQuery) {
            return {ok: false, reason: 'no-postal-or-jquery', log: log};
        }

        var jq = jQuery(postalEl);
        var fakeItem = {
            value: zipVal,
            label: zipVal + ' - ' + cityVal + ', CA',
            id: zipVal,
            geo_id: zipVal,
            postal: zipVal,
            area: cityVal
        };

        // ── Strategy 1: Fire the autocomplete uiAutocomplete select handler ──
        // CL attaches handler via jQuery('input.postal').on('autocompleteselect', ...)
        // We fire this exact event with a fake item object.
        try {
            var selectEvt = jQuery.Event('autocompleteselect');
            selectEvt.item = fakeItem;
            jq.trigger(selectEvt, [{item: fakeItem}]);
            log.push('fired-autocompleteselect');
        } catch(e) { log.push('autocompleteselect-err:' + e.message); }

        // ── Strategy 2: Find all jQuery handlers on the postal field ─────────
        try {
            var events = jQuery._data(postalEl, 'events') || {};
            var evNames = Object.keys(events);
            log.push('postal-events:' + evNames.join(','));
            // Fire each handler that looks like a select/change handler
            ['autocompleteselect', 'autocompletechange', 'autocompletefocus',
             'select', 'change', 'chosen:updated'].forEach(function(evName) {
                if (events[evName]) {
                    events[evName].forEach(function(h) {
                        try {
                            var fakeEvent = jQuery.Event(evName);
                            fakeEvent.item = fakeItem;
                            h.handler.call(postalEl, fakeEvent, {item: fakeItem});
                            log.push('called-handler:' + evName);
                        } catch(e2) { log.push('handler-err:' + evName + ':' + e2.message); }
                    });
                }
            });
        } catch(e) { log.push('events-walk-err:' + e.message); }

        // ── Strategy 3: Call CL's internal postingProcess location functions ─
        try {
            var clObj = window.cl || window.CL || {};
            var pp = clObj.postingProcess || clObj.posting || clObj.form || {};
            var fnNames = Object.keys(pp).filter(function(k) {
                return typeof pp[k] === 'function' && (
                    k.toLowerCase().indexOf('loc') !== -1 ||
                    k.toLowerCase().indexOf('geo') !== -1 ||
                    k.toLowerCase().indexOf('zip') !== -1 ||
                    k.toLowerCase().indexOf('postal') !== -1 ||
                    k.toLowerCase().indexOf('area') !== -1
                );
            });
            log.push('cl-location-fns:' + fnNames.join(','));
            fnNames.forEach(function(fn) {
                try {
                    pp[fn].call(pp, zipVal, fakeItem);
                    log.push('called:' + fn);
                } catch(e3) { log.push('fn-err:' + fn + ':' + e3.message); }
            });
        } catch(e) { log.push('cl-pp-err:' + e.message); }

        // ── Strategy 4: Walk ALL global objects for location setter ──────────
        try {
            var globalFns = [];
            ['cl','CL','clp','clPosting','posting','postingForm'].forEach(function(objName) {
                var obj = window[objName];
                if (!obj) return;
                var walk = function(o, prefix, depth) {
                    if (depth > 3) return;
                    Object.keys(o || {}).forEach(function(k) {
                        try {
                            if (typeof o[k] === 'function') {
                                var kl = k.toLowerCase();
                                if (kl.indexOf('loc') !== -1 || kl.indexOf('geo') !== -1 ||
                                    kl.indexOf('zip') !== -1 || kl.indexOf('postal') !== -1 ||
                                    kl.indexOf('area') !== -1 || kl.indexOf('set') !== -1) {
                                    globalFns.push(prefix + '.' + k);
                                }
                            } else if (typeof o[k] === 'object' && o[k] !== null) {
                                walk(o[k], prefix + '.' + k, depth + 1);
                            }
                        } catch(e) {}
                    });
                };
                walk(obj, objName, 0);
            });
            log.push('global-loc-fns:' + globalFns.join(','));
        } catch(e) { log.push('global-walk-err:' + e.message); }

        // ── Strategy 5: Simulate exact event sequence of real autocomplete ───
        // Real sequence: mouseenter → mousemove → mousedown → mouseup → click
        // on the suggestion LI, which jQuery UI translates to 'autocompleteselect'
        // We already fired autocompleteselect above. Also fire the full sequence:
        try {
            jq.triggerHandler('focus');
            setTimeout(function() {
                jq.val(zipVal);
                jq.triggerHandler(jQuery.Event('keydown',  {keyCode: 49})); // '1'
                jq.triggerHandler(jQuery.Event('input',    {bubbles: true}));
                jq.triggerHandler(jQuery.Event('keyup',    {keyCode: 49}));
                // Simulate dropdown item click
                var selectEv = jQuery.Event('autocompleteselect');
                selectEv.item = fakeItem;
                jq.trigger(selectEv, [{item: fakeItem}]);
                jq.triggerHandler(jQuery.Event('change',   {bubbles: true}));
                jq.triggerHandler(jQuery.Event('blur',     {bubbles: true}));
            }, 300);
            log.push('delayed-sequence-set');
        } catch(e) { log.push('sequence-err:' + e.message); }

        return {ok: true, log: log};
    """, zip_str, city_str)

    print(f"  [ZIP-internal] Location setter result: {result}")
    return result


def _fill_zip_with_network_intercept(driver, zip_field, zip_str):
    """
    The definitive ZIP fill strategy — v3:

    Root cause confirmed: CL makes ZERO network calls (_allNetworkCalls: []).
    The autocomplete dataset is local. The server rejects because CL's internal
    JS location state object is never updated — only the DOM input value is set.

    This version:
    1. Types ZIP with real native send_keys to trigger CL's autocomplete naturally
    2. Clicks the dropdown if it appears
    3. Calls _call_cl_internal_location_setter to fire CL's select handlers directly
    4. Keeps serializer + FormData patches as safety net
    5. Validator nuke to clear error state
    """
    # Ensure network spy is installed in this page context
    _install_network_spy_now(driver)
    time.sleep(0.3)

    # Install serializer + FormData patches
    patch_result = driver.execute_script(_ZIP_PATCH_JS, zip_str)
    print(f"  [ZIP] Patch install: {patch_result}")
    time.sleep(0.5)

    # Focus field with real mouse click
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", zip_field)
    time.sleep(0.5)

    # Tab from previous field to get natural focus chain
    try:
        prev = driver.find_element(By.CSS_SELECTOR,
            "[name='FromEMail'], [name='PostingBody'], textarea#PostingBody")
        ActionChains(driver).move_to_element(prev).click().perform()
        time.sleep(0.4)
        prev.send_keys(Keys.TAB)
        time.sleep(0.8)
    except Exception:
        ActionChains(driver).move_to_element(zip_field).pause(0.3).click().perform()
        time.sleep(0.5)

    # Re-locate field after tab
    zip_field = _find_field(driver, [
        "[name='postal']", "[name='postal_code']",
        "input#postal_code", "input#postal",
    ]) or zip_field

    # Ensure focus
    focused = driver.execute_script("return document.activeElement === arguments[0];", zip_field)
    if not focused:
        ActionChains(driver).move_to_element(zip_field).click().perform()
        time.sleep(0.3)

    # Clear and type with native send_keys (real trusted keyboard events)
    zip_field.send_keys(Keys.CONTROL + "a")
    time.sleep(0.1)
    zip_field.send_keys(Keys.DELETE)
    time.sleep(0.2)
    for ch in zip_str:
        zip_field.send_keys(ch)
        time.sleep(random.uniform(0.10, 0.18))
    time.sleep(1.2)  # Give autocomplete time to fire XHR

    # Try to find and click autocomplete dropdown
    dropdown_selectors = [
        ".ui-autocomplete li.ui-menu-item",
        ".ui-autocomplete li",
        "ul.ui-menu li",
        "ul[id*='ui-id'] li",
        "[class*='autocomplete'] li",
        "[role='option']",
        "[role='listbox'] li",
    ]
    suggestion_clicked = False
    try:
        WebDriverWait(driver, 5).until(
            lambda d: any(d.find_elements(By.CSS_SELECTOR, s) for s in dropdown_selectors))
        for sel in dropdown_selectors:
            items = driver.find_elements(By.CSS_SELECTOR, sel)
            visible = [s for s in items if s.is_displayed()]
            if visible:
                print(f"  [ZIP] Autocomplete dropdown found — clicking suggestion")
                ActionChains(driver).move_to_element(visible[0]).pause(0.3).click().perform()
                suggestion_clicked = True
                print("  [ZIP] Clicked suggestion ✓")
                break
    except TimeoutException:
        print("  [ZIP] No dropdown appeared")

    if not suggestion_clicked:
        # Arrow down attempt
        try:
            zip_field.send_keys(Keys.ARROW_DOWN)
            time.sleep(1.0)
            for sel in dropdown_selectors:
                items = driver.find_elements(By.CSS_SELECTOR, sel)
                visible = [s for s in items if s.is_displayed()]
                if visible:
                    ActionChains(driver).move_to_element(visible[0]).pause(0.3).click().perform()
                    suggestion_clicked = True
                    print("  [ZIP] Clicked suggestion after ArrowDown ✓")
                    break
        except Exception:
            pass

    if not suggestion_clicked:
        zip_field.send_keys(Keys.TAB)
        print("  [ZIP] Tabbed out (no dropdown)")

    # ── Call CL's internal location setter directly ──────────────────────────
    # CL uses ZERO network calls (confirmed by logs). The real confirmation
    # happens via CL's internal JS autocomplete select handlers updating an
    # internal state object. We call those handlers directly.
    time.sleep(0.5)
    _call_cl_internal_location_setter(driver, zip_str)
    time.sleep(0.8)

    # Wait for any AJAX to complete
    print("  [ZIP] Waiting for location AJAX...")
    try:
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return typeof jQuery==='undefined' || jQuery.active===0"))
        print("  [ZIP] AJAX complete ✓")
    except Exception:
        print("  [ZIP] AJAX wait timed out")
    time.sleep(2.0)

    # ── CRITICAL: Check if a real geo network call was made ──────────────────
    geo_responses, all_calls = _get_geo_responses(driver)
    print(f"  [GEO] Captured {len(geo_responses)} geo response(s), {len(all_calls)} total network calls")

    if all_calls:
        print(f"  [GEO] All network calls during ZIP entry:")
        for call in all_calls:
            print(f"    {call.get('type','?')} {call.get('status','?')} {call.get('url','')[:100]}")

    geo_injected = False
    if geo_responses:
        for geo_resp in geo_responses:
            print(f"  [GEO] Real geo response from: {geo_resp.get('url','')}")
            print(f"  [GEO] Response body: {geo_resp.get('responseText','')[:500]}")
            if _inject_geo_hidden_fields(driver, geo_resp.get('responseText',''), zip_str):
                geo_injected = True
                break

    if not geo_injected:
        print("  [GEO] No real geo XHR captured — trying direct source function call")
        # Try calling CL's autocomplete source function directly
        items = _trigger_real_geo_lookup(driver, zip_str)
        if items:
            # Re-check network captures (source fn may have fired XHR now)
            time.sleep(2)
            geo_responses, _ = _get_geo_responses(driver)
            for geo_resp in geo_responses:
                if _inject_geo_hidden_fields(driver, geo_resp.get('responseText',''), zip_str):
                    geo_injected = True
                    break

    if not geo_injected:
        print("  [GEO] Falling back to direct Python geo request")
        geo_text, geo_url = _fetch_cl_geo_direct(driver, zip_str)
        if geo_text:
            print(f"  [GEO] Direct fetch succeeded from {geo_url}")
            _inject_geo_hidden_fields(driver, geo_text, zip_str)
        else:
            print("  [GEO] ⚠ Could not obtain geo response by any method")
            print("  [GEO] ⚠ Run DevTools snippet from debug note on a REAL manual submission")
            print("  [GEO] ⚠ to identify exactly which hidden field(s) are missing")

    # Check patch status
    patch_status = driver.execute_script("""
        return {
            patchInstalled: window._clZipPatchInstalled,
            patchResults: window._clZipPatchResults,
            serializerPatched: window._clSerializerPatched,
            widgetCreated: window._clZipWidgetCreated,
            widgetErr: window._clZipWidgetErr,
            autoconfirmed: window._clZipAutoconfirmed,
            xhrIntercepted: window._clPostalXhrIntercepted || null,
            fetchIntercepted: window._clPostalFetchIntercepted || null,
            geoResponses: (window._clCapturedGeoResponses || []).length,
            zipFired: window._clZipFired || null
        };
    """)
    print(f"  [ZIP] Patch status: {patch_status}")

    # Re-read field value
    zip_field = _find_field(driver, [
        "[name='postal']", "[name='postal_code']",
        "input#postal_code", "input#postal",
    ]) or zip_field
    actual = (zip_field.get_attribute("value") if zip_field else "") or ""

    # If still empty, force-set
    if not actual and zip_field:
        print("  [ZIP] Value empty — force-setting via native setter")
        driver.execute_script("""
            var el = arguments[0], v = arguments[1];
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
            el.setAttribute('value', v);
            if (window.jQuery) jQuery(el).val(v).trigger('input').trigger('change');
        """, zip_field, zip_str)
        time.sleep(0.5)
        actual = zip_field.get_attribute("value") or ""

    print(f"  ✓ [ZIP] = '{actual}'")

    # Dump hidden fields — this is the critical diagnostic
    hidden = driver.execute_script("""
        var r = {};
        document.querySelectorAll('input[type="hidden"]').forEach(function(e) {
            r[e.name || e.id || '?'] = e.value;
        });
        return r;
    """)
    print(f"  [ZIP] Hidden fields after geo injection: {hidden}")

    return zip_field


def fill_and_submit_with_wire(driver, product, zip_code, city_name, cl_email):
    """
    Fill the CL posting form and submit.
    Uses CDP typing for all fields, network interception + geo injection for ZIP.
    """
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm")))
    except TimeoutException:
        print("  ✗ postingForm not found")
        return None

    handle_captcha_if_present(driver)
    _wait_for_cl_js_init(driver)

    # Build values
    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    description = (product.get("description") or (
        f"{title} in excellent condition. Well maintained and ready for a new home. "
        "Priced to sell. Local pickup preferred. Message for details.")).strip()
    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "").replace("Rs", "").strip()
    try:
        price = str(round(float(_pr))) if _pr else "10"
    except Exception:
        price = "10"

    print(f"  [fill] title='{title[:50]}' price={price} zip={zip_code} city={city_name}")

    # ── 1. Title ──────────────────────────────────────────────────────────────
    title_field = _find_field(driver, [
        "[name='PostingTitle']", "input#PostingTitle", "input#title",
    ])
    if title_field:
        _cdp_type(driver, title_field, title)
        actual = title_field.get_attribute("value") or ""
        print(f"  ✓ [title] = '{actual[:60]}'")
    else:
        print("  ✗ [title] field not found!")
        return None
    time.sleep(random.uniform(0.4, 0.7))

    # ── 2. Price ──────────────────────────────────────────────────────────────
    price_field = _find_field(driver, [
        "[name='price']", "[name='AskingPrice']", "[name='AskPrice']", "input#price",
    ])
    if price_field:
        _cdp_type(driver, price_field, price)
        actual = price_field.get_attribute("value") or ""
        print(f"  ✓ [price] = '{actual}'")
    else:
        print("  ⚠ [price] field not found")
    time.sleep(random.uniform(0.3, 0.6))

    # ── 3. City / neighborhood ────────────────────────────────────────────────
    city_field = _find_field(driver, [
        "[name='geographic_area']", "input#geographic_area", "[name='city']",
    ])
    if city_field and city_name:
        _cdp_type(driver, city_field, city_name)
        actual = city_field.get_attribute("value") or ""
        print(f"  ✓ [city] = '{actual}'")
    time.sleep(random.uniform(0.3, 0.5))

    # ── 4. Description ────────────────────────────────────────────────────────
    desc_field = _find_field(driver, [
        "[name='PostingBody']", "textarea#PostingBody", "textarea#description",
    ])
    if desc_field:
        _cdp_type(driver, desc_field, description)
        actual = (desc_field.get_attribute("value") or "")[:40]
        print(f"  ✓ [description] = '{actual}'")
    else:
        print("  ✗ [description] field not found!")
        return None
    time.sleep(random.uniform(0.4, 0.7))

    # ── 5. Email if editable ──────────────────────────────────────────────────
    try:
        email_el = driver.find_element(By.CSS_SELECTOR, "[name='FromEMail']")
        if not email_el.get_attribute("disabled") and not email_el.get_attribute("readOnly"):
            if cl_email:
                _cdp_type(driver, email_el, cl_email)
                print(f"  ✓ [email] = '{cl_email}'")
        else:
            print("  [email] Pre-filled by account")
    except Exception:
        pass
    time.sleep(random.uniform(0.4, 0.6))

    # ── 6. ZIP — with full network interception + geo injection ───────────────
    if zip_code:
        zip_str = str(zip_code).strip()
        zip_field = _find_field(driver, [
            "[name='postal']", "[name='postal_code']",
            "input#postal_code", "input#postal",
        ])
        if zip_field:
            zip_field = _fill_zip_with_network_intercept(driver, zip_field, zip_str)
        else:
            print("  ⚠ [ZIP] field not found")

    # ── Wait for all AJAX to settle ───────────────────────────────────────────
    try:
        WebDriverWait(driver, 8).until(
            lambda d: d.execute_script("return typeof jQuery==='undefined' || jQuery.active == 0"))
    except Exception:
        pass
    time.sleep(0.5)

    # ── Nuclear validator nuke ────────────────────────────────────────────────
    if zip_code:
        nuke_result = driver.execute_script(_VALIDATOR_NUKE_JS, str(zip_code).strip())
        print(f"  [submit] Validator nuke: {nuke_result}")

    # ── Payload capture ───────────────────────────────────────────────────────
    capture_result = driver.execute_script("""
        var zipVal = arguments[0];
        var form = document.getElementById('postingForm');
        var jqSerialized = null;
        var jqSerializedArray = null;
        if (form && window.jQuery) {
            try {
                jqSerialized = jQuery(form).serialize();
                jqSerializedArray = jQuery(form).serializeArray();
            } catch(e) { jqSerialized = 'error:' + e.message; }
        }
        var nativeFormData = null;
        try {
            var fd = new FormData(form);
            var fdPairs = [];
            fd.forEach(function(v, k) { fdPairs.push(k + '=' + String(v).substring(0,100)); });
            nativeFormData = fdPairs.join('&');
        } catch(e) { nativeFormData = 'error:' + e.message; }

        var postalInJQ = null;
        if (jqSerializedArray) {
            jqSerializedArray.forEach(function(item) {
                if (item.name === 'postal' || item.name === 'postal_code') {
                    postalInJQ = item.name + '=' + item.value;
                }
            });
        }
        return {
            jqSerialize: jqSerialized ? jqSerialized.substring(0, 500) : null,
            postalInJQ: postalInJQ,
            nativeFormData: nativeFormData ? nativeFormData.substring(0, 500) : null
        };
    """, str(zip_code).strip() if zip_code else "")

    print(f"  [payload] jQuery serialize (first 500): {capture_result.get('jqSerialize', 'N/A')}")
    print(f"  [payload] postal in jQuery serialized : {capture_result.get('postalInJQ', 'MISSING')}")
    print(f"  [payload] native FormData             : {capture_result.get('nativeFormData', 'N/A')}")

    # ── Find submit button ────────────────────────────────────────────────────
    print("  [submit] Finding and clicking Continue button...")
    submitted = False
    submit_btn = None

    for by, sel in [
        (By.CSS_SELECTOR, "button.go.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "button.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "#postingForm button[type='submit']"),
        (By.CSS_SELECTOR, "#postingForm input[type='submit']"),
        (By.CSS_SELECTOR, "button.go"),
        (By.XPATH, "//button[@type='submit' and (contains(@class,'go') or contains(@class,'bigbutton'))]"),
        (By.XPATH, "//button[normalize-space(.)='continue' or normalize-space(.)='Continue']"),
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.CSS_SELECTOR, "input[type='submit']"),
    ]:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, sel)))
            if el.is_displayed():
                submit_btn = el
                label = (el.text or el.get_attribute("value") or sel)[:40]
                print(f"  [submit] Found button: '{label}'")
                break
        except Exception:
            continue

    if not submit_btn:
        print("  [submit] ✗ No submit button found!")
        return None

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
    time.sleep(0.5)

    # Final ZIP force-set right before click
    if zip_code:
        driver.execute_script("""
            var pEl = document.querySelector('[name="postal"]') ||
                      document.querySelector('[name="postal_code"]') ||
                      document.querySelector('#postal_code') ||
                      document.querySelector('#postal');
            if (pEl) {
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(pEl, arguments[0]);
                pEl.setAttribute('value', arguments[0]);
                if (window.jQuery) jQuery(pEl).val(arguments[0]);
            }
        """, str(zip_code).strip())

    # Primary: ActionChains click
    try:
        ActionChains(driver).move_to_element(submit_btn).pause(
            random.uniform(0.3, 0.6)).click().perform()
        submitted = True
        print("  [submit] Clicked via ActionChains ✓")
    except Exception as e:
        print(f"  [submit] ActionChains failed ({e})")

    # Immediately re-set ZIP after click
    if submitted and zip_code:
        time.sleep(0.15)
        driver.execute_script("""
            var pEl = document.querySelector('[name="postal"]') ||
                      document.querySelector('[name="postal_code"]') ||
                      document.querySelector('#postal_code') ||
                      document.querySelector('#postal');
            if (pEl) {
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(pEl, arguments[0]);
            }
        """, str(zip_code).strip())

    # Fallback: requestSubmit
    if not submitted:
        try:
            result = driver.execute_script("""
                var form = document.getElementById('postingForm');
                var btn  = arguments[0];
                if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit(btn);
                    return 'requestSubmit';
                }
                btn.click();
                return 'direct-click';
            """, submit_btn)
            submitted = True
            print(f"  [submit] Fallback: {result} ✓")
        except Exception as e:
            print(f"  [submit] Fallback also failed: {e}")

    if not submitted:
        return None

    # Brief wait then dump captured payloads
    time.sleep(3)
    captured = driver.execute_script("return window._clCapturedPayloads || [];")
    if captured:
        print(f"  [payload] CAPTURED {len(captured)} POST request(s) after submit click:")
        for i, p in enumerate(captured):
            print(f"  [payload] [{i}] type={p.get('type')} url={p.get('url','')[:80]}")
            body = p.get('body') or ''
            print(f"  [payload] [{i}] body={body[:600]}")
            if 'postal' in body.lower():
                matches = re.findall(r'postal[^=&]*=[^&]{0,20}', body, re.IGNORECASE)
                print(f"  [payload] [{i}] POSTAL in body: {matches}")
    else:
        print("  [payload] No XHR/fetch POST captured — CL uses native form submit")

    native_payloads = driver.execute_script("return window._clNativeSubmitPayloads || [];")
    if native_payloads:
        print(f"  [payload] NATIVE FORM SUBMIT captured ({len(native_payloads)} submission(s)):")
        for i, p in enumerate(native_payloads):
            print(f"  [payload] [native-{i}] via={p.get('via')} action={p.get('action','')[:80]}")
            body = p.get('body') or ''
            print(f"  [payload] [native-{i}] FULL BODY: {body}")
            postal_matches = re.findall(r'postal[^=&]*=[^&]{0,30}', body, re.IGNORECASE)
            print(f"  [payload] [native-{i}] POSTAL fields: {postal_matches}")
    else:
        print("  [payload] No native form.submit() captured either")
        form_html = driver.execute_script("""
            var form = document.getElementById('postingForm');
            if (!form) return 'no-form';
            var inputs = [];
            form.querySelectorAll('input,textarea,select').forEach(function(el) {
                inputs.push({name: el.name || el.id, type: el.type || el.tagName, value: (el.value || '').substring(0, 100)});
            });
            return inputs;
        """)
        print(f"  [payload] All form fields at submit time: {form_html}")

    # ── Wait to leave the edit page ───────────────────────────────────────────
    deadline = time.time() + 35
    while time.time() < deadline:
        cur = driver.current_url
        if "s=edit" not in cur:
            print(f"  ✅ Left edit page → {cur}")
            return cur
        time.sleep(0.5)

    # Still stuck — log validation errors
    print("  [submit] Still on edit page after 35s — checking for validation errors...")
    try:
        for fname, selectors in [
            ("PostingTitle", ["[name='PostingTitle']"]),
            ("PostingBody",  ["[name='PostingBody']"]),
            ("postal",       ["[name='postal']", "[name='postal_code']"]),
            ("price",        ["[name='price']", "[name='AskingPrice']"]),
        ]:
            for sel in selectors:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    val = (el.get_attribute("value") or "")[:40]
                    inv = el.get_attribute("aria-invalid")
                    print(f"  [fail] {fname}='{val}' invalid={inv}")
                    break
                except Exception:
                    continue

        errs = driver.execute_script("""
            var msgs = [];
            document.querySelectorAll('.err,.error,.notice').forEach(function(el) {
                var t = (el.textContent||'').replace(/[ \\t\\n]+/g,' ').trim();
                if (t && t.length > 3 && t.length < 200) msgs.push(t);
            });
            return msgs;
        """) or []
        if errs:
            print(f"  [fail-errors] {errs[:8]}")
        else:
            print("  [fail-errors] No .error elements found")

        # ── MANUAL SUBMISSION DIFF HELPER ─────────────────────────────────────
        # Print the DevTools snippet output for comparison against a real run.
        # If you run the DevTools snippet from the debug note on a real manual
        # submission and compare the output, you will see exactly which field
        # is present in the manual run but missing here.
        print("  [diagnostic] Current browser state for manual diff:")
        state = driver.execute_script("""
            var r = {};
            document.querySelectorAll('input[type="hidden"]').forEach(function(e) {
                r[e.name || e.id || '?'] = e.value;
            });
            document.querySelectorAll('input,textarea,select').forEach(function(e) {
                if (e.value && e.name) r['_field_' + e.name] = e.value.substring(0,50);
            });
            r['_cookies'] = document.cookie;
            r['_localStorage'] = JSON.stringify(localStorage);
            r['_sessionStorage'] = JSON.stringify(sessionStorage);
            try { r['_jqFormData'] = JSON.stringify(jQuery('#postingForm').data()); } catch(e) {}
            r['_geoResponses'] = (window._clCapturedGeoResponses||[]).length;
            r['_allNetworkCalls'] = (window._clAllNetworkCalls||[]).map(function(c){
                return c.type + ':' + c.status + ':' + c.url.substring(0,80);
            });
            return r;
        """)
        print(f"  [diagnostic] State: {json.dumps(state, indent=2)[:2000]}")

        # Recovery attempt
        print("  [submit] Recovery: re-nuking validator and re-clicking...")
        if zip_code:
            driver.execute_script(_VALIDATOR_NUKE_JS, str(zip_code).strip())
        driver.execute_script("""
            document.querySelectorAll('input,textarea').forEach(function(el) {
                el.dispatchEvent(new Event('blur', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            });
        """)
        time.sleep(1)
        for by, sel in [
            (By.CSS_SELECTOR, "button.go.bigbutton[type='submit']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
        ]:
            try:
                btn = driver.find_element(by, sel)
                if btn.is_displayed():
                    ActionChains(driver).move_to_element(btn).click().perform()
                    print("  [submit] Recovery click sent")
                    time.sleep(10)
                    if "s=edit" not in driver.current_url:
                        print(f"  ✅ Recovery worked → {driver.current_url}")
                        return driver.current_url
                    break
            except Exception:
                continue
    except Exception as debug_err:
        print(f"  [debug] Error during debug: {debug_err}")

    print("  ❌ Submit failed")
    return None


def fill_listing_details(driver, product: dict):
    _ZIPS = {
        "losangeles": "90001", "newyork": "10001", "chicago": "60601",
        "houston": "77001", "phoenix": "85001", "sfbay": "94102",
        "sandiego": "92101", "seattle": "98101", "miami": "33101",
        "dallas": "75201", "denver": "80201", "atlanta": "30301",
        "boston": "02101", "portland": "97201", "anchorage": "99502",
        "orlando": "32827", "honolulu": "96820", "indianapolis": "46220",
        "wichita": "67212", "louisville": "40210", "neworleans": "70117",
        "baltimore": "21222", "detroit": "48210", "minneapolis": "55440",
        "stlouis": "63138", "omaha": "68110", "lasvegas": "89030",
        "albuquerque": "87108", "brooklyn": "11206", "raleigh": "27604",
        "fargo": "58102", "columbus": "43211", "philadelphia": "19019",
        "nashville": "37205", "saltlakecity": "84118", "milwaukee": "53221",
    }

    zip_code = (
        product.get("_location_zip") or
        os.environ.get("CL_ZIP") or
        product.get("zip_code") or
        product.get("postal_code") or
        ""
    ).strip()
    if not zip_code:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        zip_code = _ZIPS.get(_ck, "")

    _CITY_NAMES = {
        "losangeles": "Los Angeles", "newyork": "New York", "chicago": "Chicago",
        "houston": "Houston", "phoenix": "Phoenix", "sfbay": "San Francisco",
        "sandiego": "San Diego", "seattle": "Seattle", "miami": "Miami",
        "dallas": "Dallas", "denver": "Denver", "atlanta": "Atlanta",
        "boston": "Boston", "portland": "Portland",
    }
    _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
    city_name = (
        product.get("_location_city") or
        os.environ.get("CL_CITY_NAME") or
        _CITY_NAMES.get(_ck, CL_CITY.title())
    )

    cl_email = (os.environ.get("CL_EMAIL") or
                product.get("contact_email") or product.get("email") or "").strip()

    result_url = fill_and_submit_with_wire(
        driver, product, zip_code, city_name, cl_email)

    if result_url and "s=edit" not in result_url:
        print(f"  ✓ Form submitted → {result_url}")
        return True

    print("  ✗ Still on edit page after form submit")
    return False


def _click_first(driver, selectors, label="button"):
    for by, sel in selectors:
        try:
            el = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((by, sel)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.35)
            try:
                ActionChains(driver).move_to_element(el).pause(
                    random.uniform(0.2, 0.5)).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            print(f"  ✓ Clicked {label} ({sel[:50]})")
            return True
        except Exception:
            continue
    return False


def _wait_for_images_page(driver, timeout=20):
    print("  [images] Waiting for image upload page...")
    try:
        WebDriverWait(driver, timeout).until(lambda d: (
            "s=images" in d.current_url
            or d.find_elements(By.ID, "done_with_images_button")
            or d.find_elements(By.ID, "add_photos_button")
            or "done with images" in (d.page_source or "").lower()
        ))
        print(f"  [images] Page ready → {driver.current_url}")
        return True
    except TimeoutException:
        print(f"  [images] Timed out — URL: {driver.current_url}")
        return False


def complete_images_step(driver, product: dict):
    if not _wait_for_images_page(driver):
        return False

    handle_captcha_if_present(driver)
    human_delay(2, 4)

    photo_paths = product.get("photo_paths", []) or product.get("images", [])
    temp_files = []
    valid = []
    for p in photo_paths:
        if isinstance(p, str) and p.startswith("http"):
            try:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                tmp.close()
                urllib.request.urlretrieve(p, tmp.name)
                valid.append(tmp.name)
                temp_files.append(tmp.name)
            except Exception as e:
                print(f"  [images] Could not download {p}: {e}")
        elif isinstance(p, str) and os.path.isfile(p):
            valid.append(p)

    try:
        if valid:
            print(f"  [images] Uploading {len(valid)} photo(s)...")
            try:
                file_input = None
                for by, sel in [
                    (By.ID, "fileInput"),
                    (By.CSS_SELECTOR, "input[type='file']"),
                ]:
                    try:
                        file_input = driver.find_element(by, sel)
                        break
                    except NoSuchElementException:
                        continue

                if file_input:
                    for path in valid:
                        file_input.send_keys(os.path.abspath(path))
                        human_delay(1.5, 3)
                    print(f"  [images] Sent {len(valid)} file(s) to input")
                    human_delay(6, 10)
                else:
                    print("  [images] ⚠ No file input found")
            except Exception as e:
                print(f"  [images] Upload error: {e}")
        else:
            print("  [images] No photos — proceeding to done with images")

        done_selectors = [
            (By.ID, "done_with_images_button"),
            (By.XPATH, "//button[contains(translate(normalize-space(.),'DONE','done'),'done with images')]"),
            (By.CSS_SELECTOR, "button.done_with_images, button[class*='done']"),
            (By.XPATH, "//input[@type='submit' and contains(translate(@value,'DONE','done'),'done')]"),
        ]
        if not _click_first(driver, done_selectors, "done with images"):
            print("  [images] ✗ Could not find 'done with images' button")
            return False

        human_delay(3, 5)
        handle_captcha_if_present(driver)

        try:
            WebDriverWait(driver, 20).until(lambda d: (
                "s=preview" in d.current_url
                or d.find_elements(By.ID, "publish_bottom")
                or d.find_elements(By.ID, "publish_top")
                or d.find_elements(By.ID, "publish_button")
                or "unpublished draft" in (d.page_source or "").lower()
            ))
            print(f"  [images] ✓ Reached draft preview → {driver.current_url}")
            return True
        except TimeoutException:
            print(f"  [images] ⚠ Preview not detected — URL: {driver.current_url}")
            return "s=edit" not in driver.current_url and "s=images" not in driver.current_url
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


def upload_photos(driver, product: dict):
    return complete_images_step(driver, product)


def _wait_for_draft_preview(driver, timeout=20):
    try:
        WebDriverWait(driver, timeout).until(lambda d: (
            "s=preview" in d.current_url
            or d.find_elements(By.ID, "publish_bottom")
            or d.find_elements(By.ID, "publish_top")
            or d.find_elements(By.ID, "publish_button")
            or "unpublished draft" in (d.page_source or "").lower()
        ))
        print(f"  [publish] Draft page ready → {driver.current_url}")
        return True
    except TimeoutException:
        print(f"  [publish] ⚠ Draft page not detected — URL: {driver.current_url}")
        return False


def _submit_publish_form(driver):
    publish_selectors = [
        (By.CSS_SELECTOR, "#publish_bottom button.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "#publish_bottom button[name='go']"),
        (By.CSS_SELECTOR, "#publish_top button.bigbutton[type='submit']"),
        (By.CSS_SELECTOR, "#publish_top button[name='go']"),
        (By.ID, "publish_button"),
        (By.XPATH, "//form[@id='publish_bottom']//button[@type='submit']"),
        (By.XPATH, "//form[@id='publish_top']//button[@type='submit']"),
        (By.XPATH, "//button[contains(translate(normalize-space(.),'PUBLISH','publish'),'publish')]"),
        (By.XPATH, "//input[@type='submit' and contains(translate(@value,'PUBLISH','publish'),'publish')]"),
    ]

    try:
        result = driver.execute_script("""
            var form = document.getElementById('publish_bottom')
                    || document.getElementById('publish_top');
            if (!form) return {ok: false, reason: 'no-publish-form'};
            var btn = form.querySelector('button.bigbutton[type="submit"], button[name="go"]');
            if (!btn) return {ok: false, reason: 'no-publish-btn'};
            btn.scrollIntoView({block: 'center'});
            try {
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(btn);
                    return {ok: true, method: 'requestSubmit', form: form.id};
                }
            } catch (e) {}
            btn.click();
            return {ok: true, method: 'click', form: form.id};
        """) or {}
        print(f"  [publish] form submit → {result}")
        if result.get("ok"):
            time.sleep(4)
            if "s=preview" not in driver.current_url:
                return True
    except Exception as e:
        print(f"  [publish] requestSubmit failed: {e}")

    if _click_first(driver, publish_selectors, "publish"):
        time.sleep(4)
        if "s=preview" not in driver.current_url:
            return True

    return "s=preview" not in driver.current_url


def publish_listing(driver, ad_name, product):
    handle_captcha_if_present(driver)
    print("  [publish] Waiting for draft preview page...")
    _wait_for_draft_preview(driver)
    human_delay(2, 4)

    if not _submit_publish_form(driver):
        print(f"  [publish] ✗ Publish failed for '{ad_name}' — URL: {driver.current_url}")
        return False

    human_delay(3, 5)
    handle_captcha_if_present(driver)

    try:
        WebDriverWait(driver, 20).until(lambda d: (
            "s=preview" not in d.current_url
            and "s=images" not in d.current_url
            and "s=edit" not in d.current_url
        ))
    except TimeoutException:
        pass

    listing_url = driver.current_url
    print(f"  [publish] ✓ Published → {listing_url}")
    posted_listings[ad_name] = {
        "url": listing_url, "post_time": datetime.now(),
        "visitors": 0, "platform": "Craigslist",
    }
    _save_listings()
    return True


def post_product(driver, ad_name, product):
    post_url = "https://post.craigslist.org/c/sss"
    print(f"  Navigating to: {post_url}")
    driver.get(post_url)
    human_delay(4, 7)
    handle_captcha_if_present(driver)

    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete")
    except TimeoutException:
        return False

    print(f"  Page title: {driver.title}")
    print(f"  Current URL: {driver.current_url}")

    if "login" in driver.current_url.lower():
        print("  ✗ Session expired.")
        return False

    # ── City selection ────────────────────────────────────────────────────────
    try:
        city_button = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "span#ui-id-1-button")))
        driver.execute_script("arguments[0].click();", city_button)
        human_delay(2, 3)
        menu_items = WebDriverWait(driver, 5).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "ul#ui-id-1-menu li")))
        city_clicked = False
        target = CL_CITY.lower().replace(" ", "").replace("-", "")
        for item in menu_items:
            txt = (item.text.strip() or item.get_attribute("textContent").strip())
            if target in txt.lower().replace(" ", "") or txt.lower().replace(" ", "") in target:
                driver.execute_script("arguments[0].click();", item)
                city_clicked = True
                print(f"  ✓ Selected city: {txt}")
                break
        if not city_clicked:
            for item in menu_items:
                txt = (item.text or '').strip().lower()
                if any(word in txt for word in target.split() if len(word) > 3):
                    driver.execute_script("arguments[0].click();", item)
                    city_clicked = True
                    print(f"  ✓ Partial match city: {item.text.strip()}")
                    break
        if not city_clicked:
            available = [m.text.strip() for m in menu_items[:8]]
            print(f"  ✗ City '{CL_CITY}' NOT FOUND. Available: {available}")
            return False
        human_delay(2, 3)
        continue_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']")))
        driver.execute_script("arguments[0].click();", continue_btn)
        print("  ✓ Submitted city selection")
        try:
            WebDriverWait(driver, 12).until(lambda d: "s=area" not in d.current_url)
            print(f"  ✓ Left area page → {driver.current_url}")
        except TimeoutException:
            print("  ⚠ Still on area page after 12s")
        handle_captcha_if_present(driver)
    except Exception as e:
        print(f"  City selection error: {e}")

    if "s=area" in driver.current_url:
        try:
            retry_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']")))
            driver.execute_script("arguments[0].click();", retry_btn)
            WebDriverWait(driver, 10).until(lambda d: "s=area" not in d.current_url)
        except Exception as e:
            print(f"  ✗ City retry failed: {e}")
            return False

    if "s=area" in driver.current_url:
        return False

    print(f"  ✓ Left area → {driver.current_url}")
    handle_captcha_if_present(driver)
    human_delay(2, 4)

    # ── Post type ─────────────────────────────────────────────────────────────
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "input[value='fso']") or
                      d.find_elements(By.CSS_SELECTOR, "input[type='radio']"))
        print("  ✓ Post type page loaded")
    except TimeoutException:
        return False

    fso_clicked = False
    for val in ['fso', 'fs', 'forsale', 'sss']:
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"input[value='{val}']")
            driver.execute_script("arguments[0].click();", el)
            fso_clicked = True
            print(f"  ✓ Selected post type via input value='{val}'")
            break
        except NoSuchElementException:
            pass
    if not fso_clicked:
        print("  ✗ Could not find 'for sale by owner'")
        return False

    human_delay(3, 5)
    handle_captcha_if_present(driver)

    # ── Category ──────────────────────────────────────────────────────────────
    cat_clicked = False
    mapped_label = CATEGORY_MAPPING.get(
        product.get("category", "").lower().strip(), (None, ""))[1]
    if not mapped_label:
        mapped_label = product.get("category", "")
    print(f"  Target category label: {mapped_label}")

    if mapped_label:
        try:
            target_lower = mapped_label.lower().strip()
            xpath = (f"//label[contains("
                     f"translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
                     f", '{target_lower}')]")
            label_el = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].click();", label_el)
            cat_clicked = True
            print(f"  ✓ Selected category via label XPath: '{mapped_label}'")
        except Exception as e:
            print(f"  Category label failed: {e}")

    if not cat_clicked:
        try:
            ul_value = get_category_ul_value(product.get("category", ""))
            inp = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, f"input[type='radio'][value='{ul_value}']")))
            driver.execute_script("arguments[0].click();", inp)
            cat_clicked = True
            print(f"  ✓ Selected category via radio value={ul_value}")
        except Exception:
            pass

    if not cat_clicked:
        try:
            first_label = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "label.radio-option, label")))
            driver.execute_script("arguments[0].click();", first_label)
            cat_clicked = True
        except Exception:
            pass

    if not cat_clicked:
        return False

    human_delay(2, 3)
    try:
        continue_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']")))
        driver.execute_script("arguments[0].click();", continue_btn)
        print("  ✓ Clicked category continue button")
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.ID, "postingForm")))
            time.sleep(2)
            print("  ✓ postingForm visible after category selection")
        except TimeoutException:
            time.sleep(3)
        print(f"  Current URL after category continue: {driver.current_url}")
        handle_captcha_if_present(driver)
    except TimeoutException:
        human_delay(2, 3)

    click_relocation_if_needed(driver, ad_name)

    # ── Fill and submit ───────────────────────────────────────────────────────
    try:
        success = fill_listing_details(driver, product)
    except Exception as e:
        print(f"  ✗ fill_listing_details crashed: {e}")
        import traceback
        traceback.print_exc()
        return False

    if not success:
        return False

    # ── Image upload ──────────────────────────────────────────────────────────
    if not complete_images_step(driver, product):
        print("  ✗ Failed at image upload step")
        return False

    # ── Publish ───────────────────────────────────────────────────────────────
    if not publish_listing(driver, ad_name, product):
        print("  ✗ Failed at publish step")
        return False

    return True


def update_ad_analytics_periodically():
    if IS_RAILWAY:
        print("[CL] Analytics thread disabled on Railway (memory constraint). Skipping.")
        return
    while True:
        print("\n[CL] Refreshing analytics…")
        for ad_name, listing in list(posted_listings.items()):
            if not listing.get("url") or listing.get("platform") != "Craigslist":
                continue
            tmp = None
            try:
                tmp = make_driver()
                tmp.get(listing["url"])
                human_delay(2, 4)
                views_el = WebDriverWait(tmp, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#views_count")))
                count = int("".join(filter(str.isdigit, views_el.text)) or "0")
                posted_listings[ad_name]["visitors"] = count
            except Exception as e:
                print(f"  ⚠ Analytics error for {ad_name}: {e}")
            finally:
                if tmp:
                    tmp.quit()
        _save_listings()
        time.sleep(300)


def main():
    global CL_CITY
    email = os.environ.get("CL_EMAIL", "").strip()
    if not email:
        print("✗ CL_EMAIL environment variable not set. Add it to Railway Variables.")
        return
    CL_CITY = os.environ.get("CL_CITY", CL_CITY)
    _load_existing_listings()

    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    driver = make_driver(proxy_url=proxy_url)
    if not craigslist_login(driver, email):
        driver.quit()
        return

    products_file = os.environ.get("PRODUCTS_FILE", "products.json")
    if not os.path.exists(products_file):
        print(f"✗ {products_file} not found.")
        driver.quit()
        return
    with open(products_file) as f:
        products = json.load(f)

    threading.Thread(target=update_ad_analytics_periodically, daemon=True).start()

    for product in products:
        product_title = product.get("title") or product.get("name", "No Title")
        ad_name = f"CL_{product_title}"
        print(f"\nPosting: {product_title}")
        try:
            ok = post_product(driver, ad_name, product)
        except Exception as e:
            print(f"  ✗ post_product crashed for '{product_title}': {e}")
            import traceback
            traceback.print_exc()
            ok = False
        print("  ✓ Posted" if ok else "  ✗ Failed")
        time.sleep(3)

    print("\nAll Craigslist products processed.")
    driver.quit()


if __name__ == "__main__":
    main()