"""
BMS Ticket Checker — Telegram + Email notifier for Avengers: Doomsday.
Cloud mode for GitHub Actions. Runs once, checks configured movie, alerts on changes.
State is persisted via commit-back to the repo (no artifact expiry).

Configure via environment variables:
  BMS_URL              - BookMyShow buytickets URL (required)
  BMS_DATES            - comma-separated YYYYMMDD (optional, auto-detect if empty)
  BMS_THEATRE          - substring filter, comma-separated (optional)
  BMS_TIME             - morning,afternoon,evening,night (optional)
  TELEGRAM_BOT_TOKEN   - from @BotFather (required for Telegram)
  TELEGRAM_CHAT_ID     - your chat id (required for Telegram)
  RESEND_API_KEY       - optional, for email
  RESEND_FROM_EMAIL    - optional
  RESEND_TO_EMAIL      - optional
"""
import os
import re
import sys
import json
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    "url": os.getenv(
        "BMS_URL",
        "https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/buytickets/ET00526306",
    ),
    "dates": os.getenv("BMS_DATES", ""),
    "theatre": os.getenv("BMS_THEATRE", ""),
    "time_period": os.getenv("BMS_TIME", ""),
    "format": os.getenv("BMS_FORMAT", ""),  # e.g. "DOLBY CINEMA,IMAX"
    # Per-theatre format rules: "theatre_substr:format_substr,theatre_substr:format_substr"
    # e.g. "Prasads:PCX,AMB:*,Allu:*"
    # A show passes if it matches ANY group. "*" means any format.
    # When set, takes precedence over BMS_THEATRE + BMS_FORMAT.
    "filter_groups": os.getenv("BMS_FILTER_GROUPS", ""),
    "movie_label": os.getenv("BMS_MOVIE_LABEL", ""),  # optional display label
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")

# Allow per-movie state files (needed for matrix workflow).
STATE_FILE = os.getenv("BMS_STATE_FILE", "bms_state.json")

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",     "🔴"),
    "1": ("ALMOST FULL",  "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE",    "🟢"),
}
DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}
TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}
REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}

# ──────────────────────────────────────────────────────────────────────
# DATA
# ──────────────────────────────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list = field(default_factory=list)

@dataclass
class DateInfo:
    date_code: str
    status: str

# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result

def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")

