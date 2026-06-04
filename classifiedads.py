"""
classifiedads.py  —  CLBlast automation module for ClassifiedAds.com
Follows the same structure as craigslist.py and adlandpro.py.

Anti-detection strategy:
  • Standard selenium webdriver with system Chromium (Railway) or local Chrome
  • Random human-like typing delays via send_keys_slow()
  • Random waits between actions (1–3 s)
  • Rotating User-Agent on each session start
  • 2captcha integration for reCAPTCHA / image CAPTCHAs
  • Persistent Chrome profile so cookies/sessions survive across runs
"""

import time
import json
import os
import random
import threading
import tempfile
import urllib.request
from datetime import datetime, timedelta

# ── Third-party ──────────────────────────────────────────────────────────────
from selenium import webdriver

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.common.action_chains import ActionChains

try:
    import twocaptcha                              # pip install 2captcha-python
    from twocaptcha import TwoCaptcha
    CAPTCHA_SOLVER_AVAILABLE = True
except ImportError:
    CAPTCHA_SOLVER_AVAILABLE = False
    print("⚠  2captcha-python not installed. CAPTCHA auto-solve disabled.")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  — edit these before running
# ─────────────────────────────────────────────────────────────────────────────
TWO_CAPTCHA_API_KEY = os.environ.get("TWO_CAPTCHA_KEY", "YOUR_2CAPTCHA_API_KEY")
CHROME_PROFILE_DIR  = os.path.join(os.path.expanduser("~"), ".clblast_chrome_ca")
BASE_URL            = "https://www.classifiedads.com"
POST_AD_URL         = f"{BASE_URL}/post_ad"
LOGIN_URL           = f"{BASE_URL}/users/sign_in"

# Detect Railway / headless cloud environment (no terminal available)
IS_RAILWAY = any(os.path.exists(p) for p in [
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/run/current-system/sw/bin/chromium"
])

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY MAPPING  —  ClassifiedAds.com visible category labels
# ─────────────────────────────────────────────────────────────────────────────
CATEGORY_MAPPING = {
    "antiques":           "Antiques & Collectibles",
    "appliances":         "Appliances",
    "art":                "Arts & Crafts",
    "paintings":          "Arts & Crafts",
    "art supplies":       "Arts & Crafts",
    "automotive":         "Auto Parts & Accessories",
    "auto parts":         "Auto Parts & Accessories",
    "tires":              "Auto Parts & Accessories",
    "bicycles":           "Bicycles",
    "bicycle parts":      "Bicycles",
    "boats":              "Boats",
    "boat parts":         "Boats",
    "books":              "Books, Movies & Music",
    "novels":             "Books, Movies & Music",
    "magazines":          "Books, Movies & Music",
    "cds":                "Books, Movies & Music",
    "dvds":               "Books, Movies & Music",
    "business":           "Business & Office",
    "office furniture":   "Business & Office",
    "cars":               "Cars & Trucks",
    "trucks":             "Cars & Trucks",
    "vans":               "Cars & Trucks",
    "suvs":               "Cars & Trucks",
    "pickup trucks":      "Cars & Trucks",
    "phones":             "Cell Phones",
    "cell phones":        "Cell Phones",
    "clothing":           "Clothing & Accessories",
    "fashion":            "Clothing & Accessories",
    "women's clothing":   "Clothing & Accessories",
    "men's clothing":     "Clothing & Accessories",
    "collectibles":       "Antiques & Collectibles",
    "coins":              "Antiques & Collectibles",
    "computers":          "Computers & Laptops",
    "desktops":           "Computers & Laptops",
    "laptops":            "Computers & Laptops",
    "tablets":            "Computers & Laptops",
    "computer parts":     "Computer Parts & Accessories",
    "electronics":        "Electronics",
    "cameras":            "Electronics",
    "farm":               "Farm & Garden",
    "garden":             "Farm & Garden",
    "home & garden":      "Farm & Garden",
    "lawn care":          "Farm & Garden",
    "free":               "Free Stuff",
    "furniture":          "Furniture",
    "home furniture":     "Furniture",
    "chairs":             "Furniture",
    "tables":             "Furniture",
    "sofas":              "Furniture",
    "dressers":           "Furniture",
    "garage":             "Garage Sales",
    "health":             "Health & Beauty",
    "beauty":             "Health & Beauty",
    "skin":               "Health & Beauty",
    "heavy duty":         "Heavy Equipment",
    "household":          "Household Items",
    "jewelry":            "Jewelry & Watches",
    "watches":            "Jewelry & Watches",
    "bracelets":          "Jewelry & Watches",
    "necklaces":          "Jewelry & Watches",
    "rings":              "Jewelry & Watches",
    "earrings":           "Jewelry & Watches",
    "motorcycles":        "Motorcycles",
    "motorcycle parts":   "Motorcycles",
    "instruments":        "Musical Instruments",
    "musical instruments":"Musical Instruments",
    "photos":             "Photography",
    "cameras dslr":       "Photography",
    "rvs":                "RVs & Campers",
    "trailers":           "Trailers",
    "sporting goods":     "Sporting Goods",
    "sports":             "Sporting Goods",
    "tickets":            "Tickets & Events",
    "tools":              "Tools & Hardware",
    "toys":               "Toys & Games",
    "board games":        "Toys & Games",
    "video games":        "Video Games & Consoles",
    "game consoles":      "Video Games & Consoles",
    "wanted":             "Wanted",
    "miscellaneous":      "Miscellaneous",
    # ── Products.json category keys ──────────────────────────────────
    "men":                "Clothing & Accessories",
    "women":              "Clothing & Accessories",
    "accessories":        "Clothing & Accessories",
    "artandcollectibles": "Antiques & Collectibles",
    "art and collectibles":"Antiques & Collectibles",
    "homeandappliances":  "Household Items",
    "home and appliances":"Household Items",
    "entertainment":      "Books, Movies & Music",
}


