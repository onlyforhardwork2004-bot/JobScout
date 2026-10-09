import json, os, re, smtplib, hashlib, datetime, time
from email.message import EmailMessage
from urllib.parse import urlparse, parse_qs, quote
import requests

def read_lines(path):
    if not os.path.exists(path):
        return []
    return [l.strip() for l in open(path, encoding="utf-8")
            if l.strip() and not l.startswith("#")]

KEYWORDS = [k.lower() for k in read_lines("keywords.txt")]
LOCATIONS = [l.lower() for l in read_lines("locations.txt")]
EXCLUDE = [x.lower() for x in read_lines("exclude.txt")]
FRESHER = [x.lower() for x in read_lines("fresher.txt")]
SOURCES = [[x.strip() for x in l.split("|")] for l in read_lines("sources.txt")]

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

def http(method, url, **kw):
    kw.setdefault("timeout", 30)
    headers = dict(BROWSER)
    headers.update(kw.pop("headers", None) or {})
    last = None
    for attempt in range(3):
        r = requests.request(method, url, headers=headers, **kw)
        if r.status_code == 429:
            last = r
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(0.4)
        return r
    last.raise_for_status()
    return last

def has_word(text, words):
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in words)

def title_ok(title):
    return has_word(title, KEYWORDS) and not has_word(title, EXCLUDE)

def is_fresh(title):
    return has_word(title, FRESHER)

def location_ok(loc):
    if not LOCATIONS:
        return True
    l = (loc or "").lower()
    if not l or re.match(r"\d+ locations", l):
        return True
    return any(x in l for x in LOCATIONS)

# ---------- Workday ----------
def fetch_workday(url, extra=None):
    m = re.match(r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?]+)", url)
    if m:
        tenant, wd, site = m.groups()
        host = f"{tenant}.{wd}.myworkdayjobs.com"
        job_base = f"https://{host}/en-US/{site}"
    else:
        m = re.match(r"https://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([^/]+)/([^/?]+)", url)
        if not m:
            raise Exception("cannot understand this Workday link")
        wd, tenant, site = m.groups()
        host = f"{wd}.myworkdaysite.com"
        job_base = f"https://{host}/en-US/recruiting/{tenant}/{site}"
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs = {}
    for kw in KEYWORDS:
        offset = 0
        while offset < 100:
            r = http("POST", api, json={
                "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": kw})
            posts = r.json().get("jobPostings", [])
            for p in posts:
                loc = p.get("locationsText", "")
                if re.match(r"\d+ locations", loc.lower()):
                    parts = p["externalPath"].split("/")
                    if len(parts) > 2:
                        loc = parts[2]
                jobs[p["externalPath"]] = {
                    "id": p["externalPath"], "title": p["title"],
                    "location": loc, "url": job_base + p["externalPath"]}
            if len(posts) < 20:
                break
            offset += 20
    return list(jobs.values())

# ---------- Eightfold ----------
def eightfold_domain(url, extra):
    if extra:
        return extra
    u = urlparse(url)
    d = (parse_qs(u.query).get("domain") or [None])[0]
    if d:
        return d
    labels = u.netloc.split(".")
    if u.netloc.endswith("eightfold.ai"):
        return labels[0] + ".com"
    return ".".join(labels[-2:])

def eightfold_new(host, domain, kw):
    out, start = [], 0
    while start < 50:
        r = http("GET", f"https://{host}/api/pcsx/search",
                 params={"domain": domain, "query": kw, "location": "",
                         "start": start, "sort_by": "timestamp"})
        d = r.json()
        data = d.get("data") or d
        posts = data.get("positions")
        if posts is None:
            raise Exception("unexpected reply")
        for p in posts:
            loc = p.get("locations") or p.get("location") or ""
            if isinstance(loc, list):
                loc = "; ".join(loc)
            path = p.get("positionUrl") or f"/careers/job/{p['id']}"
            out.append({"id": str(p["id"]), "title": p.get("name", ""), "location": loc,
                        "url": path if path.startswith("http") else f"https://{host}{path}"})
        if not posts:
            break
        start += len(posts)
    return out

