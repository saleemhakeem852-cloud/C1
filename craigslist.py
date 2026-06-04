"""
craigslist.py  —  CLBlast automation module for Craigslist
Anti-detection: selenium webdriver, human typing delays, 2captcha, persistent profile.
"""

import time
import json
import os
import random
import threading
import tempfile
import urllib.request
from datetime import datetime, timedelta

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

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
TWO_CAPTCHA_API_KEY = os.environ.get("TWO_CAPTCHA_KEY", "YOUR_2CAPTCHA_API_KEY")
CHROME_PROFILE_DIR  = os.path.join(os.path.expanduser("~"), ".clblast_chrome_cl")
LISTINGS_JSON       = "posted_listings.json"
CL_CITY             = os.environ.get("CL_CITY", "losangeles")

# Speed mode — 70% faster delays on Railway (set FAST_MODE=0 to disable)
IS_FAST_MODE = os.environ.get("FAST_MODE", "1") == "1"

# Detect Railway / headless cloud environment (no terminal available)
IS_RAILWAY = any(os.path.exists(p) for p in [
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/run/current-system/sw/bin/chromium"
])

# ─────────────────────────────────────────────────────────────
# CATEGORY MAPPING
# ─────────────────────────────────────────────────────────────
CATEGORY_MAPPING = {
    "antiques": (1, "antiques"),
    "appliances": (2, "appliances"),
    "art": (3, "arts & crafts"),
    "paintings": (3, "arts & crafts"),
    "art supplies": (3, "arts & crafts"),
    "atvs": (4, None), "utvs": (4, None), "snowmobiles": (4, None),
    "automotive": (5, "auto parts"), "auto parts": (5, "auto parts"),
    "tires": (6, "auto wheels & tires"),
    "planes": (7, "aviation"), "plane parts": (7, "aviation"),
    "jets": (7, "aviation"), "jet parts": (7, "aviation"),
    "helicopters": (7, "aviation"), "helicopter parts": (7, "aviation"),
    "toddlers": (8, "baby & kid stuff"), "youth": (8, "baby & kid stuff"),
    "barter": (9, "barter"),
    "bicycle parts": (10, "bicycle parts"),
    "bicycles": (11, "bicycles"),
    "boat parts": (12, "boat parts"), "boats": (13, "boats"),
    "books": (14, "books & magazines"), "novels": (14, "books & magazines"),
    "magazines": (14, "books & magazines"),
    "business": (15, "business/commercial"),
    "cars": (16, "cars & trucks"), "trucks": (16, "cars & trucks"),
    "pickup trucks": (16, "cars & trucks"), "vans": (16, "cars & trucks"),
    "suvs": (16, "cars & trucks"),
    "cds": (17, "cds / dvds / vhs"), "dvds": (17, "cds / dvds / vhs"),
    "vhs": (17, "cds / dvds / vhs"),
    "phones": (18, "cell phones"), "cell phones": (18, "cell phones"),
    "fashion": (19, "clothing & accessories"),
    "women's clothing": (19, "clothing & accessories"),
    "men's clothing": (19, "clothing & accessories"),
    "collectibles": (20, "collectibles"), "coins": (20, "collectibles"),
    "computer parts": (21, "computer parts"),
    "computers": (22, "computers"), "desktops": (22, "computers"),
    "laptops": (22, "computers"), "tablets": (22, "computers"),
    "ipads": (22, "computers"),
    "electronics": (23, "electronics"), "cameras": (23, "electronics"),
    "lawn care": (24, "farm & garden"), "farming": (24, "farm & garden"),
    "home & garden": (24, "farm & garden"),
    "free": (25, "free stuff"),
    "furniture": (26, "furniture"), "home furniture": (26, "furniture"),
    "office furniture": (26, "furniture"), "chairs": (26, "furniture"),
    "tables": (26, "furniture"), "dressers": (26, "furniture"),
    "sofas": (26, "furniture"),
    "garage": (27, "garage & moving sales"),
    "packing & moving": (27, "garage & moving sales"),
    "miscellaneous": (28, "general for sale"),
    "health": (29, "health and beauty"), "beauty": (29, "health and beauty"),
    "skin": (29, "health and beauty"),
    "heavy duty equipment": (30, "heavy equipment"),
    "household": (31, "household items"),
    "jewelry": (32, "jewelry"), "bracelets": (32, "jewelry"),
    "necklaces": (32, "jewelry"), "chains": (32, "jewelry"),
    "watches": (32, "jewelry"), "rings": (32, "jewelry"),
    "earrings": (32, "jewelry"),
    "materials": (33, "materials"),
    "motorcycle parts": (34, "motorcycle parts"),
    "motorcycles": (35, "motorcycles/scooters"),
    "instruments": (36, "musical instruments"),
    "photos": (37, "photo/video"), "videos": (37, "photo/video"),
    "rvs": (38, "rvs"),
    "sports": (39, "sporting goods"),
    "sporting goods": (39, "sporting goods"),
    "tickets": (40, "tickets"),
    "tools": (41, "tools"),
    "board games": (42, "toys & games"), "toys": (42, "toys & games"),
    "trailers": (43, "trailers"),
    "video games": (44, "video gaming"),
    "game consoles": (44, "video gaming"),
    "wanted": (45, "wanted"),
    # ── Products.json category keys (exact strings used in products.json) ─────
    "men": (19, "clothing & accessories"),
    "women": (19, "clothing & accessories"),
    "accessories": (19, "clothing & accessories"),
    "artandcollectibles": (20, "collectibles"),
    "art and collectibles": (20, "collectibles"),
    "homeandappliances": (31, "household items"),
    "home and appliances": (31, "household items"),
    "entertainment": (17, "cds / dvds / vhs"),
}


