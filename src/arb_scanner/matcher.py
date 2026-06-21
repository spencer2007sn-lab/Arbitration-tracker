from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import structlog
import yaml
from rapidfuzz import fuzz, process

from arb_scanner.models import MarketPair, NormalizedMarket, Venue

log = structlog.get_logger()

_ABBREV = [
    (r"\bvs\.?\b", "versus"),
    (r"\bfc\b", "football club"),
    (r"\butd\b", "united"),
    (r"\bman\b", "manchester"),
    (r"\bla\b", "los angeles"),
    (r"\bny\b", "new york"),
    (r"\bkc\b", "kansas city"),
    (r"\bsf\b", "san francisco"),
    (r"\btb\b", "tampa bay"),
]

# Words that carry no information about which teams/players are involved.
# Deliberately excludes single letters (a, b, …) which appear in team names.
_NOISE = re.compile(
    r"\b(will|the|to|in|at|on|or|of|and|be|by|"
    r"versus|"
    r"win|wins|winning|won|beat|beats|beating|defeat|defeats|"
    r"cover|covers|score|scores|reach|make|take|"
    r"championship|title|finals|final|cup|series|"
    r"game|match|season|playoffs|playoff|tournament|"
    r"moneyline|ml|spread|total|over|under|ou)\b"
)


def _normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    for pattern, replacement in _ABBREV:
        t = re.sub(pattern, replacement, t)
    t = _NOISE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


_RESOLUTION_KEYWORDS = {
    "90min": ["90 min", "90min", "regulation", "full time", "ft only"],
    "extra_time": ["extra time", "overtime", "ot", "et", "incl ot", "incl et", "including ot"],
}

FUZZY_THRESHOLD = 75.0


def _resolution_warning(kalshi: NormalizedMarket, poly: NormalizedMarket) -> Optional[str]:
    """Return a warning string if the two markets may settle differently."""
    k_notes = (kalshi.resolution_notes or kalshi.title or "").lower()
    p_notes = (poly.resolution_notes or poly.title or "").lower()

    k_has_90 = any(kw in k_notes for kw in _RESOLUTION_KEYWORDS["90min"])
    p_has_ot = any(kw in p_notes for kw in _RESOLUTION_KEYWORDS["extra_time"])
    p_has_90 = any(kw in p_notes for kw in _RESOLUTION_KEYWORDS["90min"])
    k_has_ot = any(kw in k_notes for kw in _RESOLUTION_KEYWORDS["extra_time"])

    if (k_has_90 and p_has_ot) or (p_has_90 and k_has_ot):
        return (
            f"Settlement mismatch: Kalshi notes='{kalshi.resolution_notes}', "
            f"Polymarket title='{poly.title[:60]}'"
        )
    return None


def _exact_key(market: NormalizedMarket) -> Optional[tuple]:
    """Return a hashable key for exact matching, or None if not enough info."""
    if market.team_a and market.sport:
        return (
            market.sport,
            market.outcome_type,
            market.team_a,
            market.team_b or "",
        )
    return None


