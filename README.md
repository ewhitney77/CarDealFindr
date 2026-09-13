# CarDealFindr

A command-line tool that hunts for well-priced 3-row midsize luxury SUVs within
200 miles of Franklin, MA (02038), scores every listing 0–100 with visible
component scores, keeps a SQLite history so you can watch prices move, and
writes a single HTML report.

Search set (edit in `config.py`):

| Condition | Models |
|---|---|
| New | Mazda CX-90 (Preferred Plus and up), VW Atlas (SEL and up), Acura MDX, Infiniti QX60 |
| Used / CPO, 2021–2024 | Audi Q7, Audi Q8, BMW X5, Volvo XC90, Genesis GV80, Lincoln Aviator, Acura MDX |

Hard rules: price ≤ $50,000, mileage ≤ 40,000 (under 25,000 preferred and
scored higher), dealer in MA/RI/CT/NH/VT/ME, no Stellantis products ever.

---

## 1. Setup

```bash
git clone https://github.com/ewhitney77/CarDealFindr.git
cd CarDealFindr
python3 -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt
cp .env.example .env                                      # then paste your keys into .env
```

Python 3.10 or newer. No database server: SQLite is built into Python.

### Getting the API keys

**MarketCheck (primary, required).**
1. Go to <https://developers.marketcheck.com/> and create an account.
2. Create an application; the dashboard shows an **API key**.
3. Put it in `.env` as `MARKETCHECK_API_KEY=...`.
   Free plan: 500 calls/month, 5 calls/second. One page of 50 listings = one
   call. A default run is roughly 15–30 calls, and identical queries are
   cached for 6 hours so re-running while you tune scoring costs nothing.

**Apify (CarGurus IMV + deal ratings, optional, billed per listing).**
1. Create an account at <https://apify.com/> and open
   <https://console.apify.com/account/integrations>.
2. Copy your **Personal API token** into `.env` as `APIFY_TOKEN=...`.
3. Open the actor page <https://apify.com/ahmed_jasarevic/cargurus-scraper>
   and check its **Input** tab. The field names it expects are configured in
   `config.py` under `APIFY_INPUT_FIELDS`; adjust them if the actor's input
   tab shows different names.
4. It is **off by default** (`APIFY_ENABLED = False`). Turn it on in
   `config.py` or per run with `run --apify`. `APIFY_MAX_LISTINGS_PER_TARGET`
   caps the bill; results are cached for `APIFY_CACHE_DAYS` and every IMV is
   stored in the database so it keeps feeding the score for
   `IMV_MAX_AGE_DAYS` even on runs where Apify is off.

**Auto.dev (free cross-check).**
1. Sign up at <https://www.auto.dev/> and copy the API key from your dashboard.
2. `.env`: `AUTODEV_API_KEY=...`. Free plan: 1,000 calls/month.
3. It is only queried for models where MarketCheck returned fewer than
   `AUTODEV_THIN_COVERAGE_THRESHOLD` listings.

**Dealer-group sites** (Herb Chambers, Prime Motor Group, Ira Motor Group,
Village Automotive) need no key. See "Dealer scraping" below before relying
on them.

---

## 2. Running it

```bash
python cardealfindr.py run                 # MarketCheck (+ Auto.dev when thin, + dealer sites)
python cardealfindr.py run --apify         # also pull CarGurus IMVs this run (costs money)
python cardealfindr.py run --open          # open the HTML report when done
python cardealfindr.py run --sources marketcheck          # exactly these sources
python cardealfindr.py run --no-dealers --no-cache        # skip scraping, ignore cache
python cardealfindr.py run --debug-dealers                # dump fetched dealer pages to cache/dealer_debug/

python cardealfindr.py report              # rebuild reports/latest.html for the latest run
python cardealfindr.py report --run-id 3 --top 50
python cardealfindr.py history WA1LXBF70PD012345          # every observation of one VIN
python cardealfindr.py sql "SELECT * FROM v_price_changes WHERE change_amount < 0"
python cardealfindr.py targets             # print the search set as config resolves it
```