def get_category_ul_value(category_name: str):
    key = category_name.lower().strip().replace(" ", "")
    for k in CATEGORY_MAPPING:
        if k.replace(" ", "") == key:
            return CATEGORY_MAPPING[k][0]
    key_spaced = category_name.lower().strip()
    for k in CATEGORY_MAPPING:
        if k in key_spaced or key_spaced in k:
            return CATEGORY_MAPPING[k][0]
    print(f"  ⚠  Category '{category_name}' not in mapping — defaulting to 'general for sale'")
    return CATEGORY_MAPPING["miscellaneous"][0]


# ─────────────────────────────────────────────────────────────
# POSTED LISTINGS TRACKER
# ─────────────────────────────────────────────────────────────
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
        print(f"  ⚠  Could not load existing listings: {e}")


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


# ─────────────────────────────────────────────────────────────
# DRIVER FACTORY
# ─────────────────────────────────────────────────────────────
def _find_binary(names: list, fallback_paths: list) -> str | None:
    import shutil, subprocess

    for name in names:
        path = shutil.which(name)
        if path:
            return path

    for name in names:
        try:
            result = subprocess.run(["which", name], capture_output=True, text=True, timeout=3)
            p = result.stdout.strip()
            if p and os.path.exists(p):
                return p
        except Exception:
            pass

    extended = ["/usr/local/bin/" + n for n in names] + fallback_paths
    for p in extended:
        if os.path.exists(p):
            return p

    for name in names:
        try:
            result = subprocess.run(
                ["find", "/nix", "-name", name, "-type", "f"],
                capture_output=True, text=True, timeout=10
            )
            hits = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
            hits.sort(key=lambda h: (0 if "/bin/" in h else 1, "doc" in h))
            if hits:
                print(f"  [driver] Found {name} via find: {hits[0]}")
                return hits[0]
        except Exception:
            pass

    return None


def make_driver(headless: bool = False) -> webdriver.Chrome:
    import tempfile
    from selenium.webdriver.chrome.service import Service

    os.environ["SE_MANAGER_PATH"] = ""
    os.environ["WDM_SKIP_DOWNLOAD"] = "1"

    options = webdriver.ChromeOptions()
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--memory-pressure-off")
    options.add_argument("--no-zygote")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-javascript-harmony-shipping")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--shm-size=128m")
    options.add_argument("--disable-blink-features=AutomationControlled")

    fresh_profile = tempfile.mkdtemp(prefix="clblast_chrome_")
    options.add_argument(f"--user-data-dir={fresh_profile}")

    ua_pool = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    ]
    options.add_argument(f"--user-agent={random.choice(ua_pool)}")

    chromium_bin = _find_binary(
        ["chromium", "chromium-browser", "google-chrome"],
        ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]
    )
    if chromium_bin:
        print(f"  [driver] Using chromium: {chromium_bin}")
        options.binary_location = chromium_bin
    else:
        print("  [driver] WARNING: chromium binary not found")

    chromedriver_bin = _find_binary(
        ["chromedriver"],
        ["/usr/bin/chromedriver"]
    )
    if not chromedriver_bin:
        raise RuntimeError("chromedriver not found.")

    print(f"  [driver] Using chromedriver: {chromedriver_bin}")
    service = Service(
        executable_path=chromedriver_bin,
        log_output="/tmp/chromedriver.log"
    )
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"  [driver] Chrome session failed: {e}")
        try:
            with open("/tmp/chromedriver.log") as log:
                print("  [chromedriver log]", log.read()[-2000:])
        except Exception:
            pass
        raise
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver


# ─────────────────────────────────────────────────────────────
# HUMAN-LIKE HELPERS
# ─────────────────────────────────────────────────────────────
def human_delay(lo: float = 0.8, hi: float = 2.5):
    if IS_FAST_MODE:
        time.sleep(random.uniform(lo * 0.3, hi * 0.3))
    else:
        time.sleep(random.uniform(lo, hi))


