"""
HTML report: one self-contained file you can open in any browser.

Sections
  1. Run summary (sources, counts, rejections)
  2. Top-N table: score + the five component scores, price, delta vs
     IMV/median, mileage, trim, dealer, distance, days on market, link
  3. Price drops since the previous run (from v_price_changes)
  4. How the score is built (weights straight from config)

No external assets: CSS and a tiny click-to-sort script are inlined.
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime

import config
from .db import Database

FLAG_HELP = {
    "VERIFY_PRICE":   ("verify", "More than 15% below IMV/median - check for accident history or mis-listed trim"),
    "HIGH_MILES":     ("miles", "Inside the 40k cap but above the preferred 25k band"),
    "NEGOTIATE_DOM":  ("60+ days", "On the market 60+ days - negotiation opportunity"),
    "PRICE_DROP":     ("price cut", "Price has already been reduced"),
    "NO_PRICE_BASIS": ("no basis", "No IMV and too few peers for a median - price score is neutral"),
    "DISTANCE_UNKNOWN": ("dist ?", "Could not geocode the dealer"),
}

CSS = """
:root { --ink:#1b1f24; --muted:#6a737d; --line:#e1e4e8; --bg:#fff; --soft:#f6f8fa;
        --good:#1a7f37; --warn:#9a6700; --bad:#cf222e; --accent:#0969da; }
* { box-sizing:border-box; }
body { margin:0; padding:24px 20px 60px; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:var(--ink); background:var(--bg); }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:32px 0 10px; }
.sub { color:var(--muted); margin:0 0 18px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:8px; }
.card { background:var(--soft); border:1px solid var(--line); border-radius:8px; padding:10px 14px; min-width:140px; }
.card b { display:block; font-size:20px; } .card span { color:var(--muted); font-size:12px; }
.tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; min-width:1100px; }
th, td { padding:8px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap; }
th { background:var(--soft); font-weight:600; cursor:pointer; user-select:none; position:sticky; top:0; }
th.sorted-asc::after { content:" \\25B2"; } th.sorted-desc::after { content:" \\25BC"; }
tr:hover td { background:#fbfcfd; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.score { font-weight:700; font-size:15px; }
.bars { display:flex; gap:3px; margin-top:3px; }
.bar { width:22px; height:6px; background:#e6e8eb; border-radius:2px; position:relative; }
.bar i { position:absolute; left:0; top:0; bottom:0; background:var(--accent); border-radius:2px; }
.veh b { display:block; } .veh small { color:var(--muted); }
.badge { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; margin:0 3px 3px 0; border:1px solid transparent; }
.b-new { background:#ddf4ff; color:#0550ae; } .b-used { background:#eaeef2; color:#424a53; } .b-cpo { background:#dafbe1; color:#116329; }
.b-verify { background:#fff8c5; color:#7d4e00; border-color:#d4a72c; }
.b-miles { background:#ffebe9; color:#a40e26; }
.b-60\\+ { background:#ddf4ff; color:#0550ae; } .b-price { background:#dafbe1; color:#116329; }
.b-no, .b-dist { background:#f6f8fa; color:#57606a; border-color:#d0d7de; }
.t-low { background:#eaeef2; color:#424a53; } .t-medium { background:#ddf4ff; color:#0550ae; }
.t-high { background:#fbefff; color:#8250df; } .t-unknown { background:#f6f8fa; color:#8c959f; border-color:#d0d7de; }
.vin { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; color:var(--muted); user-select:all; }
.links a { display:block; }
.lbl-great { color:var(--good); font-weight:700; } .lbl-good { color:var(--good); }
.lbl-verify { color:var(--warn); font-weight:700; } .lbl-above { color:var(--bad); }
.delta-neg { color:var(--good); } .delta-pos { color:var(--bad); }
.muted { color:var(--muted); } .hi { color:var(--bad); font-weight:600; }
a { color:var(--accent); }
.legend { font-size:12px; color:var(--muted); margin-top:8px; }
code { background:var(--soft); padding:1px 5px; border-radius:4px; font-size:12px; }
@media (max-width:600px){ body{padding:14px 12px 40px;} }
"""

JS = """
document.querySelectorAll('table.sortable th').forEach(function(th, idx){
  th.addEventListener('click', function(){
    var table = th.closest('table'), tbody = table.tBodies[0];
    var rows = Array.from(tbody.rows);
    var asc = !th.classList.contains('sorted-asc');
    table.querySelectorAll('th').forEach(function(h){ h.classList.remove('sorted-asc','sorted-desc'); });
    th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
    rows.sort(function(a, b){
      var x = a.cells[idx].dataset.v ?? a.cells[idx].textContent.trim();
      var y = b.cells[idx].dataset.v ?? b.cells[idx].textContent.trim();
      var nx = parseFloat(x), ny = parseFloat(y);
      if (!isNaN(nx) && !isNaN(ny)) return asc ? nx - ny : ny - nx;
      return asc ? x.localeCompare(y) : y.localeCompare(x);
    });
    rows.forEach(function(r){ tbody.appendChild(r); });
  });
});
"""


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _money(v) -> str:
    if v is None:
        return "—"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _badges(flags: str) -> str:
    out = []
    for f in (flags or "").split(";"):
        if not f:
            continue
        short, tip = FLAG_HELP.get(f, (f.lower(), f))
        cls = "b-" + short.split()[0]
        out.append(f'<span class="badge {cls}" title="{_esc(tip)}">{_esc(short)}</span>')
    return "".join(out)


def _tier_badge(tier) -> str:
    t = tier or "unknown"
    text = {"low": "LOW", "medium": "MID", "high": "HIGH"}.get(t, "?")
    tip = {"low": "base / entry trim", "medium": "mid trim", "high": "top trim"}.get(
        t, "trim not on the ladder in config.TRIM_LADDERS")
    return f'<span class="badge t-{t}" title="{tip}">{text}</span>'


def _tier_sort(tier) -> int:
    return config.TRIM_TIER_ORDER.index(tier) if tier in config.TRIM_TIER_ORDER else -1


def _vin_search_url(vin: str) -> str:
    return f"https://www.google.com/search?q={_esc(vin)}"


def _links(row) -> str:
    """
    Always give the reader something clickable:
      * the listing URL the source reported (dealer VDP / CarGurus page)
      * a VIN web search as a fallback and as a cross-check
    """
    parts = []
    url = row["listing_url"]
    if url:
        parts.append(f'<a href="{_esc(url)}" target="_blank" rel="noopener" title="{_esc(url)}">listing</a>')
    else:
        parts.append('<span class="muted" title="source gave no listing URL">no listing URL</span>')
    parts.append(f'<a href="{_vin_search_url(row["vin"])}" target="_blank" rel="noopener" '
                 f'title="search the web for this VIN">VIN search</a>')
    return '<div class="links">' + "".join(parts) + "</div>"


def _label(row) -> str:
    flags = row["flags"] or ""
    d = row["price_delta_pct"]
    if "VERIFY_PRICE" in flags:
        return '<span class="lbl-verify" title="Too far below market to trust at face value">Verify this</span>'
    if d is None:
        return '<span class="muted">No comparison</span>'
    if d <= -7.5:
        return '<span class="lbl-great">Great deal</span>'
    if d <= -2.5:
        return '<span class="lbl-good">Good deal</span>'
    if d <= 2.5:
        return "Fair price"
    return '<span class="lbl-above">Above market</span>'


def _basis_text(row) -> str:
    b = row["comparison_basis"] or "none"
    if b == "imv":
        return f"vs CarGurus IMV {_money(row['comparison_value'])}"
    if b.startswith("peer_median"):
        level = {"peer_median_trim": "same trim", "peer_median_model_year": "same year",
                 "peer_median_adjacent_years": "±1 year", "peer_median_model": "any year"}.get(b, b)
        return f"vs median {_money(row['comparison_value'])} ({row['peer_count']} {level})"
    return "no basis"


def _bars(row) -> str:
    parts = []
    for key, col in (("price", "price_score"), ("mileage", "mileage_score"), ("dom", "dom_score"),
                     ("price_drop", "price_drop_score"), ("distance", "distance_score")):
        v = row[col] or 0
        parts.append(f'<span class="bar" title="{key}: {v:.0f} (weight {config.SCORE_WEIGHTS[key]})">'
                     f'<i style="width:{max(0, min(100, v)):.0f}%"></i></span>')
    return '<div class="bars">' + "".join(parts) + "</div>"


def _top_table(rows) -> str:
    head = ("<tr><th>#</th><th>Score</th><th>Verdict</th><th>Vehicle</th><th>Trim</th><th>Price</th>"
            "<th>Δ vs basis</th><th>Miles</th><th>Dealer</th><th>Dist (mi)</th><th>Days listed</th>"
            "<th>Flags</th><th>Links</th></tr>")
    body = []
    for r in rows:
        cond = r["condition"] or "used"
        d = r["price_delta_pct"]
        delta_cls = "delta-neg" if (d is not None and d < 0) else ("delta-pos" if d else "")
        delta_txt = f"{d:+.1f}%" if d is not None else "—"
        miles = r["miles"] or 0
        miles_cls = "hi" if miles >= config.MILEAGE_PREFERRED_MAX else ""
        comp = (f"price {r['price_score']:.0f} · miles {r['mileage_score']:.0f} · dom {r['dom_score']:.0f} · "
                f"drop {r['price_drop_score']:.0f} · dist {r['distance_score']:.0f}")
        drop = ""
        if r["price_drop_amount"]:
            drop = f'<br><small class="muted">cut {_money(r["price_drop_amount"])} ({r["price_drop_pct"]:.1f}%)</small>'
        body.append(
            f"<tr>"
            f'<td class="num">{r["rank"]}</td>'
            f'<td data-v="{r["total_score"]}"><span class="score" title="{comp}">{r["total_score"]:.1f}</span>{_bars(r)}</td>'
            f"<td>{_label(r)}</td>"
            f'<td class="veh"><b>{_esc(r["year"])} {_esc(r["make"])} {_esc(r["model"])}</b>'
            f'<span class="badge b-{cond}">{cond.upper()}</span><br><span class="vin">{_esc(r["vin"])}</span></td>'
            f'<td data-v="{_tier_sort(r["trim_tier"])}">{_tier_badge(r["trim_tier"])} {_esc(r["trim"] or "—")}</td>'
            f'<td class="num" data-v="{r["price"]}">{_money(r["price"])}{drop}</td>'
            f'<td data-v="{d if d is not None else 999}"><span class="{delta_cls}">{delta_txt}</span>'
            f'<br><small class="muted">{_basis_text(r)}</small></td>'
            f'<td class="num {miles_cls}" data-v="{miles}">{miles:,}</td>'
            f'<td>{_esc(r["dealer_name"])}<br><small class="muted">{_esc(r["dealer_city"] or "")}'
            f'{", " + _esc(r["dealer_state"]) if r["dealer_state"] else ""}</small></td>'
            f'<td class="num" data-v="{r["distance_miles"] if r["distance_miles"] is not None else 9999}">'
            f'{r["distance_miles"] if r["distance_miles"] is not None else "?"}</td>'
            f'<td class="num" data-v="{r["days_on_market"] if r["days_on_market"] is not None else -1}">'
            f'{r["days_on_market"] if r["days_on_market"] is not None else "?"}</td>'
            f"<td>{_badges(r['flags'])}</td>"
            f"<td>{_links(r)}</td>"
            f"</tr>")
    return f'<div class="tablewrap"><table class="sortable"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>'


def _drops_table(rows) -> str:
    if not rows:
        return '<p class="muted">No price drops since the previous run (or this is the first run).</p>'
    head = ("<tr><th>Vehicle</th><th>Trim</th><th>Was</th><th>Now</th><th>Change</th><th>Previous run</th>"
            "<th>Dealer</th><th>Rank now</th><th>Links</th></tr>")
    body = []
    for r in rows:
        prev = (r["prev_run_started_at"] or "")[:10]
        body.append(
            f'<tr><td class="veh"><b>{_esc(r["year"])} {_esc(r["make"])} {_esc(r["model"])}</b>'
            f'<small>{(r["miles"] or 0):,} mi</small><br><span class="vin">{_esc(r["vin"])}</span></td>'
            f'<td data-v="{_tier_sort(r["trim_tier"])}">{_tier_badge(r["trim_tier"])} {_esc(r["trim"] or "—")}</td>'
            f'<td class="num">{_money(r["prev_price"])}</td><td class="num">{_money(r["price"])}</td>'
            f'<td class="num delta-neg" data-v="{r["change_amount"]}">{_money(r["change_amount"])} ({r["change_pct"]:+.1f}%)</td>'
            f'<td>run {r["prev_run_id"]} · {prev}</td>'
            f'<td>{_esc(r["dealer_name"])}<br><small class="muted">{_esc(r["dealer_city"] or "")}'
            f'{", " + _esc(r["dealer_state"]) if r["dealer_state"] else ""}</small></td>'
            f'<td class="num">{r["rank"] if r["rank"] else "—"}</td><td>{_links(r)}</td></tr>')
    return f'<div class="tablewrap"><table class="sortable"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>'


def build_report(db: Database, run_id: int, top_n: int = config.REPORT_TOP_N,
                 out_path: str | None = None) -> str:
    run = db.run_info(run_id)
    if run is None:
        raise ValueError(f"run {run_id} does not exist")
    rows = db.leaderboard(run_id, top_n)
    drops = db.price_drops_for_run(run_id)
    rejections = json.loads(run["rejections_json"] or "{}")
    notes = json.loads(run["notes"] or "{}")
    per_source = notes.get("per_source", {})
    total_scored = db.query("SELECT COUNT(*) AS n FROM scores WHERE run_id=?", (run_id,))[0]["n"]

    rej_rows = "".join(f"<tr><td>{_esc(k)}</td><td class='num'>{v}</td></tr>"
                       for k, v in sorted(rejections.items(), key=lambda kv: -kv[1]))
    src_rows = "".join(f"<tr><td>{_esc(k)}</td><td class='num'>{v}</td></tr>" for k, v in per_source.items())
    weights = " · ".join(f"{k} {v}" for k, v in config.SCORE_WEIGHTS.items())
    started = (run["started_at"] or "").replace("T", " ").replace("Z", " UTC")

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CarDealFindr · run {run_id}</title><style>{CSS}</style></head>
<body>
<h1>CarDealFindr — 3-row SUV deals within {config.SEARCH_RADIUS_MILES} mi of {config.HOME_ZIP}</h1>
<p class="sub">Run {run_id} · {started} · sources: {_esc(run["sources"])} · budget ≤ {_money(config.BUDGET_CAP)} · miles ≤ {config.MILEAGE_HARD_CAP:,} · used {config.USED_YEAR_MIN}–{config.USED_YEAR_MAX}</p>

<div class="cards">
  <div class="card"><b>{run["raw_listings"] or 0}</b><span>raw listings from all sources</span></div>
  <div class="card"><b>{run["kept_listings"] or 0}</b><span>unique eligible VINs</span></div>
  <div class="card"><b>{total_scored}</b><span>scored</span></div>
  <div class="card"><b>{len(drops)}</b><span>price drops since last run</span></div>
</div>

<h2>Top {min(top_n, len(rows))} deals</h2>
<p class="legend">Click a column header to sort. Hover the score for component values; the five small bars are
price · mileage · days-on-market · price-drop · distance. <b>Verify this</b> = more than {config.PRICE_VERIFY_THRESHOLD_PCT:.0f}% below
IMV/median: usually accident history, a mis-listed trim, or a bait price — check before getting excited.
Red mileage = over {config.MILEAGE_PREFERRED_MAX:,} mi (allowed, not preferred).
Trim tier LOW / MID / HIGH comes from the ladders in <code>config.TRIM_LADDERS</code>; "?" means the trim string
matched nothing on the ladder. <b>Links</b>: "listing" is the URL the source reported; "VIN search" always works.
If links do not respond inside a preview pane, open the file directly in your browser.</p>
{_top_table(rows)}

<h2>Price drops since the previous run</h2>
{_drops_table(drops)}

<h2>Run summary</h2>
<div class="cards">
  <div class="card"><span>listings per source</span><table>{src_rows or "<tr><td class='muted'>none</td></tr>"}</table></div>
  <div class="card"><span>excluded, by reason (per unique VIN)</span><table>{rej_rows or "<tr><td class='muted'>none</td></tr>"}</table></div>
</div>

<h2>How the score is built</h2>
<p class="legend">Weighted average of five 0–100 components (weights: {weights}).
<b>price</b>: 50 at market, 100 at {config.PRICE_FULL_SCORE_PCT:.0f}% below, 0 at {config.PRICE_FULL_SCORE_PCT:.0f}% above; basis is CarGurus IMV when known, else the median of peers in this run.
<b>mileage</b>: vs {config.EXPECTED_MILES_PER_YEAR:,}/yr for the car's age, +{config.MILEAGE_PREFERRED_BONUS:.0f} under {config.MILEAGE_PREFERRED_MAX:,} mi.
<b>days-on-market</b>: {config.DOM_FLOOR_SCORE:.0f} when brand new on the lot, 100 at {config.DOM_NEGOTIATE_DAYS}+ days.
<b>price-drop</b>: {config.PRICE_DROP_NONE_SCORE:.0f} if never cut, {config.PRICE_DROP_BASE_SCORE:.0f}+ once cut.
<b>distance</b>: 100 at home, 0 at {config.SEARCH_RADIUS_MILES} mi.
Everything is in <code>config.py</code>; the numbers behind every row are in the SQLite <code>scores</code> table.</p>
<script>{JS}</script>
</body></html>"""

    os.makedirs(config.REPORT_DIR, exist_ok=True)
    if out_path is None:
        out_path = os.path.join(config.REPORT_DIR, "latest.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    stamped = os.path.join(config.REPORT_DIR, f"run_{run_id:04d}_{datetime.now():%Y%m%d_%H%M}.html")
    if os.path.abspath(stamped) != os.path.abspath(out_path):
        with open(stamped, "w", encoding="utf-8") as fh:
            fh.write(doc)
    return out_path
