"""
craigslist.py — CLBlast Craigslist automation

DEFINITIVE ROOT CAUSE (after many attempts):
  - DOM values are correct (confirmed by form dump every time)
  - CL validates via cryptedStepCheck token + server-side session
  - Headless browser gets flagged; CL's JS never marks fields as "valid" internally
  - Fix: extract all form fields + cryptedStepCheck, then POST directly via requests
    bypassing CL's client-side JS validation entirely
  - Selenium only used for login + navigation; requests handles form submission
"""

import time
import json
import os
import random
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

def make_driver():
    from selenium.webdriver.chrome.service import Service
    os.environ["SE_MANAGER_PATH"] = ""
    os.environ["WDM_SKIP_DOWNLOAD"] = "1"
    options = webdriver.ChromeOptions()
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
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
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
        if not CAPTCHA_SOLVER_AVAILABLE:
            if IS_RAILWAY:
                print("  CAPTCHA detected — no solver available.")
                return
        else:
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

def get_selenium_cookies_as_requests_session(driver):
    """
    Transfer Selenium browser cookies into a requests.Session.
    Visits all CL domains to collect every cookie before transferring.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    # Must collect cookies from EACH domain separately —
    # Selenium only returns cookies for the currently loaded domain.
    # Save current URL so we can return after collecting.
    original_url = driver.current_url
    all_cookie_names = set()

    for cl_domain in [
        "https://accounts.craigslist.org",
        "https://post.craigslist.org",
        "https://www.craigslist.org",
        "https://losangeles.craigslist.org",
    ]:
        try:
            driver.get(cl_domain)
            time.sleep(2)
            for cookie in driver.get_cookies():
                all_cookie_names.add(cookie["name"])
                session.cookies.set(cookie["name"], cookie["value"])
        except Exception as e:
            print(f"  [cookies] Could not visit {cl_domain}: {e}")

    # Return to original page
    try:
        driver.get(original_url)
        time.sleep(1)
    except Exception:
        pass

    print(f"  ✓ Transferred {len(all_cookie_names)} cookies to requests session")
    print(f"  Cookie names: {sorted(all_cookie_names)}")
    return session

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
#  DIRECT POST SUBMISSION
#
#  After years of fighting CL's JS validation, the answer is simple:
#  1. Navigate to the edit page with Selenium (gets us the cryptedStepCheck token)
#  2. Extract ALL form fields from the DOM
#  3. Override the fields we want (title, body, price, email, postal, geo)
#  4. POST the form data directly via requests (using Selenium's session cookies)
#
#  This bypasses CL's client-side JS validation completely.
#  The server only checks: cryptedStepCheck token + session cookie + field values.
#  All three are now correct.
# ─────────────────────────────────────────────────────────────────────────────

def _selenium_fill_field(driver, el, value, slow=False):
    """Clear a Selenium element and type value, dispatching all relevant JS events."""
    try:
        driver.execute_script("""
            var el = arguments[0];
            el.focus();
            // React / framework-safe value setter
            var nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value') ||
                Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value');
            if (nativeSetter && nativeSetter.set) {
                nativeSetter.set.call(el, '');
            } else {
                el.value = '';
            }
            el.dispatchEvent(new Event('input', {bubbles:true}));
        """, el)
        el.clear()
        if slow:
            for ch in value:
                el.send_keys(ch)
                time.sleep(random.uniform(0.04, 0.1))
        else:
            el.send_keys(value)
        driver.execute_script("""
            var el = arguments[0];
            ['input','change','keyup','blur'].forEach(function(name) {
                el.dispatchEvent(new Event(name, {bubbles:true, cancelable:true}));
            });
        """, el)
        time.sleep(0.4)
        return True
    except Exception as e:
        return False


def prefill_form_fields_via_selenium(driver, product, zip_code, city_name, cl_email):
    """
    Fill all form fields via Selenium with proper JS event dispatching.
    This triggers CL's client-side validation (including the postal ZIP check)
    so the server accepts the submission.
    Returns True if the postal field was successfully filled.
    """
    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    desc  = (product.get("description") or (
        f"{title} in excellent condition. Well maintained and ready for a new home. "
        f"Priced to sell. Local pickup preferred. Message for details.")).strip()
    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "")
    try:
        price_f = float(_pr) if _pr else 1.0
        price_val = str(int(price_f)) if price_f == int(price_f) else str(price_f)
    except Exception:
        price_val = "1"

    postal_filled = False

    # ── Postal / ZIP field (most critical) ──────────────────────────────
    postal_selectors = [
        "input[name='postal']",
        "input.postal",
        "input[id*='postal']",
        "input[placeholder*='zip' i]",
        "input[placeholder*='postal' i]",
        "input[type='text'][name*='zip' i]",
    ]
    for sel in postal_selectors:
        try:
            el = WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            if _selenium_fill_field(driver, el, zip_code, slow=True):
                actual = el.get_attribute("value") or ""
                print(f"  [prefill] postal '{sel}' → '{actual}'")
                if actual.strip() == zip_code.strip():
                    postal_filled = True
                    break
        except Exception:
            pass

    if not postal_filled:
        # Last attempt: find by XPath text label proximity
        try:
            el = driver.find_element(By.XPATH,
                "//label[contains(translate(.,'ZIP','zip'),'zip') or "
                "contains(translate(.,'POSTAL','postal'),'postal')]"
                "/following-sibling::input | "
                "//label[contains(translate(.,'ZIP','zip'),'zip') or "
                "contains(translate(.,'POSTAL','postal'),'postal')]"
                "/..//input")
            if _selenium_fill_field(driver, el, zip_code, slow=True):
                print(f"  [prefill] postal via XPath label → '{el.get_attribute('value')}'")
                postal_filled = True
        except Exception:
            pass

    if not postal_filled:
        print(f"  [prefill] WARNING: Could not fill postal field — will rely on direct POST")

    # ── Title ──
    for sel in ["input[name='PostingTitle']", "#PostingTitle", "input[name='title']"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            _selenium_fill_field(driver, el, title)
            break
        except Exception:
            pass

    # ── Body / description ──
    for sel in ["textarea[name='PostingBody']", "#PostingBody", "textarea[name='body']"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            _selenium_fill_field(driver, el, desc)
            break
        except Exception:
            pass

    # ── Price ──
    for sel in ["input[name='price']", "#price", "input[name='AskingPrice']"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            _selenium_fill_field(driver, el, price_val)
            break
        except Exception:
            pass

    # ── Geographic area ──
    for sel in ["input[name='geographic_area']", "#geographic_area"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            _selenium_fill_field(driver, el, city_name)
            break
        except Exception:
            pass

    time.sleep(1.5)  # Give CL's JS time to react to all events
    return postal_filled


def submit_form_via_requests(driver, session, product, zip_code, city_name, cl_email):
    """
    Extract the form from the current page and POST it directly via requests.
    Returns the URL we land on after submission.
    """
    current_url = driver.current_url
    print(f"  [direct-post] Extracting form from: {current_url}")

    # Extract ALL form fields as they currently exist in the DOM
    # (postal should already be filled by prefill_form_fields_via_selenium)
    form_data_raw = driver.execute_script("""
        var form = document.getElementById('postingForm');
        if (!form) return null;
        var data = [];
        form.querySelectorAll('input, textarea, select').forEach(function(el) {
            if (!el.name) return;
            if (el.type === 'checkbox' || el.type === 'radio') {
                if (el.checked) data.push([el.name, el.value]);
            } else {
                data.push([el.name, el.value || '']);
            }
        });
        // Also grab the form action
        data.push(['__form_action__', form.action || '']);
        return data;
    """)

    if not form_data_raw:
        print("  [direct-post] ✗ Could not extract form data")
        return None

    # Convert to dict, keeping last value for duplicates (except checkboxes)
    form_action = current_url
    form_dict = {}
    for pair in form_data_raw:
        name, value = pair[0], pair[1]
        if name == '__form_action__':
            if value:
                form_action = value
        else:
            form_dict[name] = value

    print(f"  [direct-post] Extracted {len(form_dict)} fields, action={form_action}")

    # Resolve values
    title = (product.get("title") or product.get("name") or "Quality Item For Sale").strip()
    description = (product.get("description") or (
        f"{title} in excellent condition. A unique piece perfect for collectors "
        f"and enthusiasts. Well maintained and ready for a new home. "
        f"Priced to sell. Local pickup preferred. Message for more details.")).strip()
    _pr = str(product.get("price", "")).strip().replace("$", "").replace(",", "")
    try:
        price_f = float(_pr) if _pr else 1.0
        price = str(int(price_f)) if price_f == int(price_f) else str(price_f)
    except Exception:
        price = "1"

    # Override with our values — postal set via ALL known CL field names
    form_dict["PostingTitle"]    = title
    form_dict["PostingBody"]     = description
    form_dict["postal"]          = zip_code
    form_dict["postal_code"]     = zip_code
    form_dict["zip"]             = zip_code
    form_dict["geographic_area"] = city_name
    form_dict["city"]            = city_name
    if cl_email:
        form_dict["FromEMail"] = cl_email

    # Set price in whichever field exists
    for price_field in ["price", "AskingPrice", "AskPrice"]:
        if price_field in form_dict:
            form_dict[price_field] = price
            break

    # Ensure required checkboxes are present
    for cb in ["crypto_currency_ok", "delivery_available", "see_my_other",
               "show_phone_ok", "contact_phone_ok", "contact_text_ok",
               "show_address_ok", "save_contact_preferences"]:
        if cb not in form_dict:
            form_dict[cb] = "1"

    # Add step marker so CL's server knows which step we're submitting
    form_dict["s"] = "edit"

    # Log ALL fields we're sending for debugging
    print(f"  [direct-post] Posting to: {form_action}")
    print(f"  [direct-post] Title={form_dict.get('PostingTitle','')[:30]}")
    print(f"  [direct-post] postal={form_dict.get('postal','')}")
    print(f"  [direct-post] postal_code={form_dict.get('postal_code','')}")
    print(f"  [direct-post] email={form_dict.get('FromEMail','')}")
    print(f"  [direct-post] geo={form_dict.get('geographic_area','')}")
    print(f"  [direct-post] cryptedStepCheck={form_dict.get('cryptedStepCheck','')[:20]}...")
    print(f"  [direct-post] ALL field names: {sorted(form_dict.keys())}")

    try:
        resp = session.post(
            form_action,
            data=form_dict,
            headers={
                "Referer":      current_url,
                "Origin":       "https://post.craigslist.org",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept":       "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            allow_redirects=True,
            timeout=30,
        )
        print(f"  [direct-post] Response: {resp.status_code}, final URL: {resp.url}")

        # Sniff the response for error clues before loading into Selenium
        resp_text = resp.text
        import re
        if "ZIP" in resp_text or "postal" in resp_text.lower() or "zip" in resp_text.lower():
            zip_snippets = re.findall(r'.{0,80}(?:zip|postal|ZIP).{0,80}', resp_text, re.IGNORECASE)
            for s in zip_snippets[:5]:
                print(f"  [direct-post] ZIP hint: {s.strip()}")
        
        field_errors = re.findall(r"""name=["']([^"']+)["']""", resp_text)
        zip_fields = [f for f in field_errors if 'post' in f.lower() or 'zip' in f.lower() or 'code' in f.lower()]
        if zip_fields:
            print(f"  [direct-post] ZIP-related field names in response: {zip_fields[:10]}")

        # Load the response page into Selenium so we can continue normally
        driver.get(resp.url)
        time.sleep(3)
        return resp.url

    except Exception as e:
        print(f"  [direct-post] ✗ POST failed: {e}")
        return None


def fill_listing_details(driver, session, product: dict):
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "postingForm")))
    except TimeoutException:
        print(f"  ✗ postingForm never appeared. URL: {driver.current_url}")
        return False

    handle_captcha_if_present(driver)
    time.sleep(3)  # Let CL's JS fully initialize so cryptedStepCheck is populated

    # ── Resolve zip / city / email ───────────────────────────────────────────
    # Priority 1: explicit values from UI's Location Manager (sent in product dict)
    # Priority 2: per-product zip_code / postal_code fields
    # Priority 3: env var
    # Priority 4: lookup table based on CL_CITY
    _ZIPS = {
        "losangeles": "90025", "newyork": "10001", "chicago": "60601",
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
        "tampa": "33601", "sacramento": "95814", "kansascity": "64101",
        "charlotte": "28201", "richmond": "23219", "tucson": "85701",
        "fresno": "93701", "memphis": "38101", "jacksonville": "32099",
    }

    zip_code = (
        product.get("_location_zip") or
        product.get("zip_code") or
        product.get("postal_code") or
        os.environ.get("CL_ZIP", "")
    ).strip()
    if not zip_code:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        zip_code = _ZIPS.get(_ck, "90025")

    _CITY_NAMES = {
        "losangeles": "Los Angeles", "newyork": "New York", "chicago": "Chicago",
        "houston": "Houston", "phoenix": "Phoenix", "sfbay": "San Francisco",
        "sandiego": "San Diego", "seattle": "Seattle", "miami": "Miami",
        "dallas": "Dallas", "denver": "Denver", "atlanta": "Atlanta",
        "boston": "Boston", "portland": "Portland", "lasvegas": "Las Vegas",
        "nashville": "Nashville", "sacramento": "Sacramento", "tampa": "Tampa",
    }
    city_name = (
        product.get("_location_city") or
        os.environ.get("CL_CITY_NAME", "")
    ).strip()
    if not city_name:
        _ck = CL_CITY.lower().replace(" ", "").replace("-", "")
        city_name = _CITY_NAMES.get(_ck, CL_CITY.title())

    state = (product.get("_location_state") or "").strip()
    if state:
        print(f"  [location] State={state}, City={city_name}, ZIP={zip_code}")
    else:
        print(f"  [location] City={city_name}, ZIP={zip_code}")

    cl_email = (os.environ.get("CL_EMAIL") or
                product.get("contact_email") or product.get("email") or "").strip()

    # ── LAYER 1: Selenium fills fields + Selenium clicks submit ──────────────
    # Most reliable: CL's own JS runs full validation, including postal.
    print("  [submit] Layer 1: Selenium form fill + Selenium click submit")
    try:
        prefill_form_fields_via_selenium(driver, product, zip_code, city_name, cl_email)

        # Click the continue/submit button
        submit_btn = None
        for sel in [
            "button[type='submit'].go",
            "button.go.pickbutton",
            "button[type='submit']",
            "input[type='submit']",
        ]:
            try:
                submit_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                break
            except Exception:
                pass

        if submit_btn:
            driver.execute_script("arguments[0].click();", submit_btn)
            print("  [submit] Layer 1: Clicked submit via Selenium")

            # Wait to see if we advance off the edit page
            try:
                WebDriverWait(driver, 12).until(
                    lambda d: "s=edit" not in d.current_url)
                print(f"  [submit] Layer 1 ✓ Advanced past edit page → {driver.current_url}")
                return True
            except TimeoutException:
                print("  [submit] Layer 1: Still on edit page after Selenium submit → Layer 2")
        else:
            print("  [submit] Layer 1: No submit button found → Layer 2")
    except Exception as e:
        print(f"  [submit] Layer 1 error: {e}")

    # ── LAYER 2: Pre-fill postal via Selenium, then direct requests POST ─────
    # Extract the form AFTER Selenium has filled it (postal value is now in DOM).
    print("  [submit] Layer 2: Selenium pre-fill postal + requests POST")
    # Re-navigate to edit page if Selenium submit sent us somewhere weird
    if "s=edit" not in driver.current_url:
        # We may have advanced already — check
        if "s=images" in driver.current_url or "s=preview" in driver.current_url:
            return True  # Already past edit!

    # Refresh cookies right before the POST
    original_url = driver.current_url
    for cl_domain in [
        "https://accounts.craigslist.org",
        "https://post.craigslist.org",
        "https://www.craigslist.org",
    ]:
        try:
            driver.get(cl_domain)
            time.sleep(1.5)
            for cookie in driver.get_cookies():
                session.cookies.set(cookie["name"], cookie["value"])
        except Exception:
            pass
    driver.get(original_url)
    time.sleep(2)

    # Ensure postal is filled in the DOM before extraction
    prefill_form_fields_via_selenium(driver, product, zip_code, city_name, cl_email)
    time.sleep(1)

    result_url = submit_form_via_requests(
        driver, session, product, zip_code, city_name, cl_email)

    if result_url and "s=edit" not in result_url:
        print(f"  [submit] Layer 2 ✓ Form submitted successfully → {result_url}")
        return True

    print(f"  [submit] Layer 2 failed: {result_url}")

    # ── LAYER 3: Brute-force Selenium form fill → wait for success ───────────
    # If both previous layers failed, try typing in the form and submitting
    # directly through the browser as a final attempt.
    print("  [submit] Layer 3: Full Selenium form fill + submit (final attempt)")
    try:
        # Navigate back to edit page if needed
        if "s=edit" not in driver.current_url:
            driver.get(original_url)
            time.sleep(3)

        prefill_form_fields_via_selenium(driver, product, zip_code, city_name, cl_email)
        human_delay(2, 3)

        # Try every possible submit selector
        submitted = False
        for sel in [
            "button[type='submit']", "input[type='submit']",
            "button.go", "button[class*='submit']",
        ]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(8)
                if "s=edit" not in driver.current_url:
                    print(f"  [submit] Layer 3 ✓ → {driver.current_url}")
                    submitted = True
                    break
            except Exception:
                pass

        if submitted:
            return True

    except Exception as e:
        print(f"  [submit] Layer 3 error: {e}")

    # All layers failed — log server errors for diagnosis
    print("  ✗ All submission layers failed.")
    try:
        errs = [e.text.strip() for e in driver.find_elements(
            By.CSS_SELECTOR, ".notices li, .err, .error, span.notice, .warning"
        ) if e.text.strip() and len(e.text.strip()) > 3]
        if errs:
            print("  [server errors]:")
            for et in sorted(set(errs)):
                print(f"    → {et[:120]}")
    except Exception:
        pass
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

def post_product(driver, session, ad_name, product):
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

    # Refresh session cookies right before submission from all CL domains
    original_url = driver.current_url
    for cl_domain in [
        "https://accounts.craigslist.org",
        "https://post.craigslist.org",
        "https://www.craigslist.org",
    ]:
        try:
            driver.get(cl_domain)
            time.sleep(1.5)
            for cookie in driver.get_cookies():
                session.cookies.set(cookie["name"], cookie["value"])
        except Exception:
            pass
    driver.get(original_url)
    time.sleep(2)
    fresh_names = [c.name for c in session.cookies]
    print(f"  [cookies] Fresh session has {len(fresh_names)} cookies: {sorted(fresh_names)}")

    try:
        success = fill_listing_details(driver, session, product)
    except Exception as e:
        print(f"  ✗ fill_listing_details crashed: {e}")
        import traceback
        traceback.print_exc()
        return False

    if not success:
        return False

    # Wait for photo/publish step
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
        print("  ✗ Still on edit page after direct POST. Aborting.")
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
    email    = os.environ.get("CL_EMAIL")    or input("Enter Craigslist email: ").strip()
    password = os.environ.get("CL_PASSWORD") or input("Enter Craigslist password: ").strip()
    CL_CITY  = os.environ.get("CL_CITY", CL_CITY)
    _load_existing_listings()
    driver = make_driver()
    if not craigslist_login(driver, email, password):
        driver.quit()
        return

    # Build a requests session from Selenium's authenticated cookies
    session = get_selenium_cookies_as_requests_session(driver)

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
            ok = post_product(driver, session, ad_name, product)
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