#!/usr/bin/env python3
"""
CarDealFindr command line.

  python cardealfindr.py run                      # query sources, score, write DB + report
  python cardealfindr.py run --apify              # also call the paid CarGurus actor this run
  python cardealfindr.py run --sources marketcheck,autodev
  python cardealfindr.py run --fixture tests/fixtures/sample_listings.json   # offline demo
  python cardealfindr.py report                   # rebuild the HTML for the latest run
  python cardealfindr.py report --run-id 3 --top 50
  python cardealfindr.py history <VIN>            # every observation of one car
  python cardealfindr.py sql "SELECT ... "        # ad-hoc read-only query, printed as a table
  python cardealfindr.py targets                  # show the search set as config resolves it

API keys are read from .env (see .env.example).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser

from dotenv import load_dotenv

import config
from cardealfindr.db import Database
from cardealfindr.pipeline import ALL_SOURCES, run_pipeline
from cardealfindr.report import build_report


def _keys() -> dict[str, str]:
    load_dotenv()
    return {k: os.getenv(k, "") for k in ("MARKETCHECK_API_KEY", "APIFY_TOKEN", "AUTODEV_API_KEY")}


def _default_sources(args) -> list[str]:
    """Which sources this run should hit, from config flags + CLI overrides."""
    if args.sources:
        wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
        bad = [s for s in wanted if s not in ALL_SOURCES]
        if bad:
            sys.exit(f"unknown source(s) {bad}; choose from {ALL_SOURCES}")
        return wanted
    wanted = []
    if config.MARKETCHECK_ENABLED:
        wanted.append("marketcheck")
    if (config.APIFY_ENABLED or args.apify) and not args.no_apify:
        wanted.append("cargurus_apify")
    if config.AUTODEV_ENABLED:
        wanted.append("autodev")
    if config.DEALER_SCRAPING_ENABLED and not args.no_dealers:
        wanted.append("dealers")
    return wanted


def _print_table(rows) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = rows[0].keys()
    widths = {c: max(len(c), *(len(str(r[c])) if r[c] is not None else 4 for r in rows)) for c in cols}
    widths = {c: min(w, 48) for c, w in widths.items()}
    line = " | ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("-" * len(line))
    for r in rows:
        print(" | ".join(("" if r[c] is None else str(r[c]))[:48].ljust(widths[c]) for c in cols))
    print(f"({len(rows)} rows)")


def cmd_run(args) -> None:
    db = Database(config.DB_PATH)
    sources = _default_sources(args)
    keys = _keys()
    missing = [k for k, s in (("MARKETCHECK_API_KEY", "marketcheck"), ("APIFY_TOKEN", "cargurus_apify"),
                              ("AUTODEV_API_KEY", "autodev")) if s in sources and not keys[k] and not args.fixture]
    if missing:
        logging.warning("missing keys in .env: %s - those sources will be skipped", ", ".join(missing))
    run_id = run_pipeline(db, sources, keys, use_cache=not args.no_cache,
                          debug_dealers=args.debug_dealers, fixture_path=args.fixture)
    path = build_report(db, run_id, top_n=args.top)
    rows = db.leaderboard(run_id, min(args.top, 10))
    print(f"\nrun {run_id} complete. Top {len(rows)}:")
    _print_table([{
        "rank": r["rank"], "score": r["total_score"], "vehicle": f'{r["year"]} {r["make"]} {r["model"]} {r["trim"] or ""}'.strip(),
        "price": r["price"], "delta%": r["price_delta_pct"], "miles": r["miles"],
        "dealer": r["dealer_name"], "mi": r["distance_miles"], "dom": r["days_on_market"], "flags": r["flags"],
    } for r in rows])
    print(f"\nreport: {os.path.abspath(path)}")
    if args.open:
        webbrowser.open(f"file://{os.path.abspath(path)}")
    db.close()


def cmd_report(args) -> None:
    db = Database(config.DB_PATH)
    run_id = args.run_id or db.latest_finished_run_id()
    if run_id is None:
        sys.exit("no completed runs in the database yet - run `cardealfindr.py run` first")
    path = build_report(db, run_id, top_n=args.top, out_path=args.out)
    print(f"report for run {run_id}: {os.path.abspath(path)}")
    if args.open:
        webbrowser.open(f"file://{os.path.abspath(path)}")
    db.close()


def cmd_history(args) -> None:
    db = Database(config.DB_PATH)
    vin = args.vin.strip().upper()
    v = db.query("SELECT * FROM vehicles WHERE vin=?", (vin,))
    if not v:
        sys.exit(f"{vin} is not in the database")
    r = v[0]
    print(f"{r['year']} {r['make']} {r['model']} {r['trim'] or ''} ({r['condition']}) "
          f"first seen {r['first_seen_at']} @ ${r['first_seen_price'] or 0:,}; "
          f"IMV {r['cargurus_imv'] or '—'} {r['cargurus_deal_rating'] or ''}")
    _print_table(db.vin_timeline(vin))
    hist = db.query("SELECT * FROM vin_history WHERE vin=? ORDER BY last_seen_date DESC", (vin,))
    if hist:
        print("\nMarketCheck listing history:")
        _print_table(hist)
    db.close()


def cmd_sql(args) -> None:
    db = Database(config.DB_PATH)
    sql = args.query.strip()
    if not sql.lower().startswith(("select", "with", "pragma", "explain")):
        sys.exit("only read-only queries here; use the sqlite3 CLI for writes")
    _print_table(db.query(sql))
    db.close()


def cmd_targets(_args) -> None:
    from cardealfindr.filters import trim_rank
    for t in config.SEARCH_TARGETS:
        extra = ""
        if t.get("min_trim"):
            ladder = config.TRIM_LADDERS.get((t["make"], t["model"]), [])
            rank = trim_rank(t["make"], t["model"], t["min_trim"])
            ok = ", ".join(ladder[rank:]) if rank is not None else "!! min_trim not on ladder"
            extra = f"  trims allowed: {ok}"
        years = "any model year" if t["condition"] == "new" else f"{config.USED_YEAR_MIN}-{config.USED_YEAR_MAX}"
        print(f"{t['condition']:<4} {t['make']:<11} {t['model']:<8} {years}{extra}")
    print(f"\nexcluded makes: {', '.join(sorted(config.EXCLUDED_MAKES))}")
    print(f"budget <= ${config.BUDGET_CAP:,}; miles <= {config.MILEAGE_HARD_CAP:,} (prefer < {config.MILEAGE_PREFERRED_MAX:,}); "
          f"radius {config.SEARCH_RADIUS_MILES} mi from {config.HOME_ZIP}; states {', '.join(sorted(config.ALLOWED_STATES))}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="cardealfindr", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="query sources, score, persist, write report")
    r.add_argument("--sources", help=f"comma list from {ALL_SOURCES} (overrides config switches)")
    r.add_argument("--apify", action="store_true", help="call the paid CarGurus actor this run")
    r.add_argument("--no-apify", action="store_true", help="never call Apify this run")
    r.add_argument("--no-dealers", action="store_true", help="skip dealer-site scraping this run")
    r.add_argument("--no-cache", action="store_true", help="ignore cached API responses (spends quota)")
    r.add_argument("--debug-dealers", action="store_true", help="dump fetched dealer pages to cache/dealer_debug/")
    r.add_argument("--fixture", help="offline: load raw listings from a JSON file instead of APIs")
    r.add_argument("--top", type=int, default=config.REPORT_TOP_N)
    r.add_argument("--open", action="store_true", help="open the report in a browser when done")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="rebuild the HTML report for a run")
    rp.add_argument("--run-id", type=int)
    rp.add_argument("--top", type=int, default=config.REPORT_TOP_N)
    rp.add_argument("--out", help="output path (default reports/latest.html)")
    rp.add_argument("--open", action="store_true")
    rp.set_defaults(func=cmd_report)

    h = sub.add_parser("history", help="show every observation of one VIN")
    h.add_argument("vin")
    h.set_defaults(func=cmd_history)

    q = sub.add_parser("sql", help="run a read-only SQL query against the database")
    q.add_argument("query")
    q.set_defaults(func=cmd_sql)

    t = sub.add_parser("targets", help="print the search set")
    t.set_defaults(func=cmd_targets)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else getattr(logging, config.LOG_LEVEL, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
