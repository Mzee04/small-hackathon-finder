# Small Hackathon Finder

A free list of small, low-competition, cash-prize hackathons and online
contests that are still open — for developers who want a realistic chance
of winning. Refreshed twice a day (GitHub may run it a few hours late).

Twice a day a GitHub Actions workflow pulls public listings from
[Devpost](https://devpost.com/hackathons), [DEV challenges](https://dev.to/challenges),
[Superteam Earn](https://earn.superteam.fun) (global bounties only), and
[MLH](https://www.mlh.com/events) online events. DoraHacks has a public
hackathon API too; when that site answers with a human-verification page,
that source is skipped and the others are still published. It keeps contests
that are still before their deadline and offer a cash or crypto prize, and
ranks them by **prize money per registered participant** (in approximate US
dollars) — a rough "less competition" signal. A hackathon with a decent
prize and only a few dozen registrants sits at the top; a $100k prize with
30,000 registrants sinks to the bottom. Each row names the platform it came
from.

**Live page:** https://mzee04.github.io/small-hackathon-finder/

## What it does

1. Fetches every source on its own, politely (identifying User-Agent, short
   delay between pages, retries with backoff). If one source errors or is
   blocked, the build still writes the others and prints a warning.
   - **Devpost:** every page of
     `https://devpost.com/api/hackathons?status[]=open`, then each kept
     hackathon page for the **Who can participate** sidebar. A page that
     cannot be read leaves that row visible with no eligibility badge.
     The report says how many hackathons it got versus `meta.total_count`.
   - **DEV:** the public challenges page (there is no challenges JSON API).
     Only cards marked Live are opened. Prize boxes such as `$250 USD` are
     added up.
   - **Superteam Earn:** `https://earn.superteam.fun/api/listings?status=open`,
     then each listing's public detail. Only `region: Global` is kept.
     Submission counts are used as the crowd number.
   - **MLH:** the public season pages (the event data is JSON inside the
     page). Online events only; prize text comes from each event's prizes
     page. Swag-only events are left out.
   - **DoraHacks:** `https://dorahacks.io/api/v1/hub/hackathons` for ongoing
     and upcoming hackathons. If the response is a human-verification page,
     the source is skipped.
2. On Devpost, keeps only hackathons that are:
   - open (`open_state` is `open`),
   - still before the submission end date (Devpost's
     `submission_period_dates`, stored as the last second of that UTC day),
   - **not** invite-only,
   - offering at least one cash prize (`prizes_counts.cash >= 1`),
   - with a prize amount that parses to a number above 0
     (cash, or a crypto amount the feed actually states).
   Listings from DEV, Superteam, MLH, and DoraHacks are also dropped when
   their own text says the contest is students-only or charges an entry fee.
   Devpost still only *hides* a closed group or a required team until you
   tick the checkbox; it does not delete those rows.
   Prize amounts arrive as HTML like `$<span data-currency-value>1,500</span>`;
   they are parsed to a number and the currency symbol is kept as-is.
3. Converts each prize to **approximate US dollars** using a small,
   hard-coded table of exchange rates in `hackathons.py` (`UNITS_PER_USD`,
   dated by `FX_RATES_DATE`), so ₹200,000 (about $2,100) does not rank
   above $45,000. USD-pegged stablecoins (USDC, USDT, USDG, DAI, PYUSD)
   count as one dollar each. No live rates and no network call: the rates are rough
   and dated, good enough for ordering, not for money decisions. A prize in
   a currency the table doesn't know (or an ambiguous symbol like ¥) keeps
   its raw value, is marked "currency not converted", and is always listed
   after every converted prize.
4. Flags suspicious dollar amounts instead of letting them jump the queue.
   Indian-style digit grouping (`1,55,000`, `1,00,000`) on a non-rupee
   prize, or a very large dollar prize at a college or university with very
   few registrants, gets a **verify currency** badge. Those rows are ranked
   below the others, using a conservative rupee reading of the same number.
   The listed amount and the normal exchange-rate conversion are still stored.
5. Marks **includes non-cash prizes** when the prize text mentions credits,
   bitcoin/BTC/crypto/tokens, subscriptions, swag, or goodies.
6. Records who can enter, when the sidebar says so: students only, high
   school only, a required team (and its minimum size), professionals,
   partners or another closed group, a country list tighter than Devpost's
   usual sanctions exceptions, and a clear hardware requirement (a physical
   robot, microcontroller, or similar). These are new fields on each
   hackathon in `hackathons.json`. They do not change the older fields.
7. Ranks by approximate USD prize per registrant and writes:
   - `docs/index.html` — a simple, mobile-friendly table with links,
   - `docs/hackathons.json` — the same data, machine-readable,
   - both stamped with a "last updated" UTC time.

The table shows the prize as listed (plus "≈ $…" for non-dollar prizes),
number of registrants, approximate USD per registrant, the submission
deadline, whether the hackathon is online, and which platform the row came
from. Cards also show badges such
as **Students only**, **High school only**, **Team required (2+)**,
**Restricted**, **Country-limited**, and **Needs hardware** when those
apply. Time left is counted in the browser from that deadline, and a row
hides itself once the deadline has passed. The header says how many rows
are shown and how many were skipped.
`hackathons.json` keeps the original amount and currency next to
`prize_usd_approx` and `usd_per_registrant_approx`, plus
`submission_deadline_utc`, and records the rate table and its date.

## How to run it

Python 3.8+ with the standard library only. No API keys, no dependencies.
(On Windows, type `python` instead of `python3`.)

```bash
git clone https://github.com/Mzee04/small-hackathon-finder.git
cd small-hackathon-finder
python3 hackathons.py            # fetch live data, write docs/, print top 10
python3 hackathons.py --top 20   # print a longer console table
```

It takes a couple of minutes (it waits 1.5 s between Devpost listing pages,
and again before each hackathon page, to be polite). Note that it
**overwrites** `docs/index.html` and
`docs/hackathons.json` in your copy of the repo. To try it without touching
`docs/`, write somewhere else:

```bash
python3 hackathons.py --out-dir /tmp/shf-out   # then open /tmp/shf-out/index.html
```

Other options: `--delay SECONDS` (pause between pages) and `--help`.

## How to use the list

Open the [live page](https://mzee04.github.io/small-hackathon-finder/) (or
your generated `index.html`). The best odds are at the top. Only online
hackathons are listed until you tick **Show in-person events too**. Listings
limited to students, high school, professionals, partners, or a required
team are hidden until you tick **Show restricted / team-only**. Solo-friendly
listings that are open to anyone stay in the default view. A country limit
or a hardware need is badged and still shown. If eligibility could not be
read, there is no badge and the row stays visible. An event Devpost marks
as online, including one that only lists a host city, shows as Online.
Click a title to open that contest on its own site, then read its rules before
entering. The platform name under the title says whether the link goes to
Devpost, DEV, Superteam Earn, MLH, or DoraHacks.
For scripts, use [`hackathons.json`](https://mzee04.github.io/small-hackathon-finder/hackathons.json).

## Run the tests

The tests are offline: they use a saved copy of a real Devpost API page and
never touch the network. Use a virtual environment, because many systems
(Homebrew Python on macOS, recent Debian/Ubuntu) refuse a plain
`pip install` with an "externally-managed-environment" error:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

## Updates

[`.github/workflows/update.yml`](.github/workflows/update.yml) runs twice a
day, at 06:00 and 18:00 UTC (and on manual trigger from the Actions tab).
GitHub may run it a few hours late. It runs the tests, regenerates `docs/`,
and commits any changed files to `main` using `permissions: contents: write`.
GitHub Pages serves `docs/` at the live page above (enable Pages → Deploy
from a branch → `main` / `docs` if it is not already on).

## Honest limits

- **A handful of sources, not every hackathon site.** Devpost, DEV
  challenges, global Superteam Earn bounties, and online MLH events are
  included. DoraHacks is included only when its public API answers; a
  human-verification page skips that source for the run. Plenty of
  contests live somewhere else and will not appear.
- **Registrants are not submissions.** Most people who register never
  submit a project, so real odds are usually better than the raw numbers
  suggest — but prize-per-registrant is a heuristic, not a prediction.
- **Prizes are not verified.** Amounts are self-reported by organisers and
  may be split across many tracks or teams. Each amount is shown as listed
  and converted to approximate US dollars with hard-coded exchange rates
  (see `FX_RATES_DATE` in `hackathons.py`) that are not updated
  automatically. That dollar figure is what the ranking uses, and it is
  shown next to non-dollar prizes. A **verify currency** badge means the
  figure looks like rupees mislabeled as dollars; those rows are ranked
  below the others. Prizes in a currency without a known rate are listed
  after the converted ones. **Includes non-cash prizes** means the prize
  text mentions credits, bitcoin, crypto, tokens, subscriptions, swag, or
  goodies.
- **Deadlines are dates.** Devpost's listing gives a submission end date,
  not a clock time. This tool treats that as the end of the UTC day. The
  page counts down from it and hides the row when the moment has passed.
- **Eligibility is the sidebar, not the full rules.** Badges come from
  Devpost's "Who can participate" list, plus an obvious hardware line when
  the page requires a physical robot or similar. Devpost's usual sanctions
  exceptions are not marked country-limited. A page that fails to load is
  left visible with no badge. Always read the rules before entering:
  eligibility, team size, residency, and exact prize conditions can still
  be stricter than the sidebar.

## License

MIT (see [LICENSE](LICENSE)) — do whatever you like, attribution appreciated.
