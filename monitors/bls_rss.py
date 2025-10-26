#!/usr/bin/env python3
"""
BLS RSS Feed Monitor
Fetches and monitors BLS RSS feeds for new releases.
Auto-scrapes CPI/PPI schedules, infers NFP (first Friday),
includes full cleaned release text, and can optionally run a
full test bundle on startup via BLS_FORCE_STARTUP_RUN=true.
"""

import os
import re
import time
import json
import random
import pathlib
import datetime as dt
from typing import Dict, List, Tuple, Optional
from html.parser import HTMLParser

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

from .base import Monitor as BaseMonitor
from core.bus import Event

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
FEEDS = {
    "CPI": "https://www.bls.gov/feed/cpi_latest.rss",
    "PPI": "https://www.bls.gov/feed/ppi_latest.rss",
    "NFP": "https://www.bls.gov/feed/ces_latest.rss",
}

NORMAL_INTERVAL = 6 * 3600      # 6h
FAST_WINDOW = (-600, 900)       # 10m before → 15m after release
FAST_INTERVAL = (15, 25)        # 15–25s around release
COOLDOWN_INTERVAL = 3600        # 1h after detection
SCHEDULE_REFRESH_SEC = 6 * 3600 # rescrape schedule every 6h

LOG_ROOT = pathlib.Path("./log/rssfeeds").resolve()

STATE_LAST_GUID = "bls_rss:last_guid:{}"
STATE_STASH = "bls_rss:stash"
STATE_LAST_BUNDLE = "bls_rss:last_bundle"

# --------------------------------------------------------------------
# HTML parsing helpers
# --------------------------------------------------------------------
class _BLSDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current_label: Optional[str] = None
        self.data_points: List[Tuple[str, str]] = []
        self.in_data_span = False
        self.current_value = ""

    def handle_starttag(self, tag, attrs):
        if tag == "span":
            attrs_dict = dict(attrs)
            if attrs_dict.get("class") == "data":
                self.in_data_span = True
                self.current_value = ""

    def handle_endtag(self, tag):
        if tag == "span" and self.in_data_span:
            self.in_data_span = False
            if self.current_label and self.current_value:
                self.data_points.append((self.current_label, self.current_value.strip()))

    def handle_data(self, data):
        if self.in_data_span:
            self.current_value += data
        else:
            text = (data or "").strip()
            if text and ":" in text:
                self.current_label = text.rstrip(":")


def _parse_bls_html(html: str) -> List[Tuple[str, str]]:
    p = _BLSDataParser()
    p.feed(html or "")
    return p.data_points


def _fetch_full_release(link: str) -> str:
    """Fetch full BLS release page and strip tags."""
    if not link:
        return ""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
        r = requests.get(link, headers=headers, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return re.sub(r"\s+", " ", text)
    except Exception as e:
        print(f"[bls_rss] failed to fetch full report {link}: {e}", flush=True)
        return ""



# --------------------------------------------------------------------
# Schedule utilities
# --------------------------------------------------------------------
def _parse_release_table(html: str) -> List[Tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=re.compile(r"release-list"))
    rows = []
    if not table:
        return rows
    for tr in table.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) >= 3:
            rows.append((tds[0], tds[1], tds[2]))
    return rows


def _to_dt(date_str: str, time_str: str, tz: Optional[ZoneInfo]) -> Optional[dt.datetime]:
    dnorm = date_str.replace(".", "")
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            d = dt.datetime.strptime(f"{dnorm} {time_str}", fmt)
            return d.replace(tzinfo=tz)
        except Exception:
            continue
    return None


def _next_from_schedule_page(url: str, tz: Optional[ZoneInfo]) -> Optional[str]:
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    rows = _parse_release_table(r.text)
    now = dt.datetime.now(tz) if tz else dt.datetime.now()
    cands = []
    for _, date_str, time_str in rows:
        if "08:30" not in time_str:
            continue
        d = _to_dt(date_str, time_str, tz)
        if d and d > now:
            cands.append(d)
    if not cands:
        return None
    nxt = min(cands)
    return nxt.isoformat()


def _first_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    delta = (4 - d.weekday()) % 7
    return d + dt.timedelta(days=delta)


def _next_nfp_first_friday(tz: Optional[ZoneInfo]) -> Optional[str]:
    now = dt.datetime.now(tz) if tz else dt.datetime.now()
    y, m = now.year, now.month
    for i in range(0, 14):
        mm = ((m - 1 + i) % 12) + 1
        yy = y + ((m - 1 + i) // 12)
        ff = _first_friday(yy, mm)
        dt_ff = dt.datetime(yy, mm, ff.day, 8, 30, tzinfo=tz)
        if dt_ff > now:
            return dt_ff.isoformat()
    return None


def _expected_release_map() -> Dict[str, str]:
    tz = ZoneInfo("America/New_York") if ZoneInfo else None
    out: Dict[str, str] = {}
    try:
        out["CPI"] = _next_from_schedule_page(
            "https://www.bls.gov/schedule/news_release/cpi.htm", tz
        )
    except Exception:
        pass
    try:
        out["PPI"] = _next_from_schedule_page(
            "https://www.bls.gov/schedule/news_release/ppi.htm", tz
        )
    except Exception:
        pass
    try:
        out["NFP"] = _next_nfp_first_friday(tz)
    except Exception:
        pass
    return {k: v for k, v in out.items() if v}


# --------------------------------------------------------------------
# Persistence helpers
# --------------------------------------------------------------------
def _ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)


