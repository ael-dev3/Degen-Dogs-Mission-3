#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import concurrent.futures
import hashlib
import html
import ipaddress
import json
import os
import queue
import random
import re
import signal
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any, Callable

getcontext().prec = 80

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "sql" / "mission3_dashboard.sql"
GENERATED = ROOT / "generated"
PUBLIC_GENERATED = ROOT / "public" / "generated"
CACHE_DIR = ROOT / ".cache"
LOG_CACHE_DIR = CACHE_DIR / "rpc_logs"
DOG_METADATA_CACHE = CACHE_DIR / "dog_metadata.json"
BLOCK_TIME_CACHE = CACHE_DIR / "block_times.json"
WOOF_BALANCE_CACHE = CACHE_DIR / "woof_balances.json"
README_TEMPLATE_PATH = ROOT / "README.template.md"
HISTORICAL_ARCHIVE_INDEXES = {
    1: ROOT / "archive" / "mission1" / "data" / "generated" / "mission1_dog_search_index.json",
    2: ROOT / "archive" / "mission2" / "data" / "generated" / "mission2_dog_search_index.json",
    3: ROOT / "archive" / "mission3" / "data" / "generated" / "mission3_dog_search_index.json",
}
IDENTITY_PATH = ROOT / "archive" / "data" / "identity" / "wallet_profiles.json"
HISTORICAL_PRICES_DAILY = ROOT / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json"
HISTORICAL_PRICE_SCHEMA = [
    ("asset_key", "TEXT"),
    ("date_utc", "TEXT"),
    ("price_usd", "TEXT"),
    ("source", "TEXT"),
    ("source_detail", "TEXT"),
    ("confidence", "TEXT"),
    ("timestamp_utc", "TEXT"),
    ("notes", "TEXT"),
]
MISSION_CHAIN = {
    1: ("Polygon", 137),
    2: ("Degen Chain", 666666666),
    3: ("Base", 8453),
}

