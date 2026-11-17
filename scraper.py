#!/usr/bin/env python3
"""
NUPCO Tender Tracker  (v2 - "never miss a tender" edition)

Design rules
------------
1. AT-LEAST-ONCE delivery.  A tender is only marked as "notified" AFTER Telegram
   confirmed delivery.  If Telegram fails, the tender stays pending and is retried
   on every following run until it is delivered.
2. UNION, never overwrite.  State only ever grows (old tenders are pruned after
   PRUNE_DAYS).  A partial / broken scrape can therefore never erase history.
3. CHANGE detection.  A tender is re-announced when its status, submission
   deadline or bid-opening date changes (e.g. "Available / Updated", extensions,
   cancellations) - not just when its ID appears for the first time.
4. FAIL LOUD.  Zero parsed tenders, a blocked request or a changed page layout
   raises an error, is counted, and triggers a Telegram health alert.
5. MULTIPLE SOURCES.  HTML list (+ pagination)  ->  WordPress REST API  ->
   XML sitemap.  Anything the HTML list does not show but the other sources do is
   picked up as well.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE = "https://www.nupco.com"
LIST_URL = os.getenv("NUPCO_LIST_URL", f"{BASE}/tenders/tenders-list/")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
LEGACY_FILE = Path(os.getenv("LEGACY_FILE", "last_seen.txt"))

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_ON_FIRST_RUN = os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"
# Send a short "check finished, nothing new" message after every clean run.
# Set NOTIFY_NO_CHANGE=false in the workflow to switch it off.
NOTIFY_NO_CHANGE = os.getenv("NOTIFY_NO_CHANGE", "true").lower() == "true"

MAX_PAGES = 40            # hard cap on listing pages followed
LOOKBACK_DAYS = 7         # REST/sitemap safety-net only looks at recent items
MAX_DETAIL_FETCHES = 30   # cap on extra detail pages fetched per run
HOLD_HOURS = 24          # ID-less records are held this long while we keep trying to resolve the ID
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))
FAIL_ALERT_AFTER = 3      # consecutive failed runs before a health alert
HEARTBEAT_DAYS = 6        # rewrite state at least this often (keeps repo "active")
PRUNE_DAYS = 400          # forget tenders not seen for this long
TG_CHUNK = 3800           # Telegram hard limit is 4096 chars
KNOWN_STATUSES = {
    "available / new", "available / updated", "cancelled", "direct purchase",
    "final results", "initial results", "under studying",
}
SILENT_OFFLIST_STATUSES = {"final results", "initial results"}
TRACKED_FIELDS = (("status", "Status"), ("deadline", "Submission deadline"), ("opening", "Bid opening"))

# NUPCO's own pages spell it "Tendr Id" (typo) - accept every variant, plus Arabic.
LABEL_RE = re.compile(r"^\s*(?:t[e]?nd[e]?r\s*(?:id|no\.?|number)|رقم\s*المنافسة)\s*:?\s*$", re.I)
# Real NUPCO IDs look like NDP0846/26, NPT0006/24 - usable even when the label is missing.
TID_RE = re.compile(r"^\s*([A-Za-z]{2,5}\s?\d{2,6}\s?[/\-]\s?\d{2,4})\s*$")
TID_FIND_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5}\d{3,6}/\d{2})(?![0-9])")
DEADLINE_RE = re.compile(r"^\s*submission\s*deadline\s*:?\s*$", re.I)
OPENING_RE = re.compile(r"^\s*bid\s*opening\s*:?\s*$", re.I)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/\-_.]{2,39}$")
BLOCK_MARKERS = ("just a moment", "cf-chl", "access denied", "attention required",
                 "captcha", "request unsuccessful", "incapsula")

log = logging.getLogger("nupco")


class ScrapeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canon(url: str | None) -> str:
    """Canonical form of a tender URL (decoded, no scheme/www/trailing slash)."""
    if not url:
        return ""
    p = urlparse(unquote(url.strip()))
    return p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")


def snapshot(rec: dict) -> dict:
    return {k: norm(rec.get(k)) for k, _ in TRACKED_FIELDS}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, connect=4, read=4, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "HEAD"]),
                  respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


def fetch(session: requests.Session, url: str, bust: bool = False,
          allow_404: bool = False, params: dict | None = None) -> str | None:
    q = dict(params or {})
    if bust:  # defeat CDN / page-cache staleness
        q["nc"] = str(int(time.time()))
    r = session.get(url, params=q or None, timeout=(10, 45))
    if r.status_code == 404 and allow_404:
        return None
    r.raise_for_status()
    # requests guesses ISO-8859-1 when no charset is sent -> Arabic would be garbled
    return r.content.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Parsing  (layout-agnostic: anchored on the "Tender ID" label, not on CSS classes)
# --------------------------------------------------------------------------- #
def _tid_norm(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _climb(node, count_fn):
    """Largest ancestor that still contains exactly ONE distinct tender ID."""
    card = node.parent
    while (card.parent is not None
           and card.parent.name not in ("body", "html", "[document]")
           and count_fn(card.parent) == 1):
        card = card.parent
    return card


def parse_cards(soup: BeautifulSoup, page_url: str, default_url: str | None = None) -> list[dict]:
    """Layout-agnostic.  Primary anchor = text that LOOKS like a NUPCO tender ID
    (NDP0846/26).  Fallback anchor = the 'Tender ID' / 'Tendr Id' label."""
    def ids_in(el) -> int:
        return len({_tid_norm(x) for x in el.find_all(string=TID_RE)})

    anchors = [(n, _tid_norm(n)) for n in soup.find_all(string=TID_RE)]
    if anchors:
        count_fn = ids_in
    else:                                   # label-only layouts (ID in a non-standard format)
        def count_fn(el):
            return len(el.find_all(string=LABEL_RE))
        anchors = []
        for lab in soup.find_all(string=LABEL_RE):
            card = _climb(lab, count_fn)
            strings = [x for x in (norm(t) for t in card.stripped_strings) if x]
            i = next((k for k, t in enumerate(strings) if LABEL_RE.match(t)), None)
            if i is not None and i + 1 < len(strings) and ID_RE.match(strings[i + 1]) \
                    and re.search(r"\d", strings[i + 1]):
                anchors.append((lab, strings[i + 1]))

    items: list[dict] = []
    seen: set[str] = set()
    for node, tid in anchors:
        if tid in seen:
            continue
        seen.add(tid)
        card = _climb(node, count_fn)
        strings = [x for x in (norm(t) for t in card.stripped_strings) if x]

        link = card.find("a", href=re.compile(r"/tenders?(_post)?/"))
        url = urljoin(page_url, link["href"]) if link else (default_url or "")
        heading = card.find("h1") if default_url else None
        heading = heading or card.find(["h1", "h2", "h3", "h4", "h5"])
        title = norm(heading.get_text(" ")) if heading else (norm(link.get_text(" ")) if link else "")
        if title and (LABEL_RE.match(title) or TID_RE.match(title)):    # heading was the ID label itself
            title = ""

        status = next((x for x in strings if x.lower() in KNOWN_STATUSES), "")
        deadline = opening = ""
        for i, x in enumerate(strings[:-1]):
            if DEADLINE_RE.match(x) and not deadline:
                deadline = strings[i + 1]
            elif OPENING_RE.match(x) and not opening:
                opening = strings[i + 1]

        items.append({"id": tid, "url": url, "title": title, "status": status,
                      "deadline": deadline, "opening": opening})
    return items


def parse_detail(html_text: str, url: str, title_hint: str = "") -> dict | None:
    """Parse ONE tender page (or REST content).  Last resort: a single ID anywhere in the text."""
    soup = BeautifulSoup(html_text, "html.parser")
    got = parse_cards(soup, url, default_url=url)
    if got:
        item = got[0]
    else:
        ids = {m for m in TID_FIND_RE.findall(soup.get_text(" "))}
        if len(ids) != 1:
            return None
        item = {"id": ids.pop(), "url": url, "title": "", "status": "", "deadline": "", "opening": ""}
        text = [x for x in (norm(t) for t in soup.stripped_strings) if x]
        for i, x in enumerate(text[:-1]):
            if DEADLINE_RE.match(x) and not item["deadline"]:
                item["deadline"] = text[i + 1]
            elif OPENING_RE.match(x) and not item["opening"]:
                item["opening"] = text[i + 1]
    item["url"] = item["url"] or url
    if not item["title"]:
        h1 = soup.find("h1")
        item["title"] = norm(h1.get_text(" ")) if h1 else title_hint
    return item


def same_listing_path(url: str, list_url: str = LIST_URL) -> bool:
    a, b = urlparse(url), urlparse(list_url)
    return a.netloc.lower().removeprefix("www.") == b.netloc.lower().removeprefix("www.") \
        and a.path.startswith(b.path.rstrip("/"))


def next_links(soup: BeautifulSoup, page_url: str, list_url: str = LIST_URL) -> list[str]:
    found: list[str] = []
    for el in soup.find_all(["a", "link"], rel="next"):
        if el.get("href"):
            found.append(urljoin(page_url, el["href"]))
    for a in soup.select("a.page-numbers, .pagination a, .elementor-pagination a, "
                         "a[href*='paged='], a[href*='/page/']"):
        if a.get("href") and "#" not in a["href"]:
            found.append(urljoin(page_url, a["href"]))
    out, seen = [], set()
    for u in found:
        if same_listing_path(u, list_url) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def dump_debug(name: str, text: str, note: str) -> None:
    """Keep what the bot actually received, so a layout change can be diagnosed from the Actions run."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{name}.html").write_text(text[:300_000], encoding="utf-8")
        (DEBUG_DIR / f"{name}.txt").write_text(note, encoding="utf-8")
    except OSError:
        pass


