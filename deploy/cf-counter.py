"""One-time setup of the visit counter: a D1 database "paperfigure-stats" bound as DB to the Pages project.
Usage: python deploy/cf-counter.py [site.env]     (token needs D1:Edit and Pages:Edit)
Prints only ids and status, never the token."""
import json, subprocess, sys
import httpx

ENV_PATH = sys.argv[1] if len(sys.argv) > 1 else "site.env"
try:
    text = open(ENV_PATH).read()
except (FileNotFoundError, PermissionError):    # AI-OS keeps it root-only
    text = subprocess.run(["sudo", "cat", ENV_PATH], capture_output=True, text=True).stdout
env = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
TOK, ACC, PROJECT = env["CLOUDFLARE_API_TOKEN"], env["CLOUDFLARE_ACCOUNT_ID"], env["CF_PAGES_PROJECT"]
API = "https://api.cloudflare.com/client/v4"
c = httpx.Client(timeout=60, trust_env=False, headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"})
DB_NAME = "paperfigure-stats"


def call(method, path, **kw):
    r = c.request(method, API + path, **kw)
    try:
        d = r.json()
    except Exception:
        d = {"success": False, "errors": [r.text[:200]]}
    return r.status_code, d


# 1. database (reuse if it exists)
st, d = call("GET", f"/accounts/{ACC}/d1/database?name={DB_NAME}")
if not d.get("success"):
    sys.exit(f"cannot list D1 databases ({st}): {d.get('errors')}  -> the token needs the D1:Edit permission")
dbs = [x for x in d.get("result", []) if x.get("name") == DB_NAME]
if dbs:
    db_id = dbs[0]["uuid"]
    print("d1 exists", db_id)
else:
    st, d = call("POST", f"/accounts/{ACC}/d1/database", json={"name": DB_NAME})
    if not d.get("success"):
        sys.exit(f"d1 create failed ({st}): {d.get('errors')}")
    db_id = d["result"]["uuid"]
    print("d1 created", db_id)

# 2. schema
sql = ("CREATE TABLE IF NOT EXISTS counters (k TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);"
       "INSERT OR IGNORE INTO counters (k, n) VALUES ('views', 0), ('visitors', 0);")
st, d = call("POST", f"/accounts/{ACC}/d1/database/{db_id}/query", json={"sql": sql})
print("schema:", "ok" if d.get("success") else d.get("errors"))

# 3. bind as DB on the Pages project (production and preview)
st, d = call("GET", f"/accounts/{ACC}/pages/projects/{PROJECT}")
if not d.get("success"):
    sys.exit(f"pages project read failed ({st}): {d.get('errors')}")
cfg = d["result"].get("deployment_configs", {})
patch = {"deployment_configs": {}}
for envname in ("production", "preview"):
    cur = cfg.get(envname, {}) or {}
    d1 = dict(cur.get("d1_databases") or {})
    d1["DB"] = {"id": db_id}
    patch["deployment_configs"][envname] = {"d1_databases": d1}
st, d = call("PATCH", f"/accounts/{ACC}/pages/projects/{PROJECT}", json=patch)
print("pages binding:", "ok" if d.get("success") else d.get("errors"))
print("done: redeploy the site once so the function picks up the binding")