def human_scroll(driver):
    try:
        scroll_amount = random.randint(100, 400)
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        time.sleep(random.uniform(0.5, 1.5))
        if random.choice([True, False]):
            driver.execute_script(f"window.scrollBy(0, {-random.randint(50, 200)});")
            time.sleep(random.uniform(0.5, 1.0))
    except Exception:
        pass


def human_mouse_movement(driver, element):
    try:
        actions = ActionChains(driver)
        offset_x = random.randint(-10, 10)
        offset_y = random.randint(-10, 10)
        actions.move_to_element_with_offset(element, offset_x, offset_y)
        actions.pause(random.uniform(0.2, 0.5))
        actions.move_by_offset(random.randint(-5, 5), random.randint(-5, 5))
        actions.pause(random.uniform(0.1, 0.3))
        actions.perform()
    except Exception:
        pass


def send_keys_slow(driver, element, text: str):
    try:
        ActionChains(driver).move_to_element(element).click().perform()
    except Exception:
        try:
            element.click()
        except Exception:
            pass
    time.sleep(random.uniform(0.5, 1.2))
    element.clear()
    time.sleep(random.uniform(0.3, 0.7))
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.05, 0.22))
    time.sleep(random.uniform(0.5, 1.0))


def safe_click(driver, element):
    human_delay(2.0, 5.0)
    if random.random() < 0.3:
        human_scroll(driver)
    human_mouse_movement(driver, element)
    try:
        ActionChains(driver).move_to_element(element).pause(random.uniform(0.3, 0.8)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    human_delay(1.0, 2.5)


# ─────────────────────────────────────────────────────────────
# CAPTCHA
# ─────────────────────────────────────────────────────────────
def solve_recaptcha_v2(driver) -> bool:
    if not CAPTCHA_SOLVER_AVAILABLE:
        if IS_RAILWAY:
            print("  CAPTCHA detected but no solver available on Railway. Skipping.")
            return False
        input("  CAPTCHA detected. Solve manually then press ENTER…")
        return True
    try:
        iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        src    = iframe.get_attribute("src")
        sitekey = [p.split("=")[1] for p in src.split("&") if "k=" in p][0]
        solver  = TwoCaptcha(TWO_CAPTCHA_API_KEY)
        print("  Sending CAPTCHA to 2captcha…")
        result  = solver.recaptcha(sitekey=sitekey, url=driver.current_url)
        token   = result["code"]
        driver.execute_script(
            "document.getElementById('g-recaptcha-response').innerHTML = arguments[0];", token)
        driver.execute_script(
            "if(typeof ___grecaptcha_cfg !== 'undefined'){"
            "  Object.values(___grecaptcha_cfg.clients).forEach(c=>{"
            "    if(c && c.oResCb) c.oResCb(arguments[0]);"
            "  })"
            "}", token
        )
        print("  CAPTCHA solved ✓")
        return True
    except Exception as e:
        print(f"  CAPTCHA solve failed: {e}")
        if IS_RAILWAY:
            print("  Railway mode: skipping manual CAPTCHA.")
            return False
        input("  Solve manually then press ENTER…")
        return True


def handle_captcha_if_present(driver):
    try:
        driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        solve_recaptcha_v2(driver)
        human_delay(1, 2)
    except NoSuchElementException:
        pass
    if "Just a moment" in driver.title:
        print("  Cloudflare — waiting…")
        time.sleep(8)
        if "Just a moment" in driver.title:
            if IS_RAILWAY:
                print("  Cloudflare not cleared on Railway. Continuing anyway.")
            else:
                input("  Solve Cloudflare manually then press ENTER…")


# ─────────────────────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────────────────────
def craigslist_login(driver, email: str, password: str) -> bool:
    driver.get("https://accounts.craigslist.org/login")
    human_delay(2, 4)
    handle_captcha_if_present(driver)

    try:
        email_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "inputEmailHandle"))
        )
        send_keys_slow(driver, email_field, email)
        human_delay()

        pw_field = driver.find_element(By.ID, "inputPassword")
        send_keys_slow(driver, pw_field, password)
        human_delay()

        btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        safe_click(driver, btn)

        WebDriverWait(driver, 15).until(EC.url_contains("craigslist.org"))
        handle_captcha_if_present(driver)
        print("Logged in to Craigslist ✓")
        return True
    except TimeoutException:
        print("Login failed.")
        return False


# ─────────────────────────────────────────────────────────────
# CLICK RELOCATION IF NEEDED
# ─────────────────────────────────────────────────────────────
def click_relocation_if_needed(driver, ad_name: str):
    try:
        btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#relocationButton"))
        )
        safe_click(driver, btn)
        local_btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#localAreaButton"))
        )
        safe_click(driver, local_btn)
        print("  Relocation handled ✓")
    except TimeoutException:
        pass


