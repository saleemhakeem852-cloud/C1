"""
craigslist.py — CLBlast Craigslist automation
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
    "entertainment": (17, "cds / dvds / vhs"),
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
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36",
        "platform": "Win32", "vendor": "Google Inc.", "lang": "en-US",
        "screen": (1440, 900), "tz": "America/Denver",
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
    Object.defineProperty(navigator, 'plugins', {{
        get: () => {{
            var ps = [
                {{name:'Chrome PDF Plugin',      filename:'internal-pdf-viewer', description:'Portable Document Format'}},
                {{name:'Chrome PDF Viewer',      filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:''}},
                {{name:'Native Client',          filename:'internal-nacl-plugin', description:''}},
            ];
            ps.refresh = function(){{}};
            ps.item    = function(i){{return ps[i];}};
            ps.namedItem = function(n){{return ps.find(p=>p.name===n)||null;}};
            Object.defineProperty(ps,'length',{{get:()=>ps.length}});
            return ps;
        }}
    }});
    const origQuery = window.Permissions && window.Permissions.prototype.query;
    if (origQuery) {{
        window.Permissions.prototype.query = function(p) {{
            return p.name === 'notifications'
                ? Promise.resolve({{state: 'granted', onchange: null}})
                : origQuery.apply(this, arguments);
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
        print(f"  [driver] Proxy: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")
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
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{fp["platform"]}"',
        }})
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


def _type_into_field(driver, selector, value, label="field"):
    """
    Click a field, triple-click to select all, delete, then type char by char.
    Goes through OS keyboard pipeline — CL cannot flag as autofill.
    """
    value = str(value).strip()
    if not value:
        return ""
    try:
        el = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
    except Exception as e:
        print(f"  ✗ [{label}] not found: {e}")
        return ""

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.3)

    # Triple-click to select all existing text
    ActionChains(driver).move_to_element(el).click(el).click(el).click(el).perform()
    time.sleep(0.2)
    el.send_keys(Keys.DELETE)
    time.sleep(0.2)

    # Type character by character — human speed
    for ch in value:
        el.send_keys(ch)
        time.sleep(random.uniform(0.08, 0.18))

    # Tab away to trigger blur/change events
    el.send_keys(Keys.TAB)
    time.sleep(0.35)

    actual = (el.get_attribute("value") or "").strip()
    print(f"  ✓ [{label}] = '{actual[:60]}'")
    return actual


def fill_and_submit_with_wire(driver, product, zip_code, city_name, cl_email):
    """
    Fill CL posting form with human keystrokes, then submit via
    Python requests POST — bypasses CL's JS that clears postal on submit.
    """
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm")))
    except TimeoutException:
        print("  ✗ postingForm not found")
        return None

    handle_captcha_if_present(driver)
    time.sleep(2)

    # ── Build values ──────────────────────────────────────────────────────────
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

    # ── Disable browser autofill ──────────────────────────────────────────────
    try:
        driver.execute_script("""
            var f = document.getElementById('postingForm');
            if (f) {
                f.setAttribute('autocomplete','off');
                f.querySelectorAll('input,textarea').forEach(function(el) {
                    el.setAttribute('autocomplete','off');
                    el.setAttribute('data-lpignore','true');
                });
            }
        """)
    except Exception:
        pass

    # ── Fill each field ───────────────────────────────────────────────────────
    _type_into_field(driver, "[name='PostingTitle']", title, "title")
    time.sleep(0.4)

    for price_sel in ["[name='price']", "[name='AskingPrice']", "[name='AskPrice']"]:
        try:
            driver.find_element(By.CSS_SELECTOR, price_sel)
            _type_into_field(driver, price_sel, price, "price")
            break
        except Exception:
            continue

    try:
        driver.find_element(By.CSS_SELECTOR, "[name='geographic_area']")
        _type_into_field(driver, "[name='geographic_area']", city_name, "city")
    except Exception:
        pass

    if zip_code:
        _type_into_field(driver, "[name='postal']", zip_code, "ZIP")
        time.sleep(0.5)

    _type_into_field(driver, "[name='PostingBody']", description, "description")
    time.sleep(0.5)

    try:
        email_el = driver.find_element(By.CSS_SELECTOR, "[name='FromEMail']")
        if not email_el.get_attribute("disabled") and not email_el.get_attribute("readOnly"):
            if cl_email:
                _type_into_field(driver, "[name='FromEMail']", cl_email, "email")
        else:
            print("  [email] Pre-filled by account (not editable)")
    except Exception:
        pass

    time.sleep(1.0)

    # ── Log pre-submit state ──────────────────────────────────────────────────
    for fname in ["PostingTitle", "PostingBody", "postal", "price", "FromEMail"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[name='{fname}']")
            val = (el.get_attribute("value") or "")[:50]
            print(f"  [pre-submit] {fname}: '{val}'")
        except Exception:
            print(f"  [pre-submit] {fname}: NOT FOUND")

    # ── SUBMIT: type postal last, then click Continue immediately ───────────────
    # Key insight from logs: region/subregion/category are server-side only —
    # they are NOT in the DOM. So requests POST always fails on ZIP.
    # Solution: use the BROWSER to submit (it has the full session).
    # Type postal as the very last action, then click Continue within 200ms
    # before CL's JS can run and clear it.
    print("  [submit] Typing postal last then clicking Continue...")

    # Re-type postal right now as absolute last action
    if zip_code:
        try:
            postal_el = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[name='postal']")))
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", postal_el)
            time.sleep(0.2)
            postal_el.click()
            postal_el.send_keys(Keys.CONTROL + "a")
            time.sleep(0.08)
            postal_el.send_keys(Keys.DELETE)
            time.sleep(0.08)
            for ch in zip_code:
                postal_el.send_keys(ch)
                time.sleep(random.uniform(0.06, 0.10))
            # Do NOT tab away — keep focus on postal, do not trigger blur
            actual = (postal_el.get_attribute("value") or "").strip()
            print(f"  [ZIP-final] typed='{actual}' (not blurred)")
        except Exception as e:
            print(f"  [ZIP-final] failed: {e}")

    # Find and click Continue button via JS — fastest possible path
    # JS click fires synchronously before React's async onChange can clear postal
    result = driver.execute_script("""
        var form = document.getElementById('postingForm');
        if (!form) return 'no-form';
        var btn = form.querySelector(
            'button.go, button.bigbutton, button[type="submit"], input[type="submit"]');
        if (!btn) return 'no-btn';
        btn.scrollIntoView({block: 'center'});
        btn.click();
        return 'clicked:' + (btn.textContent || btn.value || '').trim().substring(0, 20);
    """)
    print(f"  [submit] JS click → {result}")

    # Wait up to 25s to leave the edit page
    deadline = time.time() + 25
    while time.time() < deadline:
        cur = driver.current_url
        if "s=edit" not in cur:
            print(f"  ✅ Left edit page → {cur}")
            return cur
        time.sleep(0.4)

    # Still on edit page — check what CL says
    try:
        errs = driver.execute_script("""
            var msgs = [];
            document.querySelectorAll('.err, .error, [class*="error"]').forEach(function(el) {
                var t = (el.textContent || '').replace(/\\s+/g,' ').trim();
                if (t && t.length > 4 && t.length < 120) msgs.push(t);
            });
            return msgs;
        """) or []
        print(f"  [submit-errors] {errs[:5]}")
        # Log field state
        for fname in ["PostingTitle", "PostingBody", "postal", "price"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, f"[name='{fname}']")
                print(f"  [fail] {fname}='{(el.get_attribute('value') or '')[:40]}' "
                      f"invalid={el.get_attribute('aria-invalid')}")
            except Exception:
                print(f"  [fail] {fname}: NOT FOUND")
    except Exception:
        pass

    print("  ❌ All submit strategies failed")
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
        "chandigarh": "", "delhi": "", "mumbai": "", "bangalore": "",
        "hyderabad": "", "chennai": "", "kolkata": "", "pune": "",
        "ahmedabad": "", "jaipur": "", "lucknow": "", "surat": "",
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
        "chandigarh": "Chandigarh", "delhi": "Delhi", "mumbai": "Mumbai",
        "bangalore": "Bangalore", "hyderabad": "Hyderabad", "chennai": "Chennai",
        "kolkata": "Kolkata", "pune": "Pune",
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

    # Strategy A: requestSubmit
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

    # Strategy B: Selenium click
    if _click_first(driver, publish_selectors, "publish"):
        time.sleep(4)
        if "s=preview" not in driver.current_url:
            return True

    # Strategy C: requests POST
    try:
        form_data = driver.execute_script("""
            var form = document.getElementById('publish_bottom')
                    || document.getElementById('publish_top');
            if (!form) return null;
            var data = {};
            form.querySelectorAll('input,button').forEach(function(el) {
                if (!el.name) return;
                if (el.type === 'submit' || el.tagName === 'BUTTON') {
                    data[el.name] = el.value || el.textContent || 'Continue';
                } else {
                    data[el.name] = el.value || '';
                }
            });
            data.__action__ = form.action || '';
            return data;
        """)
        if not form_data:
            return False
        action = form_data.pop("__action__", "") or driver.current_url
        if action.startswith("/"):
            action = "https://post.craigslist.org" + action
        session = requests.Session()
        for cookie in driver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"],
                                domain=cookie.get("domain", ""))
        resp = session.post(
            action,
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": driver.current_url,
                "Origin": "https://post.craigslist.org",
            },
            allow_redirects=True,
            timeout=30,
        )
        print(f"  [publish] POST → {resp.status_code} → {resp.url}")
        if resp.status_code == 200 and "s=preview" not in resp.url:
            driver.get(resp.url)
            time.sleep(3)
            return True
    except Exception as e:
        print(f"  [publish] POST fallback failed: {e}")

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
            # Partial match
            for item in menu_items:
                txt = (item.text or '').strip().lower()
                if any(word in txt for word in target.split() if len(word) > 3):
                    driver.execute_script("arguments[0].click();", item)
                    city_clicked = True
                    print(f"  ✓ Partial match city: {item.text.strip()}")
                    break
        if not city_clicked:
            available = [m.text.strip() for m in menu_items[:8]]
            print(f"  ✗ City '{CL_CITY}' NOT FOUND in dropdown.")
            print(f"  Available cities: {available}")
            print(f"  Fix: set CL_CITY env var to one of the above (e.g. 'losangeles')")
            return False  # Hard fail — do NOT pick a wrong city
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

    # ── Fill and submit the form ───────────────────────────────────────────────
    try:
        success = fill_listing_details(driver, product)
    except Exception as e:
        print(f"  ✗ fill_listing_details crashed: {e}")
        import traceback
        traceback.print_exc()
        return False

    if not success:
        return False

    # ── Image upload step ─────────────────────────────────────────────────────
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