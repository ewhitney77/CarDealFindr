"""
CarDealFindr configuration.

EVERY tunable value lives in this one file. The rest of the code imports
from here and never hardcodes a number. Think of this as the "parameters
table" for the whole pipeline.

Sections:
  1. Location & geography
  2. Budget, mileage, year rules
  3. The search set (which models, which trims, new vs used)
  4. Hard exclusions (Stellantis)
  5. Data source switches, quotas and caching
  6. Scoring weights and curves
  7. Output paths

API keys do NOT go here. They go in the .env file (see .env.example).
"""

# ---------------------------------------------------------------------------
# 1. LOCATION & GEOGRAPHY
# ---------------------------------------------------------------------------

# Home base for all distance calculations.
HOME_ZIP = "02038"                      # Franklin, MA
HOME_LAT = 42.0834                      # used when a source does not return
HOME_LON = -71.3967                     # a distance for us

# Search radius in miles. MarketCheck / Auto.dev / CarGurus all take this
# directly. Anything further than this is dropped even if a source returns it.
SEARCH_RADIUS_MILES = 200

# Only keep listings whose dealer is in one of these states.
ALLOWED_STATES = {"MA", "RI", "CT", "NH", "VT", "ME"}

# ---------------------------------------------------------------------------
# 2. BUDGET, MILEAGE, YEAR RULES
# ---------------------------------------------------------------------------

BUDGET_CAP = 50_000                     # hard ceiling on asking price (USD)

MILEAGE_HARD_CAP = 40_000               # anything above this is excluded
MILEAGE_PREFERRED_MAX = 25_000          # under this gets the "preferred band" bonus
EXPECTED_MILES_PER_YEAR = 12_000        # used to judge mileage vs. age

USED_YEAR_MIN = 2021                    # used / CPO model-year window
USED_YEAR_MAX = 2024

# ---------------------------------------------------------------------------
# 3. THE SEARCH SET
# ---------------------------------------------------------------------------
#
# Each entry is one "target": a make + model + condition.
#   condition:  "new"  -> only brand-new inventory, any model year
#               "used" -> used AND certified pre-owned, model years
#                         USED_YEAR_MIN..USED_YEAR_MAX
#   min_trim:   optional. The lowest acceptable trim. Requires a matching
#               entry in TRIM_LADDERS below so we know the ordering.
#
# Make/model spellings here are what we SEND to the APIs, so keep them as the
# APIs expect them (MarketCheck and Auto.dev are case-insensitive on these).

SEARCH_TARGETS = [
    # ---- New ----
    {"make": "Mazda",    "model": "CX-90",   "condition": "new", "min_trim": "Preferred Plus"},
    {"make": "Volkswagen", "model": "Atlas", "condition": "new", "min_trim": "SEL"},
    {"make": "Acura",    "model": "MDX",     "condition": "new"},
    {"make": "Infiniti", "model": "QX60",    "condition": "new"},
    # ---- Used / CPO (2021-2024) ----
    {"make": "Audi",     "model": "Q7",      "condition": "used"},
    {"make": "Audi",     "model": "Q8",      "condition": "used"},
    {"make": "BMW",      "model": "X5",      "condition": "used"},
    {"make": "Volvo",    "model": "XC90",    "condition": "used"},
    {"make": "Genesis",  "model": "GV80",    "condition": "used"},
    {"make": "Lincoln",  "model": "Aviator", "condition": "used"},
    {"make": "Acura",    "model": "MDX",     "condition": "used"},
]

# Trim ladders: lowest -> highest. Used to enforce "min_trim and up".
# Matching is case-insensitive substring; the LONGEST matching ladder entry
# wins (so "Preferred Plus" beats "Preferred"). A listing whose trim matches
# nothing on the ladder is EXCLUDED when a min_trim is set, and counted in
# the run summary so you can see if the ladder needs a new entry.
TRIM_LADDERS = {
    ("Mazda", "CX-90"): [
        "Select", "Preferred", "Preferred Plus", "Premium", "Premium Plus",
        "Premium Sport", "Turbo S", "Turbo S Premium", "Turbo S Premium Plus",
    ],
    ("Volkswagen", "Atlas"): [
        "SE", "SE w/Technology", "SE Technology", "SEL", "SEL R-Line",
        "SEL Premium", "SEL Premium R-Line", "Peak Edition",
    ],
}

# ---------------------------------------------------------------------------
# 4. HARD EXCLUSIONS
# ---------------------------------------------------------------------------

