#!/usr/bin/env python3
"""small-hackathon-finder: a free daily list of small, low-competition, cash-prize
hackathons that are still open on Devpost.

Fetches every page of Devpost's public JSON listing of open hackathons,
then each hackathon page for its "Who can participate" list. Keeps the ones
that are open, not past their submission end date, not invite-only, and offer
at least one cash prize worth more than zero, ranks them by prize money per
registered participant (a rough proxy for "realistic chance of winning"), and
writes:

  docs/hackathons.json  - machine-readable results
  docs/index.html       - simple mobile-friendly table (published via
                          GitHub Pages)

Python 3.8+, standard library only, no API keys.

Usage:
    python3 hackathons.py            # fetch live data, write docs/
    python3 hackathons.py --top 20   # print top 20 to the console too
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL_TEMPLATE = "https://devpost.com/api/hackathons?status[]=open&page={page}"
USER_AGENT = (
    "small-hackathon-finder/1.0 "
    "(+https://github.com/Mzee04/small-hackathon-finder; open-source, runs twice daily)"
)
REQUEST_TIMEOUT = 30          # seconds per HTTP request
PAGE_DELAY = 1.5              # seconds between pages (be polite)
MAX_RETRIES = 3               # attempts per page
RETRY_BACKOFF = 2.0           # seconds, doubled after each failed attempt
PER_PAGE = 9                  # Devpost page size, used only as a sanity limit
MAX_PAGES = 500               # hard safety stop for the pagination loop
FX_RATES_DATE = "2026-10-26"  # when the approximate exchange rates below were taken

# Devpost's listing only gives a calendar end date, not a clock time. The
# deadline we store is the last second of that UTC day. The page then counts
# down from it and hides the row once the moment has passed.
UPDATE_CADENCE = "twice a day (GitHub may run it a few hours late)"

# A "$" prize this large, at a college or university, with this few people
# signed up, is often rupees typed in as dollars. Flag it rather than trust it.
SUSPICIOUS_USD_MIN = 20000.0
SUSPICIOUS_REGISTRANT_MAX = 100

LIMITS_NOTE = (
    "Honest limits: this list only covers hackathons listed on Devpost. "
    "Registration counts are not submissions - most registered teams never "
    "submit, so real odds are usually better than the numbers suggest, but "
    "the ratio is only a heuristic, not a prediction. Prize amounts are "
    "self-reported by organisers and are not verified, and may be split across "
    "many tracks or teams. Amounts are shown as listed and converted to "
    "approximate US dollars with hard-coded exchange rates (taken " + FX_RATES_DATE +
    ", not live); that dollar figure is used for ranking and shown next to "
    "non-dollar prizes. Listings flagged 'verify currency' look like rupees "
    "mislabeled as dollars (Indian-style digit grouping such as 1,55,000, or "
    "a very large dollar prize at a college or university with very few "
    "registrants). Those rows are ranked below the others, using a "
    "conservative rupee value. A currency with no known rate is listed after "
    "every converted prize. Prize text that mentions credits, bitcoin, crypto, "
    "tokens, subscriptions, swag, or goodies is marked 'includes non-cash "
    "prizes'. The countdown uses the submission end date (through the end of "
    "that UTC day), and a row disappears once that moment has passed. Always "
    "read each hackathon's rules page for eligibility, team-size limits and "
    "exact prize conditions before entering. "
    "Who can participate is read from each hackathon page. Listings limited "
    "to students, high school, professionals, partners, or a required team "
    "are hidden until you ask to see them. A country list that is tighter "
    "than Devpost's usual sanctions exceptions is marked country-limited, and "
    "a page that clearly requires a physical robot or other hardware is "
    "marked too. Neither of those hides the row. If that sidebar cannot be "
    "read, the row stays visible with no eligibility badge."
)

# ---------------------------------------------------------------------------
# Approximate exchange rates (offline, hard-coded)
# ---------------------------------------------------------------------------
# Prizes are listed in many currencies, so they are converted to US dollars
# before ranking. These rates are APPROXIMATE and DATED: units of each
# currency per 1 USD, rounded, taken from open.er-api.com on FX_RATES_DATE.
# They are only good enough to put hackathons in a sensible order; never use
# them for real money decisions. Update the table (and the date) now and then.
UNITS_PER_USD: Dict[str, float] = {
    "USD": 1.0,
    "INR": 95.2,
    "EUR": 0.892,
    "GBP": 0.748,
    "CAD": 1.40,
    "AUD": 1.42,
    "NZD": 1.75,
    "SGD": 1.28,
    "HKD": 7.84,
    "JPY": 157.0,
    "CNY": 6.68,
    "KRW": 1324.0,
    "BRL": 5.00,
    "MXN": 18.7,
    "PHP": 63.7,
    "IDR": 18090.0,
    "MYR": 4.09,
    "CHF": 0.825,
    "ZAR": 16.6,
    "TRY": 49.8,
    "AED": 3.67,
    "PKR": 277.0,
    "BDT": 123.5,
    "KES": 130.0,
}

# How Devpost (and organisers) write each currency -> ISO code. Matching is
# case-insensitive and ignores spaces. A plain "$" is US dollars on Devpost
# (other dollars are written like "$CAD"). Ambiguous symbols such as "¥"
# (yen or yuan) and a missing symbol are deliberately left out: those prizes
# are treated as an unknown currency.
CURRENCY_ALIASES: Dict[str, str] = {
    "$": "USD", "US$": "USD", "USD": "USD", "$USD": "USD", "USD$": "USD",
    "\u20b9": "INR", "INR": "INR", "RS": "INR", "RS.": "INR", "\u20b9INR": "INR",
    "\u20ac": "EUR", "EUR": "EUR",
    "\u00a3": "GBP", "GBP": "GBP",
    "$CAD": "CAD", "CAD": "CAD", "CA$": "CAD", "C$": "CAD", "CAD$": "CAD",
    "$AUD": "AUD", "AUD": "AUD", "AU$": "AUD", "A$": "AUD", "AUD$": "AUD",
    "$NZD": "NZD", "NZD": "NZD", "NZ$": "NZD",
    "$SGD": "SGD", "SGD": "SGD", "S$": "SGD",
    "$HKD": "HKD", "HKD": "HKD", "HK$": "HKD",
    "JPY": "JPY", "CNY": "CNY", "RMB": "CNY",
    "\u20a9": "KRW", "KRW": "KRW",
    "R$": "BRL", "BRL": "BRL",
    "MXN": "MXN", "$MXN": "MXN", "MX$": "MXN",
    "\u20b1": "PHP", "PHP": "PHP",
    "RP": "IDR", "IDR": "IDR",
    "RM": "MYR", "MYR": "MYR",
    "CHF": "CHF",
    "ZAR": "ZAR",
    "\u20ba": "TRY", "TRY": "TRY",
    "AED": "AED",
    "PKR": "PKR",
    "\u09f3": "BDT", "BDT": "BDT",
    "KES": "KES", "KSH": "KES",
}

_MONTH_ABBR = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
_FULL_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_MONTH_INDEX: Dict[str, int] = {}
for _i, _abbr in enumerate(_MONTH_ABBR, start=1):
    _MONTH_INDEX[_abbr.lower()] = _i
for _i, _name in enumerate(_FULL_MONTHS, start=1):
    _MONTH_INDEX[_name] = _i
_MONTH_INDEX["sept"] = 9

# Indian grouping puts a comma after the thousands, then every two digits:
# 1,55,000 or 1,00,000. Western grouping (150,000 / 1,000,000) does not match.
_INDIAN_GROUP_RE = re.compile(r"(?<![\d,])\d{1,2}(?:,\d{2})+,\d{3}(?![\d,])")
_INSTITUTION_RE = re.compile(
    r"\b(?:universit(?:y|ies)|colleges?|campuses|campus|institutes?|iits?|nits?|univ)\b",
    re.IGNORECASE,
)
_NON_CASH_RE = re.compile(
    r"\b(?:credits?|bitcoins?|btc|cryptocurrenc(?:y|ies)|crypto|tokens?|"
    r"subscriptions?|swag|goodies|goodie)\b",
    re.IGNORECASE,
)
_PERIOD_RE = re.compile(
    r"^\s*"
    r"(?P<sm>[A-Za-z]+)\s+"
    r"(?P<sd>\d{1,2})"
    r"(?:\s*,\s*(?P<sy>\d{4}))?"
    r"\s*[-–—]\s*"
    r"(?:(?P<em>[A-Za-z]+)\s+)?"
    r"(?P<ed>\d{1,2})"
    r"(?:\s*,\s*(?P<ey>\d{4}))?"
    r"\s*$"
)
_DEADLINE_ISO = "%Y-%m-%dT%H:%M:%SZ"


def currency_code(symbol: str) -> Optional[str]:
    """Map a prize currency symbol to an ISO code we have a rate for, or None."""
    key = re.sub(r"\s+", "", symbol or "").upper()
    code = CURRENCY_ALIASES.get(key)
    return code if code in UNITS_PER_USD else None


def to_usd(amount: float, symbol: str) -> Optional[float]:
    """Approximate USD value of an amount, or None for an unknown currency."""
    code = currency_code(symbol)
    if code is None:
        return None
    return amount / UNITS_PER_USD[code]


def has_indian_digit_grouping(text: str) -> bool:
    """True when text contains a number written the Indian way (1,55,000)."""
    return _INDIAN_GROUP_RE.search(text or "") is not None


def prize_text_includes_non_cash(text: str) -> bool:
    """True when prize text mentions credits, crypto, subscriptions, swag, etc."""
    return _NON_CASH_RE.search(text or "") is not None


def currency_review_reasons(
    symbol: str,
    amount: float,
    prize_text: str,
    title: str,
    organization: str,
    location: str,
    registrants: int,
) -> List[str]:
    """Reasons to distrust a listed currency, or an empty list.

    Indian digit grouping on a non-rupee amount (1,55,000 written next to a
    dollar sign) is a strong signal. So is a very large dollar prize at a
    college or university event that almost nobody has registered for.
    A real rupee symbol is trusted: the fx table already converts it.
    """
    reasons: List[str] = []
    code = currency_code(symbol)
    if code != "INR" and has_indian_digit_grouping(prize_text):
        reasons.append("indian_digit_grouping")
    blob = " ".join([title or "", organization or "", location or ""])
    if (
        code == "USD"
        and amount >= SUSPICIOUS_USD_MIN
        and registrants <= SUSPICIOUS_REGISTRANT_MAX
        and _INSTITUTION_RE.search(blob)
    ):
        reasons.append("large_college_prize")
    return reasons


def parse_submission_deadline(text: Optional[str]) -> Optional[datetime]:
    """Parse Devpost's submission_period_dates into an absolute UTC deadline.

    Examples: "Oct 05 - 27, 2026", "Aug 26 - Oct 30, 2026",
    "Dec 20, 2026 - Jan 05, 2027". The clock time is the last second of the
    end date in UTC, because the listing has no time of day.
    Returns None when the text is missing or not a date range.
    """
    if not text:
        return None
    match = _PERIOD_RE.match(str(text).strip())
    if not match:
        return None
    start_month = _MONTH_INDEX.get(match.group("sm").lower())
    end_month_raw = match.group("em")
    end_month = _MONTH_INDEX.get(end_month_raw.lower()) if end_month_raw else start_month
    if start_month is None or end_month is None:
        return None
    end_day = int(match.group("ed"))
    end_year = int(match.group("ey")) if match.group("ey") else None
    start_year = int(match.group("sy")) if match.group("sy") else None
    if end_year is None and start_year is None:
        return None
    if end_year is None:
        assert start_year is not None
        end_year = start_year + 1 if end_month < start_month else start_year
    try:
        return datetime(end_year, end_month, end_day, 23, 59, 59, tzinfo=timezone.utc)
    except ValueError:
        return None


def format_deadline_display(deadline: datetime) -> str:
    """'Oct 27, 2026' — English month abbreviations, not the machine locale."""
    return f"{_MONTH_ABBR[deadline.month - 1]} {deadline.day}, {deadline.year}"


def deadline_to_iso(deadline: datetime) -> str:
    return deadline.astimezone(timezone.utc).strftime(_DEADLINE_ISO)


def deadline_from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, _DEADLINE_ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_time_left(deadline: datetime, now: datetime) -> str:
    """Short countdown such as '3 days left' or 'less than a minute left'."""
    seconds = int((deadline - now).total_seconds())
    if seconds <= 0:
        return "ended"
    if seconds < 60:
        return "less than a minute left"
    minutes = seconds // 60
    hours = seconds // 3600
    days = seconds // 86400
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} left"
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} left"
    return f"{minutes} minute{'s' if minutes != 1 else ''} left"


_TAG_RE = re.compile(r"<[^>]+>")
# Currency (optional), a number, then any leftover prize wording ("plus swag").
_PRIZE_RE = re.compile(
    r"^\s*([^\d]*?)\s*([\d][\d,]*(?:\.\d+)?)(?:\s*(\D.*))?\s*$"
)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_json(url: str) -> Dict[str, Any]:
    """GET a URL with a polite User-Agent and return parsed JSON.

    Retries with linear-ish backoff on network errors and 5xx responses.
    Raises URLError/HTTPError on final failure.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            last_error = err
            if 400 <= err.code < 500 and err.code != 429:
                raise  # client errors other than rate-limiting will not fix themselves
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as err:
            last_error = err
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    assert last_error is not None
    raise last_error


