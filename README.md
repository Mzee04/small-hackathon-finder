# Small Hackathon Finder

A free daily list of small, low-competition, cash-prize hackathons that are
still open — for developers who want a realistic chance of winning.

Every day a GitHub Actions workflow pulls the public listing of open
hackathons from [Devpost](https://devpost.com/hackathons), keeps the ones
that are open to everyone and offer at least one cash prize, and ranks them
by **prize money per registered participant** — a rough "less competition"
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
   they are parsed to a number and the currency symbol is kept as-is
   (no currency conversion).
3. Ranks by prize per registrant and writes:
   - `docs/index.html` — a simple, mobile-friendly table with links,
   - `docs/hackathons.json` — the same data, machine-readable,
   - both stamped with a "last updated" UTC time.

The table shows prize, number of registrants, time left to submit, and
whether the hackathon is online.

## How to run it

Python 3.8+ with the standard library only. No API keys, no dependencies.

```bash
python3 hackathons.py            # fetch live data, write docs/, print top 10
python3 hackathons.py --top 20   # print a longer console table
```

Run the tests (offline — they use a saved copy of a real Devpost API page,
never the network):

```bash
python3 -m pip install pytest    # only needed for tests, not for the tool
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
  Devpost lists them, with no conversion.
- **Check the rules.** Always read each hackathon's rules page for
  eligibility, team size, residency restrictions and exact prize conditions
  before entering.

## License

MIT — do whatever you like, attribution appreciated.
