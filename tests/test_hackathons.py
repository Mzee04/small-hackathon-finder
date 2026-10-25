"""Tests for hackathons.py - all offline, using saved sample JSON."""

import json
import sys
from pathlib import Path

import pytest

import hackathons

HERE = Path(__file__).resolve().parent
SAMPLE_PAGE_PATH = HERE / "data" / "devpost_open_page1.json"


@pytest.fixture(scope="module")
def sample_page() -> dict:
    return json.loads(SAMPLE_PAGE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Prize parsing
# ---------------------------------------------------------------------------

class TestParsePrize:
    def test_dollar_with_span(self):
        assert hackathons.parse_prize("$<span data-currency-value>1,500</span>") == ("$", 1500.0)

    def test_large_amount_with_commas(self):
        assert hackathons.parse_prize("$<span data-currency-value>1,000,000</span>") == ("$", 1000000.0)

    def test_indian_rupee(self):
        assert hackathons.parse_prize("\u20b9<span data-currency-value>7,00,000</span>") == ("\u20b9", 700000.0)

    def test_pound(self):
        assert hackathons.parse_prize("\u00a3<span data-currency-value>10,000</span>") == ("\u00a3", 10000.0)

    def test_cad_prefixed_dollar(self):
        # Devpost sometimes writes "$CAD <span...>5,000</span>"
        assert hackathons.parse_prize("$CAD <span data-currency-value>5,000</span>") == ("$CAD", 5000.0)

    def test_decimal_amount(self):
        assert hackathons.parse_prize("$<span data-currency-value>2,500.50</span>") == ("$", 2500.50)

    def test_zero_amount(self):
        assert hackathons.parse_prize("$<span data-currency-value>0</span>") == ("$", 0.0)

    def test_empty_and_none(self):
        assert hackathons.parse_prize("") is None
        assert hackathons.parse_prize(None) is None

    def test_unparseable_text(self):
        assert hackathons.parse_prize("Prizes available") is None
        assert hackathons.parse_prize("<span></span>") is None

    def test_no_conversion_happens(self):
        # currencies are kept as-is; a rupee stays a rupee, never converted
        symbol, amount = hackathons.parse_prize("\u20b9<span data-currency-value>1,00,000</span>")
        assert symbol == "\u20b9"
        assert amount == 100000.0


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def make_item(**overrides) -> dict:
    """A minimal, valid Devpost listing item (open, public, cash prize)."""
    item = {
        "id": 1,
        "title": "Test Hackathon",
        "url": "https://example.devpost.com/",
        "open_state": "open",
        "invite_only": False,
        "prize_amount": "$<span data-currency-value>3,000</span>",
        "prizes_counts": {"cash": 2, "other": 0},
        "registrations_count": 100,
        "time_left_to_submission": "10 days left",
        "submission_period_dates": "Oct 01 - Nov 05, 2026",
        "displayed_location": {"location": "Online", "icon": "globe"},
        "themes": [{"id": 6, "name": "Machine Learning/AI"}],
        "organization_name": "acme",
    }
    item.update(overrides)
    return item


class TestBuildEntry:
    def test_keeps_valid_item(self):
        entry = hackathons.build_entry(make_item())
        assert entry is not None
        assert entry["prize_amount"] == 3000.0
        assert entry["prize_currency"] == "$"
        assert entry["registrations_count"] == 100
        assert entry["prize_per_registrant"] == 30.0
        assert entry["is_online"] is True
        assert entry["cash_prizes"] == 2
        assert entry["themes"] == ["Machine Learning/AI"]

    def test_drops_invite_only(self):
        assert hackathons.build_entry(make_item(invite_only=True)) is None

    def test_drops_not_open(self):
        assert hackathons.build_entry(make_item(open_state="closed")) is None

    def test_drops_zero_cash_prizes(self):
        assert hackathons.build_entry(make_item(prizes_counts={"cash": 0, "other": 3})) is None

    def test_drops_missing_cash_count(self):
        assert hackathons.build_entry(make_item(prizes_counts={"other": 3})) is None

    def test_drops_non_cash_only(self):
        # cash key absent but "other" prizes exist -> still dropped
        assert hackathons.build_entry(make_item(prizes_counts={"other": 5})) is None

    def test_drops_zero_prize_amount(self):
        assert hackathons.build_entry(make_item(prize_amount="$<span data-currency-value>0</span>")) is None

    def test_drops_unparseable_prize(self):
        assert hackathons.build_entry(make_item(prize_amount="Swag and glory")) is None

    def test_drops_missing_prize(self):
        assert hackathons.build_entry(make_item(prize_amount=None)) is None

    def test_in_person_location(self):
        entry = hackathons.build_entry(make_item(displayed_location={"location": "New York, NY"}))
        assert entry["is_online"] is False
        assert entry["location"] == "New York, NY"

    def test_zero_registrants_counted_as_one_for_ratio(self):
        entry = hackathons.build_entry(make_item(registrations_count=0))
        assert entry["registrations_count"] == 0
        assert entry["prize_per_registrant"] == 3000.0

    def test_negative_registrations_treated_as_zero(self):
        entry = hackathons.build_entry(make_item(registrations_count=-5))
        assert entry["registrations_count"] == 0


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

class TestRanking:
    def test_orders_by_prize_per_registrant(self):
        low_value = make_item(title="Low ratio", registrations_count=1000)      # 3.0/reg
        high_value = make_item(title="High ratio", registrations_count=10)      # 300/reg
        ranked = hackathons.build_entries([low_value, high_value])
        assert [e["title"] for e in ranked] == ["High ratio", "Low ratio"]

    def test_same_ratio_breaks_on_bigger_prize(self):
        big = make_item(title="Big prize", prize_amount="$<span data-currency-value>30,000</span>")
        small = make_item(title="Small prize")  # 3,000 / 100 = same 30/reg
        ranked = hackathons.build_entries([small, big])
        assert [e["title"] for e in ranked] == ["Big prize", "Small prize"]

    def test_filters_before_ranking(self):
        ranked = hackathons.build_entries([
            make_item(title="Invite only", invite_only=True),
            make_item(title="No cash", prizes_counts={"cash": 0, "other": 2}),
            make_item(title="Keeper"),
        ])
        assert [e["title"] for e in ranked] == ["Keeper"]


# ---------------------------------------------------------------------------
# Pagination (offline, using saved sample JSON)
# ---------------------------------------------------------------------------

class TestLoadPages:
    def test_paginates_until_empty_page(self, sample_page):
        pages = {1: sample_page, 2: {"hackathons": [], "meta": sample_page["meta"]}}
        items, stats = hackathons.load_pages(
            fetch_page=lambda page: pages[page], delay=0
        )
        assert len(items) == 9
        assert stats["fetched"] == 9
        assert stats["total_count"] == 75
        assert stats["complete"] is True
        assert stats["pages_fetched"] == 2

    def test_stops_after_partial_page(self, sample_page):
        # a page with fewer than PER_PAGE items is the last one - no extra request
        last = {"hackathons": sample_page["hackathons"][:2], "meta": sample_page["meta"]}
        pages = {1: sample_page, 2: last}
        calls = []

        def fake_page(page):
            calls.append(page)
            return pages[page]

        items, stats = hackathons.load_pages(fetch_page=fake_page, delay=0)
        assert len(items) == 11
        assert stats["pages_fetched"] == 2
        assert calls == [1, 2]  # did not ask for page 3

    def test_reports_partial_on_error(self, sample_page):
        def failing_page(page):
            if page == 1:
                return sample_page
            raise OSError("network down")

        items, stats = hackathons.load_pages(fetch_page=failing_page, delay=0)
        assert len(items) == 9
        assert stats["complete"] is False
        assert "network down" in stats["error"]

    def test_empty_first_page(self):
        items, stats = hackathons.load_pages(
            fetch_page=lambda page: {"hackathons": [], "meta": {"total_count": 0}}, delay=0
        )
        assert items == []
        assert stats["fetched"] == 0
        assert stats["total_count"] == 0
        assert stats["complete"] is True


# ---------------------------------------------------------------------------
# End-to-end over saved sample data (still no network)
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_sample_page_yards_entries(self, sample_page):
        entries = hackathons.build_entries(sample_page["hackathons"])
        assert entries, "page 1 of real data should contain at least one keeper"
        for e in entries:
            assert e["prize_amount"] > 0
            assert e["cash_prizes"] >= 1
            assert e["url"].startswith("https://")
        ratios = [e["prize_per_registrant"] for e in entries]
        assert ratios == sorted(ratios, reverse=True)

    def test_write_docs(self, sample_page, tmp_path):
        stats = {"total_count": 75, "fetched": 9, "pages_fetched": 1, "complete": True}
        entries = hackathons.build_entries(sample_page["hackathons"])

        json_path = tmp_path / "hackathons.json"
        html_path = tmp_path / "index.html"
        hackathons.write_json_doc(entries, stats, json_path)
        hackathons.write_html_doc(entries, stats, html_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["last_updated_utc"].endswith("Z")
        assert len(data["last_updated_utc"]) == 20  # YYYY-MM-DDTHH:MM:SSZ
        assert "Devpost" in data["note"]
        assert "registration" in data["note"].lower()  # registrants != submissions disclaimer
        assert data["stats"]["fetched"] == 9
        assert data["hackathons"] == entries

        html = html_path.read_text(encoding="utf-8")
        assert 'name="viewport"' in html  # mobile-friendly
        for e in entries:
            assert e["title"] in html
            assert e["url"] in html
        assert "Last updated" in html
        assert "honest limits" in html.lower()

    def test_html_escapes_titles(self, tmp_path):
        nasty = make_item(title='Evil <script>alert(1)</script>')
        entries = hackathons.build_entries([nasty])
        hackathons.write_html_doc(entries, {"total_count": 1, "fetched": 1, "complete": True},
                                   tmp_path / "index.html")
        html = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Politeness
# ---------------------------------------------------------------------------

class TestPoliteness:
    def test_user_agent_identifies_tool(self):
        assert "small-hackathon-finder" in hackathons.USER_AGENT
        assert "github.com/Mzee04/small-hackathon-finder" in hackathons.USER_AGENT

    def test_delay_used_between_pages(self, sample_page):
        sleeps = []
        pages = {1: sample_page, 2: {"hackathons": [], "meta": sample_page["meta"]}}

        def fake_sleep(seconds):
            sleeps.append(seconds)

        original_sleep = hackathons.time.sleep
        hackathons.time.sleep = fake_sleep
        try:
            hackathons.load_pages(fetch_page=lambda page: pages[page], delay=1.5)
        finally:
            hackathons.time.sleep = original_sleep
        assert sleeps and all(s == 1.5 for s in sleeps)

    def test_retries_on_transient_error(self, sample_page, monkeypatch):
        """fetch_json retries transient network failures before giving up."""
        calls = {"count": 0}
        sleeps = []
        monkeypatch.setattr(hackathons.time, "sleep", lambda s: sleeps.append(s))

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout):
            calls["count"] += 1
            if calls["count"] < 3:
                raise OSError("flaky")
            return FakeResponse(sample_page)

        monkeypatch.setattr(hackathons, "urlopen", fake_urlopen)
        data = hackathons.fetch_json(hackathons.API_URL_TEMPLATE.format(page=1))
        assert data["meta"]["total_count"] == 75
        assert calls["count"] == 3  # failed twice, succeeded on the third try
        assert sleeps  # backed off between attempts

    def test_fetch_json_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr(hackathons.time, "sleep", lambda s: None)
        monkeypatch.setattr(hackathons, "urlopen",
                            lambda request, timeout: (_ for _ in ()).throw(OSError("down")))
        with pytest.raises(OSError):
            hackathons.fetch_json(hackathons.API_URL_TEMPLATE.format(page=1))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