LIST_URLS = list(dict.fromkeys([LIST_URL, f"{BASE}/en/tenders/tenders-list/", f"{BASE}/en/tenders/"]))


def scrape_listing(session: requests.Session) -> list[dict]:
    """Try each known listing URL until one yields tenders."""
    last: Exception | None = None
    for u in LIST_URLS:
        try:
            items = _scrape_one(session, u)
            if items:
                return items
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("Listing %s failed: %s: %s", u, type(e).__name__, e)
    raise last or ScrapeError("No listing URL returned tenders.")


def _scrape_one(session: requests.Session, list_url: str) -> list[dict]:
    """Return every tender on one HTML list, following pagination."""
    items: dict[str, dict] = {}
    visited: set[str] = set()
    queue = [list_url]
    first_text = ""
    while queue and len(visited) < MAX_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        text = fetch(session, url, bust=True)
        if len(visited) == 1:
            first_text = text or ""
        soup = BeautifulSoup(text, "html.parser")
        page_items = parse_cards(soup, url)
        fresh = [i for i in page_items if i["id"] not in items]
        for i in fresh:
            items[i["id"]] = i
        if len(visited) > 1 and not fresh:
            continue  # nothing new here -> do not expand further from this page
        queue.extend(n for n in next_links(soup, url, list_url) if n not in visited)

    if not items:
        low = first_text.lower()
        sample = norm(BeautifulSoup(first_text, "html.parser").get_text(" "))[:400]
        dump_debug("list_page", first_text, f"url={list_url}\nbytes={len(first_text)}\nsample={sample}")
        if any(m in low for m in BLOCK_MARKERS):
            raise ScrapeError("Request was blocked (WAF / bot challenge page received).")
        raise ScrapeError(f"{list_url}: {len(first_text)} bytes received but no tender IDs parsed "
                          "(layout changed or list is JavaScript-rendered).")

    # No pagination links advertised?  Probe /page/N/ anyway (WordPress default).
    if len(visited) == 1:
        for n in range(2, MAX_PAGES + 1):
            purl = list_url.rstrip("/") + f"/page/{n}/"
            try:
                text = fetch(session, purl, bust=True, allow_404=True)
            except requests.RequestException:
                break
            if text is None:
                break
            fresh = [i for i in parse_cards(BeautifulSoup(text, "html.parser"), purl)
                     if i["id"] not in items]
            if not fresh:
                break
            for i in fresh:
                items[i["id"]] = i
    return list(items.values())


