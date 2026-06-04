# server.py — CLBlast backend
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import requests as req_lib

# ── Flask ────────────────────────────────────────────────────────────────────
try:
    from flask import Flask, Response, jsonify, request
    from flask_cors import CORS
except ImportError:
    sys.exit(
        "Missing dependencies.\n"
        "Run:  pip install flask flask-cors\n"
    )

app = Flask(__name__)
CORS(app)  # enable CORS on all endpoints

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
PRODUCTS_JSON = BASE_DIR / "products.json"
LISTINGS_JSON = BASE_DIR / "posted_listings.json"
SUBSET_JSON   = BASE_DIR / "products_subset.json"
ACCOUNTS_JSON = BASE_DIR / "accounts.json"
SCRIPTS = {
    "craigslist":    BASE_DIR / "craigslist.py",
    "adlandpro":     BASE_DIR / "adlandpro.py",
    "classifiedads": BASE_DIR / "classifiedads.py",
}

# ── Global state (protected by _lock) ────────────────────────────────────────
_lock     = threading.Lock()
_proc     = None           # subprocess.Popen or None
_lines    = []             # list of str, stdout from running script
_status   = "idle"         # "idle"|"running"|"done"|"error"
_platform = ""

# ── Bulk Post state variables ──
_bulk_active = False
_bulk_next_post_time = 0.0
_bulk_current_product = ""
_bulk_remaining_count = 0