def get_ca_category(category_name: str) -> str:
    key = category_name.lower().strip().replace(" ", "")
    # Try exact match with spaces stripped (handles 'homeandappliances', 'artandcollectibles')
    for k, v in CATEGORY_MAPPING.items():
        if k.replace(" ", "") == key:
            return v
    # Try substring match with original spacing
    key_spaced = category_name.lower().strip()
    for k, v in CATEGORY_MAPPING.items():
        if k in key_spaced or key_spaced in k:
            return v
    return "Miscellaneous"  # safe fallback


# ─────────────────────────────────────────────────────────────────────────────
# POSTED LISTINGS TRACKER  (shared with clblast.html via posted_listings.json)
# ─────────────────────────────────────────────────────────────────────────────
posted_listings: dict = {}   # ad_name -> {url, post_time, visitors, platform}

# Lock protecting concurrent writes to posted_listings.json (posting thread + analytics thread)
_listings_lock = threading.Lock()

LISTINGS_JSON = "posted_listings.json"


def _load_existing_listings():
    """Merge posted_listings.json from disk so other platforms' entries survive."""
    global posted_listings
    if not os.path.exists(LISTINGS_JSON):
        return
    try:
        with open(LISTINGS_JSON) as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in posted_listings:          # don't overwrite current session
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
    """Persist the merged posted_listings to disk so the UI can read it.
    Uses a threading.Lock + atomic rename to prevent race conditions.
    """
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
        os.replace(tmp_path, LISTINGS_JSON)  # atomic on both Windows and POSIX


# ─────────────────────────────────────────────────────────────────────────────
# DRIVER FACTORY  —  undetected-chromedriver with persistent profile
# ─────────────────────────────────────────────────────────────────────────────
def _find_binary(names: list, fallback_paths: list) -> str | None:
    """Search PATH, /usr/local/bin, common Nix paths, and the Nix store for a binary."""
    import shutil, subprocess

    # 1. Standard PATH lookup
    for name in names:
        path = shutil.which(name)
        if path:
            return path

    # 2. Shell 'which' — catches Nix profile paths not propagated to os.environ["PATH"]
    for name in names:
        try:
            result = subprocess.run(["which", name], capture_output=True, text=True, timeout=3)
            p = result.stdout.strip()
            if p and os.path.exists(p):
                return p
        except Exception:
            pass

    # 3. Hardcoded fallback paths (including Procfile symlink target /usr/local/bin)
    extended = ["/usr/local/bin/" + n for n in names] + fallback_paths
    for p in extended:
        if os.path.exists(p):
            return p

    # 4. Last resort: search the entire Nix store (slow but guaranteed)
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
    from selenium.webdriver.chrome.service import Service

    os.environ["SE_MANAGER_PATH"] = ""
    os.environ["WDM_SKIP_DOWNLOAD"] = "1"

    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")

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
        raise RuntimeError("chromedriver not found. Check Dockerfile has: RUN apt-get install -y chromium chromium-driver")

    print(f"  [driver] Using chromedriver: {chromedriver_bin}")
    service = Service(executable_path=chromedriver_bin)
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver


