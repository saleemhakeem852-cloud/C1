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


_REACT_HUMAN_SET_JS = """
var el = arguments[0];
var value = String(arguments[1]);
function patchFiber(f, d) {
    if (!f || d > 24) return;
    var s = f.memoizedState;
    while (s) {
        var m = s.memoizedState;
        if (m && typeof m === 'object') {
            Object.keys(m).forEach(function(k) {
                if (/autofill/i.test(k)) m[k] = false;
                if (/userEdited|userModified|touched|dirty|manual/i.test(k)) m[k] = true;
            });
        }
        s = s.next;
    }
    patchFiber(f.child, d + 1);
    patchFiber(f.sibling, d + 1);
}
el.focus();
el.dispatchEvent(new FocusEvent('focusin', {bubbles: true}));
var proto = el.tagName === 'TEXTAREA'
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
if (el._valueTracker) { el._valueTracker.setValue(''); }
setter.call(el, '');
el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
for (var i = 0; i < value.length; i++) {
    setter.call(el, value.substring(0, i + 1));
    el.dispatchEvent(new InputEvent('input', {
        bubbles: true, cancelable: true, inputType: 'insertText', data: value[i]
    }));
}
el.dispatchEvent(new Event('change', {bubbles: true}));
el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
var fk = Object.keys(el).find(function(k) { return k.indexOf('__reactFiber') === 0; });
if (fk) patchFiber(el[fk], 0);
return el.value;
"""


def _react_human_set(driver, element, value):
    """Set field via React InputEvent chain — updates CL internal state, not just DOM."""
    try:
        return driver.execute_script(_REACT_HUMAN_SET_JS, element, str(value).strip()) or ""
    except Exception:
        return ""


def _clear_stale_errors(driver):
    """Remove stale error banner when all fields pass aria-invalid check."""
    try:
        driver.execute_script("""
            var form = document.getElementById('postingForm');
            if (!form) return;
            if (form.querySelector('[aria-invalid="true"]')) return;
            form.querySelectorAll('.err, .error, .errorbox, [class*="error"]').forEach(function(el) {
                var t = (el.textContent || '').toLowerCase();
                if (t.indexOf('autofill') !== -1 || t.indexOf('missing') !== -1 ||
                    t.indexOf('incorrect') !== -1) {
                    el.textContent = '';
                    el.style.display = 'none';
                }
            });
        """)
    except Exception:
        pass


def _focus_field(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    time.sleep(0.2)
    try:
        ActionChains(driver).move_to_element(element).pause(
            random.uniform(0.1, 0.3)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].focus(); arguments[0].click();", element)
    time.sleep(0.25)


# Fields on CL edit page (s=edit) — see posting form screenshot
_CL_FORM_FIELDS = {
    "PostingTitle": "posting title",
    "PostingBody": "description",
    "postal": "ZIP code",
    "geographic_area": "city or neighborhood",
    "price": "price",
    "FromEMail": "email",
}


def _field_editable(driver, name):
    try:
        return driver.execute_script("""
            var el = document.querySelector('[name="'+arguments[0]+'"]');
            if (!el) return false;
            if (el.disabled || el.readOnly) return false;
            var st = window.getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return false;
            var r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        """, name)
    except Exception:
        return False


def _clear_and_type(driver, element, value):
    """Ctrl+A clear then slow send_keys — works on CL React inputs when backspace fails."""
    value = str(value).strip()
    _focus_field(driver, element)
    element.send_keys(Keys.CONTROL + "a")
    time.sleep(0.08)
    element.send_keys(Keys.DELETE)
    time.sleep(0.12)
    for ch in value:
        element.send_keys(ch)
        time.sleep(random.uniform(0.08, 0.15))
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));"
        "arguments[0].blur();", element)
    time.sleep(0.35)
    return (element.get_attribute("value") or "").strip()


def _human_fill(driver, element, value, use_tab=False):
    return _clear_and_type(driver, element, value)


def _autofill_banner_fields(driver):
    """
    Parse ONLY short error-banner lines like 'posting title • autofilled'.
    Do NOT walk parent DOM — that falsely flags every field on the page.
    """
    try:
        return driver.execute_script("""
            var names = [];
            var form = document.getElementById('postingForm');
            if (!form) return names;
            function add(n) { if (n && names.indexOf(n) === -1) names.push(n); }
            form.querySelectorAll('li, span, p, a, label').forEach(function(el) {
                var t = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (t.indexOf('autofill') === -1) return;
                if (t.length > 55 || t.length < 8) return;
                if (t.indexOf('title') !== -1) add('PostingTitle');
                if (t.indexOf('zip') !== -1) add('postal');
                if (t.indexOf('description') !== -1) add('PostingBody');
                if (t.indexOf('email') !== -1) add('FromEMail');
            });
            return names;
        """) or []
    except Exception:
        return []


