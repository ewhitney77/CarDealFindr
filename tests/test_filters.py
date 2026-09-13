import config
from cardealfindr.filters import apply_filters, find_target, trim_rank
from cardealfindr.models import Listing


def mk(**kw):
    base = dict(vin="WA1LXBF70PD000001", source="marketcheck", year=2023, make="Audi", model="Q7",
                trim="Premium Plus", condition="used", price=45000, miles=20000, dealer_state="MA",
                distance_miles=20.0)
    base.update(kw)
    return Listing(**base)


def test_trim_rank_longest_match_wins():
    assert trim_rank("Mazda", "CX-90", "3.3 Turbo Preferred Plus AWD") == 2
    assert trim_rank("Mazda", "CX-90", "Preferred") == 1
    assert trim_rank("Mazda", "CX-90", "Premium Plus") == 4
    assert trim_rank("Volkswagen", "Atlas", "2.0T SEL Premium R-Line") == 6
    assert trim_rank("Volkswagen", "Atlas", "2.0T SE") == 0
    assert trim_rank("Audi", "Q7", "Premium") is None        # no ladder for Q7


def test_find_target_condition_aware():
    assert find_target(mk())["model"] == "Q7"
    assert find_target(mk(condition="cpo"))["model"] == "Q7"
    assert find_target(mk(condition="new")) is None          # Q7 is used-only
    assert find_target(mk(make="Acura", model="MDX", condition="new"))["condition"] == "new"
    assert find_target(mk(make="Acura", model="MDX", condition="used"))["condition"] == "used"
    assert find_target(mk(make="Kia", model="Telluride")) is None


def test_apply_filters_reasons():
    rows = [
        mk(),                                                           # keep
        mk(vin="WA1LXBF70PD000002", make="Jeep", model="Grand Cherokee L"),   # excluded make
        mk(vin="WA1LXBF70PD000003", price=50001),                       # over budget
        mk(vin="WA1LXBF70PD000004", miles=40001),                       # over mileage cap
        mk(vin="WA1LXBF70PD000005", year=2020),                         # outside used window
        mk(vin="WA1LXBF70PD000006", dealer_state="NY"),                 # wrong state
        mk(vin="WA1LXBF70PD000007", distance_miles=250.0),              # too far
        mk(vin="WA1LXBF70PD000008", make="Mazda", model="CX-90", trim="Preferred", condition="new", year=2026, miles=5),   # below min trim
        mk(vin="WA1LXBF70PD000009", make="Mazda", model="CX-90", trim="Preferred Plus", condition="new", year=2026, miles=5),  # keep
        mk(vin="WA1LXBF70PD000010", make="Mazda", model="CX-90", trim="Signature", condition="new", year=2026, miles=5),   # unknown trim
        mk(vin="WA1LXBF70PD000011", make="Acura", model="MDX", condition="new", year=2026, miles=None),   # keep, miles defaulted
    ]
    kept, why = apply_filters(rows)
    assert {l.vin for l in kept} == {"WA1LXBF70PD000001", "WA1LXBF70PD000009", "WA1LXBF70PD000011"}
    assert why["excluded_make"] == 1 and why["over_budget"] == 1 and why["over_mileage_cap"] == 1
    assert why["outside_used_year_window"] == 1 and why["outside_allowed_states"] == 1
    assert why["outside_radius"] == 1 and why["below_min_trim"] == 1
    assert sum(v for k, v in why.items() if k.startswith("trim_unrecognised")) == 1
    assert [l for l in kept if l.vin.endswith("11")][0].miles == 10