# ─────────────────────────────────────────────────────────────────────────────
# HUMAN-LIKE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def human_delay(lo: float = 0.8, hi: float = 2.5):
    time.sleep(random.uniform(lo, hi))


def send_keys_slow(element, text: str, lo: float = 0.05, hi: float = 0.18):
    """Type text character-by-character with random inter-key delays."""
    element.clear()
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(lo, hi))


def safe_click(driver, element):
    """Scroll into view then click, with fallback to JS click."""
    try:
        ActionChains(driver).move_to_element(element).pause(random.uniform(0.3, 0.8)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    human_delay(0.5, 1.5)


# ─────────────────────────────────────────────────────────────────────────────
# CAPTCHA SOLVER
# ─────────────────────────────────────────────────────────────────────────────
def solve_recaptcha_v2(driver) -> bool:
    """
    Attempt to solve a reCAPTCHA v2 on the current page using 2captcha.
    Returns True if solved, False otherwise.
    """
    if not CAPTCHA_SOLVER_AVAILABLE:
        print("⚠  CAPTCHA detected but solver not available.")
        if IS_RAILWAY:
            print("  Railway mode: skipping manual CAPTCHA solve.")
            return False
        input("   Press ENTER after solving the CAPTCHA to continue...")
        return True

    try:
        # Find sitekey
        iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        src = iframe.get_attribute("src")
        sitekey = [p.split("=")[1] for p in src.split("&") if "k=" in p][0]
        page_url = driver.current_url

        solver = TwoCaptcha(TWO_CAPTCHA_API_KEY)
        print("  Sending CAPTCHA to 2captcha…")
        result = solver.recaptcha(sitekey=sitekey, url=page_url)
        token = result["code"]

        # Inject the token
        driver.execute_script(
            "document.getElementById('g-recaptcha-response').innerHTML = arguments[0];",
            token
        )
        # Also trigger the callback if present
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
        print(f"  CAPTCHA solve failed: {e}.")
        if IS_RAILWAY:
            print("  Railway mode: skipping manual CAPTCHA.")
            return False
        input("  Please solve the CAPTCHA manually, then press ENTER…")
        return True


def handle_captcha_if_present(driver):
    """Check for reCAPTCHA or image CAPTCHA and solve."""
    try:
        driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        print("  reCAPTCHA detected.")
        solve_recaptcha_v2(driver)
        human_delay(1, 2)
    except NoSuchElementException:
        pass   # No CAPTCHA found

    # Cloudflare challenge page
    if "Just a moment" in driver.title or "cf-browser-verification" in driver.page_source:
        print("  Cloudflare challenge detected — waiting for auto-pass…")
        time.sleep(8)
        if "Just a moment" in driver.title:
            if IS_RAILWAY:
                print("  Cloudflare not cleared on Railway. Continuing anyway.")
            else:
                input("  Cloudflare not cleared. Solve manually then press ENTER…")


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────────────────────────────────────
def classifiedads_login(driver, email: str, password: str) -> bool:
    """Log in to ClassifiedAds.com. Returns True on success."""
    driver.get(LOGIN_URL)
    human_delay(2, 4)
    handle_captcha_if_present(driver)

    try:
        email_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH,
                "//input[@name='email' or @id='email' or @type='email' "
                "or @name='user[email]' or @id='user_email']"
            ))
        )
        send_keys_slow(email_field, email)
        human_delay()

        pw_field = driver.find_element(By.XPATH,
            "//input[@type='password' or @name='password' "
            "or @name='user[password]' or @id='user_password']"
        )
        send_keys_slow(pw_field, password)
        human_delay()

        login_btn = driver.find_element(By.XPATH,
            "//button[@type='submit'] | //input[@type='submit'] | "
            "//button[contains(translate(text(),'LOGIN','login'),'log')] | "
            "//input[@value='Log in'] | //input[@value='Sign in']"
        )
        safe_click(driver, login_btn)

        WebDriverWait(driver, 15).until_not(EC.url_contains("sign_in"))
        handle_captcha_if_present(driver)
        print("Logged in to ClassifiedAds.com ✓")
        return True

    except (TimeoutException, NoSuchElementException) as e:
        print(f"Login failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SELECT CATEGORY
# ─────────────────────────────────────────────────────────────────────────────
def select_category(driver, category_name: str):
    """
    ClassifiedAds.com uses a two-step category picker (main → sub).
    We try dropdown first, then clickable links/buttons.
    """
    target = get_ca_category(category_name)
    print(f"  Selecting category: {target}")
    human_delay(1, 2)

    # ── Try <select> dropdown ────────────────────────────────────────────────
    for sel_xpath in [
        "//select[@name='category' or @id='category' or contains(@id,'cat')]",
        "//select[contains(@name,'category')]",
    ]:
        try:
            sel_el = driver.find_element(By.XPATH, sel_xpath)
            sel = Select(sel_el)
            try:
                sel.select_by_visible_text(target)
                print(f"  Category set via dropdown ✓")
                human_delay()
                return
            except Exception:
                # Try partial match
                for opt in sel.options:
                    if target.lower() in opt.text.lower():
                        opt.click()
                        print(f"  Category partial match '{opt.text}' ✓")
                        human_delay()
                        return
        except NoSuchElementException:
            continue

    # ── Try clickable link / button ──────────────────────────────────────────
    try:
        link = driver.find_element(By.XPATH,
            f"//a[contains(text(),'{target}')] | //li[contains(text(),'{target}')]"
        )
        safe_click(driver, link)
        print(f"  Category clicked ✓")
        return
    except NoSuchElementException:
        pass

    print(f"  ⚠  Could not select category '{target}'. Leaving as default.")


# ─────────────────────────────────────────────────────────────────────────────
# FILL LISTING DETAILS
# ─────────────────────────────────────────────────────────────────────────────
def fill_listing_details(driver, product: dict):
    """Fill in the post-ad form fields."""
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "form")))
    handle_captcha_if_present(driver)

    # Title
    # ISSUE 2 FIX: Use title with fallback to name field
    product_title = product.get("title") or product.get("name", "No Title")
    for xp in ["//input[@id='title']", "//input[@name='title']",
                "//input[contains(@placeholder,'Title')]",
                "//input[@name='ad[title]']", "//input[@id='ad_title']"]:
        try:
            f = driver.find_element(By.XPATH, xp)
            send_keys_slow(f, product_title)
            human_delay(0.5, 1.2)
            break
        except NoSuchElementException:
            continue
    else:
        print("  ⚠  Title field not found.")

    # Description
    for xp in ["//textarea[@id='description']", "//textarea[@name='description']",
                "//textarea[contains(@placeholder,'escription')]",
                "//textarea[@name='ad[description]']", "//textarea[@id='ad_description']"]:
        try:
            f = driver.find_element(By.XPATH, xp)
            send_keys_slow(f, product.get("description", ""), lo=0.02, hi=0.08)
            human_delay(0.5, 1.2)
            break
        except NoSuchElementException:
            continue
    else:
        print("  ⚠  Description field not found.")

    # Price
    for xp in ["//input[@id='price']", "//input[@name='price']",
                "//input[contains(@placeholder,'rice')]",
                "//input[@name='ad[price]']", "//input[@id='ad_price']"]:
        try:
            f = driver.find_element(By.XPATH, xp)
            send_keys_slow(f, str(product.get("price", "")))
            human_delay(0.5, 1.2)
            break
        except NoSuchElementException:
            continue
    else:
        print("  ⚠  Price field not found.")

    # Location / zip
    if product.get("zip_code"):
        for xp in ["//input[@id='zip_code']", "//input[@name='zip_code']",
                   "//input[@name='location']", "//input[@id='location']",
                   "//input[contains(@placeholder,'ocation')]",
                   "//input[contains(@placeholder,'ip')]"]:
            try:
                f = driver.find_element(By.XPATH, xp)
                send_keys_slow(f, product["zip_code"])
                human_delay(0.5, 1.2)
                break
            except NoSuchElementException:
                continue

    # Category
    select_category(driver, product.get("category", ""))
    human_delay(0.8, 1.5)
    print("  Listing details filled ✓")