def _nudge_autofill_field(driver, name):
    """One tiny edit at end of field — enough for CL to clear account-autofill flag."""
    try:
        el = driver.find_element(By.CSS_SELECTOR, f"[name='{name}']")
        _focus_field(driver, el)
        val = (el.get_attribute("value") or "").strip()
        if not val:
            return
        el.send_keys(Keys.END)
        time.sleep(0.08)
        el.send_keys(" ")
        time.sleep(0.08)
        el.send_keys(Keys.BACKSPACE)
        time.sleep(0.08)
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].blur();", el)
        time.sleep(0.25)
    except Exception as e:
        print(f"  [nudge] {name}: {e}")


def _verify_and_refill(driver, field_map):
    """Ensure title, body, postal, price, city match expected — refill once if empty/wrong."""
    ok = True
    checks = {
        "PostingTitle": field_map.get("PostingTitle"),
        "PostingBody": field_map.get("PostingBody"),
        "postal": field_map.get("postal"),
        "geographic_area": field_map.get("geographic_area"),
        "price": field_map.get("price"),
    }
    for name, expected in checks.items():
        if not expected:
            continue
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[name='{name}']")
            actual = (el.get_attribute("value") or "").strip()
            exp = str(expected).strip()
            if actual == exp:
                continue
            print(f"  [verify] {name} mismatch/empty (got '{actual[:30]}') — refilling")
            _clear_and_type(driver, el, exp)
            actual = (el.get_attribute("value") or "").strip()
            if actual != exp:
                print(f"  [verify] {name} still wrong after refill: '{actual[:30]}'")
                ok = False
        except NoSuchElementException:
            if name in ("postal", "geographic_area"):
                continue
            print(f"  [verify] {name} not found in form")
            ok = False
    return ok


def _ensure_fields_intact(driver, field_map):
    """Re-fill any fields CL cleared during a failed submit attempt."""
    missing = _missing_required_fields(driver)
    if not missing:
        return True
    print(f"  [repair] Restoring cleared fields: {missing}")
    for item in missing:
        name = item.split(":")[0]
        val = field_map.get(name)
        if not val:
            continue
        try:
            el = driver.find_element(By.CSS_SELECTOR, f"[name='{name}']")
            _clear_and_type(driver, el, val)
        except Exception as e:
            print(f"  [repair] {name}: {e}")
    return not _missing_required_fields(driver)


def _missing_required_fields(driver):
    """Return list of required fields missing from DOM or empty."""
    try:
        return driver.execute_script("""
            var required = ['PostingTitle','PostingBody','postal','FromEMail','price'];
            var missing = [];
            required.forEach(function(n) {
                var el = document.querySelector('[name="'+n+'"]');
                if (!el) { missing.push(n + ':not-found'); return; }
                if (!(el.value || '').trim()) missing.push(n + ':empty');
            });
            return missing;
        """) or []
    except Exception:
        return ["js-error"]