# --------------------------------------------------------------------------- #
# Safety-net sources (best effort - never allowed to break the run)
# --------------------------------------------------------------------------- #
def _is_tender_url(url: str) -> bool:
    return re.match(r"^/(?:en/)?(?:tender|tenders_post)/", urlparse(unquote(url)).path) is not None


def rest_candidates(session: requests.Session, cutoff: datetime) -> list[dict]:
    out: list[dict] = []
    r = session.get(f"{BASE}/wp-json/wp/v2/types", timeout=(10, 30))
    if r.status_code != 200:
        return out
    bases = [t.get("rest_base") for t in r.json().values()
             if "tender" in f"{t.get('slug', '')}{t.get('rest_base', '')}".lower()]
    for rb in filter(None, bases):
        for page in range(1, 6):
            rr = session.get(f"{BASE}/wp-json/wp/v2/{rb}", timeout=(10, 30), params={
                "per_page": 50, "page": page, "orderby": "date", "order": "desc",
                "_fields": "link,title,date_gmt,content"})
            if rr.status_code != 200:
                break
            rows = rr.json()
            if not rows:
                break
            stop = False
            for row in rows:
                dt = parse_iso(row.get("date_gmt"))
                if dt and dt < cutoff:
                    stop = True
                    continue
                link = row.get("link") or ""
                if link and _is_tender_url(link):
                    out.append({"url": link, "html": (row.get("content") or {}).get("rendered", ""),
                                "title": norm(html.unescape((row.get("title") or {}).get("rendered", "")))})
            if stop:
                break
    return out