Try it without any keys:

```bash
python cardealfindr.py run --fixture tests/fixtures/sample_listings.json --open
```

That loads synthetic listings, scores them, writes the database and the
report, so you can see the whole pipeline before spending a single API call.
The cars and dealers in it are invented, so its links do not open a real
listing; the report shows a yellow "Demo data" banner to make that obvious.
Links are only real on a live run.

Every run writes `reports/latest.html` plus a timestamped copy
`reports/run_0007_20260913_1030.html`.

---

## 3. What the report shows

**Top 25** sorted by score. Every row has the total, the five component
scores (hover the score, or read the mini-bars: price · mileage · days on
market · price drop · distance), price, delta vs the comparison basis and
what that basis was (CarGurus IMV, or the median of N peers), mileage (red
when over 25k), the trim with its **LOW / MID / HIGH tier**, dealer and
town, distance from 02038, days listed, flags, the VIN, and two links.

**Links.** Every link goes straight to that exact car. Nothing points at a
search page.

| Label | Where it goes | Comes from |
|---|---|---|
| Dealer page | the dealership's own vehicle detail page for that VIN | MarketCheck `vdp_url` |
| CarGurus | the CarGurus listing for that VIN | Apify actor |
| Auto.dev | Auto.dev's page for that VIN | Auto.dev `vdp` |
| *Herb Chambers* etc. | the page the scraper found the car on | dealer-site scrapers |

A car found by more than one source shows more than one link, so you can open
the dealer's own listing and the CarGurus page for the same VIN side by side.
When a source returns no vehicle page but does give a dealership website, the
row shows a greyed "… site" link to the dealership instead. When no source
gives any URL, the row says "no direct link" rather than sending you to a
search engine.

If clicks do nothing, you are probably viewing the file inside a preview pane
that blocks navigation; open `reports/latest.html` directly in a browser (or
use `--open`).

**Trim tiers.** `config.TRIM_LADDERS` orders the trims of every model in the
search set from base to top and tags each as `low`, `medium` or `high`. The
tier is stored in `vehicles.trim_tier` and shown as a badge. A "?" badge
means the source's trim string matched nothing on that model's ladder; the
run summary lists those under `trim_tier_unknown:` so you can add the
missing name. The same ladders enforce "Preferred Plus and up" / "SEL and
up" for the two new models with a `min_trim`.

**Price drops since the previous run**: any VIN whose price is lower than the
last time the tool saw it.

**Run summary**: listings per source and exclusion counts by reason. Watch
`trim_unrecognised:` here: it means a CX-90 or Atlas trim string didn't
match the ladder in `config.TRIM_LADDERS`, so add it.

Verdict labels: *Great deal* (≥ 7.5% under basis), *Good deal* (≥ 2.5%
under), *Fair price*, *Above market*, and **Verify this** for anything more
than 15% under IMV/median. A price that good usually means accident history,
a mis-listed trim, or a bait price. It still ranks by its score; the label
tells you to check before you get excited.

---

## 4. How scoring works

`total = Σ weight × component / Σ weights`, five components, each 0–100.
Weights in `config.SCORE_WEIGHTS` (default price 45, mileage 25, days on
market 10, price drop 10, distance 10).