def eightfold_old(host, domain, kw):
    out, start = [], 0
    while start < 50:
        r = http("GET", f"https://{host}/api/apply/v2/jobs",
                 params={"domain": domain, "start": start, "num": 10, "query": kw})
        posts = r.json().get("positions")
        if posts is None:
            raise Exception("unexpected reply")
        for p in posts:
            jid = str(p["id"])
            out.append({"id": jid, "title": p.get("name", ""), "location": p.get("location", ""),
                        "url": p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{jid}"})
        if len(posts) < 10:
            break
        start += 10
    return out

def fetch_eightfold(url, extra=None):
    host = urlparse(url).netloc
    domain = eightfold_domain(url, extra)
    jobs = {}
    for kw in KEYWORDS:
        try:
            posts = eightfold_new(host, domain, kw)
        except Exception as e1:
            try:
                posts = eightfold_old(host, domain, kw)
            except Exception as e2:
                raise Exception(f"both Eightfold endpoints failed ({str(e1)[:90]}; {str(e2)[:90]})")
        for p in posts:
            jobs[p["id"]] = p
    return list(jobs.values())

# ---------- Oracle (Candidate Experience) ----------
def fetch_oracle(url, extra=None):
    u = urlparse(url)
    host = u.netloc
    m = re.search(r"/sites/([^/?]+)", u.path)
    site = m.group(1) if m else "CX"
    prefix = u.path.split("/sites/")[0]
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    def call(sn, kw, limit, offset):
        finder = f"findReqs;siteNumber={sn},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
        if kw:
            finder += f",keyword=%22{quote(kw)}%22"
        r = http("GET", f"{api}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")
        items = r.json().get("items") or []
        return items[0] if items else {}

    candidates = [extra] if extra else [site, f"{site}_1", f"{site}_1001", "CX_1", "CX_1001"]
    sn, errors = None, []
    for c in candidates:
        try:
            if call(c, "", 1, 0).get("TotalJobsCount", 0) > 0:
                sn = c
                break
            errors.append(f"{c}: 0 jobs")
        except Exception as e:
            errors.append(f"{c}: {str(e)[:70]}")
    if not sn:
        raise Exception("could not find this site's Oracle number (" + "; ".join(errors[:3]) + ") - send the link to Claude")
    jobs = {}
    for kw in KEYWORDS:
        offset = 0
        while offset < 100:
            reqs = call(sn, kw, 25, offset).get("requisitionList") or []
            for q in reqs:
                jid = str(q["Id"])
                jobs[jid] = {"id": jid, "title": q.get("Title", ""),
                             "location": q.get("PrimaryLocation", ""),
                             "url": f"https://{host}{prefix}/sites/{site}/job/{jid}"}
            if len(reqs) < 25:
                break
            offset += 25
    return list(jobs.values())

# ---------- Jibe ----------
def fetch_jibe(url, extra=None):
    host = urlparse(url).netloc
    base = url.split("?")[0].rstrip("/")
    prefix = urlparse(base).path.rsplit("/jobs", 1)[0]
    apis = [f"https://{host}/api/jobs", f"https://{host}{prefix}/api/jobs"]
    working = []

    def get(params):
        last = None
        for api in (working or apis):
            try:
                data = http("GET", api, params=params).json()
                if "jobs" not in data:
                    raise Exception("unexpected reply")
                if not working:
                    working.append(api)
                return data
            except Exception as e:
                last = e
        raise last

    jobs = {}
    for kw in KEYWORDS:
        page = 1
        while page <= 5:
            posts = get({"keywords": kw, "page": page, "limit": 50}).get("jobs", [])
            for p in posts:
                d = p.get("data", p)
                jid = str(d.get("slug") or d.get("req_id") or d.get("id"))
                loc = (d.get("location_name") or d.get("full_location")
                       or ", ".join(x for x in [d.get("city"), d.get("state"), d.get("country")] if x))
                jobs[jid] = {"id": jid, "title": d.get("title", ""), "location": loc,
                             "url": f"{base}/{jid}"}
            if len(posts) < 50:
                break
            page += 1
    return list(jobs.values())

# ---------- Amazon ----------
def fetch_amazon(url, extra=None):
    loc = "India" if any("india" in l for l in LOCATIONS) else ""
    jobs = {}
    for kw in KEYWORDS:
        offset = 0
        while offset < 200:
            r = http("GET", "https://www.amazon.jobs/en/search.json",
                     params={"base_query": kw, "loc_query": loc, "result_limit": 100,
                             "offset": offset, "sort": "recent"})
            posts = r.json().get("jobs", [])
            for p in posts:
                jid = str(p.get("id_icims") or p.get("id"))
                jobs[jid] = {"id": jid, "title": p.get("title", ""),
                             "location": p.get("normalized_location") or p.get("location", ""),
                             "url": "https://www.amazon.jobs" + p.get("job_path", "")}
            if len(posts) < 100:
                break
            offset += 100
    return list(jobs.values())

# ---------- Greenhouse / Lever ----------
def fetch_greenhouse(url, extra=None):
    slug = extra or url.rstrip("/").split("/")[-1]
    r = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [{"id": str(j["id"]), "title": j["title"],
             "location": (j.get("location") or {}).get("name", ""),
             "url": j["absolute_url"]} for j in r.json()["jobs"]]

def fetch_lever(url, extra=None):
    slug = extra or url.rstrip("/").split("/")[-1]
    r = http("GET", f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [{"id": j["id"], "title": j["text"],
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j["hostedUrl"]} for j in r.json()]

# ---------- Page change ----------
def fetch_pagechange(url, extra=None):
    r = http("GET", url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 500:
        raise Exception("page looks empty (it needs JavaScript) - send the link to Claude")
    h = hashlib.md5(text.encode()).hexdigest()[:12]
    return [{"id": h, "title": "Page changed - open it and check for new jobs",
             "location": "", "url": url, "force": True}]

FETCHERS = {"workday": fetch_workday, "eightfold": fetch_eightfold,
            "oracle": fetch_oracle, "jibe": fetch_jibe, "amazon": fetch_amazon,
            "greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "pagechange": fetch_pagechange}

def send(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = os.environ["TO_EMAILS"]
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)

def fmt(jobs, limit=200):
    def line(n, j):
        return f"{n}: {j['title']}\n{j['location']}\n{j['url']}"
    pages = [x for x in jobs if x[1].get("force")]
    rest = [x for x in jobs if not x[1].get("force")]
    fresh = [x for x in rest if is_fresh(x[1]["title"])]
    other = [x for x in rest if not is_fresh(x[1]["title"])]
    out = []
    if fresh:
        out.append("=== FRESHER / INTERN FRIENDLY ===\n\n" + "\n\n".join(line(*x) for x in fresh))
    if pages:
        out.append("=== PAGES THAT CHANGED (open and check) ===\n\n" + "\n\n".join(line(*x) for x in pages))
    if other:
        s = "=== OTHER MATCHES (check the years of experience required) ===\n\n" + "\n\n".join(line(*x) for x in other[:limit])
        if len(other) > limit:
            s += f"\n\n...and {len(other) - limit} more"
        out.append(s)
    return "\n\n\n".join(out) or "(none)"

def main():
    first_run = not os.path.exists("seen.json")
    seen = set() if first_run else set(json.load(open("seen.json")))
    new_jobs, failures, ok = [], [], 0
    for parts in SOURCES:
        name = parts[0] if parts else "?"
        try:
            if len(parts) < 3:
                raise Exception("line must look like: Name | type | link")
            kind, url = parts[1].lower(), parts[2]
            extra = parts[3] if len(parts) > 3 else None
            fetcher = FETCHERS.get(kind)
            if not fetcher:
                raise Exception(f"type '{parts[1]}' is not supported - send the link to Claude")
            jobs = fetcher(url, extra)
            ok += 1
        except Exception as e:
            failures.append(f"{name}: {str(e)[:300]}")
            continue
        for j in jobs:
            if not (j.get("force") or (title_ok(j["title"]) and location_ok(j["location"]))):
                continue
            key = f"{name}|{j['id']}"
            if key not in seen:
                seen.add(key)
                if first_run and j.get("force"):
                    continue
                new_jobs.append((name, j))
    json.dump(sorted(seen), open("seen.json", "w"), indent=1)

    if first_run:
        send("Job watcher started",
             f"Checked {ok} sources OK, {len(failures)} failed:\n" + "\n".join(failures)
             + f"\n\nCurrently open matching jobs:\n\n{fmt(new_jobs)}")
    elif new_jobs:
        nf = sum(1 for _, j in new_jobs if not j.get("force") and is_fresh(j["title"]))
        send(f"{nf} fresher/intern + {len(new_jobs) - nf} other new job(s)", fmt(new_jobs))

    now = datetime.datetime.now(datetime.timezone.utc)
    if not first_run and now.hour == 4:
        send("Job watcher daily check",
             f"Checked {ok} sources OK, {len(failures)} failed.\n" + "\n".join(failures))

main()
