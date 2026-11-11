#!/usr/bin/env python3
"""
Telegram bot (long polling, no webhook) that deploys a GitHub repo to
Cloudflare Workers with the user's Cloudflare API token and creates the
resources the code needs (D1, KV, R2, Queues).

Env vars:
  TELEGRAM_BOT_TOKEN   (required)  token from @BotFather
  ALLOWED_USER_IDS     (optional)  comma separated Telegram user ids allowed to use the bot
  GITHUB_TOKEN         (optional)  default token for private repos
  PORT                 (set by Render) health-check port
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

try:
    import tomllib
except ImportError:  # python < 3.11
    tomllib = None

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}
DEFAULT_GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
TG = "https://api.telegram.org/bot" + TG_TOKEN
CF = "https://api.cloudflare.com/client/v4"
COMPAT_DATE = "2025-06-01"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- Telegram
def tg(method, **params):
    try:
        r = requests.post(TG + "/" + method, json=params, timeout=70)
        return r.json()
    except Exception as e:  # network hiccup
        log("tg error", method, e)
        return {"ok": False}


def say(chat, text):
    tg("sendMessage", chat_id=chat, text=text[:4000], disable_web_page_preview=True)


# --------------------------------------------------------------------------- Cloudflare
class CFError(Exception):
    pass


class CloudflareAPI:
    def __init__(self, token):
        self.h = {"Authorization": "Bearer " + token}

    def call(self, method, path, **kw):
        r = requests.request(method, CF + path, headers=self.h, timeout=120, **kw)
        try:
            j = r.json()
        except Exception:
            raise CFError("%s -> HTTP %s" % (path, r.status_code))
        if not j.get("success"):
            errs = "; ".join("%s: %s" % (e.get("code"), e.get("message")) for e in j.get("errors", [])) or r.text[:200]
            raise CFError("%s -> %s" % (path, errs))
        return j.get("result")


def ensure_d1(cf, acc, name):
    for d in cf.call("GET", "/accounts/%s/d1/database" % acc, params={"name": name, "per_page": 100}) or []:
        if d.get("name") == name:
            return d["uuid"]
    return cf.call("POST", "/accounts/%s/d1/database" % acc, json={"name": name})["uuid"]


def ensure_kv(cf, acc, title):
    page = 1
    while True:
        r = cf.call("GET", "/accounts/%s/storage/kv/namespaces" % acc, params={"per_page": 100, "page": page}) or []
        for n in r:
            if n.get("title") == title:
                return n["id"]
        if len(r) < 100:
            break
        page += 1
    return cf.call("POST", "/accounts/%s/storage/kv/namespaces" % acc, json={"title": title})["id"]


def ensure_r2(cf, acc, name):
    try:
        cf.call("GET", "/accounts/%s/r2/buckets/%s" % (acc, name))
        return
    except CFError:
        pass
    cf.call("POST", "/accounts/%s/r2/buckets" % acc, json={"name": name})


def ensure_queue(cf, acc, name):
    try:
        cf.call("POST", "/accounts/%s/queues" % acc, json={"queue_name": name})
    except CFError as e:
        if "exist" not in str(e).lower() and "11009" not in str(e):
            raise


def get_subdomain(cf, acc):
    """Returns (subdomain or None, error text or None). Never creates or renames anything."""
    try:
        r = cf.call("GET", "/accounts/%s/workers/subdomain" % acc)
        if r and r.get("subdomain"):
            return r["subdomain"], None
        return None, None
    except CFError as e:
        return None, str(e)


def set_subdomain(cf, acc, name):
    cf.call("PUT", "/accounts/%s/workers/subdomain" % acc, json={"subdomain": name})


# --------------------------------------------------------------------------- repo helpers
def parse_repo(text):
    parts = text.strip().split()
    if not parts:
        return None
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/(.+?))?/?$", parts[0])
    if not m:
        return None
    return {"owner": m.group(1), "repo": m.group(2), "ref": m.group(3), "name": parts[1] if len(parts) > 1 else None}


class NeedGithubToken(Exception):
    pass


def fetch_repo(owner, repo, ref, gh_token, dest):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "cf-deploy-bot"}
    if gh_token:
        h["Authorization"] = "Bearer " + gh_token
    url = "https://api.github.com/repos/%s/%s/zipball" % (owner, repo) + ("/" + ref if ref else "")
    r = requests.get(url, headers=h, timeout=180, stream=True)
    if r.status_code in (401, 403, 404):
        if not gh_token:
            raise NeedGithubToken()
        raise RuntimeError("دسترسی به ریپو ممکن نیست (کد %s). لینک یا توکن گیت‌هاب رو چک کن." % r.status_code)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(1 << 20):
        buf.write(chunk)
        if buf.tell() > 120 * 1024 * 1024:
            raise RuntimeError("ریپو بیش از حد بزرگه (بیشتر از ۱۲۰ مگابایت).")
    z = zipfile.ZipFile(buf)
    base = os.path.realpath(dest)
    for info in z.infolist():
        target = os.path.realpath(os.path.join(dest, info.filename))
        if not target.startswith(base + os.sep) and target != base:
            raise RuntimeError("فایل zip مشکوکه.")
    z.extractall(dest)
    entries = os.listdir(dest)
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        return os.path.join(dest, entries[0])
    return dest


def strip_jsonc(t):
    out, i, n, ins = [], 0, len(t), False
    while i < n:
        c = t[i]
        if ins:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(t[i + 1])
                i += 2
                continue
            if c == '"':
                ins = False
            i += 1
            continue
        if c == '"':
            ins = True
            out.append(c)
            i += 1
            continue
        if t.startswith("//", i):
            j = t.find("\n", i)
            i = n if j < 0 else j
            continue
        if t.startswith("/*", i):
            j = t.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_config(root):
    for fn in ("wrangler.jsonc", "wrangler.json", "wrangler.toml"):
        p = os.path.join(root, fn)
        if os.path.isfile(p):
            txt = open(p, encoding="utf-8").read()
            if fn.endswith(".toml"):
                if tomllib is None:
                    raise RuntimeError("tomllib در دسترس نیست (پایتون ۳.۱۱ لازمه).")
                return tomllib.loads(txt)
            return json.loads(strip_jsonc(txt))
    return None


def scan_sources(root):
    texts, total = [], 0
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in ("node_modules", ".git", "dist", "build", ".wrangler")]
        for f in fn:
            if f.endswith((".js", ".mjs", ".ts", ".tsx", ".jsx")) and not f.endswith(".d.ts"):
                p = os.path.join(dp, f)
                try:
                    s = os.path.getsize(p)
                except OSError:
                    continue
                if s > 5_000_000 or total + s > 25_000_000:
                    continue
                total += s
                texts.append(open(p, encoding="utf-8", errors="ignore").read())
    return "\n".join(texts)


def pick_entry(root):
    prefs = ["_worker.js", "worker.js", "index.js", "index.mjs", "index.ts", "src/index.ts", "src/index.js",
             "src/index.mjs", "src/worker.js", "src/worker.ts"]
    cands = []
    for p in prefs:
        fp = os.path.join(root, p)
        if os.path.isfile(fp):
            cands.append(p)
    for f in sorted(os.listdir(root)):
        if f.endswith((".js", ".mjs", ".ts")) and f not in cands and ".config." not in f:
            cands.append(f)
    good = []
    for p in cands:
        t = open(os.path.join(root, p), encoding="utf-8", errors="ignore").read()
        if re.search(r"export\s+default", t) and re.search(r"\bfetch\b", t):
            good.append((p, len(t)))
    if good:
        for p in prefs:
            for g in good:
                if g[0] == p:
                    return p
        return max(good, key=lambda x: x[1])[0]
    raise RuntimeError("نقطه ورود ورکر پیدا نشد (فایلی با export default { fetch } لازمه).")


def classify(code):
    names = set(re.findall(r"\benv\??\.([A-Za-z_]\w*)", code))
    names |= set(re.findall(r"\benv\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\]", code))
    res = {}
    for n in names:
        meth = set(re.findall(r"\benv\??\.%s\??\.(\w+)\s*\(" % re.escape(n), code))
        if meth & {"prepare", "exec"}:
            k = "d1"
        elif meth & {"idFromName", "idFromString", "newUniqueId", "getByName"}:
            k = "do"
        elif meth & {"send", "sendBatch"}:
            k = "queue"
        elif meth & {"head", "createMultipartUpload", "resumeMultipartUpload"} or (
                meth & {"get", "put", "delete", "list"} and re.search(r"R2|BUCKET", n, re.I)):
            k = "r2"
        elif meth & {"get", "put", "delete", "list", "getWithMetadata"}:
            k = "kv"
        elif "run" in meth and re.match(r"^AI$", n):
            k = "ai"
        elif "fetch" in meth:
            k = "service"
        else:
            k = "var"
        res[n] = k
    return res


def sanitize_name(s):
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return (s or "worker")[:50]


def res_name(worker, binding):
    return sanitize_name("%s-%s" % (worker, binding))


HEX32 = re.compile(r"^[0-9a-f]{32}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


import uuid as _uuid

UUID4 = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b")
UUID_KEEP = {"258eafa5-e914-47da-95ca-c5ab0dc85b11"}  # WebSocket handshake GUID (protocol constant)
UUID_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".wrangler", "test", "tests", "__tests__", "docs",
                  "doc", "example", "examples", "fixtures"}
UUID_VARS = {"UUID", "USER_UUID", "USERID", "USER_ID", "UID"}


def randomize_uuids(root):
    """Replace hardcoded UUID v4 literals in JS/TS sources with fresh ones (same old -> same new).
    Returns list of (relative file, count, old uuid prefix)."""
    mapping, report = {}, []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in UUID_SKIP_DIRS]
        for f in fn:
            if not f.endswith((".js", ".mjs", ".ts", ".tsx", ".jsx")) or f.endswith(".d.ts") or ".test." in f or ".spec." in f:
                continue
            path = os.path.join(dp, f)
            try:
                if os.path.getsize(path) > 5_000_000:
                    continue
                txt = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            hits = []

            def rep(m):
                old = m.group(0)
                lo = old.lower()
                if lo in UUID_KEEP:
                    return old
                new = mapping.get(lo) or mapping.setdefault(lo, str(_uuid.uuid4()))
                hits.append(lo)
                return new.upper() if old.isupper() else new

            new_txt = UUID4.sub(rep, txt)
            if hits:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(new_txt)
                report.append((os.path.relpath(path, root), len(hits), hits[0][:8]))
    return report


def prepare_config(cfg, root, cf, acc, worker, progress=lambda m: None):
    """Fill/patch the wrangler config. Returns (cfg, created, warnings, var_candidates, d1_list)."""
    created, warnings = [], []
    code = scan_sources(root)
    kinds = classify(code)

    if cfg is None:
        entry = pick_entry(root)
        cfg = {"main": entry}
        progress("🔎 فایل ورود ورکر: " + entry)
    cfg = dict(cfg)
    cfg["name"] = worker
    cfg.setdefault("compatibility_date", COMPAT_DATE)
    flags = list(cfg.get("compatibility_flags") or [])
    if not any(f in flags for f in ("nodejs_compat", "nodejs_compat_v2")):
        flags.append("nodejs_compat")
    cfg["compatibility_flags"] = flags
    cfg["account_id"] = acc
    cfg["workers_dev"] = True
    for k in ("route", "routes", "zone_id", "zone_name"):
        cfg.pop(k, None)

    extra = {"gen_vars": {}, "vars_randomized": []}
    for k, v in list((cfg.get("vars") or {}).items()):
        if isinstance(v, str) and UUID4.fullmatch(v) and v.lower() not in UUID_KEEP:
            cfg["vars"][k] = str(_uuid.uuid4())
            extra["vars_randomized"].append(k)

    cfg.setdefault("d1_databases", [])
    cfg.setdefault("kv_namespaces", [])
    cfg.setdefault("r2_buckets", [])
    queues = cfg.setdefault("queues", {})
    producers = queues.setdefault("producers", [])
    configured = set()
    for key in ("d1_databases", "kv_namespaces", "r2_buckets"):
        configured |= {b.get("binding") for b in cfg[key]}
    configured |= {b.get("binding") for b in producers}
    configured |= {b.get("name") for b in (cfg.get("durable_objects") or {}).get("bindings", [])}
    configured |= {b.get("binding") for b in cfg.get("services", []) or []}
    if cfg.get("ai"):
        configured.add(cfg["ai"].get("binding"))
    if cfg.get("assets"):
        configured.add(cfg["assets"].get("binding", "ASSETS"))
    configured |= set((cfg.get("vars") or {}).keys())

    # bindings the code uses but the config lacks
    for n, k in sorted(kinds.items()):
        if n in configured:
            continue
        if k == "d1":
            cfg["d1_databases"].append({"binding": n})
        elif k == "kv":
            cfg["kv_namespaces"].append({"binding": n})
        elif k == "r2":
            cfg["r2_buckets"].append({"binding": n})
        elif k == "queue":
            producers.append({"binding": n})
        elif k == "ai":
            cfg["ai"] = {"binding": n}
        elif k in ("do", "service") and n != "ASSETS":
            warnings.append("باندینگ %s (نوع %s) خودکار ساخته نمیشه؛ باید توی wrangler.toml تعریف شده باشه." % (n, k))

    d1_list = []
    for b in cfg["d1_databases"]:
        nm = b.get("database_name") or res_name(worker, b["binding"])
        b["database_name"] = nm
        if not UUID.match(str(b.get("database_id", ""))):
            progress("🗄 دیتابیس D1: " + nm)
            b["database_id"] = ensure_d1(cf, acc, nm)
            created.append("D1 → %s (%s)" % (nm, b["binding"]))
        b.pop("preview_database_id", None)
        d1_list.append(b)
    for b in cfg["kv_namespaces"]:
        if not HEX32.match(str(b.get("id", ""))):
            title = res_name(worker, b["binding"])
            progress("🔑 KV: " + title)
            b["id"] = ensure_kv(cf, acc, title)
            created.append("KV → %s (%s)" % (title, b["binding"]))
        b.pop("preview_id", None)
    for b in cfg["r2_buckets"]:
        nm = (b.get("bucket_name") or res_name(worker, b["binding"])).lower()
        b["bucket_name"] = nm
        progress("📦 R2: " + nm)
        ensure_r2(cf, acc, nm)
        created.append("R2 → %s (%s)" % (nm, b["binding"]))
        b.pop("preview_bucket_name", None)
    for b in producers:
        q = b.get("queue") or res_name(worker, b["binding"])
        b["queue"] = q
        progress("📨 Queue: " + q)
        ensure_queue(cf, acc, q)
        created.append("Queue → %s (%s)" % (q, b["binding"]))
    if not producers and not queues.get("consumers"):
        cfg.pop("queues", None)
    for key in ("d1_databases", "kv_namespaces", "r2_buckets"):
        if not cfg[key]:
            cfg.pop(key)

    auto = {"CF_API_TOKEN", "CF_ACCOUNT_ID", "WORKER_NAME"}
    for n, k in kinds.items():
        if k == "var" and n in UUID_VARS and n not in configured:
            extra["gen_vars"][n] = str(_uuid.uuid4())
    candidates = sorted(n for n, k in kinds.items()
                        if k == "var" and n not in configured and n not in auto and n not in extra["gen_vars"])
    used_auto = sorted(n for n in kinds if n in auto and n not in configured)
    return cfg, created, warnings, candidates, used_auto, extra


def find_schema(root):
    for p in ("schema.sql", "db/schema.sql", "sql/schema.sql", "init.sql", "setup.sql", "database.sql"):
        fp = os.path.join(root, p)
        if os.path.isfile(fp):
            return fp
    return None


# --------------------------------------------------------------------------- processes
def scrub(text, secrets):
    text = ANSI.sub("", text)
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def run(cmd, cwd, env, secrets, timeout):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return p.returncode, scrub((p.stdout or "") + "\n" + (p.stderr or ""), secrets)


def tail(text, n=18):
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return "\n".join(lines[-n:])


# --------------------------------------------------------------------------- sessions / flow
sessions = {}
lock = threading.Lock()


def sess(chat):
    with lock:
        return sessions.setdefault(chat, {"state": "idle"})


def reset(chat):
    s = sessions.pop(chat, None)
    if s and s.get("work"):
        shutil.rmtree(s["work"], ignore_errors=True)


WELCOME = (
    "سلام! 👋\n"
    "این ربات ریپوی گیت‌هاب تو رو روی Cloudflare Workers دیپلوی می‌کنه و هرچی که کد لازم داره "
    "(دیتابیس D1، KV، R2، Queue) رو خودش می‌سازه و وصل می‌کنه.\n\n"
    "قدم اول: Cloudflare API Token رو بفرست. بعد از خوندن، پیامت پاک میشه.\n\n"
    "دسترسی‌های لازم توکن (Account):\n"
    "• Workers Scripts: Edit\n• D1: Edit\n• Workers KV Storage: Edit\n"
    "• Workers R2 Storage: Edit (اگه ریپو R2 می‌خواد)\n• Queues: Edit (اگه Queue می‌خواد)\n"
    "• Account Settings: Read\n\n"
    "ساخت توکن: dash.cloudflare.com/profile/api-tokens\n"
    "• برای وصل کردن دامنه‌ی خودت هم Zone → Zone:Read لازمه.\n\n"
    "بعد از ورود توکن:\n/repo ← دیپلوی ریپوی گیت‌هاب\n/worker ← ساخت ورکر از کد آماده\n/subdomain ← دیدن ساب‌دامین workers.dev حساب\n/logout ← پاک کردن توکن از حافظه\n"
    "فقط ریپوهای خودت رو بده، چون کدشون روی سرور ربات build میشه."
)


def start_prepare(chat):
    s = sess(chat)
    s["state"] = "busy"
    threading.Thread(target=job_prepare, args=(chat,), daemon=True).start()


def job_prepare(chat):
    s = sess(chat)
    secrets = [s.get("cf_token"), s.get("gh_token")]
    try:
        r = s["repo"]
        say(chat, "⬇️ در حال دانلود ریپو %s/%s ..." % (r["owner"], r["repo"]))
        work = tempfile.mkdtemp(prefix="deploy_")
        s["work"] = work
        try:
            root = fetch_repo(r["owner"], r["repo"], r["ref"], s.get("gh_token") or DEFAULT_GH_TOKEN, work)
        except NeedGithubToken:
            shutil.rmtree(work, ignore_errors=True)
            s["work"] = None
            s["state"] = "ghtoken"
            say(chat, "ریپو خصوصیه یا پیدا نشد. اگه خصوصیه، یه GitHub Token (با دسترسی خوندن ریپو) بفرست. وگرنه لینک رو چک کن و /start بزن.")
            return
        s["root"] = root
        s["uuid_report"] = randomize_uuids(root) if os.environ.get("RANDOMIZE_UUID", "1") != "0" else []
        worker = sanitize_name(r["name"] or r["repo"])
        s["worker"] = worker
        cf = CloudflareAPI(s["cf_token"])
        cfg = load_config(root)
        say(chat, "⚙️ ساخت منابع مورد نیاز... (نام ورکر: %s)" % worker)
        cfg, created, warnings, cands, used_auto, extra = prepare_config(
            cfg, root, cf, s["acc"], worker, progress=lambda m: say(chat, m))
        s.update(cfg=cfg, created=created, warnings=warnings, used_auto=used_auto,
                 gen_vars=extra["gen_vars"], vars_randomized=extra["vars_randomized"])
        if cands:
            s["state"] = "vars"
            say(chat, "کد این متغیرهای محیطی رو می‌خونه:\n" + "\n".join("• " + c for c in cands) +
                "\n\nاگه می‌خوای مقدار بدی، هر خط به شکل NAME=value بفرست (به‌صورت secret ذخیره میشه). "
                "اگه لازم نیست /skip بزن.")
            return
        job_deploy(chat)
    except Exception as e:
        fail(chat, e, secrets)


def fail(chat, e, secrets):
    log("ERROR", traceback.format_exc())
    msg = scrub(str(e), secrets)
    hint = ""
    if re.search(r"10000|Authentication|permission|Unauthorized|forbidden", msg, re.I):
        hint = "\n\nاحتمالاً توکن دسترسی کافی نداره. دسترسی‌های Workers Scripts/D1/KV/R2 و Account Settings:Read رو چک کن."
    say(chat, "❌ خطا:\n" + msg[-1500:] + hint + "\n\nبرای شروع دوباره /start بزن.")
    s = sess(chat)
    if s.get("work"):
        shutil.rmtree(s["work"], ignore_errors=True)
    s.update(state="idle", work=None)


def job_deploy(chat):
    s = sess(chat)
    secrets = [s.get("cf_token"), s.get("gh_token")]
    s["state"] = "busy"
    try:
        root, cfg, acc, worker = s["root"], s["cfg"], s["acc"], s["worker"]
        cf = CloudflareAPI(s["cf_token"])
        sub, serr = get_subdomain(cf, acc)
        if not sub:
            ask_subdomain(chat, "deploy", serr)
            return
        base_env = {"PATH": os.environ.get("PATH", ""), "HOME": s["work"], "CI": "true", "NO_COLOR": "1",
                    "FORCE_COLOR": "0", "WRANGLER_SEND_METRICS": "false", "npm_config_cache": os.path.join(s["work"], ".npm")}
        wr_env = dict(base_env, CLOUDFLARE_API_TOKEN=s["cf_token"], CLOUDFLARE_ACCOUNT_ID=acc)

        if os.path.isfile(os.path.join(root, "package.json")):
            say(chat, "📦 npm install ...")
            code, out = run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], root, base_env, secrets, 900)
            if code != 0:
                raise RuntimeError("npm install ناموفق بود:\n" + tail(out))

        gen = "wrangler.generated.json"
        with open(os.path.join(root, gen), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)

        notes = []
        for b in cfg.get("d1_databases", []):
            mdir = os.path.join(root, b.get("migrations_dir") or "migrations")
            if os.path.isdir(mdir) and any(x.endswith(".sql") for x in os.listdir(mdir)):
                say(chat, "🗄 اجرای migration های %s ..." % b["database_name"])
                code, out = run(["wrangler", "d1", "migrations", "apply", b["database_name"], "--remote", "-c", gen],
                                root, wr_env, secrets, 600)
                if code != 0:
                    raise RuntimeError("migration ناموفق بود:\n" + tail(out))
            elif len(cfg["d1_databases"]) == 1 and find_schema(root):
                sql = open(find_schema(root), encoding="utf-8", errors="ignore").read()
                say(chat, "🗄 اجرای فایل schema روی %s ..." % b["database_name"])
                try:
                    cf.call("POST", "/accounts/%s/d1/database/%s/query" % (acc, b["database_id"]), json={"sql": sql})
                except CFError as e:
                    notes.append("اجرای schema.sql خطا داد (شاید قبلاً اجرا شده): " + str(e)[:200])

        say(chat, "🚀 در حال دیپلوی با wrangler ...")
        code, out = run(["wrangler", "deploy", "-c", gen], root, wr_env, secrets, 900)
        if code != 0:
            raise RuntimeError("wrangler deploy ناموفق بود:\n" + tail(out))
        m = re.search(r"https://[\w.-]+\.workers\.dev", out)
        url = m.group(0) if m else ("https://%s.%s.workers.dev" % (worker, sub) if sub else "(آدرس رو از داشبورد ببین)")

        set_secrets = {}
        if "CF_API_TOKEN" in s.get("used_auto", []):
            set_secrets["CF_API_TOKEN"] = s["cf_token"]
        if "CF_ACCOUNT_ID" in s.get("used_auto", []):
            set_secrets["CF_ACCOUNT_ID"] = acc
        if "WORKER_NAME" in s.get("used_auto", []):
            set_secrets["WORKER_NAME"] = worker
        set_secrets.update(s.get("gen_vars", {}))
        set_secrets.update(s.get("user_vars", {}))
        for k, v in set_secrets.items():
            try:
                cf.call("PUT", "/accounts/%s/workers/scripts/%s/secrets" % (acc, worker),
                        json={"name": k, "text": v, "type": "secret_text"})
            except CFError as e:
                notes.append("ست کردن secret %s ناموفق: %s" % (k, str(e)[:150]))

        lines = ["✅ دیپلوی شد!", "🌐 " + url, ""]
        if s["created"]:
            lines += ["منابع ساخته‌شده/وصل‌شده:"] + ["• " + c for c in s["created"]] + [""]
        if set_secrets:
            lines.append("Secret های ست‌شده: " + ", ".join(sorted(set_secrets)))
            if "CF_API_TOKEN" in set_secrets:
                lines.append("(چون کد از env.CF_API_TOKEN استفاده می‌کنه، توکنت به‌عنوان secret روی همین ورکر ذخیره شد.)")
            lines.append("")
        if s.get("uuid_report") or s.get("vars_randomized"):
            lines.append("🔐 UUID های ثابت داخل ریپو برای این دیپلوی رندوم شدن:")
            for f, n, old in s.get("uuid_report", []):
                lines.append("• %s (%d مورد، قبلی: %s…)" % (f, n, old))
            for k in s.get("vars_randomized", []):
                lines.append("• متغیر %s توی wrangler config" % k)
            lines.append("")
        for k, v in s.get("gen_vars", {}).items():
            if k in set_secrets:
                lines.append("🔑 %s (رندوم‌شده و به‌عنوان secret ست شد): %s" % (k, v))
        for w in s.get("warnings", []) + notes:
            lines.append("⚠️ " + w)
        lines.append("\nورکر بعدی: /worker  |  ریپو: /repo")
        say(chat, "\n".join(lines))
        keep_auth(chat)
    except Exception as e:
        fail(chat, e, secrets)


# --------------------------------------------------------------------------- quick worker (paste code -> deploy)
def strip_fences(t):
    t = t.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip() + "\n"


def worker_kind(code):
    if re.search(r"\bexport\s+(default|\{)", code):
        return "module"
    if re.search(r"addEventListener\s*\(\s*['\"]fetch['\"]", code):
        return "service"
    return None


def syntax_check(code, kind):
    fd, p = tempfile.mkstemp(suffix=".mjs" if kind == "module" else ".js")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True, timeout=30)
        return r.returncode == 0, tail(r.stderr or "", 6)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True, ""
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def upload_worker(cf, acc, name, code, kind):
    if kind == "module":
        meta = {"main_module": "worker.js", "compatibility_date": COMPAT_DATE, "compatibility_flags": ["nodejs_compat"]}
        files = {"metadata": (None, json.dumps(meta), "application/json"),
                 "worker.js": ("worker.js", code.encode("utf-8"), "application/javascript+module")}
    else:
        meta = {"body_part": "script.js", "compatibility_date": COMPAT_DATE}
        files = {"metadata": (None, json.dumps(meta), "application/json"),
                 "script.js": ("script.js", code.encode("utf-8"), "application/javascript")}
    cf.call("PUT", "/accounts/%s/workers/scripts/%s" % (acc, name), files=files)


def find_zone(cf, host):
    labels = host.split(".")
    for i in range(len(labels) - 1):
        cand = ".".join(labels[i:])
        z = cf.call("GET", "/zones", params={"name": cand, "per_page": 1})
        if z:
            return z[0]["id"], cand
    return None, None


def probe(url, tries):
    for i in range(tries):
        try:
            r = requests.get(url, timeout=10)
            return r.status_code, (r.text or "").strip()[:200]
        except Exception:
            if i < tries - 1:
                time.sleep(4)
    return None, ""


def tg_download(file_id):
    info = tg("getFile", file_id=file_id)
    path = (info.get("result") or {}).get("file_path")
    if not path:
        raise RuntimeError("دانلود فایل از تلگرام ممکن نشد.")
    r = requests.get("https://api.telegram.org/file/bot%s/%s" % (TG_TOKEN, path), timeout=60)
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


def keep_auth(chat):
    old = sessions.get(chat) or {}
    if old.get("work"):
        shutil.rmtree(old["work"], ignore_errors=True)
    keep = {k: old[k] for k in ("cf_token", "acc") if k in old}
    sessions[chat] = dict(keep, state="idle")


def ask_subdomain(chat, resume, err):
    s = sess(chat)
    s["state"] = "subname"
    s["resume"] = resume
    say(chat, "⚠️ ساب‌دامین workers.dev حساب پیدا نشد" + (" (%s)" % err[:150] if err else "") + ".\n\n"
        "اگه حسابت از قبل ساب‌دامین داره، /cancel بزن و چک کن توکن دسترسی خوندن Workers رو داره؛ "
        "چون ست کردن اسم جدید، ساب‌دامین فعلی رو عوض می‌کنه.\n"
        "اگه نداره، اسم دلخواهت رو بفرست (انگلیسی کوچک، عدد و خط تیره). آدرس‌ها میشن:\n"
        "<اسم ورکر>.<اسمی که الان می‌فرستی>.workers.dev")


def job_worker(chat):
    s = sess(chat)
    secrets = [s.get("cf_token")]
    s["state"] = "busy"
    try:
        cf, acc, name, domain = CloudflareAPI(s["cf_token"]), s["acc"], s["wname"], s.get("wdomain")
        sub, serr = get_subdomain(cf, acc)
        if not sub:
            ask_subdomain(chat, "worker", serr)
            return
        say(chat, "🚀 در حال ساخت ورکر %s ..." % name)
        upload_worker(cf, acc, name, s["wcode"], s["wkind"])
        try:
            cf.call("POST", "/accounts/%s/workers/scripts/%s/subdomain" % (acc, name), json={"enabled": True})
        except CFError as e:
            log("subdomain enable failed", e)
        urls, notes = ["https://%s.%s.workers.dev" % (name, sub)], []
        if domain:
            try:
                zid, zname = find_zone(cf, domain)
                if not zid:
                    notes.append("دامنه %s توی حساب کلودفلرت پیدا نشد (اول باید به کلودفلر اضافه شده باشه)." % domain)
                else:
                    cf.call("PUT", "/accounts/%s/workers/domains" % acc,
                            json={"environment": "production", "hostname": domain, "service": name, "zone_id": zid})
                    urls.insert(0, "https://" + domain)
            except CFError as e:
                notes.append("وصل کردن دامنه ناموفق بود: %s\n(توکن باید Zone:Read و دسترسی Workers داشته باشه)" % str(e)[:250])
        lines = ["✅ ورکر %s ساخته و دیپلوی شد." % name, ""]
        for u in urls:
            lines.append("🌐 " + u)
        if urls:
            say(chat, "\n".join(lines) + "\n\n🔎 در حال تست آدرس‌ها ...")
            lines = []
            for u in urls:
                status, body = probe(u, 6 if u.endswith(".workers.dev") else 2)
                if status is None:
                    lines.append("⏳ %s هنوز جواب نداد (انتشار DNS/SSL ممکنه چند دقیقه طول بکشه)" % u)
                else:
                    lines.append("%s %s → HTTP %s\n%s" % ("✔" if status < 400 else "⚠️", u, status, body))
        for n in notes:
            lines.append("⚠️ " + n)
        lines.append("\nورکر بعدی: /worker  |  ریپو: /repo")
        say(chat, "\n".join(lines))
        keep_auth(chat)
    except Exception as e:
        fail(chat, e, secrets)


WORKER_PROMPT = (
    "کد ورکر رو بفرست (متن، یا اگه طولانیه به‌صورت فایل .js).\n"
    "هر دو فرمت قبوله: export default { fetch } یا addEventListener('fetch', ...)."
)
NAME_PROMPT = (
    "کد معتبره ✅\n"
    "حالا اسم ورکر رو بفرست (انگلیسی کوچک، عدد و خط تیره).\n"
    "اگه دامنه‌ی خودت رو هم می‌خوای وصل کنم، بعد از اسم بنویس:\n"
    "hello hello.example.com\n"
    "(دامنه باید از قبل توی همین حساب کلودفلر باشه.)"
)


def ask_mode(chat, mode):
    s = sess(chat)
    if mode == "worker":
        s["state"] = "wcode"
        say(chat, WORKER_PROMPT)
    else:
        s["state"] = "repo"
        say(chat, "لینک ریپوی گیت‌هاب رو بفرست.\n"
                  "اگه اسم دلخواه برای ورکر می‌خوای بعد از لینک بنویس:\nhttps://github.com/user/repo myname")


def clear_job(chat):
    old = sessions.get(chat)
    if old and old.get("work"):
        shutil.rmtree(old["work"], ignore_errors=True)
    keep = {k: old[k] for k in ("cf_token", "acc") if old and k in old}
    sessions[chat] = dict(keep, state="idle")


def handle(msg):
    chat = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if msg["chat"].get("type") != "private":
        return
    if ALLOWED and uid not in ALLOWED:
        say(chat, "⛔ دسترسی نداری. آیدی عددی تو: %s" % uid)
        return
    s = sess(chat)
    if text in ("/start", "/help"):
        reset(chat)
        s = sess(chat)
        s.update(state="token", next="repo")
        say(chat, WELCOME)
        return
    if text in ("/repo", "/worker"):
        if s["state"] == "busy":
            say(chat, "⏳ یه عملیات در حال انجامه، صبر کن.")
            return
        mode = text[1:]
        clear_job(chat)
        s = sess(chat)
        if s.get("cf_token"):
            ask_mode(chat, mode)
        else:
            s.update(state="token", next=mode)
            say(chat, "اول Cloudflare API Token رو بفرست (پیامت بعد از خوندن پاک میشه).")
        return
    if text == "/subdomain":
        if not s.get("cf_token"):
            say(chat, "اول /worker یا /repo رو بزن و توکن بده.")
            return
        sub, err = get_subdomain(CloudflareAPI(s["cf_token"]), s["acc"])
        say(chat, ("ساب‌دامین فعلی حساب: %s.workers.dev\n(برای تغییر از داشبورد Workers & Pages استفاده کن؛ ربات اسم موجود رو عوض نمی‌کنه.)" % sub)
            if sub else "ساب‌دامینی پیدا نشد%s. موقع اولین دیپلوی ازت می‌پرسم." % (" (%s)" % err[:120] if err else ""))
        return
    if text == "/logout":
        reset(chat)
        say(chat, "توکن از حافظه‌ی ربات پاک شد.")
        return
    if text == "/cancel":
        clear_job(chat)
        say(chat, "لغو شد. /repo یا /worker")
        return
    st = s["state"]
    if st == "busy":
        say(chat, "⏳ یه عملیات در حال انجامه، صبر کن یا /cancel بزن.")
        return
    if st == "token":
        tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
        try:
            accs = CloudflareAPI(text).call("GET", "/accounts") or []
        except Exception as e:
            say(chat, "❌ توکن معتبر نیست یا اجازه دیدن حساب رو نداره (%s). دوباره بفرست." % scrub(str(e), [text])[:200])
            return
        if not accs:
            say(chat, "❌ این توکن به هیچ حسابی دسترسی نداره. توکن دیگه‌ای بفرست.")
            return
        s["cf_token"] = text
        if len(accs) > 1:
            s["accs"] = accs
            s["state"] = "account"
            say(chat, "چند حساب پیدا شد، شماره‌ی مورد نظر رو بفرست:\n" + "\n".join(
                "%d) %s" % (i + 1, a["name"]) for i, a in enumerate(accs)))
            return
        s["acc"] = accs[0]["id"]
        say(chat, "✅ توکن معتبره (حساب: %s)." % accs[0]["name"])
        ask_mode(chat, s.get("next", "repo"))
        return
    if st == "account":
        if text.isdigit() and 1 <= int(text) <= len(s["accs"]):
            a = s["accs"][int(text) - 1]
            s["acc"] = a["id"]
            say(chat, "✅ حساب %s انتخاب شد." % a["name"])
            ask_mode(chat, s.get("next", "repo"))
        else:
            say(chat, "یه شماره‌ی معتبر بفرست.")
        return
    if st == "repo":
        r = parse_repo(text)
        if not r:
            say(chat, "لینک گیت‌هاب معتبر نیست. مثل: https://github.com/user/repo")
            return
        s["repo"] = r
        if r["name"]:
            start_prepare(chat)
            return
        s["state"] = "rname"
        say(chat, "اسم ورکر رو بفرست (انگلیسی کوچک، عدد و خط تیره).\nدیتابیس و KV هم با همین اسم ساخته میشن.\n"
                  "برای اسم پیش‌فرض (%s) یه - بفرست." % sanitize_name(r["repo"]))
        return
    if st == "rname":
        word = text.split()[0] if text.split() else "-"
        s["repo"]["name"] = None if word == "-" else word
        start_prepare(chat)
        return
    if st == "subname":
        nm = text.strip().lower()
        if not re.match(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", nm):
            say(chat, "اسم معتبر نیست. فقط حروف کوچک انگلیسی، عدد و خط تیره.")
            return
        try:
            set_subdomain(CloudflareAPI(s["cf_token"]), s["acc"], nm)
        except CFError as e:
            say(chat, "❌ %s\nیه اسم دیگه امتحان کن (اسم‌ها بین همه‌ی حساب‌ها یکتان)." % scrub(str(e), [s["cf_token"]])[:250])
            return
        say(chat, "✅ ساب‌دامین حساب: %s.workers.dev" % nm)
        s["state"] = "busy"
        threading.Thread(target=job_worker if s.get("resume") == "worker" else job_deploy, args=(chat,), daemon=True).start()
        return
    if st == "ghtoken":
        tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
        s["gh_token"] = text
        start_prepare(chat)
        return
    if st == "vars":
        vars_ = {}
        if text != "/skip":
            for line in text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    if re.match(r"^[A-Za-z_]\w*$", k.strip()):
                        vars_[k.strip()] = v.strip()
            tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
        s["user_vars"] = vars_
        s["state"] = "busy"
        threading.Thread(target=job_deploy, args=(chat,), daemon=True).start()
        return
    if st == "wcode":
        doc = msg.get("document")
        try:
            if doc:
                if doc.get("file_size", 0) > 2 * 1024 * 1024:
                    say(chat, "فایل بزرگ‌تر از ۲ مگابایته.")
                    return
                code = tg_download(doc["file_id"])
            else:
                code = text
        except Exception as e:
            say(chat, "❌ " + str(e))
            return
        code = strip_fences(code)
        kind = worker_kind(code)
        if not kind:
            say(chat, "❌ کد ورکر معتبر نیست. باید export default { fetch } یا addEventListener('fetch', ...) داشته باشه. دوباره بفرست.")
            return
        if re.search(r"""(from\s+|import\s*\(\s*)['"]\.{1,2}/""", code):
            say(chat, "❌ کد به فایل‌های دیگه import داره (./...). این بخش فقط کد تک‌فایلی رو دیپلوی می‌کنه؛ برای پروژه‌ی چندفایلی از /repo استفاده کن.")
            return
        ok, err = syntax_check(code, kind)
        if not ok:
            say(chat, "❌ کد خطای سینتکس داره:\n" + err + "\n\nاصلاحش کن و دوباره بفرست.")
            return
        s["wcode"], s["wkind"] = code, kind
        s["state"] = "wname"
        say(chat, NAME_PROMPT)
        return
    if st in ("wname", "wconfirm"):
        if st == "wconfirm":
            if text.strip().lower() in ("بله", "yes", "y", "ok", "آره"):
                s["state"] = "busy"
                threading.Thread(target=job_worker, args=(chat,), daemon=True).start()
            else:
                s["state"] = "wname"
                say(chat, "باشه، یه اسم دیگه بفرست.")
            return
        parts = text.split()
        if not parts:
            say(chat, "اسم ورکر رو بفرست.")
            return
        name = sanitize_name(parts[0])
        domain = parts[1].lower() if len(parts) > 1 and parts[1] != "-" else None
        if domain and not re.match(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$", domain):
            say(chat, "دامنه معتبر نیست. مثل: hello.example.com")
            return
        s["wname"], s["wdomain"] = name, domain
        try:
            existing = {x.get("id") for x in CloudflareAPI(s["cf_token"]).call(
                "GET", "/accounts/%s/workers/scripts" % s["acc"]) or []}
        except Exception as e:
            say(chat, "❌ " + scrub(str(e), [s["cf_token"]])[:300])
            return
        if name in existing:
            s["state"] = "wconfirm"
            say(chat, "⚠️ ورکری با اسم %s از قبل وجود داره و جایگزین میشه. ادامه بدم؟ (بله / نه)" % name)
            return
        s["state"] = "busy"
        threading.Thread(target=job_worker, args=(chat,), daemon=True).start()
        return
    say(chat, "برای دیپلوی ریپو /repo و برای ساخت ورکر از کد آماده /worker بزن.")


# --------------------------------------------------------------------------- server / loop
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def health_server():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def keep_alive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while url:
        time.sleep(600)
        try:
            requests.get(url, timeout=20)
        except Exception:
            pass


def main():
    if not TG_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    threading.Thread(target=health_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    tg("deleteWebhook", drop_pending_updates=False)  # polling needs no webhook set
    tg("setMyCommands", commands=[
        {"command": "start", "description": "شروع و وارد کردن توکن"},
        {"command": "repo", "description": "دیپلوی ریپوی گیت‌هاب"},
        {"command": "worker", "description": "ساخت ورکر از کد آماده"},
        {"command": "cancel", "description": "لغو عملیات"},
        {"command": "logout", "description": "پاک کردن توکن"}])
    log("bot started")
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            r = requests.get(TG + "/getUpdates", params=params, timeout=70).json()
        except Exception as e:
            log("poll error", e)
            time.sleep(3)
            continue
        if not r.get("ok"):
            log("getUpdates failed", r)
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            if "message" in u:
                try:
                    handle(u["message"])
                except Exception:
                    log(traceback.format_exc())


if __name__ == "__main__":
    main()
