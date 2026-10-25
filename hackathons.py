#!/usr/bin/env python3
"""small-hackathon-finder: a free daily list of small, low-competition, cash-prize
hackathons that are still open on Devpost.

Fetches every page of Devpost's public JSON listing of open hackathons,
keeps the ones that are open, not invite-only, and offer at least one cash
prize worth more than zero, ranks them by prize money per registered
participant (a rough proxy for "realistic chance of winning"), and writes:

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
    "(+https://github.com/Mzee04/small-hackathon-finder; open-source, runs once daily)"
)
REQUEST_TIMEOUT = 30          # seconds per HTTP request
PAGE_DELAY = 1.5              # seconds between pages (be polite)
MAX_RETRIES = 3               # attempts per page
RETRY_BACKOFF = 2.0           # seconds, doubled after each failed attempt
PER_PAGE = 9                  # Devpost page size, used only as a sanity limit
MAX_PAGES = 500               # hard safety stop for the pagination loop

LIMITS_NOTE = (
    "Honest limits: this list only covers hackathons listed on Devpost. "
    "Registration counts are not submissions - most registered teams never "
    "submit, so real odds are usually better than the numbers suggest, but "
    "the ratio is only a heuristic, not a prediction. Prize amounts are "
    "self-reported by organisers and are not verified, may be split across "
    "many tracks or teams, and currencies are shown as listed without "
    "conversion. Always read each hackathon's rules page for eligibility, "
    "team-size limits and exact prize conditions before entering."
)

_TAG_RE = re.compile(r"<[^>]+>")
_PRIZE_RE = re.compile(r"^\s*([^\d]*?)\s*([\d][\d,]*(?:\.\d+)?)\s*$")


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
    Currency symbols are kept as-is; nothing is converted between currencies.
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


def build_entry(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a Devpost listing item into our record, or None if filtered out.

    Keeps only hackathons that are open, not invite-only, offer at least one
    cash prize, and have a parseable prize amount above zero.
    """
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

    registrants = item.get("registrations_count")
    registrants = registrants if isinstance(registrants, int) and registrants >= 0 else 0
    location = (item.get("displayed_location") or {}).get("location") or ""
    themes = [t.get("name") for t in (item.get("themes") or []) if t.get("name")]
    return {
        "title": item.get("title") or "Untitled hackathon",
        "url": item.get("url") or "",
        "prize_amount": amount,
        "prize_currency": symbol,
        "prize_display": _TAG_RE.sub("", html_module.unescape(str(item.get("prize_amount")))).strip(),
        "cash_prizes": prizes_counts["cash"],
        "registrations_count": registrants,
        # A hackathon with zero registrants so far still counts as low
        # competition, so it is ranked as if exactly one person registered.
        "prize_per_registrant": round(amount / max(registrants, 1), 2),
        "time_left_to_submission": item.get("time_left_to_submission") or "",
        "submission_period_dates": item.get("submission_period_dates") or "",
        "location": location,
        "is_online": location.strip().lower() == "online",
        "themes": themes,
        "organization_name": item.get("organization_name") or "",
    }


def build_entries(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter and rank a list of raw Devpost items by prize per registrant."""
    entries = [entry for item in items if (entry := build_entry(item)) is not None]
    entries.sort(key=lambda e: (-e["prize_per_registrant"], -e["prize_amount"], e["title"]))
    return entries


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_doc(entries: List[Dict[str, Any]], stats: Dict[str, Any], path: Path) -> None:
    doc = {
        "last_updated_utc": utc_now_iso(),
        "source": "https://devpost.com/api/hackathons?status[]=open",
        "ranking": "prize_amount / registrations_count (registrations are not submissions)",
        "note": LIMITS_NOTE,
        "stats": stats,
        "hackathons": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _esc(value: Any) -> str:
    return html_module.escape(str(value), quote=True)


def write_html_doc(entries: List[Dict[str, Any]], stats: Dict[str, Any], path: Path) -> None:
    updated = utc_now_iso()
    rows = []
    for i, e in enumerate(entries, start=1):
        where = "Online" if e["is_online"] else _esc(e["location"] or "See site")
        themes = ", ".join(e["themes"][:3]) + ("..." if len(e["themes"]) > 3 else "")
        rows.append(
            "<tr>"
            f"<td class='rank'>{i}</td>"
            f"<td class='title'><a href='{_esc(e['url'])}' target='_blank' rel='noopener noreferrer'>{_esc(e['title'])}</a>"
            f"<div class='sub'>{_esc(themes)}</div></td>"
            f"<td class='num'>{_esc(e['prize_display'])}<div class='sub'>{e['cash_prizes']} cash prize{'s' if e['cash_prizes'] != 1 else ''}</div></td>"
            f"<td class='num'>{e['registrations_count']:,}<div class='sub'>{_esc(e['prize_currency'])}{e['prize_per_registrant']:,.0f} / registrant</div></td>"
            f"<td>{_esc(e['time_left_to_submission'] or '-')}</td>"
            f"<td class='where'>{'🌐' if e['is_online'] else '📍'} {where}</td>"
            "</tr>"
        )
    total = stats.get("total_count")
    fetched = stats.get("fetched")
    coverage = f"{fetched} of {total if total is not None else '?'} open hackathons listed on Devpost"
    if not stats.get("complete", True):
        coverage += " (partial fetch - a page failed, see hackathons.json)"

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
  a {{ color: #0969da; }}
  @media (prefers-color-scheme: dark) {{ a {{ color: #4da3ff; }} }}
  footer {{ margin-top: 1.5rem; font-size: .8rem; color: #888; }}
</style>
</head>
<body>
<h1>🏆 Small Hackathon Finder</h1>
<p class="updated">Open hackathons with cash prizes, ranked by prize money per registered participant
(fewer registrants &rarr; higher on the list). Updated daily around 06:00 UTC.</p>
<p class="updated">Last updated: <time>{_esc(updated)}</time> &middot; {_esc(coverage)}</p>

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
    print(
        f"Kept {len(entries)} hackathons (open, not invite-only, at least 1 cash "
        f"prize, prize amount above 0), ranked by prize per registrant."
    )
    if not entries:
        print("Nothing matched the filters.")
        return
    print(f"\nTop {min(top, len(entries))} by prize per registrant:\n")
    header = f"{'#':>3}  {'Prize':>12}  {'Reg':>6}  {'Per reg':>9}  {'Left':<14}  {'Online':<6}  Title"
    print(header)
    print("-" * len(header))
    for i, e in enumerate(entries[:top], start=1):
        left = e["time_left_to_submission"] or "-"
        online = "yes" if e["is_online"] else "no"
        title = e["title"]
        if len(title) > 48:
            title = title[:45] + "..."
        print(
            f"{i:>3}  {e['prize_display']:>12}  {e['registrations_count']:>6}  "
            f"{e['prize_currency']}{e['prize_per_registrant']:>8,.0f}  {left:<14.14}  {online:<6}  {title}"
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
                        help=f"seconds to sleep between page fetches (default: {PAGE_DELAY})")
    args = parser.parse_args(argv)

    items, stats = load_pages(delay=args.delay)
    entries = build_entries(items)

    write_json_doc(entries, stats, args.out_dir / "hackathons.json")
    write_html_doc(entries, stats, args.out_dir / "index.html")
    print(f"Wrote {args.out_dir / 'hackathons.json'} and {args.out_dir / 'index.html'}")

    print_report(entries, stats, args.top)
    return 0 if stats.get("complete", True) else 1


if __name__ == "__main__":
    sys.exit(main())
