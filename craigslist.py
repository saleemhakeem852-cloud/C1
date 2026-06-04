"""
craigslist.py  —  CLBlast automation module for Craigslist

FIXES applied based on form dump diagnostics:
  1. postal (name=postal) is CLEARED by CL's JS after TAB/blur events
     → Fill via JS setValue trick + dispatch 'input'+'change' events, fill LAST before submit
  2. FromEMail has no ID, no session auto-fill → must set via JS + CL_EMAIL env var
  3. geographic_area field exists and is required → fill with city name
  4. Price is type=number → send as plain string without TAB to avoid truncation
  5. Fill order: title → body → price → email → geographic_area → postal (LAST)
  6. Submit: click button only (no form.submit() — that bypasses CL's JS validators)
  7. Never send TAB after postal — TAB triggers CL's blur handler that clears the field
"""

import time
import json
import os
import random
import threading
import tempfile
import urllib.request
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

def _find_binary(names, fallback_paths):
    import shutil, subprocess
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

def make_driver(headless=False):
    from selenium.webdriver.chrome.service import Service
    os.environ["SE_MANAGER_PATH"] = ""
    os.environ["WDM_SKIP_DOWNLOAD"] = "1"
    options = webdriver.ChromeOptions()
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    for arg in [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-software-rasterizer", "--headless=new", "--window-size=1280,800",
        "--memory-pressure-off", "--no-zygote", "--disable-setuid-sandbox",
        "--disable-extensions", "--disable-plugins",
        "--disable-background-networking", "--disable-default-apps",
        "--disable-sync", "--disable-translate", "--mute-audio",
        "--no-first-run", "--no-default-browser-check", "--shm-size=128m",
        "--disable-blink-features=AutomationControlled",
    ]:
        options.add_argument(arg)
    fresh_profile = tempfile.mkdtemp(prefix="clblast_chrome_")
    options.add_argument(f"--user-data-dir={fresh_profile}")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    chromium_bin = _find_binary(
        ["chromium", "chromium-browser", "google-chrome"],
        ["/usr/bin/chromium", "/usr/bin/chromium-browser"])
    if chromium_bin:
        print(f"  [driver] Using chromium: {chromium_bin}")
        options.binary_location = chromium_bin
    chromedriver_bin = _find_binary(["chromedriver"], ["/usr/bin/chromedriver"])
    if not chromedriver_bin:
        raise RuntimeError("chromedriver not found")
    print(f"  [driver] Using chromedriver: {chromedriver_bin}")
    service = Service(executable_path=chromedriver_bin, log_output="/tmp/chromedriver.log")
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver

def human_delay(lo=0.8, hi=2.5):
    time.sleep(random.uniform(lo * 0.3 if IS_FAST_MODE else lo,
                              hi * 0.3 if IS_FAST_MODE else hi))

def human_scroll(driver):
    try:
        driver.execute_script(f"window.scrollBy(0, {random.randint(100, 400)});")
        time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass

def human_mouse_movement(driver, element):
    try:
        ActionChains(driver).move_to_element_with_offset(
            element, random.randint(-5, 5), random.randint(-5, 5)
        ).pause(random.uniform(0.1, 0.3)).perform()
    except Exception:
        pass

def send_keys_slow(driver, element, text):
    try:
        ActionChains(driver).move_to_element(element).click().perform()
    except Exception:
        try: element.click()
        except Exception: pass
    time.sleep(random.uniform(0.3, 0.7))
    element.clear()
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.05, 0.15))

def safe_click(driver, element):
    human_delay(2.0, 5.0)
    if random.random() < 0.3:
        human_scroll(driver)
    human_mouse_movement(driver, element)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.3, 0.8)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    human_delay(1.0, 2.5)

def solve_recaptcha_v2(driver):
    if not CAPTCHA_SOLVER_AVAILABLE:
        if IS_RAILWAY:
            print("  CAPTCHA detected — no solver. Skipping.")
            return False
        input("  CAPTCHA: solve manually then ENTER…")
        return True
    try:
        iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        sitekey = [p.split("=")[1] for p in iframe.get_attribute("src").split("&") if "k=" in p][0]
        solver = TwoCaptcha(TWO_CAPTCHA_API_KEY)
        result = solver.recaptcha(sitekey=sitekey, url=driver.current_url)
        driver.execute_script(
            "document.getElementById('g-recaptcha-response').innerHTML=arguments[0];", result["code"])
        print("  CAPTCHA solved ✓")
        return True
    except Exception as e:
        print(f"  CAPTCHA solve failed: {e}")
        return False

