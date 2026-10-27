"""Tests for hackathons.py - all offline, using saved sample JSON."""

import json
import sys
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Currency conversion (approximate, offline)
# ---------------------------------------------------------------------------

INR = "\u20b9<span data-currency-value>{}</span>"
USD = "$<span data-currency-value>{}</span>"


class TestCurrencyConversion:
    def test_known_symbols_map_to_codes(self):
        assert hackathons.currency_code("$") == "USD"
        assert hackathons.currency_code("\u20b9") == "INR"
        assert hackathons.currency_code("\u00a3") == "GBP"
        assert hackathons.currency_code("\u20ac") == "EUR"
        assert hackathons.currency_code("$CAD") == "CAD"
        assert hackathons.currency_code("usd") == "USD"      # case-insensitive
        assert hackathons.currency_code(" Rs. ") == "INR"    # spaces ignored

    def test_unknown_or_ambiguous_symbols(self):
        assert hackathons.currency_code("") is None
        assert hackathons.currency_code("\u00a5") is None    # yen or yuan: ambiguous
        assert hackathons.currency_code("XYZ") is None
        assert hackathons.to_usd(1000, "XYZ") is None

    def test_every_alias_has_a_rate(self):
        for code in hackathons.CURRENCY_ALIASES.values():
            assert code in hackathons.UNITS_PER_USD
        assert hackathons.UNITS_PER_USD["USD"] == 1.0
        assert all(rate > 0 for rate in hackathons.UNITS_PER_USD.values())

    def test_usd_is_unchanged(self):
        assert hackathons.to_usd(45000, "$") == 45000

    def test_inr_converts_to_roughly_two_thousand_dollars(self):
        usd = hackathons.to_usd(200000, "\u20b9")
        assert 1500 < usd < 3000
        assert usd == pytest.approx(200000 / hackathons.UNITS_PER_USD["INR"])

    def test_entry_carries_original_and_usd_values(self):
        entry = hackathons.build_entry(make_item(prize_amount=INR.format("2,00,000"), registrations_count=10))
        assert entry["prize_currency"] == "\u20b9"
        assert entry["prize_amount"] == 200000.0
        assert entry["prize_per_registrant"] == 20000.0       # raw, original currency
        assert entry["prize_currency_code"] == "INR"
        assert entry["currency_known"] is True
        assert entry["prize_usd_approx"] == pytest.approx(200000 / hackathons.UNITS_PER_USD["INR"], abs=0.01)
        assert entry["usd_per_registrant_approx"] == pytest.approx(entry["prize_usd_approx"] / 10, abs=0.01)

    def test_unknown_currency_entry_is_flagged(self):
        entry = hackathons.build_entry(make_item(prize_amount="XYZ <span data-currency-value>5,000</span>"))
        assert entry is not None
        assert entry["currency_known"] is False
        assert entry["prize_currency_code"] is None
        assert entry["prize_usd_approx"] is None
        assert entry["usd_per_registrant_approx"] is None


class TestCurrencyRanking:
    def test_inr_200k_ranks_below_usd_45k_with_same_registrants(self):
        rupees = make_item(title="Rupees", prize_amount=INR.format("200,000"), registrations_count=19)
        dollars = make_item(title="Dollars", prize_amount=USD.format("45,000"), registrations_count=19)
        ranked = hackathons.build_entries([rupees, dollars])
        assert [e["title"] for e in ranked] == ["Dollars", "Rupees"]

    def test_real_world_case_from_live_data(self):
        # Seen on Devpost on 2026-10-26: a big raw rupee number with few
        # registrants used to beat a large dollar prize. In USD it should not.
        rupees = make_item(title="Rupees", prize_amount=INR.format("200,000"), registrations_count=10)
        dollars = make_item(title="Dollars", prize_amount=USD.format("45,000"), registrations_count=19)
        ranked = hackathons.build_entries([rupees, dollars])
        assert [e["title"] for e in ranked] == ["Dollars", "Rupees"]

    def test_inr_can_still_win_on_usd_per_registrant(self):
        rupees = make_item(title="Rupees", prize_amount=INR.format("500,000"), registrations_count=5)   # ~$1,050/reg
        dollars = make_item(title="Dollars", prize_amount=USD.format("10,000"), registrations_count=100)  # $100/reg
        ranked = hackathons.build_entries([dollars, rupees])
        assert [e["title"] for e in ranked] == ["Rupees", "Dollars"]

    def test_unknown_currency_never_ranks_above_known(self):
        unknown = make_item(title="Unknown", prize_amount="XYZ <span data-currency-value>9,000,000</span>",
                            registrations_count=1)
        tiny_usd = make_item(title="Tiny USD", prize_amount=USD.format("10"), registrations_count=5000)
        rupees = make_item(title="Rupees", prize_amount=INR.format("1,000"), registrations_count=1000)
        ranked = hackathons.build_entries([unknown, tiny_usd, rupees])
        assert ranked[-1]["title"] == "Unknown"
        assert {e["title"] for e in ranked[:2]} == {"Tiny USD", "Rupees"}

    def test_ranking_is_by_usd_per_registrant(self):
        items = [
            make_item(title="A", prize_amount=INR.format("200,000"), registrations_count=10),
            make_item(title="B", prize_amount=USD.format("45,000"), registrations_count=19),
            make_item(title="C", prize_amount="\u00a3<span data-currency-value>1,000</span>", registrations_count=2),
            make_item(title="D", prize_amount=USD.format("900"), registrations_count=19),
        ]
        ranked = hackathons.build_entries(items)
        ratios = [e["usd_per_registrant_approx"] for e in ranked]
        assert ratios == sorted(ratios, reverse=True)