# ── Helpers ──────────────────────────────────────────────────────────────────
def _read_json(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def _write_json(path: Path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        raise IOError(f"Could not write {path.name}: {exc}") from exc

# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/ping")
def ping():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

@app.get("/products")
def get_products():
    products = _read_json(PRODUCTS_JSON, [])
    # Case-insensitive alphabetical sorting by title/name
    products.sort(key=lambda p: (p.get("title") or p.get("name") or "").lower())
    return jsonify(products)

@app.get("/accounts/<platform>")
def get_accounts(platform):
    accounts_data = _read_json(ACCOUNTS_JSON, {})
    if platform == "2captcha":
        return jsonify(accounts_data.get(platform, {}))
    return jsonify(accounts_data.get(platform, []))

@app.post("/accounts/<platform>")
def save_accounts(platform):
    try:
        data = request.get_json(force=True)
        accounts_data = _read_json(ACCOUNTS_JSON, {})
        accounts_data[platform] = data
        _write_json(ACCOUNTS_JSON, accounts_data)
        return jsonify({"status": "ok"}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.post("/products")
def add_product():
    product = request.get_json(force=True)
    if not isinstance(product, dict) or not (product.get("title") or product.get("name")):
        return jsonify({"error": "Invalid product — 'title' or 'name' is required"}), 400

    products = _read_json(PRODUCTS_JSON, [])
    products.append(product)
    _write_json(PRODUCTS_JSON, products)
    return jsonify({"ok": True, "index": len(products) - 1}), 201

@app.delete("/products/<int:idx>")
def delete_product(idx):
    products = _read_json(PRODUCTS_JSON, [])
    if idx < 0 or idx >= len(products):
        return jsonify({"error": "Index out of range"}), 404
    products.pop(idx)
    _write_json(PRODUCTS_JSON, products)
    return jsonify({"ok": True})

@app.post("/sync-products")
def sync_products():
    try:
        products = request.get_json(force=True)
        if not isinstance(products, list):
            return jsonify({"error": "Expected a JSON array of products"}), 400
        _write_json(PRODUCTS_JSON, products)
        return jsonify({"status": "ok", "count": len(products)}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.post("/ai/anthropic-proxy")
def anthropic_proxy():
    try:
        body = request.get_json(force=True) or {}
        anthropic_key = request.headers.get("X-Anthropic-Key") or os.environ.get("ANTHROPIC_KEY")
        if not anthropic_key:
            return jsonify({"error": "Anthropic API Key is required"}), 400

        messages = body.get("messages")
        max_tokens = body.get("max_tokens", 300)

        outbound_headers = {
            "Content-Type": "application/json",
            "x-api-key": anthropic_key,
            "anthropic-version": "2023-06-01"
        }
        outbound_body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
            "messages": messages
        }

        response = req_lib.post(
            "https://api.anthropic.com/v1/messages",
            headers=outbound_headers,
            json=outbound_body,
            timeout=30
        )
        
        try:
            return jsonify(response.json()), response.status_code
        except Exception:
            return Response(response.content, status=response.status_code, mimetype="application/json")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.get("/listings")
def get_listings():
    return jsonify(_read_json(LISTINGS_JSON, {}))


def _run_bulk(body):
    global _proc, _status, _lines, _platform, _bulk_active, _bulk_next_post_time, _bulk_current_product, _bulk_remaining_count
    
    platform = body.get("platform", "").lower()
    email = body.get("email", "")
    password = body.get("password", "")
    two_captcha_key = body.get("two_captcha_key", "")
    craigslist_city = body.get("craigslist_city", "losangeles")
    product_indices = body.get("product_indices", [])
    account_label = body.get("account_label", "Account")
    interval_mins = float(body.get("interval", 10.0))
    
    script = SCRIPTS[platform]
    
    all_prods = _read_json(PRODUCTS_JSON, [])
    
    _bulk_active = True
    post_count = 0
    
    with _lock:
        _lines.clear()
        _lines.append(f"[CLBlast] Starting bulk posting queue for {len(product_indices)} products...\n")
    
    for idx, prod_idx in enumerate(product_indices):
        if not _bulk_active:
            break
            
        if prod_idx >= len(all_prods):
            continue
            
        prod = all_prods[prod_idx]
        
        with _lock:
            _bulk_current_product = prod.get("title") or prod.get("name", "Unknown")
            _bulk_remaining_count = len(product_indices) - idx
            _status = "running"
            _platform = platform
            _bulk_next_post_time = 0.0
            
        prod_display = prod.get('title') or prod.get('name', 'Unknown')
        _lines.append(f"\n[CLBlast] Posting product {idx + 1}/{len(product_indices)}: {prod_display}\n")
        
        _write_json(SUBSET_JSON, [prod])
        
        env = os.environ.copy()
        env["CL_EMAIL"]        = email
        env["CL_PASSWORD"]     = password
        env["TWO_CAPTCHA_KEY"] = two_captcha_key
        env["PRODUCTS_FILE"]   = str(SUBSET_JSON)
        if platform == "craigslist":
            env["CL_CITY"] = craigslist_city
            
        try:
            proc = subprocess.Popen(
                [sys.executable, str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=str(BASE_DIR),
                bufsize=1,
            )
            with _lock:
                _proc = proc
                
            for line in proc.stdout:
                if not _bulk_active:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                with _lock:
                    _lines.append(line)
                    
            proc.wait()
            rc = proc.returncode
            _lines.append(f"[CLBlast] Product post finished with exit status: {rc}\n")
            with _lock:
                _proc = None  # Reset proc so kill_post doesn't try to terminate stale process
        except Exception as e:
            _lines.append(f"[CLBlast] Error posting product: {e}\n")
            
        try:
            if SUBSET_JSON.exists():
                SUBSET_JSON.unlink()
        except Exception:
            pass
            
        if not _bulk_active:
            break
            
        post_count += 1
        
        if idx < len(product_indices) - 1:
            if post_count >= 5:
                post_count = 0
                wait_secs = 1800
                _lines.append("[CLBlast] 5 posts completed. Initiating 30-minute session cooldown to prevent flagging...\n")
            else:
                wait_secs = int(interval_mins * 60)
                _lines.append(f"[CLBlast] Waiting {interval_mins} minutes before next post...\n")
                
            with _lock:
                _status = "waiting"
                _bulk_next_post_time = time.time() + wait_secs
                
            end_wait = time.time() + wait_secs
            while time.time() < end_wait:
                if not _bulk_active:
                    break
                time.sleep(1)
                
    with _lock:
        _bulk_active = False
        _status = "done"  # frontend detects "done" and shows success, then resets
        _lines.append("[CLBlast] Bulk posting queue completed.\n")


@app.post("/bulk-post")
def bulk_post():
    global _status, _bulk_active
    body = request.get_json(force=True) or {}
    
    with _lock:
        # Auto-clear stale lock: if status is "running" but no live proc, reset it
        if (_status == "running" or _bulk_active) and (_proc is None or _proc.poll() is not None):
            _status = "idle"
            _bulk_active = False
        if _status == "running" or _bulk_active:
            return jsonify({"error": "busy"}), 409
            
    threading.Thread(target=_run_bulk, args=(body,), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/post")
def launch_post():
    global _proc, _lines, _status, _platform

    body = request.get_json(force=True) or {}
    platform = body.get("platform", "").lower()

    if platform not in SCRIPTS:
        return jsonify({"error": f"Unknown platform '{platform}'"}), 400

    script = SCRIPTS[platform]
    if not script.exists():
        return jsonify({"error": f"Script not found: {script}"}), 500

    with _lock:
        # Auto-clear stale lock: if status is "running" but no live proc, reset it
        if _status == "running" and (_proc is None or _proc.poll() is not None):
            _status = "idle"
        if _status == "running":
            return jsonify({"error": "busy"}), 409

        # Handle product indices
        product_indices = body.get("product_indices")
        if isinstance(product_indices, list) and len(product_indices) > 0:
            if not PRODUCTS_JSON.exists():
                return jsonify({"error": "products.json not found — add products first."}), 400
            
            all_prods = _read_json(PRODUCTS_JSON, [])
            subset = [all_prods[i] for i in product_indices if i < len(all_prods)]
            _write_json(SUBSET_JSON, subset)
            products_file = str(SUBSET_JSON)
        else:
            if not PRODUCTS_JSON.exists():
                return jsonify({"error": "products.json not found — add products first."}), 400
            products_file = str(PRODUCTS_JSON)

        # Build execution environment
        env = os.environ.copy()
        env["CL_EMAIL"]        = body.get("email", "")
        env["CL_PASSWORD"]     = body.get("password", "")
        env["TWO_CAPTCHA_KEY"] = body.get("two_captcha_key", "")
        env["PRODUCTS_FILE"]   = products_file
        if platform == "craigslist":
            env["CL_CITY"] = body.get("craigslist_city", "losangeles")

        account_label = body.get("account_label", "Account")
        _lines = [f"[CLBlast] Starting {platform} / {account_label}...\n"]
        _status = "running"
        _platform = platform

        def _run():
            global _proc, _status
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(script)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    cwd=str(BASE_DIR),
                    bufsize=1,
                )
                with _lock:
                    _proc = proc

                for line in proc.stdout:
                    with _lock:
                        _lines.append(line)

                proc.wait()
                with _lock:
                    rc = proc.returncode
                    _status = "done" if rc == 0 else "error"
                    _lines.append(f"[CLBlast] Script exited with code {rc}\n")
            except Exception as exc:
                with _lock:
                    _status = "error"
                    _lines.append(f"[CLBlast] Error: {exc}\n")
            finally:
                # Python 3.7 compatible clean up of temporary subset file
                try:
                    if SUBSET_JSON.exists():
                        SUBSET_JSON.unlink()
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    return jsonify({"ok": True, "platform": platform, "job_id": 1})

@app.get("/post/status")
def post_status():
    with _lock:
        now = time.time()
        time_left = max(0, int(_bulk_next_post_time - now)) if _bulk_active and _bulk_next_post_time > now else 0
        return jsonify({
            "status":   _status,
            "platform": _platform,
            "lines":    len(_lines),
            "bulk_active": _bulk_active,
            "bulk_time_left": time_left,
            "bulk_current_product": _bulk_current_product,
            "bulk_remaining_count": _bulk_remaining_count
        })

@app.post("/post/kill")
def kill_post():
    global _proc, _status, _bulk_active
    with _lock:
        _bulk_active = False
        if _proc:
            try:
                _proc.terminate()
            except Exception:
                pass
        _status = "idle"  # Always force-reset regardless of current state
        _lines.append("[CLBlast] Job terminated by user.\n")
    return jsonify({"ok": True})

@app.post("/post/reset")
def reset_post():
    """Force-clear any stuck job lock. Use when server shows 'busy' after a crash."""
    global _proc, _status, _bulk_active
    with _lock:
        _bulk_active = False
        _status = "idle"
        if _proc:
            try:
                _proc.terminate()
            except Exception:
                pass
            _proc = None
        _lines.append("[CLBlast] Server lock force-reset.\n")
    return jsonify({"ok": True, "message": "Server lock cleared. Ready for new jobs."})

@app.get("/post/stream")
def post_stream():
    try:
        from_idx = max(0, int(request.args.get("from", 0)))
    except (ValueError, TypeError):
        from_idx = 0
    def _generate():
        sent = from_idx
        while True:
            with _lock:
                new_lines = _lines[sent:]
                status    = _status

            for line in new_lines:
                safe = line.rstrip("\n").replace("\n", " ")
                yield f"data: {safe}\n\n"
                sent += 1

            if status in ("done", "error") and sent >= len(_lines):
                yield f"event: done\ndata: {status}\n\n"
                break

            time.sleep(0.25)

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

if __name__ == "__main__":
    print("+--------------------------------------+")
    print("|  CLBlast local server  - port 5000   |")
    print("|  Open clblast.html in your browser   |")
    print("+--------------------------------------+")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