def load_pages(
    fetch_page: Callable[[int], Dict[str, Any]] = lambda page: fetch_json(
        API_URL_TEMPLATE.format(page=page)
    ),
    delay: float = PAGE_DELAY,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch every page of the open-hackathon listing.

    Returns (items, stats). Pages are fetched politely (one short delay
    between requests). If a page fails after all retries the loop stops and
    returns what it has with stats["complete"] = False, so a partial result
    is reported honestly rather than silently.
    """
    items: List[Dict[str, Any]] = []
    total_count: Optional[int] = None
    pages_fetched = 0
    complete = True
    error: Optional[str] = None

    for page in range(1, MAX_PAGES + 1):
        try:
            data = fetch_page(page)
        except Exception as err:  # noqa: BLE001 - report and degrade gracefully
            complete = False
            error = f"page {page} failed after {MAX_RETRIES} attempts: {err}"
            break
        pages_fetched += 1
        page_items = data.get("hackathons") or []
        meta = data.get("meta") or {}
        if total_count is None and isinstance(meta.get("total_count"), int):
            total_count = meta["total_count"]
        items.extend(page_items)
        if not page_items:
            break  # empty page means we have reached the end of the listing
        if len(page_items) < PER_PAGE:
            break  # last partial page, no need for another request
        if delay > 0:
            time.sleep(delay)

    stats = {
        "total_count": total_count,
        "fetched": len(items),
        "pages_fetched": pages_fetched,
        "complete": complete,
        **({"error": error} if error else {}),
    }
    return items, stats


# ---------------------------------------------------------------------------
# Parsing and filtering
# ---------------------------------------------------------------------------

def parse_prize(raw: Optional[str]) -> Optional[Tuple[str, float]]:
    """Parse Devpost's prize_amount HTML into (currency_symbol, amount).

    Example: '$<span data-currency-value>1,500</span>' -> ('$', 1500.0)
    Trailing words ('plus swag') are allowed so a cash total can still be
    read when the same text mentions other prizes. Currency symbols are kept
    as-is here; see to_usd() for the approximate conversion used for ranking.
    Returns None when the value is missing or cannot be parsed.
    """
    if not raw:
        return None
    text = html_module.unescape(str(raw))
    stripped = _TAG_RE.sub("", text)
    match = _PRIZE_RE.match(stripped)
    if not match:
        return None
    symbol = match.group(1).strip()
    try:
        amount = float(match.group(2).replace(",", ""))
    except ValueError:
        return None
    if not symbol:
        symbol = ""  # e.g. bare "5,000" - still usable, just no known symbol
    return symbol, amount


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return utc_now()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


# Devpost's globe icon is its online flag. A map pin can still be an online
# contest: the location text says "Online", "City + Online", or only a host
# city such as "New York, NY" (not a venue you have to visit).
_ONLINE_WORD_RE = re.compile(r"\b(?:online|virtual|remote)\b", re.IGNORECASE)
_HOST_CITY_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]*,\s*[A-Z]{2}$")


def _location_record(item: Dict[str, Any]) -> Dict[str, Any]:
    displayed = item.get("displayed_location") or {}
    return displayed if isinstance(displayed, dict) else {}


def event_is_online(item: Dict[str, Any]) -> bool:
    """True when Devpost marks the hackathon online, including a host city.

    An explicit boolean on the item or on displayed_location wins. Otherwise
    the globe icon, the word online/virtual/remote in the location, or a
    bare host city ("New York, NY") counts as online. A venue, campus, or
    city name without that pattern stays in-person.
    """
    displayed = _location_record(item)
    for source in (item, displayed):
        for key in ("is_online", "online"):
            value = source.get(key)
            if isinstance(value, bool):
                return value
    icon = str(displayed.get("icon") or "").lower()
    if "globe" in icon:
        return True
    location = str(displayed.get("location") or "").strip()
    if _ONLINE_WORD_RE.search(location):
        return True
    return _HOST_CITY_RE.match(location) is not None


# Devpost prints the same sanctions list on most global hackathons. That is
# not a "this event is only for one country" restriction. Names are matched
# loosely because the sidebar spells them in a few official ways.
_STANDARD_EXCEPTION_MARKERS = (
    "brazil",
    "crimea",
    "cuba",
    "iran",
    "north korea",
    "democratic people's republic",
    "quebec",
    "russia",
    "syria",
)
_TEAM_LINE_RE = re.compile(
    r"team required(?:: (\d+)(?:\s*(?:to|–|—|-)\s*(\d+))?\s*members?)?$",
    re.IGNORECASE,
)
_AGE_MIN_RE = re.compile(r"ages \d+\+ only$", re.IGNORECASE)
_AGE_RANGE_RE = re.compile(r"ages (\d+) to (\d+) only$", re.IGNORECASE)
_PROFESSIONALS_RE = re.compile(
    r"\bprofessionals?(?:\s*/\s*post grads)?\s+only\b",
    re.IGNORECASE,
)
_PARTNER_AUDIENCE_RE = re.compile(
    r"\bcalling all\b(?:\s+\w+){0,8}\s+partners\b"
    r"|\bpartners?\s+only\b"
    r"|\bpartner employees\b"
    r"|\bonly\s+(?:open\s+to\s+)?(?:[\w]+\s+){0,4}partners\b",
    re.IGNORECASE,
)
_HIGH_SCHOOL_AGE_RE = re.compile(
    r"high[- ]school students(?:\s+ages?)?\s+\d{1,2}\s*(?:to|–|—|-)\s*1[1-8]\b",
    re.IGNORECASE,
)
_HARDWARE_RE = re.compile(
    r"\b("
    r"hardware(?:\s+is)?\s+required"
    r"|bring your (?:own )?(?:robot|hardware|device|kit)"
    r"|functional robot"
    r"|autonomous robot"
    r"|microcontrollers?"
    r")\b",
    re.IGNORECASE,
)
_ROBOT_WORD_RE = re.compile(r"\brobots?\b", re.IGNORECASE)
_ROBOT_PARTS_RE = re.compile(
    r"\b(arduinos?|esp32|raspberry pi|microcontrollers?)\b",
    re.IGNORECASE,
)
_NO_HARDWARE_RE = re.compile(
    r"\b(?:no hardware(?:\s+is)?\s+required|hardware not required|"
    r"without (?:any )?hardware|software[- ]only)\b",
    re.IGNORECASE,
)
_GENERIC_STUDENT_LINES = {
    "students only",
    "college students only",
    "university students only",
    "middle school students only",
}
_ID_OPEN_RE_TEMPLATE = r"<([A-Za-z0-9]+)[^>]*\bid=[\"']{element_id}[\"'][^>]*>"
_LI_INNER_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_TOOLTIP_RE = re.compile(
    r"\btitle\s*=\s*([\"'])(.*?)\1",
    re.IGNORECASE | re.DOTALL,
)


def _element_inner_by_id(html: str, element_id: str) -> Optional[str]:
    """Inner HTML of the first element with this id, respecting nesting."""
    pattern = re.compile(
        _ID_OPEN_RE_TEMPLATE.format(element_id=re.escape(element_id)),
        re.IGNORECASE,
    )
    match = pattern.search(html or "")
    if not match:
        return None
    tag = match.group(1)
    start = match.end()
    closer = re.compile(rf"</?{tag}\b[^>]*>", re.IGNORECASE)
    depth = 1
    for token in closer.finditer(html, start):
        raw = token.group(0)
        if raw.startswith("</"):
            depth -= 1
            if depth == 0:
                return html[start:token.start()]
        elif not raw.rstrip().endswith("/>"):
            depth += 1
    return None


def _plain_text(fragment: str) -> str:
    text = _TAG_RE.sub(" ", html_module.unescape(fragment or ""))
    return re.sub(r"\s+", " ", text).strip()


def _split_countries(tooltip: Optional[str]) -> List[str]:
    text = html_module.unescape(tooltip or "").replace("’", "'")
    return [part.strip() for part in text.split(",") if part.strip()]


def _is_standard_exception_list(names: List[str]) -> bool:
    """True when every name is one of Devpost's usual sanctions exceptions."""
    if not names:
        return False
    for name in names:
        folded = re.sub(r"\s+", " ", name.lower().replace("’", "'")).strip()
        if not any(marker in folded for marker in _STANDARD_EXCEPTION_MARKERS):
            return False
    return True


def blank_eligibility() -> Dict[str, Any]:
    """Eligibility we could not read. The listing stays visible, with no badge."""
    return {
        "eligibility_known": False,
        "eligibility_lines": [],
        "students_only": False,
        "high_school_only": False,
        "professionals_only": False,
        "partners_or_invite_only": False,
        "team_required": False,
        "team_size_min": None,
        "team_size_max": None,
        "country_limited": False,
        "countries": [],
        "needs_hardware": False,
    }


def text_needs_hardware(text: str) -> bool:
    """True when a page clearly requires a physical robot or other hardware.

    A passing mention is not enough. A line that says hardware is not
    required wins, so an optional kit does not hide a software event.
    """
    if not text or _NO_HARDWARE_RE.search(text):
        return False
    if _HARDWARE_RE.search(text):
        return True
    return bool(_ROBOT_WORD_RE.search(text) and _ROBOT_PARTS_RE.search(text))


def classify_eligibility_line(
    text: str,
    tooltip: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """Map one 'Who can participate' line to flags and a display string.

    Open-to-adults lines (age of majority, 18+, Devpost's standard country
    exceptions, companies excluded) add no flag. Unknown lines that still
    say 'only' are treated as a closed group.
    """
    flags: Dict[str, Any] = {
        "students_only": False,
        "high_school_only": False,
        "professionals_only": False,
        "partners_or_invite_only": False,
        "team_required": False,
        "team_size_min": None,
        "team_size_max": None,
        "country_limited": False,
        "countries": [],
    }
    low = re.sub(r"\s+", " ", (text or "")).strip().lower()
    display = re.sub(r"\s+", " ", (text or "")).strip()
    countries = _split_countries(tooltip)
    if not low:
        return flags, display

    if low.startswith("above legal age of majority") or _AGE_MIN_RE.fullmatch(low):
        return flags, display
    if "companies/professional organizations excluded" in low or low.startswith("company not required"):
        return flags, display
    if low.startswith("all countries/territories, excluding standard exceptions"):
        return flags, display

    age_range = _AGE_RANGE_RE.fullmatch(low)
    if age_range:
        hi = int(age_range.group(2))
        if hi <= 18:
            flags["high_school_only"] = True
            flags["students_only"] = True
        elif hi <= 24:
            # A closed age cap still keeps most adults out.
            flags["partners_or_invite_only"] = True
        return flags, display

    if "only specific" in low and "included" in low:
        flags["country_limited"] = True
        flags["countries"] = countries
        if countries:
            display = f"{display}: {', '.join(countries)}"
        return flags, display
    if "countries/territories excluded" in low or low.endswith("countries excluded"):
        # No tooltip, or only the usual sanctions list: not a country limit.
        # A list that names somewhere else is.
        if countries and not _is_standard_exception_list(countries):
            flags["country_limited"] = True
            flags["countries"] = countries
            display = f"{display}: {', '.join(countries)}"
        return flags, display
    if re.search(r"\b(us|usa|u\.s\.|united states|uk|united kingdom|india|canada)\s+only\b", low):
        flags["country_limited"] = True
        flags["countries"] = countries or [display]
        return flags, display

    team = _TEAM_LINE_RE.fullmatch(low)
    if team:
        flags["team_required"] = True
        if team.group(1):
            flags["team_size_min"] = int(team.group(1))
        if team.group(2):
            flags["team_size_max"] = int(team.group(2))
        return flags, display

    if _PROFESSIONALS_RE.search(low):
        flags["professionals_only"] = True
        return flags, display

    if re.search(r"\bhigh school\b", low):
        flags["high_school_only"] = True
        flags["students_only"] = True
        if low != "high school students only":
            flags["partners_or_invite_only"] = True
        return flags, display

    if re.search(r"\bstudents only\b", low):
        flags["students_only"] = True
        if low not in _GENERIC_STUDENT_LINES:
            flags["partners_or_invite_only"] = True
        return flags, display

    if re.search(r"\b(partners?|employees)\b", low) and "only" in low:
        flags["partners_or_invite_only"] = True
        return flags, display
    if "invite only" in low or "invite-only" in low:
        flags["partners_or_invite_only"] = True
        return flags, display
    if re.search(r"\bonly\b", low):
        flags["partners_or_invite_only"] = True
    return flags, display


def _merge_eligibility(into: Dict[str, Any], flags: Dict[str, Any]) -> None:
    for key in (
        "students_only",
        "high_school_only",
        "professionals_only",
        "partners_or_invite_only",
        "team_required",
        "country_limited",
    ):
        if flags.get(key):
            into[key] = True
    for key in ("team_size_min", "team_size_max"):
        if flags.get(key) is not None:
            into[key] = flags[key]
    for name in flags.get("countries") or []:
        if name not in into["countries"]:
            into["countries"].append(name)


def _li_text_and_tooltip(inner: str) -> Tuple[str, Optional[str]]:
    tip = _TOOLTIP_RE.search(inner or "")
    tooltip = html_module.unescape(tip.group(2)).strip() if tip else None
    return _plain_text(inner), tooltip


def parse_eligibility_page(html: str) -> Dict[str, Any]:
    """Read a Devpost hackathon page into eligibility fields.

    Missing sidebar -> eligibility_known False and every restriction flag
    false, so the listing stays in the default view with no badge.
    """
    result = blank_eligibility()
    listing = _element_inner_by_id(html or "", "eligibility-list")
    if listing is None:
        return result
    result["eligibility_known"] = True
    lines: List[str] = []
    for inner in _LI_INNER_RE.findall(listing):
        text, tooltip = _li_text_and_tooltip(inner)
        if not text:
            continue
        flags, display = classify_eligibility_line(text, tooltip)
        _merge_eligibility(result, flags)
        if display:
            lines.append(display)
    result["eligibility_lines"] = lines

    description_html = _element_inner_by_id(html, "challenge-description") or ""
    requirements_html = _element_inner_by_id(html, "challenge-requirements") or ""
    description = _plain_text(description_html)
    requirements = _plain_text(requirements_html)
    blob = f"{description} {requirements}".strip()
    if _PARTNER_AUDIENCE_RE.search(description[:1500]):
        result["partners_or_invite_only"] = True
    if result["students_only"] and _HIGH_SCHOOL_AGE_RE.search(blob):
        result["high_school_only"] = True
    if text_needs_hardware(blob):
        result["needs_hardware"] = True
    return result


def listing_is_limited(entry: Dict[str, Any]) -> bool:
    """True when the default view should hide this listing.

    Country limits and hardware needs are badged but still shown: the
    default view hides a closed group or a required team only.
    """
    return bool(
        entry.get("students_only")
        or entry.get("high_school_only")
        or entry.get("professionals_only")
        or entry.get("partners_or_invite_only")
        or entry.get("team_required")
    )


def eligibility_badge_labels(entry: Dict[str, Any]) -> List[str]:
    """Short badge texts for one listing. Empty when nothing was found."""
    labels: List[str] = []
    if entry.get("high_school_only"):
        labels.append("High school only")
    elif entry.get("students_only"):
        labels.append("Students only")
    if entry.get("professionals_only") or entry.get("partners_or_invite_only"):
        labels.append("Restricted")
    if entry.get("team_required"):
        minimum = entry.get("team_size_min")
        if isinstance(minimum, int) and minimum > 0:
            labels.append(f"Team required ({minimum}+)")
        else:
            labels.append("Team required")
    if entry.get("country_limited"):
        labels.append("Country-limited")
    if entry.get("needs_hardware"):
        labels.append("Needs hardware")
    return labels


def _badge_class(label: str) -> str:
    if label.startswith("Team required"):
        return "team"
    return {
        "Students only": "limit",
        "High school only": "limit",
        "Restricted": "restricted",
        "Country-limited": "country",
        "Needs hardware": "hardware",
    }.get(label, "restricted")


def fetch_text(url: str) -> str:
    """GET a hackathon page as HTML.

    Devpost's CDN serves a browser check to clients that ask for text/html
    and a normal page to clients that send Accept: */*. Retries transient
    failures. A browser-check page is a failure, not an empty eligibility list.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                text = response.read().decode("utf-8", "replace")
            if "eligibility-list" not in text and "Just a moment" in text:
                raise URLError("browser check page")
            return text
        except HTTPError as err:
            last_error = err
            if 400 <= err.code < 500 and err.code != 429:
                raise
        except (URLError, TimeoutError, OSError) as err:
            last_error = err
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    assert last_error is not None
    raise last_error


def attach_eligibility(
    entries: List[Dict[str, Any]],
    fetch_html: Callable[[str], str] = fetch_text,
    delay: float = PAGE_DELAY,
) -> Dict[str, int]:
    """Fill eligibility fields on each entry from its Devpost page.

    One short pause before every page, including the first, so this does not
    run straight into the listing fetch. A page that fails is left unknown:
    no badge, and the row stays in the default view. Returns counts.
    """
    known = 0
    unknown = 0
    for entry in entries:
        if delay > 0:
            time.sleep(delay)
        url = entry.get("url") or ""
        try:
            if not url:
                raise URLError("missing url")
            parsed = parse_eligibility_page(fetch_html(url))
        except Exception:  # noqa: BLE001 - one bad page must not drop the list
            parsed = blank_eligibility()
        if parsed.get("eligibility_known"):
            known += 1
        else:
            unknown += 1
        entry.update(parsed)
    return {"eligibility_known": known, "eligibility_unknown": unknown}


def build_entry(
    item: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Turn a Devpost listing item into our record, or None if filtered out.

    Keeps only hackathons that are open, still before their submission end
    date, not invite-only, offer at least one cash prize, and have a
    parseable prize amount above zero.
    """
    now = _as_utc(now)
    if item.get("open_state") != "open":
        return None
    if item.get("invite_only"):
        return None
    prizes_counts = item.get("prizes_counts") or {}
    if not isinstance(prizes_counts.get("cash"), int) or prizes_counts["cash"] < 1:
        return None
    parsed = parse_prize(item.get("prize_amount"))
    if parsed is None:
        return None
    symbol, amount = parsed
    if amount <= 0:
        return None

    deadline = parse_submission_deadline(item.get("submission_period_dates") or "")
    if deadline is not None and deadline <= now:
        return None

    prize_usd = to_usd(amount, symbol)
    registrants = item.get("registrations_count")
    registrants = registrants if isinstance(registrants, int) and registrants >= 0 else 0
    displayed = _location_record(item)
    location = displayed.get("location") or ""
    location_icon = displayed.get("icon") or ""
    themes = [t.get("name") for t in (item.get("themes") or []) if t.get("name")]
    title = item.get("title") or "Untitled hackathon"
    organization = item.get("organization_name") or ""
    prize_text = _TAG_RE.sub("", html_module.unescape(str(item.get("prize_amount") or "")))
    reasons = currency_review_reasons(
        symbol, amount, prize_text, title, organization, location, registrants
    )
    return {
        "title": title,
        "url": item.get("url") or "",
        "prize_amount": amount,
        "prize_currency": symbol,
        "prize_display": prize_text.strip(),
        "cash_prizes": prizes_counts["cash"],
        "registrations_count": registrants,
        # A hackathon with zero registrants so far still counts as low
        # competition, so it is ranked as if exactly one person registered.
        "prize_per_registrant": round(amount / max(registrants, 1), 2),
        # Approximate US-dollar values used for ranking (None when the
        # currency has no known rate; see UNITS_PER_USD). These follow the
        # currency the organiser wrote down. Suspicious rows are ranked
        # separately; see _rank_key.
        "prize_currency_code": currency_code(symbol),
        "currency_known": prize_usd is not None,
        "prize_usd_approx": round(prize_usd, 2) if prize_usd is not None else None,
        "usd_per_registrant_approx": (
            round(prize_usd / max(registrants, 1), 2) if prize_usd is not None else None
        ),
        "currency_needs_review": bool(reasons),
        "currency_review_reasons": reasons,
        "includes_non_cash_prizes": prize_text_includes_non_cash(prize_text),
        "time_left_to_submission": item.get("time_left_to_submission") or "",
        "submission_period_dates": item.get("submission_period_dates") or "",
        "submission_deadline_utc": deadline_to_iso(deadline) if deadline else None,
        "submission_deadline_display": format_deadline_display(deadline) if deadline else "",
        "location": location,
        "location_icon": location_icon,
        "is_online": event_is_online(item),
        "themes": themes,
        "organization_name": organization,
    }


def build_entries(
    items: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Filter and rank raw Devpost items by approximate USD prize per registrant.

    Prizes in a known currency are ranked first, by USD per registrant.
    Prizes in an unknown currency cannot be compared, so they always come
    after every known one, ordered among themselves by their raw numbers.
    Listings flagged 'verify currency' come last. Among themselves they are
    ordered as if the number were Indian rupees, so a mislabelled dollar
    figure cannot float to the top.
    """
    now = _as_utc(now)
    entries = [entry for item in items if (entry := build_entry(item, now=now)) is not None]
    entries.sort(key=_rank_key)
    return entries


def _conservative_inr_usd(amount: float) -> float:
    """Read a suspicious number as rupees and convert with the fx table."""
    return amount / UNITS_PER_USD["INR"]


def _rank_key(e: Dict[str, Any]) -> Tuple[int, float, float, str]:
    title = e["title"]
    if e.get("currency_needs_review"):
        if e.get("prize_currency_code") == "INR" and e.get("prize_usd_approx") is not None:
            usd = e["prize_usd_approx"]
        else:
            usd = _conservative_inr_usd(e["prize_amount"])
        per = usd / max(e["registrations_count"], 1)
        return (2, -per, -usd, title)
    if e["currency_known"]:
        return (0, -e["usd_per_registrant_approx"], -e["prize_usd_approx"], title)
    return (1, -e["prize_per_registrant"], -e["prize_amount"], title)


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return utc_now().strftime(_DEADLINE_ISO)


def _stats_with_shown(entries: List[Dict[str, Any]], stats: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(stats)
    fetched = out.get("fetched")
    if isinstance(fetched, int):
        out["shown"] = len(entries)
        out["skipped"] = fetched - len(entries)
    online = sum(1 for entry in entries if entry.get("is_online"))
    out["online"] = online
    out["in_person"] = len(entries) - online
    out["restricted_or_team"] = sum(1 for entry in entries if listing_is_limited(entry))
    out["eligibility_known"] = sum(1 for entry in entries if entry.get("eligibility_known"))
    out["eligibility_unknown"] = sum(
        1 for entry in entries if "eligibility_known" in entry and not entry.get("eligibility_known")
    )
    return out


def coverage_summary(
    shown: int,
    stats: Dict[str, Any],
    online: Optional[int] = None,
    in_person: Optional[int] = None,
    restricted: int = 0,
) -> str:
    """'24 online shown (7 in-person hidden, 19 skipped: ...)'.

    ``restricted`` is how many online rows the default view hides because
    they are limited to a group or require a team. Those rows are not also
    counted in ``in_person``. When it is zero the sentence stays the older
    'online shown' wording.
    """
    if online is None:
        online = shown
    if in_person is None:
        in_person = max(shown - online, 0)
    if restricted < 0:
        restricted = 0
    open_online = online - restricted
    if open_online < 0:
        open_online = 0
    fetched = stats.get("fetched")
    if isinstance(fetched, int):
        skipped = fetched - shown
        if skipped < 0:
            skipped = 0
        skip_note = f"{skipped} skipped: no cash prize or invite-only, or deadline passed"
        if restricted:
            text = (
                f"{open_online} shown ({restricted} restricted or team-only hidden, "
                f"{in_person} in-person hidden, {skip_note})"
            )
        else:
            text = f"{online} online shown ({in_person} in-person hidden, {skip_note})"
    else:
        if restricted:
            text = (
                f"{open_online} shown ({restricted} restricted or team-only hidden, "
                f"{in_person} in-person hidden)"
            )
        else:
            text = f"{online} online shown ({in_person} in-person hidden)"
    if not stats.get("complete", True):
        text += " (partial fetch - a page failed, see hackathons.json)"
    return text


def write_json_doc(entries: List[Dict[str, Any]], stats: Dict[str, Any], path: Path) -> None:
    doc = {
        "last_updated_utc": utc_now_iso(),
        "source": "https://devpost.com/api/hackathons?status[]=open",
        "ranking": (
            "approximate USD prize / registrations_count (registrations are not "
            "submissions); listings flagged verify-currency are ranked after the "
            "others using a conservative INR reading; unknown currencies listed last"
        ),
        "fx_rates": {
            "as_of": FX_RATES_DATE,
            "note": "approximate, hard-coded units per 1 USD; for ranking only",
            "units_per_usd": UNITS_PER_USD,
        },
        "note": LIMITS_NOTE,
        "stats": _stats_with_shown(entries, stats),
        "hackathons": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _esc(value: Any) -> str:
    return html_module.escape(str(value), quote=True)


def format_per_registrant(e: Dict[str, Any]) -> str:
    """'$210' for known currencies (approx. USD), raw symbol + '?' otherwise."""
    if e["currency_known"]:
        prefix = "" if e["prize_currency_code"] == "USD" else "~"
        return f"{prefix}${e['usd_per_registrant_approx']:,.0f}"
    return f"{e['prize_currency']}{e['prize_per_registrant']:,.0f}?"


# Runs in the browser so 'time left' does not stay frozen at the last refresh.
# Rows whose deadline has passed are hidden and the rank numbers close up.
PAGE_SCRIPT = """<script>
(function () {
  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s") + " left";
  }
  function label(ms) {
    var seconds = Math.floor(ms / 1000);
    if (seconds < 60) return "less than a minute left";
    var minutes = Math.floor(seconds / 60);
    var hours = Math.floor(minutes / 60);
    var days = Math.floor(hours / 24);
    if (days >= 1) return plural(days, "day");
    if (hours >= 1) return plural(hours, "hour");
    return plural(minutes, "minute");
  }
  function refresh() {
    var box = document.getElementById("show-in-person");
    var showInPerson = !!(box && box.checked);
    var limitedBox = document.getElementById("show-restricted");
    var showRestricted = !!(limitedBox && limitedBox.checked);
    var rows = document.querySelectorAll("tbody tr");
    var ended = 0;
    var onlineShown = 0;
    var inPersonShown = 0;
    var inPersonHidden = 0;
    var restrictedHidden = 0;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var iso = row.getAttribute("data-deadline");
      var online = row.getAttribute("data-online") !== "0";
      var restricted = row.getAttribute("data-restricted") === "1";
      var past = false;
      if (iso) {
        var end = Date.parse(iso);
        var text = row.querySelector(".timeleft-text");
        if (isNaN(end) || end <= Date.now()) past = true;
        else if (text) text.textContent = label(end - Date.now());
      }
      if (past) {
        row.style.display = "none";
        ended += 1;
        continue;
      }
      var hideInPerson = !online && !showInPerson;
      var hideRestricted = restricted && !showRestricted;
      if (hideInPerson || hideRestricted) {
        row.style.display = "none";
        if (hideInPerson) inPersonHidden += 1;
        else restrictedHidden += 1;
        continue;
      }
      row.style.display = "table-row";
      if (online) onlineShown += 1;
      else inPersonShown += 1;
    }
    var rank = 1;
    for (var j = 0; j < rows.length; j++) {
      if (rows[j].style.display === "none") continue;
      var cell = rows[j].querySelector("td.rank");
      if (cell) cell.textContent = String(rank++);
    }
    var el = document.getElementById("coverage-text");
    if (!el) return;
    var skipped = el.getAttribute("data-skipped") || "0";
    var endedNote = ended ? "; " + ended + " ended since the last refresh" : "";
    var skipNote = skipped + " skipped: no cash prize or invite-only, or deadline passed";
    var shown = onlineShown + inPersonShown;
    if (showInPerson) {
      var base = shown + " shown (" + onlineShown + " online, " + inPersonShown + " in-person";
      if (restrictedHidden) base += "; " + restrictedHidden + " restricted or team-only hidden";
      el.textContent = base + "; " + skipNote + endedNote + ")";
    } else if (restrictedHidden) {
      el.textContent = onlineShown + " shown (" + restrictedHidden +
        " restricted or team-only hidden, " + inPersonHidden +
        " in-person hidden, " + skipNote + endedNote + ")";
    } else {
      el.textContent = onlineShown + " online shown (" + inPersonHidden +
        " in-person hidden, " + skipNote + endedNote + ")";
    }
  }
  var toggles = ["show-in-person", "show-restricted"];
  for (var t = 0; t < toggles.length; t++) {
    var toggle = document.getElementById(toggles[t]);
    if (toggle) toggle.addEventListener("change", refresh);
  }
  refresh();
  setInterval(refresh, 60000);
})();
</script>
"""


def write_html_doc(entries: List[Dict[str, Any]], stats: Dict[str, Any], path: Path) -> None:
    updated = utc_now_iso()
    now = utc_now()
    rows = []
    online_rank = 0
    for e in entries:
        where = "Online" if e["is_online"] else _esc(e["location"] or "See site")
        limited = listing_is_limited(e)
        if e["is_online"] and not limited:
            online_rank += 1
            rank_cell = str(online_rank)
        else:
            rank_cell = ""
        themes = ", ".join(e["themes"][:3]) + ("..." if len(e["themes"]) > 3 else "")
        per_reg = format_per_registrant(e)
        usd_note = ""
        if e["prize_currency_code"] not in (None, "USD"):
            usd_note = f"<div class='sub'>&asymp; ${e['prize_usd_approx']:,.0f}</div>"
        elif not e["currency_known"]:
            usd_note = "<div class='sub'>currency not converted</div>"
        badges = ""
        for label in eligibility_badge_labels(e):
            badges += (
                f"<div><span class='badge {_badge_class(label)}'>{_esc(label)}</span></div>"
            )
        if e.get("currency_needs_review"):
            badges += "<div><span class='badge verify'>verify currency</span></div>"
        if e.get("includes_non_cash_prizes"):
            badges += "<div><span class='badge noncash'>includes non-cash prizes</span></div>"
        deadline = deadline_from_iso(e.get("submission_deadline_utc"))
        if deadline is not None:
            time_cell = (
                f"<span class='timeleft-text'>{_esc(format_time_left(deadline, now))}</span>"
                f"<div class='sub'>Deadline: {_esc(e.get('submission_deadline_display') or '')}</div>"
            )
            deadline_attr = f" data-deadline='{_esc(e['submission_deadline_utc'])}'"
        else:
            time_cell = _esc(e.get("time_left_to_submission") or "-")
            deadline_attr = ""
        online_attr = "1" if e["is_online"] else "0"
        restricted_attr = "1" if limited else "0"
        rows.append(
            f"<tr{deadline_attr} data-online='{online_attr}' data-restricted='{restricted_attr}'>"
            f"<td class='rank'>{rank_cell}</td>"
            f"<td class='title'><a href='{_esc(e['url'])}' target='_blank' rel='noopener noreferrer'>{_esc(e['title'])}</a>"
            f"{badges}"
            f"<div class='sub'>{_esc(themes)}</div></td>"
            f"<td class='num'>{_esc(e['prize_display'])}{usd_note}<div class='sub'>{e['cash_prizes']} cash prize{'s' if e['cash_prizes'] != 1 else ''}</div></td>"
            f"<td class='num'>{e['registrations_count']:,}<div class='sub'>{_esc(per_reg)} / registrant</div>"
            + ("<div class='sub'>ranked lower</div>" if e.get("currency_needs_review") else "")
            + "</td>"
            f"<td>{time_cell}</td>"
            f"<td class='where'>{'🌐' if e['is_online'] else '📍'} {where}</td>"
            "</tr>"
        )
    shown = len(entries)
    online_count = sum(1 for e in entries if e.get("is_online"))
    in_person_count = shown - online_count
    fetched = stats.get("fetched")
    skipped = fetched - shown if isinstance(fetched, int) else 0
    if skipped < 0:
        skipped = 0
    restricted_online = sum(
        1 for e in entries if e.get("is_online") and listing_is_limited(e)
    )
    coverage = coverage_summary(
        shown, stats, online=online_count, in_person=in_person_count, restricted=restricted_online
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Small Hackathon Finder</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         margin: 0 auto; padding: 1rem; max-width: 60rem; line-height: 1.45; }}
  h1 {{ font-size: 1.4rem; margin: 0.5rem 0; }}
  p.updated {{ color: #888; margin-top: 0; }}
  .note {{ background: rgba(127,127,127,.12); border-radius: 8px; padding: .75rem 1rem; font-size: .85rem; }}
  .note strong {{ display: block; margin-bottom: .25rem; }}
  .scroll {{ overflow-x: auto; -webkit-overflow-scrolling: touch; margin-top: 1rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
  th, td {{ padding: .5rem .6rem; text-align: left; border-bottom: 1px solid rgba(127,127,127,.3); vertical-align: top; }}
  th {{ position: sticky; top: 0; background: inherit; white-space: nowrap; font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; opacity: .8; }}
  td.rank {{ text-align: right; color: #888; white-space: nowrap; }}
  td.num {{ text-align: right; white-space: nowrap; }}
  td.where {{ white-space: nowrap; }}
  .sub {{ font-size: .75rem; opacity: .7; }}
  .badge {{ display: inline-block; margin-top: .25rem; font-size: .7rem; font-weight: 600; line-height: 1.3;
           padding: .05rem .4rem; border-radius: 999px; color: #1a1a1a; }}
  .badge.verify {{ background: #f5c518; }}
  .badge.noncash {{ background: #c9ddff; }}
  .badge.limit {{ background: #f3b4c8; }}
  .badge.team {{ background: #b7e4c7; }}
  .badge.restricted {{ background: #f6c59a; }}
  .badge.country {{ background: #d7c6f5; }}
  .badge.hardware {{ background: #f3dd9a; }}
  .toggle {{ display: flex; align-items: center; gap: .5rem; margin: .6rem 0 0; font-weight: 600; }}
  .toggle input {{ width: 1.15rem; height: 1.15rem; }}
  tr[data-online="0"], tr[data-restricted="1"] {{ display: none; }}
  a {{ color: #0969da; }}
  @media (prefers-color-scheme: dark) {{ a {{ color: #4da3ff; }} }}
  footer {{ margin-top: 1.5rem; font-size: .8rem; color: #888; }}
</style>
</head>
<body>
<h1>🏆 Small Hackathon Finder</h1>
<p class="updated">Open hackathons with cash prizes, ranked by prize money per registered participant
(fewer registrants &rarr; higher on the list). Non-dollar prizes are converted to
approximate US dollars for ranking (rough rates from {_esc(FX_RATES_DATE)}).
Rows marked verify currency are ranked lower. Time left is counted in your browser
from the submission deadline. Online hackathons open to one person are shown
first. Tick the boxes to include in-person events, or listings limited to a
group or a required team. Updated {_esc(UPDATE_CADENCE)}.</p>
<p class="updated">Last updated: <time datetime="{_esc(updated)}">{_esc(updated)}</time>
 &middot; <span id="coverage-text" data-shown="{shown}" data-online="{online_count}" data-in-person="{in_person_count}" data-skipped="{skipped}">{_esc(coverage)}</span></p>

<label class="toggle"><input type="checkbox" id="show-in-person"> Show in-person events too</label>
<label class="toggle"><input type="checkbox" id="show-restricted"> Show restricted / team-only</label>

<div class="note">
<strong>Read this before picking one &ndash; honest limits</strong>
{_esc(LIMITS_NOTE)}
</div>

<div class="scroll">
<table>
<thead><tr>
  <th>#</th><th>Hackathon</th><th>Prize</th><th>Registrants</th><th>Time left</th><th>Where</th>
</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
</div>

{PAGE_SCRIPT}
<footer>
Source: <a href="https://github.com/Mzee04/small-hackathon-finder">Mzee04/small-hackathon-finder</a>
&middot; data from <a href="https://devpost.com/hackathons" rel="noopener noreferrer">Devpost</a>
&middot; full data in <a href="hackathons.json">hackathons.json</a>
</footer>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------

def print_report(entries: List[Dict[str, Any]], stats: Dict[str, Any], top: int) -> None:
    total = stats.get("total_count")
    print(
        f"Fetched {stats.get('fetched')} of {total if total is not None else '?'} "
        f"open hackathons on Devpost ({stats.get('pages_fetched')} pages, "
        f"{'complete' if stats.get('complete', True) else 'PARTIAL'})."
    )
    if not stats.get("complete", True) and stats.get("error"):
        print(f"  fetch error: {stats['error']}")
    online_count = sum(1 for e in entries if e.get("is_online"))
    restricted_online = sum(1 for e in entries if e.get("is_online") and listing_is_limited(e))
    print(coverage_summary(
        len(entries), stats,
        online=online_count,
        in_person=len(entries) - online_count,
        restricted=restricted_online,
    ) + ".")
    if "eligibility_known" in stats:
        print(
            f"Eligibility read for {stats.get('eligibility_known', 0)} of {len(entries)} "
            f"listings ({stats.get('eligibility_unknown', 0)} unknown, left visible)."
        )
    print(
        "Ranked by approx. USD prize per registrant. "
        "Rows marked verify currency are listed after the others."
    )
    if not entries:
        print("Nothing matched the filters.")
        return
    print(f"\nTop {min(top, len(entries))} by approx. USD prize per registrant "
          f"(rough rates from {FX_RATES_DATE}; ~ = converted, ? = unknown currency):\n")
    header = (f"{'#':>3}  {'Prize':>12}  {'~USD':>10}  {'Reg':>6}  {'Per reg':>9}  "
              f"{'Deadline':<16}  {'Online':<6}  Title")
    print(header)
    print("-" * len(header))
    for i, e in enumerate(entries[:top], start=1):
        deadline = e.get("submission_deadline_display") or "-"
        online = "yes" if e["is_online"] else "no"
        title = e["title"]
        if len(title) > 48:
            title = title[:45] + "..."
        usd = f"${e['prize_usd_approx']:,.0f}" if e["currency_known"] else "?"
        flags = []
        if e.get("currency_needs_review"):
            flags.append("verify currency")
        if e.get("includes_non_cash_prizes"):
            flags.append("non-cash")
        flags.extend(eligibility_badge_labels(e))
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(
            f"{i:>3}  {e['prize_display']:>12}  {usd:>10}  {e['registrations_count']:>6}  "
            f"{format_per_registrant(e):>9}  {deadline:<16}  {online:<6}  {title}{suffix}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rank open Devpost hackathons by prize money per registrant."
    )
    parser.add_argument("--top", type=int, default=10,
                        help="how many rows to print to the console (default: 10)")
    parser.add_argument("--out-dir", type=Path, default=Path("docs"),
                        help="output directory for index.html and hackathons.json (default: docs)")
    parser.add_argument("--delay", type=float, default=PAGE_DELAY,
                        help=f"seconds to sleep between listing pages and between each "
                             f"hackathon page (default: {PAGE_DELAY})")
    args = parser.parse_args(argv)

    items, stats = load_pages(delay=args.delay)
    entries = build_entries(items)
    stats.update(attach_eligibility(entries, delay=args.delay))

    write_json_doc(entries, stats, args.out_dir / "hackathons.json")
    write_html_doc(entries, stats, args.out_dir / "index.html")
    print(f"Wrote {args.out_dir / 'hackathons.json'} and {args.out_dir / 'index.html'}")

    print_report(entries, stats, args.top)
    return 0 if stats.get("complete", True) else 1


if __name__ == "__main__":
    sys.exit(main())
