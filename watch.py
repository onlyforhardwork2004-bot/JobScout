import json, os, re, smtplib, hashlib, datetime
from email.message import EmailMessage
from urllib.parse import urlparse, parse_qs
import requests

def read_lines(path):
    if not os.path.exists(path):
        return []
    return [l.strip() for l in open(path, encoding="utf-8")
            if l.strip() and not l.startswith("#")]

KEYWORDS = [k.lower() for k in read_lines("keywords.txt")]
LOCATIONS = [l.lower() for l in read_lines("locations.txt")]
SOURCES = [[x.strip() for x in l.split("|")] for l in read_lines("sources.txt")]
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

def title_ok(title):
    t = title.lower()
    return any(k in t for k in KEYWORDS)

def location_ok(loc):
    if not LOCATIONS:
        return True
    l = (loc or "").lower()
    if not l or re.match(r"\d+ locations", l):
        return True
    return any(x in l for x in LOCATIONS)

def fetch_workday(url, extra=None):
    m = re.match(r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?]+)", url)
    tenant, wd, site = m.groups()
    host = f"{tenant}.{wd}.myworkdayjobs.com"
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs = {}
    for kw in KEYWORDS:
        offset = 0
        while offset < 100:
            r = requests.post(api, headers=HEADERS, timeout=30, json={
                "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": kw})
            r.raise_for_status()
            posts = r.json().get("jobPostings", [])
            for p in posts:
                jobs[p["externalPath"]] = {
                    "id": p["externalPath"], "title": p["title"],
                    "location": p.get("locationsText", ""),
                    "url": f"https://{host}/en-US/{site}{p['externalPath']}"}
            if len(posts) < 20:
                break
            offset += 20
    return list(jobs.values())

def fetch_eightfold(url, extra=None):
    u = urlparse(url)
    host = u.netloc
    domain = extra or (parse_qs(u.query).get("domain") or [None])[0]
    if not domain:
        domain = re.sub(r"^(careers|jobs|www)\.", "", host)
    api = f"https://{host}/api/apply/v2/jobs"
    jobs = {}
    for kw in KEYWORDS:
        start = 0
        while start < 100:
            r = requests.get(api, headers=HEADERS, timeout=30, params={
                "domain": domain, "start": start, "num": 10, "query": kw})
            r.raise_for_status()
            posts = r.json().get("positions", [])
            for p in posts:
                jid = str(p["id"])
                jobs[jid] = {
                    "id": jid, "title": p.get("name", ""),
                    "location": p.get("location", ""),
                    "url": p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{jid}"}
            if len(posts) < 10:
                break
            start += 10
    return list(jobs.values())

def fetch_greenhouse(url, extra=None):
    slug = url.rstrip("/").split("/")[-1]
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [{"id": str(j["id"]), "title": j["title"],
             "location": (j.get("location") or {}).get("name", ""),
             "url": j["absolute_url"]} for j in r.json()["jobs"]]

def fetch_lever(url, extra=None):
    slug = url.rstrip("/").split("/")[-1]
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [{"id": j["id"], "title": j["text"],
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j["hostedUrl"]} for j in r.json()]

def fetch_pagechange(url, extra=None):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 500:
        raise Exception("page looks empty (it needs JavaScript) - send the link to Claude")
    h = hashlib.md5(text.encode()).hexdigest()[:12]
    return [{"id": h, "title": "Page changed - open it and check for new jobs",
             "location": "", "url": url, "force": True}]

FETCHERS = {"workday": fetch_workday, "eightfold": fetch_eightfold,
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

def fmt(jobs):
    return "\n\n".join(f"{n}: {j['title']}\n{j['location']}\n{j['url']}" for n, j in jobs)

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
                raise Exception(f"type '{parts[1]}' is not supported - use workday, eightfold, greenhouse, lever or pagechange, or send the link to Claude")
            jobs = fetcher(url, extra)
            ok += 1
        except Exception as e:
            failures.append(f"{name}: {e}")
            continue
        for j in jobs:
            if not (j.get("force") or (title_ok(j["title"]) and location_ok(j["location"]))):
                continue
            key = f"{name}|{j['id']}"
            if key not in seen:
                seen.add(key)
                new_jobs.append((name, j))
    json.dump(sorted(seen), open("seen.json", "w"), indent=1)

    if first_run:
        send("Job watcher started",
             f"Checked {ok} sources OK, {len(failures)} failed:\n" + "\n".join(failures)
             + f"\n\nCurrently open matching jobs:\n\n{fmt(new_jobs)}")
    elif new_jobs:
        send(f"{len(new_jobs)} new matching job(s)", fmt(new_jobs))

    now = datetime.datetime.now(datetime.timezone.utc)
    if not first_run and now.hour == 4 and now.minute < 30:
        send("Job watcher daily check",
             f"Checked {ok} sources OK, {len(failures)} failed.\n" + "\n".join(failures))

main()