def handle_captcha_if_present(driver):
    try:
        driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        solve_recaptcha_v2(driver)
        human_delay(1, 2)
    except NoSuchElementException:
        pass
    if "Just a moment" in driver.title:
        print("  Cloudflare — waiting 8s…")
        time.sleep(8)

def craigslist_login(driver, email, password):
    driver.get("https://accounts.craigslist.org/login")
    human_delay(2, 4)
    handle_captcha_if_present(driver)
    try:
        ef = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "inputEmailHandle")))
        send_keys_slow(driver, ef, email)
        human_delay()
        pf = driver.find_element(By.ID, "inputPassword")
        send_keys_slow(driver, pf, password)
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


# ─────────────────────────────────────────────────────────────────────────────
# NATIVE SETTER — the only way to update CL's internal JS state
#
# Problem: CL's page JS overrides the .value property setter on input elements
# to track state internally. When we do el.value = x, it goes into the DOM
# but CL's tracker never sees it. At submit time CL reads from its tracker,
# not the DOM — so it sees empty fields even though the DOM shows values.
#
# Fix: Grab the ORIGINAL native setter from HTMLInputElement.prototype
# BEFORE CL overrides it, call that setter, then fire 'input'+'change' events.
# CL's event listeners update their internal model when these events fire.
# ─────────────────────────────────────────────────────────────────────────────

_JS_NATIVE_SET = """
(function(selector, value) {
    var el = document.querySelector(selector);
    if (!el) return 'NOT_FOUND:' + selector;
    el.focus();
    var proto = el.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    var nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (nativeSetter && nativeSetter.set) {
        nativeSetter.set.call(el, value);
    } else {
        el.value = value;
    }
    el.dispatchEvent(new Event('input',   {bubbles: true, cancelable: true}));
    el.dispatchEvent(new Event('change',  {bubbles: true, cancelable: true}));
    el.dispatchEvent(new KeyboardEvent('keyup',   {bubbles: true}));
    el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true}));
    return el.value;
})(arguments[0], arguments[1]);
"""

def _native_set(driver, selector: str, value: str, label: str = "field") -> bool:
    """
    Set field value using native prototype setter + fire all CL event listeners.
    Forces CL's internal JS state model to update.
    """
    try:
        WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
        result = driver.execute_script(_JS_NATIVE_SET, selector, str(value))
        if result and str(result).startswith("NOT_FOUND"):
            print(f"  ✗ {label}: element not found '{selector}'")
            return False
        actual = str(result or "").strip()
        if actual:
            print(f"  ✓ {label} = '{actual[:60]}'")
            return True
        print(f"  ✗ {label} empty after native set")
        return False
    except Exception as e:
        print(f"  ✗ _native_set({label}) error: {e}")
        return False


def _type_into_field(driver, selector: str, value: str, label: str,
                     send_tab: bool = False) -> bool:
    """
    Click, clear, type char-by-char via real key events.
    Used for title/body. send_tab=False for postal.
    Falls back to _native_set if keys don't register.
    """
    try:
        el = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.15)
        el.click()
        time.sleep(0.1)
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
        time.sleep(0.1)
        for ch in str(value):
            el.send_keys(ch)
            time.sleep(random.uniform(0.03, 0.07))
        if send_tab:
            el.send_keys(Keys.TAB)
            time.sleep(0.25)
        actual = (el.get_attribute("value") or "").strip()
        if actual:
            print(f"  ✓ {label} = '{actual[:60]}'")
            return True
        return _native_set(driver, selector, value, label)
    except Exception as e:
        print(f"  ✗ _type_into_field({label}): {e}")
        return _native_set(driver, selector, value, label)


def _fill_field(driver, selector: str, value: str, label: str,
                use_keys: bool = False, send_tab: bool = False) -> bool:
    if use_keys:
        ok = _type_into_field(driver, selector, value, label, send_tab=send_tab)
        if not ok:
            ok = _native_set(driver, selector, value, label)
    else:
        ok = _native_set(driver, selector, value, label)
    return ok