# ─────────────────────────────────────────────────────────────
# FIELD FILLERS
# ─────────────────────────────────────────────────────────────
def js_fill(driver, field_id: str, value: str):
    """Fill a field via JS with native setter + full event chain CL jQuery needs."""
    driver.execute_script("""
        var el = document.getElementById(arguments[0]);
        if (!el) return;
        el.scrollIntoView({block: 'center'});
        el.focus();
        var isTextarea = el.tagName === 'TEXTAREA';
        var proto = isTextarea
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        var setter = Object.getOwnPropertyDescriptor(proto, 'value');
        if (setter && setter.set) {
            setter.set.call(el, arguments[1]);
        } else {
            el.value = arguments[1];
        }
        ['focus','click','keydown','keypress','input','keyup','change','blur'].forEach(function(evtName) {
            var evt;
            if (evtName === 'input') {
                evt = new InputEvent('input', {bubbles: true, cancelable: true, data: arguments[1]});
            } else if (['keydown','keypress','keyup'].indexOf(evtName) > -1) {
                evt = new KeyboardEvent(evtName, {bubbles: true, cancelable: true});
            } else {
                evt = new Event(evtName, {bubbles: true, cancelable: true});
            }
            el.dispatchEvent(evt);
        });
        el.blur();
    """, field_id, value)
    time.sleep(0.2)


def _safe_fill(driver, field_id: str, value: str) -> bool:
    """
    THE definitive field filler for Craigslist on headless Railway Chromium.

    Root cause of all previous failures:
      - Keys.CONTROL + "a" (string concat) sends literal "\ue009a" characters into the
        field instead of selecting text. This corrupts the value silently.
      - ActionChains crashes on headless Linux without a display manager.
      - JS event injection alone doesn't update jQuery Validate's internal state.

    This function:
      1. Clears the field using element.clear() only (safe, no key combo needed)
      2. Types each character via element.send_keys(ch) — real browser events
      3. Sends Keys.TAB as a separate send_keys call — triggers jQuery blur handler
      4. Verifies the value landed correctly
    """
    try:
        el = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, field_id))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.3)

        # Focus the element
        try:
            el.click()
        except Exception:
            driver.execute_script("arguments[0].focus();", el)
        time.sleep(0.2)

        # Clear SAFELY — no key combos, just .clear() which always works headless
        el.clear()
        time.sleep(0.15)

        # Type character by character — generates real browser key events
        for ch in value:
            el.send_keys(ch)
            time.sleep(random.uniform(0.03, 0.07))

        # TAB away — this is what fires jQuery Validate's blur/focusout handler
        # MUST be a separate send_keys call, not concatenated with anything
        el.send_keys(Keys.TAB)
        time.sleep(0.5)

        # Verify value is correct
        actual = ""
        try:
            actual = (driver.find_element(By.ID, field_id).get_attribute("value") or "").strip()
        except Exception:
            pass

        if actual != value.strip():
            print(f"  ⚠ _safe_fill({field_id}): got '{actual[:30]}' — retrying with direct send_keys")
            try:
                el2 = driver.find_element(By.ID, field_id)
                el2.clear()
                time.sleep(0.1)
                el2.send_keys(value)
                el2.send_keys(Keys.TAB)
                time.sleep(0.3)
            except Exception as e2:
                print(f"  ⚠ _safe_fill({field_id}) retry failed: {e2}")
                return False

        print(f"  ✓ {field_id} filled ok")
        return True
    except Exception as e:
        print(f"  ⚠ _safe_fill({field_id}) failed: {e}")
        return False


def clipboard_fill(driver, field_id: str, value: str) -> bool:
    """Alias to _safe_fill — keeps the existing call sites working."""
    return _safe_fill(driver, field_id, value)


def robust_fill_zip(driver, zip_code):
    """Fill postal_code — just uses _safe_fill which is reliable."""
    result = _safe_fill(driver, "postal_code", zip_code)
    if not result:
        # Last resort: JS direct set
        driver.execute_script("""
            var el = document.getElementById('postal_code');
            if (!el) return;
            el.focus();
            el.value = arguments[0];
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            el.dispatchEvent(new Event('blur', {bubbles:true}));
        """, zip_code)
        time.sleep(0.3)
        print(f"  ✓ Zip filled via JS fallback: {zip_code}")


