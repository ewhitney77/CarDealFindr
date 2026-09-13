"""
SQLite persistence. This is the part you will actually query by hand.

Table design (see README "Database schema" for the full column list):

  runs          one row per execution of `run`
  vehicles      one row per VIN (slowly-changing: first/last seen, last IMV)
  observations  one row per (run, source, VIN) = the price/mileage FACT table
  scores        one row per (run, VIN) = scoring output incl. components
  vin_history   optional rows from MarketCheck's history-by-VIN endpoint
  http_cache    cached API/HTML responses (so re-runs don't burn quota)

Views:
  v_price_history   price per VIN per run (min across sources)
  v_price_changes   v_price_history + LAG() previous price per VIN
  v_leaderboard     scores joined to vehicle + dealer details

All timestamps are ISO-8601 strings in UTC (e.g. 2026-09-13T14:03:22Z),
which sort correctly as text and are readable in any SQL client.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .models import Listing

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    sources         TEXT,           -- comma-separated sources actually queried
    raw_listings    INTEGER,        -- rows returned by all sources before filtering
    kept_listings   INTEGER,        -- rows after filters + VIN de-dupe
    rejections_json TEXT,           -- {"over_budget": 12, ...}
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS vehicles (
    vin                 TEXT PRIMARY KEY,
    year                INTEGER,
    make                TEXT,
    model               TEXT,
    trim                TEXT,
    condition           TEXT,       -- new | used | cpo
    body_type           TEXT,
    drivetrain          TEXT,
    exterior_color      TEXT,
    interior_color      TEXT,
    first_seen_at       TEXT,       -- first time THIS TOOL saw the VIN
    first_seen_run_id   INTEGER,
    first_seen_price    INTEGER,
    last_seen_at        TEXT,
    last_seen_run_id    INTEGER,
    cargurus_imv        INTEGER,    -- last CarGurus Instant Market Value we saw
    cargurus_deal_rating TEXT,
    cargurus_seen_at    TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    vin                 TEXT NOT NULL REFERENCES vehicles(vin),
    source              TEXT NOT NULL,      -- marketcheck | cargurus_apify | autodev | dealer:<key>
    observed_at         TEXT NOT NULL,
    price               INTEGER,
    msrp                INTEGER,
    miles               INTEGER,
    days_on_market      INTEGER,
    source_prior_price  INTEGER,            -- previous price the source itself reports
    source_price_change_pct REAL,
    imv                 INTEGER,            -- CarGurus IMV (cargurus_apify rows only)
    deal_rating         TEXT,
    dealer_name         TEXT,
    dealer_city         TEXT,
    dealer_state        TEXT,
    dealer_zip          TEXT,
    dealer_lat          REAL,
    dealer_lon          REAL,
    distance_miles      REAL,
    listing_url         TEXT,
    source_listing_id   TEXT,
    source_first_seen_at TEXT,             -- when the SOURCE first saw the listing
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_vin_run ON observations(vin, run_id);
CREATE INDEX IF NOT EXISTS ix_obs_run ON observations(run_id);

CREATE TABLE IF NOT EXISTS scores (
    run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    vin                 TEXT NOT NULL REFERENCES vehicles(vin),
    rank                INTEGER,
    total_score         REAL,
    price_score         REAL,
    mileage_score       REAL,
    dom_score           REAL,
    price_drop_score    REAL,
    distance_score      REAL,
    price               INTEGER,
    miles               INTEGER,
    days_on_market      INTEGER,
    distance_miles      REAL,
    comparison_basis    TEXT,       -- imv | peer_median_trim | peer_median_model_year | peer_median_adjacent_years | peer_median_model | none
    comparison_value    INTEGER,
    peer_count          INTEGER,
    price_delta_pct     REAL,       -- (price - basis) / basis * 100 ; negative = cheaper
    expected_miles      INTEGER,
    price_drop_amount   INTEGER,    -- best known drop (source-reported or observed here)
    price_drop_pct      REAL,
    flags               TEXT,       -- semicolon-separated, e.g. VERIFY_PRICE;NEGOTIATE_DOM
    chosen_source       TEXT,       -- which observation row supplied price/miles
    PRIMARY KEY (run_id, vin)
);

CREATE TABLE IF NOT EXISTS vin_history (
    vin             TEXT NOT NULL,
    source          TEXT NOT NULL,
    listing_id      TEXT NOT NULL,
    price           INTEGER,
    miles           INTEGER,
    seller_name     TEXT,
    city            TEXT,
    state           TEXT,
    first_seen_date TEXT,
    last_seen_date  TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (vin, source, listing_id)
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key   TEXT PRIMARY KEY,
    url         TEXT,
    fetched_at  TEXT,
    expires_at  TEXT,
    status      INTEGER,
    body        TEXT
);

-- Price per VIN per run. If several sources saw the VIN in the same run,
-- take the lowest price (the one you'd actually pay attention to).
CREATE VIEW IF NOT EXISTS v_price_history AS
SELECT o.vin,
       o.run_id,
       r.started_at        AS run_started_at,
       MIN(o.price)        AS price,
       MIN(o.miles)        AS miles
FROM observations o
JOIN runs r ON r.run_id = o.run_id
WHERE o.price IS NOT NULL
GROUP BY o.vin, o.run_id;

-- Same, with the previous run's price alongside so drops are one WHERE away:
--   SELECT * FROM v_price_changes WHERE change_amount < 0 ORDER BY change_amount;
CREATE VIEW IF NOT EXISTS v_price_changes AS
SELECT vin, run_id, run_started_at, price, miles,
       LAG(price)  OVER (PARTITION BY vin ORDER BY run_id) AS prev_price,
       LAG(run_id) OVER (PARTITION BY vin ORDER BY run_id) AS prev_run_id,
       price - LAG(price) OVER (PARTITION BY vin ORDER BY run_id) AS change_amount
FROM v_price_history;

-- Everything the HTML report shows, for every run. Filter on run_id.
CREATE VIEW IF NOT EXISTS v_leaderboard AS
SELECT s.run_id, s.rank, s.total_score,
       s.price_score, s.mileage_score, s.dom_score, s.price_drop_score, s.distance_score,
       s.vin, v.year, v.make, v.model, v.trim, v.condition,
       s.price, s.comparison_basis, s.comparison_value, s.price_delta_pct, s.peer_count,
       s.miles, s.expected_miles, s.days_on_market, s.distance_miles,
       s.price_drop_amount, s.price_drop_pct, s.flags, s.chosen_source,
       o.dealer_name, o.dealer_city, o.dealer_state, o.listing_url,
       v.cargurus_imv, v.cargurus_deal_rating
FROM scores s
JOIN vehicles v ON v.vin = s.vin
LEFT JOIN observations o
       ON o.run_id = s.run_id AND o.vin = s.vin AND o.source = s.chosen_source;
"""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- generic
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------- runs
    def start_run(self, sources: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, sources) VALUES (?, ?)",
            (utcnow(), ",".join(sources)))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, raw: int, kept: int, rejections: dict, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, raw_listings=?, kept_listings=?, rejections_json=?, notes=? "
            "WHERE run_id=?",
            (utcnow(), raw, kept, json.dumps(rejections, sort_keys=True), notes, run_id))
        self.conn.commit()

    def latest_finished_run_id(self) -> Optional[int]:
        row = self.conn.execute(
            "SELECT MAX(run_id) AS id FROM runs WHERE finished_at IS NOT NULL").fetchone()
        return row["id"] if row and row["id"] is not None else None

    def run_info(self, run_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    # --------------------------------------------------------------- vehicles
    def upsert_vehicle(self, lst: Listing, run_id: int, observed_at: str) -> None:
        """Insert on first sight; afterwards refresh descriptive fields + last_seen."""
        self.conn.execute(
            """
            INSERT INTO vehicles (vin, year, make, model, trim, condition, body_type, drivetrain,
                                  exterior_color, interior_color, first_seen_at, first_seen_run_id,
                                  first_seen_price, last_seen_at, last_seen_run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(vin) DO UPDATE SET
                year           = COALESCE(excluded.year, vehicles.year),
                make           = COALESCE(excluded.make, vehicles.make),
                model          = COALESCE(excluded.model, vehicles.model),
                trim           = COALESCE(excluded.trim, vehicles.trim),
                condition      = COALESCE(excluded.condition, vehicles.condition),
                body_type      = COALESCE(excluded.body_type, vehicles.body_type),
                drivetrain     = COALESCE(excluded.drivetrain, vehicles.drivetrain),
                exterior_color = COALESCE(excluded.exterior_color, vehicles.exterior_color),
                interior_color = COALESCE(excluded.interior_color, vehicles.interior_color),
                last_seen_at   = excluded.last_seen_at,
                last_seen_run_id = excluded.last_seen_run_id
            """,
            (lst.vin, lst.year, lst.make, lst.model, lst.trim, lst.condition, lst.body_type,
             lst.drivetrain, lst.exterior_color, lst.interior_color, observed_at, run_id,
             lst.price, observed_at, run_id))

    def update_vehicle_imv(self, vin: str, imv: Optional[int], rating: Optional[str], seen_at: str) -> None:
        if imv is None:
            return
        self.conn.execute(
            "UPDATE vehicles SET cargurus_imv=?, cargurus_deal_rating=?, cargurus_seen_at=? WHERE vin=?",
            (imv, rating, seen_at, vin))

    def known_imvs(self, vins: Iterable[str], max_age_days: int) -> dict[str, tuple[int, Optional[str]]]:
        """IMVs we already have on file that are still fresh enough to use."""
        vins = list(vins)
        if not vins:
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, tuple[int, Optional[str]]] = {}
        for chunk_start in range(0, len(vins), 500):
            chunk = vins[chunk_start:chunk_start + 500]
            qmarks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT vin, cargurus_imv, cargurus_deal_rating FROM vehicles "
                f"WHERE vin IN ({qmarks}) AND cargurus_imv IS NOT NULL AND cargurus_seen_at >= ?",
                (*chunk, cutoff)).fetchall()
            for r in rows:
                out[r["vin"]] = (r["cargurus_imv"], r["cargurus_deal_rating"])
        return out

    # ----------------------------------------------------------- observations
    def insert_observation(self, run_id: int, lst: Listing, observed_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO observations (run_id, vin, source, observed_at, price, msrp, miles,
                days_on_market, source_prior_price, source_price_change_pct, imv, deal_rating,
                dealer_name, dealer_city, dealer_state, dealer_zip, dealer_lat, dealer_lon,
                distance_miles, listing_url, source_listing_id, source_first_seen_at, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (run_id, lst.vin, lst.source, observed_at, lst.price, lst.msrp, lst.miles,
             lst.days_on_market, lst.source_prior_price, lst.source_price_change_pct, lst.imv,
             lst.deal_rating, lst.dealer_name, lst.dealer_city, lst.dealer_state, lst.dealer_zip,
             lst.dealer_lat, lst.dealer_lon, lst.distance_miles, lst.listing_url,
             lst.source_listing_id, lst.first_seen_at,
             json.dumps(lst.raw, default=str)[:200_000] if lst.raw else None))

    def prior_prices(self, vins: Iterable[str], before_run_id: int) -> dict[str, dict]:
        """
        For each VIN: the most recent price we recorded in an EARLIER run and
        the highest price we've ever recorded. Feeds the price-drop score.
        """
        vins = list(vins)
        if not vins:
            return {}
        out: dict[str, dict] = {}
        for chunk_start in range(0, len(vins), 500):
            chunk = vins[chunk_start:chunk_start + 500]
            qmarks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"""
                SELECT vin,
                       MAX(price)      AS max_price,
                       MAX(run_id)     AS last_run_id
                FROM v_price_history
                WHERE vin IN ({qmarks}) AND run_id < ?
                GROUP BY vin
                """, (*chunk, before_run_id)).fetchall()
            for r in rows:
                last = self.conn.execute(
                    "SELECT price FROM v_price_history WHERE vin=? AND run_id=?",
                    (r["vin"], r["last_run_id"])).fetchone()
                out[r["vin"]] = {"max_price": r["max_price"],
                                 "last_price": last["price"] if last else None,
                                 "last_run_id": r["last_run_id"]}
        return out

    # ----------------------------------------------------------------- scores
    def insert_scores(self, run_id: int, scored: list) -> None:
        """`scored` is a list of scoring.ScoreResult."""
        rows = []
        for s in scored:
            rows.append((
                run_id, s.vin, s.rank, s.total,
                s.components["price"], s.components["mileage"], s.components["dom"],
                s.components["price_drop"], s.components["distance"],
                s.price, s.miles, s.days_on_market, s.distance_miles,
                s.comparison_basis, s.comparison_value, s.peer_count, s.price_delta_pct,
                s.expected_miles, s.price_drop_amount, s.price_drop_pct,
                ";".join(s.flags), s.chosen_source))
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO scores (run_id, vin, rank, total_score, price_score, mileage_score,
                dom_score, price_drop_score, distance_score, price, miles, days_on_market,
                distance_miles, comparison_basis, comparison_value, peer_count, price_delta_pct,
                expected_miles, price_drop_amount, price_drop_pct, flags, chosen_source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
        self.conn.commit()

    def leaderboard(self, run_id: int, top_n: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM v_leaderboard WHERE run_id=? ORDER BY rank LIMIT ?", (run_id, top_n)).fetchall()

    def price_drops_for_run(self, run_id: int) -> list[sqlite3.Row]:
        """VINs whose price in `run_id` is lower than the last time we saw them."""
        return self.conn.execute(
            """
            SELECT c.vin, c.price, c.prev_price, c.change_amount, c.prev_run_id,
                   ROUND(100.0 * c.change_amount / c.prev_price, 1) AS change_pct,
                   pr.started_at AS prev_run_started_at,
                   v.year, v.make, v.model, v.trim, v.condition,
                   lb.rank, lb.total_score, lb.dealer_name, lb.dealer_city, lb.dealer_state,
                   lb.listing_url, lb.miles, lb.distance_miles
            FROM v_price_changes c
            JOIN vehicles v ON v.vin = c.vin
            LEFT JOIN runs pr ON pr.run_id = c.prev_run_id
            LEFT JOIN v_leaderboard lb ON lb.run_id = c.run_id AND lb.vin = c.vin
            WHERE c.run_id = ? AND c.change_amount < 0
            ORDER BY c.change_amount ASC
            """, (run_id,)).fetchall()

    def vin_timeline(self, vin: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT o.run_id, o.observed_at, o.source, o.price, o.miles, o.days_on_market,
                   o.imv, o.deal_rating, o.dealer_name, o.dealer_city, o.dealer_state, o.listing_url
            FROM observations o WHERE o.vin = ? ORDER BY o.run_id, o.source
            """, (vin,)).fetchall()

    # ------------------------------------------------------------ vin_history
    def insert_vin_history(self, rows: list[dict]) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO vin_history (vin, source, listing_id, price, miles, seller_name,
                city, state, first_seen_date, last_seen_date, fetched_at)
            VALUES (:vin, :source, :listing_id, :price, :miles, :seller_name, :city, :state,
                    :first_seen_date, :last_seen_date, :fetched_at)
            """, rows)
        self.conn.commit()

    # ------------------------------------------------------------------ cache
    def cache_get(self, key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT status, body FROM http_cache WHERE cache_key=? AND expires_at > ?",
            (key, utcnow())).fetchone()
        return dict(row) if row else None

    def cache_put(self, key: str, url: str, status: int, body: str, ttl_hours: float) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "INSERT OR REPLACE INTO http_cache (cache_key, url, fetched_at, expires_at, status, body) "
            "VALUES (?,?,?,?,?,?)", (key, url.split("?")[0], utcnow(), expires, status, body))
        self.conn.commit()

    def cache_purge_expired(self) -> int:
        cur = self.conn.execute("DELETE FROM http_cache WHERE expires_at <= ?", (utcnow(),))
        self.conn.commit()
        return cur.rowcount

    def commit(self) -> None:
        self.conn.commit()
