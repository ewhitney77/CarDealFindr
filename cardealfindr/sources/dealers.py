"""
Direct scraping of New England dealer-group websites (source #4).

ONLY the groups in config.DEALER_GROUPS are touched. These are sites that,
at the time of writing, do not sit behind aggressive bot protection. We do
NOT scrape Cars.com, CarGurus, Autotrader, TrueCar or CarMax - they will
break, and CarGurus is covered by the Apify actor instead.

Manners:
  * robots.txt is fetched and honoured (urllib.robotparser) per site
  * a real browser User-Agent (config.DEALER_USER_AGENT)
  * DEALER_REQUEST_DELAY_SECONDS between page fetches (default 2s)
  * every page is cached in SQLite for DEALER_CACHE_HOURS

Parsing strategies, tried in order until one yields vehicles:
  1. Dealer.com JSON inventory widget (platform == "dealer.com")
       GET {base}/apis/widget/INVENTORY_LISTING_DEFAULT_AUTO_USED:inventory-data-bus1/getInventory?make=..&model=..
  2. JSON-LD blocks in the HTML (<script type="application/ld+json"> with
     @type Car / Vehicle / Product) - most dealer platforms emit these
  3. Elements carrying a data-vin attribute plus sibling data-* attributes

IMPORTANT: these scrapers were written against the platforms' documented
page structures, not against a live fetch of each site, so treat the first
run as a smoke test. `run --debug-dealers` writes every fetched page to
cache/dealer_debug/ so you can see exactly what came back.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.robotparser
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config
from ..geo import distance_from_home
from ..http import CachedHttp, HttpError
from ..models import (Listing, clean_vin, first_present, normalise_condition,
                      to_int, CONDITION_NEW, CONDITION_USED)

log = logging.getLogger(__name__)


class DealerGroupSource:
    """One instance per dealer group. `name` is "dealer:<key>"."""

    def __init__(self, http: CachedHttp, group: dict, debug: bool = False):
        self.http = http
        self.group = group
        self.name = f"dealer:{group['key']}"
        self.debug = debug
        self._robots: Optional[urllib.robotparser.RobotFileParser] = None
        self._robots_loaded = False

    # ------------------------------------------------------------- robots.txt
    def _allowed(self, url: str) -> bool:
        if not self._robots_loaded:
            self._robots_loaded = True
            robots_url = urljoin(self.group["base_url"], "/robots.txt")
            try:
                text = self.http.get_text(robots_url, ttl_hours=24 * 7,
                                          min_interval=config.DEALER_REQUEST_DELAY_SECONDS,
                                          max_retries=1, timeout=20)
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(text.splitlines())
                self._robots = rp
            except HttpError as exc:
                log.info("%s: no robots.txt readable (%s); proceeding", self.name, exc)
                self._robots = None
        if self._robots is None:
            return True
        return self._robots.can_fetch(config.DEALER_USER_AGENT, url)

    # ------------------------------------------------------------------ fetch
    def _fetch(self, url: str) -> Optional[str]:
        if not self._allowed(url):
            log.warning("%s: robots.txt disallows %s - skipping", self.name, url)
            return None
        try:
            text = self.http.get_text(url, headers={"User-Agent": config.DEALER_USER_AGENT,
                                                    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
                                      ttl_hours=config.DEALER_CACHE_HOURS,
                                      min_interval=config.DEALER_REQUEST_DELAY_SECONDS,
                                      max_retries=2, timeout=40)
        except HttpError as exc:
            log.warning("%s: %s", self.name, exc)
            return None
        if self.debug:
            os.makedirs(os.path.join(config.CACHE_DIR, "dealer_debug"), exist_ok=True)
            fname = re.sub(r"[^A-Za-z0-9]+", "_", url.replace(self.group["base_url"], ""))[:120]
            path = os.path.join(config.CACHE_DIR, "dealer_debug", f"{self.group['key']}{fname}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            log.info("%s: saved %s", self.name, path)
        return text

    def fetch_target(self, target: dict) -> list[Listing]:
        is_new = target["condition"] == CONDITION_NEW
        path_tpl = self.group["new_path"] if is_new else self.group["used_path"]
        page_url = urljoin(self.group["base_url"], path_tpl.format(make=target["make"], model=target["model"]))
        raws: list[dict] = []

        if self.group.get("platform") == "dealer.com":
            raws = self._dealer_com_json(target, is_new)
        if not raws:
            html = self._fetch(page_url)
            if html:
                raws = self._jsonld_vehicles(html) or self._data_vin_cards(html, page_url)
        out = []
        for raw in raws:
            lst = self._to_listing(raw, target, page_url)
            if lst:
                out.append(lst)
        log.info("%s %s %s %s: %d listings", self.name, target["make"], target["model"],
                 target["condition"], len(out))
        return out

    # ------------------------------------------------- strategy 1: dealer.com
    def _dealer_com_json(self, target: dict, is_new: bool) -> list[dict]:
        kind = "NEW" if is_new else "USED"
        url = urljoin(self.group["base_url"],
                      f"/apis/widget/INVENTORY_LISTING_DEFAULT_AUTO_{kind}:inventory-data-bus1/getInventory")
        full = f"{url}?make={target['make']}&model={target['model']}&start=0&pageSize=100"
        text = self._fetch(full)
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        items = data.get("inventory") or data.get("vehicles") or []
        out = []
        for it in items:
            pricing = it.get("pricing") or {}
            price = None
            for k in ("dPrice", "dprice", "internetPrice", "salePrice", "sellingPrice", "retailPrice", "msrp"):
                price = to_int(pricing.get(k)) if isinstance(pricing, dict) else None
                if price:
                    break
            if price is None:
                price = to_int(first_present(it, "price", "internetPrice", "salePrice"))
            out.append({
                "vin": it.get("vin"), "year": it.get("year"), "make": it.get("make"),
                "model": it.get("model"), "trim": it.get("trim"), "price": price,
                "miles": it.get("odometer") or it.get("mileage"),
                "url": it.get("link") or it.get("vdpUrl"),
                "condition": "new" if is_new else it.get("type") or it.get("condition") or "used",
                "certified": it.get("certified"),
                "city": first_present(it, "address.city", "dealer.city", "city"),
                "state": first_present(it, "address.state", "dealer.state", "state"),
                "zip": first_present(it, "address.postalCode", "dealer.zip", "zip"),
                "dealer": first_present(it, "dealer.name", "dealerName", "accountName"),
                "exterior_color": it.get("exteriorColor"), "_raw": it,
            })
        return out

    # ---------------------------------------------------- strategy 2: JSON-LD
    @staticmethod
    def _jsonld_vehicles(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        found: list[dict] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for n in node:
                    walk(n)
            elif isinstance(node, dict):
                t = node.get("@type")
                types = {t} if isinstance(t, str) else set(t or [])
                if types & {"Car", "Vehicle", "Product"} and (
                        node.get("vehicleIdentificationNumber") or node.get("vin") or node.get("sku")):
                    found.append(node)
                for k in ("@graph", "itemListElement", "mainEntity", "item", "offers", "hasVariant"):
                    if k in node:
                        walk(node[k])

        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                walk(json.loads(tag.string or ""))
            except (json.JSONDecodeError, TypeError):
                continue

        out = []
        for n in found:
            offers = n.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            seller = offers.get("seller") or {}
            addr = seller.get("address") or {}
            odo = n.get("mileageFromOdometer")
            miles = odo.get("value") if isinstance(odo, dict) else odo
            brand = n.get("brand") or n.get("manufacturer")
            out.append({
                "vin": n.get("vehicleIdentificationNumber") or n.get("vin") or n.get("sku"),
                "year": n.get("vehicleModelDate") or n.get("modelDate") or n.get("productionDate"),
                "make": brand.get("name") if isinstance(brand, dict) else brand,
                "model": n.get("model"), "trim": n.get("vehicleConfiguration") or n.get("trim"),
                "price": offers.get("price") or n.get("price"),
                "miles": miles,
                "url": offers.get("url") or n.get("url"),
                "condition": (n.get("itemCondition") or offers.get("itemCondition") or ""),
                "dealer": seller.get("name"),
                "city": addr.get("addressLocality"), "state": addr.get("addressRegion"),
                "zip": addr.get("postalCode"),
                "exterior_color": n.get("color"), "_raw": n,
            })
        return out

    # ------------------------------------------- strategy 3: data-vin cards
    @staticmethod
    def _data_vin_cards(html: str, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for el in soup.find_all(attrs={"data-vin": True}):
            attrs = {k[5:]: v for k, v in el.attrs.items() if k.startswith("data-")}
            link = el.find("a", href=True)
            out.append({
                "vin": attrs.get("vin"),
                "year": first_present(attrs, "year", "model-year", "modelyear"),
                "make": first_present(attrs, "make"), "model": first_present(attrs, "model"),
                "trim": first_present(attrs, "trim"),
                "price": first_present(attrs, "price", "internet-price", "sale-price", "final-price"),
                "miles": first_present(attrs, "mileage", "miles", "odometer"),
                "url": urljoin(page_url, link["href"]) if link else None,
                "condition": first_present(attrs, "type", "condition", "vehicle-type", "new-used") or "",
                "dealer": first_present(attrs, "dealer", "dealer-name", "dealership"),
                "city": first_present(attrs, "city"), "state": first_present(attrs, "state"),
                "zip": first_present(attrs, "zip", "postal-code"),
                "exterior_color": first_present(attrs, "exterior-color", "color"),
                "_raw": attrs,
            })
        return out

    # -------------------------------------------------------------- normalise
    def _to_listing(self, raw: dict, target: dict, page_url: str) -> Optional[Listing]:
        vin = clean_vin(raw.get("vin"))
        if not vin:
            return None
        cond = normalise_condition(raw.get("condition"), raw.get("certified"))
        if cond is None:
            cond = CONDITION_NEW if target["condition"] == CONDITION_NEW else CONDITION_USED
        url = raw.get("url")
        if url and not str(url).startswith("http"):
            url = urljoin(self.group["base_url"], str(url))
        state = (raw.get("state") or self.group.get("state") or "").upper()[:2] or None
        zip_code = str(raw.get("zip")) if raw.get("zip") else None
        return Listing(
            vin=vin, source=self.name,
            year=to_int(raw.get("year")),
            make=raw.get("make") or target["make"], model=raw.get("model") or target["model"],
            trim=raw.get("trim"), condition=cond,
            exterior_color=raw.get("exterior_color"),
            price=to_int(raw.get("price")), miles=to_int(raw.get("miles")),
            dealer_name=raw.get("dealer") or self.group["name"],
            dealer_city=raw.get("city"), dealer_state=state, dealer_zip=zip_code,
            distance_miles=distance_from_home(None, None, zip_code),
            listing_url=url or page_url,
            raw=raw.get("_raw") or raw,
        )