def fill_listing_details(driver, product: dict):
    # 1. Wait for form
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm"))
        )
    except TimeoutException:
        print(f"  ✗ postingForm never appeared. URL: {driver.current_url}")
        return

    handle_captcha_if_present(driver)
    time.sleep(2)

    # 2. Resolve values
    title = product.get("title") or product.get("name") or "Quality Item For Sale"
    description = product.get("description") or (
        f"{title} in excellent condition. A unique piece perfect for collectors and enthusiasts. "
        f"Well maintained and ready for a new home. Priced to sell. Local pickup preferred. "
        f"Message for more details or to arrange viewing."
    )
    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "")
    price = _pr if _pr and float(_pr) > 0 else "1"

    _ZIPS = {
        "losangeles": "90001", "los angeles": "90001",
        "newyork": "10001",    "new york": "10001",
        "chicago": "60601",    "houston": "77001",
        "phoenix": "85001",    "sfbay": "94102",
        "sandiego": "92101",   "seattle": "98101",
        "miami": "33101",      "dallas": "75201",
        "denver": "80201",     "atlanta": "30301",
        "boston": "02101",     "portland": "97201",
        # Extended locations
        "anchorage": "99502",
        "brookland": "72417", "jonesboro": "72417",
        "coloradosprings": "80990", "cosprings": "80990",
        "bloomfield": "06002", "hartford": "06002",
        "newark": "19702", "delaware": "19702",
        "orlando": "32827",
        "honolulu": "96820",
        "pocatello": "83204", "eastidaho": "83204",
        "bloomington": "61701",
        "indianapolis": "46220",
        "cedarrapids": "52404",
        "wichita": "67212",
        "louisville": "40210",
        "neworleans": "70117",
        "westbrook": "04092", "maine": "04092",
        "baltimore": "21222",
        "detroit": "48210",
        "minneapolis": "55440",
        "jackson": "39201",
        "stlouis": "63138",
        "ashland": "59004", "billings": "59004",
        "omaha": "68110",
        "lasvegas": "89030",
        "manchester": "03063",
        "lakewood": "08701", "jerseyshore": "08701",
        "albuquerque": "87108",
        "brooklyn": "11206", "newyork": "10001",
        "raleigh": "27604",
        "fargo": "58102",
        "columbus": "43211",
        "mustang": "73064", "oklahomacity": "73064",
        "canby": "97013",
        "philadelphia": "19019",
        "providence": "02858",
        "columbia": "29223",
        "siouxfalls": "57101",
        "nashville": "37205",
        "saltlakecity": "84118",
        "burlington": "05401", "vermont": "05401",
        "bristol": "24202",
        "charleston": "25302", "charlestonwv": "25302",
        "milwaukee": "53221",
        "sheridan": "82801",
    }
    zip_code = (product.get("zip_code") or product.get("postal_code") or "").strip()
    if not zip_code:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        zip_code = _ZIPS.get(_ck, "90001")

    cl_email = (
        os.environ.get("CL_EMAIL") or
        product.get("contact_email") or
        product.get("email") or ""
    ).strip()
    city_name = CL_CITY.replace("-", " ").title()

    # 3. Fill all fields using _safe_fill (correct key handling, headless-safe)
    print("  Filling title...")
    _safe_fill(driver, "PostingTitle", title)
    time.sleep(0.3)

    print("  Filling description...")
    _safe_fill(driver, "PostingBody", description)
    time.sleep(0.3)

    print("  Filling zip...")
    _safe_fill(driver, "postal_code", zip_code)
    time.sleep(0.5)

    # Price — try by ID, then CSS fallback
    price_filled = False
    for pid in ["AskingPrice", "AskPrice", "price", "Price", "asking_price", "AskPriceText"]:
        try:
            driver.find_element(By.ID, pid)
            _safe_fill(driver, pid, price)
            print(f"  ✓ Price: {price}")
            price_filled = True
            break
        except Exception:
            continue
    if not price_filled:
        try:
            price_inputs = driver.find_elements(By.CSS_SELECTOR,
                "input[id*='rice'], input[name*='rice'], input[id*='ask'], input[name*='ask']")
            for pi in price_inputs:
                if pi.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pi)
                    pi.click()
                    time.sleep(0.1)
                    pi.clear()
                    for ch in price:
                        pi.send_keys(ch)
                        time.sleep(0.04)
                    pi.send_keys(Keys.TAB)
                    driver.execute_script(
                        "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));"
                        "arguments[0].dispatchEvent(new Event('blur',{bubbles:true}));", pi)
                    print(f"  ✓ Price filled via CSS fallback: {price}")
                    price_filled = True
                    break
        except Exception as pe:
            print(f"  ⚠ Price CSS fallback failed: {pe}")
    if not price_filled:
        print("  ⚠ Price field not found — posting without price")

    # Email
    email_filled = False
    try:
        ef = WebDriverWait(driver, 4).until(
            EC.presence_of_element_located((By.ID, "FromEMail"))
        )
        cur_val = (ef.get_attribute("value") or "").strip()
        if cur_val:
            print(f"  ✓ Email already in field: {cur_val}")
            email_filled = True
        elif cl_email:
            _safe_fill(driver, "FromEMail", cl_email)
            print(f"  ✓ Email: {cl_email}")
            email_filled = True
        else:
            print("  ✗ CL_EMAIL env var not set AND field is empty — will fail")
    except TimeoutException:
        print("  [info] FromEMail absent — CL using session email (OK if logged in)")
        email_filled = True

    # Condition dropdown
    try:
        cond = Select(WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.ID, "condition"))
        ))
        cond.select_by_visible_text(product.get("condition", "new"))
    except Exception:
        pass

    time.sleep(1)

    # Pre-submit verification
    time.sleep(0.5)
    try:
        zip_val = (driver.find_element(By.ID, "postal_code").get_attribute("value") or "").strip()
        if not zip_val or zip_val != zip_code:
            print(f"  ✗ postal_code mismatch (got '{zip_val}') — re-filling")
            _safe_fill(driver, "postal_code", zip_code)
            time.sleep(0.8)
        else:
            print(f"  ✓ postal_code confirmed: '{zip_val}'")
    except Exception:
        pass
    try:
        title_val = (driver.find_element(By.ID, "PostingTitle").get_attribute("value") or "").strip()
        if not title_val:
            print(f"  ✗ PostingTitle is EMPTY — re-filling")
            _safe_fill(driver, "PostingTitle", title)
            time.sleep(0.5)
        else:
            print(f"  ✓ title confirmed: '{title_val[:40]}...'")
    except Exception:
        pass
    try:
        body_val = (driver.find_element(By.ID, "PostingBody").get_attribute("value") or "").strip()
        if not body_val:
            print(f"  ✗ PostingBody is EMPTY — re-filling")
            _safe_fill(driver, "PostingBody", description)
            time.sleep(0.5)
        else:
            print(f"  ✓ description confirmed ({len(body_val)} chars)")
    except Exception:
        pass

    time.sleep(2.0)

    # 4. Submit
    url_before = driver.current_url

    for attempt in range(4):
        if attempt > 0:
            if "s=edit" not in driver.current_url:
                break
            print(f"  ⚠ Still on edit page — retry {attempt}/3, re-filling fields...")
            time.sleep(1.0)
            try:
                if not (driver.find_element(By.ID, "PostingTitle").get_attribute("value") or "").strip():
                    _safe_fill(driver, "PostingTitle", title)
                    time.sleep(0.3)
            except Exception: pass
            try:
                if not (driver.find_element(By.ID, "PostingBody").get_attribute("value") or "").strip():
                    _safe_fill(driver, "PostingBody", description)
                    time.sleep(0.3)
            except Exception: pass
            try:
                if not (driver.find_element(By.ID, "postal_code").get_attribute("value") or "").strip():
                    _safe_fill(driver, "postal_code", zip_code)
                    time.sleep(0.3)
            except Exception: pass

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5)

        clicked = False
        for sel in [
            (By.XPATH, '//*[@id="postingForm"]/button'),
            (By.XPATH, '//form[@id="postingForm"]//button[@type="submit"]'),
            (By.XPATH, '//button[contains(@class,"go")]'),
            (By.CSS_SELECTOR, "button.go"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
        ]:
            try:
                btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable(sel))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.3)
                btn.click()
                print(f"  ✓ Submit clicked (attempt {attempt + 1})")
                clicked = True
                time.sleep(5)
                break
            except Exception:
                continue

        if not clicked:
            print("  ✗ No submit button found")
            return

        if "s=edit" not in driver.current_url:
            break

    if "s=edit" in driver.current_url:
        print("  ✗ Still on edit page after retries — giving up")

    # 5. Check for validation errors
    try:
        errs = [e.text.strip() for e in driver.find_elements(
            By.CSS_SELECTOR, ".notices li, .err, .error, span.notice"
        ) if e.text.strip() and len(e.text.strip()) > 5]
        if errs:
            print("  [validation errors]:")
            for et in set(errs):
                print(f"    → {et[:100]}")
    except Exception:
        pass

    # 6. Wait for URL to change
    try:
        WebDriverWait(driver, 12).until(lambda d: d.current_url != url_before)
        print(f"  ✓ Navigated to: {driver.current_url}")
    except TimeoutException:
        print(f"  ⚠ Still on edit page after continue: {driver.current_url}")
        print("  ⚠ CL validation blocked submit — check errors above")


