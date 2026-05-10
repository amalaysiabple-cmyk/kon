"""YAML configuration loader with pydantic validation.

Fails fast at startup if config is malformed so we never silently fall back
to default magic numbers.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator


class LRMCfg(BaseModel):
    proximity_band_atr: float
    min_cluster_density: float
    min_oi_usd: float
    kernel_bandwidth_pct: float


class FBPDCfg(BaseModel):
    min_divergence: float
    funding_filter: float


class MPWeights(BaseModel):
    cvd_velocity: float
    liq_hazard: float
    spread_compression: float
    taker_aggression: float
    book_pressure: float


class MPCfg(BaseModel):
    gate: float
    hazard_min_rate: float
    weights: MPWeights
    bias: float


class EntryCfg(BaseModel):
    limit_offset_pct: float


class PairCfg(BaseModel):
    maintenance_margin: float
    max_leverage: int
    quarterly_symbol: str
    lrm: LRMCfg
    fbpd: FBPDCfg
    mp: MPCfg
    entry: EntryCfg


class GlobalCfg(BaseModel):
    default_equity_usd: float = 1000.0
    risk_per_trade_pct: float = 2.0
    liq_buffer_seconds: int = 60
    log_path: str = "logs/calculations.jsonl"


class MTSCfg(BaseModel):
    block_before_minutes: int
    block_after_minutes: int
    boost_window_minutes: Tuple[int, int]
    boost_multiplier: float
    funding_proximity_minutes: int
    funding_settlement_hours_utc: List[int]


class Config(BaseModel):
    global_: GlobalCfg = Field(alias="global")
    pairs: Dict[str, PairCfg]
    mts: MTSCfg

    model_config = {"populate_by_name": True}


class EventEntry(BaseModel):
    name: str
    impact: str
    datetime_utc: datetime

    @field_validator("impact")
    @classmethod
    def lower_impact(cls, v: str) -> str:
        return v.lower()


class EventsCfg(BaseModel):
    events: List[EventEntry] = []


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)


def load_events(path: str | Path) -> EventsCfg:
    p = Path(path)
    if not p.exists():
        return EventsCfg(events=[])
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return EventsCfg.model_validate(raw)