def booking_deep_link(url):
    """Strip query string and any date path segment so the link lands
    on the movie's main booking page (where dates auto-load)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.split('/buytickets')[0] + '/buytickets/' + parse_bms_url(url)['event_code']}"

# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

# A pool of recent Chrome User-Agents. Rotating helps avoid trivial
# fingerprint blocks. All are realistic desktop Chrome on common OSes.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
]

def _pick_user_agent():
    """Pick a deterministic-ish UA per run (based on minute of day)."""
    idx = datetime.now().minute % len(USER_AGENTS)
    return USER_AGENTS[idx]

def fetch_bms(event_code, date_code, region_code, region_slug,
              lat, lon, geohash):
    """Two-step fetch:
    1. GET the movie page HTML to warm up session cookies (BMS sets
       several anti-bot cookies on first page load).
    2. Call the showtimes API with the same session + cookies.
    Also sends more complete browser headers (sec-fetch-*, etc.).
    """
    ua = _pick_user_agent()
    page_url = (
        f"https://in.bookmyshow.com/movies/"
        f"{region_slug}/buytickets/{event_code}/"
    )

    session = requests.Session()

    # Step 1: warm-up GET to acquire cookies
    warmup_headers = {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    try:
        warmup = session.get(page_url, headers=warmup_headers, timeout=15)
        print(f"  warmup: HTTP {warmup.status_code} "
              f"({len(session.cookies)} cookies)")
    except requests.RequestException as e:
        print(f"  warmup failed: {e}")
        # Continue anyway — sometimes API works without warmup

    # Step 2: API call with warmed session
    api_headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": page_url,
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
        "origin": "https://in.bookmyshow.com",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    try:
        resp = session.get(API_URL, headers=api_headers,
                           params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        body_preview = resp.text[:200] if resp.text else "(empty)"
        print(f"  HTTP {resp.status_code}: {body_preview}")
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
    return None

# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info

def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates

def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")
                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not date_code and re.match(
                        r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]
                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "")
                                     or sa.get("attributes", "")),
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=ca,
                        ))
                    shows.append(show)
    return shows

# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def parse_filter_groups(spec):
    """Parse 'theatre:format,theatre:format,...' into a list of
    (theatre_substr, format_substr) tuples. Returns empty list if spec
    is empty or invalid. '*' as format means any format.
    """
    if not spec or not spec.strip():
        return []
    groups = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            t, f = part.split(":", 1)
            t = t.strip()
            f = f.strip()
            if t:
                groups.append((t.lower(), f.lower() if f else "*"))
        else:
            # No colon — treat as theatre-only filter, any format
            groups.append((part.lower(), "*"))
    return groups

def show_matches_group(show, theatre_substr, format_substr):
    """Returns True if show's venue_name contains theatre_substr AND
    (format_substr is '*' OR show's screen_attr/categories contain it)."""
    if theatre_substr not in show.venue_name.lower():
        return False
    if format_substr == "*":
        return True
    haystack_parts = [show.screen_attr.lower()]
    for c in show.categories:
        haystack_parts.append(c.name.lower())
    haystack = " | ".join(haystack_parts)
    return format_substr in haystack

def filter_shows(shows, theatre_filter, time_periods, date_codes, format_filter="", filter_groups=None):
    """Apply filters. If filter_groups is non-empty, it takes precedence
    over theatre_filter + format_filter (per-theatre format rules).
    Time and date filters are always applied regardless.
    """
    result = []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",")
                    if d.strip()) if date_codes else set()
    groups = filter_groups or []
    # Legacy single-filter fallback (only used when no filter_groups)
    kws = [k.strip().lower() for k in theatre_filter.split(",")
           if k.strip()] if theatre_filter else []
    fmts = [f.strip().lower() for f in format_filter.split(",")
            if f.strip()] if format_filter else []

    for s in shows:
        # Date filter (always applies)
        if dates_set and s.date_code and s.date_code not in dates_set:
            continue
        # Time period filter (always applies)
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue
        # Theatre + format filter — two modes
        if groups:
            # Per-theatre format rules: must match ANY group
            if not any(show_matches_group(s, t, f) for t, f in groups):
                continue
        else:
            # Legacy AND-logic: theatre substring OR (if no theatre set, any), then format substring
            if kws:
                name_lower = s.venue_name.lower()
                if not any(k in name_lower for k in kws):
                    continue
            if fmts:
                haystack_parts = [s.screen_attr.lower()]
                for c in s.categories:
                    haystack_parts.append(c.name.lower())
                haystack = " | ".join(haystack_parts)
                if not any(f in haystack for f in fmts):
                    continue
        result.append(s)
    return result

# ──────────────────────────────────────────────────────────────────────
# STATE (for change detection between runs)
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def build_state(shows, dates):
    show_state = {}
    venue_state = {}  # Per-venue aggregate, used for "theatre unblocked" alerts
    for s in shows:
        # Show-level state (existing)
        for c in s.categories:
            key = f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
            }
        # Venue-level aggregate (new)
        if s.venue_code not in venue_state:
            venue_state[s.venue_code] = {
                "name": s.venue_name,
                "show_count": 0,
                "has_available": False,  # True if at least one cat is not SOLD OUT
            }
        venue_state[s.venue_code]["show_count"] += 1
        for c in s.categories:
            if c.status != "0":
                venue_state[s.venue_code]["has_available"] = True
    date_state = {d.date_code: d.status for d in dates}
    return {"shows": show_state, "dates": date_state, "venues": venue_state}