| Component | Logic (defaults in `config.py`) | Flags |
|---|---|---|
| price | `delta = (price − basis) / basis`. Basis = CarGurus IMV if we have one, else the median price of peers **in this run's result set**, trying same condition class + year + make + model + trim first, then same year any trim, then model year ± 1, then any year, using the first group with ≥ 3 members. 50 at market, 100 at −15%, 0 at +15%, linear. New and used cars never share a peer group. | `VERIFY_PRICE` at ≤ −15%; `NO_PRICE_BASIS` when no IMV and no group big enough (score 50) |
| mileage | `expected = 12,000 × model age` (model age = current year − model year, min 0.5). `100 − 50 × miles/expected`, +15 if under 25,000, clamped. | `HIGH_MILES` when ≥ 25,000 (allowed, not preferred) |
| dom | days on market: 20 for a listing that appeared today, 100 at 60+ days. Unknown = 50. | `NEGOTIATE_DOM` at ≥ 60 days |
| price_drop | 40 if no cut is known. Any cut: 60 + 8 per 1% cut, max 100. A cut is the larger of the source's own previous-price field (MarketCheck `ref_price`) and the highest price this tool recorded for the VIN in earlier runs. | `PRICE_DROP` |
| distance | 100 at home, 0 at 200 miles. Unknown = 50. | `DISTANCE_UNKNOWN` |

Days on market from MarketCheck is measured from the listing's
`first_seen_at`; its lifetime `dom` (which can include a previous dealer)
is kept in `raw_json` if you want it.

---

## 5. Database schema (`data/cardealfindr.sqlite`)

Open it with any SQLite client (DB Browser for SQLite, DBeaver, the
`sqlite3` CLI, or `python cardealfindr.py sql "..."`). All timestamps are
ISO-8601 UTC text, so they sort correctly and are readable.

Think of it as a small star: `vehicles` is the dimension, `observations` is
the fact table (one row per run × source × VIN), `scores` is a derived
fact table (one row per run × VIN), `runs` is the batch log.

### `runs` — one row per execution
| column | meaning |
|---|---|
| `run_id` | primary key |
| `started_at`, `finished_at` | UTC timestamps; `finished_at` NULL means the run crashed |
| `sources` | comma list of sources queried, e.g. `marketcheck,autodev,dealers` |
| `raw_listings` | rows returned by all sources before filters |
| `kept_listings` | unique VINs that passed every filter |
| `rejections_json` | `{"over_budget": 12, "over_mileage_cap": 7, ...}` |
| `notes` | JSON: listings per source, API calls used, errors |

### `vehicles` — one row per VIN
| column | meaning |
|---|---|
| `vin` | primary key |
| `year`, `make`, `model`, `trim`, `condition` | `condition` is `new`, `used` or `cpo` |
| `trim_tier` | `low`, `medium`, `high` from `config.TRIM_LADDERS`; NULL if the trim matched nothing |
| `body_type`, `drivetrain`, `exterior_color`, `interior_color` | when a source provides them |
| `first_seen_at`, `first_seen_run_id`, `first_seen_price` | first time this tool saw the VIN |
| `last_seen_at`, `last_seen_run_id` | most recent run that saw it |
| `cargurus_imv`, `cargurus_deal_rating`, `cargurus_seen_at` | last CarGurus IMV; reused for scoring while younger than `IMV_MAX_AGE_DAYS` |

### `observations` — one row per run × source × VIN (the price history)
| column | meaning |
|---|---|
| `observation_id` | primary key |
| `run_id`, `vin`, `source`, `observed_at` | `source` is `marketcheck`, `cargurus_apify`, `autodev` or `dealer:<key>` |
| `price`, `msrp`, `miles` | as reported by that source |
| `days_on_market` | see scoring notes |
| `source_prior_price`, `source_price_change_pct` | the source's own price-change fields (MarketCheck `ref_price` / `price_change_percent`) |
| `imv`, `deal_rating` | CarGurus rows only |
| `dealer_name`, `dealer_city`, `dealer_state`, `dealer_zip`, `dealer_lat`, `dealer_lon`, `distance_miles` | where the car is |
| `dealer_website` | the dealership's home page, used as a link only when that source gave no vehicle page |
| `listing_url` | direct link to this exact car on that source's site |
| `source_listing_id`, `source_first_seen_at` | provenance |
| `raw_json` | the untouched source payload, for forensics |

Only VINs that passed the filters are written; rejected listings are
counted in `runs.rejections_json`, not stored.

