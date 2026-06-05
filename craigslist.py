"""
craigslist.py — CLBlast Craigslist automation

DEFINITIVE FIX using selenium-wire:
  - Intercept the real POST request CL's own JS makes on form submit
  - Modify field values in that captured request
  - Replay it — server sees a legitimate browser-generated request
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

def _ensure_xvfb():
    """Headed Chromium on a virtual display — CL often rejects headless fills."""
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


def make_driver(proxy_url=None):
    from selenium.webdriver.chrome.service import Service as ChromeService
    os.environ["SE_MANAGER_PATH"] = ""
    os.environ["WDM_SKIP_DOWNLOAD"] = "1"
    _ensure_xvfb()
    use_headed = bool(os.environ.get("DISPLAY"))

    # Use proxy from argument or environment
    if not proxy_url:
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    options = webdriver.ChromeOptions()
    chrome_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1280,800",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--ignore-certificate-errors",
        "--disable-extensions",
        "--mute-audio",
        "--no-first-run",
        "--shm-size=256m",
    ]
    if not use_headed:
        chrome_args.insert(3, "--headless=new")
    for arg in chrome_args:
        options.add_argument(arg)
    if use_headed:
        print("  [driver] Headed mode (virtual display)")

    # Apply proxy to Chrome so login + navigation use the same IP as the POST request
    if proxy_url:
        # Strip scheme for --proxy-server (Chrome accepts http:// or just host:port)
        proxy_for_chrome = proxy_url
        print(f"  [driver] Proxy: {proxy_for_chrome.split('@')[-1] if '@' in proxy_for_chrome else proxy_for_chrome}")
        options.add_argument(f"--proxy-server={proxy_for_chrome}")
        # Bypass proxy for localhost only
        options.add_argument("--proxy-bypass-list=localhost,127.0.0.1")
    fresh_profile = tempfile.mkdtemp(prefix="clblast_chrome_")
    options.add_argument(f"--user-data-dir={fresh_profile}")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "autofill.profile_enabled": False,
        "autofill.credit_card_enabled": False,
    })
    options.add_argument("--disable-features=AutofillServerCommunication")
    chromium_bin = _find_binary(
        ["google-chrome", "chromium", "chromium-browser"],
        ["/usr/local/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"])
    if chromium_bin:
        print(f"  [driver] Using chromium: {chromium_bin}")
        options.binary_location = chromium_bin
    chromedriver_bin = _find_binary(
        ["chromedriver"],
        ["/usr/local/bin/chromedriver", "/usr/bin/chromedriver"])
    # Prefer our wrapper script which passes --allowed-origins=*
    if os.path.exists("/usr/local/bin/chromedriver"):
        chromedriver_bin = "/usr/local/bin/chromedriver"
    if not chromedriver_bin:
        raise RuntimeError("chromedriver not found")
    print(f"  [driver] Using chromedriver: {chromedriver_bin}")
    service = ChromeService(
        executable_path=chromedriver_bin,
        log_output="/tmp/chromedriver.log",
    )
    # ChromeDriver 115+ requires explicit allowed origins in newer builds
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
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver

def human_delay(lo=0.8, hi=2.5):
    time.sleep(random.uniform(lo * 0.3 if IS_FAST_MODE else lo,
                              hi * 0.3 if IS_FAST_MODE else hi))

def send_keys_slow(driver, element, text):
    try:
        ActionChains(driver).move_to_element(element).click().perform()
    except Exception:
        try:
            element.click()
        except Exception:
            pass
    time.sleep(random.uniform(0.3, 0.7))
    element.clear()
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.05, 0.15))

def safe_click(driver, element):
    human_delay(2.0, 5.0)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.3, 0.8)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    human_delay(1.0, 2.5)

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
        send_keys_slow(driver, ef, email)
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
#  SELENIUM-WIRE INTERCEPT APPROACH
#
#  The problem: CL server validates ZIP against session region.
#  When we POST directly, something in the session context doesn't match.
#
#  Solution: Let CL's own JS submit the form (which it validates correctly),
#  intercept that request with selenium-wire, capture it, then we know the
#  exact format CL expects. We use that as a template for future posts.
#
#  For the FIRST post: fill form via JS + let CL submit it, intercept request,
#  modify values (title/desc/price) and replay for subsequent posts.
# ─────────────────────────────────────────────────────────────────────────────

_REACT_SET_VALUE_JS = """
var el = arguments[0];
var value = String(arguments[1]);
var tracker = el._valueTracker;
if (tracker) { tracker.setValue(''); }
var proto = el.tagName === 'TEXTAREA'
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
var ownDesc = Object.getOwnPropertyDescriptor(el, 'value');
var protoDesc = Object.getOwnPropertyDescriptor(proto, 'value');
if (ownDesc && ownDesc.set && protoDesc && ownDesc.set !== protoDesc.set) {
    protoDesc.set.call(el, value);
} else if (protoDesc && protoDesc.set) {
    protoDesc.set.call(el, value);
} else {
    el.value = value;
}
el.dispatchEvent(new Event('input',  {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
return el.value;
"""


def _react_set_value(driver, element, value):
    """Update a React controlled input so CL's validator sees the value."""
    return driver.execute_script(_REACT_SET_VALUE_JS, element, str(value)) or ""


def _disable_form_autofill(driver):
    """Block browser autofill before we type."""
    try:
        driver.execute_script("""
            var form = document.getElementById('postingForm');
            if (!form) return;
            form.setAttribute('autocomplete', 'off');
            form.querySelectorAll('input,textarea').forEach(function(el) {
                el.setAttribute('autocomplete', 'off');
                el.setAttribute('data-lpignore', 'true');
            });
        """)
    except Exception:
        pass


def _cdp_click_element(driver, element):
    rect = element.rect
    x = rect['x'] + rect['width'] / 2
    y = rect['y'] + rect['height'] / 2
    for event_type in ('mousePressed', 'mouseReleased'):
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": event_type,
            "x": x, "y": y,
            "buttons": 1,
            "button": "left",
            "clickCount": 1,
        })


# Only these fields accept text entry — never "re-type" hidden inputs or checkboxes
_FILLABLE_FIELDS = frozenset({
    "PostingTitle", "PostingBody", "postal", "FromEMail",
    "geographic_area", "price", "AskingPrice", "AskPrice",
})


def _focus_field(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    time.sleep(0.2)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.1, 0.3)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].focus(); arguments[0].click();", element)
    time.sleep(0.25)


