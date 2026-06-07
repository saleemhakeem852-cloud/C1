r"""
RUN_DIRECT.py - Run in D:\AAAAA\clblast45
python RUN_DIRECT.py
Runs craigslist_new.py directly so you see ALL output in real time.
"""
import subprocess, sys, os, json
from pathlib import Path

HERE = Path(__file__).parent

# Get credentials
accounts = json.loads((HERE / "accounts.json").read_text(encoding="utf-8"))
cl_acc = accounts.get("craigslist", [{}])[0]
email    = cl_acc.get("email", "")
password = cl_acc.get("password", "")
loc      = (cl_acc.get("locations") or cl_acc.get("saved_locations") or [{}])
if isinstance(loc, list) and loc:
    active_loc = next((l for l in loc if l.get("active") or l.get("isActive")), loc[0])
else:
    active_loc = {}
zip_code  = active_loc.get("zip", "90001")
city_name = active_loc.get("city", "Los Angeles")
state     = active_loc.get("state", "CA")

print(f"Email:    {email}")
print(f"ZIP:      {zip_code} / {city_name}, {state}")
print(f"Product:  Gold Coin (from products_subset.json)")
print("="*50)

env = os.environ.copy()
env["PYTHONUTF8"]        = "1"
env["PYTHONIOENCODING"]  = "utf-8:replace"
env["CL_EMAIL"]          = email
env["CL_PASSWORD"]       = password
env["CL_EMAIL_PASSWORD"] = password
env["CLB_PRODUCTS_FILE"] = str(HERE / "products_subset.json")
env["PRODUCTS_FILE"]     = str(HERE / "products_subset.json")
env["CL_CITY"]           = "losangeles"
env["CL_ZIP"]            = zip_code
env["CL_CITY_NAME"]      = city_name

# Run with real-time output (no capture)
proc = subprocess.Popen(
    [sys.executable, "-u", str(HERE / "craigslist_new.py")],
    env=env,
    cwd=str(HERE),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    bufsize=1
)

print("Script started. Output:\n")
try:
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()
except KeyboardInterrupt:
    proc.terminate()
    print("\n[Stopped by user]")

print(f"\nExit code: {proc.returncode}")