class TestCurrencyOutput:
    def test_html_shows_original_and_approx_usd(self, tmp_path):
        entries = hackathons.build_entries([
            make_item(title="Rupees", prize_amount=INR.format("200,000"), registrations_count=10),
            make_item(title="Unknown", prize_amount="XYZ <span data-currency-value>5,000</span>"),
        ])
        out = tmp_path / "index.html"
        hackathons.write_html_doc(entries, {"total_count": 2, "fetched": 2, "complete": True}, out)
        html = out.read_text(encoding="utf-8")
        usd = round(200000 / hackathons.UNITS_PER_USD["INR"])
        assert "\u20b9 200,000" in html or "\u20b9200,000" in html
        assert f"&asymp; ${usd:,}" in html
        assert "currency not converted" in html
        assert hackathons.FX_RATES_DATE in html

    def test_json_doc_records_rates_and_date(self, tmp_path):
        out = tmp_path / "hackathons.json"
        hackathons.write_json_doc([], {"total_count": 0, "fetched": 0, "complete": True}, out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["fx_rates"]["as_of"] == hackathons.FX_RATES_DATE
        assert doc["fx_rates"]["units_per_usd"]["INR"] == hackathons.UNITS_PER_USD["INR"]

    def test_console_report_shows_usd_column(self, capsys):
        entries = hackathons.build_entries([
            make_item(title="Rupees", prize_amount=INR.format("200,000"), registrations_count=10),
        ])
        hackathons.print_report(entries, {"total_count": 1, "fetched": 1, "pages_fetched": 1, "complete": True}, 5)
        out = capsys.readouterr().out
        assert "~USD" in out
        assert "~$210" in out


# ---------------------------------------------------------------------------
# Submission deadlines
# ---------------------------------------------------------------------------

def utc(*parts) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


class TestSubmissionDeadline:
    def test_same_month_range(self):
        # Life After Code / Engage 6.0 style: "Oct 05 - 27, 2026"
        deadline = hackathons.parse_submission_deadline("Oct 05 - 27, 2026")
        assert deadline == utc(2026, 10, 27, 23, 59, 59)
        assert hackathons.format_deadline_display(deadline) == "Oct 27, 2026"

    def test_cross_month_range(self):
        assert hackathons.parse_submission_deadline("Aug 26 - Oct 30, 2026") == utc(2026, 10, 30, 23, 59, 59)
        assert hackathons.parse_submission_deadline("Oct 27 - 28, 2026") == utc(2026, 10, 28, 23, 59, 59)
        assert hackathons.parse_submission_deadline("Sep 24 - Nov 18, 2026") == utc(2026, 11, 18, 23, 59, 59)

    def test_year_rollover(self):
        assert hackathons.parse_submission_deadline("Dec 20, 2026 - Jan 05, 2027") == utc(2027, 1, 5, 23, 59, 59)
        assert hackathons.parse_submission_deadline("Dec 28 - Jan 05, 2027") == utc(2027, 1, 5, 23, 59, 59)
        assert hackathons.parse_submission_deadline("Dec 28, 2026 - Jan 05") == utc(2027, 1, 5, 23, 59, 59)

    def test_unparseable(self):
        assert hackathons.parse_submission_deadline("") is None
        assert hackathons.parse_submission_deadline(None) is None
        assert hackathons.parse_submission_deadline("soon") is None
        assert hackathons.parse_submission_deadline("Oct 05 - 27") is None  # no year

    def test_invalid_calendar_day(self):
        assert hackathons.parse_submission_deadline("Feb 01 - 31, 2026") is None

    def test_keeps_hackathon_on_its_last_day(self):
        item = make_item(
            title="Life After Code",
            time_left_to_submission="about 2 hours left",
            submission_period_dates="Oct 05 - 27, 2026",
        )
        morning = utc(2026, 10, 27, 11, 16, 14)
        entry = hackathons.build_entry(item, now=morning)
        assert entry is not None
        assert entry["submission_deadline_utc"] == "2026-10-27T23:59:59Z"
        assert entry["submission_deadline_display"] == "Oct 27, 2026"
        assert "about 2 hours left" == entry["time_left_to_submission"]

    def test_drops_once_end_date_has_passed_even_if_time_left_text_says_open(self):
        # The stale Devpost sentence is not the deadline. The end date is.
        for title, dates in (
            ("Life After Code", "Oct 05 - 27, 2026"),
            ("Engage 6.0 – Code with AI Hackathon", "Oct 21 - 27, 2026"),
        ):
            item = make_item(
                title=title,
                time_left_to_submission="about 2 hours left",
                submission_period_dates=dates,
            )
            assert hackathons.build_entry(item, now=utc(2026, 10, 28, 0, 0, 1)) is None

    def test_drops_at_the_deadline_instant(self):
        item = make_item(submission_period_dates="Oct 21 - 27, 2026")
        assert hackathons.build_entry(item, now=utc(2026, 10, 27, 23, 59, 59)) is None

    def test_missing_date_is_kept(self):
        entry = hackathons.build_entry(make_item(submission_period_dates=""))
        assert entry is not None
        assert entry["submission_deadline_utc"] is None

    def test_time_left_formatting(self):
        deadline = utc(2026, 10, 27, 23, 59, 59)
        assert hackathons.format_time_left(deadline, utc(2026, 10, 28, 0, 0)) == "ended"
        assert hackathons.format_time_left(deadline, utc(2026, 10, 27, 23, 59, 30)) == "less than a minute left"
        assert hackathons.format_time_left(deadline, utc(2026, 10, 27, 22, 0)) == "1 hour left"
        assert hackathons.format_time_left(deadline, utc(2026, 10, 27, 11, 16)) == "12 hours left"
        assert hackathons.format_time_left(deadline, utc(2026, 10, 26, 12, 0)) == "1 day left"
        assert hackathons.format_time_left(deadline, utc(2026, 10, 24, 15, 0)) == "3 days left"


# ---------------------------------------------------------------------------
# Suspicious currency and non-cash prizes
# ---------------------------------------------------------------------------

class TestCurrencyReview:
    def test_indian_grouping_examples(self):
        assert hackathons.has_indian_digit_grouping("1,55,000 total prize pool")
        assert hackathons.has_indian_digit_grouping("Rs 1,00,000")
        assert hackathons.has_indian_digit_grouping("7,00,000")
        assert hackathons.has_indian_digit_grouping("10,00,000")
        assert not hackathons.has_indian_digit_grouping("150,000")
        assert not hackathons.has_indian_digit_grouping("1,000,000")
        assert not hackathons.has_indian_digit_grouping("45,000")
        assert not hackathons.has_indian_digit_grouping("10,000")

    def test_lakh_style_dollar_amount_is_flagged(self):
        entry = hackathons.build_entry(make_item(
            title="Engage 6.0",
            prize_amount="$<span data-currency-value>1,55,000</span>",
            registrations_count=22,
            organization_name="The Northcap University",
            displayed_location={"location": "The NorthCap University"},
        ))
        assert entry["currency_needs_review"] is True
        assert "indian_digit_grouping" in entry["currency_review_reasons"]
        # Listed currency is still run through the fx table (here it is USD).
        assert entry["prize_currency_code"] == "USD"
        assert entry["prize_usd_approx"] == 155000.0
        assert entry["currency_known"] is True

    def test_one_lakh_grouping_is_flagged_without_a_college(self):
        entry = hackathons.build_entry(make_item(
            prize_amount="$<span data-currency-value>1,00,000</span>",
            registrations_count=5000,
            organization_name="Some Company",
            displayed_location={"location": "Online"},
        ))
        assert entry["currency_needs_review"] is True
        assert entry["currency_review_reasons"] == ["indian_digit_grouping"]

    def test_large_college_dollar_prize_with_few_registrants(self):
        sustain = hackathons.build_entry(make_item(
            title="SUSTAIN-A-THON 2.0",
            prize_amount=USD.format("150,000"),
            registrations_count=38,
            organization_name="Sharda University Greater Noida",
            displayed_location={"location": "Block 45, Sharda University, "},
        ))
        engage = hackathons.build_entry(make_item(
            title="Engage 6.0",
            prize_amount=USD.format("45,000"),
            registrations_count=22,
            organization_name="The Northcap University",
            displayed_location={"location": "The NorthCap University"},
        ))
        assert sustain["currency_needs_review"] is True
        assert "large_college_prize" in sustain["currency_review_reasons"]
        assert sustain["prize_usd_approx"] == 150000.0
        assert engage["currency_needs_review"] is True
        assert "large_college_prize" in engage["currency_review_reasons"]

    def test_real_rupee_symbol_is_not_flagged(self):
        entry = hackathons.build_entry(make_item(
            prize_amount=INR.format("1,00,000"),
            registrations_count=14,
            organization_name="VIT Bhopal University",
            displayed_location={"location": "VIT Bhopal University"},
        ))
        assert entry["currency_needs_review"] is False
        assert entry["prize_currency_code"] == "INR"
        assert entry["currency_review_reasons"] == []

    def test_large_dollar_prize_with_many_registrants_is_not_flagged(self):
        meta = hackathons.build_entry(make_item(
            title="Meta VR Start Developer Competition 2026",
            prize_amount=USD.format("1,000,000"),
            registrations_count=4169,
            organization_name="Meta",
            displayed_location={"location": "Online"},
        ))
        cal = hackathons.build_entry(make_item(
            title="Cal Hacks 13.0",
            prize_amount=USD.format("159,822"),
            registrations_count=1457,
            organization_name="Cal Hacks",
            displayed_location={"location": "Palace of Fine Arts"},
        ))
        assert meta["currency_needs_review"] is False
        assert cal["currency_needs_review"] is False

    def test_small_college_prize_is_not_flagged(self):
        entry = hackathons.build_entry(make_item(
            prize_amount=USD.format("500"),
            registrations_count=20,
            organization_name="State University",
            displayed_location={"location": "State University"},
        ))
        assert entry["currency_needs_review"] is False

    def test_boundaries(self):
        college = {
            "organization_name": "State University",
            "displayed_location": {"location": "State University"},
        }
        at_cap = hackathons.build_entry(make_item(
            registrations_count=hackathons.SUSPICIOUS_REGISTRANT_MAX,
            prize_amount=USD.format("20,000"),
            **college,
        ))
        one_more_person = hackathons.build_entry(make_item(
            registrations_count=hackathons.SUSPICIOUS_REGISTRANT_MAX + 1,
            prize_amount=USD.format("20,000"),
            **college,
        ))
        just_under = hackathons.build_entry(make_item(
            registrations_count=10,
            prize_amount=USD.format("19,999"),
            **college,
        ))
        assert at_cap["currency_needs_review"] is True
        assert one_more_person["currency_needs_review"] is False
        assert just_under["currency_needs_review"] is False

    def test_flagged_rows_rank_below_unflagged_ones(self):
        flagged = make_item(
            title="College",
            prize_amount=USD.format("150,000"),
            registrations_count=38,
            organization_name="Sharda University",
            displayed_location={"location": "Sharda University"},
        )
        tiny = make_item(title="Tiny", prize_amount=USD.format("30"), registrations_count=152)
        tiny_inr = make_item(
            title="Tiny INR",
            prize_amount=INR.format("1,000"),
            registrations_count=1000,
        )
        ranked = hackathons.build_entries([flagged, tiny, tiny_inr])
        assert ranked[-1]["title"] == "College"
        assert ranked[-1]["currency_needs_review"] is True
        # The fx conversion of the listed dollars is unchanged.
        assert ranked[-1]["prize_usd_approx"] == 150000.0

    def test_flagged_rows_are_ordered_by_a_rupee_reading(self):
        bigger = make_item(
            title="Bigger",
            prize_amount=USD.format("150,000"),
            registrations_count=38,
            organization_name="Sharda University",
        )
        smaller = make_item(
            title="Smaller",
            prize_amount=USD.format("45,000"),
            registrations_count=22,
            organization_name="NorthCap University",
        )
        ranked = hackathons.build_entries([smaller, bigger])
        assert [e["title"] for e in ranked] == ["Bigger", "Smaller"]
        # 150,000 INR / 38 people is more per person than 45,000 INR / 22.
        inr = hackathons.UNITS_PER_USD["INR"]
        assert 150000 / inr / 38 > 45000 / inr / 22

    def test_indian_grouped_amount_cannot_outrank_a_known_prize(self):
        suspicious = make_item(
            title="Grouped",
            prize_amount="$<span data-currency-value>1,55,000</span>",
            registrations_count=1,
        )
        ordinary = make_item(title="Ordinary", prize_amount=USD.format("100"), registrations_count=10)
        ranked = hackathons.build_entries([suspicious, ordinary])
        assert [e["title"] for e in ranked] == ["Ordinary", "Grouped"]


class TestNonCashPrizes:
    @pytest.mark.parametrize("word", [
        "credits", "credit", "Bitcoin", "BTC", "crypto", "cryptocurrency",
        "tokens", "token", "subscription", "subscriptions", "swag", "goodies",
    ])
    def test_keywords_set_the_flag(self, word):
        raw = f"$<span data-currency-value>5,000</span> plus {word}"
        entry = hackathons.build_entry(make_item(prize_amount=raw))
        assert entry is not None
        assert entry["prize_amount"] == 5000.0
        assert entry["includes_non_cash_prizes"] is True

    def test_plain_cash_is_not_flagged(self):
        entry = hackathons.build_entry(make_item())
        assert entry["includes_non_cash_prizes"] is False

    def test_keyword_inside_another_word_does_not_match(self):
        assert not hackathons.prize_text_includes_non_cash("accredited tokenizer swagger")


class TestPageCopy:
    def test_html_shows_live_deadline_badges_and_counts(self, tmp_path):
        entries = hackathons.build_entries([
            make_item(title="Ordinary", prize_amount=USD.format("100"), registrations_count=10),
            make_item(
                title="College",
                prize_amount=USD.format("150,000") + " plus goodies",
                registrations_count=38,
                organization_name="Sharda University",
                displayed_location={"location": "Sharda University"},
                submission_period_dates="Oct 27 - 28, 2026",
            ),
        ])
        stats = {"total_count": 5, "fetched": 5, "pages_fetched": 1, "complete": True}
        out = tmp_path / "index.html"
        hackathons.write_html_doc(entries, stats, out)
        html = out.read_text(encoding="utf-8")

        assert html.index("Ordinary") < html.index("College")
        assert "class='badge verify'>verify currency</span>" in html
        assert "class='badge noncash'>includes non-cash prizes</span>" in html
        assert "data-deadline='2026-11-05T23:59:59Z'" in html
        assert "data-deadline='2026-10-28T23:59:59Z'" in html
        assert "Deadline: Nov 5, 2026" in html
        assert "Deadline: Oct 28, 2026" in html
        assert "Date.now()" in html
        assert "ended since the last refresh" in html
        assert "2 shown (3 skipped: no cash prize or invite-only, or deadline passed)" in html
        assert "around 06:00" not in html
        assert "06:00 UTC" not in html
        assert hackathons.UPDATE_CADENCE in html
        assert "GitHub may run it a few hours late" in html
        assert "without conversion" not in html
        assert "verify currency" in html
        assert "hard-coded exchange rates" in html

    def test_json_records_deadline_and_counts(self, tmp_path):
        entries = hackathons.build_entries([make_item()])
        out = tmp_path / "hackathons.json"
        hackathons.write_json_doc(entries, {"total_count": 4, "fetched": 4, "complete": True}, out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["stats"]["shown"] == 1
        assert doc["stats"]["skipped"] == 3
        assert doc["hackathons"][0]["submission_deadline_utc"] == "2026-11-05T23:59:59Z"
        assert "without conversion" not in doc["note"]
        assert "verify currency" in doc["note"]

    def test_coverage_mentions_a_partial_fetch(self):
        text = hackathons.coverage_summary(2, {"fetched": 5, "complete": False})
        assert text.startswith("2 shown (3 skipped:")
        assert "partial fetch" in text

    def test_readme_and_workflow_match_the_schedule(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        workflow = (root / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
        assert "around 06:00" not in readme
        assert "without conversion" not in readme
        assert "GitHub may run it a few hours late" in readme
        assert "18:00" in readme
        assert "verify currency" in readme.lower()
        assert 'cron: "0 6 * * *"' in workflow
        assert 'cron: "0 18 * * *"' in workflow