class Matcher:
    def __init__(
        self,
        mappings_path: Path = Path("market_mappings.yaml"),
        fuzzy_threshold: float = FUZZY_THRESHOLD,
    ) -> None:
        self._fuzzy_threshold = fuzzy_threshold
        self._manual_overrides: dict[str, str] = {}  # kalshi_ticker -> poly_condition_id
        self._load_overrides(mappings_path)

    def _load_overrides(self, path: Path) -> None:
        if not path.exists():
            return
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        for entry in data.get("overrides", []):
            self._manual_overrides[entry["kalshi"]] = entry["polymarket"]

    def match(
        self,
        kalshi_markets: list[NormalizedMarket],
        poly_markets: list[NormalizedMarket],
    ) -> list[MarketPair]:
        pairs: list[MarketPair] = []
        poly_by_id = {m.market_id: m for m in poly_markets}

        # --- 1. Manual overrides ---
        matched_poly_ids: set[str] = set()
        matched_kalshi_ids: set[str] = set()

        for k in kalshi_markets:
            poly_id = self._manual_overrides.get(k.market_id)
            if poly_id and poly_id in poly_by_id:
                p = poly_by_id[poly_id]
                warning = _resolution_warning(k, p)
                pairs.append(MarketPair(
                    kalshi_market=k,
                    polymarket_market=p,
                    match_method="manual",
                    resolution_warning=warning,
                ))
                matched_kalshi_ids.add(k.market_id)
                matched_poly_ids.add(poly_id)

        # --- 2. Exact structural match ---
        unmatched_kalshi = [k for k in kalshi_markets if k.market_id not in matched_kalshi_ids]
        unmatched_poly = [p for p in poly_markets if p.market_id not in matched_poly_ids]

        exact_index: dict[tuple, list[NormalizedMarket]] = {}
        for p in unmatched_poly:
            key = _exact_key(p)
            if key:
                exact_index.setdefault(key, []).append(p)

        still_unmatched_kalshi: list[NormalizedMarket] = []
        for k in unmatched_kalshi:
            key = _exact_key(k)
            matched = False
            if key and key in exact_index:
                # Take the first candidate that hasn't already been matched
                for p in exact_index[key]:
                    if p.market_id in matched_poly_ids:
                        continue
                    warning = _resolution_warning(k, p)
                    pairs.append(MarketPair(
                        kalshi_market=k,
                        polymarket_market=p,
                        match_method="exact",
                        resolution_warning=warning,
                    ))
                    matched_poly_ids.add(p.market_id)
                    matched_kalshi_ids.add(k.market_id)
                    matched = True
                    break
            if not matched:
                still_unmatched_kalshi.append(k)

        # --- 2b. Partial exact match (sport + team_a only, no team_b required) ---
        partial_index: dict[tuple, list[NormalizedMarket]] = {}
        for p in unmatched_poly:
            if p.market_id in matched_poly_ids:
                continue
            if p.team_a and p.sport:
                key = (p.sport, p.outcome_type, p.team_a)
                partial_index.setdefault(key, []).append(p)

        for k in list(still_unmatched_kalshi):
            if not (k.team_a and k.sport):
                continue
            key = (k.sport, k.outcome_type, k.team_a)
            if key not in partial_index:
                continue
            for p in partial_index[key]:
                if p.market_id in matched_poly_ids:
                    continue
                # Skip if both sides have team_b but they differ
                if k.team_b and p.team_b and k.team_b != p.team_b:
                    continue
                warning = _resolution_warning(k, p)
                pairs.append(MarketPair(
                    kalshi_market=k,
                    polymarket_market=p,
                    match_method="partial_exact",
                    resolution_warning=warning,
                ))
                matched_poly_ids.add(p.market_id)
                matched_kalshi_ids.add(k.market_id)
                still_unmatched_kalshi.remove(k)
                break

        # --- 3. Fuzzy title match ---
        remaining_poly = [p for p in unmatched_poly if p.market_id not in matched_poly_ids]
        poly_titles = {p.market_id: _normalize(p.title) for p in remaining_poly}
        poly_by_id_remaining = {p.market_id: p for p in remaining_poly}

        for k in still_unmatched_kalshi:
            if not poly_titles:
                break
            k_title = _normalize(k.title)
            # Build sport-filtered candidate set to prevent cross-sport false positives
            candidates = {
                pid: title
                for pid, title in poly_titles.items()
                if k.sport is None
                or poly_by_id_remaining[pid].sport is None
                or k.sport == poly_by_id_remaining[pid].sport
            }
            if not candidates:
                continue
            result = process.extractOne(
                k_title,
                candidates,
                scorer=fuzz.token_set_ratio,
                score_cutoff=self._fuzzy_threshold,
            )
            if result is None:
                continue
            _matched_title, score, poly_id = result
            p = poly_by_id_remaining[poly_id]
            # Block fuzzy pair when both sides have extracted team_b but they differ
            if k.team_b and p.team_b and k.team_b != p.team_b:
                continue
            warning = _resolution_warning(k, p)
            pairs.append(MarketPair(
                kalshi_market=k,
                polymarket_market=p,
                match_method="fuzzy",
                match_score=float(score),
                resolution_warning=warning,
            ))
            matched_poly_ids.add(poly_id)
            # Remove from candidates to prevent double-matching
            del poly_titles[poly_id]

        # --- 4. Team-level fuzzy (handles abbreviations like "man city" → "manchester city") ---
        remaining_poly_for_team = [p for p in poly_markets if p.market_id not in matched_poly_ids]

        for k in [m for m in still_unmatched_kalshi if m.team_a]:
            if k.market_id in matched_kalshi_ids:
                continue
            best_score = 0
            best_p = None
            for p in remaining_poly_for_team:
                if p.market_id in matched_poly_ids or not p.team_a:
                    continue
                if k.sport and p.sport and k.sport != p.sport:
                    continue
                # Block when both sides have different team_b (different opponents)
                if k.team_b and p.team_b and k.team_b != p.team_b:
                    continue
                # partial_ratio: "man city" scores 100% against "manchester city"
                score_a = fuzz.partial_ratio(k.team_a, p.team_a)
                score_b = fuzz.partial_ratio(k.team_b or "", p.team_a) if k.team_b else 0
                score = max(score_a, score_b)
                if score > best_score and score >= 80:
                    best_score = score
                    best_p = p
            if best_p:
                warning = _resolution_warning(k, best_p)
                pairs.append(MarketPair(
                    kalshi_market=k,
                    polymarket_market=best_p,
                    match_method="team_fuzzy",
                    match_score=float(best_score),
                    resolution_warning=warning,
                ))
                matched_poly_ids.add(best_p.market_id)
                matched_kalshi_ids.add(k.market_id)
                remaining_poly_for_team = [
                    p for p in remaining_poly_for_team if p.market_id not in matched_poly_ids
                ]

        log.info(
            "matcher_complete",
            kalshi_input=len(kalshi_markets),
            poly_input=len(poly_markets),
            pairs_found=len(pairs),
        )
        return pairs