DEFAULT_RPC_URLS = [
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://base-mainnet.g.alchemy.com/public",
    "https://developer-access-mainnet.base.org",
]
DEFAULT_LOG_RPC_URLS = [
    "https://mainnet.base.org",
    "https://developer-access-mainnet.base.org",
    "https://base.gateway.tenderly.co",
    "https://base.lava.build",
]
PUBLIC_RPC_HOSTNAMES = frozenset(
    (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    for url in (*DEFAULT_RPC_URLS, *DEFAULT_LOG_RPC_URLS)
)
EXPLICIT_RPC_CONFIG = any(
    os.environ.get(name, "").strip()
    for name in ("BASE_RPC_URL", "BASE_RPC_URLS", "BASE_LOG_RPC_URLS")
)
INCLUDE_PUBLIC_RPC_FALLBACKS = (
    os.environ.get("BASE_INCLUDE_PUBLIC_FALLBACKS", "").strip().lower() in {"1", "true", "yes", "on"}
    if "BASE_INCLUDE_PUBLIC_FALLBACKS" in os.environ
    else not EXPLICIT_RPC_CONFIG
)
_SINGLE_RPC_URL = os.environ.get("BASE_RPC_URL", "").strip()
_MULTI_RPC_URLS = os.environ.get("BASE_RPC_URLS", "").strip()
if _SINGLE_RPC_URL:
    RPC_URLS = [_SINGLE_RPC_URL]
elif _MULTI_RPC_URLS:
    RPC_URLS = [url.strip() for url in _MULTI_RPC_URLS.split(",") if url.strip()]
else:
    RPC_URLS = list(DEFAULT_RPC_URLS)
if _SINGLE_RPC_URL:
    LOG_RPC_URLS = [_SINGLE_RPC_URL]
elif os.environ.get("BASE_LOG_RPC_URLS"):
    LOG_RPC_URLS = [url.strip() for url in os.environ["BASE_LOG_RPC_URLS"].split(",") if url.strip()]
elif EXPLICIT_RPC_CONFIG:
    LOG_RPC_URLS = list(RPC_URLS)
else:
    LOG_RPC_URLS = list(DEFAULT_LOG_RPC_URLS)
FROM_BLOCK = int(os.environ.get("BASE_FROM_BLOCK", "40500000"))
# Base recommends keeping eth_getLogs scans under 2,000 blocks for reliable
# results. Credentialed providers may safely advertise a larger limit, so keep
# a bounded override while using the official reliability recommendation by
# default. The low-latency current refresher overrides this to 100 blocks.
LOG_CHUNK = max(1, min(int(os.environ.get("BASE_LOG_CHUNK", "2000")), 10000))
LOG_WORKERS = max(1, min(int(os.environ.get("BASE_LOG_WORKERS", "4")), 16))
RPC_BATCH_LIMIT = max(1, min(int(os.environ.get("BASE_RPC_BATCH_LIMIT", "10")), 10))
RPC_ATTEMPTS = max(1, min(int(os.environ.get("BASE_RPC_ATTEMPTS", "6")), 10))
RPC_QUORUM_DEADLINE_SECONDS = max(
    5.0,
    min(float(os.environ.get("BASE_RPC_QUORUM_DEADLINE_SECONDS", "35")), 120.0),
)
RPC_HEAD_PROBE_DEADLINE_SECONDS = max(
    2.0,
    min(float(os.environ.get("BASE_RPC_HEAD_PROBE_DEADLINE_SECONDS", "12")), 60.0),
)
RPC_HEAD_PROBE_GRACE_SECONDS = max(
    0.0,
    min(float(os.environ.get("BASE_RPC_HEAD_PROBE_GRACE_SECONDS", "0.35")), 3.0),
)
RPC_SLOW_COOLDOWN_SECONDS = max(
    1.0,
    min(float(os.environ.get("BASE_RPC_SLOW_COOLDOWN_SECONDS", "60")), 600.0),
)
RPC_MAX_HEAD_SPREAD_BLOCKS = max(
    1,
    min(int(os.environ.get("BASE_RPC_MAX_HEAD_SPREAD_BLOCKS", "20")), 10_000),
)
RPC_MAX_BLOCK_AGE_SECONDS = max(
    30,
    min(int(os.environ.get("BASE_RPC_MAX_BLOCK_AGE_SECONDS", "600")), 86_400),
)
LOG_RPC_TIMEOUT = max(10, min(int(os.environ.get("BASE_LOG_RPC_TIMEOUT", "35")), 120))
BLOCK_TIME_RPC_TIMEOUT = max(10, min(int(os.environ.get("BASE_BLOCK_TIME_RPC_TIMEOUT", "30")), 120))
LOG_CACHE_OVERLAP_BLOCKS = max(1, min(int(os.environ.get("MISSION3_LOG_CACHE_OVERLAP_BLOCKS", "100")), 500))
RPC_QUORUM_SIZE = max(2, min(int(os.environ.get("BASE_RPC_QUORUM_SIZE", "2")), 3))
SNAPSHOT_CONFIRMATIONS = max(1, min(int(os.environ.get("BASE_SNAPSHOT_CONFIRMATIONS", "1")), 64))
LOG_QUORUM_MAX_BLOCKS = max(1, min(int(os.environ.get("MISSION3_LOG_QUORUM_MAX_BLOCKS", "50")), 10000))
LOG_QUORUM_WINDOW_BLOCKS = max(
    LOG_QUORUM_MAX_BLOCKS,
    min(int(os.environ.get("MISSION3_LOG_QUORUM_WINDOW_BLOCKS", "500")), 10000),
)
DOG_METADATA_FETCH_TIMEOUT = max(3, min(int(os.environ.get("DOG_METADATA_FETCH_TIMEOUT", "12")), 45))
DOG_METADATA_FALLBACK_TIMEOUT = max(3, min(int(os.environ.get("DOG_METADATA_FALLBACK_TIMEOUT", "20")), 60))
DOG_METADATA_ITEM_TIMEOUT = max(15, min(int(os.environ.get("DOG_METADATA_ITEM_TIMEOUT", "75")), 180))
DOG_METADATA_SEQUENTIAL_THRESHOLD = max(0, min(int(os.environ.get("DOG_METADATA_SEQUENTIAL_THRESHOLD", "8")), 100))
DOG_METADATA_ALLOWED_HOSTS = frozenset(
    host.strip().lower().rstrip(".")
    for host in os.environ.get(
        "DOG_METADATA_ALLOWED_HOSTS",
        "degendogs.club,api.degendogs.club,ipfs.io",
    ).split(",")
    if host.strip()
)
DOG_METADATA_MAX_RESPONSE_BYTES = max(
    65_536,
    min(int(os.environ.get("DOG_METADATA_MAX_RESPONSE_BYTES", "2097152")), 10_485_760),
)
DOG_METADATA_CACHE_MAX_AGE_SECONDS = max(
    3_600,
    min(int(os.environ.get("DOG_METADATA_CACHE_MAX_AGE_SECONDS", "86400")), 2_592_000),
)
RPC_MAX_RESPONSE_BYTES = max(
    1_048_576,
    min(int(os.environ.get("BASE_RPC_MAX_RESPONSE_BYTES", "33554432")), 67_108_864),
)
EXTERNAL_JSON_MAX_RESPONSE_BYTES = 8_388_608
WOOF_HOLDER_DISCOVERY_MAX_RESPONSE_BYTES = 2_097_152
WOOF_HOLDER_DISCOVERY_MAX_PAGES = 100
WOOF_HOLDER_DISCOVERY_MAX_CANDIDATES = 10_000

AUCTION_HOUSE = "0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA"
DEGEN_DOGS = "0x09154248fFDbaF8aA877aE8A4bf8cE1503596428"
WOOF = "0x3e5c4FA0cAA794516eD0DF77f31daA534918d492"
SUP = "0xa69f80524381275A7fFdb3AE01c54150644c8792"
DEFAULT_WOOF_HOLDER_DISCOVERY_URL = (
    f"https://base.blockscout.com/api/v2/tokens/{WOOF}/holders"
)
WOOF_HOLDER_DISCOVERY_URL = os.environ.get(
    "WOOF_HOLDER_DISCOVERY_URL",
    DEFAULT_WOOF_HOLDER_DISCOVERY_URL,
).strip()
ZERO = "0x0000000000000000000000000000000000000000"
OPENSEA_ITEM_BASE = "https://opensea.io/item/base"
OPENSEA_COLLECTION_URL = "https://opensea.io/collection/degen-dogs-club"
DASHBOARD_LINK_HOSTS = frozenset({
    "basescan.org",
    "degendogs.club",
    "explorer.degen.tips",
    "farcaster.xyz",
    "opensea.io",
    "polygonscan.com",
})
DASHBOARD_IMAGE_HOSTS = frozenset({"api.degendogs.club", "degendogs.club", "ipfs.io"})

# Populated only after verified_snapshot() establishes a canonical block hash.
# Critical hash-pinned reads and short-range event scans are then required to
# agree across the same independent RPC quorum before data is publishable.
VERIFIED_SNAPSHOT_URLS: list[str] = []
VERIFIED_LOG_URLS: list[str] = []
RPC_SLOW_UNTIL: dict[tuple[str, str], float] = {}


def dog_opensea_url(token_id: int | str) -> str:
    return f"{OPENSEA_ITEM_BASE}/{DEGEN_DOGS.lower()}/{int(token_id)}"


def safe_http_url(value: Any, *, allowed_hosts: frozenset[str] | None = None) -> str:
    """Return a browser-safe HTTPS URL, or an empty string when unsafe."""
    text = str(value or "").strip()
    if not text or any(character.isspace() or ord(character) < 32 for character in text):
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
        host = (parsed.hostname or "").lower().rstrip(".")
        # Accessing .port validates malformed/out-of-range ports. Dashboard
        # links are deliberately limited to HTTPS' default port so an
        # allowlisted hostname cannot tunnel browser traffic to another
        # service exposed on that host.
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.lower() != "https" or not host:
        return ""
    if port not in {None, 443}:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        pass
    if allowed_hosts is not None and host not in allowed_hosts:
        return ""
    return text


def safe_dashboard_link(value: Any) -> str:
    return safe_http_url(value, allowed_hosts=DASHBOARD_LINK_HOSTS)


def safe_dashboard_image(value: Any) -> str:
    return safe_http_url(value, allowed_hosts=DASHBOARD_IMAGE_HOSTS)


def opensea_trait_url(trait_type: str, trait_value: str) -> str:
    payload = json.dumps(
        [{"traitType": str(trait_type), "values": [str(trait_value)]}],
        separators=(",", ":"),
    )
    encoded = urllib.parse.quote(payload, safe="[]{}:,")
    return f"{OPENSEA_COLLECTION_URL}?traits={encoded}"

REWARD_STREAM_SNAPSHOT_PATH = ROOT / "config" / "reward_stream_snapshot.json"
REWARD_EXCLUDES = "woof_vault_bonus"


@dataclass(frozen=True)
class RewardStreamSnapshot:
    snapshot_utc: str
    dogs_count: Decimal
    woof_received: Decimal | None
    woof_flow_per_day: Decimal
    sup_received: Decimal | None
    sup_flow_per_day: Decimal
    basis_source: str
    note: str = ""
    excludes: str = REWARD_EXCLUDES

    @property
    def woof_per_dog_per_day(self) -> Decimal:
        return self.woof_flow_per_day / self.dogs_count

    @property
    def sup_per_dog_per_day(self) -> Decimal:
        return self.sup_flow_per_day / self.dogs_count


def required_decimal_field(data: dict[str, Any], key: str, path: Path) -> Decimal:
    raw = str(data.get(key, "")).replace(",", "").strip()
    if not raw:
        raise ValueError(f"{path.relative_to(ROOT)} missing required decimal field {key}")
    value = Decimal(raw)
    if value <= 0:
        raise ValueError(f"{path.relative_to(ROOT)} {key} must be positive")
    return value


def optional_decimal_field(data: dict[str, Any], key: str, path: Path) -> Decimal | None:
    raw = str(data.get(key, "")).replace(",", "").strip()
    if not raw or raw.upper() == "N/A":
        return None
    value = Decimal(raw)
    if value < 0:
        raise ValueError(f"{path.relative_to(ROOT)} {key} must not be negative")
    return value


def validate_derived_reward_value(data: dict[str, Any], key: str, calculated: Decimal, path: Path) -> None:
    if key not in data:
        return
    supplied = required_decimal_field(data, key, path)
    quant = Decimal(1).scaleb(supplied.as_tuple().exponent)
    expected = calculated.quantize(quant, rounding=ROUND_HALF_UP)
    if supplied != expected:
        raise ValueError(
            f"{path.relative_to(ROOT)} {key} stale: expected {expected:f} from observed stream, got {supplied:f}"
        )


def load_reward_stream_snapshot(path: Path = REWARD_STREAM_SNAPSHOT_PATH) -> RewardStreamSnapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a JSON object")
    dogs_count = required_decimal_field(data, "reward_account_dogs_count", path)
    snapshot = RewardStreamSnapshot(
        snapshot_utc=str(data.get("snapshot_utc") or "").strip(),
        dogs_count=dogs_count,
        woof_received=optional_decimal_field(data, "account_woof_received", path),
        woof_flow_per_day=required_decimal_field(data, "account_woof_flow_per_day", path),
        sup_received=optional_decimal_field(data, "account_sup_received", path),
        sup_flow_per_day=required_decimal_field(data, "account_sup_flow_per_day", path),
        basis_source=str(data.get("basis_source") or "observed_stream_snapshot").strip(),
        note=str(data.get("note") or "").strip(),
    )
    if not snapshot.snapshot_utc:
        raise ValueError(f"{path.relative_to(ROOT)} missing snapshot_utc")
    if not snapshot.basis_source:
        raise ValueError(f"{path.relative_to(ROOT)} missing basis_source")
    validate_derived_reward_value(data, "derived_woof_per_dog_per_day", snapshot.woof_per_dog_per_day, path)
    validate_derived_reward_value(data, "derived_sup_per_dog_per_day", snapshot.sup_per_dog_per_day, path)
    return snapshot


SEASON6_SUP_CONFIG_PATH = ROOT / "config" / "season6_sup_rewards.json"


@dataclass(frozen=True)
class Season6SupConfig:
    enabled: bool = True
    sup_token: str = SUP.lower()
    season_start_utc: str = "2026-06-02T00:00:00Z"
    season_end_utc: str = "2026-09-01T00:00:00Z"
    total_sup: Decimal = Decimal("251340")
    cap_sup: Decimal = Decimal("12500")
    xp_per_settled_win: Decimal = Decimal("100")
    reward_start_delay_days: int = 0
    cap_level: str = "wallet_estimate"
    projection_model: str = "time_weighted_xp_with_expected_future_daily_auctions"
    expected_future_settlement_interval_seconds: int = 86400
    visible_dashboard_mode: str = "compact_final_estimate_only"
    cap_percent_label: str = "5% cap"
    cap_overflow_policy: str = "no_redistribution_assumed"

    @property
    def xp_start_utc(self) -> str:
        return self.season_start_utc

    @property
    def reward_start_utc(self) -> str:
        if self.reward_start_delay_days == 0:
            return self.season_start_utc
        start = parse_utc_datetime(self.season_start_utc)
        if start is None:
            return self.season_start_utc
        return iso_utc_z(start + timedelta(days=self.reward_start_delay_days))

    @property
    def campaign_end_utc(self) -> str:
        return self.season_end_utc


def config_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def config_decimal(data: dict[str, Any], key: str, default: Decimal) -> Decimal:
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return Decimal(str(raw).replace(",", ""))


def config_int(data: dict[str, Any], key: str, default: int) -> int:
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def load_season6_sup_config(path: Path = SEASON6_SUP_CONFIG_PATH) -> Season6SupConfig:
    default = Season6SupConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    if not isinstance(data, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return Season6SupConfig(
        enabled=config_bool(data.get("enabled"), default.enabled),
        sup_token=str(data.get("sup_token") or default.sup_token).lower(),
        season_start_utc=str(data.get("season_start_utc") or default.season_start_utc),
        season_end_utc=str(data.get("season_end_utc") or default.season_end_utc),
        total_sup=config_decimal(data, "total_allocation_sup", default.total_sup),
        cap_sup=config_decimal(data, "wallet_cap_sup", default.cap_sup),
        xp_per_settled_win=config_decimal(data, "xp_per_settled_win", default.xp_per_settled_win),
        reward_start_delay_days=config_int(data, "reward_start_delay_days", default.reward_start_delay_days),
        cap_level=str(data.get("cap_level") or default.cap_level),
        projection_model=str(data.get("projection_model") or default.projection_model),
        expected_future_settlement_interval_seconds=config_int(
            data,
            "expected_future_settlement_interval_seconds",
            default.expected_future_settlement_interval_seconds,
        ),
        visible_dashboard_mode=str(data.get("visible_dashboard_mode") or default.visible_dashboard_mode),
        cap_percent_label=str(data.get("cap_percent_label") or default.cap_percent_label),
        cap_overflow_policy=str(data.get("cap_overflow_policy") or default.cap_overflow_policy),
    )


SEASON6_SUP_CONFIG = load_season6_sup_config()

SEASON6_BY_WINNER_COLUMNS = [
    "winner_wallet",
    "winner_display",
    "winner_url",
    "farcaster_username",
    "season6_wins_confirmed",
    "season6_xp_confirmed",
    "season6_raw_sup_earned_to_date",
    "season6_raw_sup_projected_full",
    "season6_capped_sup_projected_full",
    "season6_cap_sup",
    "season6_cap_remaining_sup",
    "season6_cap_limited",
    "season6_raw_usd_earned_to_date",
    "season6_raw_usd_projected_full",
    "season6_capped_usd_projected_full",
    "first_s6_win_time_utc",
    "latest_s6_win_time_utc",
    "season6_wallet_note",
    "season6_token_ids",
]
SEASON6_REWARDS_BY_AUCTION_COLUMNS = [
    "auction_id",
    "token_id",
    "dog",
    "winner_wallet",
    "winner_display",
    "winner_url",
    "farcaster_username",
    "settled_time_utc",
    "winning_bid_eth",
    "winning_bid_usd",
    "season6_xp",
    "season6_raw_sup_earned_to_date",
    "season6_raw_sup_projected_full",
    "season6_capped_sup_projected_full",
    "season6_raw_usd_earned_to_date",
    "season6_raw_usd_projected_full",
    "season6_capped_usd_projected_full",
    "cap_limited_by_wallet",
]
SEASON6_CURRENT_BIDDER_STATUS_COLUMNS = [
    "current_auction_token_id",
    "current_bidder_wallet",
    "current_bidder_display",
    "current_bid_eth",
    "current_bid_usd",
    "current_auction_end_utc",
    "prior_s6_wins_confirmed",
    "prior_s6_xp_confirmed",
    "prior_s6_raw_sup_projected_full",
    "prior_s6_capped_sup_projected_full",
    "prior_s6_cap_remaining_sup",
    "projected_s6_wins_if_current_bid_wins",
    "projected_s6_xp_if_current_bid_wins",
    "projected_raw_sup_if_current_bid_wins",
    "projected_capped_sup_if_current_bid_wins",
    "projected_cap_remaining_sup_if_current_bid_wins",
    "projected_raw_usd_if_current_bid_wins",
    "projected_capped_usd_if_current_bid_wins",
    "projected_total_without_current_win_sup",
    "projected_total_with_current_win_sup",
    "estimated_raw_incremental_sup",
    "estimated_cap_aware_incremental_sup",
    "estimated_cap_aware_incremental_usd",
    "cap_remaining_before_current_win_sup",
    "future_dilution_enabled",
    "expected_future_settlement_interval_seconds",
    "current_bidder_cap_status",
    "estimate_status",
    "projection_note",
]

OUTPUT_TABLES = [
    "mission3_metrics",
    "auction_feed",
    "historical_dog_search",
    "historical_dog_report",
    "current_latest_bid",
    "current_auction_bid_history",
    "recent_auction_winners",
    "current_auction",
    "auction_timeline",
    "auction_extensions",
    "auction_daily_activity",
    "auction_bidder_leaderboard",
    "season5_sup_by_winner",
    "season5_sup_rewards_by_auction",
    "season6_metrics",
    "season6_sup_by_winner",
    "season6_sup_rewards_by_auction",
    "season6_sup_current_bidder_status",
    "auction_winners",
    "recent_bids",
    "top_woof_holders",
]
PRIMARY_TABLES = ["auction_feed"]

DATASET_DESCRIPTIONS = {
    "mission3_metrics": "Key dashboard metrics, refresh metadata, and verified contract snapshot values.",
    "auction_feed": "Homepage-ready current auction plus recent settled auctions.",
    "historical_dog_search": "Combined all-mission Dog lookup with one hosted row per current Dog token ID and searchable hidden metadata.",
    "historical_dog_report": "Mission-level coverage report for the combined historical Dog lookup.",
    "current_latest_bid": "Current auction latest bid and high-bidder snapshot.",
    "current_auction_bid_history": "All decoded bid events for the current ongoing auction with Farcaster identity and live USD estimates.",
    "recent_auction_winners": "Recent settled winners formatted for the homepage.",
    "current_auction": "Full current auction state, dog metadata, rarity, and countdown fields.",
    "auction_timeline": "One row per auction with bid, winner, settlement, and effective extended-end-time summary.",
    "auction_extensions": "Verified AuctionExtended events used to derive canonical auction end times.",
    "auction_daily_activity": "Daily auction counts, settlement counts, and bid/settlement volume.",
    "auction_bidder_leaderboard": "Ranked bidder activity across decoded auction events.",
    "season5_sup_by_winner": "Estimated Season 5 SUP rewards grouped by winning wallet/profile.",
    "season5_sup_rewards_by_auction": "Estimated Season 5 SUP rewards per auction.",
    "season6_metrics": "Season 6 SUP reward configuration, projection totals, pricing, and current bidder metrics.",
    "season6_sup_by_winner": "Time-sliced Season 6 SUP projections grouped by winning wallet/profile.",
    "season6_sup_rewards_by_auction": "Season 6 settled Dog win rows with XP and wallet-level SUP projection context.",
    "season6_sup_current_bidder_status": "Current high bidder Season 6 SUP status plus hypothetical projection if the current bid wins.",
    "auction_winners": "Settled auction winners with bid values and identity fields.",
    "recent_bids": "Latest bid events decoded from the auction house.",
    "top_woof_holders": "WOOF holder snapshot from transfer participants and balance checks.",
}

CONFIGURATION_ENV_VARS = [
    ("BASE_RPC_URL", "Single Base RPC endpoint for contract calls; also overrides log RPC lists when set."),
    ("BASE_RPC_URLS", "Comma-separated fallback Base RPC endpoints for contract calls."),
    ("BASE_LOG_RPC_URLS", "Comma-separated Base RPC endpoints used for `eth_getLogs` history scans."),
    ("BASE_INCLUDE_PUBLIC_FALLBACKS", "Opt in to public RPC fallbacks when explicit provider URLs are configured; defaults off for explicit production configurations."),
    ("BASE_RPC_QUORUM_SIZE", "Minimum independently operated RPC providers that must agree; clamped to two or three."),
    ("BASE_SNAPSHOT_CONFIRMATIONS", "Confirmed blocks subtracted from the agreed provider head before the snapshot is hash-pinned."),
    ("BASE_FROM_BLOCK", "First Base block scanned for Mission 3 logs; defaults to the known Mission 3 start range."),
    ("BASE_LOG_CHUNK", "Maximum block range per eth_getLogs request; defaults to Base's reliable 2,000-block recommendation and is capped at 10,000."),
    ("BASE_LOG_WORKERS", "Concurrent log-fetch workers, capped by the builder to avoid public RPC overload."),
    ("BASE_RPC_BATCH_LIMIT", "Maximum JSON-RPC batch size for balance/metadata calls, capped at 10."),
    ("BASE_TOKEN_URI_CHUNK_DELAY_SECONDS", "Minimum spacing between cross-provider exists/tokenURI batches; defaults to one second to avoid burst throttling."),
    ("BASE_RPC_MAX_RESPONSE_BYTES", "Maximum accepted JSON-RPC response size; defaults to 32 MiB and is capped at 64 MiB."),
    ("BASE_RPC_ATTEMPTS", "Maximum attempts per JSON-RPC request before failing over/failing fast."),
    ("BASE_RPC_QUORUM_DEADLINE_SECONDS", "Hard wall-clock deadline for a cross-provider quorum call."),
    ("BASE_RPC_HEAD_PROBE_DEADLINE_SECONDS", "Hard wall-clock deadline for snapshot endpoint discovery."),
    ("BASE_RPC_HEAD_PROBE_GRACE_SECONDS", "Small grace window for extra healthy providers after the minimum snapshot quorum responds."),
    ("BASE_RPC_SLOW_COOLDOWN_SECONDS", "In-process circuit-breaker cooldown for endpoints left pending after quorum."),
    ("BASE_RPC_MAX_HEAD_SPREAD_BLOCKS", "Maximum block spread allowed inside the independently operated RPC head quorum."),
    ("BASE_RPC_MAX_BLOCK_AGE_SECONDS", "Maximum age of the hash-agreed snapshot block before publication fails closed."),
    ("BASE_LOG_RPC_TIMEOUT", "Per-attempt timeout for eth_getLogs requests."),
    ("BASE_BLOCK_TIME_RPC_TIMEOUT", "Per-attempt timeout for block timestamp batch lookups."),
    ("DOG_METADATA_WORKERS", "Concurrent Dog metadata fetch workers, capped by the builder."),
    ("DOG_METADATA_FETCH_TIMEOUT", "Primary per-request metadata HTTP timeout in seconds."),
    ("DOG_METADATA_FALLBACK_TIMEOUT", "Fallback tokenURI metadata HTTP timeout in seconds."),
    ("DOG_METADATA_ITEM_TIMEOUT", "Hard wall-clock timeout per new Dog metadata row."),
    ("DOG_METADATA_SEQUENTIAL_THRESHOLD", "Fetch small missing metadata batches sequentially so one hung request cannot trap the runner."),
    ("DOG_METADATA_CACHE_MAX_AGE_SECONDS", "Maximum reuse age for offchain metadata whose onchain tokenURI is unchanged; defaults to 24 hours."),
    ("MISSION3_LOG_CACHE", "Enables local RPC log caching under `.cache/rpc_logs`; defaults on."),
    ("MISSION3_LOG_CACHE_OVERLAP_BLOCKS", "Re-fetch overlap when extending cached log ranges; defaults to 100 blocks."),
    ("MISSION3_LOG_QUORUM_MAX_BLOCKS", "Maximum block span per recent cross-provider eth_getLogs request; defaults to 50 for public-fallback compatibility."),
    ("MISSION3_LOG_QUORUM_WINDOW_BLOCKS", "Maximum total recent window split into quorum-checked log requests; defaults to 500."),
    ("MISSION3_BALANCE_CACHE", "Enables local WOOF holder balance caching under `.cache/woof_balances.json`; defaults on."),
    ("WOOF_HOLDER_DISCOVERY_URL", "Blockscout Base token-holder endpoint used only to discover candidate WOOF addresses; host and path are pinned, while balances and completeness remain quorum-verified onchain."),
    ("NEYNAR_API_KEY", "Optional Neynar API key for identity resolution."),
    ("WOOF_USD_PRICE", "Optional manual WOOF/USD override; otherwise fetched from Dexscreener Base pools."),
    ("SUP_USD_PRICE", "Optional manual SUP/USD override; otherwise fetched from Dexscreener Base pools."),
]


def progress(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[build_dashboard {stamp}] {message}", file=sys.stderr, flush=True)


TOPIC_AUCTION_BID = "0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3"
TOPIC_AUCTION_CREATED = "0xd6eddd1118d71820909c1197aa966dbc15ed6f508554252169cc3d5ccac756ca"
TOPIC_AUCTION_EXTENDED = "0x6e912a3a9105bdd2af817ba5adc14e6c127c1035b5b648faa29ca0d58ab8ff4e"
TOPIC_AUCTION_SETTLED = "0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

SELECTOR_NAME = "0x06fdde03"
SELECTOR_SYMBOL = "0x95d89b41"
SELECTOR_DECIMALS = "0x313ce567"
SELECTOR_TOTAL_SUPPLY = "0x18160ddd"
SELECTOR_AUCTION = "0x7d9f6db5"
SELECTOR_BALANCE_OF = "0x70a08231"
SELECTOR_TOKEN_URI = "0xc87b56dd"
SELECTOR_EXISTS = "0x4f558e79"
# OpenZeppelin ERC721NonexistentToken(uint256). Only this exact revert, with
# the requested token ID encoded in its data, is accepted as an onchain proof
# that a sparse/burned token currently has no tokenURI binding.
ERC721_NONEXISTENT_TOKEN_ERROR = "0x7e273289"
TOKEN_URI_CHUNK_WORKERS = 1
TOKEN_URI_CHUNK_DELAY_SECONDS = max(
    0.0,
    min(float(os.environ.get("BASE_TOKEN_URI_CHUNK_DELAY_SECONDS", "1.0")), 5.0),
)
DOG_RARITY_TRAIT_TYPES = (
    "Background",
    "Body",
    "Neck",
    "Mouth",
    "Ears",
    "Head",
    "Eyes",
)


def read_bounded_json_response(response: Any, limit: int, label: str) -> Any:
    headers = getattr(response, "headers", None)
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() if headers else ""
    if content_type and content_type != "application/json" and not content_type.endswith("+json"):
        raise RuntimeError(f"{label} returned unsafe content type {content_type}")
    content_length = headers.get("Content-Length") if headers else None
    if content_length is not None:
        try:
            parsed_length = int(content_length)
        except (TypeError, ValueError):
            raise RuntimeError(f"{label} returned an invalid Content-Length") from None
        if parsed_length < 0:
            raise RuntimeError(f"{label} returned an invalid Content-Length")
        if parsed_length > limit:
            raise RuntimeError(f"{label} response exceeds {limit} bytes")
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise RuntimeError(f"{label} response exceeds {limit} bytes")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc


def read_owned_json_file(path: Path, limit: int, label: str) -> Any | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"refusing unsafe {label} file {path}: {exc}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise RuntimeError(f"refusing {label} file that is not an owned regular file: {path}")
        if details.st_size > limit:
            raise RuntimeError(f"{label} file exceeds {limit} bytes: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(limit + 1)
        if len(payload) > limit:
            raise RuntimeError(f"{label} file exceeds {limit} bytes: {path}")
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} file contains invalid JSON: {path}") from exc
    finally:
        os.close(descriptor)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed before urllib can forward RPC or API credentials."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"redirect to {urllib.parse.urlsplit(newurl).hostname or 'unknown host'} rejected",
            headers,
            fp,
        )


def open_no_redirect(req: urllib.request.Request, *, timeout: int) -> Any:
    return urllib.request.build_opener(NoRedirectHandler()).open(req, timeout=timeout)


def validate_rpc_url(url: str) -> None:
    if not isinstance(url, str) or not url or any(character.isspace() for character in url):
        raise RuntimeError("RPC endpoint URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("RPC endpoint URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError("RPC endpoint must use HTTPS on port 443 without userinfo or a fragment")


def post_json(payload: dict[str, Any] | list[dict[str, Any]], timeout: int, url: str) -> Any:
    validate_rpc_url(url)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "degen-dogs-mission3-builder/1.0",
        },
        method="POST",
    )
    try:
        response = open_no_redirect(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 - never expose credential-bearing URLs in transport errors
        raise RuntimeError(f"RPC transport failed ({type(exc).__name__})") from None
    try:
        with response:
            status = response.getcode() if hasattr(response, "getcode") else getattr(response, "status", None)
            if status != 200:
                raise RuntimeError("RPC response returned an unexpected HTTP status")
            if str(response.geturl()) != url:
                raise RuntimeError("RPC response URL changed unexpectedly")
            headers = getattr(response, "headers", None)
            content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() if headers else ""
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise RuntimeError("RPC response returned an unexpected content type")
            return read_bounded_json_response(response, RPC_MAX_RESPONSE_BYTES, "JSON-RPC")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep provider-controlled read details out of logs
        raise RuntimeError(f"RPC response read failed ({type(exc).__name__})") from None


def _redact_rpc_url(url: str) -> str:
    """Return enough endpoint identity for diagnostics without leaking keys."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port_number = parsed.port
    except (TypeError, ValueError):
        return "<redacted-url>"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "<redacted-url>"
    if host not in PUBLIC_RPC_HOSTNAMES:
        host = f"rpc-host-{hashlib.sha256(host.encode('utf-8')).hexdigest()[:12]}"
    port = f":{port_number}" if port_number else ""
    return f"https://{host}{port}"


def _rpc_provider_key(url: str) -> str:
    """Group multiple endpoints run by one operator as one quorum vote."""
    host = (urllib.parse.urlsplit(url).hostname or url).lower().strip(".")
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ("quicknode.pro", "quiknode.pro")):
        return "quicknode"
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ("alchemy.com", "alchemyapi.io", "blastapi.io")):
        return "alchemy"
    known_operators = (
        "base.org",
        "publicnode.com",
        "ankr.com",
        "drpc.org",
        "infura.io",
        "1rpc.io",
    )
    for suffix in known_operators:
        if host == suffix or host.endswith(f".{suffix}"):
            return suffix
    return f"rpc-host-{hashlib.sha256(host.encode('utf-8')).hexdigest()[:12]}"


def _configured_rpc_urls() -> list[str]:
    configured: list[str] = []
    if os.environ.get("BASE_RPC_URL"):
        configured.append(os.environ["BASE_RPC_URL"].strip())
    configured.extend(
        url.strip()
        for url in os.environ.get("BASE_RPC_URLS", "").split(",")
        if url.strip()
    )
    configured.extend(RPC_URLS)
    if INCLUDE_PUBLIC_RPC_FALLBACKS:
        configured.extend(DEFAULT_RPC_URLS)
    unique: list[str] = []
    seen: set[str] = set()
    for url in configured:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _independent_rpc_urls(configured: list[str]) -> list[str]:
    """Use at most one vote per RPC operator, preserving configured priority."""
    unique: list[str] = []
    operators: set[str] = set()
    for url in configured:
        if not url:
            continue
        operator = _rpc_provider_key(url)
        if operator in operators:
            continue
        operators.add(operator)
        unique.append(url)
    return unique


def _quorum_rpc_urls() -> list[str]:
    return _independent_rpc_urls(_configured_rpc_urls())


def _same_operator_rpc_urls(primary_url: str) -> list[str]:
    """Retain same-operator endpoints as failovers without extra quorum votes."""
    operator = _rpc_provider_key(primary_url)
    candidates = [primary_url, *_configured_rpc_urls(), *LOG_RPC_URLS]
    if INCLUDE_PUBLIC_RPC_FALLBACKS:
        candidates.extend(DEFAULT_LOG_RPC_URLS)
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen or _rpc_provider_key(candidate) != operator:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _responsive_rpc_urls(urls: list[str], required: int, scope: str) -> list[str]:
    now = time.monotonic()
    responsive = [url for url in urls if RPC_SLOW_UNTIL.get((scope, url), 0.0) <= now]
    # A fail-closed quorum cannot tolerate a single transient failure when the
    # circuit breaker trims a healthy qualified pool down to exactly the vote
    # minimum. Keep at least one spare; otherwise reintroduce cooled endpoints
    # and let the bounded concurrent quorum decide from fresh evidence.
    return responsive if len(responsive) >= required + 1 else list(urls)


def _mark_rpc_pending_slow(pending_urls: list[str], scope: str) -> None:
    until = time.monotonic() + RPC_SLOW_COOLDOWN_SECONDS
    for url in pending_urls:
        RPC_SLOW_UNTIL[(scope, url)] = until


def _rpc_once(url: str, method: str, params: list[Any], *, timeout: int = 30) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = post_json(payload, timeout, url)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected JSON-RPC response type for {method}")
    if data.get("jsonrpc") != "2.0" or type(data.get("id")) is not int or data.get("id") != 1:
        raise RuntimeError(f"mismatched JSON-RPC envelope for {method}")
    has_result = "result" in data
    has_error = "error" in data
    if has_result == has_error:
        raise RuntimeError(f"JSON-RPC response must contain exactly one of result or error for {method}")
    if has_error:
        error = data.get("error")
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise RuntimeError(f"malformed JSON-RPC error envelope for {method}")
        raise RuntimeError(f"JSON-RPC error code={error['code']} for {method}")
    return data["result"]


def _rpc_once_with_retry(url: str, method: str, params: list[Any], *, timeout: int = 30) -> Any:
    operator_urls = _same_operator_rpc_urls(url)
    attempts = max(len(operator_urls), max(1, min(RPC_ATTEMPTS, 4)))
    for attempt in range(attempts):
        candidate = operator_urls[attempt % len(operator_urls)]
        try:
            return _rpc_once(candidate, method, params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            permanent = any(code in message for code in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "-32600", "-32601", "-32602"))
            # Authentication/parameter failures are endpoint-specific when an
            # operator exposes more than one configured URL, so try each alias
            # once before declaring that operator unavailable.
            if permanent and attempt + 1 >= len(operator_urls):
                raise
            if attempt == attempts - 1:
                raise
            # Full jitter prevents both providers and overlapping runners from
            # retrying in lockstep during a transient outage or rate limit.
            time.sleep(random.uniform(0, min(2.0, 0.25 * (2**attempt))))
    raise RuntimeError(f"unreachable retry state for {method}")


def _canonical_rpc_result(method: str, value: Any) -> str:
    if method == "eth_getBlockByNumber" and isinstance(value, dict):
        # Providers may append nonstandard block fields. Quorum only over the
        # canonical identity and timestamp that the builder publishes. JSON-
        # RPC quantities are integers, so harmless leading-zero or hex-case
        # differences must not split an otherwise exact provider quorum.
        def canonical_quantity(raw: Any) -> str:
            quantity = str(raw or "").strip()
            if (
                len(quantity) >= 3
                and quantity[:2].lower() == "0x"
                and all(character in "0123456789abcdefABCDEF" for character in quantity[2:])
            ):
                return str(int(quantity[2:], 16))
            return "invalid:" + quantity.lower()

        return json.dumps(
            {
                "hash": str(value.get("hash") or "").lower(),
                "number": canonical_quantity(value.get("number")),
                "timestamp": canonical_quantity(value.get("timestamp")),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    if method == "eth_getLogs" and isinstance(value, list):
        normalized = sorted(
            (
                {
                    "address": str(item.get("address") or "").lower(),
                    "blockHash": str(item.get("blockHash") or "").lower(),
                    "blockNumber": str(item.get("blockNumber") or "").lower(),
                    "data": str(item.get("data") or "").lower(),
                    "logIndex": str(item.get("logIndex") or "").lower(),
                    "removed": bool(item.get("removed", False)),
                    "topics": [str(topic).lower() for topic in item.get("topics") or []],
                    "transactionHash": str(item.get("transactionHash") or "").lower(),
                    "transactionIndex": str(item.get("transactionIndex") or "").lower(),
                }
                for item in value
                if isinstance(item, dict)
            ),
            key=lambda item: (
                str(item.get("blockHash") or "").lower(),
                str(item.get("transactionHash") or "").lower(),
                int(str(item.get("logIndex") or "0x0"), 16),
            ),
        )
        return json.dumps(normalized, sort_keys=True, separators=(",", ":")).lower()
    if isinstance(value, str):
        return value.lower()
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def rpc_quorum(
    method: str,
    params: list[Any],
    *,
    urls: list[str] | None = None,
    min_agreement: int | None = None,
    timeout: int = 30,
) -> tuple[Any, list[str]]:
    """Require identical answers from independently operated RPC endpoints."""
    active_urls = _responsive_rpc_urls(
        urls or _quorum_rpc_urls(),
        min_agreement or RPC_QUORUM_SIZE,
        method,
    )
    required = min_agreement or RPC_QUORUM_SIZE
    if len(active_urls) < required:
        raise RuntimeError(
            f"{method} requires {required} independent Base RPC providers; configured={len(active_urls)}"
        )
    grouped: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    errors: list[str] = []
    responses: queue.Queue[tuple[int, str, Any, Exception | None]] = queue.Queue()

    def worker(index: int, url: str) -> None:
        try:
            value = _rpc_once_with_retry(url, method, params, timeout=timeout)
            responses.put((index, url, value, None))
        except Exception as exc:  # noqa: BLE001
            responses.put((index, url, None, exc))

    pending_indexes = set(range(len(active_urls)))
    for index, url in enumerate(active_urls):
        threading.Thread(
            target=worker,
            args=(index, url),
            name=f"rpc-quorum-{method}-{index}",
            daemon=True,
        ).start()

    deadline = time.monotonic() + RPC_QUORUM_DEADLINE_SECONDS
    while pending_indexes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            index, url, value, error = responses.get(timeout=remaining)
        except queue.Empty:
            break
        if index not in pending_indexes:
            continue
        pending_indexes.remove(index)
        if error is None:
            grouped[_canonical_rpc_result(method, value)].append((url, value))
        else:
            errors.append(f"{_redact_rpc_url(url)}: {error}")
        pending = len(pending_indexes)
        ordered = sorted(grouped.values(), key=len, reverse=True)
        winner = ordered[0] if ordered else []
        runner_up_votes = len(ordered[1]) if len(ordered) > 1 else 0
        # Return only when no pending result can tie or overtake the winner.
        if len(winner) >= required and len(winner) > runner_up_votes + pending:
            _mark_rpc_pending_slow([active_urls[item] for item in pending_indexes], method)
            return winner[0][1], [winner_url for winner_url, _value in winner]

    if pending_indexes:
        pending_urls = [active_urls[item] for item in pending_indexes]
        _mark_rpc_pending_slow(pending_urls, method)
        errors.append(
            "deadline exceeded: "
            + ", ".join(_redact_rpc_url(url) for url in pending_urls[:3])
        )

    vote_sizes = sorted((len(group) for group in grouped.values()), reverse=True)
    detail = f" votes={vote_sizes}" if vote_sizes else ""
    if errors:
        detail += f" errors={'; '.join(errors[:3])}"
    raise RuntimeError(f"{method} RPC quorum disagreement: required={required}{detail}")


def _collect_rpc_probes(
    urls: list[str],
    *,
    required: int,
    preferred: int | None = None,
    probe: Callable[[str], Any],
    label: str,
) -> tuple[list[Any], list[str]]:
    """Collect a minimum probe set and briefly wait for an optional spare."""
    scope = f"probe:{label}"
    active_urls = _responsive_rpc_urls(urls, required, scope)
    preferred_target = (
        None
        if preferred is None
        else max(required, min(len(active_urls), preferred))
    )
    responses: queue.Queue[tuple[int, str, Any, Exception | None]] = queue.Queue()

    def worker(index: int, url: str) -> None:
        try:
            responses.put((index, url, probe(url), None))
        except Exception as exc:  # noqa: BLE001
            responses.put((index, url, None, exc))

    pending_indexes = set(range(len(active_urls)))
    results: list[Any] = []
    errors: list[str] = []
    for index, url in enumerate(active_urls):
        threading.Thread(
            target=worker,
            args=(index, url),
            name=f"rpc-probe-{label}-{index}",
            daemon=True,
        ).start()

    hard_deadline = time.monotonic() + RPC_HEAD_PROBE_DEADLINE_SECONDS
    quorum_deadline: float | None = None
    while pending_indexes:
        now = time.monotonic()
        if preferred_target is not None and len(results) >= preferred_target:
            break
        if len(results) >= required and quorum_deadline is None:
            quorum_deadline = now + RPC_HEAD_PROBE_GRACE_SECONDS
        deadline = min(hard_deadline, quorum_deadline) if quorum_deadline is not None else hard_deadline
        remaining = deadline - now
        if remaining <= 0:
            break
        try:
            index, url, value, error = responses.get(timeout=remaining)
        except queue.Empty:
            break
        if index not in pending_indexes:
            continue
        pending_indexes.remove(index)
        if error is None:
            results.append(value)
        else:
            errors.append(f"{_redact_rpc_url(url)}: {error}")

    if pending_indexes:
        pending_urls = [active_urls[item] for item in pending_indexes]
        _mark_rpc_pending_slow(pending_urls, scope)
        errors.append(
            f"{label} probe deadline exceeded: "
            + ", ".join(_redact_rpc_url(url) for url in pending_urls[:3])
        )
    return results, errors


def verified_snapshot() -> tuple[int, dict[str, Any], dict[str, str]]:
    """Choose a hash-agreed Base block and verify critical contract code.

    The snapshot deliberately trails the fastest observed head by a small,
    configurable confirmation margin. Every published current-auction read is
    pinned to this block and checked by at least two independent RPC operators.
    """
    global VERIFIED_LOG_URLS, VERIFIED_SNAPSHOT_URLS

    urls = _quorum_rpc_urls()
    required = RPC_QUORUM_SIZE
    if len(urls) < required:
        raise RuntimeError(
            f"production snapshot requires {required} independent Base RPC providers; configured={len(urls)}"
        )

    def endpoint_head(url: str) -> tuple[str, int]:
        chain_id = int(str(_rpc_once_with_retry(url, "eth_chainId", [], timeout=20)), 16)
        if chain_id != 8453:
            raise RuntimeError(f"wrong chain id {chain_id}; expected 8453")
        head = int(str(_rpc_once_with_retry(url, "eth_blockNumber", [], timeout=20)), 16)
        return url, head

    heads, errors = _collect_rpc_probes(
        urls,
        required=required,
        probe=endpoint_head,
        label="head",
    )
    if len(heads) < required:
        raise RuntimeError(
            f"Base RPC head quorum unavailable: healthy={len(heads)} required={required}; "
            + "; ".join(errors[:3])
        )
    ordered_pairs = sorted(heads, key=lambda item: item[1], reverse=True)
    head_cluster: list[tuple[str, int]] = []
    for _anchor_url, anchor_head in ordered_pairs:
        candidate = [
            (url, head)
            for url, head in ordered_pairs
            if anchor_head - RPC_MAX_HEAD_SPREAD_BLOCKS <= head <= anchor_head
        ]
        if len(candidate) >= required:
            head_cluster = candidate
            break
    if len(head_cluster) < required:
        redacted = ", ".join(f"{_redact_rpc_url(url)}={head}" for url, head in ordered_pairs)
        raise RuntimeError(
            f"Base RPC heads cannot form a recent quorum within {RPC_MAX_HEAD_SPREAD_BLOCKS} blocks: {redacted}"
        )
    ordered_heads = sorted((head for _url, head in head_cluster), reverse=True)
    quorum_head = ordered_heads[required - 1]
    snapshot_block = max(0, quorum_head - SNAPSHOT_CONFIRMATIONS)
    eligible_urls = [url for url, head in head_cluster if head >= snapshot_block]
    block_data, agreeing_urls = rpc_quorum(
        "eth_getBlockByNumber",
        [hex(snapshot_block), False],
        urls=eligible_urls,
        min_agreement=required,
        timeout=30,
    )
    if not isinstance(block_data, dict) or not block_data.get("hash"):
        raise RuntimeError(f"Base snapshot block {snapshot_block} missing hash")
    try:
        block_number = int(str(block_data.get("number") or ""), 16)
        block_timestamp = int(str(block_data.get("timestamp") or ""), 16)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Base snapshot block {snapshot_block} has malformed number/timestamp") from exc
    if block_number != snapshot_block:
        raise RuntimeError(
            f"Base snapshot response number mismatch expected={snapshot_block} observed={block_number}"
        )
    block_age = time.time() - block_timestamp
    if block_age < -60 or block_age > RPC_MAX_BLOCK_AGE_SECONDS:
        raise RuntimeError(
            f"Base snapshot block {snapshot_block} timestamp is outside the freshness window: age_seconds={block_age:.0f}"
        )

    expected_block_hash = str(block_data["hash"]).lower()
    snapshot_state_tag = {"blockHash": expected_block_hash, "requireCanonical": True}

    def contract_code_sha256(raw_code: Any, label: str) -> str:
        code = str(raw_code or "")
        encoded = code[2:] if code.startswith("0x") else ""
        if (
            not encoded
            or len(encoded) % 2 != 0
            or any(character not in "0123456789abcdefABCDEF" for character in encoded)
        ):
            raise RuntimeError(f"{label} returned malformed or empty contract code")
        return hashlib.sha256(bytes.fromhex(encoded)).hexdigest()

    code_hashes: dict[str, str] = {}
    for label, address in (("auction_house", AUCTION_HOUSE), ("dog_nft", DEGEN_DOGS)):
        code, _code_urls = rpc_quorum(
            "eth_getCode",
            [address, snapshot_state_tag],
            # Every endpoint must resolve the already selected block hash, so
            # do not prematurely restrict code discovery to the two providers
            # that happened to finish the initial block quorum first.
            urls=eligible_urls,
            min_agreement=required,
            timeout=30,
        )
        code_hashes[f"{label}_code_sha256"] = contract_code_sha256(code, label)

    def endpoint_snapshot_qualification(url: str) -> str:
        candidate_block = _rpc_once_with_retry(
            url,
            "eth_getBlockByNumber",
            [hex(snapshot_block), False],
            timeout=20,
        )
        if not isinstance(candidate_block, dict):
            raise RuntimeError("snapshot qualification returned a non-object block")
        try:
            candidate_number = int(str(candidate_block.get("number") or ""), 16)
            candidate_timestamp = int(str(candidate_block.get("timestamp") or ""), 16)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("snapshot qualification returned malformed block quantities") from exc
        candidate_hash = canonical_block_hash(candidate_block.get("hash"))
        if (
            candidate_number != snapshot_block
            or candidate_hash != expected_block_hash
            or candidate_timestamp != block_timestamp
        ):
            raise RuntimeError("snapshot qualification block identity mismatch")
        for label, address in (("auction_house", AUCTION_HOUSE), ("dog_nft", DEGEN_DOGS)):
            candidate_code = _rpc_once_with_retry(
                url,
                "eth_getCode",
                [address, snapshot_state_tag],
                timeout=20,
            )
            if contract_code_sha256(candidate_code, label) != code_hashes[f"{label}_code_sha256"]:
                raise RuntimeError(f"snapshot qualification {label} code mismatch")
        return url

    # Run every eligible independent endpoint concurrently and retain every
    # responder that proves the exact selected block identity and both exact
    # bytecodes. Passing the candidate count as the collector threshold makes
    # this a bounded all-candidate probe rather than stopping at the first two.
    qualified_snapshot_urls, qualification_errors = _collect_rpc_probes(
        eligible_urls,
        required=len(eligible_urls),
        probe=endpoint_snapshot_qualification,
        label="snapshot-qualification",
    )
    qualified_snapshot_urls = _independent_rpc_urls(qualified_snapshot_urls)
    if len(qualified_snapshot_urls) < required:
        raise RuntimeError(
            f"Base RPC snapshot qualification unavailable: healthy={len(qualified_snapshot_urls)} "
            f"required={required}; " + "; ".join(qualification_errors[:3])
        )
    VERIFIED_SNAPSHOT_URLS = qualified_snapshot_urls

    def endpoint_log_capability(url: str) -> str:
        chain_id = int(str(_rpc_once_with_retry(url, "eth_chainId", [], timeout=15)), 16)
        if chain_id != 8453:
            raise RuntimeError(f"wrong chain id {chain_id}; expected 8453")
        candidate_block = _rpc_once_with_retry(
            url,
            "eth_getBlockByNumber",
            [hex(snapshot_block), False],
            timeout=20,
        )
        candidate_hash = str((candidate_block or {}).get("hash") or "").lower() if isinstance(candidate_block, dict) else ""
        if candidate_hash != expected_block_hash:
            raise RuntimeError(
                f"snapshot hash mismatch expected={expected_block_hash} observed={candidate_hash or 'missing'}"
            )
        # A provider that serves the hot snapshot and recent logs can still be
        # a pruned full node. Prove that every log-quorum member can read the
        # oldest block this dashboard may need before using it for historical
        # event timestamps.
        if FROM_BLOCK < snapshot_block:
            archive_probe_number = FROM_BLOCK
            archive_block = _rpc_once_with_retry(
                url,
                "eth_getBlockByNumber",
                [hex(archive_probe_number), False],
                timeout=BLOCK_TIME_RPC_TIMEOUT,
            )
            if not isinstance(archive_block, dict):
                raise RuntimeError("historical block capability check returned a non-object")
            try:
                archive_number = int(str(archive_block.get("number") or ""), 16)
                archive_timestamp = int(str(archive_block.get("timestamp") or ""), 16)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("historical block capability check returned malformed quantities") from exc
            if (
                archive_number != archive_probe_number
                or not canonical_block_hash(archive_block.get("hash"))
                or archive_timestamp <= 0
            ):
                raise RuntimeError("historical block capability check returned a mismatched block envelope")
        sample_from = max(FROM_BLOCK, snapshot_block - LOG_QUORUM_WINDOW_BLOCKS + 1)
        sample_to = min(snapshot_block, sample_from + max(1, LOG_QUORUM_MAX_BLOCKS) - 1)
        sample = _rpc_once_with_retry(
            url,
            "eth_getLogs",
            [log_filter(AUCTION_HOUSE, [TOPIC_AUCTION_CREATED], sample_from, sample_to)],
            timeout=LOG_RPC_TIMEOUT,
        )
        if not isinstance(sample, list):
            raise RuntimeError("eth_getLogs capability check did not return a list")
        return url

    log_candidates = _independent_rpc_urls([*LOG_RPC_URLS, *qualified_snapshot_urls, *_quorum_rpc_urls()])
    # Keep one independently operated spare when it responds within the bounded
    # post-quorum grace. A two-of-two log pool is accurate while both endpoints
    # are healthy, but one transient timeout leaves no way to distinguish an
    # outage from an empty security-critical result. The spare is preferred,
    # never required: two healthy witnesses still avoid the hard probe deadline.
    log_probe_target = min(len(log_candidates), required + 1)
    verified_log_urls, log_errors = _collect_rpc_probes(
        log_candidates,
        required=required,
        preferred=log_probe_target,
        probe=endpoint_log_capability,
        label="log-capability",
    )
    if len(verified_log_urls) < required:
        raise RuntimeError(
            f"Base RPC log quorum unavailable: healthy={len(verified_log_urls)} required={required}; "
            + "; ".join(log_errors[:3])
        )
    VERIFIED_LOG_URLS = verified_log_urls
    providers = sorted({_rpc_provider_key(url) for url in qualified_snapshot_urls})
    log_providers = sorted({_rpc_provider_key(url) for url in verified_log_urls})
    verification = {
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_verification_scope": (
            "snapshot_hash,contract_code,current_auction,dog_total_supply,recent_event_logs,"
            "event_block_timestamps,dog_token_uri_bindings,woof_token_state,sup_token_state"
        ),
        "onchain_chain_id": "8453",
        "snapshot_block_hash": str(block_data["hash"]).lower(),
        "snapshot_confirmations": str(max(ordered_heads) - snapshot_block),
        "rpc_quorum_size": str(required),
        "rpc_quorum_agreement": f"{len(qualified_snapshot_urls)}/{len(eligible_urls)}",
        "rpc_quorum_providers": ",".join(providers),
        "log_rpc_quorum_providers": ",".join(log_providers),
        **code_hashes,
    }
    return snapshot_block, block_data, verification


def verify_snapshot_unchanged(snapshot_block: int, expected_hash: str) -> None:
    """Fail closed if the selected block reorganized while data was assembled."""
    block_data, _agreeing_urls = rpc_quorum(
        "eth_getBlockByNumber",
        [hex(snapshot_block), False],
        urls=VERIFIED_SNAPSHOT_URLS or _quorum_rpc_urls(),
        min_agreement=RPC_QUORUM_SIZE,
        timeout=30,
    )
    observed_hash = str((block_data or {}).get("hash") or "").lower() if isinstance(block_data, dict) else ""
    if observed_hash != str(expected_hash or "").lower():
        raise RuntimeError(
            f"Base snapshot block {snapshot_block} reorganized during refresh: "
            f"expected={expected_hash} observed={observed_hash or 'missing'}"
        )


def rpc(method: str, params: list[Any], timeout: int = 60, urls: list[str] | None = None) -> Any:
    active_urls = urls or RPC_URLS
    if not active_urls:
        raise RuntimeError(f"no JSON-RPC endpoints configured for {method}")
    last: Exception | None = None
    attempts = max(RPC_ATTEMPTS, len(active_urls))
    for attempt in range(attempts):
        try:
            return _rpc_once(active_urls[attempt % len(active_urls)], method, params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0, min(4.0, 0.25 * (2**attempt))))
    raise RuntimeError(last)


def _validated_batch_items(items: Any, call_count: int) -> dict[int, dict[str, Any]]:
    if not isinstance(items, list):
        raise RuntimeError(f"unexpected JSON-RPC batch response type: {type(items).__name__}")
    by_id: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("JSON-RPC batch response contains a non-object item")
        request_id = item.get("id")
        if type(request_id) is not int or request_id < 0 or request_id >= call_count:
            raise RuntimeError(f"JSON-RPC batch response contains an invalid id: {request_id!r}")
        if request_id in by_id:
            raise RuntimeError(f"JSON-RPC batch response contains duplicate id {request_id}")
        if item.get("jsonrpc") != "2.0":
            raise RuntimeError(f"JSON-RPC batch response has a mismatched envelope for id {request_id}")
        has_result = "result" in item
        has_error = "error" in item
        if has_result == has_error:
            raise RuntimeError(
                f"JSON-RPC batch response id {request_id} must contain exactly one of result or error"
            )
        if has_error:
            error = item.get("error")
            if (
                not isinstance(error, dict)
                or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)
            ):
                raise RuntimeError(f"JSON-RPC batch response id {request_id} has a malformed error")
        by_id[request_id] = item
    expected = set(range(call_count))
    if set(by_id) != expected:
        missing = sorted(expected.difference(by_id))
        raise RuntimeError(f"JSON-RPC batch response is incomplete; missing ids={missing}")
    return by_id


def rpc_batch(calls: list[tuple[str, list[Any]]], timeout: int = 120, urls: list[str] | None = None) -> list[Any]:
    if not calls:
        return []
    active_urls = urls or RPC_URLS
    if not active_urls:
        raise RuntimeError("no JSON-RPC endpoints configured for batch request")
    if len(calls) > RPC_BATCH_LIMIT:
        out: list[Any] = []
        for i in range(0, len(calls), RPC_BATCH_LIMIT):
            out.extend(rpc_batch(calls[i : i + RPC_BATCH_LIMIT], timeout=timeout, urls=active_urls))
        return out
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        for i, (method, params) in enumerate(calls)
    ]
    attempts = max(RPC_ATTEMPTS, len(active_urls))
    for attempt in range(attempts):
        try:
            items = post_json(payload, timeout, active_urls[attempt % len(active_urls)])
            by_id = _validated_batch_items(items, len(calls))
            out = []
            for i in range(len(calls)):
                item = by_id[i]
                if "error" in item:
                    method, params = calls[i]
                    out.append(rpc(method, params, timeout=timeout, urls=active_urls))
                else:
                    out.append(item["result"])
            return out
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0, min(4.0, 0.25 * (2**attempt))))
    return []


def rpc_batch_quorum(
    calls: list[tuple[str, list[Any]]],
    *,
    urls: list[str] | None = None,
    min_agreement: int | None = None,
    timeout: int = 60,
) -> list[Any]:
    """Require independently operated RPCs to agree on an entire batch."""
    if not calls:
        return []
    required = min_agreement or RPC_QUORUM_SIZE
    active_urls = _responsive_rpc_urls(
        _independent_rpc_urls(urls or VERIFIED_SNAPSHOT_URLS or _quorum_rpc_urls()),
        required,
        "rpc-batch",
    )
    if len(active_urls) < required:
        raise RuntimeError(
            f"JSON-RPC batch requires {required} independent Base RPC providers; configured={len(active_urls)}"
        )

    responses: queue.Queue[tuple[int, str, list[Any] | None, Exception | None]] = queue.Queue()

    def worker(index: int, url: str) -> None:
        try:
            responses.put((index, url, rpc_batch(calls, timeout=timeout, urls=[url]), None))
        except Exception as exc:  # noqa: BLE001
            responses.put((index, url, None, exc))

    pending_indexes = set(range(len(active_urls)))
    grouped: dict[str, list[tuple[str, list[Any]]]] = defaultdict(list)
    errors: list[str] = []
    for index, url in enumerate(active_urls):
        threading.Thread(
            target=worker,
            args=(index, url),
            name=f"rpc-batch-quorum-{index}",
            daemon=True,
        ).start()

    deadline = time.monotonic() + RPC_QUORUM_DEADLINE_SECONDS
    while pending_indexes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            index, url, values, error = responses.get(timeout=remaining)
        except queue.Empty:
            break
        if index not in pending_indexes:
            continue
        pending_indexes.remove(index)
        if error is not None:
            errors.append(f"{_redact_rpc_url(url)}: {error}")
        elif values is not None:
            canonical = json.dumps(
                [
                    _canonical_rpc_result(method, value)
                    for (method, _params), value in zip(calls, values)
                ],
                separators=(",", ":"),
            )
            grouped[canonical].append((url, values))

        pending = len(pending_indexes)
        ordered = sorted(grouped.values(), key=len, reverse=True)
        winner = ordered[0] if ordered else []
        runner_up_votes = len(ordered[1]) if len(ordered) > 1 else 0
        if len(winner) >= required and len(winner) > runner_up_votes + pending:
            _mark_rpc_pending_slow([active_urls[item] for item in pending_indexes], "rpc-batch")
            return winner[0][1]

    if pending_indexes:
        pending_urls = [active_urls[item] for item in pending_indexes]
        _mark_rpc_pending_slow(pending_urls, "rpc-batch")
        errors.append(
            "deadline exceeded: " + ", ".join(_redact_rpc_url(url) for url in pending_urls[:3])
        )
    votes = sorted((len(group) for group in grouped.values()), reverse=True)
    detail = f" votes={votes}" if votes else ""
    if grouped:
        provider_groups = sorted(
            (
                sorted({_rpc_provider_key(url) for url, _values in group})
                for group in grouped.values()
            ),
            key=lambda providers: (-len(providers), providers),
        )
        detail += " provider_groups=" + json.dumps(provider_groups[:3], separators=(",", ":"))
    if errors:
        detail += f" errors={'; '.join(errors[:3])}"
    raise RuntimeError(f"JSON-RPC batch quorum disagreement: required={required}{detail}")


def log_filter(address: str, topic_filter: str | list[str], start: int, end: int) -> dict[str, Any]:
    return {"address": address, "fromBlock": hex(start), "toBlock": hex(end), "topics": [topic_filter]}


def _canonical_topics(topics: str | list[str]) -> list[str]:
    if isinstance(topics, str):
        return [topics.lower()]
    return [str(topic).lower() for topic in topics]


def _log_cache_enabled() -> bool:
    raw = os.environ.get("MISSION3_LOG_CACHE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _log_cache_path(address: str, topics: str | list[str], from_block: int) -> Path:
    key = json.dumps(
        {
            "address": address.lower(),
            "topics": _canonical_topics(topics),
            "from_block": int(from_block),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return LOG_CACHE_DIR / f"{digest}.json"


def _load_log_cache(path: Path, address: str, topics: str | list[str], from_block: int) -> tuple[int, list[dict[str, Any]]]:
    try:
        data = read_owned_json_file(path, 67_108_864, "RPC log cache")
        if data is None or not isinstance(data, dict):
            return 0, []
        expected_topics = _canonical_topics(topics)
        if data.get("schema_version") != 1:
            return 0, []
        if str(data.get("address", "")).lower() != address.lower():
            return 0, []
        if data.get("topics") != expected_topics:
            return 0, []
        cached_from = data.get("from_block")
        cached_to = data.get("to_block")
        if type(cached_from) is not int or type(cached_to) is not int:
            return 0, []
        if cached_from != int(from_block) or cached_to < cached_from:
            return 0, []
        logs = data.get("logs")
        if not isinstance(logs, list):
            return 0, []
        clean_logs: list[dict[str, Any]] = []
        for item in logs:
            if not isinstance(item, dict):
                return 0, []
            raw_block = item.get("blockNumber")
            raw_index = item.get("logIndex", "0x0")
            if not isinstance(raw_block, str) or not isinstance(raw_index, str):
                return 0, []
            block_number = int(raw_block, 16)
            log_index = int(raw_index, 16)
            if block_number < cached_from or block_number > cached_to or log_index < 0:
                return 0, []
            if not str(item.get("transactionHash") or "") or not isinstance(item.get("topics", []), list):
                return 0, []
            # Exercise both cache keys while loading so malformed hex or
            # identities are treated as a miss instead of crashing a refresh.
            _log_sort_key(item)
            _log_identity(item)
            clean_logs.append(item)
        return cached_to, clean_logs
    except (OSError, RuntimeError, TypeError, ValueError):
        return 0, []


def _save_log_cache(path: Path, address: str, topics: str | list[str], from_block: int, to_block: int, logs: list[dict[str, Any]]) -> None:
    data = {
        "schema_version": 1,
        "address": address.lower(),
        "topics": _canonical_topics(topics),
        "from_block": int(from_block),
        "to_block": int(to_block),
        "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "logs": logs,
    }
    atomic_write_text(path, json.dumps(data, separators=(",", ":")) + "\n")


def _log_sort_key(item: dict[str, Any]) -> tuple[int, str, int]:
    return (
        int(str(item.get("blockNumber", "0x0")), 16),
        str(item.get("transactionHash") or ""),
        int(str(item.get("logIndex", "0x0")), 16),
    )


def _log_identity(item: dict[str, Any]) -> tuple[str, str, int]:
    block_identity = str(item.get("blockHash") or "").lower() or hex(_block_number(item))
    return (
        block_identity,
        str(item.get("transactionHash") or "").lower(),
        int(str(item.get("logIndex", "0x0")), 16),
    )


def _block_number(item: dict[str, Any]) -> int:
    try:
        return int(str(item.get("blockNumber", "0x0")), 16)
    except (TypeError, ValueError):
        return 0


def _fetch_logs_uncached(address: str, topics: str | list[str], from_block: int, to_block: int) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    topic_filter: str | list[str] = topics
    ranges: list[tuple[int, int]] = []
    start = from_block
    while start <= to_block:
        end = min(to_block, start + LOG_CHUNK - 1)
        ranges.append((start, end))
        start = end + 1

    def fetch_range(bounds: tuple[int, int]) -> list[dict[str, Any]]:
        a, b = bounds
        return rpc("eth_getLogs", [log_filter(address, topic_filter, a, b)], timeout=LOG_RPC_TIMEOUT, urls=LOG_RPC_URLS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=LOG_WORKERS) as pool:
        futures = [pool.submit(fetch_range, bounds) for bounds in ranges]
        for future in concurrent.futures.as_completed(futures):
            logs.extend(future.result())

    logs.sort(key=lambda item: (int(item["blockNumber"], 16), int(item.get("logIndex", "0x0"), 16)))
    return logs


def _fetch_logs_checkpointed(
    address: str,
    topics: str | list[str],
    from_block: int,
    to_block: int,
    checkpoint: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch bounded batches so a transient failure cannot erase hours of work."""
    logs: list[dict[str, Any]] = []
    # Each inner call retains the normal worker fan-out, while this outer batch
    # gives the persistent cache a contiguous, resumable checkpoint.
    batch_span = max(LOG_CHUNK, LOG_CHUNK * LOG_WORKERS)
    total_batches = max(1, (to_block - from_block + batch_span) // batch_span)
    completed_batches = 0
    start = from_block
    while start <= to_block:
        end = min(to_block, start + batch_span - 1)
        logs.extend(_fetch_logs_uncached(address, topics, start, end))
        logs.sort(key=_log_sort_key)
        if checkpoint is not None:
            checkpoint(end, logs)
        completed_batches += 1
        if total_batches > 1 and (
            completed_batches == 1
            or completed_batches % 25 == 0
            or completed_batches == total_batches
        ):
            progress(
                f"log scan {address[:10]} batches={completed_batches}/{total_batches} "
                f"through_block={end} logs={len(logs)}"
            )
        start = end + 1
    return logs


def _fetch_logs_verified_or_uncached(
    address: str,
    topics: str | list[str],
    from_block: int,
    to_block: int,
    checkpoint: Any | None = None,
) -> list[dict[str, Any]]:
    if VERIFIED_LOG_URLS and (LOG_QUORUM_MAX_BLOCKS < 1 or LOG_QUORUM_WINDOW_BLOCKS < 1):
        raise RuntimeError("verified log collection requires positive quorum window and chunk sizes")
    if not VERIFIED_LOG_URLS:
        return _fetch_logs_checkpointed(address, topics, from_block, to_block, checkpoint)

    # A cold scan can span millions of blocks, but the reorg-sensitive newest
    # tail must never silently fall back to one provider merely because the
    # historical prefix is large. Scan/cache the cold prefix normally, then
    # require independent agreement for every chunk in the bounded recent tail.
    quorum_from = max(from_block, to_block - LOG_QUORUM_WINDOW_BLOCKS + 1)
    logs: list[dict[str, Any]] = []
    if from_block < quorum_from:
        logs.extend(
            _fetch_logs_checkpointed(
                address,
                topics,
                from_block,
                quorum_from - 1,
                checkpoint,
            )
        )
    start = quorum_from
    while start <= to_block:
        end = min(to_block, start + LOG_QUORUM_MAX_BLOCKS - 1)
        result, _agreeing_urls = rpc_quorum(
            "eth_getLogs",
            [log_filter(address, topics, start, end)],
            urls=VERIFIED_LOG_URLS,
            min_agreement=RPC_QUORUM_SIZE,
            timeout=LOG_RPC_TIMEOUT,
        )
        if not isinstance(result, list):
            raise RuntimeError(f"unexpected quorum eth_getLogs response: {result!r}")
        logs.extend(
            item
            for item in result
            if isinstance(item, dict) and not bool(item.get("removed", False))
        )
        logs.sort(key=_log_sort_key)
        if checkpoint is not None:
            checkpoint(end, logs)
        start = end + 1
    return logs


def fetch_logs(address: str, topics: str | list[str], from_block: int, to_block: int) -> list[dict[str, Any]]:
    if from_block > to_block:
        return []
    if VERIFIED_LOG_URLS and LOG_CACHE_OVERLAP_BLOCKS < 1:
        raise RuntimeError("verified log collection requires a positive cache overlap")
    if not _log_cache_enabled():
        return _fetch_logs_verified_or_uncached(address, topics, from_block, to_block)

    cache_path = _log_cache_path(address, topics, from_block)
    cached_to_block, cached_logs = _load_log_cache(cache_path, address, topics, from_block)
    if cached_to_block >= to_block and LOG_CACHE_OVERLAP_BLOCKS <= 0:
        return sorted(
            [
                item
                for item in cached_logs
                if from_block <= int(str(item.get("blockNumber", "0x0")), 16) <= to_block
                and not bool(item.get("removed", False))
            ],
            key=_log_sort_key,
        )

    start_block = from_block
    if cached_to_block >= from_block:
        overlap_tip = min(cached_to_block, to_block)
        start_block = max(from_block, overlap_tip - LOG_CACHE_OVERLAP_BLOCKS + 1)
    def checkpoint(checkpoint_to: int, fresh_so_far: list[dict[str, Any]]) -> None:
        retained = [item for item in cached_logs if _block_number(item) < start_block]
        partial_by_id: dict[tuple[str, str, int], dict[str, Any]] = {}
        for item in [*retained, *fresh_so_far]:
            block_number = _block_number(item)
            if from_block <= block_number <= checkpoint_to and not bool(item.get("removed", False)):
                partial_by_id[_log_identity(item)] = item
        partial = sorted(partial_by_id.values(), key=_log_sort_key)
        _save_log_cache(cache_path, address, topics, from_block, checkpoint_to, partial)

    fresh_logs = _fetch_logs_verified_or_uncached(
        address,
        topics,
        start_block,
        to_block,
        checkpoint=checkpoint,
    )
    # The overlap is authoritative. Drop every cached row from the re-fetched
    # range before merging so logs removed by a Base reorg cannot survive merely
    # because they are absent from the new canonical response.
    retained_logs = [item for item in cached_logs if _block_number(item) < start_block]
    by_id: dict[tuple[str, str, int], dict[str, Any]] = {}
    for item in [*retained_logs, *fresh_logs]:
        block_number = _block_number(item)
        if not block_number:
            continue
        if from_block <= block_number <= to_block and not bool(item.get("removed", False)):
            by_id[_log_identity(item)] = item
    merged = sorted(by_id.values(), key=_log_sort_key)
    _save_log_cache(cache_path, address, topics, from_block, to_block, merged)
    return merged


def word(data: str, idx: int) -> int:
    clean = data[2:] if data.startswith("0x") else data
    return int(clean[idx * 64 : (idx + 1) * 64] or "0", 16)


def word_address(data: str, idx: int) -> str:
    return "0x" + f"{word(data, idx):064x}"[-40:]


def topic_uint(topic: str) -> int:
    return int(topic, 16)


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:]


def utc_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def decimal_str(value: int, decimals: int, max_places: int = 18) -> str:
    q = Decimal(value) / (Decimal(10) ** decimals)
    s = f"{q:.{max_places}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def canonical_block_hash(value: Any) -> str:
    block_hash = str(value or "").strip().lower()
    if len(block_hash) != 66 or not block_hash.startswith("0x"):
        return ""
    return block_hash if all(character in "0123456789abcdef" for character in block_hash[2:]) else ""


def load_block_time_cache() -> dict[int, dict[str, str]]:
    """Load only hash-bound block timestamps; legacy timestamp-only rows miss."""
    cache: dict[int, dict[str, str]] = {}
    try:
        raw = read_owned_json_file(BLOCK_TIME_CACHE, 16_777_216, "block-time cache")
        if not isinstance(raw, dict) or raw.get("schema_version") != 2:
            return {}
        blocks = raw.get("blocks")
        if not isinstance(blocks, dict):
            return {}
        for key, value in blocks.items():
            if not isinstance(value, dict):
                continue
            try:
                block = int(key)
            except (TypeError, ValueError):
                continue
            if block < 0 or str(block) != str(key):
                continue
            block_hash = canonical_block_hash(value.get("block_hash"))
            timestamp = str(value.get("timestamp_utc") or "").strip()
            try:
                datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            if block_hash:
                cache[block] = {"block_hash": block_hash, "timestamp_utc": timestamp}
    except Exception as exc:  # noqa: BLE001
        print(f"warning: block time cache ignored: {exc}", file=sys.stderr)
    return cache


def save_block_time_cache(cache: dict[int, dict[str, str]]) -> None:
    atomic_write_text(
        BLOCK_TIME_CACHE,
        json.dumps(
            {
                "schema_version": 2,
                "blocks": {str(k): cache[k] for k in sorted(cache)},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
    )


def fetch_block_times(
    blocks: set[int],
    expected_hashes: dict[int, str] | None = None,
) -> dict[int, str]:
    ordered = sorted(blocks)
    normalized_hashes: dict[int, str] = {}
    for block, raw_hash in (expected_hashes or {}).items():
        block_hash = canonical_block_hash(raw_hash)
        if not block_hash:
            raise RuntimeError(f"event log for block {block} has an invalid block hash")
        normalized_hashes[int(block)] = block_hash
    if VERIFIED_LOG_URLS:
        missing_hashes = [block for block in ordered if block not in normalized_hashes]
        if missing_hashes:
            raise RuntimeError(
                "verified historical block-time lookup requires event-log block hashes; "
                f"missing={missing_hashes[:10]}"
            )
        archive_urls = _independent_rpc_urls(VERIFIED_LOG_URLS)
        if len(archive_urls) < RPC_QUORUM_SIZE:
            raise RuntimeError(
                f"historical block-time verification requires {RPC_QUORUM_SIZE} independent "
                f"archive/log RPC providers; configured={len(archive_urls)}"
            )
    elif VERIFIED_SNAPSHOT_URLS:
        raise RuntimeError("historical block-time verification has no capability-probed archive/log RPC quorum")
    else:
        archive_urls = []
    cache = load_block_time_cache()
    out: dict[int, str] = {
        block: cache[block]["timestamp_utc"]
        for block in ordered
        if block in cache
        and block in normalized_hashes
        and cache[block]["block_hash"] == normalized_hashes[block]
    }
    missing = [block for block in ordered if block not in out]
    if missing:
        progress(f"fetching block times missing={len(missing)} cached={len(out)}")
    # Give every small archive batch a fresh quorum deadline. A large outer
    # batch would otherwise force each provider through many serial HTTP
    # requests inside one all-or-nothing deadline.
    for i in range(0, len(missing), RPC_BATCH_LIMIT):
        batch = missing[i : i + RPC_BATCH_LIMIT]
        calls = [("eth_getBlockByNumber", [hex(block), False]) for block in batch]
        results = (
            rpc_batch_quorum(
                calls,
                urls=archive_urls,
                min_agreement=RPC_QUORUM_SIZE,
                timeout=BLOCK_TIME_RPC_TIMEOUT,
            )
            if archive_urls
            else rpc_batch(calls, timeout=BLOCK_TIME_RPC_TIMEOUT)
        )
        for block, result in zip(batch, results):
            if not isinstance(result, dict):
                raise RuntimeError(f"block-time lookup for {block} returned a non-object result")
            try:
                observed_number = int(str(result.get("number") or ""), 16)
                timestamp_unix = int(str(result.get("timestamp") or ""), 16)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"block-time lookup for {block} returned malformed quantities") from exc
            block_hash = canonical_block_hash(result.get("hash"))
            if observed_number != block or not block_hash or timestamp_unix <= 0:
                raise RuntimeError(f"block-time lookup for {block} returned a mismatched block envelope")
            expected_hash = normalized_hashes.get(block)
            if expected_hash and block_hash != expected_hash:
                raise RuntimeError(
                    f"block-time lookup hash mismatch for block {block}: "
                    f"expected={expected_hash} observed={block_hash}"
                )
            timestamp = utc_from_unix(timestamp_unix)
            out[block] = timestamp
            cache[block] = {"block_hash": block_hash, "timestamp_utc": timestamp}
        # Checkpoint each verified bounded chunk so a late archive outage does
        # not discard minutes of completed quorum work. The atomic writer keeps
        # readers on the previous complete cache until replacement succeeds.
        save_block_time_cache(cache)
    if set(out) != set(ordered):
        raise RuntimeError("block-time lookup returned an incomplete result")
    return out


def eth_call(to: str, data: str, block_tag: str = "latest") -> str:
    if VERIFIED_SNAPSHOT_URLS and block_tag != "latest":
        result, _agreeing_urls = rpc_quorum(
            "eth_call",
            [{"to": to, "data": data}, block_tag],
            urls=VERIFIED_SNAPSHOT_URLS,
            min_agreement=RPC_QUORUM_SIZE,
            timeout=30,
        )
        return str(result)
    return rpc("eth_call", [{"to": to, "data": data}, block_tag])


def decode_abi_string(raw: str) -> str:
    if not raw or raw == "0x":
        return ""
    clean = raw[2:] if raw.startswith("0x") else raw
    if len(clean) < 128:
        try:
            return bytes.fromhex(clean.rstrip("0")).decode("utf-8", errors="ignore").strip("\x00")
        except Exception:
            return ""
    offset = int(clean[:64], 16) * 2
    length = int(clean[offset : offset + 64], 16)
    data = clean[offset + 64 : offset + 64 + length * 2]
    return bytes.fromhex(data).decode("utf-8", errors="ignore")


def decode_uint_call(raw: str) -> int:
    return int(raw, 16) if raw and raw != "0x" else 0


def fetch_eth_usd_price() -> tuple[Decimal, str]:
    endpoints = [
        ("coinbase", "https://api.coinbase.com/v2/prices/ETH-USD/spot", lambda data: data["data"]["amount"]),
        ("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", lambda data: data["ethereum"]["usd"]),
    ]
    for source, url, picker in endpoints:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "degen-dogs-mission3-builder/1.0"})
            with open_no_redirect(req, timeout=30) as response:
                data = read_bounded_json_response(response, EXTERNAL_JSON_MAX_RESPONSE_BYTES, source)
            price = Decimal(str(picker(data)))
            if price > 0:
                return price, source
        except Exception as exc:  # noqa: BLE001
            print(f"warning: ETH/USD lookup failed via {source}: {exc}", file=sys.stderr)
    return Decimal(0), "unavailable"


def decimal_value_str(value: Decimal, max_places: int = 6) -> str:
    s = f"{value:.{max_places}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def decimal_from(value: Any) -> Decimal | None:
    raw = str(value or "").replace(",", "").strip()
    if not raw or raw.upper() == "N/A":
        return None
    try:
        return Decimal(raw)
    except Exception:
        return None


def quantized_decimal_str(value: Decimal, places: int) -> str:
    quant = Decimal(1).scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):f}".rstrip("0").rstrip(".") or "0"