# No Jeep / Stellantis. Applied to every listing from every source, even if
# a source returns one by accident.
EXCLUDED_MAKES = {
    "Jeep", "Chrysler", "Dodge", "Ram", "Fiat", "Alfa Romeo", "Maserati",
    "Lancia", "Peugeot", "Citroen", "Opel", "Vauxhall", "DS",
}

# ---------------------------------------------------------------------------
# 5. DATA SOURCES
# ---------------------------------------------------------------------------

# Priority order when the same VIN shows up in several sources. The first
# source in this list that has a value wins for price / miles / dealer info.
# (IMV always comes from CarGurus; days-on-market prefers MarketCheck.)
SOURCE_PRIORITY = ["marketcheck", "cargurus_apify", "autodev", "dealer"]

# ---- MarketCheck (primary) ----
MARKETCHECK_ENABLED = True
MARKETCHECK_BASE_URL = "https://api.marketcheck.com/v2"
MARKETCHECK_ROWS_PER_PAGE = 50          # API maximum is 50
MARKETCHECK_MAX_PAGES_PER_TARGET = 4    # 4 x 50 = up to 200 listings per target
MARKETCHECK_MIN_SECONDS_BETWEEN_CALLS = 0.4   # free plan allows 5/sec; stay well under
MARKETCHECK_MAX_RETRIES = 5             # on HTTP 429 / 5xx, with exponential backoff
MARKETCHECK_CACHE_HOURS = 6             # identical query within this window = no API call
# car_type values sent for "used" targets. MarketCheck treats CPO as a subset
# of used, so "used" alone should include CPO listings. If CPO cars seem to be
# missing from results, change this to ["used", "certified"] (doubles calls).
MARKETCHECK_USED_CAR_TYPES = ["used"]
# Also send state=MA,RI,... to MarketCheck so result slots aren't wasted on
# NY/NJ dealers inside the 200-mile circle. Set False if the API rejects it.
MARKETCHECK_PASS_STATE_FILTER = True
# Optional: pull full listing history for the top-N scored VINs via the
# History-by-VIN endpoint. Costs ONE API CALL PER VIN, so it is off by default
# (free plan = 500 calls/month). Set to e.g. 10 once you know your budget.
MARKETCHECK_HISTORY_TOP_N = 0

# ---- CarGurus via Apify (secondary, BILLED PER RESULT) ----
APIFY_ENABLED = False                    # <-- master switch. False = never calls Apify.
APIFY_ACTOR_ID = "ahmed_jasarevic~cargurus-scraper"   # "user~actor" form for the REST API
APIFY_MAX_LISTINGS_PER_TARGET = 60      # controls your bill. Actor charges per listing.
APIFY_CACHE_DAYS = 3                    # reuse a cached actor run for this many days
APIFY_RUN_TIMEOUT_SECONDS = 600         # give up waiting on an actor run after this
APIFY_POLL_SECONDS = 10                 # how often to check whether the run finished
# How long a CarGurus IMV stays usable for scoring after we last saw it.
# Lets you run with APIFY_ENABLED=False and still score against IMVs pulled
# a few days ago.
IMV_MAX_AGE_DAYS = 14
# The actor's input field names. These are the names on the actor's "Input"
# tab at https://apify.com/ahmed_jasarevic/cargurus-scraper. If the actor
# author renames a field, fix it here, not in the code.
APIFY_INPUT_FIELDS = {
    "zip": "zip",                       # our HOME_ZIP goes here
    "radius": "distance",               # our SEARCH_RADIUS_MILES goes here
    "make": "make",
    "model": "model",
    "condition": "condition",           # value from APIFY_CONDITION_VALUES below
    "max_items": "maxItems",
}
APIFY_CONDITION_VALUES = {"new": "new", "used": "used"}
# Extra static input passed on every run (sort order, fingerprint, etc.).
APIFY_EXTRA_INPUT = {}

# ---- Auto.dev (free cross-check) ----
AUTODEV_ENABLED = True
AUTODEV_BASE_URL = "https://api.auto.dev"
AUTODEV_PAGE_SIZE = 50
AUTODEV_MAX_PAGES_PER_TARGET = 2
AUTODEV_CACHE_HOURS = 6
# Auto.dev is only queried for a target when MarketCheck returned fewer than
# this many listings for it ("coverage is thin"). Set to a huge number to
# always query it, or 0 to never query it.
AUTODEV_THIN_COVERAGE_THRESHOLD = 5
AUTODEV_MIN_SECONDS_BETWEEN_CALLS = 0.5