### `scores` — one row per run × VIN
| column | meaning |
|---|---|
| `run_id`, `vin`, `rank` | rank 1 = best in that run |
| `total_score` | 0–100 |
| `price_score`, `mileage_score`, `dom_score`, `price_drop_score`, `distance_score` | the five components, each 0–100 |
| `price`, `miles`, `days_on_market`, `distance_miles` | the inputs used (after merging sources) |
| `comparison_basis` | `imv`, `peer_median_trim`, `peer_median_model_year`, `peer_median_adjacent_years`, `peer_median_model`, or `none` |
| `comparison_value`, `peer_count` | the basis price and how many peers formed it (0 for IMV) |
| `price_delta_pct` | `(price − basis) / basis × 100`; negative = cheaper |
| `expected_miles` | 12k × model age |
| `price_drop_amount`, `price_drop_pct` | best known cut |
| `flags` | semicolon list: `VERIFY_PRICE;HIGH_MILES;NEGOTIATE_DOM;PRICE_DROP;NO_PRICE_BASIS;DISTANCE_UNKNOWN` |
| `chosen_source` | which source's observation supplied price/miles/dealer |

### `vin_history` — optional MarketCheck history-by-VIN rows
Filled only when `MARKETCHECK_HISTORY_TOP_N > 0` (one API call per VIN).
Columns: `vin, source, listing_id, price, miles, seller_name, city, state,
first_seen_date, last_seen_date, fetched_at`.

### `http_cache` — cached API responses
`cache_key, url, fetched_at, expires_at, status, body`. Safe to `DELETE FROM`
at any time; the next run re-fetches (and spends quota).

### Views
| view | what it is |
|---|---|
| `v_price_history` | price per VIN per run (lowest across sources) |
| `v_price_changes` | `v_price_history` plus `prev_price`, `prev_run_id`, `change_amount` via `LAG()` |
| `v_leaderboard` | `scores` joined to `vehicles` and the chosen observation: exactly what the report shows |

### Queries to start with

```sql
-- latest leaderboard
SELECT rank, total_score, year, make, model, trim, trim_tier, price, price_delta_pct, miles,
       dealer_name, distance_miles, days_on_market, flags, listing_url
FROM v_leaderboard
WHERE run_id = (SELECT MAX(run_id) FROM runs WHERE finished_at IS NOT NULL)
ORDER BY rank;

-- every price cut ever observed
SELECT vin, run_started_at, prev_price, price, change_amount
FROM v_price_changes WHERE change_amount < 0 ORDER BY change_amount;

-- how one car moved over time, per source
SELECT run_id, observed_at, source, price, miles, days_on_market
FROM observations WHERE vin = 'WA1LXBF70PD012345' ORDER BY run_id;

-- cars that disappeared (sold?) since the previous run
SELECT v.vin, v.year, v.make, v.model, v.trim, v.first_seen_price
FROM vehicles v
WHERE v.last_seen_run_id = (SELECT MAX(run_id) FROM runs) - 1;

-- mid/high-trim used cars under 25k miles, cheapest first
SELECT year, make, model, trim, trim_tier, price, miles, dealer_name, listing_url
FROM v_leaderboard
WHERE run_id = (SELECT MAX(run_id) FROM runs WHERE finished_at IS NOT NULL)
  AND condition IN ('used', 'cpo') AND trim_tier IN ('medium', 'high') AND miles < 25000
ORDER BY price;

-- API quota used per run
SELECT run_id, started_at, json_extract(notes, '$.api_calls') FROM runs;
```

---

## 6. Data sources, in the order they are trusted

When the same VIN appears in several sources, `config.SOURCE_PRIORITY`
decides whose price/miles/dealer win; lower-priority sources only fill
gaps. IMV always comes from CarGurus. Every source row is still written to
`observations`, so nothing is thrown away.

1. **MarketCheck** `/v2/search/car/active`: price, mileage, VIN, trim, days
   on market, dealer and coordinates, distance, `ref_price` price changes.
