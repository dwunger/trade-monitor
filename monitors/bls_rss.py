#!/usr/bin/env python3
"""
BLS RSS + CES (NFP) Monitor

Tracks CPI, PPI, and NFP (CES) for new economic releases.

Features
--------
✓ CPI / PPI via RSS with full text extraction
✓ NFP via CES0000000001 (Total Nonfarm, seasonally adjusted)
✓ Adaptive time-to-release polling (seconds-fast around 8:30 ET)
✓ Cross-context bundling for macro analysis
✓ User-Agent spoofing for reliability
✓ Priority-1 pre-release reminders (24h before)
✓ Priority-1 delay alerts (10 min late)
✓ Logs next scheduled release dates on startup
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
    from zoneinfo import ZoneInfo
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
    "NFP": None,  # handled via CES text file
}

CES_TOTAL_URL = "https://download.bls.gov/pub/time.series/ce/ce.data.00a.TotalNonfarm.Employment"
CES_SERIES_ID = "CES0000000001"  # Total nonfarm, seasonally adjusted

LOG_ROOT = pathlib.Path("./log/rssfeeds").resolve()

STATE_LAST_GUID = "bls_rss:last_guid:{}"
STATE_STASH = "bls_rss:stash"
STATE_LAST_BUNDLE = "bls_rss:last_bundle"
STATE_LAST_REMINDER = "bls_rss:last_reminder:{}"
STATE_LAST_DELAY_ALERT = "bls_rss:last_delay_alert:{}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

SCHEDULE_REFRESH_SEC = 6 * 3600
COOLDOWN_INTERVAL = 3600

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
class _BLSDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current_label = None
        self.data_points: List[Tuple[str, str]] = []
        self.in_data_span = False
        self.current_value = ""

    def handle_starttag(self, tag, attrs):
        if tag == "span" and dict(attrs).get("class") == "data":
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
    if not link:
        return ""
    try:
        r = requests.get(link, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))
    except Exception as e:
        print(f"[bls_rss] failed to fetch full report {link}: {e}")
        return ""


def _ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)


def _save_payload(feed: str, guid: str, payload: dict) -> pathlib.Path:
    _ensure_dir(LOG_ROOT / feed)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_guid = re.sub(r"[^A-Za-z0-9_-]", "_", guid or "unknown")
    path = LOG_ROOT / feed / f"{ts}-{safe_guid[:40]}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _load_recent_logs(exclude: str) -> Dict[str, dict]:
    bundle = {}
    for feed in FEEDS:
        if feed == exclude:
            continue
        feed_dir = LOG_ROOT / feed
        if not feed_dir.exists():
            continue
        files = sorted(feed_dir.glob("*.json"), reverse=True)
        if not files:
            continue
        try:
            with open(files[0], "r", encoding="utf-8") as fh:
                bundle[feed] = json.load(fh)
        except Exception as e:
            print(f"[bls_rss] failed to read {files[0]}: {e}")
    return bundle


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
# CES (NFP)
# --------------------------------------------------------------------
def _fetch_latest_nfp() -> tuple[Optional[str], Optional[dict]]:
    """
    Fetch CES0000000001 — total nonfarm, seasonally adjusted.
    Adds momentum, trend strength, and policy signal context.
    """
    try:
        r = requests.get(CES_TOTAL_URL, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()

        # Filter only CES0000000001 lines
        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip().startswith(CES_SERIES_ID)]
        if not lines:
            print("[bls_rss] no CES0000000001 lines found in file")
            return None, None

        # Last 12 months
        tail = lines[-12:]
        records: List[Tuple[str, int]] = []
        for ln in tail:
            parts = re.split(r"\s+", ln)
            if len(parts) < 4:
                continue
            _, year, period, value = parts[:4]
            if not period.startswith("M"):
                continue
            month = int(period[1:])
            try:
                val = int(float(value))
            except Exception:
                continue
            records.append((f"{year}-{month:02d}", val))
        if len(records) < 6:
            return None, None

        # --- Derived metrics ---
        vals = [v for _, v in records]
        months = [d for d, _ in records]
        latest_ym, latest_val = months[-1], vals[-1]
        prev_val = vals[-2]
        mom_change = latest_val - prev_val

        # 3- and 6-month averages of monthly changes
        deltas = [b - a for a, b in zip(vals[:-1], vals[1:])]
        avg3 = sum(deltas[-3:]) / 3
        avg6 = sum(deltas[-6:]) / 6
        momentum = avg3 - avg6

        # Trend classification
        if avg3 > 250 and momentum > 0:
            trend = "STRONG"
        elif avg3 > 150:
            trend = "MODERATE"
        elif avg3 > 50:
            trend = "SLOWING"
        else:
            trend = "WEAK"

        # Fed policy interpretation
        if trend in ("STRONG", "MODERATE") and momentum > 0:
            fed_signal = "Labor market still hot → cuts less likely near term."
        elif trend in ("SLOWING", "WEAK") and momentum < 0:
            fed_signal = "Hiring momentum cooling → supports dovish bias."
        else:
            fed_signal = "Automated evaluation not available."

        guid = f"NFP-{latest_ym.replace('-', '')}"

        summary_text = (
            f"Nonfarm payrolls trend ({latest_ym}): {trend}\n"
            f"3-mo avg change: {avg3:,.0f}k | 6-mo avg: {avg6:,.0f}k | Momentum: {momentum:+.0f}k\n"
            f"Latest change: {mom_change:+,}k (prior {prev_val:,} → {latest_val:,})\n"
            f"Fed interpretation: {fed_signal}"
        )

        # Package last 6 months for LLM visibility
        last6 = records[-6:]
        data_fmt = [f"{m}: {v:,}" for m, v in last6]

        payload = {
            "title": "Nonfarm Payroll Employment — Trend Analysis (CES0000000001, SA)",
            "link": CES_TOTAL_URL,
            "published": dt.datetime.utcnow().isoformat() + "Z",
            "guid": guid,
            "data": data_fmt,
            "full_text": summary_text,
        }

        return guid, payload

    except Exception as e:
        print(f"[bls_rss] failed to fetch CES NFP: {e}")
        return None, None


# --------------------------------------------------------------------
# Schedule utilities
# --------------------------------------------------------------------
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


def _next_from_schedule_page(url: str, tz: Optional[ZoneInfo]) -> Optional[str]:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    table = soup.find("table", class_=re.compile(r"release-list"))
    if not table:
        tables = soup.find_all("table")
        table = tables[0] if tables else None
    if not table:
        raise RuntimeError("schedule table not found")

    now = dt.datetime.now(tz) if tz else dt.datetime.now()
    cands: list[dt.datetime] = []

    for tr in table.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 3:
            continue

        date_str, time_str = tds[1], tds[2]
        # normalize abbreviations like "Nov." → "Nov"
        date_str = re.sub(r"\.", "", date_str.strip())
        time_str = time_str.strip().upper().replace(".", "")
        if "8:30" not in time_str:
            continue

        # Try several formats (handles "Nov 13, 2025", "November 13, 2025")
        parsed = None
        for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
            try:
                parsed = dt.datetime.strptime(f"{date_str} {time_str}", fmt).replace(tzinfo=tz)
                break
            except Exception:
                continue

        if not parsed:
            continue
        if parsed > now:
            cands.append(parsed)

    if not cands:
        print(f"[bls_rss] schedule parse produced no future rows for {url}")
        return None

    return min(cands).isoformat()



# --------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------
class Monitor(BaseMonitor):
    name = "bls_rss"

    def __init__(self, publish, config, ctx):
        super().__init__(publish, config, ctx)
        self.state = ctx.get("state")
        self._expected = self._expected_release_map()
        self._last_schedule_refresh = 0.0
        self.force_startup_run = os.getenv("BLS_FORCE_STARTUP_RUN", "false").lower() in ("1", "true", "yes")

    def _expected_release_map(self) -> Dict[str, str]:
        tz = ZoneInfo("America/New_York") if ZoneInfo else None
        out = {}
        for name, url in [
            ("CPI", "https://www.bls.gov/schedule/news_release/cpi.htm"),
            ("PPI", "https://www.bls.gov/schedule/news_release/ppi.htm"),
        ]:
            try:
                out[name] = _next_from_schedule_page(url, tz)
            except Exception as e:
                print(f"[bls_rss] schedule fetch failed for {name}: {e}")
        try:
            out["NFP"] = _next_nfp_first_friday(tz)
        except Exception as e:
            print(f"[bls_rss] NFP schedule calc failed: {e}")
        return {k: v for k, v in out.items() if v}


    def _log_expected_schedule(self):
        if not self._expected:
            print("[bls_rss] no upcoming releases detected")
            return
        print("[bls_rss] upcoming releases:")
        for name, iso in self._expected.items():
            try:
                rel = dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc)
                print(f"  - {name}: {rel.strftime('%Y-%m-%d %H:%M:%S')}Z ({rel.strftime('%a')})")
            except Exception as e:
                print(f"  - {name}: invalid date ({e})")

    # Adaptive polling
    def _compute_poll_interval(self, delta: float) -> float:
        if delta > 172800: return 86400
        elif delta > 25200: return 21600
        elif delta > 3600: return 3600
        elif delta > 1800: return 600
        elif delta > 600: return 240
        elif delta > 60: return random.uniform(15, 45)
        elif delta > -900: return random.uniform(15, 25)
        elif delta > -3600: return random.uniform(120, 180)
        else: return 3600

    @staticmethod
    def _fmt_delta(delta: float) -> str:
        sign = "-" if delta < 0 else ""
        s = abs(int(delta))
        h, r = divmod(s, 3600)
        m, r = divmod(r, 60)
        return f"{sign}{h}h{m}m{r}s"

    # Pre-release reminders
    def _check_pre_release_reminders(self):
        now = dt.datetime.now(dt.timezone.utc)
        for name, iso in (self._expected or {}).items():
            try:
                rel = dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc)
            except Exception:
                continue
            hrs = (rel - now).total_seconds() / 3600
            if 23 <= hrs <= 25:
                key = STATE_LAST_REMINDER.format(name)
                if self.state.get(key, default=None) == rel.isoformat():
                    continue
                evt = Event(
                    source=self.name,
                    title=f"Reminder: {name} release tomorrow",
                    message=f"{name} expected {rel.strftime('%H:%M ET')} tomorrow. Review positions beforehand.",
                    created_at=now.isoformat(),
                    priority=1,
                )
                self.publish(evt)
                self.state.set(rel.isoformat(), key)
                print(f"[bls_rss] reminder dispatched for {name}")

    # Delay alerts
    def _check_release_delays(self):
        now = dt.datetime.now(dt.timezone.utc)
        for name, iso in (self._expected or {}).items():
            try:
                rel = dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc)
            except Exception:
                continue
            delay = (now - rel).total_seconds()
            if 600 < delay < 3600:
                key = STATE_LAST_DELAY_ALERT.format(name)
                if self.state.get(key, default=None) == rel.isoformat():
                    continue
                evt = Event(
                    source=self.name,
                    title=f"{name} release delayed",
                    message=f"No {name} update yet (expected {rel.strftime('%H:%M ET')}).",
                    created_at=now.isoformat(),
                    priority=1,
                )
                self.publish(evt)
                self.state.set(rel.isoformat(), key)
                print(f"[bls_rss] delay alert dispatched for {name}")

    # Fetch helpers
    def _fetch_latest_rss(self, name: str, url: str):
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": UA})
        except Exception as e:
            print(f"[bls_rss] feedparser error {name}: {e}")
            return None, None
        if not getattr(feed, "entries", None):
            return None, None
        e = feed.entries[0]
        guid = e.get("id") or e.get("guid") or e.get("link")
        desc = e.get("description") or e.get("summary") or ""
        data_points = _parse_bls_html(desc)
        fulltext = _fetch_full_release(e.get("link", ""))
        return guid, {
            "title": e.get("title"),
            "link": e.get("link"),
            "published": e.get("published"),
            "guid": guid,
            "data": data_points,
            "full_text": fulltext,
        }

    def _same_day_expected(self) -> bool:
        dates = []
        for iso in self._expected.values():
            try: dates.append(dt.datetime.fromisoformat(iso).date())
            except Exception: pass
        return len(set(dates)) == 1 if dates else False

    def _try_bundle_and_publish(self):
        stash = self.state.get(STATE_STASH, default={}) or {}
        if isinstance(stash, str):
            try: stash = json.loads(stash)
            except Exception: stash = {}
        if not stash: return False
        have_all = all(k in stash for k in ("CPI", "PPI", "NFP"))
        if have_all and self._same_day_expected():
            bundle_id = tuple(stash[k]["guid"] for k in ("CPI", "PPI", "NFP"))
            if self.state.get(STATE_LAST_BUNDLE, default=None) == list(bundle_id):
                return False
            bundle = {k: stash[k]["payload"] for k in ("CPI", "PPI", "NFP")}
            evt = Event(
                source=self.name,
                title="BLS bundle — analysis",
                message="Analyzing CPI, PPI, and NFP...",
                created_at=dt.datetime.utcnow().isoformat() + "Z",
                priority=0,
                payload={"analyze": True, "text": _compose_prompt(bundle),
                         "taco_mode": False, "pre_screened": True},
            )
            self.publish(evt)
            self.state.set(list(bundle_id), STATE_LAST_BUNDLE)
            print("[bls_rss] published bundle")
            return True
        for feed_name, data in stash.items():
            guid, payload = data.get("guid"), data.get("payload")
            if not guid or not payload: continue
            analyzed_key = f"bls_rss:analyzed:{feed_name}:{guid}"
            if self.state.get(analyzed_key, default=False): continue
            bundle = {feed_name: payload, **_load_recent_logs(feed_name)}
            evt = Event(
                source=self.name,
                title=f"BLS {feed_name} — analysis",
                message=f"Analyzing {feed_name} release...",
                created_at=dt.datetime.utcnow().isoformat() + "Z",
                priority=0,
                payload={"analyze": True, "text": _compose_prompt(bundle),
                         "taco_mode": False, "pre_screened": True},
            )
            self.publish(evt)
            self.state.set(True, analyzed_key)
            print(f"[bls_rss] published {feed_name} analysis")
        return True

    def run(self):
        print(f"[bls_rss] start | log_dir={LOG_ROOT}")
        self._log_expected_schedule()  # <-- NEW: log CPI/PPI/NFP next release times
        for k in FEEDS: _ensure_dir(LOG_ROOT / k)
        if self.force_startup_run:
            stash = {}
            for name, url in FEEDS.items():
                guid, payload = _fetch_latest_nfp() if name=="NFP" else self._fetch_latest_rss(name, url)
                if guid and payload:
                    _save_payload(name, guid, payload)
                    stash[name]={"guid":guid,"payload":payload,"ts":dt.datetime.utcnow().isoformat()}
            if stash:
                self.state.set(stash, STATE_STASH)
                self._try_bundle_and_publish()
        while True:
            triggered=False
            now_ts=time.time()
            if now_ts-self._last_schedule_refresh>SCHEDULE_REFRESH_SEC:
                try:
                    self._expected=self._expected_release_map()
                    self._last_schedule_refresh=now_ts
                    print("[bls_rss] schedule refreshed")
                except Exception as e:
                    print(f"[bls_rss] schedule refresh failed: {e}")
            for name,url in FEEDS.items():
                try:
                    guid,payload=_fetch_latest_nfp() if name=="NFP" else self._fetch_latest_rss(name,url)
                except Exception as e:
                    print(f"[bls_rss] fetch error {name}: {e}")
                    continue
                if not guid or not payload: continue
                last_guid_key=STATE_LAST_GUID.format(name)
                if self.state.get(last_guid_key,default=None)==guid: continue
                self.state.set(guid,last_guid_key)
                triggered=True
                path=_save_payload(name,guid,payload)
                print(f"[bls_rss] NEW {name} → {guid} | saved {path}")
                stash=self.state.get(STATE_STASH,default={}) or {}
                stash[name]={"guid":guid,"payload":payload,"ts":dt.datetime.utcnow().isoformat()}
                self.state.set(stash,STATE_STASH)
            if triggered:
                self._try_bundle_and_publish()
                time.sleep(COOLDOWN_INTERVAL)
                continue
            # pre-release reminders & delay alerts
            self._check_pre_release_reminders()
            self._check_release_delays()
            # adaptive polling
            intervals=[]
            for name in FEEDS:
                iso=self._expected.get(name)
                if not iso:
                    intervals.append(3600)
                    continue
                try:
                    rel=dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc)
                    delta=(rel-dt.datetime.now(dt.timezone.utc)).total_seconds()
                    iv=self._compute_poll_interval(delta)
                    intervals.append(iv)
                    print(f"[bls_rss] poll {name}: delta={self._fmt_delta(delta)} → interval={int(iv)}s")
                except Exception as e:
                    intervals.append(3600)
                    print(f"[bls_rss] poll {name}: failed {e}")
            sleep=min(intervals)+random.uniform(0,5)
            print(f"[bls_rss] sleeping {sleep:.1f}s\n")
            time.sleep(sleep)