# _fill_by_keys kept as alias for backward compat in submit loop
def _fill_by_keys(driver, selector: str, value: str, label: str = "field",
                  send_tab: bool = True) -> bool:
    return _fill_field(driver, selector, value, label,
                       use_keys=True, send_tab=send_tab)


# ─────────────────────────────────────────────────────────────────────────────
# FILL LISTING DETAILS  — native setter version
# Fill order: title → body → price → email → geographic_area → postal (LAST)
# ─────────────────────────────────────────────────────────────────────────────
def fill_listing_details(driver, product: dict):
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm")))
    except TimeoutException:
        print(f"  ✗ postingForm never appeared. URL: {driver.current_url}")
        return

    handle_captcha_if_present(driver)
    time.sleep(2.5)   # let CL's JS fully initialize

    # ── Values ────────────────────────────────────────────────────────────
    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    description = (product.get("description") or (
        f"{title} in excellent condition. A unique piece perfect for collectors and enthusiasts. "
        f"Well maintained and ready for a new home. Priced to sell. Local pickup preferred. "
        f"Message for more details or to arrange viewing.")).strip()

    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "")
    try:
        price = str(float(_pr)) if _pr and float(_pr) > 0 else "1"
        if price.endswith(".0"):
            price = price[:-2]
    except Exception:
        price = "1"

    _ZIPS = {
        "losangeles": "90001", "los angeles": "90001", "newyork": "10001",
        "new york": "10001", "chicago": "60601", "houston": "77001",
        "phoenix": "85001", "sfbay": "94102", "sandiego": "92101",
        "seattle": "98101", "miami": "33101", "dallas": "75201",
        "denver": "80201", "atlanta": "30301", "boston": "02101",
        "portland": "97201", "anchorage": "99502", "orlando": "32827",
        "honolulu": "96820", "indianapolis": "46220", "wichita": "67212",
        "louisville": "40210", "neworleans": "70117", "baltimore": "21222",
        "detroit": "48210", "minneapolis": "55440", "stlouis": "63138",
        "omaha": "68110", "lasvegas": "89030", "albuquerque": "87108",
        "brooklyn": "11206", "raleigh": "27604", "fargo": "58102",
        "columbus": "43211", "philadelphia": "19019", "nashville": "37205",
        "saltlakecity": "84118", "milwaukee": "53221",
    }
    zip_code = (product.get("zip_code") or product.get("postal_code") or "").strip()
    if not zip_code:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        zip_code = _ZIPS.get(_ck, "90001")

    _CITY_NAMES = {
        "losangeles": "Los Angeles", "newyork": "New York", "chicago": "Chicago",
        "houston": "Houston", "phoenix": "Phoenix", "sfbay": "San Francisco",
        "sandiego": "San Diego", "seattle": "Seattle", "miami": "Miami",
        "dallas": "Dallas", "denver": "Denver", "atlanta": "Atlanta",
        "boston": "Boston", "portland": "Portland",
    }
    _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
    city_name = _CITY_NAMES.get(_ck, CL_CITY.title())

    cl_email = (os.environ.get("CL_EMAIL") or
                product.get("contact_email") or product.get("email") or "").strip()

    # ── 1. PostingTitle — keys + native fallback, TAB ok ─────────────────
    print("  Filling title...")
    _fill_field(driver, "[name='PostingTitle']", title,
                label="PostingTitle", use_keys=True, send_tab=True)

    # ── 2. PostingBody — keys + native fallback, TAB ok ──────────────────
    print("  Filling description...")
    _fill_field(driver, "[name='PostingBody']", description,
                label="PostingBody", use_keys=True, send_tab=True)

    # ── 3. Price — native set ─────────────────────────────────────────────
    print("  Filling price...")
    price_filled = False
    for price_sel in ["[name='price']", "[name='AskingPrice']", "[name='AskPrice']",
                      "#AskingPrice", "#AskPrice", "#price"]:
        try:
            driver.find_element(By.CSS_SELECTOR, price_sel)
            if _native_set(driver, price_sel, price, label="price"):
                price_filled = True
                break
        except Exception:
            continue
    if not price_filled:
        try:
            price_inputs = driver.find_elements(By.CSS_SELECTOR,
                "input[type='number'], input[id*='rice'], input[name*='rice']")
            for pi in price_inputs:
                if pi.is_displayed():
                    sel = f"input[name='{pi.get_attribute('name')}']" if pi.get_attribute('name') else None
                    if sel and _native_set(driver, sel, price, label="price-fallback"):
                        price_filled = True
                        break
        except Exception as pe:
            print(f"  ⚠ Price fallback: {pe}")

    # ── 4. FromEMail — native set (no ID, must use name=) ─────────────────
    print("  Filling email...")
    try:
        ef = driver.find_element(By.CSS_SELECTOR, "[name='FromEMail']")
        cur_val = (ef.get_attribute("value") or "").strip()
        if cur_val:
            print(f"  ✓ FromEMail already set: {cur_val}")
        elif cl_email:
            _native_set(driver, "[name='FromEMail']", cl_email, label="FromEMail")
        else:
            print("  ⚠ CL_EMAIL not set — FromEMail will be empty")
    except NoSuchElementException:
        print("  [info] FromEMail not on this form")

    # ── 5. geographic_area — native set ───────────────────────────────────
    print("  Filling geographic_area...")
    try:
        ga_el = driver.find_element(By.CSS_SELECTOR, "[name='geographic_area']")
        ga_val = (ga_el.get_attribute("value") or "").strip()
        if not ga_val:
            _native_set(driver, "[name='geographic_area']", city_name, label="geographic_area")
        else:
            print(f"  ✓ geographic_area already: {ga_val}")
    except NoSuchElementException:
        print("  [info] geographic_area not on this form")

    # ── 6. Condition ───────────────────────────────────────────────────────
    try:
        cond_val = product.get("condition", "")
        if cond_val:
            cond = Select(driver.find_element(By.NAME, "condition"))
            try:
                cond.select_by_visible_text(cond_val)
            except Exception:
                pass
    except Exception:
        pass

    time.sleep(0.5)

    # ── 7. postal — native set, ABSOLUTE LAST, NO TAB ─────────────────────
    # CL's JS wipes postal on blur from any other field.
    # Fill it dead-last with native setter, no TAB, no other field touches after.
    print("  Filling postal (native, LAST)...")
    _native_set(driver, "[name='postal']", zip_code, label="postal")
    time.sleep(0.4)

    # ── Pre-submit verification ────────────────────────────────────────────
    print("  [pre-submit check]")
    for fname, fval in [
        ("PostingTitle", title),
        ("PostingBody",  description),
        ("postal",       zip_code),
        ("FromEMail",    cl_email),
    ]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[name='{fname}']")
            actual = (el.get_attribute("value") or "").strip()
            if actual:
                print(f"    ✓ {fname}: '{actual[:50]}'")
            else:
                print(f"    ✗ {fname} EMPTY — emergency fill")
                if fval:
                    _native_set(driver, f"[name='{fname}']", fval, label=fname)
        except Exception:
            pass

    # Re-set postal one last time — must be after ALL other fields
    _native_set(driver, "[name='postal']", zip_code, label="postal-prefinal")
    time.sleep(0.3)

    url_before = driver.current_url

    # ── Submit loop ────────────────────────────────────────────────────────
    for attempt in range(4):
        if attempt > 0:
            if "s=edit" not in driver.current_url:
                break
            print(f"  ⚠ Still on edit page — retry {attempt}/3")
            time.sleep(1.0)

            # Re-fill any cleared fields
            for fname, fval in [
                ("PostingTitle",    title),
                ("PostingBody",     description),
                ("FromEMail",       cl_email),
                ("geographic_area", city_name),
            ]:
                if not fval:
                    continue
                try:
                    cur = (driver.find_element(By.CSS_SELECTOR, f"[name='{fname}']")
                           .get_attribute("value") or "").strip()
                    if not cur:
                        print(f"    ↻ {fname} cleared — re-filling")
                        if fname in ("PostingTitle", "PostingBody"):
                            _fill_field(driver, f"[name='{fname}']", fval,
                                        label=fname, use_keys=True, send_tab=True)
                        else:
                            _native_set(driver, f"[name='{fname}']", fval, label=fname)
                except Exception:
                    pass

            # postal always last always native
            _native_set(driver, "[name='postal']", zip_code, label="postal-retry")
            time.sleep(0.3)

        # Set postal right before the click
        _native_set(driver, "[name='postal']", zip_code, label="postal-preclick")
        time.sleep(0.1)

        clicked = False
        for sel in [
            (By.CSS_SELECTOR, "button.go.big-button.submit-button"),
            (By.CSS_SELECTOR, "button.go.submit-button"),
            (By.CSS_SELECTOR, "button.submit-button"),
            (By.XPATH, '//*[@id="postingForm"]/button'),
            (By.XPATH, '//form[@id="postingForm"]//button[@type="submit"]'),
            (By.CSS_SELECTOR, "button.go"),
            (By.CSS_SELECTOR, "button[type='submit']"),
        ]:
            try:
                btn = WebDriverWait(driver, 4).until(EC.element_to_be_clickable(sel))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.2)
                btn.click()
                print(f"  ✓ Submit clicked (attempt {attempt+1})")
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

    # ── Diagnostics on failure ─────────────────────────────────────────────
    if "s=edit" in driver.current_url:
        print("  ✗ Still on edit page after all retries — giving up")
        try:
            form_dump = driver.execute_script("""
                var form = document.getElementById('postingForm');
                if (!form) return 'NO FORM';
                var out = '';
                form.querySelectorAll('input,textarea,select').forEach(function(f) {
                    if (f.name) out += f.tagName+'#'+(f.id||'?')+' name='+f.name+
                        ' type='+(f.type||'?')+' required='+(f.required||'')+
                        ' val=['+(f.value||'').substring(0,40)+']\\n';
                });
                return out;
            """)
            print("  [form fields dump]:\n" + form_dump)
        except Exception:
            pass

    try:
        errs = [e.text.strip() for e in driver.find_elements(
            By.CSS_SELECTOR, ".notices li, .err, .error, span.notice, .warning"
        ) if e.text.strip() and len(e.text.strip()) > 3]
        if errs:
            print("  [validation errors]:")
            for et in sorted(set(errs)):
                print(f"    → {et[:120]}")
    except Exception:
        pass

    try:
        WebDriverWait(driver, 12).until(lambda d: d.current_url != url_before)
        print(f"  ✓ Navigated to: {driver.current_url}")
    except TimeoutException:
        print(f"  ⚠ Still on edit page after continue: {driver.current_url}")
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm")))
    except TimeoutException:
        print(f"  ✗ postingForm never appeared. URL: {driver.current_url}")
        return

    handle_captcha_if_present(driver)
    time.sleep(2)   # let all CL JS finish rendering

    # ── Resolve values ───────────────────────────────────────────────────────
    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    description = (product.get("description") or (
        f"{title} in excellent condition. A unique piece perfect for collectors and enthusiasts. "
        f"Well maintained and ready for a new home. Priced to sell. Local pickup preferred. "
        f"Message for more details or to arrange viewing.")).strip()

    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "")
    try:
        price = str(float(_pr)) if _pr and float(_pr) > 0 else "1"
        # CL type=number field: send as integer string if .0
        if price.endswith(".0"):
            price = price[:-2]
    except Exception:
        price = "1"

    _ZIPS = {
        "losangeles": "90001", "los angeles": "90001", "newyork": "10001",
        "new york": "10001", "chicago": "60601", "houston": "77001",
        "phoenix": "85001", "sfbay": "94102", "sandiego": "92101",
        "seattle": "98101", "miami": "33101", "dallas": "75201",
        "denver": "80201", "atlanta": "30301", "boston": "02101",
        "portland": "97201", "anchorage": "99502", "orlando": "32827",
        "honolulu": "96820", "indianapolis": "46220", "wichita": "67212",
        "louisville": "40210", "neworleans": "70117", "baltimore": "21222",
        "detroit": "48210", "minneapolis": "55440", "stlouis": "63138",
        "omaha": "68110", "lasvegas": "89030", "albuquerque": "87108",
        "brooklyn": "11206", "raleigh": "27604", "fargo": "58102",
        "columbus": "43211", "philadelphia": "19019", "nashville": "37205",
        "saltlakecity": "84118", "milwaukee": "53221",
    }
    zip_code = (product.get("zip_code") or product.get("postal_code") or "").strip()
    if not zip_code:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        zip_code = _ZIPS.get(_ck, "90001")

    # city name for geographic_area field
    _CITY_NAMES = {
        "losangeles": "Los Angeles", "newyork": "New York", "chicago": "Chicago",
        "houston": "Houston", "phoenix": "Phoenix", "sfbay": "San Francisco",
        "sandiego": "San Diego", "seattle": "Seattle", "miami": "Miami",
        "dallas": "Dallas", "denver": "Denver", "atlanta": "Atlanta",
        "boston": "Boston", "portland": "Portland",
    }
    _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
    city_name = _CITY_NAMES.get(_ck, CL_CITY.title())

    cl_email = (os.environ.get("CL_EMAIL") or
                product.get("contact_email") or product.get("email") or "").strip()

    # ── 1. PostingTitle ──────────────────────────────────────────────────────
    print("  Filling title...")
    _fill_by_keys(driver, "[name='PostingTitle']", title, label="PostingTitle", send_tab=True)

    # ── 2. PostingBody ───────────────────────────────────────────────────────
    print("  Filling description...")
    _fill_by_keys(driver, "[name='PostingBody']", description, label="PostingBody", send_tab=True)

    # ── 3. Price ─────────────────────────────────────────────────────────────
    print("  Filling price...")
    price_filled = False
    for price_sel in ["[name='price']", "[name='AskingPrice']", "[name='AskPrice']",
                      "#AskingPrice", "#AskPrice", "#price"]:
        try:
            driver.find_element(By.CSS_SELECTOR, price_sel)
            # Use JS set for price — type=number fields can behave oddly with send_keys
            if _js_set(driver, price_sel, price, label="price"):
                price_filled = True
                break
        except Exception:
            continue
    if not price_filled:
        try:
            price_inputs = driver.find_elements(By.CSS_SELECTOR,
                "input[type='number'], input[id*='rice'], input[name*='rice']")
            for pi in price_inputs:
                if pi.is_displayed():
                    driver.execute_script("arguments[0].focus(); arguments[0].value = arguments[1];", pi, price)
                    driver.execute_script(
                        "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                        "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", pi)
                    print(f"  ✓ Price filled via fallback: {price}")
                    price_filled = True
                    break
        except Exception as pe:
            print(f"  ⚠ Price fallback failed: {pe}")

    # ── 4. FromEMail — NO ID, must use name selector + JS ───────────────────
    print("  Filling email...")
    email_filled = False
    try:
        ef = driver.find_element(By.CSS_SELECTOR, "[name='FromEMail']")
        cur_val = (ef.get_attribute("value") or "").strip()
        if cur_val:
            print(f"  ✓ FromEMail already populated: {cur_val}")
            email_filled = True
        elif cl_email:
            # JS set — bypasses any CL event handler interference
            _js_set(driver, "[name='FromEMail']", cl_email, label="FromEMail")
            email_filled = True
        else:
            # CL requires this field even when logged in for some categories
            # Grab the email from the env at login time if possible
            print("  ⚠ FromEMail empty and CL_EMAIL not set — validation may fail")
            print("  ⚠ Set CL_EMAIL environment variable to fix this")
    except NoSuchElementException:
        print("  [info] FromEMail field not present — session auth")
        email_filled = True

    # ── 5. geographic_area — required field, was completely missed before ────
    print("  Filling geographic_area...")
    try:
        ga = driver.find_element(By.CSS_SELECTOR, "[name='geographic_area']")
        cur = (ga.get_attribute("value") or "").strip()
        if not cur:
            _js_set(driver, "[name='geographic_area']", city_name, label="geographic_area")
        else:
            print(f"  ✓ geographic_area already set: {cur}")
    except NoSuchElementException:
        print("  [info] geographic_area field not present on this form")

    # ── 6. Condition dropdown (optional) ────────────────────────────────────
    try:
        cond_el = driver.find_element(By.NAME, "condition")
        cond = Select(cond_el)
        cond_val = product.get("condition", "")
        if cond_val:
            try:
                cond.select_by_visible_text(cond_val)
            except Exception:
                pass
    except Exception:
        pass

    # ── Small pause before postal fill ──────────────────────────────────────
    time.sleep(0.8)

    # ── 7. postal — MUST BE LAST, JS only, NO TAB ───────────────────────────
    # The postal field (name="postal", id="postal_code") is wiped by CL's JS
    # whenever blur events fire from other fields. By filling it dead-last
    # with pure JS (no TAB, no blur), it stays set when we click submit.
    print("  Filling postal (LAST, JS-only, no TAB)...")
    _js_set(driver, "[name='postal']", zip_code, label="postal")
    time.sleep(0.3)

    # ── Verify all critical fields ───────────────────────────────────────────
    print("  [pre-submit verification]")
    for fname, fval in [
        ("PostingTitle", title),
        ("PostingBody",  description),
        ("postal",       zip_code),
        ("FromEMail",    cl_email),
    ]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[name='{fname}']")
            actual = (el.get_attribute("value") or "").strip()
            if actual:
                print(f"    ✓ {fname}: '{actual[:50]}'")
            else:
                print(f"    ✗ {fname} EMPTY — emergency re-fill")
                if fname == "postal":
                    # Always JS-only for postal
                    _js_set(driver, f"[name='{fname}']", fval, label=fname)
                elif fname == "FromEMail" and fval:
                    _js_set(driver, f"[name='{fname}']", fval, label=fname)
                else:
                    _fill_by_keys(driver, f"[name='{fname}']", fval, label=fname,
                                  send_tab=(fname != "postal"))
        except Exception:
            pass  # field may not exist on all forms

    # ── RE-SET postal one final time right before clicking submit ────────────
    # Some CL pages run a final JS pass when scrolling to bottom — re-set after scroll
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.4)
    _js_set(driver, "[name='postal']", zip_code, label="postal-final")
    time.sleep(0.3)

    url_before = driver.current_url

    # ── Submit loop ──────────────────────────────────────────────────────────
    for attempt in range(4):
        if attempt > 0:
            if "s=edit" not in driver.current_url:
                break
            print(f"  ⚠ Still on edit page — retry {attempt}/3")
            time.sleep(1.2)

            # Re-fill cleared fields (postal always via JS)
            for fname, fval in [
                ("PostingTitle", title),
                ("PostingBody",  description),
                ("FromEMail",    cl_email),
                ("geographic_area", city_name),
            ]:
                try:
                    cur = (driver.find_element(By.CSS_SELECTOR, f"[name='{fname}']")
                           .get_attribute("value") or "").strip()
                    if not cur and fval:
                        print(f"    ↻ {fname} cleared — re-filling")
                        _fill_by_keys(driver, f"[name='{fname}']", fval, label=fname, send_tab=True)
                except Exception:
                    pass

            # Postal always last, always JS
            _js_set(driver, "[name='postal']", zip_code, label="postal-retry")
            time.sleep(0.3)

        # Click the submit button — do NOT call form.submit() as it skips CL's JS validation
        clicked = False
        for sel in [
            (By.CSS_SELECTOR, "button.go.big-button.submit-button"),
            (By.CSS_SELECTOR, "button.go.submit-button"),
            (By.CSS_SELECTOR, "button.submit-button"),
            (By.XPATH, '//*[@id="postingForm"]/button'),
            (By.XPATH, '//form[@id="postingForm"]//button[@type="submit"]'),
            (By.CSS_SELECTOR, "button.go"),
            (By.CSS_SELECTOR, "button[type='submit']"),
        ]:
            try:
                btn = WebDriverWait(driver, 4).until(EC.element_to_be_clickable(sel))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.3)
                # Re-set postal right before the actual click — CL sometimes clears it on scroll
                _js_set(driver, "[name='postal']", zip_code, label="postal-preclick")
                time.sleep(0.1)
                btn.click()
                print(f"  ✓ Submit clicked (attempt {attempt+1})")
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

    # ── Post-submit diagnostics ──────────────────────────────────────────────
    if "s=edit" in driver.current_url:
        print("  ✗ Still on edit page after all retries — giving up")
        try:
            form_dump = driver.execute_script("""
                var form = document.getElementById('postingForm');
                if (!form) return 'NO FORM';
                var result = '';
                form.querySelectorAll('input,textarea,select').forEach(function(f) {
                    if (f.name) result += f.tagName+'#'+(f.id||'?')+' name='+f.name+
                              ' type='+(f.type||'?')+' required='+(f.required||'')+
                              ' val=['+(f.value||'').substring(0,40)+']\\n';
                });
                return result;
            """)
            print("  [form fields dump]:\n" + form_dump)
        except Exception:
            pass

    # Validation error messages
    try:
        errs = [e.text.strip() for e in driver.find_elements(
            By.CSS_SELECTOR, ".notices li, .err, .error, span.notice, .warning"
        ) if e.text.strip() and len(e.text.strip()) > 3]
        if errs:
            print("  [validation errors]:")
            for et in sorted(set(errs)):
                print(f"    → {et[:120]}")
    except Exception:
        pass

    try:
        WebDriverWait(driver, 12).until(lambda d: d.current_url != url_before)
        print(f"  ✓ Navigated to: {driver.current_url}")
    except TimeoutException:
        print(f"  ⚠ Still on edit page after continue: {driver.current_url}")


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
            EC.element_to_be_clickable((By.ID, "add_photos_button")))
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
            try: os.unlink(tf)
            except Exception: pass

