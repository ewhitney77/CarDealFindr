"""
Eligibility rules: does a listing belong in our result set at all?

This is the WHERE clause of the pipeline. Every rule here is a hard
include/exclude; "how good is it" is scoring.py's job.

Rules applied, in order (the first failure wins and is counted):
  1. Valid VIN
  2. Make not on the Stellantis exclusion list
  3. Make/model/condition matches one of SEARCH_TARGETS
  4. Model year inside the used window (used/CPO only)
  5. Trim at or above min_trim (only for targets that set one)
  6. Price known and <= BUDGET_CAP
  7. Miles known and <= MILEAGE_HARD_CAP
  8. Dealer state in ALLOWED_STATES (if the state is known)
  9. Distance <= SEARCH_RADIUS_MILES (if the distance is known)
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

import config
from .models import Listing, CONDITION_NEW, CONDITION_USED, CONDITION_CPO

log = logging.getLogger(__name__)


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower().replace("-", "").replace(" ", "")


def find_target(listing: Listing) -> Optional[dict]:
    """
    Return the SEARCH_TARGETS entry this listing satisfies, or None.
    A "used" target accepts both used and CPO listings.
    """
    lm, lmo = _norm(listing.make), _norm(listing.model)
    for t in config.SEARCH_TARGETS:
        if _norm(t["make"]) != lm:
            continue
        # Model match: exact after normalisation, or the target model is a
        # prefix of the listing model (e.g. "Q7" vs "Q7 Premium Plus").
        if lmo != _norm(t["model"]) and not lmo.startswith(_norm(t["model"])):
            continue
        if t["condition"] == CONDITION_NEW and listing.condition == CONDITION_NEW:
            return t
        if t["condition"] == CONDITION_USED and listing.condition in (CONDITION_USED, CONDITION_CPO):
            return t
    return None


def trim_rank(make: str, model: str, trim: Optional[str]) -> Optional[int]:
    """
    Position of `trim` on the ladder for this make/model (0 = lowest).
    Longest ladder entry contained in the trim string wins, so
    "Preferred Plus" beats "Preferred". None if no ladder or no match.
    """
    ladder = config.TRIM_LADDERS.get((make, model))
    if not ladder or not trim:
        return None
    t = trim.lower()
    best: Optional[tuple[int, int]] = None      # (match_length, index)
    for idx, name in enumerate(ladder):
        n = name.lower()
        if n in t and (best is None or len(n) > best[0]):
            best = (len(n), idx)
    return best[1] if best else None


def apply_filters(listings: list[Listing]) -> tuple[list[Listing], Counter]:
    """
    Split listings into (kept, rejection_counts). `rejection_counts` is a
    Counter keyed by reason so the run summary can show e.g.
    {"over_budget": 12, "over_mileage_cap": 7, "excluded_make": 1}.
    """
    kept: list[Listing] = []
    why: Counter = Counter()

    for lst in listings:
        if not lst.vin:
            why["no_vin"] += 1
            continue
        if (lst.make or "").strip().lower() in {m.lower() for m in config.EXCLUDED_MAKES}:
            why["excluded_make"] += 1
            continue
        target = find_target(lst)
        if target is None:
            why["not_in_search_set"] += 1
            continue
        if target["condition"] == CONDITION_USED:
            if lst.year is None or not (config.USED_YEAR_MIN <= lst.year <= config.USED_YEAR_MAX):
                why["outside_used_year_window"] += 1
                continue
        min_trim = target.get("min_trim")
        if min_trim:
            want = trim_rank(target["make"], target["model"], min_trim)
            have = trim_rank(target["make"], target["model"], lst.trim)
            if have is None:
                why[f"trim_unrecognised:{target['make']} {target['model']}:{lst.trim}"] += 1
                continue
            if want is not None and have < want:
                why["below_min_trim"] += 1
                continue
        if lst.price is None:
            why["no_price"] += 1
            continue
        if lst.price > config.BUDGET_CAP:
            why["over_budget"] += 1
            continue
        if lst.miles is None:
            # New cars sometimes come through with no odometer; treat as ~10.
            if lst.condition == CONDITION_NEW:
                lst.miles = 10
            else:
                why["no_mileage"] += 1
                continue
        if lst.miles > config.MILEAGE_HARD_CAP:
            why["over_mileage_cap"] += 1
            continue
        if lst.dealer_state and lst.dealer_state.upper() not in config.ALLOWED_STATES:
            why["outside_allowed_states"] += 1
            continue
        if lst.distance_miles is not None and lst.distance_miles > config.SEARCH_RADIUS_MILES:
            why["outside_radius"] += 1
            continue
        kept.append(lst)

    return kept, why