def sitemap_candidates(session: requests.Session, cutoff: datetime) -> list[dict]:
    def locs(xml_text: str):
        root = ET.fromstring(xml_text.encode("utf-8"))
        for node in root.iter():
            if node.tag.split("}")[-1] in ("url", "sitemap"):
                loc = lastmod = ""
                for ch in node:
                    t = ch.tag.split("}")[-1]
                    if t == "loc":
                        loc = (ch.text or "").strip()
                    elif t == "lastmod":
                        lastmod = (ch.text or "").strip()
                yield loc, lastmod

    out: list[dict] = []
    for entry in ("/wp-sitemap.xml", "/sitemap_index.xml", "/sitemap.xml"):
        r = session.get(BASE + entry, timeout=(10, 30))
        if r.status_code != 200 or "<" not in r.text[:200]:
            continue
        children = [loc for loc, _ in locs(r.text) if "tender" in loc.lower()]
        for child in children[:10]:
            cr = session.get(child, timeout=(10, 30))
            if cr.status_code != 200:
                continue
            for loc, lastmod in locs(cr.text):
                dt = parse_iso(lastmod)
                if loc and _is_tender_url(loc) and (dt is None or dt >= cutoff):
                    out.append({"url": loc, "title": ""})
        if children:
            break
    return out


def discover_extra(session: requests.Session, state: dict, list_items: list[dict]) -> list[dict]:
    cutoff = utcnow() - timedelta(days=LOOKBACK_DAYS)
    known = {canon(t.get("url")) for t in state["tenders"].values() if t.get("url")}
    known |= {canon(i["url"]) for i in list_items if i.get("url")}
    cands: dict[str, dict] = {}
    for name, fn in (("REST API", rest_candidates), ("sitemap", sitemap_candidates)):
        try:
            for c in fn(session, cutoff):
                cands.setdefault(canon(c["url"]), c)
        except Exception as e:  # noqa: BLE001 - safety net must never break the run
            log.info("%s source unavailable (%s)", name, type(e).__name__)
    unknown = [c for k, c in cands.items() if k and k not in known][:MAX_DETAIL_FETCHES]
    # Re-try records that were stored WITHOUT an ID on earlier runs.
    for t in state["tenders"].values():
        if not t.get("id") and t.get("url") and len(unknown) < MAX_DETAIL_FETCHES * 2:
            unknown.append({"url": t["url"], "title": t.get("title", ""), "html": ""})
    extra: list[dict] = []
    for c in unknown:
        item = None
        try:
            text = fetch(session, c["url"], bust=True)
            item = parse_detail(text, c["url"], c.get("title", ""))
            if item is None:
                log.warning("No tender ID found on detail page %s", c["url"])
        except Exception as e:  # noqa: BLE001
            log.warning("detail fetch failed for %s (%s: %s)", c["url"], type(e).__name__, e)
        if item is None and c.get("html"):                      # REST gave us the body already
            item = parse_detail(c["html"], c["url"], c.get("title", ""))
        if item is None:
            slug = unquote(urlparse(c["url"]).path.rstrip("/").split("/")[-1]).replace("-", " ")
            item = {"id": "", "url": c["url"], "title": c.get("title") or slug,
                    "status": "", "deadline": "", "opening": ""}
        item["source"] = "offlist"
        extra.append(item)
    if extra:
        log.info("Safety-net sources contributed %d tender(s) not on the HTML list", len(extra))
    return extra


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def new_state() -> dict:
    return {"version": 2, "meta": {}, "health": {"failures": 0, "alerted": False}, "tenders": {}}