# ─────────────────────────────────────────────────────────────────────────────
# PHOTO UPLOAD
# ─────────────────────────────────────────────────────────────────────────────
def upload_photos(driver, product: dict):
    """Upload photos to the listing form."""
    # ISSUE 3 FIX: Support both URL and local file paths for photos
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
        print("  ⚠  No valid photos to upload. Skipping.")
        return

    try:
        file_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
        )
        # Some sites only accept one file at a time
        for path in valid:
            file_input.send_keys(os.path.abspath(path))
            human_delay(1.5, 3)
        print(f"  Uploaded {len(valid)} photo(s) ✓")
    except (TimeoutException, NoSuchElementException):
        print("  ⚠  File input not found. Skipping photos.")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass

    human_delay(3, 6)


# ─────────────────────────────────────────────────────────────────────────────
# PUBLISH
# ─────────────────────────────────────────────────────────────────────────────
def publish_listing(driver, ad_name: str):
    """Click the submit/post button and store the listing URL."""
    handle_captcha_if_present(driver)

    try:
        publish_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[contains(translate(text(),'POSTSUBMITPUBLISH','postsubmitpublish'),'post')] | "
                "//button[contains(translate(text(),'POSTSUBMITPUBLISH','postsubmitpublish'),'submit')] | "
                "//button[contains(translate(text(),'POSTSUBMITPUBLISH','postsubmitpublish'),'publish')] | "
                "//input[@type='submit'] | "
                "//input[@value='Post Ad'] | //input[@value='Submit'] | "
                "//button[@type='submit']"
            ))
        )
        safe_click(driver, publish_btn)
        print("  Publish button clicked ✓")

        human_delay(6, 10)
        handle_captcha_if_present(driver)

        listing_url = driver.current_url
        print(f"  Listing URL: {listing_url}")

        posted_listings[ad_name] = {
            "url": listing_url,
            "post_time": datetime.now(),
            "visitors": 0,
            "platform": "ClassifiedAds",
        }
        _save_listings()

    except TimeoutException:
        print(f"  ⚠  Publish button not found for '{ad_name}'.")