def reward_apr_display_value(apr: Decimal | None) -> str:
    if apr is None or apr <= 0:
        return "N/A"
    rounded = apr.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"≈{rounded:,.0f}% APR"


def current_bid_reward_stats(current: dict[str, Any], token_stats: dict[str, str]) -> dict[str, str]:
    unavailable = {
        "reward_current_bid_payback_days": "N/A",
        "reward_current_bid_daily_roi_pct": "N/A",
        "reward_current_bid_apr_pct": "N/A",
        "reward_current_bid_apr_display": "N/A",
    }
    amount_wei = decimal_from(current.get("amount_wei"))
    eth_usd = decimal_from(token_stats.get("eth_usd_price"))
    per_dog_daily_usd = decimal_from(token_stats.get("reward_total_per_dog_usd_per_day"))
    if amount_wei is None or eth_usd is None or per_dog_daily_usd is None:
        return unavailable
    if amount_wei <= 0 or eth_usd <= 0 or per_dog_daily_usd <= 0:
        return unavailable

    current_bid_usd = ((amount_wei / (Decimal(10) ** 18)) * eth_usd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if current_bid_usd <= 0:
        return unavailable

    payback_days = current_bid_usd / per_dog_daily_usd
    daily_roi_pct = (per_dog_daily_usd / current_bid_usd) * Decimal(100)
    apr_pct = daily_roi_pct * Decimal(365)
    return {
        "reward_current_bid_payback_days": quantized_decimal_str(payback_days, 2),
        "reward_current_bid_daily_roi_pct": quantized_decimal_str(daily_roi_pct, 4),
        "reward_current_bid_apr_pct": quantized_decimal_str(apr_pct, 2),
        "reward_current_bid_apr_display": reward_apr_display_value(apr_pct),
    }


def parse_utc_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc_z(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def season6_wallet_display(wallet: str, profiles_by_address: dict[str, dict[str, Any]]) -> tuple[str, str, str]:
    profile = profiles_by_address.get(wallet, {})
    username = str(profile.get("username") or "").strip()
    display = str(profile.get("display_name") or "").strip()
    label = f"@{username}" if username else display or short_address(wallet)
    url = f"https://farcaster.xyz/{username}" if username else basescan_address_url(wallet)
    return label, url, username


def season6_decimal_display(value: Decimal, max_places: int = 6) -> str:
    return decimal_value_str(value, max_places)


def season6_usd_value(value: Decimal, sup_usd: Decimal) -> str:
    if sup_usd <= 0:
        return "N/A"
    return decimal_value_str(value * sup_usd, 2)


def season6_event_time(row: dict[str, Any]) -> datetime | None:
    return parse_utc_datetime(row.get("settled_time_utc") or row.get("block_time_utc") or row.get("auction_time_utc"))


def season6_allocate_time_slices(
    events: list[tuple[datetime, str, Decimal]],
    *,
    config: Season6SupConfig,
    allocation_end: datetime,
) -> tuple[dict[str, Decimal], Decimal]:
    reward_start = parse_utc_datetime(config.reward_start_utc)
    campaign_end = parse_utc_datetime(config.campaign_end_utc)
    if reward_start is None or campaign_end is None or campaign_end <= reward_start:
        return {}, Decimal(0)
    end = min(allocation_end, campaign_end)
    if end <= reward_start:
        return {}, Decimal(0)

    total_seconds = Decimal(str((campaign_end - reward_start).total_seconds()))
    active_xp: defaultdict[str, Decimal] = defaultdict(Decimal)
    allocations: defaultdict[str, Decimal] = defaultdict(Decimal)
    unallocated = Decimal(0)
    sorted_events = sorted((event_time, wallet, xp) for event_time, wallet, xp in events if wallet and xp > 0)

    idx = 0
    current_time = reward_start
    while idx < len(sorted_events) and sorted_events[idx][0] <= current_time:
        _event_time, wallet, xp = sorted_events[idx]
        active_xp[wallet] += xp
        idx += 1

    while current_time < end:
        next_event_time = sorted_events[idx][0] if idx < len(sorted_events) else end
        interval_end = min(next_event_time, end)
        if interval_end > current_time:
            interval_seconds = Decimal(str((interval_end - current_time).total_seconds()))
            interval_sup = config.total_sup * interval_seconds / total_seconds
            total_xp = sum(active_xp.values(), Decimal(0))
            if total_xp > 0:
                for wallet, xp in active_xp.items():
                    if xp > 0:
                        allocations[wallet] += interval_sup * xp / total_xp
            else:
                unallocated += interval_sup
            current_time = interval_end
        while idx < len(sorted_events) and sorted_events[idx][0] <= current_time:
            _event_time, wallet, xp = sorted_events[idx]
            active_xp[wallet] += xp
            idx += 1
        if interval_end == end:
            break
    return dict(allocations), unallocated


def season6_future_dilution_events(
    start_after: datetime,
    *,
    config: Season6SupConfig,
    campaign_end: datetime,
) -> list[tuple[datetime, str, Decimal]]:
    interval_seconds = int(config.expected_future_settlement_interval_seconds or 0)
    if interval_seconds <= 0 or start_after >= campaign_end:
        return []
    events: list[tuple[datetime, str, Decimal]] = []
    event_time = start_after + timedelta(seconds=interval_seconds)
    idx = 1
    while event_time < campaign_end:
        events.append((event_time, f"__season6_future_winner_{idx}", config.xp_per_settled_win))
        event_time += timedelta(seconds=interval_seconds)
        idx += 1
    return events


def season6_cap_status(cap_aware_incremental: Decimal, cap_remaining: Decimal, raw_incremental: Decimal) -> str:
    if cap_aware_incremental <= 0 and cap_remaining <= 0:
        return "wallet_near_cap"
    if cap_aware_incremental <= 0 and raw_incremental <= 0:
        return "no_incremental_estimate"
    if cap_aware_incremental < raw_incremental:
        return "cap_limited_incremental"
    return "estimated"


def build_season6_sup_outputs(
    settled_rows: list[dict[str, Any]],
    current: dict[str, Any],
    token_stats: dict[str, str],
    *,
    snapshot_time_utc: str,
    config: Season6SupConfig = SEASON6_SUP_CONFIG,
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    xp_start = parse_utc_datetime(config.xp_start_utc)
    reward_start = parse_utc_datetime(config.reward_start_utc)
    campaign_end = parse_utc_datetime(config.campaign_end_utc)
    snapshot_time = parse_utc_datetime(snapshot_time_utc) or datetime.now(timezone.utc)
    if xp_start is None or reward_start is None or campaign_end is None:
        raise ValueError("invalid Season 6 SUP config dates")

    profiles_by_address = {
        normalize_address(row.get("address")): row
        for row in profiles or []
        if normalize_address(row.get("address"))
    }
    sup_usd = decimal_from(token_stats.get("sup_usd_price")) or Decimal(0)
    sup_usd_source = token_stats.get("sup_usd_source") or "unavailable"
    eth_usd = decimal_from(token_stats.get("eth_usd_price")) or Decimal(0)

    events: list[tuple[datetime, str, Decimal]] = []
    win_rows: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, Any]] = {}
    for row in settled_rows:
        event_time = season6_event_time(row)
        wallet = normalize_address(row.get("winner") or row.get("winner_wallet"))
        if not event_time or not wallet or wallet == ZERO:
            continue
        if event_time < xp_start or event_time >= campaign_end or event_time > snapshot_time:
            continue
        xp = config.xp_per_settled_win
        events.append((event_time, wallet, xp))
        amount_eth = decimal_from(row.get("amount_eth")) or Decimal(0)
        amount_usd = amount_eth * eth_usd if eth_usd > 0 else Decimal(0)
        label, url, username = season6_wallet_display(wallet, profiles_by_address)
        token_id = int_value(row.get("token_id"), 0)
        win_rows.append({
            "auction_id": token_id,
            "token_id": token_id,
            "dog": f"Dog #{token_id}" if token_id else "",
            "winner_wallet": wallet,
            "winner_display": label,
            "winner_url": url,
            "farcaster_username": username,
            "settled_time_utc": iso_utc_z(event_time),
            "winning_bid_eth": season6_decimal_display(amount_eth, 8),
            "winning_bid_usd": season6_usd_value(amount_eth, eth_usd) if eth_usd > 0 else "N/A",
            "season6_xp": int(xp),
        })
        entry = grouped.setdefault(wallet, {"wins": 0, "xp": Decimal(0), "tokens": [], "first": event_time, "latest": event_time, "display": label, "url": url, "username": username})
        entry["wins"] += 1
        entry["xp"] += xp
        entry["tokens"].append(str(token_id))
        entry["first"] = min(entry["first"], event_time)
        entry["latest"] = max(entry["latest"], event_time)

    full_allocations, full_unallocated = season6_allocate_time_slices(events, config=config, allocation_end=campaign_end)
    to_date_end = min(snapshot_time, campaign_end)
    earned_to_date, unallocated_to_date = season6_allocate_time_slices(events, config=config, allocation_end=to_date_end)

    by_winner: list[dict[str, Any]] = []
    for wallet, entry in grouped.items():
        raw_full = full_allocations.get(wallet, Decimal(0))
        earned = earned_to_date.get(wallet, Decimal(0))
        capped_full = min(raw_full, config.cap_sup)
        cap_remaining = max(config.cap_sup - capped_full, Decimal(0))
        by_winner.append({
            "winner_wallet": wallet,
            "winner_display": entry["display"],
            "winner_url": entry["url"],
            "farcaster_username": entry["username"],
            "season6_wins_confirmed": int(entry["wins"]),
            "season6_xp_confirmed": int(entry["xp"]),
            "season6_raw_sup_earned_to_date": season6_decimal_display(earned),
            "season6_raw_sup_projected_full": season6_decimal_display(raw_full),
            "season6_capped_sup_projected_full": season6_decimal_display(capped_full),
            "season6_cap_sup": season6_decimal_display(config.cap_sup, 0),
            "season6_cap_remaining_sup": season6_decimal_display(cap_remaining),
            "season6_cap_limited": "true" if raw_full > config.cap_sup else "false",
            "season6_raw_usd_earned_to_date": season6_usd_value(earned, sup_usd),
            "season6_raw_usd_projected_full": season6_usd_value(raw_full, sup_usd),
            "season6_capped_usd_projected_full": season6_usd_value(capped_full, sup_usd),
            "first_s6_win_time_utc": iso_utc_z(entry["first"]),
            "latest_s6_win_time_utc": iso_utc_z(entry["latest"]),
            "season6_wallet_note": "wallet-level estimate; cap overflow redistribution not assumed",
            "season6_token_ids": ",".join(entry["tokens"]),
        })
    by_winner.sort(key=lambda row: (decimal_from(row["season6_capped_sup_projected_full"]) or Decimal(0), row["season6_wins_confirmed"]), reverse=True)
    by_wallet = {row["winner_wallet"]: row for row in by_winner}

    rewards_by_auction: list[dict[str, Any]] = []
    for row in win_rows:
        winner = by_wallet.get(row["winner_wallet"], {})
        rewards_by_auction.append({
            **row,
            "season6_raw_sup_earned_to_date": winner.get("season6_raw_sup_earned_to_date", "0"),
            "season6_raw_sup_projected_full": winner.get("season6_raw_sup_projected_full", "0"),
            "season6_capped_sup_projected_full": winner.get("season6_capped_sup_projected_full", "0"),
            "season6_raw_usd_earned_to_date": winner.get("season6_raw_usd_earned_to_date", "N/A"),
            "season6_raw_usd_projected_full": winner.get("season6_raw_usd_projected_full", "N/A"),
            "season6_capped_usd_projected_full": winner.get("season6_capped_usd_projected_full", "N/A"),
            "cap_limited_by_wallet": winner.get("season6_cap_limited", "false"),
        })
    rewards_by_auction.sort(key=lambda row: (row.get("settled_time_utc", ""), row.get("token_id", 0)), reverse=True)

    current_status: list[dict[str, Any]] = []
    current_bidder = normalize_address(current.get("bidder_wallet") or current.get("bidder"))
    no_current_defaults = {
        "season6_sup_current_bidder_wallet": "",
        "season6_sup_current_bidder_prior_s6_wins": "0",
        "season6_sup_current_bidder_prior_s6_xp": "0",
        "season6_sup_current_bid_estimated_win_time_utc": "N/A",
        "season6_sup_current_bid_estimated_raw_incremental_sup": "0",
        "season6_sup_current_bid_estimated_cap_aware_sup": "0",
        "season6_sup_current_bid_estimated_cap_aware_usd": "N/A",
        "season6_sup_current_bid_projected_total_without_win_sup": "0",
        "season6_sup_current_bid_projected_total_with_win_sup": "0",
        "season6_sup_current_bid_cap_remaining_before_win_sup": season6_decimal_display(config.cap_sup, 0),
        "season6_sup_estimate_status": "no_current_bid",
        "season6_sup_current_bid_estimate_status": "no_current_bid",
    }
    current_metric_values = dict(no_current_defaults)
    if current_bidder and current_bidder != ZERO:
        current_end = parse_utc_datetime(current.get("end_time_utc")) or snapshot_time
        estimated_win_time = current_end if current_end >= snapshot_time else snapshot_time
        current_amount_eth = decimal_from(current.get("amount_eth")) or Decimal(0)
        current_win_in_window = xp_start <= estimated_win_time < campaign_end
        projected_events_without = list(events)
        projected_events_with = list(events)
        if current_win_in_window:
            projected_events_with.append((estimated_win_time, current_bidder, config.xp_per_settled_win))
        future_events = season6_future_dilution_events(estimated_win_time, config=config, campaign_end=campaign_end)
        future_enabled = bool(future_events)
        projected_events_without.extend(future_events)
        projected_events_with.extend(future_events)
        without_allocations, _without_unallocated = season6_allocate_time_slices(projected_events_without, config=config, allocation_end=campaign_end)
        with_allocations, _with_unallocated = season6_allocate_time_slices(projected_events_with, config=config, allocation_end=campaign_end)
        projected_without = without_allocations.get(current_bidder, Decimal(0))
        projected_raw = with_allocations.get(current_bidder, Decimal(0))
        projected_without_capped = min(projected_without, config.cap_sup)
        projected_capped = min(projected_raw, config.cap_sup)
        raw_incremental = max(projected_raw - projected_without, Decimal(0))
        cap_aware_incremental = max(projected_capped - projected_without_capped, Decimal(0))
        cap_remaining_before_win = max(config.cap_sup - projected_without_capped, Decimal(0))
        projected_remaining = max(config.cap_sup - projected_capped, Decimal(0))
        prior = grouped.get(current_bidder, {"wins": 0, "xp": Decimal(0)})
        prior_row = by_wallet.get(current_bidder, {})
        label, _url, _username = season6_wallet_display(current_bidder, profiles_by_address)
        estimate_status = season6_cap_status(cap_aware_incremental, cap_remaining_before_win, raw_incremental) if current_win_in_window else "outside_season_window"
        current_status.append({
            "current_auction_token_id": int_value(current.get("token_id"), 0),
            "current_bidder_wallet": current_bidder,
            "current_bidder_display": label,
            "current_bid_eth": season6_decimal_display(current_amount_eth, 8),
            "current_bid_usd": season6_usd_value(current_amount_eth, eth_usd) if eth_usd > 0 else "N/A",
            "current_auction_end_utc": iso_utc_z(current_end),
            "prior_s6_wins_confirmed": int(prior.get("wins", 0)),
            "prior_s6_xp_confirmed": int(prior.get("xp", Decimal(0))),
            "prior_s6_raw_sup_projected_full": prior_row.get("season6_raw_sup_projected_full", "0"),
            "prior_s6_capped_sup_projected_full": prior_row.get("season6_capped_sup_projected_full", "0"),
            "prior_s6_cap_remaining_sup": prior_row.get("season6_cap_remaining_sup", season6_decimal_display(config.cap_sup, 0)),
            "projected_s6_wins_if_current_bid_wins": int(prior.get("wins", 0)) + (1 if current_win_in_window else 0),
            "projected_s6_xp_if_current_bid_wins": int(prior.get("xp", Decimal(0)) + (config.xp_per_settled_win if current_win_in_window else Decimal(0))),
            "projected_raw_sup_if_current_bid_wins": season6_decimal_display(projected_raw),
            "projected_capped_sup_if_current_bid_wins": season6_decimal_display(projected_capped),
            "projected_cap_remaining_sup_if_current_bid_wins": season6_decimal_display(projected_remaining),
            "projected_raw_usd_if_current_bid_wins": season6_usd_value(projected_raw, sup_usd),
            "projected_capped_usd_if_current_bid_wins": season6_usd_value(projected_capped, sup_usd),
            "projected_total_without_current_win_sup": season6_decimal_display(projected_without),
            "projected_total_with_current_win_sup": season6_decimal_display(projected_raw),
            "estimated_raw_incremental_sup": season6_decimal_display(raw_incremental),
            "estimated_cap_aware_incremental_sup": season6_decimal_display(cap_aware_incremental),
            "estimated_cap_aware_incremental_usd": season6_usd_value(cap_aware_incremental, sup_usd),
            "cap_remaining_before_current_win_sup": season6_decimal_display(cap_remaining_before_win),
            "future_dilution_enabled": "true" if future_enabled else "false",
            "expected_future_settlement_interval_seconds": str(config.expected_future_settlement_interval_seconds),
            "current_bidder_cap_status": estimate_status,
            "estimate_status": estimate_status,
            "projection_note": "cap-aware incremental estimate; future daily dilution projected; cap overflow redistribution not assumed",
        })
        current_metric_values = {
            "season6_sup_current_bidder_wallet": current_bidder,
            "season6_sup_current_bidder_prior_s6_wins": str(int(prior.get("wins", 0))),
            "season6_sup_current_bidder_prior_s6_xp": str(int(prior.get("xp", Decimal(0)))),
            "season6_sup_current_bid_estimated_win_time_utc": iso_utc_z(estimated_win_time),
            "season6_sup_current_bid_estimated_raw_incremental_sup": season6_decimal_display(raw_incremental),
            "season6_sup_current_bid_estimated_cap_aware_sup": season6_decimal_display(cap_aware_incremental),
            "season6_sup_current_bid_estimated_cap_aware_usd": season6_usd_value(cap_aware_incremental, sup_usd),
            "season6_sup_current_bid_projected_total_without_win_sup": season6_decimal_display(projected_without),
            "season6_sup_current_bid_projected_total_with_win_sup": season6_decimal_display(projected_raw),
            "season6_sup_current_bid_cap_remaining_before_win_sup": season6_decimal_display(cap_remaining_before_win),
            "season6_sup_estimate_status": estimate_status,
            "season6_sup_current_bid_estimate_status": estimate_status,
        }

    current_row = current_status[0] if current_status else {}
    days_remaining = max(Decimal(0), Decimal(str((campaign_end - to_date_end).total_seconds())) / Decimal(86400))
    metrics = {
        "season6_sup_status": "live_estimate" if config.enabled else "disabled",
        "season6_sup_enabled": "true" if config.enabled else "false",
        "season6_sup_token": config.sup_token,
        "season6_sup_usd_price": season6_decimal_display(sup_usd, 8) if sup_usd > 0 else "N/A",
        "season6_sup_total_allocation": season6_decimal_display(config.total_sup, 0),
        "season6_sup_wallet_cap": season6_decimal_display(config.cap_sup, 0),
        "season6_sup_xp_per_win": season6_decimal_display(config.xp_per_settled_win, 0),
        "season6_sup_start_utc": config.season_start_utc,
        "season6_sup_end_utc": config.season_end_utc,
        "season6_sup_reward_start_delay_days": str(config.reward_start_delay_days),
        "season6_sup_projection_model": config.projection_model,
        "season6_sup_future_dilution_enabled": "true" if int(config.expected_future_settlement_interval_seconds or 0) > 0 else "false",
        "season6_sup_expected_future_settlement_interval_seconds": str(config.expected_future_settlement_interval_seconds),
        "season6_sup_settled_win_count_to_date": str(len(win_rows)),
        "season6_sup_total_allocated": season6_decimal_display(config.total_sup, 0),
        "season6_sup_cap_per_wallet": season6_decimal_display(config.cap_sup, 0),
        "season6_sup_cap_percent_label": config.cap_percent_label,
        "season6_sup_xp_per_settled_win": season6_decimal_display(config.xp_per_settled_win, 0),
        "season6_sup_xp_start_utc": config.xp_start_utc,
        "season6_sup_reward_start_utc": config.reward_start_utc,
        "season6_sup_campaign_end_utc": config.campaign_end_utc,
        "season6_sup_days_remaining": season6_decimal_display(days_remaining, 2),
        "season6_sup_confirmed_wins": str(len(win_rows)),
        "season6_sup_confirmed_wallets": str(len(grouped)),
        "season6_sup_total_xp_confirmed": season6_decimal_display(sum((entry["xp"] for entry in grouped.values()), Decimal(0)), 0),
        "season6_sup_raw_allocated_to_date": season6_decimal_display(sum(earned_to_date.values(), Decimal(0))),
        "season6_sup_raw_projected_full_allocated": season6_decimal_display(sum(full_allocations.values(), Decimal(0))),
        "season6_sup_capped_projected_full_allocated": season6_decimal_display(sum((decimal_from(row["season6_capped_sup_projected_full"]) or Decimal(0)) for row in by_winner)),
        "season6_sup_unallocated_due_to_zero_xp": season6_decimal_display(full_unallocated + unallocated_to_date * Decimal(0)),
        "season6_sup_cap_overflow_policy": config.cap_overflow_policy,
        "season6_sup_usd_price_used": season6_decimal_display(sup_usd, 8) if sup_usd > 0 else "N/A",
        "season6_sup_usd_source_used": sup_usd_source,
        **current_metric_values,
        "season6_current_bidder_prior_wins": str(current_row.get("prior_s6_wins_confirmed", 0)),
        "season6_current_bidder_cap_remaining_sup": str(current_row.get("projected_cap_remaining_sup_if_current_bid_wins", "N/A")),
        "season6_current_bidder_projected_raw_sup_if_wins": str(current_row.get("projected_raw_sup_if_current_bid_wins", "N/A")),
        "season6_current_bidder_projected_capped_sup_if_wins": str(current_row.get("projected_capped_sup_if_current_bid_wins", "N/A")),
        "season6_current_bidder_projected_raw_usd_if_wins": str(current_row.get("projected_raw_usd_if_current_bid_wins", "N/A")),
        "season6_current_bidder_projected_capped_usd_if_wins": str(current_row.get("projected_capped_usd_if_current_bid_wins", "N/A")),
    }
    return {
        "season6_metrics": metrics,
        "season6_sup_by_winner": by_winner,
        "season6_sup_rewards_by_auction": rewards_by_auction,
        "season6_sup_current_bidder_status": current_status,
    }


def configured_price(symbol: str) -> tuple[Decimal, str] | None:
    env_name = f"{symbol.upper()}_USD_PRICE"
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        price = Decimal(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: invalid {env_name}: {exc}", file=sys.stderr)
        return None
    if price > 0:
        return price, f"env:{env_name}"
    return None


def fetch_token_usd_price(symbol: str, token_address: str) -> tuple[Decimal, str]:
    configured = configured_price(symbol)
    if configured:
        return configured

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "degen-dogs-mission3-builder/1.0"})
        with open_no_redirect(req, timeout=30) as response:
            data = read_bounded_json_response(response, EXTERNAL_JSON_MAX_RESPONSE_BYTES, "Dexscreener")
        candidates = []
        token_lower = token_address.lower()
        for pair in data.get("pairs") or []:
            if str(pair.get("chainId", "")).lower() != "base":
                continue
            base_token = pair.get("baseToken") or {}
            if str(base_token.get("address") or "").lower() != token_lower:
                continue
            try:
                price = Decimal(str(pair.get("priceUsd") or "0"))
                liquidity = Decimal(str((pair.get("liquidity") or {}).get("usd") or "0"))
            except Exception:
                continue
            if price <= 0:
                continue
            candidates.append((liquidity, price, pair))
        if candidates:
            _liquidity, price, pair = max(candidates, key=lambda item: item[0])
            source = f"dexscreener:{pair.get('dexId', 'unknown')}:{pair.get('pairAddress', '')}"
            return price, source
    except Exception as exc:  # noqa: BLE001
        print(f"warning: {symbol}/USD lookup failed via Dexscreener: {exc}", file=sys.stderr)
    return Decimal(0), "unavailable"


def optional_reward_decimal_str(value: Decimal | None, max_places: int = 6) -> str:
    return "N/A" if value is None else decimal_value_str(value, max_places)


def reward_token_stats(woof_usd: Decimal, sup_usd: Decimal, snapshot: RewardStreamSnapshot | None = None) -> dict[str, str]:
    snapshot = snapshot or load_reward_stream_snapshot()
    woof_per_dog = snapshot.woof_per_dog_per_day
    sup_per_dog = snapshot.sup_per_dog_per_day
    woof_flow_usd = snapshot.woof_flow_per_day * woof_usd
    sup_flow_usd = snapshot.sup_flow_per_day * sup_usd
    woof_received_usd = snapshot.woof_received * woof_usd if snapshot.woof_received is not None else None
    sup_received_usd = snapshot.sup_received * sup_usd if snapshot.sup_received is not None else None
    woof_per_dog_usd = woof_per_dog * woof_usd
    sup_per_dog_usd = sup_per_dog * sup_usd
    total_flow_usd = woof_flow_usd + sup_flow_usd
    total_per_dog_usd = woof_per_dog_usd + sup_per_dog_usd
    return {
        "reward_basis_dogs": decimal_value_str(snapshot.dogs_count, 0),
        "reward_basis_source": snapshot.basis_source,
        "reward_snapshot_utc": snapshot.snapshot_utc,
        "reward_excludes": snapshot.excludes,
        "reward_observed_dogs_count": decimal_value_str(snapshot.dogs_count, 0),
        "reward_observed_woof_received": optional_reward_decimal_str(snapshot.woof_received, 2),
        "reward_observed_woof_flow_per_day": decimal_value_str(snapshot.woof_flow_per_day, 2),
        "reward_observed_woof_per_dog_per_day": decimal_value_str(woof_per_dog, 12),
        "reward_observed_sup_received": optional_reward_decimal_str(snapshot.sup_received, 2),
        "reward_observed_sup_flow_per_day": decimal_value_str(snapshot.sup_flow_per_day, 2),
        "reward_observed_sup_per_dog_per_day": decimal_value_str(sup_per_dog, 16),
        "reward_basis_note": snapshot.note,
        "reward_woof_received": optional_reward_decimal_str(snapshot.woof_received, 2),
        "reward_woof_received_usd": optional_reward_decimal_str(woof_received_usd, 2),
        "reward_woof_flow_per_day": decimal_value_str(snapshot.woof_flow_per_day, 2),
        "reward_woof_flow_usd_per_day": decimal_value_str(woof_flow_usd, 2),
        "reward_woof_per_dog_per_day": decimal_value_str(woof_per_dog, 12),
        "reward_woof_per_dog_usd_per_day": decimal_value_str(woof_per_dog_usd, 6),
        "reward_sup_received": optional_reward_decimal_str(snapshot.sup_received, 2),
        "reward_sup_received_usd": optional_reward_decimal_str(sup_received_usd, 2),
        "reward_sup_flow_per_day": decimal_value_str(snapshot.sup_flow_per_day, 2),
        "reward_sup_flow_usd_per_day": decimal_value_str(sup_flow_usd, 2),
        "reward_sup_per_dog_per_day": decimal_value_str(sup_per_dog, 16),
        "reward_sup_per_dog_usd_per_day": decimal_value_str(sup_per_dog_usd, 6),
        "reward_total_flow_usd_per_day": decimal_value_str(total_flow_usd, 2),
        "reward_total_per_dog_usd_per_day": decimal_value_str(total_per_dog_usd, 6),
    }