2. **CarGurus via Apify**: IMV and deal rating per listing. Off by default.
3. **Auto.dev**: free cross-check when MarketCheck is thin for a model.
4. **Dealer-group sites**: Herb Chambers, Prime Motor Group, Ira Motor Group,
   Village Automotive. `requests` + BeautifulSoup, real user agent, 2-second
   delay, robots.txt honoured, pages cached 12 hours.

Deliberately **not** scraped: Cars.com, CarGurus directly, Autotrader,
TrueCar, CarMax. They sit behind bot protection and would break the tool.

### Dealer scraping: read this before trusting it

The scrapers in `cardealfindr/sources/dealers.py` try three parsing
strategies per page: the Dealer.com JSON inventory widget (Herb Chambers'
site uses Dealer.com URL patterns), schema.org JSON-LD blocks, and
`data-vin` vehicle cards. They were written against those platforms'
documented structures, **not** against live fetches of each site, because
the machine this was built on could not reach them. Treat the first run as
a smoke test:

```bash
python cardealfindr.py run --sources dealers --debug-dealers
ls cache/dealer_debug/
```

If a group returns 0 listings, open its dump, find where the vehicles are in
the HTML, and adjust that group's `used_path` / `new_path` / `platform` in
`config.DEALER_GROUPS`. If a site turns out to be behind Cloudflare, remove
it from the list; MarketCheck already indexes those dealers' inventory.

---

## 7. Layout

```
config.py                     every tunable value (budget, miles, radius, models, trims, weights, switches)
cardealfindr.py               command line entry point
cardealfindr/
  models.py                   the Listing shape every source is normalised into
  filters.py                  eligibility rules (the WHERE clause)
  scoring.py                  0-100 score + components + flags
  pipeline.py                 collect -> merge by VIN -> filter -> score -> persist
  db.py                       SQLite schema, views, queries
  report.py                   HTML report
  http.py                     cached, throttled, retrying HTTP client
  geo.py                      distance from 02038
  sources/marketcheck.py      source 1
  sources/cargurus_apify.py   source 2
  sources/autodev.py          source 3
  sources/dealers.py          source 4
tests/                        pytest suite (python -m pytest)
data/cardealfindr.sqlite      created on first run
reports/latest.html           created on first run
```

## 8. Things worth knowing

* **Quota safety.** Identical MarketCheck queries are cached for 6 hours and
  every run logs `api_calls` into `runs.notes`. `--no-cache` forces fresh
  calls. The MarketCheck history-by-VIN endpoint is off by default because
  it is one call per VIN.
* **CPO coverage.** MarketCheck is queried with `car_type=used`, which should
  include certified cars (they come back with `is_certified=1` and are stored
  as `cpo`). If CPO listings look absent, set
  `MARKETCHECK_USED_CAR_TYPES = ["used", "certified"]`.
* **Peer medians are only as good as the result set.** A lone 2021 Q7 with
  no same-year peers is compared to 2020–2022 Q7s, and if that is still too
  thin, to every used Q7 in the run, which biases it toward looking cheap.
  The basis level is shown in the report and stored in
  `scores.comparison_basis`, so you can see when that happened. Turning on
  Apify replaces the median with an IMV for the listings CarGurus covers.
* **Distance** uses dealer coordinates when a source provides them
  (MarketCheck does), otherwise the dealer ZIP via `pgeocode` (downloads a
  GeoNames file once), otherwise it is unknown and scored neutrally.
* **Trim ladders** cover all ten models for tier labelling, but only the
  CX-90 and Atlas targets set a `min_trim`, so every other model is accepted
  at any trim. Ladders are best-effort lists of factory trim names; when a
  source formats a trim differently ("SH-AWD w/Technology Package"), the
  whole-word matcher usually still finds the right entry. Check the
  `trim_tier_unknown:` counts in the run summary after your first real run.
