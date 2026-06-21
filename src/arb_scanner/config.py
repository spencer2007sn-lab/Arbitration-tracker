from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kalshi_api_key: str = Field(default="", alias="KALSHI_API_KEY")
    kalshi_private_key_path: Path = Field(
        default=Path("kalshi_private.pem"),
        alias="KALSHI_PRIVATE_KEY_PATH",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


def load_yaml(path: Path = Path("config.yaml")) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class AppConfig:
    """Merged configuration: secrets from env + operational params from yaml."""

    def __init__(self, settings: Settings, yaml_cfg: dict) -> None:
        self.kalshi_api_key: str = settings.kalshi_api_key
        self.kalshi_private_key_path: Path = settings.kalshi_private_key_path
        self.log_level: str = settings.log_level

        self.refresh_interval: int = yaml_cfg.get("refresh_interval_seconds", 20)
        self.min_edge_pct: Decimal = Decimal(str(yaml_cfg.get("min_edge_pct", 0.5)))
        self.max_hours_to_close: int = yaml_cfg.get("max_hours_to_close", 48)
        self.sport_filter: list[str] = [
            s.lower() for s in yaml_cfg.get("sport_filter", [])
        ]

        venues = yaml_cfg.get("venues", {})
        kalshi_cfg = venues.get("kalshi", {})
        poly_cfg = venues.get("polymarket", {})

        self.kalshi_enabled: bool = kalshi_cfg.get("enabled", True)
        self.kalshi_base_url: str = kalshi_cfg.get(
            "base_url", "https://api.kalshi.com/trade-api/v2"
        )
        self.polymarket_enabled: bool = poly_cfg.get("enabled", True)
        self.polymarket_gamma_url: str = poly_cfg.get(
            "gamma_url", "https://gamma-api.polymarket.com"
        )
        self.polymarket_clob_url: str = poly_cfg.get(
            "clob_url", "https://clob.polymarket.com"
        )

        fees = yaml_cfg.get("fees", {})
        self.kalshi_taker_coeff: Decimal = Decimal(
            str(fees.get("kalshi_taker_coeff", "0.07"))
        )
        self.polymarket_sports_rate: Decimal = Decimal(
            str(fees.get("polymarket_sports_rate", "0.0075"))
        )
        self.polymarket_default_rate: Decimal = Decimal(
            str(fees.get("polymarket_default_rate", "0.01"))
        )


def validate_kalshi_credentials(cfg: "AppConfig") -> list[str]:
    """Return a list of warning strings about missing/invalid Kalshi credentials."""
    warnings: list[str] = []
    if not cfg.kalshi_enabled:
        return []
    if not cfg.kalshi_api_key:
        warnings.append(
            "KALSHI_API_KEY is not set in .env — Kalshi requests will be unauthenticated."
        )
    if not cfg.kalshi_private_key_path.exists():
        warnings.append(
            f"Kalshi PEM not found: '{cfg.kalshi_private_key_path}' — "
            "run: uv run python scripts/generate_kalshi_key.py"
        )
    return warnings


def build_config(
    yaml_path: Path = Path("config.yaml"),
    env_path: Optional[Path] = None,
) -> AppConfig:
    settings = Settings(_env_file=env_path or ".env")  # type: ignore[call-arg]
    yaml_cfg = load_yaml(yaml_path)
    return AppConfig(settings, yaml_cfg)
