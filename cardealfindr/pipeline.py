"""
The orchestration layer: one `run` = collect -> merge -> filter -> score -> persist.

    sources ──> raw Listings (many per VIN, one per source)
            ──> merge_by_vin()      one Listing per VIN, best fields from each source
            ──> apply_filters()     budget / miles / years / trims / states / radius
            ──> enrich with IMVs we already have on file
            ──> score_all()         0-100 + components + flags
            ──> SQLite              runs, vehicles, observations, scores
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from dataclasses import fields as dc_fields
from typing import Optional

import config
from .db import Database, utcnow
from .filters import apply_filters
from .http import CachedHttp
from .models import Listing
from .scoring import ScoreResult, score_all
from .sources.autodev import AutoDevSource
from .sources.cargurus_apify import CarGurusApifySource
from .sources.dealers import DealerGroupSource
from .sources.marketcheck import MarketCheckSource

log = logging.getLogger(__name__)

ALL_SOURCES = ["marketcheck", "cargurus_apify", "autodev", "dealers"]


def _priority(source: str) -> int:
    """Index into SOURCE_PRIORITY; 'dealer:<key>' collapses to 'dealer'."""
    base = "dealer" if source.startswith("dealer") else source
    try:
        return config.SOURCE_PRIORITY.index(base)
    except ValueError:
        return len(config.SOURCE_PRIORITY)


def merge_by_vin(listings: list[Listing]) -> list[Listing]:
    """
    Collapse many source rows per VIN into one Listing.
    The highest-priority source supplies each field; lower-priority sources
    only fill gaps. IMV / deal rating are taken from whichever source has them.
    """
    groups: dict[str, list[Listing]] = defaultdict(list)
    for l in listings:
        groups[l.vin].append(l)
    merged: list[Listing] = []
    for vin, rows in groups.items():
        rows.sort(key=lambda r: _priority(r.source))
        base = Listing(vin=vin, source=rows[0].source)
        for f in dc_fields(Listing):
            if f.name in ("vin", "source", "raw"):
                continue
            for r in rows:
                v = getattr(r, f.name)
                if v is not None:
                    setattr(base, f.name, v)
                    break
        base.raw = {"sources": [r.source for r in rows]}
        merged.append(base)
    return merged


def _load_fixture(path: str) -> list[Listing]:
    """Offline mode: parse a JSON file of raw source payloads instead of calling APIs."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out: list[Listing] = []
    for raw in data.get("marketcheck", []):
        l = MarketCheckSource.parse_listing(raw)
        if l:
            out.append(l)
    for raw in data.get("cargurus_apify", []):
        l = CarGurusApifySource.parse_item(raw)
        if l:
            out.append(l)
    for raw in data.get("autodev", []):
        l = AutoDevSource.parse_record(raw)
        if l:
            out.append(l)
    log.info("fixture %s: %d raw listings", path, len(out))
    return out


def collect(sources: list[str], db: Database, http: CachedHttp, keys: dict[str, str],
            debug_dealers: bool = False) -> tuple[list[Listing], dict]:
    """Query every enabled source for every target. Returns (raw listings, stats)."""
    raw: list[Listing] = []
    stats: dict = {"per_source": Counter(), "api_calls": {}, "errors": []}
    per_target_mc: Counter = Counter()

    mc: Optional[MarketCheckSource] = None
    if "marketcheck" in sources:
        try:
            mc = MarketCheckSource(http, keys.get("MARKETCHECK_API_KEY", ""))
        except ValueError as exc:
            log.error("%s", exc)
            stats["errors"].append(str(exc))
    if mc:
        for t in config.SEARCH_TARGETS:
            rows = mc.fetch_target(t)
            per_target_mc[(t["make"], t["model"], t["condition"])] = len(rows)
            raw.extend(rows)
        stats["api_calls"]["marketcheck"] = mc.calls_made
        stats["per_source"]["marketcheck"] = sum(per_target_mc.values())

    if "cargurus_apify" in sources:
        try:
            cg = CarGurusApifySource(http, keys.get("APIFY_TOKEN", ""), db)
            for t in config.SEARCH_TARGETS:
                rows = cg.fetch_target(t)
                stats["per_source"]["cargurus_apify"] += len(rows)
                raw.extend(rows)
            stats["api_calls"]["apify_runs"] = cg.runs_started
        except ValueError as exc:
            log.error("%s", exc)
            stats["errors"].append(str(exc))

    if "autodev" in sources:
        try:
            ad = AutoDevSource(http, keys.get("AUTODEV_API_KEY", ""))
            forced = mc is None                    # no MarketCheck at all -> use it for everything
            for t in config.SEARCH_TARGETS:
                n = per_target_mc.get((t["make"], t["model"], t["condition"]), 0)
                if forced or n < config.AUTODEV_THIN_COVERAGE_THRESHOLD:
                    log.info("Auto.dev cross-check for %s %s %s (MarketCheck had %d)",
                             t["make"], t["model"], t["condition"], n)
                    rows = ad.fetch_target(t)
                    stats["per_source"]["autodev"] += len(rows)
                    raw.extend(rows)
            stats["api_calls"]["autodev"] = ad.calls_made
        except ValueError as exc:
            log.error("%s", exc)
            stats["errors"].append(str(exc))

    if "dealers" in sources:
        dealer_http = CachedHttp(db, user_agent=config.DEALER_USER_AGENT, use_cache=http.use_cache)
        for g in config.DEALER_GROUPS:
            src = DealerGroupSource(dealer_http, g, debug=debug_dealers)
            for t in config.SEARCH_TARGETS:
                try:
                    rows = src.fetch_target(t)
                except Exception as exc:           # a broken site must never kill the run
                    log.warning("%s %s %s: %s", src.name, t["make"], t["model"], exc)
                    rows = []
                stats["per_source"][src.name] += len(rows)
                raw.extend(rows)
    return raw, stats