def _type_with_send_keys(driver, element, value):
    """Type with real send_keys — trusted events React/CL accept."""
    driver.execute_script("""
        var el = arguments[0];
        if (el._valueTracker) { el._valueTracker.setValue(''); }
    """, element)
    element.send_keys(Keys.CONTROL + "a")
    time.sleep(0.06)
    element.send_keys(Keys.DELETE)
    time.sleep(0.1)
    for ch in str(value):
        element.send_keys(ch)
        time.sleep(random.uniform(0.06, 0.14))


def _nudge_user_edited(element):
    """Tiny edit at end-of-field via send_keys — clears CL 'autofilled' flag."""
    element.click()
    time.sleep(0.12)
    element.send_keys(Keys.END)
    time.sleep(0.08)
    val = (element.get_attribute("value") or "").strip()
    ch = val[-1] if val else " "
    element.send_keys(ch)
    time.sleep(0.08)
    element.send_keys(Keys.BACKSPACE)
    time.sleep(0.1)


def _cl_fill_field(driver, element, value, *, nudge=False, use_tab=True):
    """Focus, type slowly, optional nudge for autofilled-sensitive fields."""
    _focus_field(driver, element)
    _type_with_send_keys(driver, element, value)
    if nudge:
        _nudge_user_edited(element)
    if use_tab:
        element.send_keys(Keys.TAB)
    else:
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('blur',{bubbles:true}));", element)
    time.sleep(0.4)