def detect_changes(old_state, new_state):
    changes = []
    # ── Date-level changes ──
    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    for dc, status in new_dates.items():
        old_status = old_dates.get(dc)
        if (old_status == "NOT_OPEN"
                and status in ("BOOKABLE", "AVAILABLE")):
            changes.append(f"📅 NEW DATE OPENED: {dc}")
        elif old_status is None and status in ("BOOKABLE", "AVAILABLE"):
            # Brand-new date that wasn't in the previous snapshot at all
            changes.append(f"📅 NEW DATE OPENED: {dc}")

    # ── Venue-level changes (theatre unblocked) ──
    # Only detect if old_state already has venue tracking (avoids false
    # alerts on the first run after deploying this update).
    if "venues" in old_state:
        old_venues = old_state.get("venues", {})
        new_venues = new_state.get("venues", {})
        for vcode, vinfo in new_venues.items():
            old_v = old_venues.get(vcode)
            if old_v is None:
                # Venue wasn't in previous snapshot at all
                if vinfo["has_available"]:
                    changes.append(
                        f"🔓 THEATRE UNBLOCKED: {vinfo['name']} — "
                        f"now selling tickets ({vinfo['show_count']} show(s))"
                    )
            elif not old_v["has_available"] and vinfo["has_available"]:
                # Venue had shows but all SOLD OUT, now has available
                changes.append(
                    f"🔓 THEATRE UNBLOCKED: {vinfo['name']} — "
                    f"tickets back in stock"
                )

    # ── Show-level changes ──
    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})
    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        changes.append(
            f"🆕 NEW SHOW: {s['venue']} {s['time']} [{s['date']}] "
            f"— {s['cat']} Rs.{s['price']}"
        )
    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(
                new_s["status"], ("UNKNOWN", "⚪"))
            changes.append(
                f"{ico} BACK IN STOCK: {new_s['venue']} {new_s['time']} "
                f"[{new_s['date']}] — {new_s['cat']} -> {lbl}"
            )
    return changes

# ──────────────────────────────────────────────────────────────────────
# TELEGRAM NOTIFICATION
# ──────────────────────────────────────────────────────────────────────
def send_telegram(text):
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    if not token or not chat_id:
        print("  Telegram skipped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"  Telegram sent to chat {chat_id}")
            return True
        print(f"  Telegram error {resp.status_code}: {resp.text}")
        return False
    except requests.RequestException as e:
        print(f"  Telegram failed: {e}")
        return False

# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION (Resend) — optional
# ──────────────────────────────────────────────────────────────────────
def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]

