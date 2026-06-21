from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from arb_scanner.matcher import Matcher, _normalize, _resolution_warning
from arb_scanner.models import NormalizedMarket, Venue


def _close(hours: int = 4) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _km(
    ticker: str = "K1",
    title: str | None = None,
    sport: str = "soccer",
    team_a: str = "team a",
    team_b: str = "team b",
    close_hours: int = 4,
    resolution_notes: str | None = None,
) -> NormalizedMarket:
    raw = title or f"{team_a} to win versus {team_b}"
    return NormalizedMarket(
        venue=Venue.KALSHI,
        market_id=ticker,
        title=raw,
        normalized_title=_normalize(raw),
        sport=sport,
        team_a=team_a,
        team_b=team_b,
        outcome_type="moneyline",
        close_time=_close(close_hours),
        resolution_notes=resolution_notes,
    )


def _pm(
    cid: str = "P1",
    title: str | None = None,
    sport: str = "soccer",
    team_a: str = "team a",
    team_b: str = "team b",
    close_hours: int = 4,
    resolution_notes: str | None = None,
) -> NormalizedMarket:
    raw = title or f"will {team_a} win versus {team_b}"
    return NormalizedMarket(
        venue=Venue.POLYMARKET,
        market_id=cid,
        title=raw,
        normalized_title=_normalize(raw),
        sport=sport,
        team_a=team_a,
        team_b=team_b,
        outcome_type="moneyline",
        close_time=_close(close_hours),
        resolution_notes=resolution_notes,
        token_id_yes="t1",
        token_id_no="t2",
    )


def make_matcher(overrides: dict | None = None) -> Matcher:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump({"overrides": overrides or []}, f)
        path = Path(f.name)
    return Matcher(mappings_path=path)


# --- normalize ---

def test_normalize_strips_punctuation():
    # "man" is expanded to "manchester" by the abbreviation map
    assert _normalize("Man City!") == "manchester city"


def test_normalize_expands_vs():
    # "vs" expands to "versus" then "versus" is stripped as noise; team names remain
    assert _normalize("Team A vs Team B") == "team a team b"


def test_normalize_collapses_whitespace():
    assert _normalize("A   B") == "a b"


# --- exact matching ---

def test_exact_match_same_teams():
    m = make_matcher()
    pairs = m.match([_km("K1", team_a="team a", team_b="team b")],
                    [_pm("P1", team_a="team a", team_b="team b")])
    assert len(pairs) == 1
    assert pairs[0].match_method == "exact"


def test_exact_no_match_different_sport():
    m = make_matcher()
    pairs = m.match(
        [_km("K1", sport="soccer", team_a="team a", team_b="team b")],
        [_pm("P1", sport="basketball", team_a="team a", team_b="team b")],
    )
    assert len(pairs) == 0


def test_exact_no_match_different_teams():
    m = make_matcher()
    pairs = m.match(
        [_km("K1", title="Kansas City Chiefs to win", team_a="kansas city chiefs", team_b=None)],
        [_pm("P1", title="Will the Miami Heat win?", team_a="miami heat", team_b=None)],
    )
    assert len(pairs) == 0


# --- fuzzy matching ---

def test_fuzzy_match_abbreviated_names():
    m = make_matcher()
    k = NormalizedMarket(
        venue=Venue.KALSHI, market_id="K1",
        title="Man City to win vs Arsenal",
        normalized_title="manchester city to win versus arsenal",
        sport="soccer", outcome_type="moneyline",
        close_time=_close(), team_a=None, team_b=None,
    )
    p = NormalizedMarket(
        venue=Venue.POLYMARKET, market_id="P1",
        title="Will Manchester City win vs Arsenal?",
        normalized_title="will manchester city win versus arsenal",
        sport="soccer", outcome_type="moneyline",
        close_time=_close(),
        token_id_yes="t1", token_id_no="t2", team_a=None, team_b=None,
    )
    pairs = m.match([k], [p])
    assert len(pairs) == 1
    assert pairs[0].match_method == "fuzzy"
    assert pairs[0].match_score is not None
    assert pairs[0].match_score >= 85.0


def test_fuzzy_no_match_below_threshold():
    m = make_matcher()
    k = NormalizedMarket(
        venue=Venue.KALSHI, market_id="K1",
        title="Lakers to win",
        normalized_title="lakers to win",
        sport="basketball", outcome_type="moneyline",
        close_time=_close(), team_a=None, team_b=None,
    )
    p = NormalizedMarket(
        venue=Venue.POLYMARKET, market_id="P1",
        title="Will Real Madrid win?",
        normalized_title="will real madrid win",
        sport="soccer", outcome_type="moneyline",
        close_time=_close(),
        token_id_yes="t1", token_id_no="t2", team_a=None, team_b=None,
    )
    pairs = m.match([k], [p])
    assert len(pairs) == 0


