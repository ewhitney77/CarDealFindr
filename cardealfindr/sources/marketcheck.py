"""
MarketCheck - primary source.

Endpoint: GET https://api.marketcheck.com/v2/search/car/active
Docs:     https://docs.marketcheck.com/docs/api/cars/inventory/inventory-search
Auth:     ?api_key=...   (MARKETCHECK_API_KEY in .env)
Quota:    free plan = 500 calls/month, 5 calls/second. Every page is one call.
          Responses are cached for MARKETCHECK_CACHE_HOURS so re-running the
          tool while tuning scoring does not spend quota.

One "target" (make + model + condition) = 1..MARKETCHECK_MAX_PAGES_PER_TARGET
calls, 50 listings per call. With the default 11 targets that is roughly
15-30 calls per run.

Fields we pull from each listing (see parse_listing):
  price, msrp, miles, vin, build.trim, dom / dom_active / first_seen_at,
  dealer.name/city/state/zip/latitude/longitude, dist, vdp_url,
  ref_price + price_change_percent (the source's own price-change signal).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import config
from ..geo import distance_from_home
from ..http import CachedHttp, HttpError
from ..models import (Listing, clean_vin, first_present, normalise_condition,
                      to_float, to_int, CONDITION_NEW)

log = logging.getLogger(__name__)


class MarketCheckSource:
    name = "marketcheck"

    def __init__(self, http: CachedHttp, api_key: str):
        if not api_key:
            raise ValueError("MARKETCHECK_API_KEY is not set (put it in .env)")
        self.http = http
        self.api_key = api_key
        self.calls_made = 0

    # ---------------------------------------------------------------- search
    def _base_params(self, target: dict) -> dict[str, Any]:
        p: dict[str, Any] = {
            "api_key": self.api_key,
            "zip": config.HOME_ZIP,
            "radius": config.SEARCH_RADIUS_MILES,
            "make": target["make"],
            "model": target["model"],
            "price_range": f"1-{config.BUDGET_CAP}",
            "miles_range": f"0-{config.MILEAGE_HARD_CAP}",
            "rows": config.MARKETCHECK_ROWS_PER_PAGE,
            "sort_by": "dist",
            "sort_order": "asc",
        }
        if getattr(config, "MARKETCHECK_PASS_STATE_FILTER", True):
            p["state"] = ",".join(sorted(config.ALLOWED_STATES))
        if target["condition"] == CONDITION_NEW:
            p["car_type"] = "new"
        else:
            p["car_type"] = ",".join(config.MARKETCHECK_USED_CAR_TYPES)
            p["year"] = ",".join(str(y) for y in range(config.USED_YEAR_MIN, config.USED_YEAR_MAX + 1))
        return p

    def fetch_target(self, target: dict) -> list[Listing]:
        """All pages for one make/model/condition, parsed into Listings."""
        url = f"{config.MARKETCHECK_BASE_URL}/search/car/active"
        params = self._base_params(target)
        out: list[Listing] = []
        start = 0
        for page in range(config.MARKETCHECK_MAX_PAGES_PER_TARGET):
            params["start"] = start
            try:
                status, text, cached = self.http.request(
                    "GET", url, params=params,
                    ttl_hours=config.MARKETCHECK_CACHE_HOURS,
                    min_interval=config.MARKETCHECK_MIN_SECONDS_BETWEEN_CALLS,
                    max_retries=config.MARKETCHECK_MAX_RETRIES)
            except HttpError as exc:
                log.error("MarketCheck %s %s (%s): %s", target["make"], target["model"],
                          target["condition"], exc)
                break
            if not cached:
                self.calls_made += 1
            import json
            data = json.loads(text)
            listings = data.get("listings") or []
            num_found = int(data.get("num_found") or 0)
            for raw in listings:
                lst = self.parse_listing(raw)
                if lst:
                    out.append(lst)
            start += len(listings)
            log.info("MarketCheck %-11s %-8s %-4s page %d: %d of %d%s",
                     target["make"], target["model"], target["condition"], page + 1,
                     start, num_found, " (cached)" if cached else "")
            if not listings or start >= num_found:
                break
        return out

    # ----------------------------------------------------------------- parse
    @staticmethod
    def parse_listing(raw: dict) -> Optional[Listing]:
        vin = clean_vin(raw.get("vin"))
        if not vin:
            return None
        build = raw.get("build") or {}
        dealer = raw.get("dealer") or {}

        # Days on market: prefer the age of THIS listing (now - first_seen_at).
        # MarketCheck's `dom` is lifetime across every listing of the VIN,
        # which can include a previous owner's dealer; `dom_active` is closer.
        dom: Optional[int] = None
        first_seen_iso: Optional[str] = None
        fs = raw.get("first_seen_at")
        if fs:
            try:
                fs_dt = datetime.fromtimestamp(float(fs), tz=timezone.utc)
                first_seen_iso = fs_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                dom = max(0, (datetime.now(timezone.utc) - fs_dt).days)
            except (TypeError, ValueError, OSError):
                pass
        if dom is None:
            dom = to_int(first_present(raw, "dom_active", "dom_180", "dom"))
        if first_seen_iso is None and raw.get("first_seen_at_date"):
            first_seen_iso = str(raw["first_seen_at_date"])

        lat = to_float(dealer.get("latitude"))
        lon = to_float(dealer.get("longitude"))
        dist = to_float(raw.get("dist"))
        if dist is None:
            dist = distance_from_home(lat, lon, dealer.get("zip"))

        return Listing(
            vin=vin, source=MarketCheckSource.name,
            year=to_int(build.get("year")), make=build.get("make"), model=build.get("model"),
            trim=build.get("trim"),
            condition=normalise_condition(raw.get("inventory_type"), raw.get("is_certified")),
            body_type=build.get("body_type"), drivetrain=build.get("drivetrain"),
            exterior_color=raw.get("exterior_color"), interior_color=raw.get("interior_color"),
            price=to_int(raw.get("price")), msrp=to_int(raw.get("msrp")), miles=to_int(raw.get("miles")),
            days_on_market=dom,
            source_prior_price=to_int(raw.get("ref_price")),
            source_price_change_pct=to_float(raw.get("price_change_percent")),
            first_seen_at=first_seen_iso,
            dealer_name=dealer.get("name"), dealer_city=dealer.get("city"),
            dealer_state=(dealer.get("state") or "").upper() or None, dealer_zip=dealer.get("zip"),
            dealer_lat=lat, dealer_lon=lon, distance_miles=dist,
            listing_url=raw.get("vdp_url"), source_listing_id=raw.get("id"),
            raw=raw,
        )

    # --------------------------------------------------------------- history
    def fetch_history(self, vin: str) -> list[dict]:
        """
        History-by-VIN: every past listing of this VIN MarketCheck knows about.
        ONE API CALL per VIN - only called for the top-N when
        MARKETCHECK_HISTORY_TOP_N > 0.
        """
        url = f"{config.MARKETCHECK_BASE_URL}/history/car/{vin}"
        params = {
            "api_key": self.api_key,
            "fields": "id,price,miles,seller_name,city,state,first_seen_at_date,last_seen_at_date",
            "page": 1, "sort_order": "desc",
        }
        try:
            status, text, cached = self.http.request(
                "GET", url, params=params, ttl_hours=24 * 3,
                min_interval=config.MARKETCHECK_MIN_SECONDS_BETWEEN_CALLS,
                max_retries=config.MARKETCHECK_MAX_RETRIES)
        except HttpError as exc:
            log.warning("MarketCheck history %s: %s", vin, exc)
            return []
        if not cached:
            self.calls_made += 1
        import json
        data = json.loads(text)
        rows = data if isinstance(data, list) else (data.get("history") or data.get("listings") or [])
        out = []
        for r in rows:
            out.append({
                "vin": vin, "source": self.name, "listing_id": str(r.get("id") or ""),
                "price": to_int(r.get("price")), "miles": to_int(r.get("miles")),
                "seller_name": r.get("seller_name"), "city": r.get("city"), "state": r.get("state"),
                "first_seen_date": r.get("first_seen_at_date"), "last_seen_date": r.get("last_seen_at_date"),
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        return [o for o in out if o["listing_id"]]