def fetch_token_stats(block_tag: str) -> dict[str, str]:
    name = decode_abi_string(eth_call(WOOF, SELECTOR_NAME, block_tag))
    symbol = decode_abi_string(eth_call(WOOF, SELECTOR_SYMBOL, block_tag))
    decimals = decode_uint_call(eth_call(WOOF, SELECTOR_DECIMALS, block_tag))
    supply_raw = decode_uint_call(eth_call(WOOF, SELECTOR_TOTAL_SUPPLY, block_tag))
    eth_usd, eth_usd_source = fetch_eth_usd_price()
    sup_name = decode_abi_string(eth_call(SUP, SELECTOR_NAME, block_tag))
    sup_symbol = decode_abi_string(eth_call(SUP, SELECTOR_SYMBOL, block_tag))
    sup_decimals = decode_uint_call(eth_call(SUP, SELECTOR_DECIMALS, block_tag))
    woof_usd, woof_usd_source = fetch_token_usd_price("WOOF", WOOF)
    sup_usd, sup_usd_source = fetch_token_usd_price("SUP", SUP)
    return {
        "auction_house": AUCTION_HOUSE,
        "dog_nft": DEGEN_DOGS,
        "woof_token": WOOF,
        "woof_name": name,
        "woof_symbol": symbol,
        "woof_decimals": str(decimals),
        "woof_total_supply": decimal_str(supply_raw, decimals, 6),
        "woof_total_supply_raw": str(supply_raw),
        "woof_usd_price": decimal_value_str(woof_usd, 12),
        "woof_usd_source": woof_usd_source,
        "sup_token": SUP,
        "sup_name": sup_name,
        "sup_symbol": sup_symbol,
        "sup_decimals": str(sup_decimals),
        "sup_usd_price": decimal_value_str(sup_usd, 8),
        "sup_usd_source": sup_usd_source,
        "eth_usd_price": decimal_str(int(eth_usd * 100), 2, 2) if eth_usd else "0",
        "eth_usd_source": eth_usd_source,
        **reward_token_stats(woof_usd, sup_usd),
    }


def token_uri_data(token_id: int) -> str:
    return SELECTOR_TOKEN_URI + f"{token_id:x}".rjust(64, "0")


def exists_data(token_id: int) -> str:
    return SELECTOR_EXISTS + f"{token_id:x}".rjust(64, "0")


def fetch_dog_total_supply(block_tag: str) -> int:
    return decode_uint_call(eth_call(DEGEN_DOGS, SELECTOR_TOTAL_SUPPLY, block_tag))


def fetch_token_uri(token_id: int, block_tag: str) -> str:
    return decode_abi_string(eth_call(DEGEN_DOGS, token_uri_data(token_id), block_tag))


def normalize_metadata_url(url: str) -> str:
    normalized = str(url or "").strip()
    if normalized.lower().startswith("ipfs://"):
        ipfs_path = normalized[7:].removeprefix("ipfs/").lstrip("/")
        if not ipfs_path:
            raise ValueError("empty IPFS metadata URI")
        return "https://ipfs.io/ipfs/" + ipfs_path
    return normalized


def validate_metadata_url(url: str) -> str:
    normalized = normalize_metadata_url(url)
    if normalized.lower().startswith("data:"):
        return normalized
    try:
        parsed = urllib.parse.urlsplit(normalized)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid metadata URL") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("metadata URL must use HTTPS, IPFS, or an inline JSON data URI")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("metadata URL must not contain credentials")
    if not host or host not in DOG_METADATA_ALLOWED_HOSTS:
        raise ValueError(f"metadata host is not trusted: {host or '<missing>'}")
    if port not in {None, 443}:
        raise ValueError("metadata URL must use the default HTTPS port")
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def decode_metadata_json(payload: bytes) -> dict[str, Any]:
    if len(payload) > DOG_METADATA_MAX_RESPONSE_BYTES:
        raise ValueError("metadata JSON response exceeds the configured size limit")
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("metadata JSON response must be an object")
    return decoded


