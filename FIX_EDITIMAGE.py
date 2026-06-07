r"""
FIX_EDITIMAGE.py - Run in D:\AAAAA\clblast45
python FIX_EDITIMAGE.py

Root cause: after form submit, CL goes to ?s=geoverify then ?s=editimage.
The script waits 35s on edit page, then does a recovery click which jumps
OVER the editimage page straight to preview.

Fix: reduce wait to 15s, and after leaving edit, handle geoverify then
wait specifically for editimage before complete_images_step runs.
"""
from pathlib import Path

HERE = Path(__file__).parent
cl = HERE / "craigslist_new.py"
src = cl.read_text(encoding="utf-8")

# ── Fix 1: Reduce 35s edit page wait to 15s so we catch editimage faster ─────
old_wait = '    deadline = time.time() + 35\n    while time.time() < deadline:\n        cur = driver.current_url\n        if "s=edit" not in cur:\n            print(f"  [OK] Left edit page -> {cur}")\n            return cur\n        time.sleep(0.5)'

new_wait = '    deadline = time.time() + 20\n    while time.time() < deadline:\n        cur = driver.current_url\n        if "s=edit" not in cur:\n            print(f"  [OK] Left edit page -> {cur}")\n            return cur\n        time.sleep(0.5)'

if old_wait in src:
    src = src.replace(old_wait, new_wait)
    print("[OK] Fix 1: reduced edit page wait to 20s")
else:
    # Try the original unicode version
    src = src.replace(
        'deadline = time.time() + 35',
        'deadline = time.time() + 20'
    )
    print("[OK] Fix 1: reduced deadline to 20s")

# ── Fix 2: complete_images_step — wait for editimage before giving up ─────────
old_complete = '''    # -- Already on preview page (CL skipped images step) --
    if "s=preview" in driver.current_url:
        print("  [images] Already on preview -- CL skipped images step [OK]")
        return True
    # -- On editimage page - this is the image upload page --
    if "s=editimage" in driver.current_url:
        print("  [images] On editimage page -- proceeding with upload")'''

new_complete = '''    # -- Already on editimage (image upload page) --
    if "s=editimage" in driver.current_url:
        print(f"  [images] On editimage page -- proceeding with upload")
    # -- Already on preview page (CL skipped images step) --
    elif "s=preview" in driver.current_url:
        print("  [images] Already on preview -- CL skipped images step [OK]")
        return True
    else:
        # Wait up to 25s for editimage, geoverify, or preview
        print(f"  [images] Waiting for image/preview page from: {driver.current_url}")
        try:
            WebDriverWait(driver, 25).until(lambda d: (
                "s=editimage" in d.current_url
                or "s=preview" in d.current_url
                or "s=images" in d.current_url
                or "s=geoverify" in d.current_url
                or d.find_elements(By.CSS_SELECTOR, "input[type='file']")
                or "done with images" in (d.page_source or "").lower()
            ))
        except TimeoutException:
            print(f"  [images] Timed out -- URL: {driver.current_url}")

        cur = driver.current_url
        print(f"  [images] Landed on: {cur}")

        if "s=geoverify" in cur:
            print("  [images] Geoverify -- attempting bypass...")
            for attempt in range(3):
                if _click_geoverify_button(driver):
                    break
                time.sleep(3)
            # After geoverify, wait for editimage or preview
            try:
                WebDriverWait(driver, 20).until(lambda d: (
                    "s=editimage" in d.current_url
                    or "s=preview" in d.current_url
                    or "s=images" in d.current_url
                ))
                print(f"  [images] Post-geoverify: {driver.current_url}")
            except TimeoutException:
                print(f"  [images] Post-geoverify timeout: {driver.current_url}")

        if "s=preview" in driver.current_url:
            print("  [images] CL went to preview (no image step) -- continuing")
            return True'''

if old_complete in src:
    src = src.replace(old_complete, new_complete)
    print("[OK] Fix 2: complete_images_step waits for editimage")
else:
    # Simpler approach: just patch the preview check
    old_simple = '''    if "s=preview" in driver.current_url:
        print("  [images] Already on preview -- CL skipped images step [OK]")
        return True'''
    new_simple = '''    # Wait for editimage if we're not there yet
    if "s=editimage" not in driver.current_url and "s=preview" not in driver.current_url:
        print(f"  [images] Waiting for editimage from: {driver.current_url}")
        try:
            WebDriverWait(driver, 25).until(lambda d: (
                "s=editimage" in d.current_url or "s=preview" in d.current_url
                or "s=images" in d.current_url or "s=geoverify" in d.current_url
                or d.find_elements(By.CSS_SELECTOR, "input[type='file']")
            ))
            print(f"  [images] Landed: {driver.current_url}")
        except TimeoutException:
            print(f"  [images] Timed out: {driver.current_url}")
        # Handle geoverify
        if "s=geoverify" in driver.current_url:
            print("  [images] Geoverify -- bypassing...")
            for _ in range(3):
                if _click_geoverify_button(driver): break
                time.sleep(3)
            try:
                WebDriverWait(driver, 20).until(lambda d:
                    "s=editimage" in d.current_url or "s=preview" in d.current_url)
            except TimeoutException:
                pass
    if "s=preview" in driver.current_url:
        print("  [images] Already on preview -- CL skipped images step [OK]")
        return True'''
    if old_simple in src:
        src = src.replace(old_simple, new_simple)
        print("[OK] Fix 2 (simple): added editimage wait before preview check")
    else:
        print("[WARN] Fix 2: pattern not found")

cl.write_text(src, encoding="utf-8")
print(f"\n[OK] Saved {cl.name}")
print("\nRun: python RUN_DIRECT.py")
print("Watch for: [images] Landed: ...editimage")
