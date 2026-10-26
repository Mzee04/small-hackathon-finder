# Small Hackathon Finder

A free daily list of small, low-competition, cash-prize hackathons that are
still open — for developers who want a realistic chance of winning.

Every day a GitHub Actions workflow pulls the public listing of open
hackathons from [Devpost](https://devpost.com/hackathons), keeps the ones
that are open to everyone and offer at least one cash prize, and ranks them
by **prize money per registered participant** (in approximate US dollars) — a rough "less competition"
signal. A hackathon with a decent prize and only a few dozen registrants
sits at the top; a $100k prize with 30,000 registrants sinks to the bottom.

**Live page:** https://mzee04.github.io/small-hackathon-finder/

## What it does

1. Fetches every page of
   `https://devpost.com/api/hackathons?status[]=open` politely
   (identifying User-Agent, short delay between pages, retries with backoff),
   and reports how many hackathons it got versus `meta.total_count`.
2. Keeps only hackathons that are:
   - open (`open_state` is `open`),
   - **not** invite-only,
   - offering at least one cash prize (`prizes_counts.cash >= 1`),
   - with a prize amount that parses to a number above 0.
   Prize amounts arrive as HTML like `$<span data-currency-value>1,500</span>`;
   they are parsed to a number and the currency symbol is kept as-is.
3. Converts each prize to **approximate US dollars** using a small,
   hard-coded table of exchange rates in `hackathons.py` (`UNITS_PER_USD`,
   dated by `FX_RATES_DATE`), so ₹200,000 (about $2,100) does not rank
   above $45,000. No live rates and no network call: the rates are rough
   and dated, good enough for ordering, not for money decisions. A prize in
   a currency the table doesn't know (or an ambiguous symbol like ¥) keeps
   its raw value, is marked "currency not converted", and is always listed
   after every converted prize.
4. Ranks by approximate USD prize per registrant and writes:
   - `docs/index.html` — a simple, mobile-friendly table with links,
   - `docs/hackathons.json` — the same data, machine-readable,
   - both stamped with a "last updated" UTC time.

The table shows the prize as listed (plus "≈ $…" for non-dollar prizes),
number of registrants, approximate USD per registrant, time left to submit,
and whether the hackathon is online. `hackathons.json` keeps the original
amount and currency next to `prize_usd_approx` and
`usd_per_registrant_approx`, and records the rate table and its date.

## How to run it

Python 3.8+ with the standard library only. No API keys, no dependencies.
(On Windows, type `python` instead of `python3`.)

```bash
git clone https://github.com/Mzee04/small-hackathon-finder.git
cd small-hackathon-finder
python3 hackathons.py            # fetch live data, write docs/, print top 10
python3 hackathons.py --top 20   # print a longer console table
```

It takes about 10 seconds (it waits 1.5 s between Devpost pages to be
polite). Note that it **overwrites** `docs/index.html` and
`docs/hackathons.json` in your copy of the repo. To try it without touching
`docs/`, write somewhere else:

```bash
python3 hackathons.py --out-dir /tmp/shf-out   # then open /tmp/shf-out/index.html
```

Other options: `--delay SECONDS` (pause between pages) and `--help`.

## How to use the list

Open the [live page](https://mzee04.github.io/small-hackathon-finder/) (or
your generated `index.html`). The best odds are at the top. Click a title
to open that hackathon on Devpost, then read its rules before entering.
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

## Daily updates

[`.github/workflows/update.yml`](.github/workflows/update.yml) runs every day
at 06:00 UTC (and on manual trigger from the Actions tab). It runs the
tests, regenerates `docs/`, and commits any changed files to `main` using
`permissions: contents: write`. GitHub Pages serves `docs/` at the live page
above (enable Pages → Deploy from a branch → `main` / `docs` if it is not
already on).

## Honest limits

- **Devpost only.** Many hackathons live elsewhere; this list will never
  claim to cover them.
- **Registrants are not submissions.** Most people who register never
  submit a project, so real odds are usually better than the raw numbers
  suggest — but prize-per-registrant is a heuristic, not a prediction.
- **Prizes are not verified.** Amounts are self-reported by organisers, may
  be split across many tracks or teams, and currencies are shown exactly as
  Devpost lists them. The US-dollar values used for ranking come from
  approximate, hard-coded exchange rates (see `FX_RATES_DATE` in
  `hackathons.py`) that are not updated automatically, so treat them as
  rough. Prizes in a currency without a known rate are listed last.
- **Check the rules.** Always read each hackathon's rules page for
  eligibility, team size, residency restrictions and exact prize conditions
  before entering.

## License

MIT (see [LICENSE](LICENSE)) — do whatever you like, attribution appreciated.