def _save_payload(feed: str, guid: str, payload: dict) -> pathlib.Path:
    """Save payload JSON safely under log/rssfeeds/<feed>/."""
    _ensure_dir(LOG_ROOT / feed)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Sanitize guid for filesystem
    safe_guid = re.sub(r"[^A-Za-z0-9_-]", "_", guid or "unknown")
    fname = f"{ts}-{safe_guid[:40]}.json"
    path = LOG_ROOT / feed / fname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path



def _compose_prompt(bundle: Dict[str, dict]) -> str:
    lines = ["The following BLS reports just released:\n"]
    for k in ("CPI", "PPI", "NFP"):
        if k not in bundle:
            continue
        item = bundle[k]
        lines.append(f"{k}: {item.get('title', '')}")
        for label, val in item.get("data", []):
            lines.append(f"- {label}: {val}")
        if item.get("link"):
            lines.append(f"Source: {item['link']}")
        if item.get("full_text"):
            lines.append("\n" + item["full_text"])
        lines.append("")
    lines.append("Would this data deserve a trade signal on treasuries (e.g TLT)?")
    lines.append(
        "\nReturn concise analysis and explicit trade instructions if warranted. "
        "priority=2 if high-confidence actionable, 1 if moderate, 0 if none."
    )
    return "\n".join(lines).strip()