# --- manual overrides ---

def test_manual_override_respected():
    overrides = [{"kalshi": "KXMANUAL-001", "polymarket": "0xoverride"}]
    m = make_matcher(overrides=overrides)
    k = _km("KXMANUAL-001", team_a="totally different")
    p = _pm("0xoverride", team_a="also different")
    pairs = m.match([k], [p])
    assert len(pairs) == 1
    assert pairs[0].match_method == "manual"


def test_manual_override_missing_poly_id_skipped():
    overrides = [{"kalshi": "KXMANUAL-001", "polymarket": "0xnonexistent"}]
    m = make_matcher(overrides=overrides)
    pairs = m.match([_km("KXMANUAL-001")], [])
    assert len(pairs) == 0


# --- resolution warning ---

def test_resolution_warning_overtime_mismatch():
    k = _km(resolution_notes="90 min only")
    p = _pm(resolution_notes="including OT")
    warning = _resolution_warning(k, p)
    assert warning is not None
    assert "mismatch" in warning.lower()


def test_no_resolution_warning_when_same():
    k = _km(resolution_notes="full time")
    p = _pm(resolution_notes="full time 90 min")
    warning = _resolution_warning(k, p)
    assert warning is None


def test_resolution_warning_surfaced_in_pair():
    m = make_matcher()
    k = _km("K1", resolution_notes="90 min only")
    p = _pm("P1", resolution_notes="including overtime")
    pairs = m.match([k], [p])
    assert len(pairs) == 1
    assert pairs[0].resolution_warning is not None


# --- partial exact match ---

def test_partial_exact_match_missing_team_b():
    m = make_matcher()
    # Kalshi has both teams; Polymarket only extracted team_a (from market question)
    k = _km("K1", sport="soccer", team_a="manchester city", team_b="arsenal")
    p = NormalizedMarket(
        venue=Venue.POLYMARKET, market_id="P1",
        title="Will Manchester City win?",
        normalized_title=_normalize("Will Manchester City win?"),
        sport="soccer", outcome_type="moneyline",
        close_time=_close(), team_a="manchester city", team_b=None,
        token_id_yes="t1", token_id_no="t2",
    )
    pairs = m.match([k], [p])
    assert len(pairs) == 1
    assert pairs[0].match_method == "partial_exact"


def test_partial_exact_no_match_when_team_b_conflicts():
    m = make_matcher()
    k = _km("K1", sport="soccer", team_a="manchester city", team_b="arsenal")
    p = NormalizedMarket(
        venue=Venue.POLYMARKET, market_id="P1",
        title="Will Manchester City win vs Chelsea?",
        normalized_title=_normalize("Will Manchester City win vs Chelsea?"),
        sport="soccer", outcome_type="moneyline",
        close_time=_close(), team_a="manchester city", team_b="chelsea",
        token_id_yes="t1", token_id_no="t2",
    )
    pairs = m.match([k], [p])
    assert len(pairs) == 0


# --- team fuzzy match ---

def test_team_fuzzy_match_abbreviation():
    m = make_matcher()
    # "man city" is an abbreviation of "manchester city" — partial_ratio gives 100%
    k = NormalizedMarket(
        venue=Venue.KALSHI, market_id="K1",
        title="Man City to win",
        normalized_title=_normalize("Man City to win"),
        sport="soccer", outcome_type="moneyline",
        close_time=_close(), team_a="manchester city", team_b=None,
    )
    p = NormalizedMarket(
        venue=Venue.POLYMARKET, market_id="P1",
        title="Will Manchester City win?",
        normalized_title=_normalize("Will Manchester City win?"),
        sport="soccer", outcome_type="moneyline",
        close_time=_close(), team_a="manchester city", team_b=None,
        token_id_yes="t1", token_id_no="t2",
    )
    pairs = m.match([k], [p])
    assert len(pairs) == 1
    assert pairs[0].match_method in ("exact", "partial_exact", "fuzzy", "team_fuzzy")


# --- no double matching ---

def test_poly_market_not_matched_twice():
    m = make_matcher()
    k1 = _km("K1", team_a="team a", team_b="team b")
    k2 = _km("K2", team_a="team a", team_b="team b")
    p = _pm("P1", team_a="team a", team_b="team b")
    pairs = m.match([k1, k2], [p])
    # Only one Kalshi market can match the single Poly market
    poly_ids = [pair.polymarket_market.market_id for pair in pairs]
    assert poly_ids.count("P1") == 1