# ─────────────────────────────────────────────────────────────
# PHOTO UPLOAD
# ─────────────────────────────────────────────────────────────
def upload_photos(driver, product: dict):
    photo_paths = product.get("photo_paths", []) or product.get("images", [])
    if not photo_paths:
        print("  No photos to upload.")
        return

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
                print(f"  Could not download photo {p}: {e}")
        elif isinstance(p, str) and os.path.isfile(p):
            valid.append(p)

    if not valid:
        print("  No valid photos to upload.")
        return

    human_delay(4, 6)
    try:
        add_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "add_photos_button"))
        )
        safe_click(driver, add_btn)

        fi = driver.find_element(By.ID, "fileInput")
        for path in valid:
            fi.send_keys(os.path.abspath(path))
            human_delay(1.5, 3)
        print(f"  Uploaded {len(valid)} photo(s) ✓")

        human_delay(8, 12)
        done = driver.find_element(By.ID, "done_with_images_button")
        safe_click(driver, done)
    except (TimeoutException, NoSuchElementException) as e:
        print(f"  ⚠  Photo upload issue: {e}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# PUBLISH
# ─────────────────────────────────────────────────────────────
def publish_listing(driver, ad_name: str, product: dict) -> bool:
    handle_captcha_if_present(driver)
    human_delay(4, 6)

    try:
        pub = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "publish_button"))
        )
        safe_click(driver, pub)
        human_delay(5, 8)
        handle_captcha_if_present(driver)

        listing_url = driver.current_url
        print(f"  Published → {listing_url}")
        posted_listings[ad_name] = {
            "url": listing_url,
            "post_time": datetime.now(),
            "visitors": 0,
            "platform": "Craigslist",
        }
        _save_listings()
        return True
    except TimeoutException:
        print(f"  ⚠  Publish button not found for '{ad_name}'.")
        return False


