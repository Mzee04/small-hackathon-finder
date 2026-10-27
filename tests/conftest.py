"""Make the repo root importable so tests can `import hackathons`."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hackathons  # noqa: E402


@pytest.fixture(autouse=True)
def freeze_utc_now(monkeypatch):
    """Keep deadline checks stable. The saved Devpost sample ends in late 2026.

    2026-10-24 is before every end date in that sample and before the
    default fixture hackathon (Nov 05, 2026), so existing tests still see
    those rows. Tests of expiry pass ``now=`` themselves.
    """
    frozen = datetime(2026, 10, 24, 15, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(hackathons, "utc_now", lambda: frozen)
