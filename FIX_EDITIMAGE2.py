r"""
FIX_EDITIMAGE2.py - Run in D:\AAAAA\clblast45
python FIX_EDITIMAGE2.py
"""
from pathlib import Path

HERE = Path(__file__).parent
cl = HERE / "craigslist_new.py"
src = cl.read_text(encoding="utf-8")

lines = src.splitlines()

# ── Find and show current deadline line ───────────────────────────────────────
for i, line in enumerate(lines):
    if 'deadline = time.time()' in line:
        print(f"Line {i+1}: {line.strip()}")

# ── Find the submit wait block by looking for key markers ─────────────────────
submit_wait_start = None
submit_wait_end = None
for i, line in enumerate(lines):
    if 'deadline = time.time() + 35' in line or 'deadline = time.time() + 20' in line:
        submit_wait_start = i
    if submit_wait_start and i > submit_wait_start:
        if 'Still on edit page after' in line or 'checking for validation' in line:
            submit_wait_end = i
            break

print(f"\nSubmit wait block: lines {submit_wait_start} to {submit_wait_end}")
if submit_wait_start:
    for i in range(max(0,submit_wait_start-1), min(len(lines), submit_wait_start+8)):
        print(f"  {i+1}: {lines[i]}")

# ── Find complete_images_step function ────────────────────────────────────────
images_func_start = None
preview_check_line = None
for i, line in enumerate(lines):
    if 'def complete_images_step' in line:
        images_func_start = i
    if images_func_start and i > images_func_start:
        if 's=preview' in line and 'skipped' in lines[i+1] if i+1 < len(lines) else False:
            preview_check_line = i
            break
        if 'Already on preview' in line or 'CL skipped images' in line:
            preview_check_line = i - 1  # the if line is one before the print
            break

print(f"\ncomplete_images_step: line {images_func_start}")
print(f"preview check at: line {preview_check_line}")
if preview_check_line:
    for i in range(max(0,preview_check_line-2), min(len(lines), preview_check_line+15)):
        print(f"  {i+1}: {lines[i]}")

# ── Apply fixes directly by line number ───────────────────────────────────────
new_lines = lines[:]

# Fix 1: Change deadline from 35 to 15 (faster so recovery doesn't jump to preview)
if submit_wait_start is not None:
    old_line = new_lines[submit_wait_start]
    new_line = old_line.replace('+ 35', '+ 15').replace('+ 20', '+ 15')
    new_lines[submit_wait_start] = new_line
    print(f"\n[OK] Fix 1: Changed line {submit_wait_start+1}")
    print(f"     FROM: {old_line.strip()}")
    print(f"     TO:   {new_line.strip()}")

# Fix 2: After preview check in complete_images_step, add editimage wait
# Find the exact line with 's=preview' check inside complete_images_step
img_preview_line = None
for i in range(images_func_start or 0, len(new_lines)):
    if 's=preview' in new_lines[i] and 'in driver.current_url' in new_lines[i]:
        img_preview_line = i
        break

print(f"\nImage preview check at line: {img_preview_line}")
if img_preview_line:
    # Show context
    for i in range(max(0,img_preview_line-3), min(len(new_lines), img_preview_line+6)):
        print(f"  {i+1}: {new_lines[i]}")

    # Insert editimage wait BEFORE the s=preview check
    indent = '    '  # 4 spaces
    insert_block = [
        f'{indent}# Wait for editimage or preview after form submit',
        f'{indent}cur = driver.current_url',
        f'{indent}print(f"  [images] Checking URL: {{cur}}")',
        f'{indent}if "s=editimage" not in cur and "s=preview" not in cur:',
        f'{indent}    print(f"  [images] Waiting for editimage/preview...")',
        f'{indent}    try:',
        f'{indent}        WebDriverWait(driver, 25).until(lambda d: (',
        f'{indent}            "s=editimage" in d.current_url',
        f'{indent}            or "s=preview" in d.current_url',
        f'{indent}            or "s=images" in d.current_url',
        f'{indent}            or "s=geoverify" in d.current_url',
        f'{indent}            or d.find_elements(By.CSS_SELECTOR, "input[type=\'file\']")',
        f'{indent}            or "done with images" in (d.page_source or "").lower()',
        f'{indent}        ))',
        f'{indent}        print(f"  [images] Landed: {{driver.current_url}}")',
        f'{indent}    except TimeoutException:',
        f'{indent}        print(f"  [images] Timed out waiting: {{driver.current_url}}")',
        f'{indent}    # Handle geoverify',
        f'{indent}    if "s=geoverify" in driver.current_url:',
        f'{indent}        print("  [images] Geoverify -- bypassing...")',
        f'{indent}        for _ in range(3):',
        f'{indent}            if _click_geoverify_button(driver): break',
        f'{indent}            time.sleep(3)',
        f'{indent}        try:',
        f'{indent}            WebDriverWait(driver, 20).until(lambda d:',
        f'{indent}                "s=editimage" in d.current_url or "s=preview" in d.current_url',
        f'{indent}                or "s=images" in d.current_url)',
        f'{indent}            print(f"  [images] Post-geoverify: {{driver.current_url}}")',
        f'{indent}        except TimeoutException:',
        f'{indent}            print(f"  [images] Post-geoverify timeout: {{driver.current_url}}")',
        f'',
    ]
    new_lines = new_lines[:img_preview_line] + insert_block + new_lines[img_preview_line:]
    print(f"\n[OK] Fix 2: Inserted {len(insert_block)} lines before preview check")

# Fix 3: Also handle s=editimage in _wait_for_images_page
# Add editimage to the condition
for i, line in enumerate(new_lines):
    if '"s=images" in url or "s=preview" in url' in line:
        new_lines[i] = new_lines[i].replace(
            '"s=images" in url or "s=preview" in url',
            '"s=images" in url or "s=preview" in url or "s=editimage" in url'
        )
        print(f"[OK] Fix 3: Added editimage to _wait_for_images_page line {i+1}")
        break

src = '\n'.join(new_lines)
cl.write_text(src, encoding="utf-8")
print(f"\n[OK] Saved {cl.name} ({len(new_lines)} lines)")

# Verify
src2 = cl.read_text(encoding="utf-8")
print("\n=== VERIFICATION ===")
print(f"  deadline=15: {'YES' if 'deadline = time.time() + 15' in src2 else 'NO'}")
print(f"  editimage wait: {'YES' if 'Waiting for editimage/preview' in src2 else 'NO'}")
print(f"  editimage in _wait: {'YES' if 's=editimage' in src2 else 'NO'}")
print("\nRun: python RUN_DIRECT.py")
print("Look for: [images] Landed: ...editimage")