def _ensure_field_value(driver, name, expected, nudge=False):
    """Re-fill if DOM value does not match expected."""
    expected = str(expected).strip()
    if not expected:
        return True
    try:
        el = driver.find_element(By.CSS_SELECTOR, f"[name='{name}']")
        actual = (el.get_attribute("value") or "").strip()
        if actual == expected:
            return True
        print(f"  [fix] {name}: got '{actual[:40]}' want '{expected[:40]}'")
        _cl_fill_field(driver, el, expected, nudge=nudge, use_tab=True)
        actual = (el.get_attribute("value") or "").strip()
        ok = actual == expected
        if not ok:
            print(f"  [fix] {name}: still '{actual[:40]}' after retry")
        return ok
    except Exception as e:
        print(f"  [fix] {name}: {e}")
        return False


# CL flags these unless the user appears to have edited them manually
_NUDGE_FIELDS = frozenset({"PostingTitle", "postal", "FromEMail"})


def _autofill_fields_from_errors(errs):
    """Map CL validation messages to field names that need re-typing."""
    blob = " ".join(errs).lower()
    names = []
    if "title" in blob:
        names.append("PostingTitle")
    if "zip" in blob or "postal" in blob:
        names.append("postal")
    if "description" in blob or "must have a des" in blob:
        names.append("PostingBody")
    if "email" in blob:
        names.append("FromEMail")
    return names


def _form_validation_errors(driver):
    """Return visible CL form validation messages (empty list = OK to submit)."""
    try:
        return driver.execute_script("""
            var msgs = [];
            var form = document.getElementById('postingForm');
            if (!form) return msgs;
            form.querySelectorAll('.err, .error, [class*="error"], [class*="invalid"]')
                .forEach(function(el) {
                    var t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t && t.length > 2 && msgs.indexOf(t) === -1) msgs.push(t);
                });
            return msgs;
        """) or []
    except Exception:
        return []


def _js_fill_field(driver, selector, value):
    """Fill field using React-aware native setter."""
    el = driver.find_element(By.CSS_SELECTOR, selector)
    _react_set_value(driver, el, value)