# ─────────────────────────────────────────────────────────────
# POST PRODUCT
# ─────────────────────────────────────────────────────────────
def post_product(driver, ad_name: str, product: dict) -> bool:
    product_title = product.get("title") or product.get("name", "No Title")

    post_url = "https://post.craigslist.org/c/sss"
    print(f"  Navigating to: {post_url}")
    driver.get(post_url)
    human_delay(4, 7)
    handle_captcha_if_present(driver)

    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        print(f"  ✗ Page timed out for '{product_title}'. URL: {driver.current_url}")
        return False

    print(f"  Page title: {driver.title}")
    print(f"  Current URL: {driver.current_url}")

    if "login" in driver.current_url.lower() or "accounts.craigslist" in driver.current_url.lower():
        print(f"  ✗ Session expired, redirected to login.")
        return False

    # 1. City / Location selection
    try:
        city_button = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "span#ui-id-1-button"))
        )
        driver.execute_script("arguments[0].click();", city_button)
        human_delay(2, 3)

        menu_items = WebDriverWait(driver, 5).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "ul#ui-id-1-menu li"))
        )

        city_clicked = False
        target_city_normalized = CL_CITY.lower().replace(" ", "").replace("-", "").strip()

        for item in menu_items:
            item_text = item.text.strip() or item.get_attribute("textContent").strip()
            item_text_normalized = item_text.lower().replace(" ", "").replace("-", "").strip()
            if target_city_normalized in item_text_normalized or item_text_normalized in target_city_normalized:
                driver.execute_script("arguments[0].click();", item)
                city_clicked = True
                print(f"  ✓ Selected city: {item_text}")
                break

        if not city_clicked and menu_items:
            driver.execute_script("arguments[0].click();", menu_items[0])
            fallback_text = menu_items[0].text.strip() or menu_items[0].get_attribute("textContent").strip()
            print(f"  ⚠ Target city '{CL_CITY}' not found. Selected fallback: {fallback_text}")

        human_delay(2, 3)

        try:
            select_el = driver.find_element(By.CSS_SELECTOR, "select#ui-id-1")
            driver.execute_script(
                "arguments[0].value = arguments[1];",
                select_el,
                CL_CITY.lower().replace(" ", "")
            )
        except Exception:
            pass

        continue_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']"))
        )
        driver.execute_script("arguments[0].click();", continue_btn)
        print("  ✓ Submitted city selection")
        try:
            WebDriverWait(driver, 12).until(
                lambda d: "s=area" not in d.current_url
            )
            print(f"  ✓ Left area page → {driver.current_url}")
        except TimeoutException:
            print(f"  ⚠ Still on area page after 12s wait")
        handle_captcha_if_present(driver)
    except Exception as e:
        print(f"  City selection error: {e}")

    # 2. Wait to leave ?s=area
    print(f"  Waiting for post-type page... current URL: {driver.current_url}")
    if "s=area" in driver.current_url:
        print("  Still on area page, retrying city continue...")
        try:
            retry_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']"))
            )
            driver.execute_script("arguments[0].click();", retry_btn)
            try:
                WebDriverWait(driver, 10).until(lambda d: "s=area" not in d.current_url)
            except TimeoutException:
                pass
        except Exception as e:
            print(f"  ✗ City retry failed: {e}")
            return False

    if "s=area" in driver.current_url:
        print(f"  ✗ Still on area page after retry, giving up")
        return False
    print(f"  ✓ Left area → {driver.current_url}")

    handle_captcha_if_present(driver)
    human_delay(2, 4)

    # 3. Post type selection
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "input[value='fso']") or
                      d.find_elements(By.CSS_SELECTOR, "input[value='fs']") or
                      d.find_elements(By.CSS_SELECTOR, "input[type='radio']")
        )
        print(f"  ✓ Post type page loaded")
    except TimeoutException:
        print(f"  ✗ Post type radio buttons never appeared.")
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
        for tag in ["label", "li", "a"]:
            elements = driver.find_elements(By.TAG_NAME, tag)
            for el in elements:
                try:
                    txt = el.text.lower().strip()
                    if "sale by owner" in txt or ("for sale" in txt and len(txt) < 40):
                        driver.execute_script("arguments[0].click();", el)
                        fso_clicked = True
                        print(f"  ✓ Selected post type via <{tag}>: '{el.text.strip()}'")
                        break
                except Exception:
                    pass
            if fso_clicked:
                break

    if not fso_clicked:
        print(f"  ✗ Could not find 'for sale by owner'.")
        return False

    human_delay(3, 5)
    handle_captcha_if_present(driver)

    # 4. Category selection
    cat_clicked = False
    mapped_label = CATEGORY_MAPPING.get(product.get("category", "").lower().strip(), (None, ""))[1]
    if not mapped_label:
        mapped_label = product.get("category", "")

    print(f"  Target category label: {mapped_label}")

    if mapped_label:
        try:
            target_lower = mapped_label.lower().strip()
            xpath = f"//label[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{target_lower}')]"
            label_el = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            driver.execute_script("arguments[0].click();", label_el)
            cat_clicked = True
            print(f"  ✓ Selected category via label XPath: '{mapped_label}'")
        except Exception as e:
            print(f"  Category lookup via label failed: {e}")

    if not cat_clicked:
        try:
            ul_value = get_category_ul_value(product.get("category", ""))
            inp = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f"input[type='radio'][value='{ul_value}']"))
            )
            driver.execute_script("arguments[0].click();", inp)
            cat_clicked = True
            print(f"  ✓ Selected category via radio value={ul_value}")
        except Exception as e:
            print(f"  Category selection via radio value failed: {e}")

    if not cat_clicked:
        try:
            first_label = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "label.radio-option, label"))
            )
            driver.execute_script("arguments[0].click();", first_label)
            cat_clicked = True
            print(f"  ✓ Selected first category as fallback: '{first_label.text.strip()}'")
        except Exception as e:
            print(f"  Category selection via first label fallback failed: {e}")

    if not cat_clicked:
        print(f"  ✗ Could not select category.")
        return False

    human_delay(2, 3)
    try:
        continue_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']"))
        )
        driver.execute_script("arguments[0].click();", continue_btn)
        print("  ✓ Clicked category continue button")
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.ID, "postingForm"))
            )
            time.sleep(1.5)
            print(f"  ✓ postingForm visible after category selection")
        except TimeoutException:
            time.sleep(3)
        print(f"  Current URL after category continue: {driver.current_url}")
        handle_captcha_if_present(driver)
    except TimeoutException:
        print("  No continue button found on category page; hoping it autosubmitted.")
        human_delay(2, 3)

    click_relocation_if_needed(driver, ad_name)
    try:
        fill_listing_details(driver, product)
    except Exception as e:
        print(f"  ✗ fill_listing_details crashed: {e}")
        return False

    reached_photo_step = False
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.ID, "add_photos_button") or
                      d.find_elements(By.ID, "publish_button") or
                      "s=images" in d.current_url or
                      "s=preview" in d.current_url
        )
        reached_photo_step = True
        print(f"  ✓ Reached next step: {driver.current_url}")
    except TimeoutException:
        print(f"  ⚠ Did not reach photo step. Still at: {driver.current_url}")

    if not reached_photo_step or "s=edit" in driver.current_url:
        print(f"  ✗ Skipping photo upload — still on edit/form page. Aborting post.")
        return False

    upload_photos(driver, product)
    return publish_listing(driver, ad_name, product)


