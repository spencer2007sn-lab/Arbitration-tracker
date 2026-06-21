from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from arb_scanner.models import ScanResult

log = structlog.get_logger()


@dataclass
class AppState:
    result: ScanResult | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


app_state = AppState()
_scanner_task: asyncio.Task | None = None
_fetchers: dict = {}
_config = None


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


async def scanner_loop(state: AppState, cfg, fetchers: dict) -> None:
    kalshi: KalshiFetcher = fetchers["kalshi"]
    poly: PolymarketFetcher = fetchers["polymarket"]
    matcher = Matcher(mappings_path=Path("market_mappings.yaml"))
    interval = cfg.refresh_interval

    while True:
        start = time.monotonic()
        errors: list[str] = []
        opportunities = []
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

            opportunities.sort(key=lambda o: o.edge_pct, reverse=True)

            scan_result = ScanResult(
                scanned_at=datetime.now(timezone.utc),
                opportunities=opportunities,
                pairs_checked=pairs_checked,
                scan_duration_ms=(time.monotonic() - start) * 1000,
                errors=errors,
            )
            async with state.lock:
                state.result = scan_result

            log.info(
                "scan_complete",
                pairs=pairs_checked,
                opportunities=len(opportunities),
                duration_ms=round(scan_result.scan_duration_ms, 1),
                errors=len(errors),
            )

        except Exception as e:
            log.error("scanner_loop_unhandled", error=str(e))

        elapsed = time.monotonic() - start
        await asyncio.sleep(max(0.0, interval - elapsed))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _fetchers, _scanner_task

    _config = build_config()
    _fetchers["kalshi"] = KalshiFetcher(
        api_key=_config.kalshi_api_key,
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

    for warning in validate_kalshi_credentials(_config):
        log.warning("kalshi_credential_warning", msg=warning)

    _scanner_task = asyncio.create_task(scanner_loop(app_state, _config, _fetchers))
    log.info("scanner_started", interval=_config.refresh_interval)

    yield

    _scanner_task.cancel()
    for fetcher in _fetchers.values():
        await fetcher.close()
    log.info("scanner_stopped")


app = FastAPI(title="Prediction Market Arb Scanner", lifespan=lifespan)

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = _STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=500)


@app.get("/opportunities")
async def get_opportunities():
    if app_state.result is None:
        return JSONResponse(
            {"error": "First scan not yet complete. Please wait a few seconds."},
            status_code=503,
        )
    return app_state.result


@app.get("/health")
async def health():
    last_scan = app_state.result.scanned_at.isoformat() if app_state.result else None
    return {"status": "ok", "last_scan": last_scan}


@app.get("/status")
async def status():
    kalshi_fetcher: KalshiFetcher | None = _fetchers.get("kalshi")
    cfg = _config
    result = app_state.result

    kalshi_info: dict = {"enabled": cfg.kalshi_enabled if cfg else False}
    if kalshi_fetcher:
        kalshi_info["auth_ready"] = kalshi_fetcher.auth_ready
        kalshi_info["pem_path"] = str(cfg.kalshi_private_key_path)
        kalshi_info["pem_exists"] = cfg.kalshi_private_key_path.exists()
        if cfg.kalshi_api_key:
            kalshi_info["api_key_prefix"] = cfg.kalshi_api_key[:8] + "…"
        else:
            kalshi_info["api_key_prefix"] = None

    return {
        "kalshi": kalshi_info,
        "polymarket": {
            "enabled": cfg.polymarket_enabled if cfg else False,
            "auth_required": False,
        },
        "last_scan": result.scanned_at.isoformat() if result else None,
        "pairs_checked": result.pairs_checked if result else 0,
    }


if __name__ == "__main__":
    uvicorn.run("arb_scanner.main:app", host="0.0.0.0", port=8000, reload=False)
