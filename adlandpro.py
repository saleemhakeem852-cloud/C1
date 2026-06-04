"""
adlandpro.py  —  CLBlast automation module for AdLandPro.com
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

try:
    from twocaptcha import TwoCaptcha
    CAPTCHA_SOLVER_AVAILABLE = True
except ImportError:
    CAPTCHA_SOLVER_AVAILABLE = False

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
TWO_CAPTCHA_API_KEY = os.environ.get("TWO_CAPTCHA_KEY", "YOUR_2CAPTCHA_API_KEY")
CHROME_PROFILE_DIR  = os.path.join(os.path.expanduser("~"), ".clblast_chrome_ap")
LISTINGS_JSON       = "posted_listings.json"

# Detect Railway / headless cloud environment (no terminal available)
IS_RAILWAY = any(os.path.exists(p) for p in [
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/run/current-system/sw/bin/chromium"
])

# ─────────────────────────────────────────────────────────────
# CATEGORY MAPPING
# ─────────────────────────────────────────────────────────────
CATEGORY_MAPPING = {
    "antiques": "Antiques", "appliances": "Appliances",
    "art": "Arts & Crafts", "paintings": "Arts & Crafts",
    "art supplies": "Arts & Crafts", "automotive": "Automotive",
    "auto parts": "Automotive", "bicycles": "Bicycles",
    "bicycle parts": "Bicycles", "boats": "Boats & Watercraft",
    "boat parts": "Boats & Watercraft", "books": "Books & Magazines",
    "novels": "Books & Magazines", "magazines": "Books & Magazines",
    "business": "Business", "cars": "Cars & Trucks",
    "trucks": "Cars & Trucks", "vans": "Cars & Trucks",
    "suvs": "Cars & Trucks", "clothing": "Clothing & Accessories",
    "fashion": "Clothing & Accessories",
    "women's clothing": "Clothing & Accessories",
    "men's clothing": "Clothing & Accessories",
    "collectibles": "Collectibles", "coins": "Collectibles",
    "computers": "Computers", "desktops": "Computers",
    "laptops": "Computers", "tablets": "Computers",
    "computer parts": "Computer Parts",
    "electronics": "Electronics", "cameras": "Electronics",
    "phones": "Electronics", "cell phones": "Electronics",
    "furniture": "Furniture", "home furniture": "Furniture",
    "office furniture": "Furniture", "chairs": "Furniture",
    "tables": "Furniture", "sofas": "Furniture", "dressers": "Furniture",
    "health": "Health & Beauty", "beauty": "Health & Beauty",
    "skin": "Health & Beauty", "household": "Household Items",
    "home & garden": "Household Items", "jewelry": "Jewelry",
    "bracelets": "Jewelry", "necklaces": "Jewelry",
    "watches": "Jewelry", "rings": "Jewelry", "earrings": "Jewelry",
    "chains": "Jewelry", "motorcycles": "Motorcycles",
    "motorcycle parts": "Motorcycles",
    "instruments": "Musical Instruments",
    "musical instruments": "Musical Instruments",
    "sporting goods": "Sporting Goods", "sports": "Sporting Goods",
    "tools": "Tools & Hardware", "toys": "Toys & Games",
    "board games": "Toys & Games", "video games": "Video Games",
    "game consoles": "Video Games", "wanted": "Wanted",
    "free": "Free Stuff", "miscellaneous": "General",
    "tickets": "Tickets", "rvs": "RVs & Campers",
    "trailers": "Trailers",
    # ── Products.json category keys ──────────────────────────────────
    "men": "Clothing & Accessories",
    "women": "Clothing & Accessories",
    "accessories": "Clothing & Accessories",
    "artandcollectibles": "Collectibles",
    "art and collectibles": "Collectibles",
    "homeandappliances": "Household Items",
    "home and appliances": "Household Items",
    "entertainment": "General",
}


def get_adlandpro_category(category_name: str) -> str:
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
    return "General"  # safe fallback


# ─────────────────────────────────────────────────────────────
# POSTED LISTINGS TRACKER
# ─────────────────────────────────────────────────────────────
posted_listings: dict = {}

# Lock protecting concurrent writes to posted_listings.json (posting thread + analytics thread)
_listings_lock = threading.Lock()


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


# ─────────────────────────────────────────────────────────────
# DRIVER FACTORY
# ─────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────
# HUMAN-LIKE HELPERS
# ─────────────────────────────────────────────────────────────
def human_delay(lo: float = 0.8, hi: float = 2.5):
    time.sleep(random.uniform(lo, hi))


def send_keys_slow(element, text: str):
    element.clear()
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.05, 0.18))


def safe_click(driver, element):
    try:
        ActionChains(driver).move_to_element(element).pause(random.uniform(0.3, 0.8)).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    human_delay(0.5, 1.5)


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
        iframe  = driver.find_element(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        src     = iframe.get_attribute("src")
        sitekey = [p.split("=")[1] for p in src.split("&") if "k=" in p][0]
        solver  = TwoCaptcha(TWO_CAPTCHA_API_KEY)
        print("  Sending CAPTCHA to 2captcha…")
        result  = solver.recaptcha(sitekey=sitekey, url=driver.current_url)
        token   = result["code"]
        # Inject token and fire the callback so the site accepts it
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
def adlandpro_login(driver, email: str, password: str) -> bool:
    driver.get("https://www.adlandpro.com/login.aspx")
    human_delay(2, 4)
    handle_captcha_if_present(driver)

    try:
        email_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH,
                "//input[@name='email' or @id='email' or @name='Email' or @id='Email' "
                "or @name='txtEmail' or @id='txtEmail' or @type='email']"
            ))
        )
        send_keys_slow(email_field, email)
        human_delay()

        pw_field = driver.find_element(By.XPATH,
            "//input[@name='password' or @id='password' or @name='Password' "
            "or @id='Password' or @name='txtPassword' or @id='txtPassword' or @type='password']"
        )
        send_keys_slow(pw_field, password)
        human_delay()

        login_btn = driver.find_element(By.XPATH,
            "//button[@type='submit'] | //input[@type='submit'] | "
            "//input[@value='Login'] | //input[@value='Log In'] | "
            "//button[contains(text(),'Login')] | //button[contains(text(),'Log In')]"
        )
        safe_click(driver, login_btn)

        WebDriverWait(driver, 15).until_not(EC.url_contains("login"))
        handle_captcha_if_present(driver)
        print("Logged in to Adland Pro ✓")
        return True

    except (TimeoutException, NoSuchElementException) as e:
        print(f"Login failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# FILL LISTING DETAILS
# ─────────────────────────────────────────────────────────────
def fill_listing_details(driver, product: dict):
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "form")))
    handle_captcha_if_present(driver)

    # Title
    # ISSUE 2 FIX: Use title with fallback to name field
    product_title = product.get("title") or product.get("name", "No Title")
    for xp in ["//input[@name='title']", "//input[@id='title']",
                "//input[@placeholder='Title']", "//input[@name='txtTitle']"]:
        try:
            f = driver.find_element(By.XPATH, xp)
            send_keys_slow(f, product_title)
            human_delay(0.5, 1)
            break
        except NoSuchElementException:
            continue
    else:
        print("  ⚠  Title field not found.")

    # Description
    for xp in ["//textarea[@name='description']", "//textarea[@id='description']",
                "//textarea[@name='txtDescription']"]:
        try:
            f = driver.find_element(By.XPATH, xp)
            send_keys_slow(f, product.get("description", ""))
            human_delay(0.5, 1)
            break
        except NoSuchElementException:
            continue
    else:
        print("  ⚠  Description field not found.")

    # Price
    for xp in ["//input[@name='price']", "//input[@id='price']",
                "//input[@name='txtPrice']"]:
        try:
            f = driver.find_element(By.XPATH, xp)
            send_keys_slow(f, str(product.get("price", "")))
            human_delay(0.5, 1)
            break
        except NoSuchElementException:
            continue

    # Zip
    if product.get("zip_code"):
        for xp in ["//input[@name='zip_code']", "//input[@id='zip_code']",
                   "//input[@name='location']"]:
            try:
                f = driver.find_element(By.XPATH, xp)
                send_keys_slow(f, product["zip_code"])
                human_delay(0.5, 1)
                break
            except NoSuchElementException:
                continue

    # Category
    category_text = get_adlandpro_category(product.get("category", ""))
    try:
        cat_sel = Select(driver.find_element(By.XPATH,
            "//select[@name='category' or @id='category' or @name='ddlCategory']"
        ))
        try:
            cat_sel.select_by_visible_text(category_text)
        except Exception:
            for opt in cat_sel.options:
                if category_text.lower() in opt.text.lower():
                    opt.click()
                    break
            else:
                cat_sel.select_by_index(1)
    except NoSuchElementException:
        print("  ⚠  Category dropdown not found.")
    human_delay(0.5, 1)
    print("  Listing details filled ✓")


# ─────────────────────────────────────────────────────────────
# PHOTO UPLOAD
# ─────────────────────────────────────────────────────────────
def upload_photos(driver, product: dict):
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
        print("  No valid photos to upload.")
        return

    try:
        fi = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
        )
        for path in valid:
            fi.send_keys(os.path.abspath(path))
            human_delay(1.5, 3)
        print(f"  Uploaded {len(valid)} photo(s) ✓")
    except (TimeoutException, NoSuchElementException) as e:
        print(f"  ⚠  Photo upload skipped: {e}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass
    human_delay(4, 7)


# ─────────────────────────────────────────────────────────────
# PUBLISH
# ─────────────────────────────────────────────────────────────
def publish_listing(driver, ad_name: str):
    handle_captcha_if_present(driver)
    try:
        pub = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[contains(text(),'Post Ad')] | //button[contains(text(),'Submit')] | "
                "//button[contains(text(),'Publish')] | //input[@type='submit'] | "
                "//input[@value='Post Ad'] | //input[@value='Submit'] | //button[@type='submit']"
            ))
        )
        safe_click(driver, pub)
        human_delay(6, 10)
        handle_captcha_if_present(driver)

        listing_url = driver.current_url
        print(f"  Published → {listing_url}")
        posted_listings[ad_name] = {
            "url": listing_url,
            "post_time": datetime.now(),
            "visitors": 0,
            "platform": "AdLandPro",
        }
        _save_listings()
    except TimeoutException:
        print(f"  ⚠  Publish button not found for '{ad_name}'.")


# ─────────────────────────────────────────────────────────────
# POST PRODUCT
# ─────────────────────────────────────────────────────────────
def post_product(driver, ad_name: str, product: dict) -> bool:
    driver.get("https://www.adlandpro.com/post_ad.aspx")
    human_delay(2, 4)
    handle_captcha_if_present(driver)

    try:
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "form")))
    except TimeoutException:
        print("  ✗ Post-ad form not loaded.")
        return False

    fill_listing_details(driver, product)
    upload_photos(driver, product)
    publish_listing(driver, ad_name)
    return True


# ─────────────────────────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────────────────────────
def update_ad_analytics_periodically():
    # On Railway/cloud, skip live Chrome-based analytics to avoid memory crashes.
    if IS_RAILWAY:
        print("[AP] Analytics thread disabled on Railway (memory constraint). Skipping.")
        return
    while True:
        print("\n[AP] Refreshing analytics…")
        for ad_name, listing in list(posted_listings.items()):
            if not listing.get("url") or listing.get("platform") != "AdLandPro":
                continue
            tmp = None
            try:
                tmp = make_driver()
                tmp.get(listing["url"])
                human_delay(2, 4)
                views_el = WebDriverWait(tmp, 15).until(
                    EC.presence_of_element_located((By.XPATH,
                        "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                        "'abcdefghijklmnopqrstuvwxyz'),'views') "
                        "or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                        "'abcdefghijklmnopqrstuvwxyz'),'hits')]"
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


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    # Accept credentials from env vars (set by server.py) or fall back to interactive
    email    = os.environ.get("CL_EMAIL")    or input("Enter Adland Pro email: ").strip()
    password = os.environ.get("CL_PASSWORD") or input("Enter Adland Pro password: ").strip()

    # Merge any listings already written by other platform scripts
    _load_existing_listings()

    driver = make_driver()

    if not adlandpro_login(driver, email, password):
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
        ad_name = f"AP_{product_title}"
        print(f"\nPosting: {product_title}")
        ok = post_product(driver, ad_name, product)
        print("  ✓ Posted" if ok else "  ✗ Failed")
        human_delay(3, 7)

    print("\nAll Adland Pro products processed.")
    driver.quit()


if __name__ == "__main__":
    main()