def fill_and_submit_with_wire(driver, product, zip_code, city_name, cl_email):
    """
    Fill CL's React posting form, then submit via browser click (preferred)
    or requests POST fallback. Returns the next-step URL on success.
    """
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm")))
    except TimeoutException:
        print(f"  ✗ postingForm not found")
        return None

    handle_captcha_if_present(driver)
    _disable_form_autofill(driver)
    time.sleep(1.5)

    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    description = (product.get("description") or (
        f"{title} in excellent condition. Well maintained and ready for a new home. "
        f"Priced to sell. Local pickup preferred. Message for details.")).strip()
    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "")
    try:
        price_f = float(_pr) if _pr else 1.0
        price = str(int(price_f)) if price_f == int(price_f) else str(price_f)
    except Exception:
        price = "1"

    def real_fill(selector, value, use_tab=True):
        value = str(value).strip()
        if not value:
            return ""
        try:
            el = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
            name = el.get_attribute("name") or ""
            _cl_fill_field(
                driver, el, value,
                nudge=(name in _NUDGE_FIELDS),
                use_tab=use_tab,
            )
            actual = (el.get_attribute("value") or "").strip()
            if actual != value:
                print(f"  ⚠ {selector} mismatch — retrying")
                _cl_fill_field(driver, el, value, nudge=True, use_tab=use_tab)
                actual = (el.get_attribute("value") or "").strip()
            print(f"  ✓ {selector} = '{actual[:50]}'")
            return actual
        except Exception as e:
            print(f"  ✗ real_fill({selector}): {e}")
            return ""

    _field_map = {
        "PostingTitle": title,
        "PostingBody": description,
        "FromEMail": cl_email,
        "postal": zip_code,
        "geographic_area": city_name,
        "price": price,
    }

    def _retry_fields(field_map, names):
        """Re-fill only whitelisted text fields (never hidden/checkbox names)."""
        todo = [n for n in dict.fromkeys(names) if n in _FILLABLE_FIELDS]
        if not todo:
            return
        print(f"  [retry] Re-filling fields: {todo}")
        for name in todo:
            val = field_map.get(name)
            if not val:
                continue
            try:
                el = driver.find_element(By.CSS_SELECTOR, f"[name='{name}']")
                _cl_fill_field(
                    driver, el, val,
                    nudge=(name in _NUDGE_FIELDS),
                    use_tab=True,
                )
            except Exception as e:
                print(f"  [retry] Could not re-fill {name}: {e}")

    print("  Filling title...")
    real_fill("[name='PostingTitle']", title, use_tab=True)

    print("  Filling description...")
    real_fill("[name='PostingBody']", description, use_tab=True)

    print("  Filling price...")
    for price_sel in ["[name='price']", "[name='AskingPrice']", "[name='AskPrice']"]:
        try:
            driver.find_element(By.CSS_SELECTOR, price_sel)
            real_fill(price_sel, price, use_tab=True)
            break
        except Exception:
            continue

    print("  Filling email...")
    if cl_email:
        real_fill("[name='FromEMail']", cl_email, use_tab=True)

    print("  Filling geographic_area...")
    try:
        driver.find_element(By.CSS_SELECTOR, "[name='geographic_area']")
        real_fill("[name='geographic_area']", city_name, use_tab=True)
    except NoSuchElementException:
        pass

    # Condition
    try:
        cond_val = product.get("condition", "")
        if cond_val:
            Select(driver.find_element(By.NAME, "condition")).select_by_visible_text(cond_val)
    except Exception:
        pass

    # postal — fill last, no TAB (TAB can clear React state on the last field)
    print("  Filling postal (last, no TAB)...")
    if zip_code:
        real_fill("[name='postal']", zip_code, use_tab=False)
        time.sleep(0.5)
    else:
        print("  ⚠ No ZIP/postal code for this city — skipping postal field")

    print("  Nudging title, email, ZIP (CL autofill check)...")
    for nudge_name in _NUDGE_FIELDS:
        nudge_val = _field_map.get(nudge_name)
        if not nudge_val:
            continue
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[name='{nudge_name}']")
            _nudge_user_edited(el)
            time.sleep(0.35)
        except Exception as e:
            print(f"  [nudge] {nudge_name}: {e}")

    print("  Verifying field values...")
    for fname, fval in _field_map.items():
        if fval:
            _ensure_field_value(
                driver, fname, fval, nudge=(fname in _NUDGE_FIELDS))

    time.sleep(0.5)
    val_errs = _form_validation_errors(driver)
    if val_errs:
        print(f"  [validate] Errors after fill ({len(val_errs)} msg(s))")
        for err in val_errs[:4]:
            print(f"  [validate] {err[:120]}")
        err_fields = _autofill_fields_from_errors(val_errs)
        if not err_fields:
            err_fields = list(_field_map.keys())
        _retry_fields(_field_map, err_fields)
        time.sleep(0.8)
        for fname, fval in _field_map.items():
            if fval:
                _ensure_field_value(
                    driver, fname, fval, nudge=(fname in _NUDGE_FIELDS))
        val_errs = _form_validation_errors(driver)
        for err in val_errs[:4]:
            print(f"  [validate] Still visible: {err[:120]}")

    # Extract form + POST via requests with residential proxy
    time.sleep(0.5)

    form_data = driver.execute_script("""
        var form = document.getElementById('postingForm');
        if (!form) return null;
        var data = [];
        form.querySelectorAll('input,textarea,select').forEach(function(el) {
            if (!el.name) return;
            if ((el.type === 'checkbox' || el.type === 'radio') && !el.checked) return;
            data.push([el.name, el.value || '']);
        });
        data.push(['__action__', form.action || '']);
        return data;
    """)

    if not form_data:
        print("  ✗ Could not extract form data")
        return None

    form_dict = {}
    form_action = driver.current_url
    for pair in form_data:
        if pair[0] == '__action__':
            if pair[1]: form_action = pair[1]
        else:
            form_dict[pair[0]] = pair[1]

    # Ensure our filled values are in the POST
    form_dict['PostingTitle'] = title
    form_dict['PostingBody'] = description
    form_dict['geographic_area'] = city_name
    if zip_code and re.match(r'^\d{5}$', zip_code):
        form_dict['postal'] = zip_code
    else:
        form_dict.pop('postal', None)  # Don't send invalid/empty postal
    if cl_email:
        form_dict['FromEMail'] = cl_email

    # Price: set in whichever field CL uses, AND always include 'price' as fallback
    price_set = False
    for pf in ['price', 'AskingPrice', 'AskPrice']:
        if pf in form_dict:
            form_dict[pf] = price
            price_set = True
    if not price_set:
        form_dict['price'] = price  # Always include price

    # Privacy field: CL requires 'C' (contact by email only) — preserve if already set
    if 'Privacy' not in form_dict:
        form_dict['Privacy'] = 'C'

    # Language: preserve whatever the form has; default to English (5) if missing
    if 'language' not in form_dict:
        form_dict['language'] = '5'

    # Only include phone-related checkboxes if a phone number is actually provided
    contact_phone = (product.get("phone") or product.get("contact_phone") or
                     os.environ.get("CL_PHONE", "")).strip()
    if contact_phone:
        form_dict['contact_phone_ok'] = '1'
        form_dict['contact_text_ok'] = '1'
        form_dict['show_phone_ok'] = '1'
        form_dict['contact_phone'] = contact_phone
    else:
        # Explicitly remove all phone fields so CL doesn't require a number
        for pf in ['contact_phone_ok', 'contact_text_ok', 'show_phone_ok',
                   'contact_phone', 'contact_phone_extension', 'contact_name']:
            form_dict.pop(pf, None)

    # Non-phone optional checkboxes — safe to include
    for cb in ['crypto_currency_ok', 'delivery_available', 'see_my_other',
               'show_address_ok', 'save_contact_preferences']:
        form_dict[cb] = '1'

    # Remove empty optional fields that confuse CL's validator
    for optional in ['xstreet0', 'xstreet1', 'city',
                     'sale_manufacturer', 'sale_model', 'sale_size', 'condition']:
        form_dict.pop(optional, None)

    # For non-US accounts (e.g. Chandigarh), postal/ZIP may not be needed or uses local format
    # If zip_code looks non-US (not 5 digits), remove it to avoid CL's ZIP validator
    if zip_code and not re.match(r'^\d{5}$', zip_code):
        form_dict.pop('postal', None)
    elif not zip_code:
        form_dict.pop('postal', None)

    try:
        for btn in driver.find_elements(By.CSS_SELECTOR,
                "button.go, button[type='submit'], input[type='submit']"):
            btn_name = (btn.get_attribute("name") or "").strip()
            if btn_name:
                form_dict[btn_name] = (
                    btn.get_attribute("value") or btn.text or "continue").strip()
                break
    except Exception:
        pass

    print(f"  [post] {len(form_dict)} fields → {form_action}")
    print(f"  [post] postal={form_dict.get('postal')} title={form_dict.get('PostingTitle','')[:25]}")
    print(f"  [post] cryptedStepCheck={form_dict.get('cryptedStepCheck','')[:20]}...")
    # Print every field name and value so we can see exactly what's being sent
    for k, v in sorted(form_dict.items()):
        print(f"  [post-field] {k}={str(v)[:50]}")

    for nudge_name in ("PostingTitle", "postal", "FromEMail"):
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[name='{nudge_name}']")
            _nudge_user_edited(el)
        except Exception:
            pass
    time.sleep(0.5)

    print("  [submit] Attempting form submission (multi-strategy)...")

    # Diagnostic: show what submit buttons are available
    try:
        btns = driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit']")
        for b in btns:
            btype = b.get_attribute('type') or ''
            bcls = b.get_attribute('class') or ''
            btxt = (b.text or b.get_attribute('value') or '')[:30]
            bdis = b.get_attribute('disabled')
            print(f"  [btn-scan] type={btype} class={bcls[:30]} text={btxt} disabled={bdis}")
    except Exception:
        pass

    # Helper: detect whether submission actually succeeded
    def _submission_succeeded(wait_secs=12):
        """
        Returns True if we've moved past the edit page.
        CL can redirect to s=images, s=preview, s=confirm, s=success, etc.
        Also returns True if a requests.Response is passed and its HTML
        doesn't contain the posting form (meaning CL accepted it).
        """
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            url = driver.current_url
            if "s=edit" not in url:
                return True
            time.sleep(1)
        return False

    # ── Strategy 1: ActionChains real click (most human-like) ──────────────
    submitted = False
    _SUBMIT_SEL = (
        "button.go:not([disabled]), "
        "button.continue:not([disabled]), "
        "button[type='submit']:not([disabled]), "
        "button.pickbutton:not([disabled]), "
        "input[type='submit']:not([disabled])"
    )
    def _try_click_submit_buttons():
        """Try clicking all submit buttons, return True if any navigation occurs."""
        try:
            all_btns = driver.find_elements(By.CSS_SELECTOR, _SUBMIT_SEL)
        except Exception:
            return False
        # Try last button first (CL's continue is at page bottom)
        for btn in reversed(all_btns):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.4)
                ActionChains(driver).move_to_element(btn).pause(
                    random.uniform(0.2, 0.5)).click().perform()
                time.sleep(2)
                if "s=edit" not in driver.current_url:
                    return True
                # Also try JS click on same button
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                if "s=edit" not in driver.current_url:
                    return True
            except Exception:
                continue
        return False

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, _SUBMIT_SEL))
        )
        print("  [submit] ActionChains click sent")
        if _try_click_submit_buttons() or _submission_succeeded(10):
            submitted = True
            print(f"  ✅ Strategy 1 (ActionChains) succeeded → {driver.current_url}")
        else:
            print(f"  [submit] Strategy 1: still on edit page after click")
    except Exception as e:
        print(f"  [submit] Strategy 1 failed: {e}")

    # ── Strategy 2: JS click on all buttons ───────────────────────────────
    if not submitted:
        try:
            all_btns = driver.find_elements(By.CSS_SELECTOR, _SUBMIT_SEL)
            for btn in reversed(all_btns):
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(3)
                if "s=edit" not in driver.current_url:
                    break
            print("  [submit] JS click sent")
            if _submission_succeeded(10):
                submitted = True
                print(f"  ✅ Strategy 2 (JS click) succeeded → {driver.current_url}")
        except Exception as e:
            print(f"  [submit] Strategy 2 failed: {e}")

    # ── Strategy 3: Enter key on focused form field ─────────────────────────
    if not submitted:
        try:
            # Focus a text field then press Enter — triggers CL's own submit handler
            title_el = driver.find_element(By.CSS_SELECTOR, "[name='PostingTitle']")
            title_el.send_keys(Keys.RETURN)
            print("  [submit] Enter key sent on title field")
            if _submission_succeeded(12):
                submitted = True
                print(f"  ✅ Strategy 3 (Enter key) succeeded → {driver.current_url}")
        except Exception as e:
            print(f"  [submit] Strategy 3 failed: {e}")

    # ── Strategy 4: JS form.submit() ──────────────────────────────────────
    if not submitted:
        try:
            driver.execute_script(
                "var f=document.getElementById('postingForm'); if(f) f.submit();"
            )
            print("  [submit] form.submit() sent")
            if _submission_succeeded(12):
                submitted = True
                print(f"  ✅ Strategy 4 (form.submit) succeeded → {driver.current_url}")
        except Exception as e:
            print(f"  [submit] Strategy 4 failed: {e}")

    # ── Strategy 5: requests POST using cookies from browser session ───────
    if not submitted:
        print("  [submit] Strategy 5: requests POST with browser cookies...")
        try:
            session = requests.Session()
            # Copy all browser cookies into requests session
            for cookie in driver.get_cookies():
                session.cookies.set(cookie['name'], cookie['value'],
                                    domain=cookie.get('domain', ''))
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': driver.current_url,
                'Origin': 'https://post.craigslist.org',
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
            }
            resp = session.post(
                form_action,
                data=form_dict,
                headers=headers,
                allow_redirects=True,
                timeout=30,
            )
            print(f"  [submit] requests POST → {resp.status_code} → {resp.url}")
            # CL returns 200 even on failure — check for postingForm in body
            # If the response body still contains postingForm, submission was rejected
            resp_has_form = 'id="postingForm"' in resp.text or "name=\"cryptedStepCheck\"" in resp.text
            if resp.status_code == 200 and "s=edit" not in resp.url and not resp_has_form:
                # Navigate driver to the response URL so publish step works
                driver.get(resp.url)
                time.sleep(3)
                submitted = True
                print(f"  ✅ Strategy 5 (requests POST) succeeded → {resp.url}")
            else:
                # Extract any error messages from the response HTML
                err_matches = re.findall(r'class="[^"]*err[^"]*"[^>]*>([^<]{5,})<', resp.text)
                if err_matches:
                    print(f"  [submit] CL error(s): {err_matches[:3]}")
                print(f"  [submit] Strategy 5 rejected (form still present: {resp_has_form})")
        except Exception as e:
            print(f"  [submit] Strategy 5 failed: {e}")

    if not submitted:
        print("  ❌ All submit strategies failed — still on edit page")
        return None

    print(f"  ✅ Posted successfully → {driver.current_url}")
    return driver.current_url


