from datetime import date

import config
from cardealfindr.models import Listing
from cardealfindr.scoring import (dom_component, expected_miles_for, mileage_component,
                                  price_component, price_drop_component, distance_component,
                                  score_all)

TODAY = date(2026, 9, 13)


def test_price_component_curve():
    assert price_component(50000, 50000)[0] == 50.0            # at market
    assert price_component(42500, 50000)[0] == 100.0           # 15% under -> max
    assert price_component(57500, 50000)[0] == 0.0             # 15% over -> min
    score, delta, flags = price_component(40000, 50000)         # 20% under
    assert score == 100.0 and delta == -20.0 and "VERIFY_PRICE" in flags
    assert price_component(45000, 50000)[2] == []               # 10% under, no flag


def test_price_component_without_basis_is_neutral():
    assert price_component(40000, None) == (50.0, None, ["NO_PRICE_BASIS"])


def test_expected_miles_is_model_age_times_12k():
    assert expected_miles_for(2023, TODAY) == 36_000          # 3 model years old in 2026
    assert expected_miles_for(2024, TODAY) == 24_000
    # current / next model year is floored at half a year
    assert expected_miles_for(2026, TODAY) == int(12_000 * config.MILEAGE_MIN_AGE_YEARS)
    assert expected_miles_for(2027, TODAY) == int(12_000 * config.MILEAGE_MIN_AGE_YEARS)


def test_mileage_component_bonus_and_flag():
    low, exp, flags = mileage_component(10_000, 2023, TODAY)
    high, _, flags_hi = mileage_component(30_000, 2023, TODAY)
    assert low > high
    assert flags == [] and flags_hi == ["HIGH_MILES"]
    assert 0 <= high <= 100


def test_dom_component():
    assert dom_component(None) == (config.DOM_UNKNOWN_SCORE, [])
    assert dom_component(0)[0] == config.DOM_FLOOR_SCORE
    assert dom_component(60) == (100.0, ["NEGOTIATE_DOM"])
    assert dom_component(30)[0] == 60.0


def test_price_drop_component_prefers_biggest_known_cut():
    assert price_drop_component(45000, None, None, None)[0] == config.PRICE_DROP_NONE_SCORE
    s, amt, pct, flags = price_drop_component(45000, 47000, None, {"max_price": 48000})
    assert amt == 3000 and abs(pct - 6.25) < 0.06 and flags == ["PRICE_DROP"]   # pct is rounded to 0.1
    assert s == min(100.0, config.PRICE_DROP_BASE_SCORE + pct * config.PRICE_DROP_PCT_MULTIPLIER)


def test_distance_component():
    assert distance_component(0) == (100.0, [])
    assert distance_component(config.SEARCH_RADIUS_MILES)[0] == 0.0
    assert distance_component(None) == (config.DISTANCE_UNKNOWN_SCORE, ["DISTANCE_UNKNOWN"])


def _l(vin, price, miles, imv=None, trim="Premium Plus", year=2023, dom=20, dist=10.0):
    return Listing(vin=vin, source="marketcheck", year=year, make="Audi", model="Q7", trim=trim,
                   condition="used", price=price, miles=miles, imv=imv, days_on_market=dom,
                   distance_miles=dist)


def test_score_all_uses_imv_when_present_else_peer_median():
    listings = [_l("V1", 45000, 15000, imv=50000), _l("V2", 46000, 20000), _l("V3", 47000, 22000),
                _l("V4", 48000, 25000)]
    scored = score_all(listings, {}, today=TODAY)
    by = {s.vin: s for s in scored}
    assert by["V1"].comparison_basis == "imv" and by["V1"].comparison_value == 50000
    assert by["V2"].comparison_basis == "peer_median_trim" and by["V2"].peer_count == 4
    assert by["V2"].comparison_value == 46500                      # median of 45,46,47,48k
    assert scored[0].vin == "V1" and scored[0].rank == 1            # cheapest vs basis, lowest miles
    assert all(0 <= s.total <= 100 for s in scored)
    assert set(scored[0].components) == set(config.SCORE_WEIGHTS)


def test_score_all_falls_back_when_trim_group_too_small():
    listings = [_l("V1", 45000, 15000, trim="Prestige"), _l("V2", 46000, 20000), _l("V3", 47000, 22000),
                _l("V4", 48000, 25000)]
    by = {s.vin: s for s in score_all(listings, {}, today=TODAY)}
    assert by["V1"].comparison_basis == "peer_median_model_year"


def test_adjacent_year_fallback_before_any_year():
    listings = [_l("A", 40000, 30000, year=2021, trim="Premium"),
                _l("B", 44000, 22000, year=2022, trim="Premium Plus"),
                _l("C", 45000, 20000, year=2022, trim="Prestige"),
                _l("D", 49000, 12000, year=2024, trim="Premium Plus")]
    by = {s.vin: s for s in score_all(listings, {}, today=TODAY)}
    assert by["A"].comparison_basis == "peer_median_adjacent_years" and by["A"].peer_count == 3
    assert by["A"].comparison_value == 44000                       # median of 40,44,45k (2024 excluded)
    assert by["D"].comparison_basis == "peer_median_model" and by["D"].peer_count == 4


def test_new_and_used_never_share_a_peer_group():
    used = [_l(f"U{i}", 45000, 20000) for i in range(3)]
    new = [Listing(vin="N1", source="marketcheck", year=2026, make="Audi", model="Q7", trim="Premium Plus",
                   condition="new", price=45000, miles=10, days_on_market=5, distance_miles=5)]
    by = {s.vin: s for s in score_all(used + new, {}, today=TODAY)}
    assert by["N1"].comparison_basis == "none"
