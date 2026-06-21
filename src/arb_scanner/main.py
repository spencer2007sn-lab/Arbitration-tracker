from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from arb_scanner import calculator
from arb_scanner.config import build_config, validate_kalshi_credentials
from arb_scanner.fetchers.kalshi import KalshiFetcher
from arb_scanner.fetchers.polymarket import PolymarketFetcher
from arb_scanner.matcher import Matcher
from arb_scanner.models import ScanResult, TrackedPair

log = structlog.get_logger()

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_fetchers: dict = {}
_config = None

# Initialize at module level — runs once per process (cold start on Vercel,
# once per server start locally). Reused across warm function invocations.
_config = build_config(yaml_path=_PROJECT_ROOT / "config.yaml")
_fetchers["kalshi"] = KalshiFetcher(
    api_key=_config.kalshi_api_key,
    private_key_content=_config.kalshi_private_key_content,
    private_key_path=_config.kalshi_private_key_path,
    base_url=_config.kalshi_base_url,
    max_hours_to_close=_config.max_hours_to_close,
    sport_filter=_config.sport_filter,
)
_fetchers["polymarket"] = PolymarketFetcher(
    gamma_url=_config.polymarket_gamma_url,
    clob_url=_config.polymarket_clob_url,
    max_hours_to_close=_config.max_hours_to_close,
    sport_filter=_config.sport_filter,
    poly_fee_rate=_config.polymarket_sports_rate,
)
for _w in validate_kalshi_credentials(_config):
    log.warning("kalshi_credential_warning", msg=_w)


async def _fetch_pair_books(
    kalshi: KalshiFetcher,
    poly: PolymarketFetcher,
    pair,
) -> tuple:
    kb, pb = await asyncio.gather(
        kalshi.fetch_order_book(pair.kalshi_market),
        poly.fetch_order_book(pair.polymarket_market),
        return_exceptions=True,
    )
    return kb, pb


async def _do_scan() -> ScanResult:
    """Run one full market scan and return the result."""
    kalshi: KalshiFetcher = _fetchers["kalshi"]
    poly: PolymarketFetcher = _fetchers["polymarket"]
    matcher = Matcher(mappings_path=_PROJECT_ROOT / "market_mappings.yaml")
    cfg = _config

    start = time.monotonic()
    errors: list[str] = []
    opportunities = []
    tracked_pairs: list[TrackedPair] = []
    pairs_checked = 0

    try:
        results = await asyncio.gather(
            kalshi.fetch_markets(),
            poly.fetch_markets(),
            return_exceptions=True,
        )

        kalshi_markets, poly_markets = [], []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                errors.append(f"{'kalshi' if i == 0 else 'polymarket'}_fetch_error: {res}")
                log.error("fetch_error", venue=["kalshi", "polymarket"][i], error=str(res))
            elif i == 0:
                kalshi_markets = res
            else:
                poly_markets = res

        log.info("markets_fetched", kalshi=len(kalshi_markets), polymarket=len(poly_markets))
        pairs = matcher.match(kalshi_markets, poly_markets)
        pairs_checked = len(pairs)

        if pairs:
            book_results = await asyncio.gather(
                *[_fetch_pair_books(kalshi, poly, p) for p in pairs],
                return_exceptions=True,
            )

            for pair, books in zip(pairs, book_results):
                if isinstance(books, Exception):
                    errors.append(f"orderbook_error({pair.kalshi_market.market_id}): {books}")
                    continue
                kb, pb = books
                if isinstance(kb, Exception):
                    errors.append(f"kalshi_book_error: {kb}")
                    continue
                if isinstance(pb, Exception):
                    errors.append(f"poly_book_error: {pb}")
                    continue

                found = calculator.check_arb(
                    pair, kb, pb,
                    cfg.kalshi_taker_coeff,
                    cfg.polymarket_sports_rate,
                    cfg.min_edge_pct,
                )
                opportunities.extend(found)

                # Record this pair in tracked_pairs regardless of arb
                costs = []
                for yes_ask, no_ask in [
                    (kb.best_ask_yes, pb.best_ask_no),
                    (pb.best_ask_yes, kb.best_ask_no),
                ]:
                    if yes_ask is not None and no_ask is not None:
                        costs.append(yes_ask + no_ask)
                tracked_pairs.append(TrackedPair(
                    kalshi_title=pair.kalshi_market.title,
                    poly_title=pair.polymarket_market.title,
                    match_method=pair.match_method,
                    sport=pair.kalshi_market.sport or pair.polymarket_market.sport,
                    kalshi_yes_ask=kb.best_ask_yes,
                    kalshi_no_ask=kb.best_ask_no,
                    poly_yes_ask=pb.best_ask_yes,
                    poly_no_ask=pb.best_ask_no,
                    best_net_cost=min(costs) if costs else None,
                ))

        opportunities.sort(key=lambda o: o.edge_pct, reverse=True)

        tracked_pairs.sort(key=lambda p: p.best_net_cost or 999)
        scan_result = ScanResult(
            scanned_at=datetime.now(timezone.utc),
            opportunities=opportunities,
            pairs_checked=pairs_checked,
            kalshi_markets=len(kalshi_markets),
            polymarket_markets=len(poly_markets),
            scan_duration_ms=(time.monotonic() - start) * 1000,
            errors=errors,
            tracked_pairs=tracked_pairs,
        )
        log.info(
            "scan_complete",
            pairs=pairs_checked,
            opportunities=len(opportunities),
            duration_ms=round(scan_result.scan_duration_ms, 1),
            errors=len(errors),
        )
        return scan_result

    except Exception as e:
        log.error("scan_unhandled_error", error=str(e))
        return ScanResult(
            scanned_at=datetime.now(timezone.utc),
            opportunities=[],
            pairs_checked=0,
            scan_duration_ms=(time.monotonic() - start) * 1000,
            errors=[f"unhandled_error: {e}"],
        )


app = FastAPI(title="Prediction Market Arb Scanner")

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = _STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=500)


@app.get("/opportunities")
async def get_opportunities():
    result = await _do_scan()
    return JSONResponse(
        result.model_dump(mode="json"),
        headers={"Cache-Control": "s-maxage=20, stale-while-revalidate=30"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
async def status():
    kalshi_fetcher: KalshiFetcher | None = _fetchers.get("kalshi")
    cfg = _config

    kalshi_info: dict = {"enabled": cfg.kalshi_enabled if cfg else False}
    if kalshi_fetcher:
        kalshi_info["auth_ready"] = kalshi_fetcher.auth_ready
        if cfg and cfg.kalshi_private_key_content:
            kalshi_info["pem_source"] = "env_var"
        else:
            kalshi_info["pem_path"] = str(cfg.kalshi_private_key_path) if cfg else None
            kalshi_info["pem_exists"] = cfg.kalshi_private_key_path.exists() if cfg else False
        if cfg and cfg.kalshi_api_key:
            kalshi_info["api_key_prefix"] = cfg.kalshi_api_key[:8] + "…"
        else:
            kalshi_info["api_key_prefix"] = None

    return {
        "kalshi": kalshi_info,
        "polymarket": {
            "enabled": cfg.polymarket_enabled if cfg else False,
            "auth_required": False,
        },
        "mode": "on_demand",
    }


if __name__ == "__main__":
    uvicorn.run("arb_scanner.main:app", host="0.0.0.0", port=8000, reload=False)
