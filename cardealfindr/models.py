"""
The one shape every data source is converted into.

Think of `Listing` as the row layout of a staging table: every source module
(MarketCheck, CarGurus/Apify, Auto.dev, dealer sites) is responsible for
turning its own JSON/HTML into this exact shape. From this point on, the rest
of the pipeline (filters, scoring, DB, report) never has to know where a row
came from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# Normalised condition values. "cpo" = certified pre-owned.
CONDITION_NEW = "new"
CONDITION_USED = "used"
CONDITION_CPO = "cpo"


@dataclass
class Listing:
    # ---- identity ----
    vin: str
    source: str                          # "marketcheck" | "cargurus_apify" | "autodev" | "dealer:<key>"

    # ---- what the car is ----
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    trim_tier: Optional[str] = None      # low | medium | high (from config.TRIM_LADDERS)
    condition: Optional[str] = None      # new | used | cpo
    body_type: Optional[str] = None
    drivetrain: Optional[str] = None
    exterior_color: Optional[str] = None
    interior_color: Optional[str] = None

    # ---- money & miles ----
    price: Optional[int] = None
    msrp: Optional[int] = None
    miles: Optional[int] = None

    # ---- market signals ----
    days_on_market: Optional[int] = None
    source_prior_price: Optional[int] = None    # previous price the SOURCE reports (MarketCheck ref_price)
    source_price_change_pct: Optional[float] = None
    imv: Optional[int] = None                   # CarGurus Instant Market Value
    deal_rating: Optional[str] = None           # CarGurus "Great Deal" / "Good Deal" / ...
    first_seen_at: Optional[str] = None         # source-reported first listing date (ISO string)

    # ---- where the car is ----
    dealer_name: Optional[str] = None
    dealer_city: Optional[str] = None
    dealer_state: Optional[str] = None
    dealer_zip: Optional[str] = None
    dealer_lat: Optional[float] = None
    dealer_lon: Optional[float] = None
    distance_miles: Optional[float] = None

    # ---- links & provenance ----
    listing_url: Optional[str] = None
    source_listing_id: Optional[str] = None
    raw: dict = field(default_factory=dict)     # untouched source payload, stored as JSON in the DB

    def is_new(self) -> bool:
        return self.condition == CONDITION_NEW

    def to_row(self) -> dict[str, Any]:
        """Dict without the raw payload - handy for logging and DB inserts."""
        d = asdict(self)
        d.pop("raw", None)
        return d


# ---------------------------------------------------------------------------
# Small parsing helpers shared by every source module
# ---------------------------------------------------------------------------

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def clean_vin(value: Any) -> Optional[str]:
    """Upper-case, strip, and validate a VIN. Returns None if it is not a plausible VIN."""
    if not value:
        return None
    v = str(value).strip().upper()
    return v if _VIN_RE.match(v) else None


def to_int(value: Any) -> Optional[int]:
    """'$41,995' -> 41995 ; '12,345 mi' -> 12345 ; None/'' -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^0-9.]", "", str(value))
    if not digits or digits == ".":
        return None
    try:
        return int(float(digits))
    except ValueError:
        return None


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def normalise_condition(raw: Any, is_certified: Any = None) -> Optional[str]:
    """
    Map the many ways sources say "new/used/CPO" onto our three values.
    `is_certified` is an optional extra hint (MarketCheck has a separate flag).
    """
    text = (str(raw) if raw is not None else "").strip().lower()
    certified = str(is_certified).lower() in ("1", "true", "yes") if is_certified is not None else False
    if "cert" in text or "cpo" in text or certified:
        return CONDITION_CPO
    if text.startswith("new"):
        return CONDITION_NEW
    if text.startswith("used") or text.startswith("pre") or text == "preowned":
        return CONDITION_USED
    return None


def first_present(d: dict, *keys: str, default: Any = None) -> Any:
    """
    Return the first non-empty value among several candidate keys.
    Supports dotted paths ("dealer.city"). Used heavily by the Apify and
    Auto.dev adapters where the exact field names may drift.
    """
    for key in keys:
        cur: Any = d
        ok = True
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return default