def publish_listing(driver, ad_name, product):
    handle_captcha_if_present(driver)
    human_delay(4, 6)
    try:
        pub = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "publish_button")))
        safe_click(driver, pub)
        human_delay(5, 8)
        handle_captcha_if_present(driver)
        listing_url = driver.current_url
        print(f"  Published → {listing_url}")
        posted_listings[ad_name] = {
            "url": listing_url, "post_time": datetime.now(),
            "visitors": 0, "platform": "Craigslist",
        }
        _save_listings()
        return True
    except TimeoutException:
        print(f"  ⚠  Publish button not found for '{ad_name}'.")
        return False

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

    # City selection
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
        if not city_clicked and menu_items:
            driver.execute_script("arguments[0].click();", menu_items[0])
            print("  ⚠ Fallback city selected")
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
            print("  ⚠ Still on area page after 12s wait")
        handle_captcha_if_present(driver)
    except Exception as e:
        print(f"  City selection error: {e}")

    print(f"  Waiting for post-type page... current URL: {driver.current_url}")
    if "s=area" in driver.current_url:
        print("  Still on area page, retrying city continue...")
        try:
            retry_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR,
                    "button.go.pickbutton, button[class*='pickbutton'], button[type='submit']")))
            driver.execute_script("arguments[0].click();", retry_btn)
            try:
                WebDriverWait(driver, 10).until(lambda d: "s=area" not in d.current_url)
            except TimeoutException:
                pass
        except Exception as e:
            print(f"  ✗ City retry failed: {e}")
            return False

    if "s=area" in driver.current_url:
        return False
    print(f"  ✓ Left area → {driver.current_url}")
    handle_captcha_if_present(driver)
    human_delay(2, 4)

    # Post type
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

    # Category
    cat_clicked = False
    mapped_label = CATEGORY_MAPPING.get(product.get("category", "").lower().strip(), (None, ""))[1]
    if not mapped_label:
        mapped_label = product.get("category", "")
    print(f"  Target category label: {mapped_label}")

    if mapped_label:
        try:
            target_lower = mapped_label.lower().strip()
            xpath = f"//label[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{target_lower}')]"
            label_el = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].click();", label_el)
            cat_clicked = True
            print(f"  ✓ Selected category via label XPath: '{mapped_label}'")
        except Exception as e:
            print(f"  Category label failed: {e}")

    if not cat_clicked:
        try:
            ul_value = get_category_ul_value(product.get("category", ""))
            inp = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f"input[type='radio'][value='{ul_value}']")))
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
            time.sleep(1.5)
            print("  ✓ postingForm visible after category selection")
        except TimeoutException:
            time.sleep(3)
        print(f"  Current URL after category continue: {driver.current_url}")
        handle_captcha_if_present(driver)
    except TimeoutException:
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
                      "s=preview" in d.current_url)
        reached_photo_step = True
        print(f"  ✓ Reached next step: {driver.current_url}")
    except TimeoutException:
        print(f"  ⚠ Did not reach photo step. Still at: {driver.current_url}")

    if not reached_photo_step or "s=edit" in driver.current_url:
        print("  ✗ Skipping photo upload — still on edit/form page. Aborting post.")
        return False

    upload_photos(driver, product)
    return publish_listing(driver, ad_name, product)


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
                print(f"  ⚠  Analytics error for {ad_name}: {e}")
            finally:
                if tmp: tmp.quit()
        _save_listings()
        time.sleep(300)

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