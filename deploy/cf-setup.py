"""One-time Cloudflare setup (usage: python deploy/cf-setup.py [site.env]): R2 bucket + custom domain, Pages project + custom domain, DNS.
Reads site.env; prints only non-secret results. Derives R2 S3 credentials from the API token."""
import hashlib, json, subprocess, sys, time
import httpx

ENV_PATH = sys.argv[1] if len(sys.argv) > 1 else "site.env"
env = dict(l.split("=", 1) for l in open(ENV_PATH).read().splitlines() if "=" in l)
TOK, ACC, ZONE_NAME = env["CLOUDFLARE_API_TOKEN"], env["CLOUDFLARE_ACCOUNT_ID"], env["SITE_DOMAIN"]
BUCKET, PROJECT = env["R2_BUCKET"], env["CF_PAGES_PROJECT"]
API = "https://api.cloudflare.com/client/v4"
c = httpx.Client(timeout=60, trust_env=False, headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"})


def call(method, path, **kw):
    r = c.request(method, API + path, **kw)
    try:
        d = r.json()
    except Exception:
        d = {"success": False, "errors": [r.text[:200]]}
    return r.status_code, d


# token id -> R2 S3 access key; sha256(token) -> secret
st, d = call("GET", "/user/tokens/verify")
token_id = d["result"]["id"]
secret = hashlib.sha256(TOK.encode()).hexdigest()
print("token ok, id", token_id)

# zone
st, d = call("GET", f"/zones?name={ZONE_NAME}")
zones = d.get("result") or []
if not zones:
    sys.exit(f"zone {ZONE_NAME} not found in this account ({st}): {d.get('errors')}")
zone_id = zones[0]["id"]
print("zone", ZONE_NAME, zone_id, zones[0]["status"])

# R2 bucket
st, d = call("POST", f"/accounts/{ACC}/r2/buckets", json={"name": BUCKET})
print("r2 bucket create:", st, "ok" if d.get("success") else d.get("errors"))
# R2 custom domain img.<domain>
st, d = call("POST", f"/accounts/{ACC}/r2/buckets/{BUCKET}/domains/custom", json={"domain": f"img.{ZONE_NAME}", "zoneId": zone_id, "enabled": True})
print("r2 custom domain:", st, "ok" if d.get("success") else d.get("errors"))

# Pages project
st, d = call("POST", f"/accounts/{ACC}/pages/projects", json={"name": PROJECT, "production_branch": "main"})
print("pages project create:", st, "ok" if d.get("success") else d.get("errors"))
# Pages custom domains: apex + www
for dom in (ZONE_NAME, f"www.{ZONE_NAME}"):
    st, d = call("POST", f"/accounts/{ACC}/pages/projects/{PROJECT}/domains", json={"name": dom})
    print("pages domain", dom, ":", st, "ok" if d.get("success") else d.get("errors"))

# DNS: apex and www CNAME -> <project>.pages.dev (proxied). img.<domain> is created by R2 automatically.
st, d = call("GET", f"/zones/{zone_id}/dns_records?per_page=100")
existing = {(r["type"], r["name"]) for r in d.get("result", [])}
for name in (ZONE_NAME, f"www.{ZONE_NAME}"):
    if ("CNAME", name) in existing or ("A", name) in existing:
        print("dns exists", name)
        continue
    st, d = call("POST", f"/zones/{zone_id}/dns_records", json={"type": "CNAME", "name": name, "content": f"{PROJECT}.pages.dev", "proxied": True, "ttl": 1})
    print("dns", name, ":", st, "ok" if d.get("success") else d.get("errors"))

# persist derived R2 creds (append to the secrets file if missing)
env_text = open(ENV_PATH).read()
if "R2_ACCESS_KEY_ID=" not in env_text:
    add = f"R2_ACCESS_KEY_ID={token_id}\nR2_SECRET_ACCESS_KEY={secret}\nCF_ZONE_ID={zone_id}\n"
    open(ENV_PATH, "a").write(add)
    print("r2 credentials derived and stored")