def fill_listing_details(driver, product: dict):
    _ZIPS = {
        # US cities
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
        # India / non-US (CL India uses area name instead of postal code — leave blank)
        "chandigarh": "", "delhi": "", "mumbai": "", "bangalore": "",
        "hyderabad": "", "chennai": "", "kolkata": "", "pune": "",
        "ahmedabad": "", "jaipur": "", "lucknow": "", "surat": "",
    }
    # Priority: server-injected > product field > env var > city lookup
    zip_code = (
        product.get("_location_zip") or
        os.environ.get("CL_ZIP") or
        product.get("zip_code") or
        product.get("postal_code") or
        ""
    ).strip()
    if not zip_code:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        zip_code = _ZIPS.get(_ck, "")  # Default empty for unknown cities

    _CITY_NAMES = {
        "losangeles": "Los Angeles", "newyork": "New York", "chicago": "Chicago",
        "houston": "Houston", "phoenix": "Phoenix", "sfbay": "San Francisco",
        "sandiego": "San Diego", "seattle": "Seattle", "miami": "Miami",
        "dallas": "Dallas", "denver": "Denver", "atlanta": "Atlanta",
        "boston": "Boston", "portland": "Portland",
        # India
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
        print(f"  ✓ Form submitted successfully → {result_url}")
        return True

    print(f"  ✗ Still on edit page after wire submit")
    return False


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
        print(f"  ⚠ Photo upload issue: {e}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass

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
        print(f"  ⚠ Publish button not found for '{ad_name}'.")
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

    try:
        success = fill_listing_details(driver, product)
    except Exception as e:
        print(f"  ✗ fill_listing_details crashed: {e}")
        import traceback
        traceback.print_exc()
        return False

    if not success:
        return False

    reached_next_step = False
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.ID, "add_photos_button") or
                      d.find_elements(By.ID, "publish_button") or
                      "s=images" in d.current_url or
                      "s=preview" in d.current_url)
        reached_next_step = True
        print(f"  ✓ Reached next step: {driver.current_url}")
    except TimeoutException:
        print(f"  ⚠ Did not reach photo/publish step. URL: {driver.current_url}")

    if not reached_next_step or "s=edit" in driver.current_url:
        print("  ✗ Still on edit page. Aborting.")
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
                print(f"  ⚠ Analytics error for {ad_name}: {e}")
            finally:
                if tmp:
                    tmp.quit()
        _save_listings()
        time.sleep(300)


def main():
    global CL_CITY
    email    = os.environ.get("CL_EMAIL", "").strip()
    if not email:
        print("✗ CL_EMAIL environment variable not set. Add it to Railway Variables.")
        return
    CL_CITY  = os.environ.get("CL_CITY", CL_CITY)
    _load_existing_listings()

    vdisplay = None
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
    try:
        if vdisplay:
            vdisplay.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()