"""End-to-end on the fixture with an in-memory database: two runs, a price drop between them."""
import json
import os

import pytest

import config
from cardealfindr.db import Database
from cardealfindr.pipeline import run_pipeline
from cardealfindr.report import build_report

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_listings.json")


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def test_two_runs_persist_history_and_detect_drop(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_DIR", str(tmp_path))
    run1 = run_pipeline(db, ["fixture"], {}, fixture_path=FIXTURE)
    lb = db.leaderboard(run1, 100)
    assert len(lb) > 10
    vins = {r["vin"] for r in lb}
    assert not any(r["make"] == "Jeep" for r in lb)
    assert all(r["price"] <= config.BUDGET_CAP and r["miles"] <= config.MILEAGE_HARD_CAP for r in lb)
    assert all(r["dealer_state"] in config.ALLOWED_STATES for r in lb)
    # IMV from the CarGurus row was attached to the merged MarketCheck record
    q7_cheap = next(r for r in lb if r["vin"] == "WA1LXBF7000000007")
    assert q7_cheap["comparison_basis"] == "imv" and "VERIFY_PRICE" in q7_cheap["flags"]
    # Mazda Preferred (below min trim) and VW SE excluded; Preferred Plus kept
    assert "JM3KKDHA000000003" not in vins and "JM3KKDHA000000001" in vins
    assert "1V2BR2CA000000003" not in vins and "1V2BR2CA000000001" in vins
    # observations stored per source for the same VIN
    n = db.query("SELECT COUNT(*) AS n FROM observations WHERE vin='WA1LXBF7000000001'")[0]["n"]
    assert n == 2   # marketcheck + cargurus rows
    assert db.price_drops_for_run(run1) == []

    # second run: same data but one car got cheaper
    data = json.load(open(FIXTURE))
    for row in data["marketcheck"]:
        if row["vin"] == "WA1LXBF7000000004":
            row["price"] = 42995      # was 44995
    f2 = tmp_path / "f2.json"
    f2.write_text(json.dumps(data))
    run2 = run_pipeline(db, ["fixture"], {}, fixture_path=str(f2))
    drops = db.price_drops_for_run(run2)
    assert [d["vin"] for d in drops] == ["WA1LXBF7000000004"]
    assert drops[0]["change_amount"] == -2000 and drops[0]["prev_run_id"] == run1
    s = db.query("SELECT * FROM scores WHERE run_id=? AND vin='WA1LXBF7000000004'", (run2,))[0]
    assert "PRICE_DROP" in s["flags"] and s["price_drop_amount"] == 2000

    # IMV reuse: the CarGurus rows are still in the fixture, but check the vehicles table persisted it
    v = db.query("SELECT cargurus_imv FROM vehicles WHERE vin='WA1LXBF7000000001'")[0]
    assert v["cargurus_imv"] == 49800

    # views are queryable
    assert db.query("SELECT * FROM v_price_changes WHERE change_amount < 0")[0]["vin"] == "WA1LXBF7000000004"
    out = build_report(db, run2, top_n=25)
    html = open(out).read()
    assert "Verify this" in html and "Price drops since the previous run" in html
    assert "$44,995" in html and "$42,995" in html            # was / now in the drops table
    assert "2023 Audi Q7" in html
    # trim tiers stored and rendered; every row has a VIN-search link even without a listing URL
    tiers = {r["vin"]: r["trim_tier"] for r in db.leaderboard(run2, 100)}
    assert tiers["WA1LXBF7000000007"] == "medium" and tiers["JM3KKDHA000000001"] == "medium"
    assert tiers["5UXCR6C0000000003"] == "low" and tiers["YV4062PE000000004"] == "high"
    # Links point at real listing pages, never at a search engine.
    assert "google.com/search" not in html and "bing.com" not in html
    assert ">Dealer page<" in html and ">CarGurus<" in html
    links = db.listing_links(run2)
    # a VIN seen by MarketCheck and CarGurus carries both direct links
    both = {src for src, _url, _home in links["WA1LXBF7000000001"]}
    assert both == {"marketcheck", "cargurus_apify"}
    assert all(url.startswith("http") for entries in links.values() for _s, url, _h in entries)
    # the fixture listing with no URL and no dealer site falls back to a plain note
    assert links.get("KMUHBDSB000000001") in (None, [])
    assert "no direct link" in open(build_report(db, run2, top_n=100)).read()
    # dealer home page is used only when that source gave no vehicle page
    db.conn.execute("UPDATE observations SET listing_url=NULL, dealer_website='https://example-dealer.com' "
                    "WHERE vin='WA1LXBF7000000002'")
    db.conn.commit()
    entries = db.listing_links(run2)["WA1LXBF7000000002"]
    assert entries and all(is_home for _s, _u, is_home in entries)
    assert "site</a>" in open(build_report(db, run2, top_n=25)).read()