def load_state(now: datetime) -> tuple[dict, bool]:
    """Returns (state, is_first_run)."""
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            # Refuse to continue: silently resetting would re-announce everything.
            raise SystemExit(f"{STATE_FILE} is corrupt ({e}). Fix or restore it from git history.")
        st.setdefault("meta", {})
        st.setdefault("health", {"failures": 0, "alerted": False})
        st.setdefault("tenders", {})
        return st, False

    st = new_state()
    if LEGACY_FILE.exists():  # migrate old last_seen.txt silently
        ids = [l.strip() for l in LEGACY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        ids = [i for i in ids if i != "0"]
        for tid in ids:
            st["tenders"][tid] = {"id": tid, "url": "", "title": "", "status": "", "deadline": "",
                                  "opening": "", "first_seen": iso(now), "last_seen": iso(now),
                                  "notified": "legacy", "source": "legacy"}
        if ids:
            log.info("Migrated %d tender IDs from %s", len(ids), LEGACY_FILE)
            return st, False
    return st, True


def save_state(state: dict, now: datetime) -> None:
    core = json.dumps({k: v for k, v in state.items() if k != "meta"},
                      sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(core.encode("utf-8")).hexdigest()
    meta = state["meta"]
    last_write = parse_iso(meta.get("last_write"))
    if (meta.get("core_sha") == digest and STATE_FILE.exists()
            and last_write and now - last_write < timedelta(days=HEARTBEAT_DAYS)):
        log.info("State unchanged - not rewriting file.")
        return
    meta["core_sha"] = digest
    meta["last_write"] = iso(now)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(STATE_FILE)


def merge(state: dict, items: list[dict], now: datetime, source: str) -> int:
    """Union-merge scraped items into state.  Returns number of brand-new records."""
    tenders = state["tenders"]
    created = 0
    for it in items:
        tid = it.get("id") or ""
        curl = canon(it.get("url"))
        by_url = {canon(t.get("url")): k for k, t in tenders.items() if t.get("url")}
        key = tid or ("url:" + curl)
        if not tid and not curl:
            continue

        rec = tenders.get(key)
        if rec is not None and tid and curl in by_url and by_url[curl].startswith("url:") \
                and by_url[curl] != key:
            tenders.pop(by_url[curl], None)                 # real record already exists -> drop the stub
        if rec is None and curl in by_url:
            old_key = by_url[curl]
            if tid and old_key.startswith("url:"):          # stub gets its real ID
                rec = tenders.pop(old_key)
                rec["id"] = tid
                tenders[key] = rec
                rec["notified"] = None                     # earlier alert had no ID -> announce again, properly
            elif not tid:
                rec, key = tenders[old_key], old_key
        if rec is None:
            rec = {"id": tid, "url": "", "title": "", "status": "", "deadline": "", "opening": "",
                   "first_seen": iso(now), "notified": None,
                   "source": it.get("source", source)}
            tenders[key] = rec
            created += 1
            fresh = True
        else:
            fresh = False

        for f in ("url", "title", "status", "deadline", "opening"):
            if norm(it.get(f)):                              # never overwrite with empty
                rec[f] = norm(it[f]) if f != "url" else it[f].strip()
        rec["last_seen"] = iso(now)

        if rec.get("notified") == "legacy":                 # first real data for a migrated ID
            rec["notified"] = snapshot(rec)
        elif (fresh and rec["source"] == "offlist"
              and rec.get("status", "").lower() in SILENT_OFFLIST_STATUSES):
            rec["notified"] = snapshot(rec)                 # old results published off-list: no alert
    return created


def prune(state: dict, now: datetime) -> None:
    cutoff = now - timedelta(days=PRUNE_DAYS)
    for k in list(state["tenders"]):
        t = state["tenders"][k]
        seen = parse_iso(t.get("last_seen"))
        if seen and seen < cutoff and t.get("notified") is not None:
            del state["tenders"][k]


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def tg_send(text: str, html_mode: bool = True) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not configured.")
        return False
    api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if html_mode:
        payload["parse_mode"] = "HTML"
    for attempt in range(1, 5):
        try:
            r = requests.post(api, json=payload, timeout=20)
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code == 200 and data.get("ok"):
                return True
            desc = str(data.get("description", ""))
            if r.status_code == 429:
                time.sleep(int(data.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            if r.status_code == 400 and "parse" in desc.lower() and html_mode:
                plain = html.unescape(re.sub(r"<[^>]+>", "", text))
                return tg_send(plain, html_mode=False)
            log.error("Telegram HTTP %s: %s (attempt %d)", r.status_code, desc, attempt)
            if r.status_code in (400, 401, 403, 404):
                return False                                # retrying will not help
        except requests.RequestException as e:
            log.error("Telegram request failed: %s (attempt %d)", type(e).__name__, attempt)
        time.sleep(2 ** attempt)
    return False


def pending_items(state: dict, now: datetime | None = None) -> list[tuple[str, str, list[str]]]:
    now = now or utcnow()
    out = []
    for key, rec in state["tenders"].items():
        prev = rec.get("notified")
        if prev == "legacy":
            continue
        cur = snapshot(rec)
        if not rec.get("id"):                              # no ID yet: keep trying, alert only after HOLD_HOURS
            seen = parse_iso(rec.get("first_seen")) or now
            if prev is not None or now - seen < timedelta(hours=HOLD_HOURS):
                continue
        if prev is None:
            out.append((key, "new", []))
        elif prev != cur:
            changes = [f"{label}: {prev.get(f) or '-'} → {cur[f] or '-'}"
                       for f, label in TRACKED_FIELDS if prev.get(f) != cur[f]]
            out.append((key, "updated", changes))
    out.sort(key=lambda x: (x[1] != "new", x[0]))
    return out


def format_block(kind: str, rec: dict, changes: list[str]) -> str:
    e = html.escape
    head = "🆕" if kind == "new" else "🔄"
    lines = [f"{head} <b>{e(rec.get('id') or 'ID unavailable')}</b>"
             + (f" · {e(rec['status'])}" if rec.get("status") else "")]
    if rec.get("title"):
        lines.append(e(rec["title"]))
    if kind == "updated":
        lines.extend(f"• {e(c)}" for c in changes)
    else:
        dl = [f"Deadline: {e(rec['deadline'])}" if rec.get("deadline") else "",
              f"Opening: {e(rec['opening'])}" if rec.get("opening") else ""]
        dl = " | ".join(x for x in dl if x)
        if dl:
            lines.append(dl)
    if rec.get("url"):
        lines.append(f'<a href="{e(rec["url"], quote=True)}">Open tender</a>')
    if rec.get("source") == "offlist":
        lines.append("<i>(found outside the main list)</i>")
    return "\n".join(lines)


def build_chunks(state: dict, pending: list) -> list[tuple[str, list[str]]]:
    n_new = sum(1 for _, k, _ in pending if k == "new")
    n_upd = len(pending) - n_new
    header = "🚨 <b>NUPCO tenders</b>: " + ", ".join(
        x for x in (f"{n_new} new" if n_new else "", f"{n_upd} updated" if n_upd else "") if x)
    chunks, cur, keys = [], header, []
    for key, kind, changes in pending:
        block = format_block(kind, state["tenders"][key], changes)
        if keys and len(cur) + len(block) + 2 > TG_CHUNK:
            chunks.append((cur, keys))
            cur, keys = header + " (cont.)", []
        cur += "\n\n" + block
        keys.append(key)
    if keys:
        chunks.append((cur, keys))
    return chunks


def send_alerts(state: dict, pending: list) -> int:
    """Send alerts; mark notified ONLY on confirmed delivery.  Returns #undelivered."""
    failed = 0
    for text, keys in build_chunks(state, pending):
        if tg_send(text):
            for k in keys:
                state["tenders"][k]["notified"] = snapshot(state["tenders"][k])
        else:
            failed += len(keys)
    return failed


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    now = utcnow()
    state, first_run = load_state(now)
    session = make_session()
    health = state["health"]

    list_items: list[dict] = []
    scrape_ok = True
    try:
        list_items = scrape_listing(session)
        log.info("HTML list: %d tender(s)", len(list_items))
        prev_n = state["meta"].get("last_list_count", 0)
        if prev_n >= 8 and len(list_items) < prev_n * 0.5:
            log.warning("List shrank from %d to %d - possible partial page (state is safe).",
                        prev_n, len(list_items))
        state["meta"]["last_list_count"] = len(list_items)
    except Exception as e:  # noqa: BLE001
        scrape_ok = False
        log.error("Listing scrape failed: %s: %s", type(e).__name__, e)
        state["health"]["last_error"] = f"{type(e).__name__}: {e}"[:300]

    extra: list[dict] = []
    if not first_run:
        try:
            extra = discover_extra(session, state, list_items)
        except Exception as e:  # noqa: BLE001
            log.info("Safety-net discovery skipped (%s)", type(e).__name__)

    merge(state, list_items, now, "list")
    merge(state, extra, now, "offlist")
    prune(state, now)

    # ---- health bookkeeping ------------------------------------------------
    if scrape_ok:
        state["meta"]["last_success"] = iso(now)
        if health.get("alerted"):
            tg_send("✅ NUPCO tracker recovered - scraping works again.")
        health["failures"], health["alerted"] = 0, False
    else:
        health["failures"] = health.get("failures", 0) + 1
        if health["failures"] >= FAIL_ALERT_AFTER and not health.get("alerted"):
            if tg_send(f"⚠️ <b>NUPCO tracker is failing</b> ({health['failures']} runs in a row). "
                       "The website may be blocking GitHub, or its layout changed.\n"
                       f"Last error: {html.escape(health.get('last_error', 'unknown'))}"):
                health["alerted"] = True

    # ---- notifications -----------------------------------------------------
    failed = 0
    if first_run and list_items and not NOTIFY_ON_FIRST_RUN:
        for rec in state["tenders"].values():
            rec["notified"] = snapshot(rec)
        if tg_send(f"✅ NUPCO tracker initialised. Baseline: {len(list_items)} tenders on the list. "
                   "You will be notified about new and updated tenders from now on."):
            log.info("Baseline stored.")
    else:
        pending = pending_items(state, now)
        if pending:
            log.info("%d notification(s) pending", len(pending))
            failed = send_alerts(state, pending)
            log.info("Delivered %d, undelivered %d (will retry next run)",
                     len(pending) - failed, failed)
        else:
            log.info("No new or updated tenders.")
            if NOTIFY_NO_CHANGE and scrape_ok:
                ksa = (now + timedelta(hours=3)).strftime("%d/%m/%Y %H:%M")
                tg_send(f"✅ NUPCO check done ({ksa} KSA): no new or updated tenders. "
                        f"{len(list_items)} tenders on the list.")

    save_state(state, now)

    if failed:
        return 1
    if not scrape_ok and health["failures"] >= FAIL_ALERT_AFTER:
        return 1  # turns the Actions run red -> GitHub emails you
    return 0


if __name__ == "__main__":
    sys.exit(main())