def send_email(subject, changes, shows, movie_info):
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"
    if not api_key or not to:
        return
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")
    changes_html = ""
    if changes:
        rows = "".join(f"- {escape(c)}\n" for c in changes)
        changes_html = f"<h3>Changes Detected</h3><ul>{rows}</ul>"
    venue_groups = {}
    for s in shows:
        venue_groups.setdefault(s.venue_name, []).append(s)
    shows_html = ""
    for vname, vshows in venue_groups.items():
        show_rows = ""
        for s in vshows:
            cats = " | ".join(
                f"{escape(c.name)} Rs.{escape(c.price)} "
                f"({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{escape(s.screen_attr)}]" if s.screen_attr else ""
            show_rows += f"<tr><td>{escape(s.time)}{fmt}</td><td>{cats}</td></tr>"
        shows_html += (
            f"<h4>{escape(vname)}</h4>"
            f"<table border='1' cellpadding='4'>"
            f"<tr><th>Time</th><th>Categories</th></tr>"
            f"{show_rows}</table>"
        )
    html = f"""
    <h2>BMS Alert: {escape(movie_name)}</h2>
    <p>{escape(now_str)}</p>
    <hr/>
    {changes_html}
    <h3>Current Showtimes</h3>
    {shows_html}
    <p><a href="{escape(CONFIG['url'])}">Book now on BookMyShow</a></p>
    <p><i>Automated alert from BMS Ticket Notifier.</i></p>
    """
    plain_lines = [subject, "", f"Checked at: {now_str}", ""]
    if changes:
        plain_lines.append("Changes Detected:")
        plain_lines.extend(f"  - {c}" for c in changes)
    plain_lines.append("")
    plain_lines.append("Current Showtimes:")
    for vname, vshows in venue_groups.items():
        plain_lines.append(f"\n{vname}")
        for s in vshows:
            cats = " | ".join(
                f"{c.name} Rs.{c.price} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{s.screen_attr}]" if s.screen_attr else ""
            plain_lines.append(f"  {s.time}{fmt} - {cats}")
    plain_lines.extend(["", f"Book now: {CONFIG['url']}",
                        "", "Automated alert from BMS Ticket Notifier."])
    plain = "\n".join(plain_lines)
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": frm, "to": [to],
                "subject": subject,
                "text": plain, "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  Email sent to {to}")
        else:
            print(f"  Resend {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  Email failed: {e}")

# ──────────────────────────────────────────────────────────────────────
# ALERT DISPATCHER
# ──────────────────────────────────────────────────────────────────────
def send_alert(movie_info, changes, shows, is_baseline=False):
    """Send alert via Telegram (and email if configured).
    is_baseline=True means this is the first-ever run; we send a
    confirmation message instead of a 'changes detected' alert."""
    movie_name = movie_info.get("name", "Movie")
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    deep_link = booking_deep_link(CONFIG["url"])

    if is_baseline:
        # Build active-filters summary so you can verify setup at a glance
        filter_lines = []
        # Show movie label if set (helps distinguish Avengers vs Dune in matrix setup)
        label = CONFIG.get("movie_label", "").strip()
        if label:
            filter_lines.append(f"🏷️ Movie label: `{label}`")
        if CONFIG["filter_groups"]:
            filter_lines.append(f"🎯 Filter groups: `{CONFIG['filter_groups']}`")
        else:
            if CONFIG["theatre"]:
                filter_lines.append(f"🎭 Theatre filter: `{CONFIG['theatre']}`")
            if CONFIG["format"]:
                filter_lines.append(f"🎬 Format filter: `{CONFIG['format']}`")
        if CONFIG["time_period"]:
            filter_lines.append(f"🕒 Time filter: `{CONFIG['time_period']}`")
        if CONFIG["dates"]:
            filter_lines.append(f"📅 Date filter: `{CONFIG['dates']}`")
        filter_block = ("\n".join(filter_lines) + "\n\n") if filter_lines else "(monitoring all theatres / formats / times)\n\n"
        tg_text = (
            f"✅ *BMS Notifier is LIVE*\n\n"
            f"🎬 Monitoring: *{movie_name}*\n"
            f"📍 URL: {CONFIG['url']}\n"
            f"🕒 Baseline stored at: {now_str}\n\n"
            f"*Active filters:*\n{filter_block}"
            f"You'll get a push the moment booking opens, "
            f"a new show is added, a sold-out show comes back, "
            f"or a theatre unblocks tickets.\n\n"
            f"👉 Current booking page: {deep_link}"
        )
        send_telegram(tg_text)
        return

    # Normal alert with changes — sort so THEATRE UNBLOCKED and NEW DATE
    # appear first (most urgent for "ASAP book it"), then NEW SHOW, then
    # BACK IN STOCK.
    priority_order = [
        "🔓 THEATRE UNBLOCKED",
        "📅 NEW DATE OPENED",
        "🆕 NEW SHOW",
        "🟢 BACK IN STOCK",
        "🟠 BACK IN STOCK",
        "🟡 BACK IN STOCK",
    ]
    def change_priority(c):
        for i, prefix in enumerate(priority_order):
            if c.startswith(prefix):
                return i
        return len(priority_order)
    sorted_changes = sorted(changes, key=change_priority)
    change_lines = "\n".join(f"• {c}" for c in sorted_changes)

    # Detect if this is a high-urgency alert (theatre unblocked or new date)
    high_urgency = any(
        c.startswith("🔓 THEATRE UNBLOCKED") or c.startswith("📅 NEW DATE OPENED")
        for c in changes
    )
    urgency_banner = "🚨 *BOOK NOW!*" if high_urgency else "⚡"

    # Add movie label prefix if set (helps distinguish Avengers vs Dune)
    label = CONFIG.get("movie_label", "").strip()
    label_prefix = f"[{label}] " if label else ""

    tg_text = (
        f"{urgency_banner} {label_prefix}*{movie_name}* — {len(changes)} change(s)!\n\n"
        f"⚡ *Changes:*\n{change_lines}\n\n"
        f"👉 *TAP TO BOOK NOW:*\n{deep_link}\n\n"
        f"🕒 Checked: {now_str}"
    )
    send_telegram(tg_text)

    # Also send email if configured
    subject = f"BMS Alert: {movie_name} - {len(changes)} change(s)"
    send_email(subject, changes, shows, movie_info)

# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def _notify_bms_unreachable(movie_info):
    """Sent on first run when BMS API is unreachable (e.g. 403 from
    datacenter IP). Tells the user the bot itself is working but BMS
    is blocking, so they know it's not a setup problem."""
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    deep_link = booking_deep_link(CONFIG["url"])
    tg_text = (
        f"⚠️ *BMS Notifier is live but can't reach BookMyShow*\n\n"
        f"🎬 Tried to monitor: *{movie_info.get('name', 'Avengers: Doomsday')}*\n"
        f"📍 URL: {CONFIG['url']}\n"
        f"🕒 Attempted at: {now_str}\n\n"
        f"Your Telegram bot works (you're seeing this message). "
        f"But BookMyShow's API returned an error — likely an HTTP 403 "
        f"blocking GitHub's datacenter IP.\n\n"
        f"The script will keep retrying every 5 minutes. If this persists "
        f"for more than an hour, see the 'What if BMS blocks GitHub IPs?' "
        f"section of the setup guide.\n\n"
        f"👉 Movie page: {deep_link}"
    )
    send_telegram(tg_text)

def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Ticket Checker — Avengers: Doomsday mode")

    parsed = parse_bms_url(CONFIG["url"])
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")
    if not event_code or not region_slug:
        print("  Invalid BMS_URL. Could not extract event/region.")
        sys.exit(1)
    region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

    raw_dates = CONFIG["dates"].strip()
    if raw_dates:
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]
    print(f"  Event: {event_code}  Region: {region_code}  "
          f"Dates: {date_list}")

    all_shows = []
    all_dates = []
    movie_info = {"name": "Unknown", "language": ""}
    fetch_failed = False
    for dc in date_list:
        data = fetch_bms(event_code, dc, region_code,
                         region_slug_r, lat, lon, geohash)
        if not data:
            print(f"  No data for date {dc or '(default)'}")
            fetch_failed = True
            continue
        if movie_info["name"] == "Unknown":
            movie_info = parse_movie_info(data)
        all_dates.extend(parse_dates(data))
        all_shows.extend(parse_shows(data))

    if fetch_failed and not all_shows and not all_dates:
        # All fetches failed on this run. Don't update state (preserves last
        # known good state for change detection), but DO notify the user
        # so they know the monitor is alive but BMS is unreachable.
        print("  All fetches failed. State not updated.")
        old_state = load_state()
        if not old_state:
            # First-ever run + BMS unreachable — tell user the bot is live
            # but couldn't seed a baseline.
            _notify_bms_unreachable(movie_info)
        return

    if not all_shows and not all_dates:
        print("  No showtimes or dates found.")
        return

    print(f"  Movie: {movie_info['name']}  {movie_info['language']}")

    filter_groups = parse_filter_groups(CONFIG["filter_groups"])
    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"],
        CONFIG["format"],
        filter_groups=filter_groups,
    )
    if filter_groups:
        print(f"  Filter groups active: {filter_groups}")
    print(f"  {len(filtered)} showtime(s) after filters")

    new_state = build_state(filtered, all_dates)
    old_state = load_state()
    is_baseline = not old_state
    changes = [] if is_baseline else detect_changes(old_state, new_state)
    save_state(new_state)

    if is_baseline:
        print("  First run — baseline stored, no changes to report yet.")
        send_alert(movie_info, [], filtered, is_baseline=True)
    elif changes:
        print(f"\n  {len(changes)} change(s) detected:")
        for c in changes:
            print(f"     {c}")
        send_alert(movie_info, changes, filtered)
    else:
        print("  No changes since last check.")

    print(f"\n  Current status ({len(filtered)} shows):")
    for s in filtered:
        cats = ", ".join(
            f"{c.name}=Rs.{c.price}"
            f"({AVAIL_STATUS_MAP.get(c.status, ('?',''))[0]})"
            for c in s.categories
        )
        fmt = f"|{s.screen_attr}" if s.screen_attr else ""
        print(f"    {s.venue_name} - {s.time}{fmt} [{s.date_code}] - {cats}")
    print("\n  Done.")

if __name__ == "__main__":
    main()
