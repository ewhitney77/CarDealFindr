"""
Auto.dev - free cross-check source.

Endpoint: GET https://api.auto.dev/listings
Docs:     https://docs.auto.dev/v2/api-reference/vehicle-listings
Auth:     Authorization: Bearer <AUTODEV_API_KEY>
Quota:    free plan = 1,000 calls/month.

Filter syntax uses dotted field names and dash ranges, e.g.
  vehicle.make=Audi&vehicle.model=Q7&vehicle.year=2021-2024
  &retailListing.price=1-50000&retailListing.miles=0-40000&zip=02038&distance=200

When it is used: only for targets where MarketCheck returned fewer than
AUTODEV_THIN_COVERAGE_THRESHOLD listings (or when forced with
`--sources autodev`). Results are de-duplicated by VIN with everything else.

The response shape has changed between Auto.dev API versions, so parsing
uses candidate key lists (new nested v2 shape first, legacy flat v1 shape
second).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

import config
from ..geo import distance_from_home
from ..http import CachedHttp, HttpError
from ..models import (Listing, clean_vin, first_present, to_float, to_int,
                      CONDITION_NEW, CONDITION_USED, CONDITION_CPO)

log = logging.getLogger(__name__)


class AutoDevSource:
    name = "autodev"

    def __init__(self, http: CachedHttp, api_key: str):
        if not api_key:
            raise ValueError("AUTODEV_API_KEY is not set (put it in .env)")
        self.http = http
        self.api_key = api_key
        self.calls_made = 0

    def _params(self, target: dict, page: int) -> dict[str, Any]:
        p: dict[str, Any] = {
            "vehicle.make": target["make"],
            "vehicle.model": target["model"],
            "retailListing.price": f"1-{config.BUDGET_CAP}",
            "retailListing.miles": f"0-{config.MILEAGE_HARD_CAP}",
            "zip": config.HOME_ZIP,
            "distance": config.SEARCH_RADIUS_MILES,
            "page": page,
            "limit": config.AUTODEV_PAGE_SIZE,
        }
        if target["condition"] != CONDITION_NEW:
            p["vehicle.year"] = f"{config.USED_YEAR_MIN}-{config.USED_YEAR_MAX}"
        return p

    def fetch_target(self, target: dict) -> list[Listing]:
        url = f"{config.AUTODEV_BASE_URL}/listings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        out: list[Listing] = []
        for page in range(1, config.AUTODEV_MAX_PAGES_PER_TARGET + 1):
            try:
                status, text, cached = self.http.request(
                    "GET", url, params=self._params(target, page), headers=headers,
                    ttl_hours=config.AUTODEV_CACHE_HOURS,
                    min_interval=config.AUTODEV_MIN_SECONDS_BETWEEN_CALLS, max_retries=4)
            except HttpError as exc:
                log.error("Auto.dev %s %s: %s", target["make"], target["model"], exc)
                break
            if not cached:
                self.calls_made += 1
            import json
            data = json.loads(text)
            records = self._records(data)
            for raw in records:
                lst = self.parse_record(raw, target)
                if lst:
                    out.append(lst)
            log.info("Auto.dev %-11s %-8s %-4s page %d: %d records%s", target["make"],
                     target["model"], target["condition"], page, len(records),
                     " (cached)" if cached else "")
            if len(records) < config.AUTODEV_PAGE_SIZE:
                break
        return out

    @staticmethod
    def _records(data: Any) -> list[dict]:
        if isinstance(data, list):
            return data
        for key in ("data", "records", "listings", "results", "items"):
            v = data.get(key) if isinstance(data, dict) else None
            if isinstance(v, list):
                return v
        return []

    @staticmethod
    def parse_record(raw: dict, target: Optional[dict] = None) -> Optional[Listing]:
        vin = clean_vin(first_present(raw, "vehicle.vin", "vin"))
        if not vin:
            return None
        year = to_int(first_present(raw, "vehicle.year", "year"))
        miles = to_int(first_present(raw, "retailListing.miles", "retailListing.mileage",
                                     "mileage", "miles", "mileageUnformatted"))
        price = to_int(first_present(raw, "retailListing.price", "price", "priceUnformatted"))

        # Condition. Auto.dev is mostly used inventory; honour explicit flags,
        # else fall back to what we asked for, else infer from miles/year.
        used_flag = first_present(raw, "retailListing.used", "used")
        cpo_flag = first_present(raw, "retailListing.cpo", "retailListing.certified", "cpo", "certified")
        if str(cpo_flag).lower() in ("true", "1", "yes"):
            cond = CONDITION_CPO
        elif used_flag is not None:
            cond = CONDITION_USED if str(used_flag).lower() in ("true", "1", "yes") else CONDITION_NEW
        elif target:
            cond = CONDITION_NEW if target["condition"] == CONDITION_NEW else CONDITION_USED
        else:
            cond = CONDITION_NEW if (miles is not None and miles < 300 and year and year >= date.today().year) else CONDITION_USED

        lat = to_float(first_present(raw, "retailListing.lat", "retailListing.latitude", "lat", "latitude"))
        lon = to_float(first_present(raw, "retailListing.lon", "retailListing.longitude", "lon", "longitude"))
        zip_code = first_present(raw, "retailListing.zip", "retailListing.postalCode", "zip", "dealerZip")
        state = first_present(raw, "retailListing.state", "state", "dealerState")
        dealer = first_present(raw, "retailListing.dealer", "retailListing.dealerName", "dealerName", "dealer")
        if isinstance(dealer, dict):
            dealer = dealer.get("name")
        distance = to_float(first_present(raw, "retailListing.distance", "distance", "distanceFromOrigin"))
        if distance is None:
            distance = distance_from_home(lat, lon, str(zip_code) if zip_code else None)

        return Listing(
            vin=vin, source=AutoDevSource.name,
            year=year,
            make=first_present(raw, "vehicle.make", "make") or (target or {}).get("make"),
            model=first_present(raw, "vehicle.model", "model") or (target or {}).get("model"),
            trim=first_present(raw, "vehicle.trim", "trim"),
            condition=cond,
            body_type=first_present(raw, "vehicle.bodyType", "vehicle.bodyStyle", "bodyType", "bodyStyle"),
            drivetrain=first_present(raw, "vehicle.drivetrain", "vehicle.driveType", "drivetrain"),
            exterior_color=first_present(raw, "vehicle.exteriorColor", "retailListing.exteriorColor", "displayColor"),
            interior_color=first_present(raw, "vehicle.interiorColor", "retailListing.interiorColor"),
            price=price,
            msrp=to_int(first_present(raw, "retailListing.msrp", "msrp")),
            miles=miles,
            days_on_market=to_int(first_present(raw, "retailListing.daysOnMarket", "retailListing.dom",
                                                "daysOnMarket")),
            first_seen_at=first_present(raw, "retailListing.firstSeen", "retailListing.listedAt",
                                        "retailListing.createdAt", "createdAt"),
            dealer_name=str(dealer) if dealer else None,
            dealer_city=first_present(raw, "retailListing.city", "city", "dealerCity"),
            dealer_state=str(state).upper()[:2] if state else None,
            dealer_zip=str(zip_code) if zip_code else None,
            dealer_lat=lat, dealer_lon=lon,
            dealer_website=first_present(raw, "retailListing.dealerWebsite", "retailListing.website",
                                         "dealerWebsite"),
            distance_miles=distance,
            listing_url=first_present(raw, "retailListing.vdp", "retailListing.vdpUrl", "retailListing.url",
                                      "clickoffUrl", "vdpUrl", "url"),
            source_listing_id=str(first_present(raw, "retailListing.id", "id", "listingId") or "") or None,
            raw=raw,
        )
