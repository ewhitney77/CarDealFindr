"""
CarGurus via the Apify actor "ahmed_jasarevic/cargurus-scraper" - secondary
source, BILLED PER LISTING RETURNED.

Why we bother: CarGurus publishes an Instant Market Value (IMV) and a deal
rating for every listing. Price-vs-IMV is a better "is this underpriced"
signal than anything we can compute from our own small result set, so when a
listing has an IMV the scorer uses it instead of the peer median.

How it is called (plain REST, no SDK needed):
  1. POST https://api.apify.com/v2/acts/<actor>/runs?token=...   with the input JSON
  2. poll GET https://api.apify.com/v2/actor-runs/<runId>?token=... until SUCCEEDED
  3. GET https://api.apify.com/v2/datasets/<datasetId>/items?token=...&clean=true

Cost control:
  * APIFY_ENABLED=False           -> this module is never called
  * APIFY_MAX_LISTINGS_PER_TARGET -> caps the bill per make/model
  * APIFY_CACHE_DAYS              -> identical input within N days reuses the
                                     cached dataset instead of a new paid run
  * every IMV we ever see is written to vehicles.cargurus_imv so it keeps
    feeding the score for IMV_MAX_AGE_DAYS even with Apify switched off.

Field names: the actor's input and output keys are configured in
config.APIFY_INPUT_FIELDS / the CANDIDATE_* lists below, because third-party
actors rename fields from time to time. If a run "succeeds" but yields 0
parsed listings, print one raw item (`--debug`) and fix the candidate lists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import config
from ..geo import distance_from_home
from ..http import CachedHttp, HttpError
from ..models import (Listing, clean_vin, first_present, normalise_condition,
                      to_float, to_int, CONDITION_NEW, CONDITION_USED, CONDITION_CPO)

log = logging.getLogger(__name__)

APIFY_API = "https://api.apify.com/v2"

# Output-field candidates, most likely first. first_present() walks these.
CANDIDATE_VIN = ("vin", "VIN", "vehicle.vin")
CANDIDATE_PRICE = ("price", "listPrice", "priceAmount", "Price ($)", "price.amount")
CANDIDATE_IMV = ("imv", "instantMarketValue", "IMV", "CarGurus IMV ($)", "expectedPrice",
                 "marketValue", "instant_market_value")
CANDIDATE_RATING = ("dealRating", "deal_rating", "Deal Rating", "dealRatingLabel", "rating")
CANDIDATE_MILES = ("mileage", "miles", "Mileage", "odometer")
CANDIDATE_YEAR = ("year", "Year", "modelYear", "vehicle.year")
CANDIDATE_MAKE = ("make", "Make", "makeName", "vehicle.make")
CANDIDATE_MODEL = ("model", "Model", "modelName", "vehicle.model")
CANDIDATE_TRIM = ("trim", "Trim", "trimName", "vehicle.trim")
CANDIDATE_DOM = ("daysOnMarket", "days_on_market", "Days on Market", "daysOnCarGurus", "daysListed")
CANDIDATE_DEALER = ("dealerName", "dealer.name", "sellerName", "Dealer", "seller.name", "dealer")
CANDIDATE_CITY = ("dealerCity", "dealer.city", "city", "City", "seller.city")
CANDIDATE_STATE = ("dealerState", "dealer.state", "state", "State", "seller.state", "stateCode")
CANDIDATE_ZIP = ("dealerZip", "dealer.zip", "zip", "postalCode", "seller.zip")
CANDIDATE_URL = ("url", "listingUrl", "Listing URL", "link", "vdpUrl")
CANDIDATE_ID = ("id", "listingId", "listing_id")
CANDIDATE_DISTANCE = ("distance", "distanceMiles", "distanceFromZip")
CANDIDATE_CONDITION = ("condition", "inventoryType", "listingType", "vehicleCondition")
CANDIDATE_CPO = ("isCpo", "cpo", "certified", "isCertified")
CANDIDATE_BODY = ("bodyType", "bodyStyle", "vehicle.bodyType")
CANDIDATE_EXT_COLOR = ("exteriorColor", "color", "exterior_color")
CANDIDATE_INT_COLOR = ("interiorColor", "interior_color")


class CarGurusApifySource:
    name = "cargurus_apify"

    def __init__(self, http: CachedHttp, token: str, db):
        if not token:
            raise ValueError("APIFY_TOKEN is not set (put it in .env)")
        self.http = http
        self.token = token
        self.db = db
        self.runs_started = 0

    # ----------------------------------------------------------------- input
    def build_input(self, target: dict) -> dict[str, Any]:
        f = config.APIFY_INPUT_FIELDS
        inp: dict[str, Any] = dict(config.APIFY_EXTRA_INPUT)
        inp[f["zip"]] = config.HOME_ZIP
        inp[f["radius"]] = config.SEARCH_RADIUS_MILES
        inp[f["make"]] = target["make"]
        inp[f["model"]] = target["model"]
        cond = "new" if target["condition"] == CONDITION_NEW else "used"
        inp[f["condition"]] = config.APIFY_CONDITION_VALUES[cond]
        inp[f["max_items"]] = config.APIFY_MAX_LISTINGS_PER_TARGET
        return inp

    # ------------------------------------------------------------------- run
    def _run_actor(self, actor_input: dict) -> list[dict]:
        """Start the actor, wait for it, return the dataset items (list of dicts)."""
        actor = config.APIFY_ACTOR_ID.replace("/", "~")
        start = self.http.post_json(
            f"{APIFY_API}/acts/{actor}/runs", actor_input,
            params={"token": self.token}, ttl_hours=0, max_retries=3, timeout=60)
        run = start.get("data") or {}
        run_id, dataset_id = run.get("id"), run.get("defaultDatasetId")
        if not run_id:
            raise HttpError(f"Apify did not return a run id: {json.dumps(start)[:300]}")
        self.runs_started += 1
        log.info("Apify run %s started (%s)", run_id, json.dumps(actor_input))

        deadline = time.monotonic() + config.APIFY_RUN_TIMEOUT_SECONDS
        status = run.get("status")
        while status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            if time.monotonic() > deadline:
                raise HttpError(f"Apify run {run_id} still {status} after "
                                f"{config.APIFY_RUN_TIMEOUT_SECONDS}s; giving up")
            time.sleep(config.APIFY_POLL_SECONDS)
            info = self.http.get_json(f"{APIFY_API}/actor-runs/{run_id}",
                                      params={"token": self.token}, ttl_hours=0, max_retries=3)
            status = (info.get("data") or {}).get("status")
            dataset_id = (info.get("data") or {}).get("defaultDatasetId") or dataset_id
        if status != "SUCCEEDED":
            raise HttpError(f"Apify run {run_id} ended with status {status}")

        items = self.http.get_json(f"{APIFY_API}/datasets/{dataset_id}/items",
                                   params={"token": self.token, "clean": "true", "format": "json"},
                                   ttl_hours=0, max_retries=3, timeout=120)
        return items if isinstance(items, list) else []

    def fetch_target(self, target: dict) -> list[Listing]:
        actor_input = self.build_input(target)
        key = "apify:" + hashlib.sha256(
            json.dumps([config.APIFY_ACTOR_ID, actor_input], sort_keys=True).encode()).hexdigest()
        cached = self.db.cache_get(key) if self.http.use_cache else None
        if cached:
            items = json.loads(cached["body"])
            log.info("Apify %s %s %s: %d items (cached, no charge)", target["make"], target["model"],
                     target["condition"], len(items))
        else:
            try:
                items = self._run_actor(actor_input)
            except HttpError as exc:
                log.error("Apify %s %s: %s", target["make"], target["model"], exc)
                return []
            self.db.cache_put(key, f"apify://{config.APIFY_ACTOR_ID}", 200, json.dumps(items),
                              ttl_hours=config.APIFY_CACHE_DAYS * 24)
            log.info("Apify %s %s %s: %d items (billed)", target["make"], target["model"],
                     target["condition"], len(items))
        out = []
        for raw in items:
            lst = self.parse_item(raw, target)
            if lst:
                out.append(lst)
        if items and not out:
            log.warning("Apify returned %d items but none parsed. First item keys: %s "
                        "-> update CANDIDATE_* lists in cargurus_apify.py",
                        len(items), sorted(items[0].keys())[:40] if isinstance(items[0], dict) else type(items[0]))
        return out

    # ----------------------------------------------------------------- parse
    @staticmethod
    def parse_item(raw: dict, target: Optional[dict] = None) -> Optional[Listing]:
        if not isinstance(raw, dict):
            return None
        vin = clean_vin(first_present(raw, *CANDIDATE_VIN))
        if not vin:
            return None

        # Condition: explicit field, else CPO flag, else the target we asked for.
        cond = normalise_condition(first_present(raw, *CANDIDATE_CONDITION),
                                   first_present(raw, *CANDIDATE_CPO))
        if cond is None and target:
            cond = CONDITION_NEW if target["condition"] == CONDITION_NEW else CONDITION_USED

        dealer = first_present(raw, *CANDIDATE_DEALER)
        if isinstance(dealer, dict):
            dealer = dealer.get("name")
        state = first_present(raw, *CANDIDATE_STATE)
        zip_code = first_present(raw, *CANDIDATE_ZIP)
        distance = to_float(first_present(raw, *CANDIDATE_DISTANCE))
        if distance is None:
            distance = distance_from_home(None, None, zip_code)

        rating = first_present(raw, *CANDIDATE_RATING)
        return Listing(
            vin=vin, source=CarGurusApifySource.name,
            year=to_int(first_present(raw, *CANDIDATE_YEAR)),
            make=first_present(raw, *CANDIDATE_MAKE) or (target or {}).get("make"),
            model=first_present(raw, *CANDIDATE_MODEL) or (target or {}).get("model"),
            trim=first_present(raw, *CANDIDATE_TRIM),
            condition=cond,
            body_type=first_present(raw, *CANDIDATE_BODY),
            exterior_color=first_present(raw, *CANDIDATE_EXT_COLOR),
            interior_color=first_present(raw, *CANDIDATE_INT_COLOR),
            price=to_int(first_present(raw, *CANDIDATE_PRICE)),
            miles=to_int(first_present(raw, *CANDIDATE_MILES)),
            days_on_market=to_int(first_present(raw, *CANDIDATE_DOM)),
            imv=to_int(first_present(raw, *CANDIDATE_IMV)),
            deal_rating=str(rating) if rating is not None else None,
            dealer_name=str(dealer) if dealer else None,
            dealer_city=first_present(raw, *CANDIDATE_CITY),
            dealer_state=str(state).upper()[:2] if state else None,
            dealer_zip=str(zip_code) if zip_code else None,
            distance_miles=distance,
            listing_url=first_present(raw, *CANDIDATE_URL),
            source_listing_id=str(first_present(raw, *CANDIDATE_ID) or "") or None,
            raw=raw,
        )