def run_pipeline(db: Database, sources: list[str], keys: dict[str, str], use_cache: bool = True,
                 debug_dealers: bool = False, fixture_path: Optional[str] = None) -> int:
    """Execute one full run and return its run_id."""
    http = CachedHttp(db, use_cache=use_cache)
    run_id = db.start_run(sources if not fixture_path else ["fixture"])
    observed_at = utcnow()
    log.info("run %d started", run_id)

    # 1. collect
    if fixture_path:
        raw, stats = _load_fixture(fixture_path), {"per_source": Counter(), "api_calls": {}, "errors": []}
        for l in raw:
            stats["per_source"][l.source] += 1
    else:
        raw, stats = collect(sources, db, http, keys, debug_dealers)
    log.info("collected %d raw listings: %s", len(raw), dict(stats["per_source"]))

    # 2. merge + 3. filter
    merged = merge_by_vin(raw)
    kept, rejections = apply_filters(merged)
    log.info("%d unique VINs -> %d eligible; rejections: %s", len(merged), len(kept), dict(rejections))
    kept_vins = {l.vin for l in kept}

    # 4. persist vehicles + every source observation for the eligible VINs
    for l in kept:
        db.upsert_vehicle(l, run_id, observed_at)
    for l in raw:
        if l.vin in kept_vins:
            db.insert_observation(run_id, l, observed_at)
            if l.imv:
                db.update_vehicle_imv(l.vin, l.imv, l.deal_rating, observed_at)
    db.commit()

    # 5. IMVs on file from earlier runs (lets scoring use CarGurus data with Apify off)
    known = db.known_imvs([l.vin for l in kept if not l.imv], config.IMV_MAX_AGE_DAYS)
    for l in kept:
        if not l.imv and l.vin in known:
            l.imv, l.deal_rating = known[l.vin]
    if known:
        log.info("reused %d CarGurus IMVs from earlier runs", len(known))

    # 6. score
    prior = db.prior_prices([l.vin for l in kept], before_run_id=run_id)
    scored = score_all(kept, prior)
    db.insert_scores(run_id, scored)

    # 7. optional MarketCheck history for the top N
    if fixture_path is None and config.MARKETCHECK_HISTORY_TOP_N > 0 and "marketcheck" in sources \
            and keys.get("MARKETCHECK_API_KEY"):
        mc = MarketCheckSource(http, keys["MARKETCHECK_API_KEY"])
        rows: list[dict] = []
        for s in scored[:config.MARKETCHECK_HISTORY_TOP_N]:
            rows.extend(mc.fetch_history(s.vin))
        if rows:
            db.insert_vin_history(rows)
        stats["api_calls"]["marketcheck_history"] = mc.calls_made

    notes = json.dumps({"per_source": dict(stats["per_source"]), "api_calls": stats["api_calls"],
                        "errors": stats["errors"]})
    db.finish_run(run_id, raw=len(raw), kept=len(kept), rejections=dict(rejections), notes=notes)
    log.info("run %d finished: %d scored", run_id, len(scored))
    return run_id