# ─────────────────────────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────────────────────────
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
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#views_count"))
                )
                count = int("".join(filter(str.isdigit, views_el.text)) or "0")
                posted_listings[ad_name]["visitors"] = count
                print(f"  {ad_name}: {count} views")
            except Exception as e:
                print(f"  ⚠  Analytics error for {ad_name}: {e}")
            finally:
                if tmp:
                    tmp.quit()
            _update_ad_status(ad_name)
        _save_listings()
        time.sleep(300)


def _update_ad_status(ad_name: str):
    listing = posted_listings.get(ad_name)
    if not listing:
        return
    pt = listing["post_time"]
    if not isinstance(pt, datetime):
        try:
            pt = datetime.fromisoformat(str(pt))
        except Exception:
            return
    if listing.get("url"):
        print(f"  {ad_name} → active ✓")
    else:
        print(f"  {ad_name} → inactive (no URL)")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    global CL_CITY
    email    = os.environ.get("CL_EMAIL")    or input("Enter Craigslist email: ").strip()
    password = os.environ.get("CL_PASSWORD") or input("Enter Craigslist password: ").strip()
    CL_CITY  = os.environ.get("CL_CITY", CL_CITY)

    _load_existing_listings()

    driver = make_driver()

    if not craigslist_login(driver, email, password):
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
            ok = False
        print("  ✓ Posted" if ok else "  ✗ Failed")
        time.sleep(3)

    print("\nAll Craigslist products processed.")
    driver.quit()


if __name__ == "__main__":
    main()