# --------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------
class Monitor(BaseMonitor):
    name = "bls_rss"

    def __init__(self, publish, config, ctx):
        super().__init__(publish, config, ctx)
        self.state = ctx.get("state")
        self._expected = _expected_release_map()
        self._last_schedule_refresh = 0.0
        self.cooldown = COOLDOWN_INTERVAL
        self.force_startup_run = os.getenv("BLS_FORCE_STARTUP_RUN", "false").lower() in ("1", "true", "yes")

    # ----------------------------------------------------------------
    # Core fetch & publish
    # ----------------------------------------------------------------
    def _fetch_latest(self, name: str, url: str):
        """Return the latest RSS entry as (guid, payload)."""
        try:
            feed = feedparser.parse(
                url,
                request_headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/122.0.0.0 Safari/537.36",
                    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Connection": "keep-alive",
                },
            )

        except Exception as e:
            print(f"[bls_rss] feedparser error {name}: {e}")
            return None, None

        if not getattr(feed, "entries", None):
            print(f"[bls_rss] no entries for {name} — feed may be blocked or empty")
            return None, None

        e = feed.entries[0]
        guid = e.get("id") or e.get("guid") or e.get("link")
        desc = e.get("description") or e.get("summary") or ""
        data_points = _parse_bls_html(desc)
        fulltext = _fetch_full_release(e.get("link", ""))
        payload = {
            "title": e.get("title"),
            "link": e.get("link"),
            "published": e.get("published"),
            "guid": guid,
            "data": data_points,
            "full_text": fulltext,
        }
        return guid, payload


    def _same_day_expected(self) -> bool:
        dates = []
        for iso in self._expected.values():
            try:
                dates.append(dt.datetime.fromisoformat(iso).date())
            except Exception:
                pass
        return len(set(dates)) == 1 if dates else False

    def _try_bundle_and_publish(self):
        """Send bundle if all three reports are same-day, else analyze each individually."""
        # --- Load state safely (some stores serialize JSON) ---
        raw_stash = self.state.get(STATE_STASH, default={}) or {}
        if isinstance(raw_stash, str):
            try:
                stash = json.loads(raw_stash)
            except Exception:
                stash = {}
                print("[bls_rss] state stash was string but not valid JSON; cleared")
        else:
            stash = raw_stash

        if not stash:
            print("[bls_rss] _try_bundle_and_publish | empty stash")
            return False

        print(f"[bls_rss] _try_bundle_and_publish | stash keys={list(stash.keys())}")

        have_all = all(k in stash for k in ("CPI", "PPI", "NFP"))

        # ---- Case 1: all three exist AND same-day ----
        if have_all and self._same_day_expected():
            bundle_id = tuple(stash[k]["guid"] for k in ("CPI", "PPI", "NFP"))
            last_bundle = self.state.get(STATE_LAST_BUNDLE, default=None)
            if last_bundle == list(bundle_id):
                print("[bls_rss] bundle already processed; skipping")
                return False

            bundle = {k: stash[k]["payload"] for k in ("CPI", "PPI", "NFP")}
            prompt = _compose_prompt(bundle)
            evt = Event(
                source=self.name,
                title="BLS bundle — analysis",
                message="Analyzing latest BLS releases...",
                created_at=dt.datetime.utcnow().isoformat() + "Z",
                priority=0,
                payload={
                    "analyze": True,
                    "text": prompt,
                    "taco_mode": False,
                    "pre_screened": True,
                },
            )
            self.publish(evt)
            self.state.set(list(bundle_id), STATE_LAST_BUNDLE)
            print("[bls_rss] bundle published")
            return True

        # ---- Case 2: analyze each individually ----
        for feed_name, data in stash.items():
            guid = data.get("guid")
            payload = data.get("payload")
            if not guid or not payload:
                continue

            analyzed_key = f"bls_rss:analyzed:{feed_name}:{guid}"
            if self.state.get(analyzed_key, default=False):
                print(f"[bls_rss] {feed_name} already analyzed; skipping")
                continue

            prompt = _compose_prompt({feed_name: payload})
            evt = Event(
                source=self.name,
                title=f"BLS {feed_name} — analysis",
                message=f"Analyzing {feed_name} release...",
                created_at=dt.datetime.utcnow().isoformat() + "Z",
                priority=0,
                payload={
                    "analyze": True,
                    "text": prompt,
                    "taco_mode": False,
                    "pre_screened": True,
                },
            )
            self.publish(evt)
            self.state.set(True, analyzed_key)
            print(f"[bls_rss] published individual analysis for {feed_name}")

        return True



    # ----------------------------------------------------------------
    # Main loop
    # ----------------------------------------------------------------
    def run(self):
        print(f"[bls_rss] start | log_dir={LOG_ROOT}")
        for k in FEEDS:
            _ensure_dir(LOG_ROOT / k)

        # optional startup forced test
        if self.force_startup_run:
            print("[bls_rss] force startup run enabled — fetching all feeds now")
            stash = {}
            for name, url in FEEDS.items():
                guid, payload = self._fetch_latest(name, url)
                if not guid or not payload:
                    continue
                _save_payload(name, guid, payload)
                stash[name] = {
                    "guid": guid,
                    "payload": payload,
                    "ts": dt.datetime.utcnow().isoformat(),
                }
                print(f"[bls_rss] startup fetched {name} ({guid})")

            if stash:
                # write state first
                self.state.set(stash, STATE_STASH)
                # force analysis of all fetched reports immediately
                print("[bls_rss] running startup analysis for fetched reports")
                self._try_bundle_and_publish()
            else:
                print("[bls_rss] startup fetch returned no entries — sending dry-run event")
                evt = Event(
                    source=self.name,
                    title="BLS dry-run test",
                    message="Dry-run startup test for BLS RSS monitor.",
                    created_at=dt.datetime.utcnow().isoformat() + "Z",
                    priority=0,
                    payload={
                        "analyze": True,
                        "text": "BLS monitor startup dry-run — no live data fetched.",
                        "taco_mode": False,
                        "pre_screened": True,
                    },
                )
                self.publish(evt)

            print("[bls_rss] startup test complete\n")


        # normal loop
        while True:
            triggered = False
            for name, url in FEEDS.items():
                try:
                    guid, payload = self._fetch_latest(name, url)
                except Exception as e:
                    print(f"[bls_rss] fetch error {name}: {e}")
                    continue
                if not guid or not payload:
                    continue
                last_guid_key = STATE_LAST_GUID.format(name)
                last_guid = self.state.get(last_guid_key, default=None)
                if last_guid == guid:
                    continue
                self.state.set(guid, last_guid_key)
                triggered = True
                path = _save_payload(name, guid, payload)
                print(f"[bls_rss] new {name} → {guid} | saved {path}")
                stash = self.state.get(STATE_STASH, default={}) or {}
                stash[name] = {"guid": guid, "payload": payload, "ts": dt.datetime.utcnow().isoformat()}
                self.state.set(stash, STATE_STASH)

            # refresh schedule periodically
            now_ts = time.time()
            if now_ts - self._last_schedule_refresh > SCHEDULE_REFRESH_SEC:
                try:
                    self._expected = _expected_release_map()
                    self._last_schedule_refresh = now_ts
                    print("[bls_rss] schedule refreshed")
                except Exception as e:
                    print(f"[bls_rss] schedule refresh failed: {e}")

            if triggered:
                bundled = self._try_bundle_and_publish()
                if bundled:
                    print("[bls_rss] bundle published")
                time.sleep(self.cooldown)
                continue

            # adaptive polling
            intervals = []
            for name in FEEDS:
                iso = self._expected.get(name)
                if not iso:
                    intervals.append(NORMAL_INTERVAL)
                    continue
                try:
                    rel = dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc)
                    delta = (rel - dt.datetime.now(dt.timezone.utc)).total_seconds()
                    if FAST_WINDOW[0] < delta < FAST_WINDOW[1]:
                        intervals.append(random.uniform(*FAST_INTERVAL))
                    else:
                        intervals.append(NORMAL_INTERVAL)
                except Exception:
                    intervals.append(NORMAL_INTERVAL)

            time.sleep(min(intervals) + random.uniform(0, 5))