# ---- Direct dealer-group scraping ----
DEALER_SCRAPING_ENABLED = True
DEALER_REQUEST_DELAY_SECONDS = 2.0      # polite delay between page fetches
DEALER_CACHE_HOURS = 12
DEALER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Each dealer group: where it lives and which parsing strategy to try.
#   platform: "dealer.com"  -> tries the Dealer.com JSON inventory widget first,
#                              then falls back to HTML/JSON-LD parsing
#             "generic"     -> HTML/JSON-LD parsing only
#   used_path / new_path: inventory search page, with {make} and {model}
#             substituted. Adjust if the site's URL structure differs.
DEALER_GROUPS = [
    {
        "key": "herb_chambers", "name": "Herb Chambers", "state": "MA",
        "base_url": "https://www.herbchambers.com", "platform": "dealer.com",
        "used_path": "/used-inventory/index.htm?make={make}&model={model}",
        "new_path": "/new-inventory/index.htm?make={make}&model={model}",
    },
    {
        "key": "prime_motor_group", "name": "Prime Motor Group", "state": "ME",
        "base_url": "https://www.primemotorgroup.com", "platform": "generic",
        "used_path": "/used-inventory/index.htm?make={make}&model={model}",
        "new_path": "/new-inventory/index.htm?make={make}&model={model}",
    },
    {
        "key": "ira_motor_group", "name": "Ira Motor Group", "state": "MA",
        "base_url": "https://www.iramotorgroup.com", "platform": "generic",
        "used_path": "/used-inventory/index.htm?make={make}&model={model}",
        "new_path": "/new-inventory/index.htm?make={make}&model={model}",
    },
    {
        "key": "village_automotive", "name": "Village Automotive Group", "state": "MA",
        "base_url": "https://www.villageautomotive.com", "platform": "generic",
        "used_path": "/used-inventory/index.htm?make={make}&model={model}",
        "new_path": "/new-inventory/index.htm?make={make}&model={model}",
    },
]

# ---------------------------------------------------------------------------
# 6. SCORING
# ---------------------------------------------------------------------------
#
# Final score = weighted average of five 0-100 component scores.
# Weights do not need to sum to 100; they are normalised.

SCORE_WEIGHTS = {
    "price":      45,   # price vs CarGurus IMV (or peer median) - the main signal
    "mileage":    25,   # miles vs expected-for-age, plus under-25k bonus
    "dom":        10,   # days on market (older listing = negotiation leverage)
    "price_drop": 10,   # has the price already been cut?
    "distance":   10,   # closer to 02038 is better
}

# --- Price component curve ---
# delta% = (price - basis) / basis * 100.  Negative = cheaper than basis.
# Score is 50 at delta 0, reaches 100 at -PRICE_FULL_SCORE_PCT and 0 at
# +PRICE_FULL_SCORE_PCT, linear in between, clamped.
PRICE_FULL_SCORE_PCT = 15.0
# More than this far BELOW basis is flagged "verify this" (too good to be
# true usually means accident history, mis-listed trim, or a bait price).
PRICE_VERIFY_THRESHOLD_PCT = 15.0
# Minimum number of same-group listings needed before a peer median is trusted.
PEER_MEDIAN_MIN_COUNT = 3

# --- Mileage component curve ---
# ratio = miles / (EXPECTED_MILES_PER_YEAR * age_in_years)
# base score = 100 - MILEAGE_RATIO_SLOPE * ratio  (ratio 1.0 -> 50, 2.0 -> 0)
MILEAGE_RATIO_SLOPE = 50.0
MILEAGE_PREFERRED_BONUS = 15.0          # added when miles < MILEAGE_PREFERRED_MAX
MILEAGE_MIN_AGE_YEARS = 0.5             # a brand-new car is treated as 6 months old

# --- Days-on-market component curve ---
DOM_NEGOTIATE_DAYS = 60                 # >= this is flagged as a negotiation opportunity
DOM_FLOOR_SCORE = 20.0                  # a listing that appeared today scores this
DOM_UNKNOWN_SCORE = 50.0                # when no source reports days on market

# --- Price-drop component ---
PRICE_DROP_NONE_SCORE = 40.0            # no recorded drop
PRICE_DROP_BASE_SCORE = 60.0            # any recorded drop starts here...
PRICE_DROP_PCT_MULTIPLIER = 8.0         # ...plus 8 points per 1% cut, capped at 100

# --- Distance component ---
DISTANCE_UNKNOWN_SCORE = 50.0           # when we could not geocode the dealer

# ---------------------------------------------------------------------------
# 7. OUTPUT
# ---------------------------------------------------------------------------

DB_PATH = "data/cardealfindr.sqlite"
REPORT_DIR = "reports"
REPORT_TOP_N = 25
CACHE_DIR = "cache"                     # dealer page debug dumps go here
LOG_LEVEL = "INFO"