def _native_request_submit(driver):
    """Trigger CL's own submit handler (runs validation + builds POST)."""
    try:
        return driver.execute_script("""
            var form = document.getElementById('postingForm');
            if (!form) return {ok: false, reason: 'no-form'};
            var btn = form.querySelector(
                'button.go, button[type="submit"], input[type="submit"]');
            if (!btn) return {ok: false, reason: 'no-btn'};
            if (btn.disabled) return {ok: false, reason: 'btn-disabled'};
            btn.scrollIntoView({block: 'center'});
            try {
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(btn);
                    return {ok: true, method: 'requestSubmit'};
                }
            } catch (e) {}
            btn.click();
            return {ok: true, method: 'click'};
        """) or {"ok": False, "reason": "empty"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _field_status(driver):
    """Per-field DOM value + aria-invalid for diagnostics."""
    try:
        return driver.execute_script("""
            var names = ['PostingTitle','PostingBody','postal','FromEMail','price'];
            var out = {};
            names.forEach(function(n) {
                var el = document.querySelector('[name="'+n+'"]');
                if (!el) return;
                out[n] = {
                    value: (el.value || '').substring(0, 40),
                    invalid: el.getAttribute('aria-invalid') === 'true',
                    autofilled: el.matches && el.matches(':-webkit-autofill')
                };
            });
            return out;
        """) or {}
    except Exception:
        return {}


def _form_validation_errors(driver):
    """Only visible, field-level errors — ignore hidden page templates."""
    try:
        return driver.execute_script("""
            var msgs = [];
            var form = document.getElementById('postingForm');
            if (!form) return msgs;
            function visible(el) {
                if (!el) return false;
                var st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            form.querySelectorAll('input,textarea').forEach(function(el) {
                if (el.getAttribute('aria-invalid') !== 'true') return;
                var row = el.closest('li, .row, .formrow, p, div') || el.parentElement;
                if (!row) return;
                row.querySelectorAll('.err, .error, [class*="error"]').forEach(function(err) {
                    if (!visible(err)) return;
                    var t = (err.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t && msgs.indexOf(t) === -1) msgs.push(t);
                });
            });
            if (!msgs.length) {
                form.querySelectorAll('.err, .error').forEach(function(el) {
                    if (!visible(el)) return;
                    var t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t && t.length > 4 && t.length < 120 && msgs.indexOf(t) === -1)
                        msgs.push(t);
                });
            }
            return msgs;
        """) or []
    except Exception:
        return []


def _extract_live_form(driver):
    """Read postingForm field values exactly as CL's React state has them."""
    try:
        pairs = driver.execute_script("""
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
    except Exception:
        return None, None
    if not pairs:
        return None, None
    out = {}
    action = driver.current_url
    for pair in pairs:
        if pair[0] == '__action__':
            if pair[1]:
                action = pair[1]
        else:
            out[pair[0]] = pair[1]
    return out, action


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
    time.sleep(3)
    _disable_form_autofill(driver)
    baseline_errs = _form_validation_errors(driver)
    if baseline_errs:
        print(f"  [validate] Baseline (before fill): {len(baseline_errs)} visible")

    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    description = (product.get("description") or (
        f"{title} in excellent condition. Well maintained and ready for a new home. "
        f"Priced to sell. Local pickup preferred. Message for details.")).strip()
    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "").replace("Rs", "").strip()
    try:
        price_f = float(_pr) if _pr else 1.0
        price = str(int(price_f)) if price_f == int(price_f) else str(price_f)
    except Exception:
        price = "1"

    def real_fill(selector, value, use_tab=False):
        value = str(value).strip()
        if not value:
            return ""
        try:
            el = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
            actual = _human_fill(driver, el, value, use_tab=use_tab)
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

    # Fill top-to-bottom matching CL form layout (see s=edit page)
    print("  Filling title...")
    real_fill("[name='PostingTitle']", title)

    print("  Filling price...")
    for price_sel in ["[name='price']", "[name='AskingPrice']", "[name='AskPrice']"]:
        try:
            driver.find_element(By.CSS_SELECTOR, price_sel)
            real_fill(price_sel, price)
            break
        except Exception:
            continue

    print("  Filling city/neighborhood...")
    try:
        driver.find_element(By.CSS_SELECTOR, "[name='geographic_area']")
        real_fill("[name='geographic_area']", city_name)
    except NoSuchElementException:
        pass

    print("  Filling ZIP...")
    if zip_code:
        real_fill("[name='postal']", zip_code)
    else:
        print("  ⚠ No ZIP/postal code for this city — skipping postal field")

    print("  Filling description...")
    real_fill("[name='PostingBody']", description)

    # Email is often account-prefilled + read-only (CL mail relay) — only fill if editable
    if cl_email and _field_editable(driver, "FromEMail"):
        print("  Filling email...")
        real_fill("[name='FromEMail']", cl_email)
    else:
        print("  [email] Using account email (field not editable or relay mode)")

    try:
        cond_val = product.get("condition", "")
        if cond_val:
            Select(driver.find_element(By.NAME, "condition")).select_by_visible_text(cond_val)
    except Exception:
        pass

    print("  Verifying fields match expected values...")
    if not _verify_and_refill(driver, _field_map):
        print("  [verify] Some fields failed verification")

    flagged = _autofill_banner_fields(driver)
    if flagged:
        print(f"  [autofill] Banner flags: {flagged} — nudging once")
        for name in flagged:
            if name in _field_map and _field_map.get(name):
                if (driver.find_element(By.CSS_SELECTOR, f"[name='{name}']").get_attribute("value") or "").strip() != str(_field_map[name]).strip():
                    el = driver.find_element(By.CSS_SELECTOR, f"[name='{name}']")
                    _clear_and_type(driver, el, _field_map[name])
                else:
                    _nudge_autofill_field(driver, name)
        time.sleep(0.5)

    status = _field_status(driver)
    for fname, st in status.items():
        print(f"  [field] {fname}: value='{st.get('value','')}' "
              f"invalid={st.get('invalid')} autofilled={st.get('autofilled')}")

    val_errs = _form_validation_errors(driver)
    print(f"  [validate] visible_errors={len(val_errs)} missing={_missing_required_fields(driver)}")
    if val_errs:
        for err in val_errs[:4]:
            print(f"  [validate] {err[:120]}")

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
    def _submission_succeeded(wait_secs=15):
        """
        Returns True if we've moved past the edit page.
        CL can redirect to s=images, s=preview, s=confirm, s=success, etc.
        """
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            url = driver.current_url
            if "s=edit" not in url:
                print(f"  [submit] left edit → {url}")
                return True
            try:
                on_images = driver.execute_script("""
                    return !!(document.getElementById('add_photos_button') ||
                              document.getElementById('fileInput') ||
                              document.querySelector('input[type="file"]'));
                """)
                if on_images:
                    print("  [submit] images step detected")
                    return True
                form_gone = driver.execute_script(
                    "return !document.getElementById('postingForm');")
                if form_gone:
                    time.sleep(1.5)
                    if "s=edit" not in driver.current_url:
                        return True
            except Exception:
                pass
            time.sleep(0.75)
        return False

    # ── Strategy 0: form.requestSubmit (CL's native handler) ───────────────
    submitted = False
    if not _ensure_fields_intact(driver, _field_map):
        _resolve_autofill_errors(driver, _field_map, max_rounds=2)
    try:
        result = _native_request_submit(driver)
        print(f"  [submit] requestSubmit → {result}")
        if result.get("ok") and _submission_succeeded(12):
            submitted = True
            print(f"  ✅ Strategy 0 (requestSubmit) succeeded → {driver.current_url}")
        elif result.get("ok"):
            print("  [submit] Strategy 0: still on edit page after requestSubmit")
            _ensure_fields_intact(driver, _field_map)
    except Exception as e:
        print(f"  [submit] Strategy 0 failed: {e}")

    # ── Strategy 1: ActionChains real click (most human-like) ──────────────
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
        if not submitted:
            _ensure_fields_intact(driver, _field_map)
        print("  [submit] ActionChains click sent")
        if _try_click_submit_buttons() or _submission_succeeded(10):
            submitted = True
            print(f"  ✅ Strategy 1 (ActionChains) succeeded → {driver.current_url}")
        else:
            print(f"  [submit] Strategy 1: still on edit page after click")
            _ensure_fields_intact(driver, _field_map)
    except Exception as e:
        print(f"  [submit] Strategy 1 failed: {e}")

    # ── Strategy 2: JS click on all buttons ───────────────────────────────
    if not submitted:
        try:
            _ensure_fields_intact(driver, _field_map)
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
            else:
                _ensure_fields_intact(driver, _field_map)
        except Exception as e:
            print(f"  [submit] Strategy 2 failed: {e}")

    # ── Strategy 5: requests POST using cookies from browser session ───────
    if not submitted:
        if _autofill_banner_fields(driver):
            print("  [submit] Strategy 5 skipped — autofill errors still present")
        else:
            print("  [submit] Strategy 5: requests POST with browser cookies...")
            try:
                _ensure_fields_intact(driver, _field_map)
                live_dict, live_action = _extract_live_form(driver)
                post_data = live_dict if live_dict else form_dict
                post_url = live_action if live_action else form_action
                if live_dict:
                    print(f"  [submit] Strategy 5 using live DOM ({len(post_data)} fields)")
                session = requests.Session()
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
                    post_url,
                    data=post_data,
                    headers=headers,
                    allow_redirects=True,
                    timeout=30,
                )
                print(f"  [submit] requests POST → {resp.status_code} → {resp.url}")
                resp_has_form = (
                    'id="postingForm"' in resp.text
                    or "name=\"cryptedStepCheck\"" in resp.text
                )
                if resp.status_code == 200 and "s=edit" not in resp.url and not resp_has_form:
                    driver.get(resp.url)
                    time.sleep(3)
                    submitted = True
                    print(f"  ✅ Strategy 5 (requests POST) succeeded → {resp.url}")
                else:
                    err_matches = re.findall(
                        r'class="[^"]*err[^"]*"[^>]*>([^<]{5,})<', resp.text)
                    if err_matches:
                        print(f"  [submit] CL error(s): {err_matches[:3]}")
                    print(f"  [submit] Strategy 5 rejected (form still present: {resp_has_form})")
            except Exception as e:
                print(f"  [submit] Strategy 5 failed: {e}")

    if not submitted:
        print("  ❌ All submit strategies failed — still on edit page")
        fail_errs = _form_validation_errors(driver)
        if fail_errs:
            print(f"  [submit-fail] visible errors ({len(fail_errs)}):")
            for err in fail_errs[:5]:
                print(f"  [submit-fail] {err[:120]}")
        fail_status = _field_status(driver)
        for fname, st in fail_status.items():
            print(f"  [submit-fail] {fname}: value='{st.get('value','')}' "
                  f"invalid={st.get('invalid')}")
        print(f"  [submit-fail] missing={_missing_required_fields(driver)}")
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