def decode_metadata_data_url(url: str) -> dict[str, Any]:
    header, separator, encoded = url.partition(",")
    if not separator:
        raise ValueError("invalid metadata data URI")
    parameters = header[5:].split(";")
    media_type = (parameters[0] or "text/plain").lower()
    if media_type != "application/json":
        raise ValueError("metadata data URI must contain application/json")
    is_base64 = any(parameter.lower() == "base64" for parameter in parameters[1:])
    if len(encoded) > DOG_METADATA_MAX_RESPONSE_BYTES * 2:
        raise ValueError("encoded metadata data URI exceeds the configured size limit")
    try:
        payload = base64.b64decode(encoded, validate=True) if is_base64 else urllib.parse.unquote_to_bytes(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid metadata data URI payload") from exc
    return decode_metadata_json(payload)


class MetadataRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validated = validate_metadata_url(newurl)
        if validated.lower().startswith("data:"):
            raise urllib.error.HTTPError(newurl, code, "metadata redirect to data URI rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, validated)


def fetch_url_json(url: str, timeout: int = 45) -> dict[str, Any]:
    validated = validate_metadata_url(url)
    if validated.lower().startswith("data:"):
        return decode_metadata_data_url(validated)
    req = urllib.request.Request(
        validated,
        headers={"Accept": "application/json", "User-Agent": "degen-dogs-mission3-builder/1.0"},
    )
    opener = urllib.request.build_opener(MetadataRedirectHandler())
    with opener.open(req, timeout=timeout) as response:
        content_type = ""
        if response.headers:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/json" and not content_type.endswith("+json"):
            raise ValueError(f"metadata response has unsafe content type: {content_type}")
        content_length = response.headers.get("Content-Length") if response.headers else None
        if content_length:
            try:
                if int(content_length) > DOG_METADATA_MAX_RESPONSE_BYTES:
                    raise ValueError("metadata JSON response exceeds the configured size limit")
            except ValueError as exc:
                if "exceeds" in str(exc):
                    raise
        payload = response.read(DOG_METADATA_MAX_RESPONSE_BYTES + 1)
    return decode_metadata_json(payload)


def simplified_dog_metadata(token_id: int, data: dict[str, Any]) -> dict[str, Any]:
    attrs = []
    for item in data.get("attributes") or []:
        if not isinstance(item, dict):
            continue
        trait_type = str(item.get("trait_type") or "").strip()
        value = str(item.get("value") or "").strip()
        if trait_type and value:
            attrs.append({"trait_type": trait_type, "value": value})
    image = str(data.get("image") or "")
    if image.startswith("ipfs://"):
        image = normalize_metadata_url(image)
    image = safe_dashboard_image(image)
    external_url = safe_dashboard_link(data.get("external_url")) or f"https://degendogs.club/#dog{token_id}"
    return {
        "token_id": token_id,
        "name": str(data.get("name") or f"Degen Dog #{token_id}"),
        "image_url": image,
        "external_url": external_url,
        "attributes": attrs,
    }


def metadata_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_dog_cache() -> dict[str, Any]:
    if not DOG_METADATA_CACHE.exists():
        return {}
    try:
        data = read_owned_json_file(DOG_METADATA_CACHE, 16_777_216, "Dog metadata cache")
        if not isinstance(data, dict) or data.get("schema_version") != 3:
            return {}
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return {}
        valid: dict[str, Any] = {}
        for key, entry in tokens.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
                continue
            uri_hash = str(entry.get("token_uri_sha256") or "")
            content_hash = str(entry.get("content_sha256") or "")
            metadata_hash = str(entry.get("metadata_sha256") or "")
            if not all(len(value) == 64 and all(ch in "0123456789abcdef" for ch in value) for value in (uri_hash, content_hash, metadata_hash)):
                continue
            if metadata_sha256(entry["metadata"]) != metadata_hash:
                continue
            fetched_at = str(entry.get("fetched_at_utc") or "")
            try:
                fetched_time = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - fetched_time.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                continue
            if age < -60 or age > DOG_METADATA_CACHE_MAX_AGE_SECONDS:
                continue
            valid[str(key)] = entry
        return valid
    except Exception:
        return {}


def write_dog_cache(cache: dict[str, Any]) -> None:
    payload = {"schema_version": 3, "tokens": cache}
    atomic_write_text(DOG_METADATA_CACHE, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


TokenUriOutcome = tuple[str, str]


def _token_uri_state_tag(block_tag: str, block_hash: str | None) -> str | dict[str, Any]:
    if block_hash is None:
        return block_tag
    normalized = str(block_hash).strip().lower()
    if (
        len(normalized) != 66
        or not normalized.startswith("0x")
        or any(character not in "0123456789abcdef" for character in normalized[2:])
    ):
        raise RuntimeError("tokenURI verification requires a canonical 32-byte snapshot block hash")
    return {"blockHash": normalized, "requireCanonical": True}


def _token_uri_nonexistent_outcome(error: dict[str, Any], token_id: int) -> TokenUriOutcome | None:
    expected_data = ERC721_NONEXISTENT_TOKEN_ERROR + f"{token_id:x}".rjust(64, "0")
    message = str(error.get("message") or "").strip().lower()
    data = str(error.get("data") or "").strip().lower()
    if error.get("code") == 3 and message.startswith("execution reverted") and data == expected_data:
        return ("unavailable", expected_data)
    return None


def _token_uri_provider_outcomes(
    url: str,
    token_ids: list[int],
    state_tag: str | dict[str, Any],
    *,
    timeout: int,
    deadline: float,
) -> list[TokenUriOutcome]:
    calls = [
        call
        for token_id in token_ids
        for call in (
            ("eth_call", [{"to": DEGEN_DOGS, "data": token_uri_data(token_id)}, state_tag]),
            ("eth_call", [{"to": DEGEN_DOGS, "data": exists_data(token_id)}, state_tag]),
        )
    ]
    payload = [
        {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
        for index, (method, params) in enumerate(calls)
    ]
    operator_urls = _same_operator_rpc_urls(url)
    attempts = max(len(operator_urls), RPC_ATTEMPTS)
    last_error: Exception | None = None
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        candidate = operator_urls[attempt % len(operator_urls)]
        try:
            items = post_json(
                payload,
                max(1, min(timeout, int(remaining) + 1)),
                candidate,
            )
            by_id = _validated_batch_items(items, len(calls))
            outcomes: list[TokenUriOutcome] = []
            for index, token_id in enumerate(token_ids):
                token_uri_item = by_id[index * 2]
                exists_item = by_id[index * 2 + 1]
                if "error" in exists_item:
                    raise RuntimeError("exists() RPC returned a contract error")
                exists_raw = exists_item.get("result")
                if not isinstance(exists_raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", exists_raw):
                    raise RuntimeError("exists() RPC returned a malformed ABI boolean")
                exists_value = int(exists_raw, 16)
                if exists_value not in {0, 1}:
                    raise RuntimeError("exists() RPC returned a non-boolean ABI value")
                if "error" in token_uri_item:
                    outcome = _token_uri_nonexistent_outcome(token_uri_item["error"], token_id)
                    if outcome is None:
                        raise RuntimeError(
                            "tokenURI RPC returned an unsupported contract error "
                            f"code={token_uri_item['error'].get('code')}"
                        )
                else:
                    raw = token_uri_item.get("result")
                    if not isinstance(raw, str):
                        raise RuntimeError("tokenURI RPC returned a non-string result")
                    uri = decode_abi_string(raw)
                    if not uri or uri != uri.strip():
                        raise RuntimeError("tokenURI RPC returned an empty or malformed URI")
                    outcome = ("uri", uri)
                if (outcome[0] == "uri") != bool(exists_value):
                    raise RuntimeError(f"exists()/tokenURI outcome mismatch for Dog #{token_id}")
                outcomes.append(outcome)
            return outcomes
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            message = str(exc)
            permanent = any(
                marker in message
                for marker in (
                    "HTTP 400",
                    "HTTP 401",
                    "HTTP 403",
                    "HTTP 404",
                    "unsupported contract error",
                    "exists()",
                    "non-string result",
                    "empty or malformed URI",
                    "JSON-RPC batch response",
                )
            )
            # Give each same-operator alias one chance, but never retry an
            # unsupported contract outcome indefinitely. HTTP 429/5xx and
            # transport failures remain bounded, jittered retries.
            if permanent and attempt + 1 >= len(operator_urls):
                raise
            if attempt == attempts - 1:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = random.uniform(0, min(4.0, 0.25 * (2**attempt)))
            if delay > 0:
                time.sleep(min(delay, remaining))
    if last_error is not None:
        raise last_error
    raise TimeoutError("tokenURI provider deadline exceeded")


def _token_uri_chunk_quorum(
    token_ids: list[int],
    state_tag: str | dict[str, Any],
    urls: list[str],
    *,
    timeout: int = 45,
) -> list[TokenUriOutcome]:
    required = RPC_QUORUM_SIZE
    scope = "token-uri-batch"
    active_urls = _responsive_rpc_urls(_independent_rpc_urls(urls), required, scope)
    if len(active_urls) < required:
        raise RuntimeError(
            f"tokenURI verification requires {required} independent Base RPC providers; configured={len(active_urls)}"
        )

    responses: queue.Queue[tuple[int, str, list[TokenUriOutcome] | None, Exception | None]] = queue.Queue()
    deadline = time.monotonic() + RPC_QUORUM_DEADLINE_SECONDS

    def worker(index: int, url: str) -> None:
        try:
            outcomes = _token_uri_provider_outcomes(
                url,
                token_ids,
                state_tag,
                timeout=timeout,
                deadline=deadline,
            )
            responses.put((index, url, outcomes, None))
        except Exception as exc:  # noqa: BLE001
            responses.put((index, url, None, exc))

    pending_indexes = set(range(len(active_urls)))
    grouped: dict[str, list[tuple[str, list[TokenUriOutcome]]]] = defaultdict(list)
    errors: list[str] = []
    for index, url in enumerate(active_urls):
        threading.Thread(
            target=worker,
            args=(index, url),
            name=f"token-uri-quorum-{index}",
            daemon=True,
        ).start()

    while pending_indexes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            index, url, outcomes, error = responses.get(timeout=remaining)
        except queue.Empty:
            break
        if index not in pending_indexes:
            continue
        pending_indexes.remove(index)
        if error is not None:
            _mark_rpc_pending_slow([url], scope)
            errors.append(f"{_redact_rpc_url(url)}: {error}")
        elif outcomes is not None:
            key = json.dumps(outcomes, ensure_ascii=False, separators=(",", ":"))
            grouped[key].append((url, outcomes))

        pending = len(pending_indexes)
        ordered = sorted(grouped.values(), key=len, reverse=True)
        winner = ordered[0] if ordered else []
        runner_up_votes = len(ordered[1]) if len(ordered) > 1 else 0
        if len(winner) >= required and len(winner) > runner_up_votes + pending:
            _mark_rpc_pending_slow([active_urls[item] for item in pending_indexes], scope)
            return winner[0][1]

    if pending_indexes:
        pending_urls = [active_urls[item] for item in pending_indexes]
        _mark_rpc_pending_slow(pending_urls, scope)
        errors.append(
            "deadline exceeded: " + ", ".join(_redact_rpc_url(url) for url in pending_urls[:3])
        )
    votes = sorted((len(group) for group in grouped.values()), reverse=True)
    detail = f" votes={votes}" if votes else ""
    if errors:
        detail += f" errors={'; '.join(errors[:3])}"
    raise RuntimeError(f"tokenURI RPC quorum disagreement: required={required}{detail}")


def fetch_token_uri_bindings(
    token_ids: list[int],
    block_tag: str,
    *,
    block_hash: str | None = None,
) -> dict[int, str | None]:
    """Verify exact tokenURI values or exact nonexistent-token reverts."""
    if not token_ids:
        return {}
    if not VERIFIED_SNAPSHOT_URLS or block_tag == "latest":
        return {token_id: fetch_token_uri(token_id, block_tag) for token_id in token_ids}

    urls = _independent_rpc_urls(VERIFIED_SNAPSHOT_URLS)
    if len(urls) < RPC_QUORUM_SIZE:
        raise RuntimeError(
            f"tokenURI verification requires {RPC_QUORUM_SIZE} independent Base RPC providers; configured={len(urls)}"
        )
    state_tag = _token_uri_state_tag(block_tag, block_hash)
    # Each token contributes tokenURI() and exists() calls. Keep the combined
    # request within the configured RPC batch cap and pace chunks so public
    # providers do not fail a correct snapshot merely due to burst throttling.
    token_chunk_size = max(1, RPC_BATCH_LIMIT // 2)
    chunks = [
        token_ids[index : index + token_chunk_size]
        for index in range(0, len(token_ids), token_chunk_size)
    ]
    pacing_lock = threading.Lock()
    next_chunk_start = 0.0

    def fetch_verified_chunk(chunk: list[int]) -> list[tuple[int, str | None]]:
        nonlocal next_chunk_start
        # Every chunk creates its own hard monotonic deadline after leaving the
        # worker queue. Production deliberately uses one chunk at a time to
        # avoid a public-RPC burst while providers within that chunk run in
        # parallel and must return an exact matching outcome vector.
        with pacing_lock:
            wait_seconds = next_chunk_start - time.monotonic()
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            next_chunk_start = time.monotonic() + TOKEN_URI_CHUNK_DELAY_SECONDS
        outcomes = _token_uri_chunk_quorum(chunk, state_tag, urls)
        values: list[tuple[int, str | None]] = []
        for token_id, (kind, value) in zip(chunk, outcomes):
            values.append((token_id, value if kind == "uri" else None))
        return values

    values_by_token: dict[int, str | None] = {}
    workers = max(1, min(TOKEN_URI_CHUNK_WORKERS, len(chunks)))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(fetch_verified_chunk, chunk) for chunk in chunks]
    try:
        for future in concurrent.futures.as_completed(futures):
            for token_id, value in future.result():
                values_by_token[token_id] = value
    except BaseException:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)

    if set(values_by_token) != set(token_ids):
        missing = sorted(set(token_ids).difference(values_by_token))
        raise RuntimeError(f"tokenURI RPC quorum result is incomplete; missing token ids={missing[:10]}")
    return {token_id: values_by_token[token_id] for token_id in token_ids}


def authoritative_metadata_record(token_id: int, block_tag: str, token_uri: str | None = None) -> dict[str, Any]:
    uri = str(token_uri if token_uri is not None else fetch_token_uri(token_id, block_tag)).strip()
    if not uri:
        raise ValueError(f"empty onchain tokenURI for Dog #{token_id}")
    raw = fetch_url_json(uri, timeout=DOG_METADATA_FALLBACK_TIMEOUT)
    metadata = simplified_dog_metadata(token_id, raw)
    return {
        "token_uri_sha256": hashlib.sha256(uri.encode("utf-8")).hexdigest(),
        "content_sha256": metadata_sha256(raw),
        "metadata_sha256": metadata_sha256(metadata),
        "verified_block": _block_tag_number(block_tag),
        "fetched_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "metadata": metadata,
    }


def fetch_one_dog_metadata(token_id: int, block_tag: str) -> dict[str, Any]:
    return authoritative_metadata_record(token_id, block_tag)["metadata"]


def fetch_one_dog_metadata_with_deadline(token_id: int, block_tag: str) -> dict[str, Any]:
    def timeout_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise TimeoutError(f"dog metadata fetch timed out after {DOG_METADATA_ITEM_TIMEOUT}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, DOG_METADATA_ITEM_TIMEOUT)
    try:
        return fetch_one_dog_metadata(token_id, block_tag)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def fetch_dog_metadata_rows(
    total_supply: int,
    block_tag: str,
    *,
    token_uris: dict[int, str | None] | None = None,
) -> list[dict[str, Any]]:
    cache = load_dog_cache()
    token_ids = list(range(total_supply))
    if token_uris is None:
        token_uris = fetch_token_uri_bindings(token_ids, block_tag)
    else:
        token_uris = dict(token_uris)
        expected_ids = set(token_ids)
        if set(token_uris) != expected_ids:
            missing = sorted(expected_ids.difference(token_uris))
            unexpected = sorted(set(token_uris).difference(expected_ids))
            raise RuntimeError(
                "preverified tokenURI bindings do not match the Dog supply: "
                f"missing={missing[:10]} unexpected={unexpected[:10]}"
            )
    unavailable_ids = {
        token_id
        for token_id, uri in token_uris.items()
        if uri is None
    }
    # A current, independently agreed ERC721NonexistentToken revert cannot be
    # rebound to the legacy mirror cache. Omit that metadata rather than
    # fabricating current tokenURI provenance for a burned/sparse token.
    cache_changed = False
    for token_id in unavailable_ids:
        if cache.pop(str(token_id), None) is not None:
            cache_changed = True
    missing = [
        token_id
        for token_id in token_ids
        if token_id not in unavailable_ids
        and (
            str(token_id) not in cache
            or str(cache[str(token_id)].get("token_uri_sha256") or "")
            != hashlib.sha256(str(token_uris[token_id]).encode("utf-8")).hexdigest()
        )
    ]
    if missing:
        # Never serve an entry bound to a different onchain tokenURI merely
        # because the replacement metadata endpoint is temporarily failing.
        for token_id in missing:
            cache.pop(str(token_id), None)
        workers = max(1, min(int(os.environ.get("DOG_METADATA_WORKERS", "16")), 24))
        print(f"fetching dog metadata: {len(missing)} missing of {total_supply}", file=sys.stderr)
        if len(missing) <= DOG_METADATA_SEQUENTIAL_THRESHOLD:
            for token_id in missing:
                try:
                    cache[str(token_id)] = authoritative_metadata_record(token_id, block_tag, token_uris[token_id])
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: metadata failed for dog {token_id}: {exc}", file=sys.stderr)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(authoritative_metadata_record, token_id, block_tag, token_uris[token_id]): token_id
                    for token_id in missing
                }
                for future in concurrent.futures.as_completed(futures):
                    token_id = futures[future]
                    try:
                        cache[str(token_id)] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        print(f"warning: metadata failed for dog {token_id}: {exc}", file=sys.stderr)
        write_dog_cache(cache)
    elif cache_changed:
        write_dog_cache(cache)

    metadata = []
    for token_id in token_ids:
        entry = cache.get(str(token_id)) or {}
        verified_metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else None
        row = dict(verified_metadata or simplified_dog_metadata(token_id, {}))
        row["token_id"] = int(row.get("token_id") or token_id)
        row["_metadata_verified"] = verified_metadata is not None
        row["_metadata_verification_status"] = (
            "onchain_token_uri_unavailable"
            if token_id in unavailable_ids
            else "onchain_token_uri_verified" if verified_metadata is not None else "unavailable"
        )
        metadata.append(row)

    verified_attributes: dict[int, list[dict[str, str]]] = {}
    trait_counts: Counter[tuple[str, str]] = Counter()
    for row in metadata:
        if not row["_metadata_verified"]:
            continue
        token_id = int(row["token_id"])
        attributes_by_type: dict[str, str] = {}
        seen_trait_types: set[str] = set()
        for attr in row.get("attributes") or []:
            trait_type = str(attr.get("trait_type") or "").strip()
            value = str(attr.get("value") or "").strip()
            if not trait_type or not value:
                continue
            if trait_type in seen_trait_types:
                raise RuntimeError(f"Dog #{token_id} metadata repeats rarity trait {trait_type!r}")
            if ";" in trait_type or ";" in value:
                raise RuntimeError(f"Dog #{token_id} metadata contains an unsafe rarity trait delimiter")
            seen_trait_types.add(trait_type)
            attributes_by_type[trait_type] = value
        actual_trait_types = set(attributes_by_type)
        expected_trait_types = set(DOG_RARITY_TRAIT_TYPES)
        if actual_trait_types != expected_trait_types:
            missing_traits = sorted(expected_trait_types.difference(actual_trait_types))
            unexpected_traits = sorted(actual_trait_types.difference(expected_trait_types))
            raise RuntimeError(
                f"Dog #{token_id} rarity trait schema mismatch: "
                f"missing={missing_traits} unexpected={unexpected_traits}"
            )
        attributes = [
            {"trait_type": trait_type, "value": attributes_by_type[trait_type]}
            for trait_type in DOG_RARITY_TRAIT_TYPES
        ]
        for attr in attributes:
            trait_counts[(attr["trait_type"], attr["value"])] += 1
        verified_attributes[token_id] = attributes

    # totalSupply is an ID-space counter on this bridged collection: some IDs
    # have an exact, hash-pinned ERC721NonexistentToken outcome on Base. Those
    # IDs are not Dogs and must not dilute trait frequencies. A URI that exists
    # but whose metadata could not be fetched is different and still fails the
    # collection-wide calculation closed.
    rarity_universe_size = len(verified_attributes)
    rarity_universe_complete = rarity_universe_size > 0 and all(
        row["_metadata_verified"]
        or row["_metadata_verification_status"] == "onchain_token_uri_unavailable"
        for row in metadata
    )

    score_by_token: dict[int, Decimal] = {}
    for token_id, attributes in verified_attributes.items():
        score = Decimal(0)
        for attr in attributes:
            key = (attr["trait_type"], attr["value"])
            count = max(1, trait_counts.get(key, 1))
            score += Decimal(rarity_universe_size) / Decimal(count)
        score_by_token[token_id] = score
    ranks: dict[int, int] = {}
    if rarity_universe_complete:
        previous_score: Decimal | None = None
        competition_rank = 0
        for position, token_id in enumerate(
            sorted(score_by_token, key=lambda tid: (-score_by_token[tid], tid)),
            start=1,
        ):
            if previous_score is None or score_by_token[token_id] != previous_score:
                competition_rank = position
                previous_score = score_by_token[token_id]
            ranks[token_id] = competition_rank

    rows: list[dict[str, Any]] = []
    for row in metadata:
        token_id = int(row["token_id"])
        attrs = verified_attributes.get(token_id, [])
        traits = []
        rarity_items = []
        for attr in attrs:
            trait_type = str(attr.get("trait_type") or "")
            value = str(attr.get("value") or "")
            if not trait_type or not value:
                continue
            traits.append(f"{trait_type}: {value}")
            if rarity_universe_complete:
                count = trait_counts[(trait_type, value)]
                pct = (Decimal(count) * Decimal(100)) / Decimal(rarity_universe_size)
                rarity_items.append(f"{trait_type}: {value} ({pct:.1f}%)")
        rows.append(
            {
                "token_id": token_id,
                "dog_name": row.get("name") or f"Degen Dog #{token_id}",
                "dog_image_url": row.get("image_url") or "",
                "dog_external_url": row.get("external_url") or f"https://degendogs.club/#dog{token_id}",
                "dog_opensea_url": dog_opensea_url(token_id),
                "traits": "; ".join(traits),
                "trait_rarity": "; ".join(rarity_items),
                "rarity": f"#{ranks[token_id]}/{rarity_universe_size}" if token_id in ranks else "Unavailable",
                "rarity_score": round(float(score_by_token[token_id]), 6) if token_id in ranks else None,
                "metadata_verification_status": row["_metadata_verification_status"],
            }
        )
    rows.sort(key=lambda item: item["token_id"])
    return rows


def fetch_current_auction(latest_block: int, latest_time: str, block_tag: str) -> dict[str, Any]:
    raw = eth_call(AUCTION_HOUSE, SELECTOR_AUCTION, block_tag)
    token_id = word(raw, 0)
    amount = word(raw, 1)
    start_ts = word(raw, 2)
    end_ts = word(raw, 3)
    bidder = word_address(raw, 4)
    settled = word(raw, 5)
    return {
        "token_id": token_id,
        "amount_eth": float(Decimal(amount) / Decimal(10**18)),
        "amount_eth_exact": decimal_str(amount, 18),
        "amount_wei": str(amount),
        "start_time_utc": utc_from_unix(start_ts) if start_ts else "",
        "end_time_utc": utc_from_unix(end_ts) if end_ts else "",
        "bidder": bidder,
        "settled": int(settled),
        "latest_block": latest_block,
        "latest_block_time_utc": latest_time,
    }

def load_neynar_api_key() -> str | None:
    if os.environ.get("NEYNAR_API_KEY"):
        return os.environ["NEYNAR_API_KEY"]
    candidates = [
        Path.home() / ".hermes" / "skills" / "openclaw-imports" / "neynar" / "config.json",
        Path.home() / ".clawdbot" / "skills" / "neynar" / "config.json",
    ]
    for config_path in candidates:
        if not config_path.exists():
            continue
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            key = data.get("apiKey") or data.get("api_key")
            if key:
                return str(key)
        except Exception:
            continue
    return None


def normalize_address(address: str | None) -> str:
    if not address:
        return ""
    text = str(address).strip().lower()
    return (
        text
        if text.startswith("0x")
        and len(text) == 42
        and all(character in "0123456789abcdef" for character in text[2:])
        else ""
    )


def short_address(address: str) -> str:
    normalized = normalize_address(address)
    if not normalized:
        return ""
    return f"{normalized[:6]}…{normalized[-4:]}"


def basescan_address_url(address: str | None) -> str:
    normalized = normalize_address(address)
    if not normalized or normalized == ZERO:
        return ""
    return f"https://basescan.org/address/{normalized}"


def basescan_tx_url(tx_hash: str | None) -> str:
    text = text_value(tx_hash)
    if not text.startswith("0x") or len(text) < 10:
        return ""
    return f"https://basescan.org/tx/{text}"


def collect_identity_addresses(
    current: dict[str, Any],
    bids: list[dict[str, Any]],
    settled: list[dict[str, Any]],
    holders: list[dict[str, Any]],
) -> list[str]:
    addresses: set[str] = set()
    for value in [current.get("bidder")]:
        normalized = normalize_address(value)
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    for row in bids:
        normalized = normalize_address(row.get("bidder"))
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    for row in settled:
        normalized = normalize_address(row.get("winner"))
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    for row in holders[:100]:
        normalized = normalize_address(row.get("address"))
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    return sorted(addresses)


def pick_farcaster_user(address: str, users: Any) -> dict[str, Any] | None:
    if not isinstance(users, list):
        return None
    candidates = [user for user in users if isinstance(user, dict)]
    if not candidates:
        return None
    address_lc = normalize_address(address)

    def score(user: dict[str, Any]) -> tuple[int, int, int]:
        raw_verifications = user.get("verifications")
        verified = [normalize_address(a) for a in raw_verifications] if isinstance(raw_verifications, list) else []
        verified_addresses = user.get("verified_addresses")
        verified_addresses = verified_addresses if isinstance(verified_addresses, dict) else {}
        primary_record = verified_addresses.get("primary")
        primary_record = primary_record if isinstance(primary_record, dict) else {}
        primary = normalize_address(primary_record.get("eth_address"))
        raw_eth_addresses = verified_addresses.get("eth_addresses")
        eth_addresses = [normalize_address(a) for a in raw_eth_addresses] if isinstance(raw_eth_addresses, list) else []
        is_primary = int(primary == address_lc)
        is_verified = int(address_lc in verified or address_lc in eth_addresses)
        try:
            followers = max(0, int(user.get("follower_count") or 0))
        except (TypeError, ValueError):
            followers = 0
        return (is_primary, is_verified, followers)

    return max(candidates, key=score)


def fetch_farcaster_profiles(addresses: list[str]) -> list[dict[str, Any]]:
    api_key = load_neynar_api_key()
    rows: list[dict[str, Any]] = []
    if not api_key or not addresses:
        return rows
    chunk_size = 100
    for i in range(0, len(addresses), chunk_size):
        chunk = addresses[i : i + chunk_size]
        query = ",".join(chunk)
        url = "https://api.neynar.com/v2/farcaster/user/bulk-by-address?" + urllib.parse.urlencode({"addresses": query})
        last: Exception | None = None
        data: dict[str, Any] | None = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"accept": "application/json", "x-api-key": api_key})
                with open_no_redirect(req, timeout=45) as response:
                    data = read_bounded_json_response(response, EXTERNAL_JSON_MAX_RESPONSE_BYTES, "Neynar")
                break
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in {401, 403}:
                    rows.sort(key=lambda row: row["address"])
                    print(f"warning: Neynar wallet lookup disabled after HTTP {exc.code}; check NEYNAR_API_KEY", file=sys.stderr)
                    return rows
                if attempt == 3:
                    print(f"warning: Neynar wallet lookup failed for {len(chunk)} addresses: HTTP {exc.code}", file=sys.stderr)
                    data = {}
                    break
                time.sleep(1.5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt == 3:
                    print(f"warning: Neynar wallet lookup failed for {len(chunk)} addresses: {last}", file=sys.stderr)
                    data = {}
                    break
                time.sleep(1.5 * (attempt + 1))
        if not isinstance(data, dict) or not data:
            continue
        for address in chunk:
            users = data.get(address) or data.get(address.lower()) or data.get(address.upper()) or []
            user = pick_farcaster_user(address, users)
            if not user:
                continue
            try:
                fid = max(0, int(user.get("fid") or 0))
            except (TypeError, ValueError):
                fid = 0
            rows.append(
                {
                    "address": address.lower(),
                    "fid": fid,
                    "username": str(user.get("username") or "").lstrip("@"),
                    "display_name": str(user.get("display_name") or ""),
                    "pfp_url": str(user.get("pfp_url") or ""),
                }
            )
    rows.sort(key=lambda row: row["address"])
    return rows


def fetch_degendogs_auction_profiles(current: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback identity source used by the live Degen Dogs miniapp.

    Neynar only resolves addresses that are currently indexed as Farcaster custody
    or verified addresses. The official auction API also returns the usernames used
    by the miniapp for current-auction bidders, so use it to link both the current
    high bidder and every visible bid-history row when Neynar has no match.
    """
    current_token_id = int(current.get("token_id") or 0)
    current_bidder = normalize_address(current.get("bidder"))
    if not current_token_id or not current_bidder or current_bidder == ZERO:
        return []
    url = "https://degendogs.club/api/auctionData"
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "degen-dogs-mission3-builder/1.0"})
        with open_no_redirect(req, timeout=45) as response:
            data = read_bounded_json_response(response, EXTERNAL_JSON_MAX_RESPONSE_BYTES, "Degen Dogs auction API")
    except Exception as exc:  # noqa: BLE001
        print(f"warning: Degen Dogs auction identity lookup failed: {exc}", file=sys.stderr)
        return []

    if not isinstance(data, dict):
        return []
    try:
        api_token_id = int(data.get("nounId") or 0)
    except (TypeError, ValueError):
        return []
    if api_token_id != current_token_id:
        return []
    api_bidder = normalize_address(data.get("bidder"))
    if api_bidder != current_bidder:
        return []
    api_amount = data.get("amount")
    if api_amount is not None:
        try:
            if abs(Decimal(str(api_amount)) - Decimal(str(current.get("amount_eth") or 0))) > Decimal("0.000000000001"):
                return []
        except Exception:
            return []

    rows_by_address: dict[str, dict[str, Any]] = {}

    def add_profile(address: str | None, username: str | None, pfp_url: str | None = "") -> None:
        bidder = normalize_address(address)
        handle = str(username or "").strip().lstrip("@")
        if not bidder or bidder == ZERO or not handle:
            return
        rows_by_address.setdefault(
            bidder.lower(),
            {
                "address": bidder.lower(),
                "fid": 0,
                "username": handle,
                "display_name": handle,
                "pfp_url": str(pfp_url or ""),
            },
        )

    add_profile(current_bidder, data.get("username"), data.get("pfp_url"))
    raw_bids = data.get("bids")
    for bid in raw_bids if isinstance(raw_bids, list) else []:
        if not isinstance(bid, dict):
            continue
        try:
            bid_token_id = int(bid.get("nounId") or api_token_id)
        except (TypeError, ValueError):
            continue
        if bid_token_id != current_token_id:
            continue
        add_profile(bid.get("bidder"), bid.get("username"), bid.get("pfp_url"))
    return [rows_by_address[key] for key in sorted(rows_by_address)]


def load_cached_farcaster_profiles(path: Path = IDENTITY_PATH) -> list[dict[str, Any]]:
    """Load Farcaster identities previously confirmed by generated archive data.

    This cache lets the dashboard keep wallet→Farcaster labels for current and
    recent auction rows even when Neynar misses a wallet on the next refresh.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    rows: list[dict[str, Any]] = []
    for key, profile in data.items():
        if not isinstance(profile, dict):
            continue
        address = normalize_address(profile.get("wallet") or key)
        if not address or address == ZERO:
            continue
        profile_url = first_text(profile.get("profile_url"))
        handle = first_text(profile.get("farcaster_handle"))
        display = first_text(profile.get("display"))
        if not handle and display.startswith("@"):
            handle = display.lstrip("@")
        if not handle and "farcaster.xyz" in profile_url:
            parsed = urllib.parse.urlparse(profile_url)
            handle = parsed.path.strip("/").split("/")[0]
        handle = handle.strip().lstrip("@")
        if not handle:
            continue
        rows.append(
            {
                "address": address.lower(),
                "fid": int(profile.get("farcaster_fid") or 0),
                "username": handle,
                "display_name": display or handle,
                "pfp_url": str(profile.get("pfp_url") or ""),
            }
        )
    rows.sort(key=lambda row: row["address"])
    return rows


def merge_farcaster_profiles(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rows in sources:
        for row in rows:
            address = normalize_address(row.get("address"))
            if not address:
                continue
            key = address.lower()
            normalized = {
                "address": key,
                "fid": int(row.get("fid") or 0),
                "username": str(row.get("username") or "").strip().lstrip("@"),
                "display_name": str(row.get("display_name") or ""),
                "pfp_url": str(row.get("pfp_url") or ""),
            }
            existing = merged.get(key)
            if not existing:
                merged[key] = normalized
                continue
            for field in ["fid", "username", "display_name", "pfp_url"]:
                if not existing.get(field) and normalized.get(field):
                    existing[field] = normalized[field]
    return [merged[key] for key in sorted(merged)]


def decode_auction_logs(created_logs: list[dict[str, Any]], bid_logs: list[dict[str, Any]], settled_logs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    block_hashes: dict[int, str] = {}
    for log in bid_logs + settled_logs:
        block = int(log["blockNumber"], 16)
        block_hash = canonical_block_hash(log.get("blockHash"))
        if not block_hash:
            raise RuntimeError(f"auction event in block {block} is missing a canonical block hash")
        previous_hash = block_hashes.setdefault(block, block_hash)
        if previous_hash != block_hash:
            raise RuntimeError(f"auction events disagree on the canonical hash for block {block}")
    block_times = fetch_block_times(set(block_hashes), block_hashes)

    created = []
    for log in created_logs:
        token_id = topic_uint(log["topics"][1])
        created.append(
            {
                "token_id": token_id,
                "start_time_utc": utc_from_unix(word(log["data"], 0)),
                "end_time_utc": utc_from_unix(word(log["data"], 1)),
                "block_number": int(log["blockNumber"], 16),
                "tx_hash": log["transactionHash"],
            }
        )

    bids = []
    for log in bid_logs:
        block = int(log["blockNumber"], 16)
        value = word(log["data"], 1)
        bids.append(
            {
                "token_id": topic_uint(log["topics"][1]),
                "bidder": word_address(log["data"], 0),
                "bid_eth": float(Decimal(value) / Decimal(10**18)),
                "bid_eth_exact": decimal_str(value, 18),
                "bid_wei": str(value),
                "extended": int(word(log["data"], 2)),
                "block_number": block,
                "tx_hash": log["transactionHash"],
                "log_index": int(log["logIndex"], 16),
                "block_time_utc": block_times.get(block, ""),
            }
        )

    settled = []
    for log in settled_logs:
        block = int(log["blockNumber"], 16)
        amount = word(log["data"], 1)
        settled.append(
            {
                "token_id": topic_uint(log["topics"][1]),
                "winner": word_address(log["data"], 0),
                "amount_eth": float(Decimal(amount) / Decimal(10**18)),
                "amount_eth_exact": decimal_str(amount, 18),
                "amount_wei": str(amount),
                "block_number": block,
                "tx_hash": log["transactionHash"],
                "log_index": int(log["logIndex"], 16),
                "block_time_utc": block_times.get(block, ""),
            }
        )

    return created, bids, settled


def decode_auction_extension_logs(extension_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decode the verified AuctionExtended(uint256,uint256) event ABI."""
    extensions = [
        {
            "token_id": topic_uint(log["topics"][1]),
            "end_time_utc": utc_from_unix(word(log["data"], 0)),
            "block_number": int(log["blockNumber"], 16),
            "tx_hash": log["transactionHash"],
            "log_index": int(log.get("logIndex", "0x0"), 16),
        }
        for log in extension_logs
    ]
    extensions.sort(key=lambda row: (int(row["block_number"]), int(row["log_index"])))
    return extensions


def validate_auction_schedules(
    created: list[dict[str, Any]],
    extensions: list[dict[str, Any]],
    current: dict[str, Any],
) -> None:
    """Fail closed when extension history cannot produce one canonical end time."""
    created_by_token: dict[int, dict[str, Any]] = {}
    for row in created:
        token_id = int(row["token_id"])
        if token_id in created_by_token:
            raise RuntimeError(f"duplicate AuctionCreated events for Dog #{token_id}")
        created_by_token[token_id] = row

    effective_end = {token_id: str(row.get("end_time_utc") or "") for token_id, row in created_by_token.items()}
    previous_position: dict[int, tuple[int, int]] = {}
    for row in extensions:
        token_id = int(row["token_id"])
        created_row = created_by_token.get(token_id)
        if created_row is None:
            raise RuntimeError(f"AuctionExtended for Dog #{token_id} has no matching AuctionCreated event")
        position = (int(row.get("block_number") or 0), int(row.get("log_index") or 0))
        if position <= previous_position.get(token_id, (-1, -1)):
            raise RuntimeError(f"AuctionExtended ordering is ambiguous for Dog #{token_id}")
        if position[0] < int(created_row.get("block_number") or 0):
            raise RuntimeError(f"AuctionExtended precedes AuctionCreated for Dog #{token_id}")
        new_end = str(row.get("end_time_utc") or "")
        previous_end = effective_end.get(token_id, "")
        if not new_end or (previous_end and new_end <= previous_end):
            raise RuntimeError(
                f"AuctionExtended end time is not strictly later for Dog #{token_id}: "
                f"previous={previous_end or '<missing>'} observed={new_end or '<missing>'}"
            )
        effective_end[token_id] = new_end
        previous_position[token_id] = position

    current_token = int(current.get("token_id") or -1)
    current_end = str(current.get("end_time_utc") or "")
    expected_end = effective_end.get(current_token)
    if expected_end is not None and current_end != expected_end:
        raise RuntimeError(
            f"current auction end time disagrees with AuctionCreated/AuctionExtended logs for Dog #{current_token}: "
            f"getter={current_end or '<missing>'} logs={expected_end or '<missing>'}"
        )


def validate_exact_wei_rows(
    rows: list[dict[str, Any]],
    *,
    wei_field: str,
    eth_field: str,
    label: str,
) -> None:
    for row in rows:
        raw = str(row.get(wei_field) or "").strip()
        exact = str(row.get(eth_field) or "").strip()
        if not raw and not exact:
            continue
        if not raw.isdigit() or decimal_str(int(raw), 18) != exact:
            raise RuntimeError(
                f"{label} exact amount mismatch for Dog #{row.get('token_id', '<unknown>')}: "
                f"{wei_field}={raw or '<missing>'} {eth_field}={exact or '<missing>'}"
            )


def _balance_cache_enabled() -> bool:
    raw = os.environ.get("MISSION3_BALANCE_CACHE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _block_tag_number(block_tag: str) -> int:
    try:
        return int(str(block_tag), 16) if str(block_tag).startswith("0x") else int(block_tag)
    except (TypeError, ValueError):
        return 0


def validate_woof_holder_discovery_url(value: str) -> str:
    """Pin candidate discovery to Blockscout Base's exact token route."""
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RuntimeError("WOOF holder discovery URL is malformed") from exc
    expected_path = f"/api/v2/tokens/{WOOF}/holders"
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower().rstrip(".") != "base.blockscout.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.lower() != expected_path.lower()
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("WOOF holder discovery URL must use the pinned Blockscout Base token-holder route")
    return f"https://base.blockscout.com{expected_path}"


def fetch_woof_holder_candidates(url: str = WOOF_HOLDER_DISCOVERY_URL) -> list[str]:
    """Discover candidates offchain; no balance or completeness value is trusted."""
    base_url = validate_woof_holder_discovery_url(url)
    candidates: set[str] = set()
    next_params: dict[str, Any] | None = None
    seen_pages: set[str] = set()
    for _page_number in range(WOOF_HOLDER_DISCOVERY_MAX_PAGES):
        encoded_params = urllib.parse.urlencode(next_params or {})
        page_key = encoded_params or "<first>"
        if page_key in seen_pages:
            raise RuntimeError("Blockscout WOOF holder pagination repeated a page")
        seen_pages.add(page_key)
        page_url = base_url + (f"?{encoded_params}" if encoded_params else "")
        request = urllib.request.Request(
            page_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "degen-dogs-mission3-builder/1.0",
            },
        )
        with open_no_redirect(request, timeout=45) as response:
            payload = read_bounded_json_response(
                response,
                WOOF_HOLDER_DISCOVERY_MAX_RESPONSE_BYTES,
                "Blockscout WOOF holder discovery",
            )
        if not isinstance(payload, dict):
            raise RuntimeError("Blockscout WOOF holder response must be an object")
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > 100:
            raise RuntimeError("Blockscout WOOF holder response has an invalid items list")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("address"), dict):
                raise RuntimeError("Blockscout WOOF holder response contains a malformed item")
            address = normalize_address(item["address"].get("hash"))
            raw_value = str(item.get("value") or "").strip()
            if not address or not raw_value.isdigit():
                raise RuntimeError("Blockscout WOOF holder response contains an invalid address or value")
            if int(raw_value) > 0:
                candidates.add(address)
            if len(candidates) > WOOF_HOLDER_DISCOVERY_MAX_CANDIDATES:
                raise RuntimeError("Blockscout WOOF holder candidate cap exceeded")

        raw_next = payload.get("next_page_params")
        if raw_next is None:
            if not candidates:
                raise RuntimeError("Blockscout WOOF holder discovery returned no candidates")
            return sorted(candidates)
        if not isinstance(raw_next, dict) or set(raw_next) != {"value", "address_hash", "items_count"}:
            raise RuntimeError("Blockscout WOOF holder response has invalid pagination parameters")
        next_value = str(raw_next.get("value") or "").strip()
        next_address = normalize_address(raw_next.get("address_hash"))
        items_count = raw_next.get("items_count")
        if (
            not next_value.isdigit()
            or not next_address
            or type(items_count) is not int
            or items_count < 1
            or items_count > WOOF_HOLDER_DISCOVERY_MAX_CANDIDATES
        ):
            raise RuntimeError("Blockscout WOOF holder response has malformed pagination values")
        next_params = {
            "value": next_value,
            "address_hash": next_address,
            "items_count": items_count,
        }
    raise RuntimeError("Blockscout WOOF holder pagination cap exceeded")


def load_woof_balance_cache() -> dict[str, Any]:
    try:
        data = read_owned_json_file(WOOF_BALANCE_CACHE, 16_777_216, "WOOF balance cache")
    except RuntimeError:
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return {}
    if str(data.get("woof_token", "")).lower() != WOOF.lower():
        return {}
    balances = data.get("balances")
    if not isinstance(balances, dict):
        return {}
    checked_block = data.get("checked_block")
    if type(checked_block) is not int or checked_block < 0:
        return {}
    normalized_balances: dict[str, str] = {}
    for raw_address, raw_balance in balances.items():
        address = str(raw_address or "").strip().lower()
        if (
            len(address) != 42
            or not address.startswith("0x")
            or not all(character in "0123456789abcdef" for character in address[2:])
        ):
            return {}
        if isinstance(raw_balance, bool):
            return {}
        balance = str(raw_balance).strip()
        if not balance.isdigit():
            return {}
        normalized_balances[address] = str(int(balance))
    return {**data, "checked_block": checked_block, "balances": normalized_balances}


def save_woof_balance_cache(cache: dict[str, Any]) -> None:
    atomic_write_text(WOOF_BALANCE_CACHE, json.dumps(cache, separators=(",", ":"), sort_keys=True) + "\n")


def transfer_addresses(log: dict[str, Any]) -> list[str]:
    topics = log.get("topics", [])
    if len(topics) < 3:
        return []
    addresses = []
    for topic in (topics[1], topics[2]):
        address = topic_address(topic).lower()
        if address != ZERO:
            addresses.append(address)
    return addresses


def collect_woof_transfer_addresses(transfer_logs: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    touched_block_by_address: dict[str, int] = {}
    for log in transfer_logs:
        block_number = _block_number(log)
        for address in transfer_addresses(log):
            touched_block_by_address[address] = max(touched_block_by_address.get(address, 0), block_number)
    return sorted(touched_block_by_address, key=str.lower), touched_block_by_address


def fetch_balances(addresses: list[str], block_tag: str) -> dict[str, int]:
    sig = SELECTOR_BALANCE_OF
    batches = [
        addresses[index : index + RPC_BATCH_LIMIT]
        for index in range(0, len(addresses), RPC_BATCH_LIMIT)
    ]

    def fetch_batch(batch: list[str]) -> dict[str, int]:
        calls = []
        for address in batch:
            data = sig + address.lower().replace("0x", "").rjust(64, "0")
            calls.append(("eth_call", [{"to": WOOF, "data": data}, block_tag]))
        results = (
            rpc_batch_quorum(
                calls,
                urls=VERIFIED_SNAPSHOT_URLS,
                min_agreement=RPC_QUORUM_SIZE,
                timeout=60,
            )
            if VERIFIED_SNAPSHOT_URLS and block_tag != "latest"
            else rpc_batch(calls)
        )
        batch_balances: dict[str, int] = {}
        for address, raw in zip(batch, results):
            encoded = str(raw or "").lower()
            if (
                not encoded.startswith("0x")
                or len(encoded) <= 2
                or not all(character in "0123456789abcdef" for character in encoded[2:])
            ):
                raise RuntimeError(f"malformed WOOF balance response for {address}")
            batch_balances[address.lower()] = int(encoded, 16)
        if len(batch_balances) != len(batch):
            raise RuntimeError("WOOF balance batch returned an incomplete result")
        return batch_balances

    # Keep provider pressure bounded. rpc_batch_quorum already sends each batch
    # to independent providers concurrently; issuing many batches at once makes
    # public fallbacks rate-limit and delays fail-closed degradation.
    balances: dict[str, int] = {}
    for batch in batches:
        balances.update(fetch_batch(batch))
    return balances


def fetch_woof_holders(
    transfer_logs: list[dict[str, Any]],
    decimals: int,
    block_tag: str,
    expected_total_supply_raw: int | str | None = None,
    candidate_addresses: list[str] | None = None,
) -> list[dict[str, Any]]:
    if candidate_addresses is None:
        ordered, _touched_block_by_address = collect_woof_transfer_addresses(transfer_logs)
    else:
        normalized_candidates = [normalize_address(address) for address in candidate_addresses]
        if any(not address or address == ZERO for address in normalized_candidates):
            raise RuntimeError("WOOF holder candidate list contains an invalid address")
        ordered = sorted(set(normalized_candidates))
    snapshot_block = _block_tag_number(block_tag)
    cache = load_woof_balance_cache() if _balance_cache_enabled() else {}
    raw_cached_balances = cache.get("balances")
    cached_balances: dict[str, Any] = raw_cached_balances if isinstance(raw_cached_balances, dict) else {}
    checked_block = int(cache.get("checked_block") or 0) if cache else 0

    # WOOF is a SuperToken: agreement accounting can change balanceOf without
    # an ERC-20 Transfer event. A cache from any other block is therefore not
    # authoritative, even when no candidate address appears in new logs.
    to_fetch = (
        [address for address in ordered if address not in cached_balances]
        if cache and checked_block == snapshot_block
        else ordered
    )

    fresh_balances = fetch_balances(to_fetch, block_tag)
    merged_balances: dict[str, str] = {}
    for address in ordered:
        if address in fresh_balances:
            merged_balances[address] = str(fresh_balances[address])
        elif address in cached_balances:
            merged_balances[address] = str(cached_balances[address])
        else:
            raise RuntimeError(f"Missing WOOF balance for {address} at {block_tag}")

    if expected_total_supply_raw is not None:
        expected_supply_text = str(expected_total_supply_raw).strip()
        if not expected_supply_text.isdigit():
            raise RuntimeError("WOOF holder completeness check received an invalid total supply")
        expected_supply = int(expected_supply_text)
        observed_supply = sum(
            int(balance)
            for balance in merged_balances.values()
            if int(balance) != 0
        )
        # Transfer discovery intentionally excludes only the ERC-20 zero
        # address, whose balance is not part of circulating account balances.
        # Any burn/dead account is a normal nonzero address and remains in the
        # discovered set. Equality therefore proves no funded holder was lost
        # from a historical log response.
        if observed_supply != expected_supply:
            error = RuntimeError(
                "WOOF holder completeness mismatch at "
                f"{block_tag}: holder_balance_sum={observed_supply} total_supply={expected_supply}"
            )
            setattr(error, "observed_supply", observed_supply)
            setattr(error, "expected_supply", expected_supply)
            raise error

    if _balance_cache_enabled():
        save_woof_balance_cache(
            {
                "schema_version": 1,
                "woof_token": WOOF.lower(),
                "checked_block": snapshot_block,
                "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "balances": merged_balances,
            }
        )

    rows: list[dict[str, Any]] = []
    for address in ordered:
        balance = int(merged_balances.get(address) or 0)
        rows.append(
            {
                "address": address,
                "balance_woof": float(Decimal(balance) / (Decimal(10) ** decimals)),
                "balance_raw": str(balance),
            }
        )
    rows.sort(key=lambda r: (-r["balance_woof"], r["address"].lower()))
    return rows


def fetch_verified_woof_holders(
    decimals: int,
    block_tag: str,
    token_stats: dict[str, str],
) -> list[dict[str, Any]]:
    """Publish holder rows only when discovery closes against totalSupply."""
    candidates: list[str] = []
    token_stats["woof_holder_discovery_source"] = "base_blockscout_candidates_onchain_authority"
    try:
        candidates = fetch_woof_holder_candidates()
        rows = fetch_woof_holders(
            [],
            decimals,
            block_tag,
            token_stats["woof_total_supply_raw"],
            candidate_addresses=candidates,
        )
    except Exception as exc:  # noqa: BLE001
        token_stats["woof_holder_verification_status"] = "unavailable_fail_closed"
        token_stats["woof_holder_candidate_count"] = str(len(candidates))
        token_stats["woof_holder_verification_error"] = type(exc).__name__
        observed = getattr(exc, "observed_supply", None)
        expected = getattr(exc, "expected_supply", None)
        token_stats["woof_holder_balance_sum_raw"] = str(observed) if observed is not None else "unavailable"
        token_stats["woof_holder_expected_supply_raw"] = (
            str(expected) if expected is not None else token_stats.get("woof_total_supply_raw", "unavailable")
        )
        print(f"warning: WOOF holder surface disabled fail-closed: {exc}", file=sys.stderr)
        return []

    scopes = {
        scope.strip()
        for scope in token_stats.get("onchain_verification_scope", "").split(",")
        if scope.strip()
    }
    scopes.add("woof_holder_balances")
    token_stats["onchain_verification_scope"] = ",".join(sorted(scopes))
    observed_supply = sum(int(row["balance_raw"]) for row in rows)
    token_stats["woof_holder_verification_status"] = "candidate_complete_onchain_quorum_verified"
    token_stats["woof_holder_candidate_count"] = str(len(candidates))
    token_stats["woof_holder_balance_sum_raw"] = str(observed_supply)
    token_stats["woof_holder_expected_supply_raw"] = token_stats["woof_total_supply_raw"]
    return rows


def quote_ident(name: str) -> str:
    if not name or name[0].isdigit() or any(not (ch.isalnum() or ch == "_") for ch in name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]], schema: list[tuple[str, str]]) -> None:
    cols = [c for c, _ in schema]
    q_table = quote_ident(table)
    ddl_cols = []
    for col, typ in schema:
        if typ not in {"INTEGER", "REAL", "TEXT"}:
            raise ValueError(f"Invalid SQLite type: {typ!r}")
        ddl_cols.append(f"{quote_ident(col)} {typ}")
    q_cols = [quote_ident(col) for col in cols]
    drop_sql = f"DROP TABLE IF EXISTS {q_table}"
    create_sql = f"CREATE TABLE {q_table} ({', '.join(ddl_cols)})"
    conn.execute(drop_sql)
    conn.execute(create_sql)
    if not rows:
        return
    placeholders = ",".join("?" for _ in cols)
    insert_sql = f"INSERT INTO {q_table} ({', '.join(q_cols)}) VALUES ({placeholders})"
    conn.executemany(insert_sql, [[row.get(col) for col in cols] for row in rows])


def season6_table_schema(columns: list[str]) -> list[tuple[str, str]]:
    integer_columns = {
        "auction_id",
        "token_id",
        "season6_wins_confirmed",
        "season6_xp_confirmed",
        "season6_xp",
        "current_auction_token_id",
        "prior_s6_wins_confirmed",
        "prior_s6_xp_confirmed",
        "projected_s6_wins_if_current_bid_wins",
        "projected_s6_xp_if_current_bid_wins",
    }
    return [(column, "INTEGER" if column in integer_columns else "TEXT") for column in columns]


def insert_season6_outputs(conn: sqlite3.Connection, outputs: dict[str, Any]) -> None:
    metric_rows = [
        {"metric": str(key), "value": str(value)}
        for key, value in sorted((outputs.get("season6_metrics") or {}).items())
    ]
    insert_rows(conn, "season6_metrics", metric_rows, [("metric", "TEXT"), ("value", "TEXT")])
    if metric_rows:
        conn.executemany(
            "INSERT INTO mission3_metrics (metric, value) VALUES (?, ?)",
            [(row["metric"], row["value"]) for row in metric_rows],
        )

    for table_name, columns in {
        "season6_sup_by_winner": SEASON6_BY_WINNER_COLUMNS,
        "season6_sup_rewards_by_auction": SEASON6_REWARDS_BY_AUCTION_COLUMNS,
        "season6_sup_current_bidder_status": SEASON6_CURRENT_BIDDER_STATUS_COLUMNS,
    }.items():
        insert_rows(conn, table_name, outputs.get(table_name) or [], season6_table_schema(columns))


def fetch_table(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    select_sql = f"SELECT * FROM {quote_ident(table)}"
    cur = conn.execute(select_sql)
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def ensure_owned_directory_tree(directory: Path) -> None:
    """Create a directory without following a user-controlled ancestor symlink."""
    missing: list[Path] = []
    cursor = directory
    while True:
        try:
            details = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            if cursor.parent == cursor:
                raise RuntimeError(f"unable to find a trusted ancestor for output directory: {directory}")
            cursor = cursor.parent
            continue
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise RuntimeError(f"refusing unsafe output directory ancestor: {cursor}")
        if details.st_uid != os.getuid() or cursor.parent == cursor:
            break
        cursor = cursor.parent
    for item in reversed(missing):
        try:
            item.mkdir(mode=0o700)
        except FileExistsError:
            pass
        details = item.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
            raise RuntimeError(f"refusing unsafe output directory ancestor: {item}")


def atomic_write_text(path: Path, payload: str) -> None:
    ensure_owned_directory_tree(path.parent)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_csv(path: Path, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
    ensure_owned_directory_tree(path.parent)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(cols)
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
    data = [dict(zip(cols, row)) for row in rows]
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def table_dicts(cols: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(cols, row)) for row in rows]


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def first_text(*values: Any) -> str:
    for value in values:
        text = text_value(value)
        if text:
            return text
    return ""


def int_value(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def is_settled_status(value: Any) -> bool:
    status = text_value(value).lower()
    return status == "settled" or (status.startswith("settled") and "unsettled" not in status)


def normalize_archive_status_label(value: Any, mission: int = 0) -> str:
    status = text_value(value).lower().strip().replace("-", "_")
    squashed = " ".join(status.replace("_", " ").split())
    if not squashed:
        return ""
    if mission == 3:
        if squashed in {"live", "ongoing"}:
            return "live" if squashed == "live" else "ongoing"
        if "pending settlement" in squashed or "unsettled" in squashed or squashed in {"ended", "live or unsettled"}:
            return "ended pending settlement"
        if "settled" in squashed:
            return "settled"
    return text_value(value)


def load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def load_historical_price_rows(path: Path = HISTORICAL_PRICES_DAILY) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in load_json_list(path):
        normalized = {column: text_value(row.get(column)) for column, _typ in HISTORICAL_PRICE_SCHEMA}
        if normalized.get("asset_key") and normalized.get("date_utc") and normalized.get("price_usd"):
            rows.append(normalized)
    return rows


def chain_address_url(mission: int, address: str | None) -> str:
    normalized = normalize_address(address)
    if not normalized or normalized == ZERO:
        return ""
    if mission == 1:
        return f"https://polygonscan.com/address/{normalized}"
    if mission == 2:
        return f"https://explorer.degen.tips/address/{normalized}"
    return basescan_address_url(normalized)


def archive_amount(row: dict[str, Any], mission: int) -> str:
    if mission == 1:
        amount = first_text(row.get("amount_display_weth"), row.get("amount_weth"))
        return f"{amount} WETH" if amount else ""
    if mission == 2:
        amount = first_text(row.get("amount_degen"), row.get("amount_display_native"))
        return f"{amount} DEGEN" if amount else ""
    amount = first_text(row.get("amount_eth"), row.get("settled_amount_eth"))
    return f"{amount} ETH" if amount else ""


def archive_status(row: dict[str, Any], mission: int = 0) -> str:
    status = first_text(row.get("auction_status"), row.get("auction_state"), row.get("status"))
    if status:
        return normalize_archive_status_label(status, mission)
    settled = row.get("settled")
    if settled is True or text_value(settled).lower() in {"1", "true", "yes"} or row.get("settled_block"):
        return "settled"
    if settled is False or text_value(settled).lower() in {"0", "false", "no"}:
        return "ended pending settlement" if mission == 3 else "live_or_unsettled"
    if row:
        return "recovered"
    return "metadata_only"


def load_archive_lookup() -> tuple[dict[int, dict[str, Any]], int, int]:
    lookup: dict[int, dict[str, Any]] = {}
    mission1_max = 200
    mission3_min = 590
    for mission, path in HISTORICAL_ARCHIVE_INDEXES.items():
        rows = load_json_list(path)
        token_ids: list[int] = []
        for row in rows:
            token_id = int_value(row.get("token_id", row.get("dog_id")), -1)
            if token_id < 0:
                continue
            token_ids.append(token_id)
            enriched = dict(row)
            enriched["_archive_mission"] = mission
            lookup[token_id] = enriched
        if mission == 1 and token_ids:
            mission1_max = max(token_ids)
        if mission == 3 and token_ids:
            mission3_min = min(token_ids)
    return lookup, mission1_max, mission3_min


def mission_for_token(token_id: int, archive: dict[str, Any], mission1_max: int, mission3_min: int) -> int:
    archived_mission = int_value(archive.get("_archive_mission"), 0)
    if archived_mission in MISSION_CHAIN:
        return archived_mission
    if token_id <= mission1_max:
        return 1
    if token_id < mission3_min:
        return 2
    return 3


def source_text(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(text_value(item) for item in value if text_value(item))
    return text_value(value)


def build_search_text(row: dict[str, Any]) -> str:
    return " ".join(
        text_value(value)
        for value in row.values()
        if value is not None and not isinstance(value, (list, dict)) and text_value(value)
    )


def build_historical_dog_tables(
    conn: sqlite3.Connection,
    total_supply: int,
    dog_metadata: list[dict[str, Any]],
) -> None:
    metadata_by_token = {int_value(row.get("token_id"), -1): row for row in dog_metadata if int_value(row.get("token_id"), -1) >= 0}
    archive_lookup, mission1_max, mission3_min = load_archive_lookup()
    timeline_cols, timeline_rows = fetch_table(conn, "auction_timeline")
    winners_cols, winners_rows = fetch_table(conn, "auction_winners")
    current_cols, current_rows = fetch_table(conn, "current_auction")
    timeline_by_token = {int_value(row.get("token_id"), -1): row for row in table_dicts(timeline_cols, timeline_rows)}
    winners_by_token = {int_value(row.get("token_id"), -1): row for row in table_dicts(winners_cols, winners_rows)}
    current_by_token = {int_value(row.get("token_id"), -1): row for row in table_dicts(current_cols, current_rows)}

    search_rows: list[dict[str, Any]] = []
    for token_id in range(total_supply):
        metadata = metadata_by_token.get(token_id, {})
        archive = archive_lookup.get(token_id, {})
        mission = mission_for_token(token_id, archive, mission1_max, mission3_min)
        chain, chain_id = MISSION_CHAIN[mission]
        timeline = timeline_by_token.get(token_id, {}) if mission == 3 else {}
        winner = winners_by_token.get(token_id, {}) if mission == 3 else {}
        current = current_by_token.get(token_id, {}) if mission == 3 else {}

        dog_label = f"Dog #{token_id}"
        image_url = first_text(metadata.get("dog_image_url"), timeline.get("dog_image_url"), winner.get("dog_image_url"))
        external_url = first_text(metadata.get("dog_external_url"), f"https://degendogs.club/#dog{token_id}")
        opensea_url = first_text(metadata.get("dog_opensea_url"), winner.get("dog_opensea_url"), dog_opensea_url(token_id))
        traits = first_text(metadata.get("traits"), winner.get("traits"))
        trait_rarity = first_text(metadata.get("trait_rarity"), winner.get("trait_rarity"))
        rarity = first_text(metadata.get("rarity"), timeline.get("rarity"), winner.get("rarity"))

        if mission == 3 and (timeline or winner or current):
            status = normalize_archive_status_label(first_text(current.get("auction_state"), timeline.get("auction_state"), archive_status(archive, mission)), mission)
            amount = first_text(current.get("current_bid"), winner.get("winning_bid"))
            if not amount:
                settled_eth = first_text(timeline.get("settled_eth"), archive.get("amount_eth"))
                high_bid_eth = first_text(timeline.get("high_bid_eth"))
                amount = f"{settled_eth or high_bid_eth} ETH" if (settled_eth or high_bid_eth) else ""
            winner_label = first_text(winner.get("winner"), current.get("bidder"), timeline.get("winner"), timeline.get("latest_bidder"), archive.get("winner"))
            winner_url = first_text(winner.get("winner_url"), current.get("bidder_url"), timeline.get("winner_url"), timeline.get("latest_bidder_url"))
            winner_wallet = first_text(winner.get("winner_wallet"), current.get("bidder_wallet"))
            if not winner_url and winner_wallet:
                winner_url = chain_address_url(mission, winner_wallet)
            bid_count = int_value(first_text(timeline.get("bids"), winner.get("bid_count"), archive.get("bid_count")))
            unique_bidder_count = int_value(first_text(timeline.get("unique_bidders"), winner.get("unique_bidders"), archive.get("unique_bidder_count")))
            created_utc = first_text(timeline.get("start_time_utc"), archive.get("auction_created_time_utc"))
            settled_utc = first_text(winner.get("settled_time_utc"), timeline.get("settled_time_utc"), archive.get("settled_time_utc"))
            confidence = first_text(archive.get("confidence"), "verified_live_base_logs")
            sources = source_text(archive.get("sources")) or "base_logs,dashboard_builder"
        else:
            status = archive_status(archive, mission)
            amount = archive_amount(archive, mission)
            raw_winner = first_text(archive.get("winner"))
            winner_wallet = normalize_address(raw_winner)
            winner_label = short_address(winner_wallet) if winner_wallet else raw_winner
            winner_url = chain_address_url(mission, winner_wallet) if winner_wallet else ""
            bid_count = int_value(archive.get("bid_count"))
            unique_bidder_count = int_value(archive.get("unique_bidder_count"))
            created_utc = first_text(archive.get("auction_created_time_utc"), archive.get("mint_time_utc"))
            settled_utc = first_text(archive.get("settled_time_utc"))
            confidence = first_text(archive.get("confidence"), "metadata_only")
            sources = source_text(archive.get("sources")) or "dog_metadata"

        raw_amount = first_text(
            current.get("current_bid_wei"),
            winner.get("winning_bid_wei"),
            timeline.get("settled_wei"),
            timeline.get("latest_bid_wei"),
            archive.get("amount_raw"),
            archive.get("amount_wei"),
        )
        row = {
            "mission": mission,
            "chain": chain,
            "chain_id": chain_id,
            "token_id": token_id,
            "dog": dog_label,
            "dog_image_url": image_url,
            "dog_external_url": external_url,
            "dog_opensea_url": opensea_url,
            "status": status,
            "winner": winner_label,
            "winner_url": winner_url,
            "winner_wallet": winner_wallet,
            "amount": amount,
            "amount_raw": raw_amount,
            "bid_count": bid_count,
            "unique_bidder_count": unique_bidder_count,
            "auction_created_time_utc": created_utc,
            "settled_time_utc": settled_utc,
            "rarity": rarity,
            "traits": traits,
            "trait_rarity": trait_rarity,
            "metadata_verification_status": first_text(
                metadata.get("metadata_verification_status"),
                "unavailable",
            ),
            "confidence": confidence,
            "sources": sources,
        }
        row["search_text"] = build_search_text(row)
        search_rows.append(row)

    search_schema = [
        ("mission", "INTEGER"),
        ("chain", "TEXT"),
        ("chain_id", "INTEGER"),
        ("token_id", "INTEGER"),
        ("dog", "TEXT"),
        ("dog_image_url", "TEXT"),
        ("dog_external_url", "TEXT"),
        ("dog_opensea_url", "TEXT"),
        ("status", "TEXT"),
        ("winner", "TEXT"),
        ("winner_url", "TEXT"),
        ("winner_wallet", "TEXT"),
        ("amount", "TEXT"),
        ("amount_raw", "TEXT"),
        ("bid_count", "INTEGER"),
        ("unique_bidder_count", "INTEGER"),
        ("auction_created_time_utc", "TEXT"),
        ("settled_time_utc", "TEXT"),
        ("rarity", "TEXT"),
        ("traits", "TEXT"),
        ("trait_rarity", "TEXT"),
        ("metadata_verification_status", "TEXT"),
        ("confidence", "TEXT"),
        ("sources", "TEXT"),
        ("search_text", "TEXT"),
    ]
    insert_rows(conn, "historical_dog_search", search_rows, search_schema)

    def report_row(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        statuses = [text_value(row.get("status")).lower() for row in rows]
        created_times = [text_value(row.get("auction_created_time_utc")) for row in rows if text_value(row.get("auction_created_time_utc"))]
        activity_times = [
            text_value(value)
            for row in rows
            for value in (row.get("settled_time_utc"), row.get("auction_created_time_utc"))
            if text_value(value)
        ]
        winners = {text_value(row.get("winner_wallet") or row.get("winner")) for row in rows if text_value(row.get("winner_wallet") or row.get("winner"))}
        mission_int = int_value(label, 0)
        chain_name = "All missions" if label == "all" else MISSION_CHAIN.get(mission_int, ("", 0))[0]
        return {
            "mission": label,
            "chain": chain_name,
            "dogs": len(rows),
            "auctions_or_records": sum(1 for row in rows if text_value(row.get("auction_created_time_utc")) or text_value(row.get("settled_time_utc")) or text_value(row.get("amount"))),
            "settled": sum(1 for status in statuses if is_settled_status(status)),
            "live_or_unsettled": sum(1 for status in statuses if "live" in status or "ongoing" in status or "unsettled" in status or "pending settlement" in status or "created" in status),
            "metadata_only": sum(1 for status in statuses if status == "metadata_only"),
            "bid_count": sum(int_value(row.get("bid_count")) for row in rows),
            "unique_winners_or_high_bidders": len(winners),
            "first_auction_utc": min(created_times) if created_times else "",
            "latest_activity_utc": max(activity_times) if activity_times else "",
            "amount_note": "Per-Dog final/high bid is in historical_dog_search.amount; currencies differ by mission.",
            "confidence": "combined archived indexes + live Base dashboard metadata",
        }

    report_rows = [report_row("all", search_rows)]
    for mission in sorted(MISSION_CHAIN):
        mission_rows = [row for row in search_rows if int_value(row.get("mission")) == mission]
        report_rows.append(report_row(str(mission), mission_rows))
    report_schema = [
        ("mission", "TEXT"),
        ("chain", "TEXT"),
        ("dogs", "INTEGER"),
        ("auctions_or_records", "INTEGER"),
        ("settled", "INTEGER"),
        ("live_or_unsettled", "INTEGER"),
        ("metadata_only", "INTEGER"),
        ("bid_count", "INTEGER"),
        ("unique_winners_or_high_bidders", "INTEGER"),
        ("first_auction_utc", "TEXT"),
        ("latest_activity_utc", "TEXT"),
        ("amount_note", "TEXT"),
        ("confidence", "TEXT"),
    ]
    insert_rows(conn, "historical_dog_report", report_rows, report_schema)


HIDDEN_UI_COLUMNS = {
    "chain_id",
    "dog_image_url",
    "dog_external_url",
    "dog_opensea_url",
    "bidder_url",
    "winner_url",
    "holder_url",
    "latest_bidder_url",
    "bidder_winner_url",
    "bidder_wallet",
    "bidder_winner_wallet",
    "winner_wallet",
    "holder_wallet",
    "unique_bidders",
    "amount_eth",
    "amount_usd",
    "latest_bid_eth",
    "latest_bid_usd",
    "winning_bid_eth",
    "winning_bid_usd",
    "current_bid_eth",
    "current_bid_usd",
    "current_bid_usd_live",
    "bid_usd_at_event",
    "amount_usd_at_event",
    "winning_bid_usd_at_settlement",
    "eth_usd_price_live",
    "eth_usd_price_at_event",
    "eth_usd_price_date_utc",
    "eth_usd_price_source",
    "eth_usd_price_source_detail",
    "eth_usd_price_timestamp_utc",
    "usd_estimate_source",
    "usd_estimate_source_detail",
    "usd_estimate_confidence",
    "usd_estimate_basis",
    "time_remaining",
    "auction_end_utc",
    "end_time_utc",
    "last_bid_utc",
    "settled_time_utc",
    "traits",
    "trait_rarity",
    "metadata_verification_status",
    "rarity_score",
    "tx_hash",
    "created_tx_hash",
    "settled_tx_hash",
    "block_number",
    "log_index",
    "bid_wei",
    "amount_wei",
    "amount_raw",
    "sources",
    "search_text",
}


def css_class_for_col(col: str) -> str:
    lowered = col.lower()
    if lowered in {"status", "auction_state", "state"}:
        return "state"
    if lowered in {"dog", "dog_name"}:
        return "dog-col"
    if "winner" in lowered or "bidder" in lowered or "farcaster" in lowered or "wallet" in lowered or "holder" in lowered:
        return "identity"
    if "time" in lowered or lowered.endswith("utc") or "date" in lowered:
        return "time"
    numeric_markers = ("_eth", "_wei", "_pct", "_reward", "_balance", "count", "bids", "rank", "remaining", "usd")
    if any(marker in lowered for marker in numeric_markers) or lowered in {"eth", "bid", "reward", "balance", "supply_pct", "rarity"}:
        return "num"
    return ""


def display_col_name(col: str) -> str:
    overrides = {
        "token_id": "dog id",
        "bidder_winner": "high bidder / winner",
        "auction_time_utc": "last bid / settled",
        "auction_created_time_utc": "created",
        "amount": "final / high bid",
        "bid_count": "bids",
        "unique_bidder_count": "unique bidders",
        "unique_winners_or_high_bidders": "unique winners / high bidders",
        "last_bid_utc": "last bid",
        "settled_time_utc": "settled",
        "time_remaining": "time left",
    }
    return overrides.get(col, col.replace("_", " "))


def cell_url(col: str, row_data: dict[str, Any]) -> str:
    if col == "bidder_winner":
        return safe_dashboard_link(row_data.get("bidder_winner_url") or basescan_address_url(row_data.get("bidder_winner_wallet")))
    if col in {"bidder", "winner", "holder", "latest_bidder"}:
        return safe_dashboard_link(row_data.get(f"{col}_url") or basescan_address_url(row_data.get(f"{col}_wallet")))
    if col == "dog":
        return safe_dashboard_link(row_data.get("dog_opensea_url") or row_data.get("dog_external_url"))
    return ""


def render_cell(col: str, value: Any, row_data: dict[str, Any]) -> str:
    text = "" if value is None else str(value)
    escaped = html.escape(text)
    lowered = col.lower()
    if col == "dog":
        image = safe_dashboard_image(row_data.get("dog_image_url"))
        text_url = cell_url(col, row_data)
        image_url = safe_dashboard_link(row_data.get("dog_opensea_url"))
        image_html = ""
        if image:
            image_html = f'<img class="dog-thumb" src="{html.escape(image, quote=True)}" alt="{html.escape(text, quote=True)} image" loading="lazy">'
            if image_url:
                dog_label = text or "Dog"
                image_label = f"Open {dog_label} on OpenSea"
                image_html = (
                    f'<a class="dog-image-link" href="{html.escape(image_url, quote=True)}" target="_blank" '
                    f'rel="noopener noreferrer" aria-label="{html.escape(image_label, quote=True)}" '
                    f'title="{html.escape(image_label, quote=True)}">{image_html}</a>'
                )
        label_html = f'<span>{escaped}</span>'
        if text_url and text:
            label_html = f'<a class="dog-link" href="{html.escape(text_url, quote=True)}" target="_blank" rel="noopener noreferrer">{escaped}</a>'
        inner = f'<span class="dog-cell">{image_html}{label_html}</span>'
        return inner
    if lowered in {"status", "auction_state"}:
        tone = "ongoing" if "ongoing" in text or text == "live" else "settled" if is_settled_status(text) else "neutral"
        return f'<span class="status-pill {tone}">{escaped}</span>'
    if col == "auction_time_utc" and text:
        status = str(row_data.get("status") or row_data.get("auction_state") or "")
        label = "Settled" if is_settled_status(status) else "Last bid"
        return f'<span class="time-cell"><b>{html.escape(label)}</b>{escaped}</span>'
    if col == "time_remaining" and text:
        status = str(row_data.get("status") or row_data.get("auction_state") or "").lower()
        end_time = str(row_data.get("auction_end_utc") or row_data.get("end_time_utc") or "")
        if end_time and ("ongoing" in status or status == "live"):
            return f'<span class="countdown" data-countdown-end="{html.escape(end_time, quote=True)}">{escaped}</span>'
    url = cell_url(col, row_data)
    if url and text:
        return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escaped}</a>'
    return escaped


def table_html(name: str, cols: list[str], rows: list[tuple[Any, ...]], *, featured: bool = False) -> str:
    visible = [(idx, col) for idx, col in enumerate(cols) if col not in HIDDEN_UI_COLUMNS]
    head = "".join(
        f'<th scope="col" aria-sort="none" class="{css_class_for_col(col)}"><button type="button" data-col="{visible_idx}">{html.escape(display_col_name(col))}</button></th>'
        for visible_idx, (_, col) in enumerate(visible)
    )
    body = []
    for row in rows:
        row_data = {col: row[i] for i, col in enumerate(cols)}
        cells = []
        for _, col in visible:
            value = row_data.get(col)
            label = html.escape(display_col_name(col), quote=True)
            cells.append(f'<td class="{css_class_for_col(col)}" data-label="{label}">{render_cell(col, value, row_data)}</td>')
        search_blob = row_data.get("search_text") or " ".join(text_value(value) for value in row_data.values() if text_value(value))
        body.append(f'<tr data-search="{html.escape(str(search_blob), quote=True)}">' + "".join(cells) + "</tr>")
    row_count = len(rows)
    caption_class = "table-caption sr-only" if featured else "table-caption"
    table_label = html.escape(name.replace("_", " "))
    caption = (
        f'<caption class="{caption_class}"><span>{table_label}</span>'
        f'<span data-total="{row_count}">{row_count} rows</span></caption>'
    )
    featured_class = " featured-table" if featured else ""
    return f'<section class="table-card{featured_class}" data-name="{html.escape(name)}"><div class="table-scroll"><table data-table="{html.escape(name)}">{caption}<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div></section>'


def metric_lookup(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> dict[str, str]:
    cols, rows = tables.get("mission3_metrics", (["metric", "value"], []))
    try:
        metric_idx = cols.index("metric")
        value_idx = cols.index("value")
    except ValueError:
        return {}
    return {str(row[metric_idx]): str(row[value_idx]) for row in rows}


def current_lookup(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> dict[str, str]:
    cols, rows = tables.get("auction_feed", ([], []))
    if not rows:
        cols, rows = tables.get("current_latest_bid", ([], []))
    if not rows:
        return {}
    row = rows[0]
    return {col: "" if row[i] is None else str(row[i]) for i, col in enumerate(cols)}


def markdown_cell(value: Any) -> str:
    return str("" if value is None else value).replace("\n", " ").replace("|", "\\|")


def markdown_table(cols: list[str], rows: list[tuple[Any, ...]], limit: int | None = None) -> str:
    selected = rows if limit is None else rows[:limit]
    out = ["| " + " | ".join(markdown_cell(col) for col in cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for row in selected:
        out.append("| " + " | ".join(markdown_cell(v) for v in row) + " |")
    return "\n".join(out) + "\n"


def metric_value(metrics: dict[str, str], key: str, fallback: str = "") -> str:
    value = metrics.get(key, fallback)
    return str(value) if value is not None else fallback


def markdown_link(label: str, href: str) -> str:
    return f"[{markdown_cell(label)}]({href})"


def format_current_bid(metrics: dict[str, str]) -> str:
    bid_eth = metric_value(metrics, "current_bid_eth")
    bid_usd = metric_value(metrics, "current_bid_usd")
    if bid_eth and bid_usd:
        return f"{bid_eth} ETH (${bid_usd})"
    if bid_eth:
        return f"{bid_eth} ETH"
    return ""


def metric_decimal(metrics: dict[str, str], key: str) -> Decimal | None:
    raw = metric_value(metrics, key).replace(",", "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except Exception:
        return None


def format_decimal_display(value: Decimal, places: int = 2) -> str:
    return f"{value:,.{places}f}"


def reward_token_display(metrics: dict[str, str], token_key: str, usd_key: str, token: str, places: int = 2) -> str:
    amount = metric_decimal(metrics, token_key)
    usd = metric_decimal(metrics, usd_key)
    if amount is None:
        return ""
    amount_text = f"{format_decimal_display(amount, places)} {token}/day"
    if usd is not None:
        amount_text += f" (${format_decimal_display(usd, 2)}/day)"
    return amount_text


def reward_usd_display(metrics: dict[str, str], key: str) -> str:
    value = metric_decimal(metrics, key)
    if value is None:
        return ""
    return f"${format_decimal_display(value, 2)}/day"


def reward_payback_display(metrics: dict[str, str]) -> str:
    raw = metric_value(metrics, "reward_current_bid_payback_days").strip()
    days = metric_decimal(metrics, "reward_current_bid_payback_days")
    if days is None or days <= 0:
        return "N/A" if raw else ""
    if days < 1:
        return "<1 day"
    places = 1 if days < 10 else 0
    return f"≈{format_decimal_display(days, places)} days"


def reward_apr_display(metrics: dict[str, str]) -> str:
    explicit = metric_value(metrics, "reward_current_bid_apr_display").strip()
    if explicit:
        return explicit
    apr = metric_decimal(metrics, "reward_current_bid_apr_pct")
    return reward_apr_display_value(apr) if apr is not None else ""


def reward_payback_apr_summary(metrics: dict[str, str]) -> str:
    payback = reward_payback_display(metrics)
    apr = reward_apr_display(metrics)
    values = [value for value in (payback, apr) if value]
    return " / ".join(values)


def reward_basis_label(metrics: dict[str, str]) -> str:
    count = metric_value(metrics, "reward_basis_dogs").strip()
    source = metric_value(metrics, "reward_basis_source").strip().lower()
    if count and "observed" in source:
        return f"Observed {count}-Dog stream"
    if count:
        return f"{count}-Dog reward basis"
    return "Observed reward stream"


def reward_basis_summary(metrics: dict[str, str]) -> str:
    label = reward_basis_label(metrics)
    woof = metric_decimal(metrics, "reward_woof_per_dog_per_day")
    sup = metric_decimal(metrics, "reward_sup_per_dog_per_day")
    if woof is None or sup is None:
        return label
    return f"{label}: ≈{woof:,.0f} WOOF + ≈{sup:,.2f} SUP / Dog / day"


def render_reward_strip(metrics: dict[str, str]) -> str:
    woof = reward_token_display(metrics, "reward_woof_per_dog_per_day", "reward_woof_per_dog_usd_per_day", "WOOF", 2)
    sup = reward_token_display(metrics, "reward_sup_per_dog_per_day", "reward_sup_per_dog_usd_per_day", "SUP", 2)
    total = reward_usd_display(metrics, "reward_total_per_dog_usd_per_day")
    payback = reward_payback_display(metrics)
    apr = reward_apr_display(metrics)
    apr_copy = (
        "Simple APR estimate. Annualized from the current bid divided by the observed estimated "
        "per-Dog daily WOOF + SUP flow; excludes WOOF Vault Bonus; does not compound; "
        "changes with token prices, bid, auction state, and reward-flow assumptions; not guaranteed future return."
    )
    tiles = [
        ("WOOF / Dog", html.escape(woof), "Observed stream", ""),
        ("SUP / Dog", html.escape(sup), "Observed stream", ""),
        ("Total / Dog", html.escape(total), "WOOF + SUP", ""),
    ]
    body_parts = []
    for label, value_html, note, title in tiles:
        if not value_html:
            continue
        title_attr = f' title="{html.escape(title, quote=True)}"' if title else ""
        caveat_html = f'<small class="reward-caveat sr-only">{html.escape(title)}</small>' if title else ""
        body_parts.append(
            f'<span class="reward-tile"{title_attr}><b>{html.escape(label)}</b><strong>{value_html}</strong>'
            f'<em>{html.escape(note)}</em>{caveat_html}</span>'
        )
    season6_tile = render_season6_strip(metrics)
    if season6_tile:
        body_parts.append(season6_tile)
    payback_html = f'<span class="payback-days">{html.escape(payback)}</span><span class="payback-apr">{html.escape(apr)}</span>'
    if payback_html != '<span class="payback-days"></span><span class="payback-apr"></span>':
        body_parts.append(
            f'<span class="reward-tile" title="{html.escape(apr_copy, quote=True)}"><b>Bid payback</b>'
            f'<strong>{payback_html}</strong><em>Current bid / observed per-Dog flow</em>'
            f'<small class="reward-caveat sr-only">{html.escape(apr_copy)}</small></span>'
        )
    body = "".join(body_parts)
    if not body:
        return ""
    return f'<section class="reward-strip" aria-label="Per-Dog reward estimate">{body}</section>'


def comma_decimal_display(value: Any, places: int = 0, prefix: str = "", suffix: str = "") -> str:
    decimal = decimal_from(value)
    if decimal is None:
        return "N/A"
    quantized = decimal.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.{places}f}" if places > 0 else f"{quantized:,.0f}"
    if places > 0:
        text = text.rstrip("0").rstrip(".")
    return f"{prefix}{text}{suffix}"


def season6_sup_display(value: Any, *, approximate: bool = True) -> str:
    rendered = comma_decimal_display(value, 0, suffix=" SUP")
    if rendered == "N/A":
        return rendered
    return f"≈{rendered}" if approximate else rendered


def season6_usd_display(value: Any) -> str:
    rendered = comma_decimal_display(value, 0, prefix="$")
    if rendered == "N/A":
        return rendered
    return f"≈{rendered}"


def render_season6_strip(metrics: dict[str, str]) -> str:
    enabled = metric_value(metrics, "season6_sup_enabled") or ("true" if metric_value(metrics, "season6_sup_status") else "")
    if enabled.lower() not in {"true", "1", "yes", "live_estimate"}:
        return ""
    status = metric_value(metrics, "season6_sup_estimate_status", "estimated")
    estimate = metric_value(metrics, "season6_sup_current_bid_estimated_cap_aware_sup")
    estimate_usd = metric_value(metrics, "season6_sup_current_bid_estimated_cap_aware_usd")
    if status == "no_current_bid" or (not metric_value(metrics, "season6_sup_current_bidder_wallet") and not estimate):
        main = "Bid to estimate S6 SUP"
        secondary = "Current high bidder needed"
        note = "Wallet-level estimate."
    else:
        main = season6_sup_display(estimate)
        secondary = f"{season6_usd_display(estimate_usd)} if current bid wins"
        note = "Wallet estimate already near cap." if status == "wallet_near_cap" else "Adjusted for prior S6 wins; estimate only."
    title = "Season 6 cap-aware incremental SUP estimate. Detailed method lives in docs."
    return (
        f'<span class="reward-tile season6-sup-estimate" title="{html.escape(title, quote=True)}">'
        "<b>Season 6 SUP estimate</b>"
        f"<strong>{html.escape(main)}"
        f"<span>{html.escape(secondary)}</span></strong>"
        f"<em>{html.escape(note)}</em>"
        f'<small class="reward-caveat sr-only">{html.escape(title)}</small>'
        "</span>"
    )

def format_current_auction(metrics: dict[str, str]) -> str:
    token_id = metric_value(metrics, "current_auction_token_id")
    return f"Dog #{token_id}" if token_id else ""


def format_created_settled(metrics: dict[str, str]) -> str:
    created = metric_value(metrics, "created_auctions")
    settled = metric_value(metrics, "settled_auctions")
    return f"{created} / {settled}" if created and settled else ""


def season6_readme_estimate_summary(metrics: dict[str, str]) -> str:
    if (metric_value(metrics, "season6_sup_enabled") or "").lower() not in {"true", "1", "yes"}:
        return ""
    status = metric_value(metrics, "season6_sup_estimate_status")
    if status == "no_current_bid" or not metric_value(metrics, "season6_sup_current_bidder_wallet"):
        return "Bid to estimate S6 SUP"
    sup = season6_sup_display(metric_value(metrics, "season6_sup_current_bid_estimated_cap_aware_sup"))
    usd = season6_usd_display(metric_value(metrics, "season6_sup_current_bid_estimated_cap_aware_usd"))
    return f"{sup} / {usd}"


def woof_holder_summary(metrics: dict[str, str]) -> str:
    status = metric_value(metrics, "woof_holder_verification_status")
    if status != "candidate_complete_onchain_quorum_verified":
        return "Unavailable (onchain verification incomplete)"
    return metric_value(metrics, "woof_holders")


def render_readme_from_template(replacements: dict[str, str]) -> str:
    # README.md is generated because `npm run data` rewrites live snapshot sections.
    # Keep stable human-written copy in README.template.md and replace only explicit placeholders here.
    template = README_TEMPLATE_PATH.read_text(encoding="utf-8")
    for token, value in replacements.items():
        template = template.replace(token, value.rstrip())
    if "{{" in template and "}}" in template:
        raise RuntimeError("README template has unresolved placeholders")
    return template.rstrip() + "\n"


def render_readme(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]], manifest_rows: list[tuple[Any, ...]]) -> str:
    metrics = metric_lookup(tables)
    site_url = metric_value(metrics, "site_url", "https://ael-dev3.github.io/Degen-Dogs-Mission-3/")

    snapshot_rows = [
        ("site_url", site_url),
        ("Network", metric_value(metrics, "network", "base")),
        ("Snapshot block", metric_value(metrics, "latest_block")),
        ("Snapshot time UTC", metric_value(metrics, "latest_block_time_utc")),
        ("Snapshot block hash", metric_value(metrics, "snapshot_block_hash")),
        ("Onchain verification", metric_value(metrics, "onchain_verification_status")),
        ("Current Dog", format_current_auction(metrics)),
        ("Current status", metric_value(metrics, "current_auction_status")),
        ("Current bid", format_current_bid(metrics)),
        ("Current high bidder", metric_value(metrics, "current_bidder")),
        ("Bid payback / APR", reward_payback_apr_summary(metrics)),
        ("Season 6 SUP estimate if current bid wins", season6_readme_estimate_summary(metrics)),
        ("Created / settled auctions", format_created_settled(metrics)),
        ("WOOF holders", woof_holder_summary(metrics)),
    ]
    snapshot_rows = [(label, value) for label, value in snapshot_rows if value]

    dataset_rows = []
    for table, csv_path, rows in manifest_rows:
        table_name = str(table)
        csv_link = str(csv_path)
        json_link = str(Path(csv_link).with_suffix(".json"))
        dataset_rows.append((
            f"`{table_name}`",
            f"`{csv_link}`",
            rows,
            markdown_link("CSV", csv_link),
            markdown_link("JSON", json_link),
            DATASET_DESCRIPTIONS.get(table_name, "Generated table exported by the approved query layer."),
        ))

    contract_rows = [
        ("Auction house", metric_value(metrics, "auction_house")),
        ("Degen Dogs NFT", metric_value(metrics, "dog_nft")),
        ("WOOF token", metric_value(metrics, "woof_token")),
        ("SUP token", metric_value(metrics, "sup_token")),
    ]
    contract_rows = [(label, address) for label, address in contract_rows if address]

    configuration_rows = [(f"`{name}`", description) for name, description in CONFIGURATION_ENV_VARS]

    return render_readme_from_template({
        "{{LIVE_DASHBOARD_LINK}}": markdown_link(site_url, site_url),
        "{{CURRENT_SNAPSHOT_TABLE}}": markdown_table(["Field", "Value"], snapshot_rows).rstrip(),
        "{{PUBLISHED_DATASETS_TABLE}}": markdown_table(["Table", "Path", "Rows", "CSV", "JSON", "Description"], dataset_rows).rstrip(),
        "{{CONFIGURATION_TABLE}}": markdown_table(["Variable", "Purpose"], configuration_rows).rstrip(),
        "{{VERIFIED_CONTRACTS_TABLE}}": markdown_table(["Contract", "Address"], contract_rows).rstrip(),
    })


def parse_trait_item(item: str) -> tuple[str, str, str]:
    if ":" not in item:
        return "", item.strip(), ""
    trait_type, raw_value = item.split(":", 1)
    trait_type = trait_type.strip()
    trait_value = raw_value.strip()
    rarity = ""
    if trait_value.endswith(")") and " (" in trait_value:
        value_part, rarity_part = trait_value.rsplit(" (", 1)
        if rarity_part.endswith(")") and rarity_part[:-1].strip().endswith("%"):
            trait_value = value_part.strip()
            rarity = f"({rarity_part}"
    return trait_type, trait_value, rarity


def trait_chips(current: dict[str, str]) -> str:
    source = current.get("trait_rarity") or current.get("traits") or ""
    items = [item.strip() for item in source.split(";") if item.strip()]
    chips = []
    for item in items:
        trait_type, trait_value, rarity = parse_trait_item(item)
        if not trait_type or not trait_value:
            chips.append(f'<span class="trait-pill">{html.escape(item)}</span>')
            continue
        url = opensea_trait_url(trait_type, trait_value)
        label = f"View Degen Dogs with {trait_type}: {trait_value} on OpenSea"
        rarity_html = f'<span class="trait-rarity">{html.escape(rarity)}</span>' if rarity else ""
        chips.append(
            f'<a class="trait-pill trait-pill-link" href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="{html.escape(label, quote=True)}" '
            f'title="{html.escape(label, quote=True)}">'
            f'<span class="trait-type">{html.escape(trait_type)}</span>'
            f'<span class="trait-value">{html.escape(trait_value)}</span>'
            f'{rarity_html}'
            '</a>'
        )
    return "".join(chips)


def current_bid_history_dicts(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> list[dict[str, str]]:
    cols, rows = tables.get("current_auction_bid_history", ([], []))
    if not cols or not rows:
        return []
    history = [
        {col: text_value(row[idx] if idx < len(row) else "") for idx, col in enumerate(cols)}
        for row in rows
    ]
    history.sort(
        key=lambda row: (
            text_value(row.get("bid_time_utc")),
            int_value(row.get("block_number")),
            int_value(row.get("log_index")),
        ),
        reverse=True,
    )
    return history


def render_bid_history_menu(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> str:
    history = current_bid_history_dicts(tables)
    if not history:
        return ""
    count = len(history)
    count_label = f"{count} bid{'s' if count != 1 else ''}"
    items = []
    for index, row in enumerate(history):
        bidder = first_text(row.get("bidder"), short_address(row.get("bidder_wallet") or ""), "Unknown bidder")
        bidder_url = safe_dashboard_link(first_text(row.get("bidder_url"), basescan_address_url(row.get("bidder_wallet"))))
        bidder_html = html.escape(bidder)
        if bidder_url:
            bidder_html = f'<a href="{html.escape(bidder_url, quote=True)}" target="_blank" rel="noopener noreferrer">{bidder_html}</a>'
        tx_hash = text_value(row.get("tx_hash"))
        tx_url = basescan_tx_url(tx_hash)
        tx_html = ""
        if tx_url:
            tx_html = f'<a class="bid-history-tx" href="{html.escape(tx_url, quote=True)}" target="_blank" rel="noopener noreferrer">Tx</a>'
        wallet = text_value(row.get("bidder_wallet"))
        wallet_html = f'<code class="bid-history-wallet">{html.escape(wallet)}</code>' if wallet else ""
        bid = first_text(row.get("bid"), f"{row.get('bid_eth')} ETH" if row.get("bid_eth") else "")
        time = text_value(row.get("bid_time_utc"))
        rank = "High bid" if index == 0 else f"Bid {count - index}"
        items.append(
            '<li class="bid-history-row">'
            f'<span class="bid-history-rank">{html.escape(rank)}</span>'
            f'<span class="bid-history-main"><strong>{html.escape(bid)}</strong>{bidder_html}</span>'
            f'<span class="bid-history-meta"><time>{html.escape(time)}</time>{tx_html}</span>'
            f'{wallet_html}'
            '</li>'
        )
    return (
        '<details class="bid-history-menu">'
        f'<summary><span>Bid history</span><b>{html.escape(count_label)}</b></summary>'
        f'<ol class="bid-history-list">{"".join(items)}</ol>'
        '</details>'
    )


def public_png_data_uri(filename: str) -> str:
    path = ROOT / "public" / filename
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except FileNotFoundError:
        return filename
    return f"data:image/png;base64,{payload}"


def parse_timer_seconds(value: str) -> int | None:
    value = (value or "").strip().lower()
    if not value or value == "ended":
        return 0 if value == "ended" else None
    if value.isdigit():
        return int(value)
    day_count = 0
    if "d " in value:
        days, value = value.split("d ", 1)
        try:
            day_count = int(days.strip())
        except ValueError:
            return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    return day_count * 86400 + hours * 3600 + minutes * 60 + seconds


def timer_urgency_state(remaining_seconds: int | None, auction_status: str = "") -> str:
    status = (auction_status or "").lower()
    if "settled" in status or "ended" in status:
        return "ended"
    if remaining_seconds is None:
        return "calm"
    if remaining_seconds <= 0:
        return "ended"
    if remaining_seconds <= 600:
        return "critical"
    if remaining_seconds < 3600:
        return "urgent"
    return "calm"


def write_html(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
    metrics = metric_lookup(tables)
    current = current_lookup(tables)
    primary_parts = []
    for name in PRIMARY_TABLES:
        if name not in tables:
            continue
        cols, rows = tables[name]
        default_rows = rows[:10] if name == "auction_feed" else rows
        primary_parts.append(table_html(name, cols, default_rows, featured=True))
    site_url = metric_value(metrics, "site_url", "https://ael-dev3.github.io/Degen-Dogs-Mission-3/")
    top_links = [
        ("Bid live", "https://degendogs.club/auction?cache=1779901567562", "Open the Degen Dogs auction mini app to bid", "utility-chip--bid"),
        ("Farcaster", "https://farcaster.xyz/~/channel/degendogs", "Open the main Degen Dogs Farcaster channel", ""),
        ("Docs", "https://docs.degendogs.club/", "Open the Degen Dogs docs", ""),
        ("GitHub repo", "https://github.com/ael-dev3/Degen-Dogs-Mission-3", "Open the Degen Dogs Mission 3 GitHub repository", ""),
    ]
    top_link_html = "".join(
        f'<a class="utility-chip {extra}" href="{html.escape(url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" aria-label="{html.escape(label, quote=True)}">{html.escape(text)}</a>'
        for text, url, label, extra in top_links
    )
    mark_avatar_src = html.escape(public_png_data_uri("mark-profile.png"), quote=True)
    mark_credit_html = (
        '<div class="credit-menu">'
        '<button type="button" class="credit-trigger" aria-haspopup="true" aria-label="Project credit: Mark Carey, the creator of Degen Dogs">Degen Dogs by Mark Carey</button>'
        '<div class="credit-popover" aria-label="Mark Carey profile links">'
        '<div class="credit-head">'
        f'<img src="{mark_avatar_src}" alt="Pixel Degen Dog avatar for Mark Carey">'
        '<div><span>Mark Carey, the creator of Degen Dogs</span></div>'
        '</div>'
        '<a href="https://farcaster.xyz/markcarey" target="_blank" rel="noopener noreferrer">Farcaster</a>'
        '<a href="https://x.com/mthacks" target="_blank" rel="noopener noreferrer">X</a>'
        '<a href="https://github.com/markcarey" target="_blank" rel="noopener noreferrer">GitHub</a>'
        '</div></div>'
    )
    top_actions_html = f'<div class="top-actions">{top_link_html}{mark_credit_html}</div>'
    metric_cols, metric_rows = tables.get("mission3_metrics", (["metric", "value"], [("site_url", site_url)]))
    metric_head = "".join(f'<th scope="col">{html.escape(str(col))}</th>' for col in metric_cols)
    metric_body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in metric_rows
    )
    site_metric_html = (
        '<table data-table="mission3_metrics" hidden aria-hidden="true">'
        '<caption class="sr-only">mission3 metrics</caption>'
        f'<thead><tr>{metric_head}</tr></thead>'
        f'<tbody>{metric_body}</tbody>'
        '</table>'
    )

    dog = current.get("dog", f"Dog #{metrics.get('current_auction_token_id', '')}").strip() or "Current dog"
    current_dog_url = safe_dashboard_link(current.get("dog_opensea_url") or current.get("dog_external_url")) or "#"
    current_dog_label = f"Open {dog} on OpenSea" if current.get("dog_opensea_url") else f"Open {dog}"
    current_dog_html = html.escape(dog)
    if current_dog_url and current_dog_url != "#":
        current_dog_html = (
            f'<a class="current-dog-link" href="{html.escape(current_dog_url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="{html.escape(current_dog_label, quote=True)}" '
            f'title="{html.escape(current_dog_label, quote=True)}">{current_dog_html}</a>'
        )
    bid = current.get("bid") or current.get("latest_bid") or f"{metrics.get('current_bid_eth', '0')} ETH"
    participant = current.get("bidder_winner") or current.get("bidder") or metrics.get("current_bidder", "")
    participant_url = safe_dashboard_link(current.get("bidder_winner_url") or current.get("bidder_url", ""))
    participant_html = html.escape(participant)
    if participant_url and participant:
        participant_html = f'<a href="{html.escape(participant_url, quote=True)}" target="_blank" rel="noopener noreferrer">{participant_html}</a>'
    status = current.get("status") or current.get("auction_state", "")
    time_left = current.get("time_remaining", "")
    time_left_end = current.get("auction_end_utc") or current.get("end_time_utc", "")
    time_left_seconds = parse_timer_seconds(current.get("seconds_remaining", ""))
    if time_left_seconds is None:
        time_left_seconds = parse_timer_seconds(time_left)
    timer_state = timer_urgency_state(time_left_seconds, status)
    auction_status_attr = html.escape(status.lower(), quote=True)
    # Verification is established asynchronously from the signed/validated
    # refresh-status payload. Never render a green dot from auction state alone.
    live_dot_html = '<span class="dot dot--idle" data-live-dot aria-hidden="true"></span>'
    time_left_html = html.escape(time_left)
    if time_left and time_left_end:
        time_left_html = (
            f'<span class="countdown timer-value countdown--{timer_state}" '
            f'data-countdown-end="{html.escape(time_left_end, quote=True)}" '
            f'data-auction-status="{auction_status_attr}">{time_left_html}</span>'
        )
    image = safe_dashboard_image(current.get("dog_image_url", ""))
    image_html = ""
    if image:
        image_html = f'<img src="{html.escape(image, quote=True)}" alt="{html.escape(dog, quote=True)} image">'
    rarity = current.get("rarity", "")
    rarity_universe = str(metrics.get("dog_rarity_universe_count", "")).strip()
    rarity_excluded = str(metrics.get("dog_rarity_excluded_nonexistent_count", "")).strip()
    rarity_title = ""
    if rarity_universe.isdigit() and rarity_excluded.isdigit():
        rarity_title = (
            f' title="Ranked across {html.escape(rarity_universe, quote=True)} Base-existing Dogs; '
            f'{html.escape(rarity_excluded, quote=True)} canonically nonexistent Base IDs excluded"'
        )
    bid_history_menu = render_bid_history_menu(tables)
    current_detail = "".join(
        [
            f'<span class="detail-status"><b>Status</b>{html.escape(status)}</span>' if status else "",
            f'<span class="detail-bid"><b>Bid</b>{html.escape(bid)}</span>' if bid else "",
            (
                f'<span class="detail-time timer-card timer-card--{timer_state}" '
                f'data-auction-status="{auction_status_attr}"><b class="timer-label">Time left</b>{time_left_html}</span>'
            ) if time_left else "",
            f'<span class="detail-rarity"{rarity_title}><b>Base rarity</b>{html.escape(rarity)}</span>' if rarity else "",
            f'<span class="detail-bidder"><b>High bidder</b>{participant_html}</span>' if participant else "",
            bid_history_menu,
        ]
    )
    reward_strip = render_reward_strip(metrics)
    chips = trait_chips(current)
    css = """
:root{color-scheme:light;--paper:#e8ded5;--paper-calm:#f0fbea;--paper-warm:#fff7e6;--paper-urgent:#fff1f1;--ink:#0a0a0a;--panel:#fffaf3;--panel2:#f4ece3;--muted:#6d625b;--line:#cdbfb3;--calm:#61bf6b;--calm-dark:#1f6b3b;--warning:#d97706;--warning-dark:#92400e;--urgent:#e51b32;--urgent-dark:#9f1239;--critical-bg:#111111;--critical-red:#ef233c;--accent:#e51b2f;--accent2:#b91325;--shadow:0 10px 26px rgba(10,10,10,.1);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;min-width:320px;background:var(--paper);color:var(--ink);font-size:14px}
a{color:var(--ink);text-decoration:none;transition:color .16s ease,background .16s ease,border-color .16s ease,box-shadow .16s ease,transform .16s ease}
a:hover{color:var(--accent2)}
.shell{width:min(1520px,calc(100% - 16px));margin:0 auto;padding:12px 0 24px}
.current-card,.table-card{background:var(--panel);border:2px solid var(--ink);box-shadow:var(--shadow)}
.current-card{display:grid;grid-template-columns:minmax(360px,.9fr) minmax(260px,.42fr);gap:0;margin-bottom:10px;min-height:300px;overflow:hidden}
.current-copy{padding:18px;display:flex;flex-direction:column;gap:10px;border-right:2px solid var(--ink)}
.topline{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.eyebrow{display:flex;gap:8px;align-items:center;font-size:12px;font-weight:900;letter-spacing:.08em;text-transform:uppercase}
.dot{width:10px;height:10px;background:#8a8178;border:2px solid var(--ink);display:inline-block;box-shadow:none}
.dot--live{background:var(--calm);animation:liveDotPulse 1.7s ease-in-out infinite;box-shadow:0 0 0 0 rgba(85,166,83,.42)}
.dot--idle{background:#8a8178}
.top-actions{display:flex;align-items:center;justify-content:flex-end;gap:7px;flex-wrap:wrap;max-width:min(100%,760px)}
.utility-chip,.credit-trigger{appearance:none;font-family:inherit;display:inline-flex;align-items:center;gap:7px;width:max-content;max-width:100%;border:2px solid var(--ink);background:var(--ink);color:white;padding:6px 10px;font-size:12px;font-weight:950;letter-spacing:.08em;text-transform:uppercase;line-height:1;box-shadow:3px 3px 0 var(--accent2);white-space:nowrap}
.utility-chip::after{content:'↗';color:#ffccd2;font-size:.85em;line-height:1}
.utility-chip:hover,.credit-trigger:hover,.credit-menu:focus-within .credit-trigger{background:white;color:var(--accent2);border-color:var(--accent2);transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--accent2)}
.utility-chip:hover::after{color:var(--accent2)}
.utility-chip--bid{background:var(--calm-dark);box-shadow:3px 3px 0 var(--calm)}
.utility-chip--bid:hover{border-color:var(--calm-dark);color:var(--calm-dark);box-shadow:4px 4px 0 var(--calm)}
.credit-menu{position:relative;display:inline-flex;padding-bottom:8px;margin-bottom:-8px}
.credit-trigger{cursor:pointer;background:#fff;color:var(--ink);box-shadow:3px 3px 0 var(--ink)}
.credit-trigger:focus{outline:2px solid var(--accent2);outline-offset:2px}
.credit-popover{position:absolute;right:0;top:100%;z-index:30;display:grid;gap:6px;min-width:252px;border:2px solid var(--ink);background:var(--panel);box-shadow:5px 5px 0 var(--ink);padding:10px;opacity:0;visibility:hidden;pointer-events:none;transform:translateY(-4px);transition:opacity .14s ease,transform .14s ease,visibility .14s ease}
.credit-head{display:grid;grid-template-columns:44px minmax(0,1fr);gap:8px;align-items:center;border-bottom:1.5px solid var(--line);padding-bottom:7px;margin-bottom:2px}
.credit-head img{width:44px;height:44px;object-fit:cover;image-rendering:pixelated;border:2px solid var(--ink);background:#fff;box-shadow:2px 2px 0 var(--ink)}
.credit-menu:hover .credit-popover,.credit-menu:focus-within .credit-popover{opacity:1;visibility:visible;pointer-events:auto;transform:translateY(0)}
.credit-popover span{font-size:12px;font-weight:850;line-height:1.2;color:var(--ink)}
.credit-popover a{display:flex;align-items:center;justify-content:space-between;border:1.5px solid var(--ink);background:var(--panel2);padding:5px 7px;font-size:12px;font-weight:950;line-height:1;text-transform:uppercase;letter-spacing:.06em;box-shadow:2px 2px 0 var(--ink)}
.credit-popover a::after{content:'↗';color:var(--accent2);font-size:.78em}
.credit-popover a:hover{background:#fff;border-color:var(--accent2);box-shadow:3px 3px 0 var(--accent2)}
.current-copy h1{font-size:clamp(34px,6vw,72px);line-height:.9;margin:0;letter-spacing:-.075em;max-width:10ch}
.current-dog-link{display:inline-flex;align-items:flex-start;gap:.04em;color:inherit;max-width:100%}
.current-dog-link::after{content:'↗';font-size:.28em;line-height:1;color:var(--accent2);letter-spacing:0;margin-left:.04em;transform:translateY(.14em);transition:transform .16s ease,color .16s ease}
.current-dog-link:hover{color:var(--accent2)}
.current-dog-link:hover::after{transform:translate(.05em,.06em)}
.subtitle{margin:0;color:var(--muted);font-weight:700}
.current-detail{display:flex;flex-wrap:wrap;align-items:stretch;gap:7px;margin-top:auto}
.reward-strip{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px;margin-top:2px}
.reward-tile{display:flex;min-width:0;flex-direction:column;gap:2px;border:1.5px solid var(--ink);background:#eff8df;padding:7px 8px;font-weight:900;line-height:1.12;box-shadow:2px 2px 0 rgba(36,84,23,.18)}
.reward-tile b{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:#31551f}
.reward-tile strong{font-size:clamp(13px,1.25vw,18px);font-weight:950;letter-spacing:-.025em;overflow-wrap:anywhere}
.reward-tile strong span{display:block}
.reward-tile em{font-style:normal;color:#5d6b48;font-size:10.5px;font-weight:800}
.reward-strip p{grid-column:1/-1;margin:0;color:var(--muted);font-size:11px;font-weight:800}
.season6-sup-estimate{background:#f3fae8;color:#1b3f24;box-shadow:2px 2px 0 rgba(31,107,59,.18)}
.season6-sup-estimate b{color:#1f6b3b}
.season6-sup-estimate em{color:#416b3f}
.current-detail > span{display:flex;min-height:48px;flex:0 1 auto;width:max-content;max-width:100%;flex-direction:column;justify-content:center;align-items:flex-start;border:1.5px solid var(--ink);background:var(--panel2);padding:7px 9px;font-weight:900;line-height:1.18}
.current-detail .detail-status{min-width:96px}
.current-detail .detail-bid{min-width:142px}
.current-detail .detail-rarity{min-width:104px}
.current-detail .detail-bidder{min-width:0}
.bid-history-menu{position:relative;align-self:stretch;flex:0 1 158px;min-width:150px;max-width:100%;margin-inline:0;font-weight:900;line-height:1.12}
.bid-history-menu summary{list-style:none;cursor:pointer;position:relative;display:flex;min-height:48px;height:100%;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:2px;border:1.5px solid var(--ink);background:#f3fae8;padding:7px 22px 7px 9px;box-shadow:2px 2px 0 rgba(31,107,59,.18)}
.bid-history-menu summary::-webkit-details-marker{display:none}
.bid-history-menu summary::after{content:'⌄';position:absolute;right:9px;top:50%;transform:translateY(-50%);font-size:14px;color:var(--calm-dark);transition:transform .16s ease}
.bid-history-menu[open] summary::after{transform:translateY(-50%) rotate(180deg)}
.bid-history-menu summary span{font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--calm-dark);font-weight:950}
.bid-history-menu summary b{font-size:13px;color:var(--ink);white-space:nowrap;letter-spacing:-.01em}
.bid-history-list{position:absolute;left:50%;top:calc(100% + 3px);z-index:24;transform:translateX(-50%);width:min(340px,calc(100vw - 24px));max-height:260px;overflow:auto;display:grid;gap:6px;margin:0;padding:8px;list-style:none;border:2px solid var(--ink);background:var(--panel);box-shadow:5px 5px 0 var(--ink);text-align:left}
.bid-history-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:3px 8px;border:1.5px solid var(--line);background:#fffdf6;padding:7px}
.bid-history-rank{grid-row:1/3;border:1.5px solid var(--ink);background:var(--paper-calm);padding:4px 5px;font-size:9px;font-weight:950;text-transform:uppercase;letter-spacing:.06em;color:var(--calm-dark);align-self:start;white-space:nowrap}
.bid-history-main{display:flex;align-items:baseline;gap:6px;min-width:0;flex-wrap:wrap}.bid-history-main strong{font-size:13px}.bid-history-main a{font-size:12px}
.bid-history-meta{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px;font-weight:850}.bid-history-tx{border:1.5px solid var(--ink);background:var(--panel2);padding:2px 6px;font-size:10px;font-weight:950;text-transform:uppercase;box-shadow:1.5px 1.5px 0 var(--ink)}
.bid-history-wallet{grid-column:1/-1;max-width:100%;overflow:hidden;text-overflow:ellipsis;border-top:1px solid var(--line);padding-top:4px;color:var(--muted);font-size:10px;background:transparent}
.current-detail .timer-card{min-width:180px;position:relative;overflow:hidden;transition:background .18s ease,color .18s ease,border-color .18s ease,box-shadow .18s ease}
.current-detail .timer-card--calm,.current-detail .timer-card--normal{background:var(--paper-calm);color:var(--ink);border-color:#a7dfa0;box-shadow:3px 3px 0 rgba(65,155,79,.16)}
.current-detail .timer-card--urgent{background:var(--paper-urgent);color:var(--ink);border-color:var(--urgent);box-shadow:3px 3px 0 rgba(229,27,50,.18)}
.current-detail .timer-card--critical{background:var(--critical-bg);color:white;border-color:var(--critical-red);box-shadow:3px 3px 0 var(--critical-red)}
.current-detail .timer-card--ended{background:#eee7dd;color:#4a403a;border-color:#8a8178;box-shadow:none}
.current-detail b,.time-cell b{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.current-detail .timer-label{display:flex;align-items:center;gap:5px}
.current-detail .timer-card--calm .timer-label,.current-detail .timer-card--normal .timer-label{color:var(--calm-dark)}
.current-detail .timer-card--urgent .timer-label{color:var(--urgent-dark)}
.current-detail .timer-card--critical .timer-label{color:#ffb3bd}
.current-detail .timer-card--ended .timer-label{color:#4a403a}
.current-detail .timer-card--urgent .timer-label::before,.current-detail .timer-card--critical .timer-label::before{content:'';width:6px;height:6px;border-radius:999px;background:currentColor;box-shadow:0 0 0 1px rgba(10,10,10,.12)}
.current-detail .timer-card--critical .timer-label::before{animation:timerPulse 1.8s ease-in-out infinite}
.current-detail .timer-value{display:block;margin-top:4px;border:0;background:transparent;padding:0;min-height:0;font-family:"Arial Black",Impact,ui-sans-serif,system-ui,sans-serif;font-size:clamp(21px,2.3vw,30px);font-weight:950;line-height:.96;letter-spacing:-.015em;font-variant-numeric:tabular-nums;transform:skewX(-4deg);transform-origin:left center;text-shadow:none;color:inherit}
.current-detail .timer-card--calm .timer-value,.current-detail .timer-card--normal .timer-value{color:var(--calm-dark);text-shadow:none}
.current-detail .timer-card--urgent .timer-value{color:var(--urgent);text-shadow:none}
.current-detail .timer-card--critical .timer-value{color:white;text-shadow:2px 2px 0 var(--critical-red),0 0 12px rgba(239,35,60,.45)}
.current-detail .timer-card--ended .timer-value{color:#4a403a;text-shadow:none}
@keyframes timerPulse{0%,100%{opacity:.55;transform:scale(.92)}50%{opacity:1;transform:scale(1.08)}}
@keyframes liveDotPulse{0%,100%{transform:scale(.94);box-shadow:0 0 0 0 rgba(85,166,83,.4)}50%{transform:scale(1.08);box-shadow:0 0 0 5px rgba(85,166,83,0)}}
.current-detail a,.identity a,td.time a{display:inline-flex;align-items:center;position:relative;width:max-content;max-width:100%;border:1.5px solid var(--ink);border-radius:999px;background:var(--panel2);padding:3px calc(8px + 1.05em) 3px 8px;font-weight:900;line-height:1.1;box-shadow:2px 2px 0 var(--ink)}
.current-detail a::after,.identity a::after,td.time a::after{content:'↗';position:absolute;inset-inline-end:7px;top:50%;transform:translateY(-50%);display:grid;place-items:center;width:.95em;height:.95em;font-size:.74em;line-height:1;color:var(--accent2);pointer-events:none}
.current-detail a:hover,.identity a:hover,td.time a:hover{background:#fff;border-color:var(--accent2);transform:translate(-1px,-1px);box-shadow:3px 3px 0 var(--accent2)}
.traits{display:flex;flex-wrap:wrap;gap:5px;max-height:78px;overflow:auto;padding-right:2px}
.traits .trait-pill{display:inline-flex;align-items:baseline;gap:3px;border:1.5px solid var(--ink);background:var(--panel);color:inherit;padding:4px 6px;font-size:11px;font-weight:800;line-height:1.15}
.trait-pill-link{cursor:pointer;text-decoration:none;transition:transform .16s ease,background .16s ease,border-color .16s ease,box-shadow .16s ease}
.trait-pill-link:hover{background:#fff;border-color:var(--accent2);transform:translateY(-1px);box-shadow:2px 2px 0 rgba(185,19,37,.18)}
.trait-pill-link:focus-visible,.dog-image-link:focus-visible{outline:3px solid currentColor;outline-offset:3px}
.trait-type{font-weight:950;color:var(--muted);text-transform:uppercase;font-size:.84em;letter-spacing:.04em}.trait-type::after{content:':'}.trait-value{color:var(--ink)}.trait-rarity{color:var(--muted);font-weight:850}
.dog-stage{display:flex;align-items:center;justify-content:center;background:var(--panel2);min-height:280px;padding:10px;overflow:hidden}
.dog-stage img{width:min(100%,330px);height:min(100%,330px);object-fit:contain;filter:drop-shadow(0 10px 18px rgba(0,0,0,.16))}
.toolbar{display:grid;grid-template-columns:minmax(260px,1fr) auto auto auto;align-items:end;gap:8px;margin:0 0 10px}
.toolbar-field,.toolbar-group{min-width:0}
.toolbar-field{display:flex;flex-direction:column;gap:3px}
.toolbar-group{display:flex;align-items:end;gap:6px;flex-wrap:wrap}
.toolbar label,.toolbar-legend{font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.toolbar input,.toolbar select{border:2px solid var(--ink);background:var(--panel);color:var(--ink);padding:8px 10px;font:inherit;font-size:12px;font-weight:850;outline:none;box-shadow:3px 3px 0 var(--ink)}
.toolbar input{width:100%}
.toolbar select{min-height:36px;cursor:pointer}
.toolbar input:focus,.toolbar select:focus{border-color:var(--accent2);box-shadow:3px 3px 0 var(--accent2)}
.mission-group{align-items:flex-end}
.mission-toggle{display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap}
.mission-toggle button,.page-btn{appearance:none;border:2px solid var(--ink);background:var(--panel2);color:var(--ink);padding:8px 9px;font:inherit;font-size:11px;font-weight:950;line-height:1;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;box-shadow:2px 2px 0 var(--ink)}
.mission-toggle button[aria-pressed="true"]{background:var(--ink);color:#fff;box-shadow:2px 2px 0 var(--accent2)}
.mission-toggle button:focus-visible,.page-btn:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
.page-btn:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
.pagination{justify-content:flex-end}
.archive-status{font-size:11px;color:var(--muted);font-weight:850;white-space:nowrap;line-height:1.15}
.archive-caveat{font-size:10.5px;color:var(--muted);font-weight:800;line-height:1.15}
.archive-caveat:empty{display:none}
.primary-grid{display:grid;gap:10px}
.table-card{overflow:hidden}
.table-scroll{width:100%;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.24;background:var(--panel)}
.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
caption.table-caption:not(.sr-only){caption-side:top;padding:8px 10px;border-bottom:2px solid var(--ink);font-weight:950;text-align:left;text-transform:uppercase;letter-spacing:.07em;display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--panel2);font-size:12px}
.table-caption [data-total]{color:var(--muted);font-size:11px;white-space:nowrap}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
td{text-align:left}
th{position:relative;background:#efe3d7;color:var(--muted);font-size:10.5px;text-align:center;text-transform:uppercase;letter-spacing:.08em;font-weight:950}
th button{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;justify-content:center;width:100%;min-height:22px;position:relative;text-align:center;line-height:1.05;white-space:normal;padding:0 14px}
th button::after{content:'↕';font-size:.78em;color:var(--muted);position:absolute;right:0;top:50%;transform:translateY(-50%)}
th[aria-sort='ascending'] button::after{content:'↑';color:var(--accent2)}
th[aria-sort='descending'] button::after{content:'↓';color:var(--accent2)}
tbody tr{transition:background .12s ease}
tbody tr:hover{background:#fff2e7}
tbody tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.time{font-variant-numeric:tabular-nums;color:#2a2725}
.time-cell{display:flex;flex-direction:column;gap:2px;line-height:1.12}
.time-cell b{margin:0;color:var(--accent2)}
@media (min-width:641px){.featured-table td{text-align:center}.featured-table td.num{text-align:center}.featured-table .identity{max-width:none;text-align:center}.featured-table .status-pill,.featured-table .dog-link,.featured-table .identity a{display:flex;width:max-content;margin-inline:auto}.featured-table .dog-cell{justify-content:center}.featured-table .time-cell{align-items:center;text-align:center}}
.status-pill{display:inline-flex;align-items:center;border:1.5px solid var(--ink);padding:3px 7px;font-size:11px;font-weight:950;text-transform:uppercase;letter-spacing:.06em;background:var(--panel2)}
.status-pill.ongoing{background:var(--accent);color:white}
.status-pill.settled{background:#efe3d7;color:var(--ink)}
.dog-link{display:inline-flex;color:var(--ink)}
.dog-link:hover{color:var(--accent2)}
.dog-cell{display:flex;align-items:center;gap:7px;font-weight:950}
.dog-image-link{display:inline-flex;flex:none;border-radius:3px;color:inherit}
.dog-image-link:hover .dog-thumb{transform:translateY(-1px);box-shadow:0 3px 0 rgba(10,10,10,.16)}
.dog-thumb{width:38px;height:38px;border:1.5px solid var(--ink);background:var(--panel2);object-fit:cover;flex:none;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}
.dog-col{min-width:132px}
.identity{max-width:180px;overflow:hidden;text-overflow:ellipsis}
.identity a{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media (prefers-reduced-motion:reduce){.timer-card,.timer-card *,.dot--live{animation:none!important;transition:none!important}}
@media (max-width:1100px){.reward-strip{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:640px){.reward-strip{grid-template-columns:1fr;gap:5px}.reward-tile{padding:6px 7px}.reward-tile strong{font-size:13px}.reward-strip p{font-size:10px}}
@media (max-width:900px){.shell{width:min(100% - 10px,760px);padding:8px 0 18px}.current-card{grid-template-columns:1fr;min-height:0}.current-copy{border-right:0;border-bottom:2px solid var(--ink);padding:14px}.dog-stage{min-height:220px}.dog-stage img{max-height:240px}.toolbar{grid-template-columns:1fr;align-items:stretch}.toolbar input{width:100%}.toolbar-group{align-items:flex-start}.pagination{justify-content:flex-start}.current-copy h1{font-size:clamp(34px,13vw,58px)}th,td{padding:6px 7px}table{font-size:12.5px}.traits{max-height:70px}}
@media (max-width:640px){.bid-history-menu{flex:0 1 150px;min-width:136px}.bid-history-list{width:min(340px,calc(100vw - 20px));box-shadow:3px 3px 0 var(--ink)}}
@media (max-width:640px){body{font-size:13px}.shell{width:calc(100% - 8px);padding:4px 0 14px}.current-card,.table-card{border-width:1.5px;box-shadow:0 6px 16px rgba(10,10,10,.1)}.current-card{margin-bottom:8px}.current-copy{padding:12px;gap:8px;border-bottom:1.5px solid var(--ink)}.eyebrow{font-size:11px;gap:6px}.dot{width:8px;height:8px}.current-copy h1{font-size:clamp(42px,17vw,62px);max-width:none;line-height:.88}.subtitle{font-size:12px}.current-detail{gap:6px}.current-detail > span{min-width:0;min-height:42px;padding:6px 7px;font-size:12.5px;overflow-wrap:anywhere}.current-detail .timer-card{flex:1 1 100%;width:100%;max-width:100%;min-width:0}.current-detail .detail-rarity,.current-detail .detail-status{min-width:84px}.current-detail .countdown{font-size:clamp(22px,9vw,36px)}.current-detail b,.time-cell b{font-size:9px}.current-detail a,.identity a,td.time a{max-width:100%;font-size:12px;box-shadow:1.5px 1.5px 0 var(--ink)}.traits{display:grid;grid-template-columns:1fr;gap:4px;max-height:none;overflow:visible}.traits .trait-pill{padding:3px 5px;font-size:9.5px;line-height:1.12;white-space:normal;overflow-wrap:anywhere}.dog-stage{min-height:166px;padding:4px}.dog-stage img{width:min(58vw,204px);height:min(58vw,204px)}.toolbar{margin:8px 0;gap:6px}.toolbar input,.toolbar select{padding:8px 10px;font-size:13px;box-shadow:2px 2px 0 var(--ink)}.mission-toggle button,.page-btn{padding:7px 8px;border-width:1.5px;box-shadow:1.5px 1.5px 0 var(--ink)}.archive-status,.archive-caveat{width:100%;white-space:normal}table{font-size:12px}.featured-table .table-scroll{overflow:visible}.featured-table table{display:block;background:transparent}.featured-table caption.table-caption:not(.sr-only){display:flex;padding:7px 8px;border-bottom:1.5px solid var(--ink)}.featured-table thead{display:none}.featured-table tbody{display:grid;gap:7px;padding:7px;background:var(--panel2)}.featured-table tr{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 8px;align-items:center;border:1.5px solid var(--ink);background:var(--panel);padding:7px;box-shadow:2px 2px 0 rgba(10,10,10,.18)}.featured-table tr:hover{background:var(--panel)}.featured-table td{display:block;min-width:0;border:0;padding:0;white-space:normal}.featured-table td::before{content:attr(data-label);display:block;margin-bottom:2px;color:var(--muted);font-size:8.5px;font-weight:950;letter-spacing:.08em;text-transform:uppercase}.featured-table td.state{align-self:start}.featured-table td.state::before{display:none}.featured-table td.dog-col{grid-column:2;grid-row:1/span 2}.featured-table td.identity{grid-column:1/-1;max-width:none}.featured-table td.num{grid-column:1/-1;text-align:left;font-size:13px;font-weight:950}.featured-table td.time{grid-column:1/-1}.featured-table td:not(.state):not(.dog-col):not(.identity):not(.num):not(.time){grid-column:1/-1}.dog-cell{gap:6px}.dog-thumb{width:34px;height:34px}.time-cell{gap:1px}.status-pill{padding:3px 6px;font-size:9px}}
@media (max-width:420px){.traits{grid-template-columns:1fr}.dog-stage img{width:min(54vw,196px);height:min(54vw,196px)}}
@media (max-width:380px){.current-detail{display:grid;grid-template-columns:1fr}.current-detail > span,.bid-history-menu{width:100%;max-width:100%}.current-copy h1{font-size:clamp(38px,16vw,54px)}}
@media (max-width:900px){.topline{align-items:flex-start}.top-actions{justify-content:flex-start;max-width:100%}}
@media (max-width:640px){.top-actions{display:grid;grid-template-columns:repeat(2,max-content);flex:1 1 100%;width:100%;justify-content:flex-start;align-items:flex-start;gap:6px}.utility-chip,.credit-trigger{font-size:10px;padding:5px 7px;border-width:1.5px;box-shadow:2px 2px 0 var(--accent2)}.utility-chip--bid{box-shadow:2px 2px 0 var(--calm)}.credit-menu{grid-column:1/-1;margin-left:0;max-width:100%}.credit-trigger{box-shadow:2px 2px 0 var(--ink);white-space:normal;text-align:left}.credit-popover{left:0;right:auto;min-width:min(280px,calc(100vw - 24px));max-width:calc(100vw - 24px)}}

""".strip()
    script = r"""
const filter=document.getElementById('filter');
const missionButtons=[...document.querySelectorAll('[data-mission-filter]')];
const sortSelect=document.getElementById('auction-sort');
const pageSizeSelect=document.getElementById('auction-page-size');
const pagePrev=document.getElementById('auction-prev');
const pageNext=document.getElementById('auction-next');
const pageLabel=document.getElementById('auction-page-label');
const showingLabel=document.getElementById('auction-showing');
const archiveCaveat=document.getElementById('auction-caveat');
const auctionTable=document.querySelector('table[data-table="auction_feed"]');
const auctionBody=auctionTable?.tBodies?.[0];
const auctionTotal=auctionTable?.caption?.querySelector('[data-total]');
const currentDogHeading=document.querySelector('[data-current-dog]');
const currentDetail=document.querySelector('[data-current-detail]');
const currentRewards=document.querySelector('[data-current-rewards]');
const currentTraits=document.querySelector('[data-current-traits]');
const currentDogStage=document.querySelector('[data-current-dog-stage]');
const liveLabel=document.querySelector('[data-live-label]');
const defaultRows=auctionBody?[...auctionBody.rows].map(row=>row.cloneNode(true)):[];
const archiveState={query:'',mission:'all',sortMode:'newest',pageSize:10,currentPage:1};
let unifiedRecords=[];
let unifiedReady=false;
let unifiedSnapshotBlock='';
let liveSnapshotKey='';
let liveSnapshotBlock='';
let liveSnapshotContext=null;
let liveRefreshPromise=null;
let archiveSnapshotKey='';
let archiveRefreshPromise=null;
let pendingArchiveContext=null;
const LIVE_REFRESH_MS=10000;
const LIVE_STALE_MS=90*60*1000;
const CURRENT_FETCH_TIMEOUT_MS=6000;
const ARCHIVE_FETCH_TIMEOUT_MS=45000;
const key=v=>{const s=v.trim().replaceAll(',','').replace(/[()$]/g,'');const n=Number(s.split(' ')[0]);return s!==''&&Number.isFinite(n)?n:v.trim().toLowerCase();};
const parseUtc=value=>{const raw=String(value||'').trim();if(!raw)return NaN;const iso=raw.includes('T')?raw:raw.replace(' ','T');return Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso)?iso:`${iso}Z`);};
const formatDuration=seconds=>{const s=Math.max(0,Math.floor(seconds));const d=Math.floor(s/86400);const h=Math.floor((s%86400)/3600);const m=Math.floor((s%3600)/60);const sec=s%60;const clock=`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;return d>0?`${d}d ${clock}`:clock;};
const TIMER_STATES=['calm','normal','urgent','critical','ended'];
const timerState=(seconds,forceEnded=false)=>forceEnded||seconds<=0?'ended':seconds<=600?'critical':seconds<3600?'urgent':'calm';
const applyTimerState=(el,state)=>{TIMER_STATES.forEach(name=>el.classList.toggle(`countdown--${name}`,name===state));const box=el.closest('.timer-card');if(box){TIMER_STATES.forEach(name=>box.classList.toggle(`timer-card--${name}`,name===state));}};
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const attr=value=>escapeHtml(value);
const shortAddress=value=>{const s=String(value||'');return s.startsWith('0x')&&s.length>=12?`${s.slice(0,6)}…${s.slice(-4)}`:s;};
const toNumber=value=>{if(value===null||value===undefined)return null;const text=String(value).replace(/[$,]/g,'').trim();if(!text)return null;const n=Number.parseFloat(text);return Number.isFinite(n)?n:null;};
const firstNumeric=values=>{for(const value of values){const n=toNumber(value);if(n!==null)return n;}return null;};
const usdCandidates=record=>{const amount=record.amount||{};return [amount.usd_estimate,amount.amount_usd_at_event,amount.estimated_usd_value,amount.usd_estimate_display,record.amount_usd_estimate,record.amount_usd,record.final_bid_usd_estimate,record.high_bid_usd_estimate,record.usd_at_time,record.usd_value,record.estimated_usd];};
const getUsdSortValue=record=>firstNumeric(usdCandidates(record));
const formatUsd=value=>{const n=toNumber(value);return n===null?'':`$${n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`;};
const usdDisplay=record=>{const amount=record.amount||{};return amount.usd_estimate_display||formatUsd(getUsdSortValue(record));};
const archiveCurrentRank=record=>{const status=String(record.status||'').toLowerCase();return status==='live'||status.includes('ongoing')?1:0;};
const compareNewest=(a,b)=>{const live=archiveCurrentRank(b)-archiveCurrentRank(a);if(live)return live;const at=Date.parse(a.activity_time_utc||'')||0;const bt=Date.parse(b.activity_time_utc||'')||0;if(bt!==at)return bt-at;return Number(b.dog_id||0)-Number(a.dog_id||0);};
const exactDogQuery=q=>{const dog=q.match(/(?:^|\s)dog\s*#?\s*(\d{1,4})(?=\s|$)/);if(dog)return Number(dog[1]);const bare=q.match(/^#?(\d{1,4})$/);return bare?Number(bare[1]):null;};
const SAFE_LINK_HOSTS=new Set(['basescan.org','degendogs.club','explorer.degen.tips','farcaster.xyz','opensea.io','polygonscan.com']);
const SAFE_IMAGE_HOSTS=new Set(['api.degendogs.club','degendogs.club','ipfs.io']);
const safeUrl=(value,hosts=SAFE_LINK_HOSTS)=>{try{const raw=String(value||'').trim();if(!raw||/[\u0000-\u0020\u007f]/.test(raw))return '';const url=new URL(raw,document.baseURI);const host=url.hostname.toLowerCase().replace(/\.$/,'');return url.protocol==='https:'&&url.port===''&&!url.username&&!url.password&&hosts.has(host)?url.href:'';}catch(_){return '';}};
const setVerificationState=(status,retrying=false)=>{const verifiedAt=parseUtc(status?.last_successful_refresh_time_utc);const blockAt=parseUtc(status?.latest_generated_block_time_utc);const now=Date.now();const refreshAge=Number.isFinite(verifiedAt)?now-verifiedAt:Infinity;const blockAge=Number.isFinite(blockAt)?now-blockAt:Infinity;const verified=!retrying&&status?.last_refresh_result==='success_generated'&&refreshAge>=0&&refreshAge<=LIVE_STALE_MS&&blockAge>=-60000&&blockAge<=LIVE_STALE_MS;const dot=document.querySelector('[data-live-dot]');if(dot){dot.classList.toggle('dot--live',verified);dot.classList.toggle('dot--idle',!verified);dot.title=retrying?'Snapshot update retrying':verified?'Onchain snapshot cross-checked':'Snapshot verification is stale';}if(liveLabel){if(!retrying)delete liveLabel.dataset.liveError;const stamp=Number.isFinite(verifiedAt)?new Date(verifiedAt).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'unavailable';liveLabel.textContent=retrying?`Mission 3 auction feed · update retrying · last verified ${stamp}`:verified?`Mission 3 auction feed · verified ${stamp}`:'Mission 3 auction feed · verification stale';}return verified;};
const rowSearchText=record=>{const amount=record.amount||{};const who=record.winner_or_high_bidder||{};const created=record.auction_created||{};const settled=record.settlement||{};const bidHashes=Array.isArray(record.bid_tx_hashes)?record.bid_tx_hashes:[];return [record.search_text,`dog #${record.dog_id}`,`dog ${record.dog_id}`,record.dog_id,`mission ${record.mission}`,record.era_label,record.chain,record.chain_id,record.status,who.wallet,who.display,who.farcaster_handle,who.farcaster_fid,amount.native,amount.native_symbol,amount.usd_estimate,amount.amount_usd_at_event,amount.usd_estimate_display,usdDisplay(record),amount.usd_estimate_price_date_utc,amount.usd_estimate_source,created.tx_hash,settled.tx_hash,...bidHashes].filter(Boolean).join(' ').toLowerCase();};
const statusCell=status=>{const text=String(status||'unknown');const lower=text.toLowerCase();const tone=lower.includes('ongoing')||lower==='live'?'ongoing':(lower.includes('settled')?'settled':'neutral');return `<span class="status-pill ${tone}">${escapeHtml(text)}</span>`;};
const dogCell=record=>{const dog=`Dog #${record.dog_id}`;const image=safeUrl(record.dog_image_url,SAFE_IMAGE_HOSTS);const img=image?`<img class="dog-thumb" src="${attr(image)}" alt="${attr(dog)} image" loading="lazy">`:'';const links=record.links||{};const item=safeUrl(record.dog_item_url||links.item||links.dog_page);const imgHtml=img&&item?`<a class="dog-image-link" href="${attr(item)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${attr(dog)}" title="Open ${attr(dog)}">${img}</a>`:img;const label=item?`<a class="dog-link" href="${attr(item)}" target="_blank" rel="noopener noreferrer">${escapeHtml(dog)}</a>`:`<span>${escapeHtml(dog)}</span>`;return `<span class="dog-cell">${imgHtml}${label}</span>`;};
const identityCell=record=>{const who=record.winner_or_high_bidder||{};const label=who.display||shortAddress(who.wallet)||'';if(!label)return '';const url=safeUrl(who.profile_url||who.wallet_explorer_url||record.links?.explorer);return url?`<a href="${attr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`:escapeHtml(label);};
const formatNativeAmount=value=>{const text=String(value??'').trim();if(!/^[-+]?\d+(?:\.\d+)?$/.test(text))return text;const [whole,fraction='']=text.split('.');const trimmed=fraction.replace(/0+$/,'');return trimmed?`${whole}.${trimmed}`:whole;};
const bidCell=record=>{const amount=record.amount||{};if(!amount.native)return archiveState.sortMode==='highest_usd'?'USD estimate unavailable':'';const native=`${formatNativeAmount(amount.native)} ${amount.native_symbol||''}`.trim();const display=usdDisplay(record);const usd=display?` (${display} est.)`:(archiveState.sortMode==='highest_usd'?' (USD estimate unavailable)':'');return escapeHtml(`${native}${usd}`);};
const timeCell=record=>{const status=String(record.status||'').toLowerCase();const label=status.includes('settled')?'Settled':(record.activity_time_basis==='last_bid_block_time'?'Last bid':'Activity');const value=record.activity_time_utc||'';return value?`<span class="time-cell"><b>${label}</b>${escapeHtml(value.replace('T',' ').replace('Z',''))}</span>`:'';};
const rarityCell=record=>escapeHtml(record.rarity?.display||'');
const unifiedRowHtml=record=>{const statusLabel=`${record.era_label||`Mission ${record.mission}`} · ${record.status||''}`;return `<tr data-search="${attr(rowSearchText(record))}"><td class="state" data-label="status">${statusCell(statusLabel)}</td><td class="dog-col" data-label="dog">${dogCell(record)}</td><td class="identity" data-label="high bidder / winner">${identityCell(record)}</td><td class="" data-label="bid">${bidCell(record)}</td><td class="time" data-label="last bid / settled">${timeCell(record)}</td><td class="num" data-label="rarity">${rarityCell(record)}</td></tr>`;};
const isDefaultArchiveState=()=>archiveState.query===''&&archiveState.mission==='all'&&archiveState.sortMode==='newest'&&archiveState.pageSize===10&&archiveState.currentPage===1;
const syncControls=()=>{if(filter&&filter.value!==archiveState.query)filter.value=archiveState.query;missionButtons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.missionFilter===archiveState.mission)));if(sortSelect)sortSelect.value=archiveState.sortMode;if(pageSizeSelect)pageSizeSelect.value=String(archiveState.pageSize);};
const generatedUrls=(name,version)=>{const url=new URL(`generated/${name}.json`,document.baseURI);url.searchParams.set('v',String(version||'latest'));url.searchParams.set('watch',String(Date.now()));return [url.href];};
const fetchGenerated=async(name,version)=>{let lastError;const timeoutMs=name==='unified_dog_search_index'?ARCHIVE_FETCH_TIMEOUT_MS:CURRENT_FETCH_TIMEOUT_MS;for(const url of generatedUrls(name,version)){const controller=new AbortController();const timeout=window.setTimeout(()=>controller.abort(),timeoutMs);try{const response=await fetch(url,{cache:'no-store',headers:{accept:'application/json'},signal:controller.signal});if(!response.ok)throw new Error(`${name} unavailable (${response.status})`);const type=(response.headers.get('content-type')||'').split(';',1)[0].trim().toLowerCase();if(type&&type!=='application/json'&&!type.endsWith('+json'))throw new Error(`${name} returned ${type}`);return await response.json();}catch(error){lastError=error;}finally{window.clearTimeout(timeout);}}throw lastError||new Error(`${name} unavailable`);};
const asRows=value=>Array.isArray(value)?value.filter(row=>row&&typeof row==='object'):[];
const metricRowsToMap=rows=>Object.fromEntries(asRows(rows).map(row=>[String(row.metric||''),String(row.value??'')]));
const dogToken=row=>{const match=String(row?.dog||row?.dog_name||'').match(/#(\d+)/);return match?Number(match[1]):Number(row?.token_id);};
const currentFeedRow=(rows,token)=>asRows(rows).find(row=>dogToken(row)===token)||asRows(rows).find(row=>{const status=String(row.status||'').toLowerCase();return status==='live'||status.includes('ongoing');})||asRows(rows)[0]||{};
const parseTrait=value=>{const text=String(value||'').trim();const split=text.indexOf(':');if(split<0)return {text,type:'',value:text,rarity:''};const type=text.slice(0,split).trim();let traitValue=text.slice(split+1).trim();let rarity='';const match=traitValue.match(/^(.*?)\s+(\([^)]+%\))$/);if(match){traitValue=match[1].trim();rarity=match[2];}return {text,type,value:traitValue,rarity};};
const traitUrl=trait=>`https://opensea.io/collection/degen-dogs-club?traits=${encodeURIComponent(JSON.stringify([{traitType:trait.type,values:[trait.value]}]))}`;
const renderTraits=row=>{if(!currentTraits)return;const source=row.trait_rarity||row.traits||'';currentTraits.innerHTML=String(source).split(';').map(parseTrait).filter(trait=>trait.value).map(trait=>{if(!trait.type)return `<span class="trait-pill">${escapeHtml(trait.text)}</span>`;const label=`View Degen Dogs with ${trait.type}: ${trait.value} on OpenSea`;return `<a class="trait-pill trait-pill-link" href="${attr(traitUrl(trait))}" target="_blank" rel="noopener noreferrer" aria-label="${attr(label)}" title="${attr(label)}"><span class="trait-type">${escapeHtml(trait.type)}</span><span class="trait-value">${escapeHtml(trait.value)}</span>${trait.rarity?`<span class="trait-rarity">${escapeHtml(trait.rarity)}</span>`:''}</a>`;}).join('');};
const bidHistoryHighFirst=rows=>asRows(rows).map((row,index)=>({row,index})).sort((a,b)=>{const amount=(toNumber(b.row.bid_eth)??-1)-(toNumber(a.row.bid_eth)??-1);if(amount)return amount;const block=(toNumber(b.row.block_number)??-1)-(toNumber(a.row.block_number)??-1);if(block)return block;const log=(toNumber(b.row.log_index)??-1)-(toNumber(a.row.log_index)??-1);if(log)return log;const time=(parseUtc(b.row.bid_time_utc)||0)-(parseUtc(a.row.bid_time_utc)||0);return time||b.index-a.index;}).map(item=>item.row);
const historyMenuHtml=(rows,wasOpen=false)=>{rows=bidHistoryHighFirst(rows);if(!rows.length)return '';const count=rows.length;const items=rows.map((row,index)=>{const bidder=row.bidder||shortAddress(row.bidder_wallet)||'Unknown bidder';const bidderUrl=safeUrl(row.bidder_url)||safeUrl(row.bidder_wallet?`https://basescan.org/address/${row.bidder_wallet}`:'');const txUrl=safeUrl(row.tx_hash?`https://basescan.org/tx/${row.tx_hash}`:'');const bidderHtml=bidderUrl?`<a href="${attr(bidderUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(bidder)}</a>`:escapeHtml(bidder);const wallet=row.bidder_wallet?`<code class="bid-history-wallet">${escapeHtml(row.bidder_wallet)}</code>`:'';const tx=txUrl?`<a class="bid-history-tx" href="${attr(txUrl)}" target="_blank" rel="noopener noreferrer">Tx</a>`:'';const rank=index===0?'High bid':`Bid ${count-index}`;return `<li class="bid-history-row"><span class="bid-history-rank">${rank}</span><span class="bid-history-main"><strong>${escapeHtml(row.bid||`${row.bid_eth||''} ETH`)}</strong>${bidderHtml}</span><span class="bid-history-meta"><time>${escapeHtml(row.bid_time_utc||'')}</time>${tx}</span>${wallet}</li>`;}).join('');return `<details class="bid-history-menu"${wasOpen?' open':''}><summary><span>Bid history</span><b>${count} bid${count===1?'':'s'}</b></summary><ol class="bid-history-list">${items}</ol></details>`;};
const metricNumber=(metrics,key)=>toNumber(metrics[key]);
const metricAmount=(metrics,key,places,suffix)=>{const value=metricNumber(metrics,key);return value===null?'':`${value.toLocaleString(undefined,{minimumFractionDigits:places,maximumFractionDigits:places})}${suffix}`;};
const rewardTile=(label,value,note,title='')=>value?`<span class="reward-tile"${title?` title="${attr(title)}"`:''}><b>${escapeHtml(label)}</b><strong>${value}</strong><em>${escapeHtml(note)}</em>${title?`<small class="reward-caveat sr-only">${escapeHtml(title)}</small>`:''}</span>`:'';
const renderRewards=metrics=>{if(!currentRewards)return;const woof=metricAmount(metrics,'reward_woof_per_dog_per_day',2,' WOOF/day');const woofUsd=metricAmount(metrics,'reward_woof_per_dog_usd_per_day',2,'/day');const sup=metricAmount(metrics,'reward_sup_per_dog_per_day',2,' SUP/day');const supUsd=metricAmount(metrics,'reward_sup_per_dog_usd_per_day',2,'/day');const total=metricAmount(metrics,'reward_total_per_dog_usd_per_day',2,'/day');const tiles=[rewardTile('WOOF / Dog',woof?`${escapeHtml(woof)}${woofUsd?` <span>($${escapeHtml(woofUsd)})</span>`:''}`:'','Observed stream'),rewardTile('SUP / Dog',sup?`${escapeHtml(sup)}${supUsd?` <span>($${escapeHtml(supUsd)})</span>`:''}`:'','Observed stream'),rewardTile('Total / Dog',total?`$${escapeHtml(total)}`:'','WOOF + SUP')];const s6Enabled=/^(true|1|yes|live_estimate)$/i.test(metrics.season6_sup_enabled||metrics.season6_sup_status||'');const currentWallet=String(metrics.current_bidder_wallet||'').toLowerCase();const s6Wallet=String(metrics.season6_sup_current_bidder_wallet||'').toLowerCase();const s6Aligned=!s6Wallet||s6Wallet===currentWallet;if(s6Enabled&&s6Aligned){const noBid=metrics.season6_sup_estimate_status==='no_current_bid'||(!metrics.season6_sup_current_bidder_wallet&&!metrics.season6_sup_current_bid_estimated_cap_aware_sup);const supEstimate=metricNumber(metrics,'season6_sup_current_bid_estimated_cap_aware_sup');const usdEstimate=metricNumber(metrics,'season6_sup_current_bid_estimated_cap_aware_usd');const main=noBid?'Bid to estimate S6 SUP':`≈${(supEstimate??0).toLocaleString(undefined,{maximumFractionDigits:0})} SUP`;const secondary=noBid?'Current high bidder needed':`≈$${(usdEstimate??0).toLocaleString(undefined,{maximumFractionDigits:0})}`;tiles.push(`<span class="reward-tile season6-sup-estimate"><b>Season 6 if bid wins</b><strong>${escapeHtml(main)}</strong><em>${escapeHtml(secondary)} · Wallet-level estimate</em></span>`);}const days=metricNumber(metrics,'reward_current_bid_payback_days');const apr=metrics.reward_current_bid_apr_display||'';const payback=days&&days>0?(days<1?'&lt;1 day':`≈${days<10?days.toFixed(1):days.toFixed(0)} days`):(metrics.reward_current_bid_payback_days?'N/A':'');if(payback||apr){const caveat='Simple APR estimate from the current bid and observed per-Dog daily reward flow; not guaranteed future return.';tiles.push(rewardTile('Bid payback',`<span class="payback-days">${payback}</span><span class="payback-apr">${escapeHtml(apr)}</span>`,'Current bid / observed per-Dog flow',caveat));}const body=tiles.filter(Boolean).join('');currentRewards.innerHTML=body?`<section class="reward-strip" aria-label="Per-Dog reward estimate">${body}</section>`:'';};
const hydrateCurrentCard=(feed,current,history,metrics)=>{const dog=feed.dog||String(current.dog_name||'').replace(/^Degen\s+/,'')||`Dog #${current.token_id??''}`;const dogUrl=safeUrl(feed.dog_opensea_url||feed.dog_external_url||current.dog_opensea_url||current.dog_external_url);if(currentDogHeading){currentDogHeading.innerHTML=dogUrl?`<a class="current-dog-link" href="${attr(dogUrl)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${attr(dog)}" title="Open ${attr(dog)}">${escapeHtml(dog)}</a>`:escapeHtml(dog);}const status=feed.status||current.auction_state||'';const bid=feed.bid||current.current_bid||`${current.current_bid_eth??metrics.current_bid_eth??0} ETH`;const bidder=feed.bidder_winner||current.bidder||metrics.current_bidder||'';const bidderUrl=safeUrl(feed.bidder_winner_url||current.bidder_url);const end=feed.auction_end_utc||current.end_time_utc||metrics.current_auction_end_utc||'';const time=feed.time_remaining||current.time_remaining||'';const rarity=feed.rarity||current.rarity||'';const rarityUniverse=String(metrics.dog_rarity_universe_count||'');const rarityExcluded=String(metrics.dog_rarity_excluded_nonexistent_count||'');const rarityTitle=/^\d+$/.test(rarityUniverse)&&/^\d+$/.test(rarityExcluded)?`Ranked across ${rarityUniverse} Base-existing Dogs; ${rarityExcluded} canonically nonexistent Base IDs excluded`:'';const state=timerState(Math.max(0,Math.floor((parseUtc(end)-Date.now())/1000)),String(status).toLowerCase().includes('settled')||String(status).toLowerCase().includes('ended'));const wasOpen=Boolean(currentDetail?.querySelector('.bid-history-menu[open]'));const parts=[status?`<span class="detail-status"><b>Status</b>${escapeHtml(status)}</span>`:'',bid?`<span class="detail-bid"><b>Bid</b>${escapeHtml(bid)}</span>`:'',time||end?`<span class="detail-time timer-card timer-card--${state}" data-auction-status="${attr(String(status).toLowerCase())}"><b class="timer-label">Time left</b><span class="countdown timer-value countdown--${state}" data-countdown-end="${attr(end)}" data-auction-status="${attr(String(status).toLowerCase())}">${escapeHtml(time||'')}</span></span>`:'',rarity?`<span class="detail-rarity"${rarityTitle?` title="${attr(rarityTitle)}"`:''}><b>Base rarity</b>${escapeHtml(rarity)}</span>`:'',bidder?`<span class="detail-bidder"><b>High bidder</b>${bidderUrl?`<a href="${attr(bidderUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(bidder)}</a>`:escapeHtml(bidder)}</span>`:'',historyMenuHtml(history,wasOpen)];if(currentDetail)currentDetail.innerHTML=parts.join('');const image=safeUrl(feed.dog_image_url||current.dog_image_url,SAFE_IMAGE_HOSTS);if(currentDogStage){currentDogStage.href=dogUrl||'#';currentDogStage.setAttribute('aria-label',dogUrl?`Open ${dog}`:dog);currentDogStage.replaceChildren();if(image){const img=document.createElement('img');img.src=image;img.alt=`${dog} image`;currentDogStage.appendChild(img);}}renderTraits({...current,...feed});renderRewards(metrics);updateCountdowns();};
const normalizedState=value=>{const state=String(value||'').toLowerCase();return state==='live'||state.includes('ongoing')?'live':state.includes('settled')||state.includes('ended')?'ended':state;};
const assertSame=(label,values,normalize=value=>String(value))=>{const present=values.filter(value=>value!==null&&value!==undefined&&String(value)!=='').map(normalize);if(new Set(present).size>1)throw new Error(`${label} datasets disagree`);};
const canonicalUint=(value,label,minimum=0)=>{const text=String(value??'').trim();if(!/^(0|[1-9]\d*)$/.test(text))throw new Error(`${label} is not a canonical integer`);const number=Number(text);if(!Number.isSafeInteger(number)||number<minimum)throw new Error(`${label} is out of range`);return number;};
const normalizedAmount=value=>{const text=String(value??'').trim();const number=Number(text);if(!text||!Number.isFinite(number)||number<0)throw new Error('amount is not finite and nonnegative');return number.toFixed(12);};
const assertStatusAttestation=status=>{const blockNumber=canonicalUint(status.latest_generated_block,'snapshot block',1);if(status.last_refresh_result!=='success_generated'||!setVerificationState(status))throw new Error('verified snapshot is stale or unsuccessful');if(!/^0x[0-9a-f]{64}$/i.test(String(status.snapshot_block_hash||'')))throw new Error('verified snapshot hash is invalid');for(const key of ['auction_house_code_sha256','dog_nft_code_sha256'])if(!/^[0-9a-f]{64}$/i.test(String(status[key]||'')))throw new Error(`verified ${key} is invalid`);const quorum=canonicalUint(status.rpc_quorum_size,'RPC quorum size',2);const agreement=String(status.rpc_quorum_agreement||'').match(/^(\d+)\/(\d+)$/);if(!agreement||canonicalUint(agreement[1],'RPC agreement',quorum)>canonicalUint(agreement[2],'RPC responders',quorum))throw new Error('verified RPC agreement is invalid');const providers=new Set(String(status.rpc_quorum_providers||'').split(',').map(value=>value.trim()).filter(Boolean));if(providers.size<quorum)throw new Error('verified RPC provider set is incomplete');if(canonicalUint(status.snapshot_confirmations,'snapshot confirmations',1)<1)throw new Error('snapshot is unconfirmed');if(canonicalUint(status.onchain_chain_id,'onchain chain ID',8453)!==8453)throw new Error('snapshot is not Base mainnet');const scope=new Set(String(status.onchain_verification_scope||'').split(',').map(value=>value.trim()).filter(Boolean));for(const required of ['snapshot_hash','contract_code','current_auction','dog_total_supply','dog_token_uri_bindings','recent_event_logs'])if(!scope.has(required))throw new Error(`verified snapshot scope is missing ${required}`);return String(blockNumber);};
const assertCurrentSnapshot=(status,current,feed,history,metrics)=>{const block=assertStatusAttestation(status);if(String(current.latest_block||'')!==block||String(metrics.latest_block||'')!==block)throw new Error('generated snapshot is not atomic yet');if(canonicalUint(metrics.onchain_chain_id,'metrics chain ID',8453)!==8453)throw new Error('metrics are not Base mainnet');const token=canonicalUint(status.current_dog_token_id??current.token_id,'current dog token');if(canonicalUint(current.token_id,'current auction token')!==token||canonicalUint(dogToken(feed),'auction feed token')!==token||canonicalUint(metrics.current_auction_token_id,'metrics auction token')!==token)throw new Error('current auction datasets disagree');assertSame('auction state',[status.current_auction_status,current.auction_state,feed.status,metrics.current_auction_status],normalizedState);assertSame('high bidder',[status.current_high_bidder_wallet,current.bidder_wallet,feed.bidder_winner_wallet,metrics.current_bidder_wallet],value=>String(value).toLowerCase());assertSame('current bid',[status.current_bid_eth,current.current_bid_eth,feed.amount_eth,metrics.current_bid_eth],normalizedAmount);const hashes=[status.snapshot_block_hash,metrics.snapshot_block_hash].filter(Boolean);if(hashes.length!==2)throw new Error('verified snapshot hash is missing');assertSame('snapshot hash',hashes,value=>String(value).toLowerCase());const verification=[status.onchain_verification_status,metrics.onchain_verification_status].filter(Boolean);if(verification.length!==2||verification.some(value=>value!=='current_snapshot_cross_provider_verified'))throw new Error('current snapshot is not cross-provider verified');const topBid=bidHistoryHighFirst(history)[0];if(topBid){if(canonicalUint(topBid.token_id,'current bid history token')!==token)throw new Error('current bid history token disagrees');assertSame('bid history bidder',[current.bidder_wallet,topBid.bidder_wallet],value=>String(value).toLowerCase());assertSame('bid history amount',[current.current_bid_eth,topBid.bid_eth],normalizedAmount);}return {block,token};};
const assertArchiveSnapshot=(context,records)=>{const unified=asRows(records).find(row=>Number(row.mission)===3&&Number(row.dog_id)===context.token);if(!unified)throw new Error('archive index is behind current auction');assertSame('archive state',[context.status.current_auction_status,context.current.auction_state,context.feed.status,context.metrics.current_auction_status,unified.status],normalizedState);assertSame('archive high bidder',[context.status.current_high_bidder_wallet,context.current.bidder_wallet,context.feed.bidder_winner_wallet,context.metrics.current_bidder_wallet,unified.winner_or_high_bidder?.wallet],value=>String(value).toLowerCase());assertSame('archive current bid',[context.status.current_bid_eth,context.current.current_bid_eth,context.feed.amount_eth,context.metrics.current_bid_eth,unified.amount?.native],normalizedAmount);};
const queueArchiveRefresh=context=>{pendingArchiveContext=context;if(archiveRefreshPromise)return archiveRefreshPromise;archiveRefreshPromise=(async()=>{while(pendingArchiveContext){const target=pendingArchiveContext;pendingArchiveContext=null;try{const records=await fetchGenerated('unified_dog_search_index',target.block);if(target.key!==liveSnapshotKey)continue;assertArchiveSnapshot(target,records);const nextRecords=asRows(records);const previousRecords=unifiedRecords;const previousReady=unifiedReady;unifiedRecords=nextRecords;unifiedReady=true;try{renderArchive();}catch(error){unifiedRecords=previousRecords;unifiedReady=previousReady;throw error;}unifiedSnapshotBlock=target.block;archiveSnapshotKey=target.key;}catch(_){if(!unifiedReady)fallbackAuctionRows();}}})().finally(()=>{archiveRefreshPromise=null;});return archiveRefreshPromise;};
const refreshLiveSurface=()=>liveRefreshPromise||(liveRefreshPromise=(async()=>{const status=await fetchGenerated('refresh_status',Date.now());const nextBlock=assertStatusAttestation(status);const nextKey=`${nextBlock}:${status.last_successful_refresh_time_utc||''}`;if(liveSnapshotBlock&&Number(nextBlock)<Number(liveSnapshotBlock))throw new Error('verified snapshot block regressed');let context=liveSnapshotContext;if(nextKey!==liveSnapshotKey){const [currentRows,feedRows,historyRows,metricRows]=await Promise.all([fetchGenerated('current_auction',nextBlock),fetchGenerated('auction_feed',nextBlock),fetchGenerated('current_auction_bid_history',nextBlock),fetchGenerated('mission3_metrics',nextBlock)]);const current=asRows(currentRows)[0];if(!current)throw new Error('current auction unavailable');const metrics=metricRowsToMap(metricRows);const token=canonicalUint(status.current_dog_token_id??current.token_id,'current dog token');const feed=currentFeedRow(feedRows,token);const snapshot=assertCurrentSnapshot(status,current,feed,historyRows,metrics);context={...snapshot,key:nextKey,status,current,feed,history:historyRows,metrics};hydrateCurrentCard(feed,current,historyRows,metrics);liveSnapshotContext=context;liveSnapshotBlock=snapshot.block;liveSnapshotKey=nextKey;}else{if(!context)throw new Error('verified snapshot context is unavailable');assertCurrentSnapshot(status,context.current,context.feed,context.history,context.metrics);context={...context,status};liveSnapshotContext=context;}if(context&&archiveSnapshotKey!==nextKey)queueArchiveRefresh(context);})().catch(error=>{const message=error instanceof Error?error.message:'unknown error';console.warn('Live dashboard refresh failed:',message);setVerificationState(liveSnapshotContext?.status,true);if(liveLabel)liveLabel.dataset.liveError=message;if(!unifiedReady)fallbackAuctionRows();}).finally(()=>{liveRefreshPromise=null;}));
const emptyArchiveMessage=()=>archiveState.mission!=='all'&&!archiveState.query?`No verified Mission ${archiveState.mission} auction rows are available yet.`:'No auctions found for this search.';
const setAuctionRows=(records,label,total=records.length)=>{if(!auctionBody)return;auctionBody.innerHTML=records.length?records.map(unifiedRowHtml).join(''):`<tr><td colspan="6">${escapeHtml(emptyArchiveMessage())}</td></tr>`;if(auctionTotal){auctionTotal.dataset.total=String(total);auctionTotal.textContent=label||`${total} rows`;}};
const filteredRows=()=>{let rows=unifiedRecords;if(archiveState.mission!=='all')rows=rows.filter(record=>String(record.mission)===archiveState.mission);const q=archiveState.query;if(q)rows=rows.filter(record=>matchesQuery(record,q));return rows;};
const sortRows=rows=>{const dogQuery=exactDogQuery(archiveState.query);rows=[...rows];if(archiveState.sortMode==='highest_usd'){return rows.sort((a,b)=>{const av=getUsdSortValue(a);const bv=getUsdSortValue(b);const aMissing=av===null||Number.isNaN(av);const bMissing=bv===null||Number.isNaN(bv);if(aMissing&&bMissing)return compareNewest(a,b);if(aMissing)return 1;if(bMissing)return -1;return bv-av||compareNewest(a,b);});}return rows.sort((a,b)=>{if(dogQuery!==null){const ae=Number(a.dog_id)===dogQuery;const be=Number(b.dog_id)===dogQuery;if(ae!==be)return ae?-1:1;}return compareNewest(a,b);});};
const renderPagination=(total,totalPages,start,count)=>{const end=count?start+count:0;if(showingLabel)showingLabel.textContent=total?`Showing ${start+1}–${end} of ${total}`:'Showing 0 of 0';if(pageLabel)pageLabel.textContent=`Page ${archiveState.currentPage} of ${totalPages}`;if(pagePrev)pagePrev.disabled=archiveState.currentPage<=1;if(pageNext)pageNext.disabled=archiveState.currentPage>=totalPages;if(archiveCaveat)archiveCaveat.textContent=archiveState.sortMode==='highest_usd'?'USD values are historical estimates where available. Missing estimates sort last.':'';};
const renderArchive=()=>{if(!auctionBody||!unifiedReady)return;syncControls();let rows=filteredRows();rows=sortRows(rows);const total=rows.length;const totalPages=Math.max(1,Math.ceil(total/archiveState.pageSize));archiveState.currentPage=Math.min(Math.max(1,archiveState.currentPage),totalPages);const start=(archiveState.currentPage-1)*archiveState.pageSize;const pageRows=rows.slice(start,start+archiveState.pageSize);const label=isDefaultArchiveState()?'Latest 10 archive records':`${total} archive ${total===1?'match':'matches'}`;setAuctionRows(pageRows,label,total);renderPagination(total,totalPages,start,pageRows.length);updateCounts();};
const restoreAuctionRows=()=>{archiveState.query='';archiveState.mission='all';archiveState.sortMode='newest';archiveState.pageSize=10;archiveState.currentPage=1;renderArchive();};
const matchesQuery=(record,q)=>{let remaining=q;const missionMatch=remaining.match(/(?:^|\s)mission\s*:?\s*([123])(?=\s|$)/);if(missionMatch&&Number(record.mission)!==Number(missionMatch[1]))return false;remaining=remaining.replace(/(?:^|\s)mission\s*:?\s*[123](?=\s|$)/g,' ');const dogMatch=remaining.match(/(?:^|\s)dog\s*#?\s*(\d{1,4})(?=\s|$)/);if(dogMatch&&Number(record.dog_id)!==Number(dogMatch[1]))return false;remaining=remaining.replace(/(?:^|\s)dog\s*#?\s*\d{1,4}(?=\s|$)/g,' ');const terms=remaining.split(/\s+/).filter(Boolean);const haystack=rowSearchText(record);return terms.every(term=>haystack.includes(term));};
const fallbackAuctionRows=()=>{if(!auctionBody)return;const rows=defaultRows.slice(0,10);auctionBody.replaceChildren(...rows.map(row=>row.cloneNode(true)));if(auctionTotal){auctionTotal.dataset.total=String(rows.length);auctionTotal.textContent='Latest 10 archive records';}renderPagination(rows.length,1,0,rows.length);updateCounts();};
filter?.addEventListener('input',()=>{archiveState.query=filter.value.trim().toLowerCase();archiveState.currentPage=1;renderArchive();});
missionButtons.forEach(button=>button.addEventListener('click',()=>{archiveState.mission=button.dataset.missionFilter||'all';archiveState.currentPage=1;renderArchive();}));
sortSelect?.addEventListener('change',()=>{archiveState.sortMode=sortSelect.value||'newest';archiveState.currentPage=1;renderArchive();});
pageSizeSelect?.addEventListener('change',()=>{archiveState.pageSize=Math.min(100,Math.max(10,Number(pageSizeSelect.value)||10));archiveState.currentPage=1;renderArchive();});
pagePrev?.addEventListener('click',()=>{archiveState.currentPage=Math.max(1,archiveState.currentPage-1);renderArchive();});
pageNext?.addEventListener('click',()=>{archiveState.currentPage+=1;renderArchive();});
const updateCountdowns=()=>{const now=Date.now();document.querySelectorAll('[data-countdown-end]').forEach(el=>{const end=parseUtc(el.dataset.countdownEnd);if(!Number.isFinite(end))return;const box=el.closest('.timer-card');const status=String(el.dataset.auctionStatus||box?.dataset.auctionStatus||'').toLowerCase();const forceEnded=status.includes('settled')||status.includes('ended');const seconds=forceEnded?0:Math.max(0,Math.floor((end-now)/1000));const state=timerState(seconds,forceEnded);el.textContent=state==='ended'?'ended':formatDuration(seconds);applyTimerState(el,state);});};
const updateCounts=()=>{document.querySelectorAll('table').forEach(table=>{if(!table.tBodies.length)return;const rows=[...table.tBodies[0].rows];const visible=rows.filter(row=>!row.hidden).length;const total=table.caption?.querySelector('[data-total]');if(total&&!table.matches('[data-table="auction_feed"]')){const suffix=visible===Number(total.dataset.total)?' rows':` / ${total.dataset.total} rows`;total.textContent=`${visible}${suffix}`;}});};
document.querySelectorAll('th button').forEach(button=>{button.addEventListener('click',()=>{const table=button.closest('table');const tbody=table.tBodies[0];const col=Number(button.dataset.col);const next=button.dataset.dir==='asc'?'desc':'asc';table.querySelectorAll('th').forEach(th=>{const b=th.querySelector('button');if(b)delete b.dataset.dir;th.setAttribute('aria-sort','none');});button.dataset.dir=next;button.closest('th').setAttribute('aria-sort',next==='asc'?'ascending':'descending');const rows=[...tbody.rows].sort((a,b)=>{const av=key(a.cells[col]?.textContent||'');const bv=key(b.cells[col]?.textContent||'');const cmp=typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv));return next==='asc'?cmp:-cmp;});rows.forEach(row=>tbody.appendChild(row));});});
updateCounts();
updateCountdowns();
setInterval(updateCountdowns,1000);
const scheduleLiveRefresh=()=>window.setTimeout(async()=>{await refreshLiveSurface();scheduleLiveRefresh();},LIVE_REFRESH_MS);
refreshLiveSurface().finally(scheduleLiveRefresh);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshLiveSurface();});
window.addEventListener('online',refreshLiveSurface);
""".strip()
    style_hash = base64.b64encode(hashlib.sha256(css.encode("utf-8")).digest()).decode("ascii")
    script_hash = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    content_security_policy = "; ".join(
        [
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            f"script-src 'sha256-{script_hash}'",
            f"style-src 'sha256-{style_hash}'",
            "img-src 'self' data: https://api.degendogs.club https://degendogs.club https://ipfs.io",
            "connect-src 'self'",
            "form-action 'none'",
        ]
    )
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#e8ded5">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="{html.escape(content_security_policy, quote=True)}">
<link rel="icon" type="image/png" sizes="32x32" href="favicon-32x32.png">
<title>Degen Dogs Mission 3 Auctions</title>
<style>{css}</style>
</head>
<body>
<div class="shell">
  <section class="current-card" data-current-card aria-label="Current auction">
    <div class="current-copy">
      <div class="topline"><div class="eyebrow">{live_dot_html}<span data-live-label>Mission 3 auction feed · verification pending</span></div>{top_actions_html}</div>
      <h1 data-current-dog>{current_dog_html}</h1>
      <div class="current-detail" data-current-detail>{current_detail}</div>
      <div data-current-rewards>{reward_strip}</div>
      <div class="traits" data-current-traits aria-label="Current dog traits and rarity">{chips}</div>
    </div>
    <a class="dog-stage" data-current-dog-stage href="{html.escape(current_dog_url, quote=True)}" target="_blank" rel="noopener noreferrer" aria-label="{html.escape(current_dog_label, quote=True)}">{image_html}</a>
  </section>
  <div class="toolbar" aria-label="Auction archive controls">
    <div class="toolbar-field search-field"><label for="filter">Search auctions</label><input id="filter" type="search" aria-label="search unified Mission 1, 2, and 3 archive" placeholder="Search all missions: Dog #, wallet, handle, tx, chain, status" autocomplete="off"></div>
    <div class="toolbar-group mission-group" role="group" aria-label="Filter by mission"><span class="toolbar-legend">Mission</span><span class="mission-toggle"><button type="button" data-mission-filter="all" aria-pressed="true">All</button><button type="button" data-mission-filter="1" aria-pressed="false">Mission 1</button><button type="button" data-mission-filter="2" aria-pressed="false">Mission 2</button><button type="button" data-mission-filter="3" aria-pressed="false">Mission 3</button></span></div>
    <div class="toolbar-field"><label for="auction-sort">Sort by</label><select id="auction-sort" aria-label="Sort auctions"><option value="newest" selected>Newest first</option><option value="highest_usd">Highest USD bid</option></select></div>
    <div class="toolbar-field"><label for="auction-page-size">Rows</label><select id="auction-page-size" aria-label="Rows per page"><option value="10" selected>10</option><option value="25">25</option><option value="50">50</option><option value="100">100</option></select></div>
    <div class="toolbar-group pagination" aria-label="Auction pagination"><span id="auction-showing" class="archive-status" aria-live="polite">Showing 1–10</span><button id="auction-prev" class="page-btn" type="button" disabled>Previous</button><span id="auction-page-label" class="archive-status">Page 1 of 1</span><button id="auction-next" class="page-btn" type="button">Next</button><span id="auction-caveat" class="archive-caveat" aria-live="polite"></span></div>
  </div>
  <main class="primary-grid">{''.join(primary_parts)}</main>
  {site_metric_html}
</div>
<script>{script}</script>
</body>
</html>
"""
    atomic_write_text(ROOT / "index.html", html_doc)

def main() -> None:
    progress("start")
    ensure_owned_directory_tree(GENERATED)
    ensure_owned_directory_tree(PUBLIC_GENERATED)
    latest_block, latest_block_data, onchain_verification = verified_snapshot()
    snapshot_tag = hex(latest_block)
    latest_time = utc_from_unix(int(latest_block_data["timestamp"], 16))
    progress(
        f"snapshot latest_block={latest_block} hash={onchain_verification['snapshot_block_hash']} "
        f"quorum={onchain_verification['rpc_quorum_agreement']}"
    )

    # Read critical state immediately while every fallback provider still
    # serves the just-selected block from its hot window. Long historical log
    # scans happen only after the publish-critical calls have quorum agreement.
    dog_total_supply = fetch_dog_total_supply(snapshot_tag)
    current = fetch_current_auction(latest_block, latest_time, snapshot_tag)
    token_stats = fetch_token_stats(snapshot_tag)
    token_stats.update(onchain_verification)
    decimals = int(token_stats["woof_decimals"])
    token_stats["dog_total_supply"] = str(dog_total_supply)
    token_stats["dog_id_ceiling"] = str(dog_total_supply)
    token_stats.update(current_bid_reward_stats(current, token_stats))
    snapshot_block_hash = onchain_verification["snapshot_block_hash"]
    dog_token_uris = fetch_token_uri_bindings(
        list(range(dog_total_supply)),
        snapshot_tag,
        block_hash=snapshot_block_hash,
    )
    token_uri_present_count = sum(uri is not None for uri in dog_token_uris.values())
    token_uri_unavailable_count = dog_total_supply - token_uri_present_count
    base_existing_ids = sorted(token_id for token_id, uri in dog_token_uris.items() if uri is not None)
    base_unclaimed_ids = sorted(token_id for token_id, uri in dog_token_uris.items() if uri is None)
    base_existing_ids_sha256 = hashlib.sha256(
        ",".join(str(token_id) for token_id in base_existing_ids).encode("ascii")
    ).hexdigest()
    base_unclaimed_ids_sha256 = hashlib.sha256(
        ",".join(str(token_id) for token_id in base_unclaimed_ids).encode("ascii")
    ).hexdigest()
    token_stats.update(
        {
            "dog_token_uri_verification_status": "hash_pinned_cross_provider_exact_outcome_quorum",
            "dog_base_existence_verification_status": "hash_pinned_cross_provider_exists_token_uri_parity_quorum",
            "dog_token_uri_present_count": str(token_uri_present_count),
            "dog_token_uri_unavailable_count": str(token_uri_unavailable_count),
            "dog_base_existing_count": str(token_uri_present_count),
            "dog_base_unclaimed_count": str(token_uri_unavailable_count),
            "dog_base_existing_token_ids_sha256": base_existing_ids_sha256,
            "dog_base_unclaimed_token_ids_sha256": base_unclaimed_ids_sha256,
        }
    )
    progress(
        f"critical state loaded dog_total_supply={dog_total_supply} "
        f"current_token_id={current.get('token_id')} token_uri_present={token_uri_present_count} "
        f"token_uri_unavailable={token_uri_unavailable_count}"
    )

    auction_logs = fetch_logs(
        AUCTION_HOUSE,
        [TOPIC_AUCTION_CREATED, TOPIC_AUCTION_BID, TOPIC_AUCTION_EXTENDED, TOPIC_AUCTION_SETTLED],
        FROM_BLOCK,
        latest_block,
    )
    progress(f"auction logs={len(auction_logs)}")
    created_logs = [log for log in auction_logs if log["topics"][0].lower() == TOPIC_AUCTION_CREATED]
    bid_logs = [log for log in auction_logs if log["topics"][0].lower() == TOPIC_AUCTION_BID]
    extension_logs = [log for log in auction_logs if log["topics"][0].lower() == TOPIC_AUCTION_EXTENDED]
    settled_logs = [log for log in auction_logs if log["topics"][0].lower() == TOPIC_AUCTION_SETTLED]
    dog_metadata = fetch_dog_metadata_rows(
        dog_total_supply,
        snapshot_tag,
        token_uris=dog_token_uris,
    )
    metadata_status_counts = Counter(str(row["metadata_verification_status"]) for row in dog_metadata)
    metadata_verified_count = metadata_status_counts.get("onchain_token_uri_verified", 0)
    metadata_unavailable_count = sum(
        count
        for status, count in metadata_status_counts.items()
        if status != "onchain_token_uri_verified"
    )
    rarity_incomplete_metadata_count = max(0, token_uri_present_count - metadata_verified_count)
    rarity_verification_status = (
        "complete_verified_existing_token_universe"
        if metadata_verified_count > 0 and rarity_incomplete_metadata_count == 0
        else "unavailable_no_verified_existing_tokens"
        if metadata_verified_count == 0
        else "incomplete_existing_token_metadata"
    )
    token_stats.update(
        {
            "dog_metadata_verification_status": (
                "complete_onchain_token_uri_verified"
                if metadata_unavailable_count == 0
                else "partial_onchain_token_uri_unavailable"
                if metadata_status_counts.get("onchain_token_uri_unavailable", 0) == metadata_unavailable_count
                else "incomplete_metadata_unavailable"
            ),
            "dog_metadata_onchain_verified_count": str(metadata_verified_count),
            "dog_metadata_unavailable_count": str(metadata_unavailable_count),
            "dog_metadata_content_verification_status": "verified_token_uri_offchain_content_hash_observed",
            "dog_metadata_content_observed_count": str(metadata_verified_count),
            "dog_rarity_verification_status": rarity_verification_status,
            "dog_rarity_universe_count": str(metadata_verified_count),
            "dog_rarity_excluded_nonexistent_count": str(token_uri_unavailable_count),
            "dog_rarity_incomplete_metadata_count": str(rarity_incomplete_metadata_count),
            "dog_rarity_scope": "base_existing",
            "dog_rarity_score_method": "sum_existing_token_count_divided_by_trait_frequency_v1",
            "dog_rarity_tie_policy": "competition_rank_equal_scores_share_rank",
            "dog_rarity_trait_schema": "|".join(DOG_RARITY_TRAIT_TYPES),
            "dog_rarity_attested_block": str(latest_block),
            "dog_rarity_attested_block_hash": snapshot_block_hash,
            "dog_rarity_continuity_through_block": str(latest_block),
            "dog_rarity_continuity_through_block_hash": snapshot_block_hash,
            "dog_rarity_continuity_verification_status": (
                "full_snapshot_exists_token_uri_content_schema_attested"
            ),
        }
    )
    progress(f"dog metadata rows={len(dog_metadata)}")
    created, bids, settled = decode_auction_logs(created_logs, bid_logs, settled_logs)
    extensions = decode_auction_extension_logs(extension_logs)
    validate_auction_schedules(created, extensions, current)
    validate_exact_wei_rows(bids, wei_field="bid_wei", eth_field="bid_eth_exact", label="AuctionBid")
    validate_exact_wei_rows(settled, wei_field="amount_wei", eth_field="amount_eth_exact", label="AuctionSettled")
    validate_exact_wei_rows([current], wei_field="amount_wei", eth_field="amount_eth_exact", label="auction()")
    progress(
        f"decoded auctions created={len(created)} bids={len(bids)} "
        f"extended={len(extensions)} settled={len(settled)}"
    )
    holders = fetch_verified_woof_holders(decimals, snapshot_tag, token_stats)
    progress(
        f"holders={len(holders)} verification={token_stats['woof_holder_verification_status']}"
    )
    identity_addresses = collect_identity_addresses(current, bids, settled, holders)
    neynar_profiles = fetch_farcaster_profiles(identity_addresses)
    auction_profiles = fetch_degendogs_auction_profiles(current)
    cached_profiles = load_cached_farcaster_profiles()
    farcaster_profiles = merge_farcaster_profiles(neynar_profiles, auction_profiles, cached_profiles)
    progress(f"profiles={len(farcaster_profiles)}")

    conn = sqlite3.connect(":memory:")
    insert_rows(conn, "auction_created", created, [("token_id", "INTEGER"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT")])
    insert_rows(conn, "auction_extensions", extensions, [("token_id", "INTEGER"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER")])
    insert_rows(conn, "auction_bids", bids, [("token_id", "INTEGER"), ("bidder", "TEXT"), ("bid_eth", "REAL"), ("bid_eth_exact", "TEXT"), ("bid_wei", "TEXT"), ("extended", "INTEGER"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "auction_settled", settled, [("token_id", "INTEGER"), ("winner", "TEXT"), ("amount_eth", "REAL"), ("amount_eth_exact", "TEXT"), ("amount_wei", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "woof_holders", holders, [("address", "TEXT"), ("balance_woof", "REAL"), ("balance_raw", "TEXT")])
    insert_rows(conn, "farcaster_profiles", farcaster_profiles, [("address", "TEXT"), ("fid", "INTEGER"), ("username", "TEXT"), ("display_name", "TEXT"), ("pfp_url", "TEXT")])
    insert_rows(conn, "dog_metadata", dog_metadata, [("token_id", "INTEGER"), ("dog_name", "TEXT"), ("dog_image_url", "TEXT"), ("dog_external_url", "TEXT"), ("dog_opensea_url", "TEXT"), ("traits", "TEXT"), ("trait_rarity", "TEXT"), ("rarity", "TEXT"), ("rarity_score", "REAL"), ("metadata_verification_status", "TEXT")])
    insert_rows(conn, "token_stats", [{"metric": k, "value": v} for k, v in token_stats.items()], [("metric", "TEXT"), ("value", "TEXT")])
    insert_rows(conn, "historical_prices_daily", load_historical_price_rows(), HISTORICAL_PRICE_SCHEMA)
    insert_rows(conn, "current_auction_source", [current], [("token_id", "INTEGER"), ("amount_eth", "REAL"), ("amount_eth_exact", "TEXT"), ("amount_wei", "TEXT"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("bidder", "TEXT"), ("settled", "INTEGER"), ("latest_block", "INTEGER"), ("latest_block_time_utc", "TEXT")])

    conn.executescript(SQL_PATH.read_text(encoding="utf-8"))
    build_historical_dog_tables(conn, dog_total_supply, dog_metadata)
    season6_outputs = build_season6_sup_outputs(
        settled,
        current,
        token_stats,
        snapshot_time_utc=latest_time,
        profiles=farcaster_profiles,
    )
    insert_season6_outputs(conn, season6_outputs)

    verify_snapshot_unchanged(latest_block, onchain_verification["snapshot_block_hash"])
    progress("snapshot hash re-verified before publish")

    tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    manifest_rows = []
    for table in OUTPUT_TABLES:
        cols, rows = fetch_table(conn, table)
        tables[table] = (cols, rows)
        out_path = GENERATED / f"{table}.csv"
        write_csv(out_path, cols, rows)
        write_json(GENERATED / f"{table}.json", cols, rows)
        write_csv(PUBLIC_GENERATED / f"{table}.csv", cols, rows)
        write_json(PUBLIC_GENERATED / f"{table}.json", cols, rows)
        manifest_rows.append((table, f"generated/{table}.csv", len(rows)))

    write_csv(GENERATED / "manifest.csv", ["table", "file", "rows"], manifest_rows)
    write_json(GENERATED / "manifest.json", ["table", "file", "rows"], manifest_rows)
    write_csv(PUBLIC_GENERATED / "manifest.csv", ["table", "file", "rows"], manifest_rows)
    write_json(PUBLIC_GENERATED / "manifest.json", ["table", "file", "rows"], manifest_rows)
    expected_public_files = {"manifest.csv", "manifest.json"}
    for table in OUTPUT_TABLES:
        expected_public_files.add(f"{table}.csv")
        expected_public_files.add(f"{table}.json")
    for stale in PUBLIC_GENERATED.glob("*"):
        if stale.is_file() and stale.name not in expected_public_files:
            stale.unlink()
    write_html(tables)

    atomic_write_text(ROOT / "README.md", render_readme(tables, manifest_rows))

    print(json.dumps({"latest_block": latest_block, "tables": {k: len(v[1]) for k, v in tables.items()}}, indent=2))


if __name__ == "__main__":
    main()
