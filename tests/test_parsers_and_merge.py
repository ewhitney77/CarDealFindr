import json
import os

from cardealfindr.models import Listing, clean_vin, normalise_condition, to_int
from cardealfindr.pipeline import merge_by_vin
from cardealfindr.sources.autodev import AutoDevSource
from cardealfindr.sources.cargurus_apify import CarGurusApifySource
from cardealfindr.sources.dealers import DealerGroupSource
from cardealfindr.sources.marketcheck import MarketCheckSource

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_listings.json")


def test_helpers():
    assert to_int("$41,995") == 41995 and to_int("12,345 mi") == 12345 and to_int(None) is None
    assert clean_vin(" wa1lxbf70pd000001 ") == "WA1LXBF70PD000001"
    assert clean_vin("WA1LXBF7OPD000001") is None            # letter O is not allowed
    assert normalise_condition("used", 1) == "cpo"
    assert normalise_condition("Certified Pre-Owned") == "cpo"
    assert normalise_condition("new") == "new" and normalise_condition("used") == "used"


def test_marketcheck_parse():
    raw = json.load(open(FIXTURE))["marketcheck"][1]        # the Q7 with a ref_price
    l = MarketCheckSource.parse_listing(raw)
    assert l.vin == raw["vin"] and l.make == "Audi" and l.model == "Q7" and l.year == 2023
    assert l.price == 46500 and l.miles == 22100 and l.source_prior_price == 48900
    assert l.source_price_change_pct < 0
    assert l.dealer_state == "RI" and l.dealer_lat and l.distance_miles is not None
    assert l.days_on_market >= 70 and l.listing_url.startswith("https://")
    assert l.condition == "used"


def test_apify_parse_with_candidate_keys():
    raw = {"VIN": "WA1LXBF70PD000001", "Year": 2023, "Make": "Audi", "Model": "Q7", "Price ($)": "$47,995",
           "CarGurus IMV ($)": "49,800", "Deal Rating": "Good Deal", "Mileage": "18,200", "Days on Market": 34,
           "Dealer": "Audi Norwood", "City": "Norwood", "State": "ma", "Listing URL": "https://cg/1"}
    l = CarGurusApifySource.parse_item(raw, {"make": "Audi", "model": "Q7", "condition": "used"})
    assert l.price == 47995 and l.imv == 49800 and l.deal_rating == "Good Deal"
    assert l.miles == 18200 and l.dealer_state == "MA" and l.condition == "used"


def test_autodev_parse_nested_and_flat():
    nested = json.load(open(FIXTURE))["autodev"][0]
    l = AutoDevSource.parse_record(nested)
    assert l.vin == nested["vehicle"]["vin"] and l.price == 43500 and l.miles == 24900
    assert l.condition == "used" and l.dealer_name == "Genesis of Warwick" and l.dealer_state == "RI"
    flat = {"vin": "5UXCR6C05N9000001", "year": 2022, "make": "BMW", "model": "X5", "price": "$47,500",
            "mileage": "23,400", "city": "Boston", "state": "MA", "clickoffUrl": "https://x/1", "lat": 42.36, "lon": -71.13}
    l2 = AutoDevSource.parse_record(flat, {"make": "BMW", "model": "X5", "condition": "used"})
    assert l2.price == 47500 and l2.miles == 23400 and l2.listing_url == "https://x/1"
    assert l2.distance_miles is not None and l2.distance_miles < 40


def test_dealer_jsonld_parse():
    html = """<html><body>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
      {"@type":"ListItem","position":1,"item":{"@type":"Car","name":"2023 Audi Q7 Premium Plus",
        "vehicleIdentificationNumber":"WA1LXBF70PD000077","vehicleModelDate":"2023","brand":{"@type":"Brand","name":"Audi"},
        "model":"Q7","vehicleConfiguration":"Premium Plus","mileageFromOdometer":{"@type":"QuantitativeValue","value":"19,850"},
        "itemCondition":"https://schema.org/UsedCondition","color":"Black",
        "offers":{"@type":"Offer","price":"46995","priceCurrency":"USD","url":"/used/Audi/2023-Audi-Q7.htm",
                  "seller":{"@type":"AutoDealer","name":"Audi Burlington","address":{"addressLocality":"Burlington","addressRegion":"MA","postalCode":"01803"}}}}}]}
    </script></body></html>"""
    src = DealerGroupSource(http=None, group={"key": "hc", "name": "Herb Chambers", "state": "MA",
                                              "base_url": "https://www.herbchambers.com", "platform": "generic",
                                              "used_path": "/u", "new_path": "/n"})
    raws = src._jsonld_vehicles(html)
    assert len(raws) == 1
    l = src._to_listing(raws[0], {"make": "Audi", "model": "Q7", "condition": "used"}, "https://www.herbchambers.com/u")
    assert l.vin == "WA1LXBF70PD000077" and l.price == 46995 and l.miles == 19850 and l.year == 2023
    assert l.trim == "Premium Plus" and l.condition == "used" and l.dealer_city == "Burlington"
    assert l.listing_url == "https://www.herbchambers.com/used/Audi/2023-Audi-Q7.htm"


def test_dealer_data_vin_cards():
    html = '<div class="vehicle" data-vin="WA1LXBF70PD000078" data-year="2022" data-make="Audi" data-model="Q7" ' \
           'data-trim="Premium" data-price="43900" data-mileage="27,000" data-type="used"><a href="/vdp/78">x</a></div>'
    raws = DealerGroupSource._data_vin_cards(html, "https://www.iramotorgroup.com/used-inventory/")
    assert raws[0]["vin"] == "WA1LXBF70PD000078" and raws[0]["price"] == "43900"
    assert raws[0]["url"] == "https://www.iramotorgroup.com/vdp/78"


def test_merge_by_vin_priority_and_gap_fill():
    a = Listing(vin="V", source="autodev", price=44000, miles=20000, trim="Premium", dealer_city="Warwick")
    m = Listing(vin="V", source="marketcheck", price=45000, miles=None, days_on_market=40)
    c = Listing(vin="V", source="cargurus_apify", price=45500, miles=20100, imv=48000, deal_rating="Good Deal")
    merged = merge_by_vin([a, m, c])
    assert len(merged) == 1
    x = merged[0]
    assert x.source == "marketcheck" and x.price == 45000            # highest priority wins
    assert x.miles == 20100 and x.imv == 48000 and x.trim == "Premium" and x.dealer_city == "Warwick"
    assert x.raw["sources"] == ["marketcheck", "cargurus_apify", "autodev"]
