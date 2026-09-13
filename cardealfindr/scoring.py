"""
Deal scoring: turns a de-duplicated list of eligible listings into a ranked
list with a 0-100 total and five visible component scores.

    total = ( w_price * price_score
            + w_mileage * mileage_score
            + w_dom * dom_score
            + w_drop * price_drop_score
            + w_dist * distance_score ) / (sum of weights)

Each component is 0-100. Weights live in config.SCORE_WEIGHTS.

Component logic (all curves are tunable in config.py):

  price      delta% = (price - basis) / basis * 100
             basis  = CarGurus IMV if we have one, else the median price of
                      peers in THIS result set (same condition class + year +
                      make + model + trim; falls back to same year, then
                      model year +/- 1, then any year, whichever first has
                      enough members).
             50 at delta 0; 100 at -15%; 0 at +15%.  <= -15% adds VERIFY_PRICE.

  mileage    expected = 12,000 * model_age_years, where model age =
             current year - model year (min 0.5 yr, so a brand-new model
             year expects 6,000). ratio = miles / expected.
             100 - 50*ratio, +15 bonus under 25k miles, clamped 0..100.

  dom        days on market. 20 when brand new on the lot, 100 at 60+ days.
             >= 60 days adds NEGOTIATE_DOM.

  price_drop 40 with no known cut. Any cut: 60 + 8 per 1% cut, max 100.
             A cut can come from the source (MarketCheck ref_price) or from
             our own DB (price lower than any earlier run). Adds PRICE_DROP.

  distance   100 at 0 miles from 02038, 0 at SEARCH_RADIUS_MILES.

Flags are short upper-case codes the report turns into badges.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import config
from .models import Listing, CONDITION_NEW

log = logging.getLogger(__name__)


@dataclass
class ScoreResult:
    vin: str
    total: float = 0.0
    rank: int = 0
    components: dict = field(default_factory=dict)      # name -> 0..100
    flags: list[str] = field(default_factory=list)
    # The inputs the score was built from, so the report can explain itself
    price: Optional[int] = None
    miles: Optional[int] = None
    days_on_market: Optional[int] = None
    distance_miles: Optional[float] = None
    comparison_basis: str = "none"
    comparison_value: Optional[int] = None
    peer_count: int = 0
    price_delta_pct: Optional[float] = None
    expected_miles: Optional[int] = None
    price_drop_amount: Optional[int] = None
    price_drop_pct: Optional[float] = None
    chosen_source: str = ""
    label: str = ""                                     # "Great deal" / "Verify this" / ...


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Peer medians
# ---------------------------------------------------------------------------

def _condition_class(lst: Listing) -> str:
    """New cars and used cars must never be compared to each other."""
    return "new" if lst.condition == CONDITION_NEW else "used"


def _norm_trim(trim: Optional[str]) -> str:
    return " ".join((trim or "").lower().split())


def build_peer_medians(listings: list[Listing]) -> dict[tuple, list[tuple[int, str, int]]]:
    """
    Group every priced listing by (condition class, make, model) so
    peer_basis() can pick the tightest peer set per listing.
    Returns {(cc, make, model): [(year, normalised_trim, price), ...]}.
    """
    groups: dict[tuple, list[tuple[int, str, int]]] = {}
    for l in listings:
        if l.price is None:
            continue
        key = (_condition_class(l), (l.make or "").lower(), (l.model or "").lower())
        groups.setdefault(key, []).append((l.year or 0, _norm_trim(l.trim), l.price))
    return groups


def peer_basis(lst: Listing, groups: dict) -> tuple[str, Optional[int], int]:
    """
    Median price of the tightest peer set with >= PEER_MEDIAN_MIN_COUNT members:
      1. same year + same trim
      2. same year, any trim
      3. model year within +/- 1 (a 2022 is compared to 2021-2023)
      4. any year (last resort; the report shows this level so you know)
    The listing itself is part of its own peer set.
    """
    key = (_condition_class(lst), (lst.make or "").lower(), (lst.model or "").lower())
    rows = groups.get(key, [])
    year, trim = lst.year or 0, _norm_trim(lst.trim)
    levels = [
        ("peer_median_trim",           [p for y, t, p in rows if y == year and t == trim]),
        ("peer_median_model_year",     [p for y, t, p in rows if y == year]),
        ("peer_median_adjacent_years", [p for y, t, p in rows if abs(y - year) <= 1]),
        ("peer_median_model",          [p for y, t, p in rows]),
    ]
    for level, prices in levels:
        if len(prices) >= config.PEER_MEDIAN_MIN_COUNT:
            return level, int(statistics.median(prices)), len(prices)
    return "none", None, 0


# ---------------------------------------------------------------------------
# Component scores
# ---------------------------------------------------------------------------

def price_component(price: int, basis: Optional[int]) -> tuple[float, Optional[float], list[str]]:
    """Returns (score, delta_pct, flags)."""
    if basis is None or basis <= 0:
        return 50.0, None, ["NO_PRICE_BASIS"]
    delta = (price - basis) / basis * 100.0
    score = clamp(50.0 - delta * (50.0 / config.PRICE_FULL_SCORE_PCT))
    flags = []
    if delta <= -config.PRICE_VERIFY_THRESHOLD_PCT:
        flags.append("VERIFY_PRICE")
    return round(score, 1), round(delta, 1), flags


def expected_miles_for(year: Optional[int], today: Optional[date] = None) -> int:
    """
    EXPECTED_MILES_PER_YEAR x model age, where model age = calendar years
    between the model year and today (2023 car in 2026 = 3 years = 36,000).
    Floored at MILEAGE_MIN_AGE_YEARS so a current model year expects 6,000.
    """
    today = today or date.today()
    if not year:
        return config.EXPECTED_MILES_PER_YEAR
    age_years = max(config.MILEAGE_MIN_AGE_YEARS, float(today.year - year))
    return int(config.EXPECTED_MILES_PER_YEAR * age_years)


def mileage_component(miles: int, year: Optional[int], today: Optional[date] = None) -> tuple[float, int, list[str]]:
    expected = expected_miles_for(year, today)
    ratio = miles / expected if expected else 0.0
    score = 100.0 - config.MILEAGE_RATIO_SLOPE * ratio
    flags = []
    if miles < config.MILEAGE_PREFERRED_MAX:
        score += config.MILEAGE_PREFERRED_BONUS
    else:
        flags.append("HIGH_MILES")          # inside the hard cap, outside the preferred band
    return round(clamp(score), 1), expected, flags


def dom_component(dom: Optional[int]) -> tuple[float, list[str]]:
    if dom is None:
        return config.DOM_UNKNOWN_SCORE, []
    span = 100.0 - config.DOM_FLOOR_SCORE
    score = config.DOM_FLOOR_SCORE + span * min(dom, config.DOM_NEGOTIATE_DAYS) / config.DOM_NEGOTIATE_DAYS
    flags = ["NEGOTIATE_DOM"] if dom >= config.DOM_NEGOTIATE_DAYS else []
    return round(clamp(score), 1), flags


def price_drop_component(price: int, source_prior: Optional[int], source_pct: Optional[float],
                         observed_prior: Optional[dict]) -> tuple[float, Optional[int], Optional[float], list[str]]:
    """
    Best known price cut from two places:
      * the source's own "previous price" field (MarketCheck ref_price)
      * our own observations table (highest price seen in an earlier run)
    """
    candidates: list[tuple[int, float]] = []          # (amount, pct)
    if source_prior and source_prior > price:
        candidates.append((source_prior - price, (source_prior - price) / source_prior * 100.0))
    elif source_pct is not None and source_pct < 0 and source_prior is None:
        # some sources only give a % change
        pct = abs(source_pct)
        candidates.append((int(price * pct / (100.0 - pct)), pct))
    if observed_prior and observed_prior.get("max_price") and observed_prior["max_price"] > price:
        amt = observed_prior["max_price"] - price
        candidates.append((amt, amt / observed_prior["max_price"] * 100.0))
    if not candidates:
        return config.PRICE_DROP_NONE_SCORE, None, None, []
    amount, pct = max(candidates)
    score = clamp(config.PRICE_DROP_BASE_SCORE + pct * config.PRICE_DROP_PCT_MULTIPLIER)
    return round(score, 1), int(amount), round(pct, 1), ["PRICE_DROP"]


def distance_component(distance: Optional[float]) -> tuple[float, list[str]]:
    if distance is None:
        return config.DISTANCE_UNKNOWN_SCORE, ["DISTANCE_UNKNOWN"]
    score = 100.0 * (1.0 - distance / config.SEARCH_RADIUS_MILES)
    return round(clamp(score), 1), []


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def label_for(result: ScoreResult) -> str:
    if "VERIFY_PRICE" in result.flags:
        return "Verify this"
    if result.price_delta_pct is None:
        return "No comparison"
    if result.price_delta_pct <= -7.5:
        return "Great deal"
    if result.price_delta_pct <= -2.5:
        return "Good deal"
    if result.price_delta_pct <= 2.5:
        return "Fair price"
    return "Above market"


def score_all(listings: list[Listing], prior_prices: dict[str, dict],
              today: Optional[date] = None) -> list[ScoreResult]:
    """
    `listings`      one Listing per VIN (already merged across sources)
    `prior_prices`  {vin: {"max_price":..., "last_price":..., "last_run_id":...}}
                    from Database.prior_prices(); may be empty on the first run
    Returns ScoreResults sorted best-first with `rank` filled in.
    """
    groups = build_peer_medians(listings)
    weights = config.SCORE_WEIGHTS
    weight_sum = float(sum(weights.values()))
    results: list[ScoreResult] = []

    for l in listings:
        if l.price is None or l.miles is None:
            continue                                 # filters should have removed these
        r = ScoreResult(vin=l.vin, price=l.price, miles=l.miles, days_on_market=l.days_on_market,
                        distance_miles=l.distance_miles, chosen_source=l.source)

        # --- price vs basis -------------------------------------------------
        if l.imv:
            r.comparison_basis, r.comparison_value, r.peer_count = "imv", l.imv, 0
        else:
            r.comparison_basis, r.comparison_value, r.peer_count = peer_basis(l, groups)
        p_score, r.price_delta_pct, f = price_component(l.price, r.comparison_value)
        r.flags += f

        # --- mileage --------------------------------------------------------
        m_score, r.expected_miles, f = mileage_component(l.miles, l.year, today)
        r.flags += f

        # --- days on market -------------------------------------------------
        d_score, f = dom_component(l.days_on_market)
        r.flags += f

        # --- price drops ----------------------------------------------------
        pd_score, r.price_drop_amount, r.price_drop_pct, f = price_drop_component(
            l.price, l.source_prior_price, l.source_price_change_pct, prior_prices.get(l.vin))
        r.flags += f

        # --- distance -------------------------------------------------------
        dist_score, f = distance_component(l.distance_miles)
        r.flags += f

        r.components = {"price": p_score, "mileage": m_score, "dom": d_score,
                        "price_drop": pd_score, "distance": dist_score}
        r.total = round(sum(weights[k] * v for k, v in r.components.items()) / weight_sum, 1)
        r.label = label_for(r)
        results.append(r)

    # Best first; ties broken by lower price then lower miles.
    results.sort(key=lambda x: (-x.total, x.price or 0, x.miles or 0))
    for i, r in enumerate(results, start=1):
        r.rank = i
    return results