# ─────────────────────────────────────────────────────────────────────────────
# POST PRODUCT  (main posting flow)
# ─────────────────────────────────────────────────────────────────────────────
def post_product(driver, ad_name: str, product: dict) -> bool:
    """Full posting flow for one product on ClassifiedAds.com."""
    driver.get(POST_AD_URL)
    human_delay(2, 4)
    handle_captcha_if_present(driver)

    try:
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "form")))
    except TimeoutException:
        print("  ✗ Post-ad form did not load.")
        return False

    fill_listing_details(driver, product)
    upload_photos(driver, product)
    publish_listing(driver, ad_name)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS POLLING  (background thread)
# ─────────────────────────────────────────────────────────────────────────────
def update_ad_analytics_periodically():
    """Poll each listing page every 5 minutes for view counts."""
    # On Railway/cloud, skip live Chrome-based analytics to avoid memory crashes.
    if IS_RAILWAY:
        print("[CA] Analytics thread disabled on Railway (memory constraint). Skipping.")
        return
    while True:
        print("\n[CA] Refreshing analytics…")
        for ad_name, listing in list(posted_listings.items()):
            if not listing.get("url") or listing.get("platform") != "ClassifiedAds":
                continue
            tmp = None
            try:
                tmp = make_driver()
                tmp.get(listing["url"])
                human_delay(2, 4)

                # Generic view-count finder
                views_el = WebDriverWait(tmp, 15).until(
                    EC.presence_of_element_located((By.XPATH,
                        "//*[contains(translate(text(),'VIEWS HITS','views hits'),'views') "
                        "or contains(translate(text(),'VIEWS HITS','views hits'),'hits')]"
                    ))
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


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    # Accept credentials from env vars (set by server.py) or fall back to interactive
    email    = os.environ.get("CL_EMAIL")    or input("Enter ClassifiedAds.com email: ").strip()
    password = os.environ.get("CL_PASSWORD") or input("Enter ClassifiedAds.com password: ").strip()

    # Merge any listings already written by other platform scripts
    _load_existing_listings()

    driver = make_driver()

    if not classifiedads_login(driver, email, password):
        driver.quit()
        return

    # Read PRODUCTS_FILE env var so server.py can pass a filtered subset
    products_file = os.environ.get("PRODUCTS_FILE", "products.json")
    if not os.path.exists(products_file):
        print(f"✗ {products_file} not found.")
        driver.quit()
        return

    with open(products_file) as f:
        products = json.load(f)

    threading.Thread(target=update_ad_analytics_periodically, daemon=True).start()

    for product in products:
        # ISSUE 2 FIX: Use title with fallback to name field
        product_title = product.get("title") or product.get("name", "No Title")
        ad_name = f"CA_{product_title}"
        print(f"\nPosting: {product_title}")
        ok = post_product(driver, ad_name, product)
        print("  ✓ Posted" if ok else "  ✗ Failed")
        human_delay(3, 7)   # Polite gap between posts

    print("\nAll ClassifiedAds products processed.")
    driver.quit()


if __name__ == "__main__":
    main()
