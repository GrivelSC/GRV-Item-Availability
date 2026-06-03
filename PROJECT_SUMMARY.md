# GRV Item Availability — Project Summary

**Last updated:** Jun 3, 2026  
**Status:** Live in production (332 SKUs across 2 locations)  
**URL:** https://grivelsc.github.io/GRV-Item-Availability/

---

## 1. Objective

A standalone web application branded as **GRV Supply Chain Control Tower**, providing company-wide visibility into the current and future net availability of Grivel finished goods across two locations: **VERRAYES** (manufacturing) and **USA-3PL** (distribution). Data is sourced from NetSuite via REST API, computed in Python, and rendered as a static dashboard on GitHub Pages.

---

## 2. Architecture

### Stack
- **Frontend:** Single-file HTML/CSS/JS app with Chart.js (no build step, no framework)
- **Computation engine:** `compute_availability.py` — runs locally on Windows
- **Data source:** NetSuite REST API via OAuth 1.0 TBA (Token-Based Authentication)
- **Refresh script:** `refresh_availability.py` — queries NetSuite, runs engine, outputs JSON
- **Hosting:** GitHub Pages (`GrivelSC/GRV-Item-Availability`, branch `main`, folder `/docs`)

### File Structure

**Local (`C:\GRV-Availability\`)**
| File | Purpose |
|---|---|
| `refresh_availability.py` | Queries NetSuite + runs engine (execute this to refresh) |
| `compute_availability.py` | Computation engine + embedded query definitions (QUERIES dict) |
| `config.json` | FG list, exclusion/embargo lists, settings (source of truth) |
| `credentials.json` | NetSuite OAuth tokens (LOCAL ONLY, never commit to GitHub) |
| `PROJECT_SUMMARY.md` | This document |
| `data/` | Raw query cache (q1.json–q5.json, auto-generated) |
| `docs/` | Files uploaded to GitHub |

**GitHub Pages (`docs/`)**
| File | Purpose | Updated by |
|---|---|---|
| `index.html` | Dashboard UI with charts and admin panel | Developer (rarely) |
| `config.json` | Copy of config for admin panel display | Admin (via app panel) |
| `availability.json` | Computed availability for all FGs × locations | Refresh script |
| `metadata.json` | Freshness timestamp, record counts | Refresh script |

**Claude Project Files**
| File | Purpose |
|---|---|
| `compute_availability.py` | Computation engine (reference for Claude conversations) |
| `config.json` | FG/exclusion/embargo config (reference) |
| `PROJECT_SUMMARY.md` | This document (reference) |

---

## 3. Scope

### Locations
- **VERRAYES** — sub-locations INBOUND (14), MANUFACTURING (15), STORAGE (17), OUTBOUND (3). Excluded: B-GRADE (19), UNSELLABLE (18).
- **USA-3PL** (2) — distribution warehouse, FG only, no manufacturing.

### Subsidiaries
- **Grivel S.r.l (1)** — VERRAYES manufacturing + sales entity
- **Grivel Corp. (3)** — USA-3PL distribution entity
- Intercompany flow: Grivel S.r.l ships to Grivel Corp. via intercompany SO/PO pairs

### Item Scope
- Finished goods defined by an **admin-managed hardcoded list** (CSV upload via app)
- Currently 332 SKUs across all product classes (Crampons, Elements, Ice Axes, etc.)
- Lifecycle attribute pulled from NetSuite (`custitemcustitem_grv_lifecycle` — note the doubled prefix)
- Lifecycle values: Active, Discontinued, New Product, End Of Life
- UI defaults to showing Active items; dropdown to filter by any lifecycle

---

## 4. Algorithm

### Core Principle
The availability engine is **supply-driven**: it first computes the maximum buildable quantity from inventory, then separately nets demand. Demand never enters the BOM explosion — it's a downstream layer.

### VERRAYES — Full Multi-Level BOM Explosion
1. **Inventory snapshot at time t:** On-hand across INBOUND + MANUFACTURING + STORAGE + OUTBOUND, plus all PO receipts with expected receipt date ≤ t
2. **Recursive BOM explosion:** At each BOM level, on-hand subassembly stock is consumed first, reducing demand passed to child components. Leaf nodes (no BOM children) are buying items.
3. **Buildable(t)** = maximum FG units producible given the snapshot at time t
4. **Cumulative Demand(t)** = sum of all open approved SO lines with expected ship date ≤ t
5. **Net Available(t)** = max(0, Buildable(t) − Cumulative Demand(t))

### USA-3PL — FG Only, No BOM
- **No BOM explosion** — this is a distribution warehouse
- **Buildable** = FG on-hand + received intercompany PO quantities
- **Constraint** = nearest intercompany PO (shows PO number, FG name, qty, date)
- **Demand** = open SOs at subsidiary 3

### Time-Phased Staircase
- Event timeline = {today} ∪ {PO receipt dates within horizon} ∪ {ALL SO ship dates regardless of horizon}
- SO dates extend beyond the planning horizon so total demand reconciles with the Open Demand summary field
- At each event date: recompute snapshot → BOM explosion → cumulative demand → net available → identify limiting components
- **Deduplication:** consecutive steps where buildable, demand, and net are unchanged are removed (only steps with actual changes are kept)

### Overdue PO Handling
- POs with expected receipt date in the past are **NOT** counted as received at t=0
- Instead, rescheduled to **today + configurable buffer** (default 7 days)
- Creates a visible staircase step at the rescheduled date
- Buffer configurable via `overdue_po_buffer_days` in config

### Exclusion List
- Admin-managed list of items excluded from BOM constraint calculations
- Excluded items treated as **unconstrained** (infinite supply)
- Loaded via CSV upload in admin panel (internal_id, item_name per row)

### Embargo / Gating List
- Admin-managed list of SKUs with an embargo date
- All computed availability before the embargo date is **suppressed to zero**
- At VERRAYES: embargo applies at the specified date
- At USA-3PL: embargo date + configurable pad (`usa3pl_embargo_pad_days`, default 90 days)
- Past embargo dates are silently inactive (no cleanup needed)

### Manual Overrides (Admin-managed)
- Admin-managed list of FG×location pairs whose availability is **set manually, bypassing all calculation**
- Loaded via CSV upload in the admin panel; stored in `config.json` as `overrides_list`; applied in `compute_availability.py` (`build_override_lookup` + `apply_override`) **after** the normal computation and after Q7 pricing is attached
- CSV format (NO header), 5 columns: `Internal ID, Location, Overridden Today Avail, Overridden Next Avail, Overridden Next Qty`
  - `Location` accepts `Verrayes` / `USA - 3PL` (normalized leniently: case, spaces, hyphens, `·` ignored)
  - `Overridden Next Avail` is a date (`YYYY-MM-DD` or `MM/DD/YYYY`, stored as ISO)
  - A **blank** numeric/date is overridden to **empty (`null`)**, not back-filled from the calculation
- On an overridden record: `available_today`, `next_available_date`, `next_available_qty` take the manual values; `status` is derived (`today>0`→in_stock, else next date present→incoming, else out_of_stock); `embargoed` is forced false; `max_buildable_*`, `committable`, `limiting_components`, `availability_staircase` are blanked; **`total_open_demand` is preserved** from live data
- Prices (`avg_eur_per_unit` / `avg_usd_per_unit`) are preserved so the €/$ toggle can still value overridden quantities
- The UI flags overridden SKUs with a ✎ icon and an amber left edge; the expanded row shows a "calculation bypassed" note instead of a staircase
- Overridden records are excluded from the Intelligence view and the Committable Distribution chart (their computed inputs are intentionally blank)
- `metadata.json` reports `override_count`

### Selling-Price / Value (Q7)
- Q7 returns per-SO-line `unit_rate` and `open_qty` filtered to FGs with `rate > 0`
- The engine computes a **weighted-average price per (item, subsidiary)** in Python: `sum(rate·qty) / sum(qty)`
- Subsidiary 1 → `avg_eur_per_unit` (EUR / Verrayes); subsidiary 3 → `avg_usd_per_unit` (USD / USA-3PL)
- Coverage is **demand-driven**: an FG only has a price if it has at least one open SO line with a positive rate. FGs without open demand have `null` price (a list-price fallback is a future enhancement)
- Used by both the Intelligence view and the dashboard's Units↔Value toggle

### BOM Comma-Separated Parent Handling
- NetSuite's `bom.restrictToAssemblies` field can contain comma-separated item IDs (e.g. "1027, 1570")
- The engine splits on comma and creates tree entries for each parent
- Cannot filter by `restrictToAssemblies` in WHERE clause (causes SuiteQL error)

### Constraint Ranking (Limiting Component Display)
- The Top Constraint column shows which BOM leaf node actually limits FG production
- **Accounts for sub-assembly buffers:** when a sub-assembly has on-hand stock, its child components are effectively protected by that buffer. A leaf with 2 raw OH under a sub-assembly with 129 OH buffer effectively supports 131 FGs — it is NOT the bottleneck
- The ancestor OH boost is accumulated through all intermediate BOM levels so multi-level sub-assemblies are handled correctly
- Validated: PIATE+.GS.SY66 correctly shows 0MAATE.SY48 (3 OH, 0 buffer) as the constraint, not 0CPGSLI.PZ (2 OH, 129 buffer)

---

## 5. Output Fields Per FG × Location

| Field | Description |
|---|---|
| `available_today` | Net available at t=0 (buildable minus demand with ship date ≤ today) |
| `total_open_demand` | Sum of ALL unfulfilled SO qty for this FG, regardless of date |
| `committable` | max(0, max_buildable_at_horizon − total_open_demand) — units available to promise to new orders |
| `max_buildable_today` | Buildable at t=0 (before demand netting) |
| `max_buildable_horizon` | Highest buildable across the entire staircase (after all PO receipts) |
| `limiting_components` | Top 3 constraining BOM nodes with item name, supportable FG units, and nearest PO date/qty |
| `status` | `in_stock` / `incoming` / `out_of_stock` / `embargoed` |
| `availability_staircase` | Full time-phased series of (date, buildable, cumulative demand, net available, limiting components) |
| `avg_eur_per_unit` | Weighted-average EUR selling price (subsidiary 1, from Q7). `null` if no priced open SO line. |
| `avg_usd_per_unit` | Weighted-average USD selling price (subsidiary 3, from Q7). `null` if no priced open SO line. |
| `overridden` | `true` when this FG×location is manually overridden (see §4 Overrides). `false` otherwise. |

When `overridden` is `true`, `available_today`, `next_available_date` and `next_available_qty` carry the manual values (any may be `null` if left blank), `status` is derived from them, `total_open_demand` is preserved from live data, and `max_buildable_today` / `max_buildable_horizon` / `committable` / `limiting_components` / `availability_staircase` are blanked.

---

## 6. NetSuite Queries — Waterfall Cascade

All queries are embedded as constants in `compute_availability.py` under the `QUERIES` dict and executed verbatim by `refresh_availability.py`. The FG list in config.json drives everything downstream — each phase narrows the scope for the next.

### Execution Order

**PHASE 1 — Scope Discovery:**
- Q1: Item master for FG IDs (filtered by `{fg_ids}`)
- Q2a: All BOM parent mappings (4 pages, ~3260 rows)
- → Python identifies FG-level BOM IDs

**PHASE 2 — Iterative BOM Explosion:**
- Q2b round 1: Components for FG BOMs (filtered by `{bom_ids}`)
- → Python checks which children are also parents in Q2a (sub-assemblies)
- Q2b round 2+: Components for sub-assembly BOMs, repeat until leaf nodes
- → Typically 2-3 rounds. Collects all unique item IDs (~344 items)

**PHASE 3 — Supply, Demand & Inventory:**
- Q3: Inventory on-hand (locations 14, 15, 17, 3, 2)
- Q4: Open SOs filtered by `{fg_ids}` (~500 rows vs 11,000+ unfiltered)
- Q5: Open POs including status A, B, D, E
- Q7: Per-SO-line `unit_rate` + `open_qty` (FGs, `rate > 0`) for weighted-average selling price (EUR sub 1 / USD sub 3)

### Query Details

**Q1 — Item Master:** Lifecycle field is `custitemcustitem_grv_lifecycle` (doubled prefix). `item.type` not exposed — use `item.itemtype`.

**Q2a — BOM Mappings:** `restrictToAssemblies` can be comma-separated. Cannot filter by it in WHERE.

**Q2b — BOM Components:** Tables are LOWERCASE: `bomrevision`, `bomrevisioncomponent`. Filtered by `{bom_ids}` and run iteratively to capture multi-level sub-assemblies.

**Q3 — Inventory:** From `inventorybalance`. Same item can appear multiple times per location (lot tracking). Includes negative quantities.

**Q4 — Open SOs:** SO quantities are NEGATIVE. `open_qty = -tl.quantity - NVL(tl.quantityshiprecv, 0)`. Status B (Pending Fulfillment), D (Partially Fulfilled). `t.location` is often NULL — use `t.subsidiary` instead. Filtered by FG IDs.

**Q5 — Open POs:** Status A (Pending Approval), B (Pending Receipt), D (Partially Received), E (Pending Billing). PO quantities are POSITIVE. `quantityreceived` does NOT exist — use `quantityshiprecv`. Status E was discovered through validation — without it, results are wrong.

**Q7 — Selling Price:** Per-SO-line `unit_rate` and `open_qty`, filtered by `{fg_ids}` and `rate > 0`. Row-level (no SQL aggregation); the weighted average per (item, subsidiary) is computed in Python. Optional — if `q7.json` is absent the price fields are `null`.

---

## 7. SuiteQL Gotchas (Learned Through Validation)

1. `item.type` is NOT exposed — use `item.itemtype` instead
2. `quantityfulfilled` / `quantityreceived` don't exist — always use `quantityshiprecv`
3. Status codes are short: `'B'` not `'SalesOrd:B'` or `'PurchOrd:B'`
4. BOM tables are lowercase: `bom`, `bomrevision`, `bomrevisioncomponent`
5. `bom.restrictToAssemblies` can be comma-separated: `"1027, 1570"`
6. Cannot filter `bom` by `restrictToAssemblies` in WHERE (causes "Invalid search")
7. Complex JOINs with subqueries on `item.itemtype` silently return 0 rows
8. SO line quantities are NEGATIVE (inventory debit convention)
9. `t.location` on SO headers is often NULL — use `t.subsidiary` to differentiate locations
10. PO status E has open quantities — omitting it produces fundamentally wrong results
11. Filtering Q2b by a single pass of FG BOMs misses sub-assembly BOMs (must iterate)
12. Constraint ranking must account for intermediate sub-assembly OH buffers, not just raw leaf OH

---

## 8. UI Features

### Dashboard
- Dark theme with custom logo and "GRV Supply Chain Control Tower" branding
- **3 dynamic charts** between filter bar and summary bar (Chart.js):
  - **Top Bottleneck Components:** dual horizontal bar — top 5 by # FGs constrained + top 5 by backlogged demand
  - **Demand Coverage Timeline:** stacked area — Met Demand (green) vs Backlogged Demand (orange)
  - **Committable Distribution:** histogram (0, 1-50, 51-200, 201-500, 500+) colored red→green
- Summary cards with **percentage breakdowns**: Total SKUs, In Stock, Incoming, Out of Stock, Embargoed
- Sortable table with columns: SKU/Name, Class, Status, Avail Today, Open Demand, Committable, Max Build, Next Avail Date, Next Qty, Top Constraint
- **Units ↔ Value toggle** (filter bar): switches the quantity columns (Max Build, Avail Today, Next Qty, Open Demand, Committable) between unit counts and value (unit × ASP), using € on Verrayes and $ on USA-3PL. Cells with no ASP are left blank. Column headers gain a `(€)`/`($)` suffix and sorting becomes value-aware. **Charts, the staircase, and CSV/XLSX downloads always stay in units.**
- Manually overridden SKUs are flagged with a ✎ icon + amber left edge
- Expandable rows showing full availability staircase with mini bar chart (overridden rows show a "calculation bypassed" note)
- "Data as of..." freshness indicator with green pulse dot
- **CSV / XLSX download buttons** — export all data (always in units) as `grv_availability_YYYY-MM-DD.csv/.xlsx`, including an `Overridden` column (`Yes`/blank)

### Filters (all dynamic, charts + table + summary update together)
- **Location toggle:** VERRAYES / USA-3PL (tab switch)
- **Lifecycle dropdown:** Active (default) / All lifecycles / Discontinued / New Product / End Of Life
- **Class dropdown:** All classes / Crampons / Elements / Ice Axes / etc. (dynamically populated)
- **Status dropdown:** All / In Stock / Incoming / Out of Stock / Embargoed
- **Search:** Free text across SKU code and display name

### Admin Panel (⚙ gear icon)
- **FG List:** CSV upload (one internal ID per row, no header). Count displayed.
- **Exclusion List:** CSV upload (internal_id, item_name per row). Current list displayed.
- **Embargo List:** CSV upload (internal_id, embargo_date per row). Current list displayed.
- **Overrides:** CSV upload (NO header; `Internal ID, Location, Today Avail, Next Avail, Next Qty`). Manually sets an FG×location's availability, bypassing calculation. Current list displayed; count shown.
- **Download config.json:** Generates updated config for local save and GitHub upload.

---

## 9. Configuration

```json
{
  "settings": {
    "planning_horizon_days": 120,
    "verrayes_pad_days": 7,
    "usa3pl_pad_days": 7,
    "usa3pl_embargo_pad_days": 90,
    "overdue_po_buffer_days": 7
  },
  "fg_list": [{ "item_id": "6895", "item_name": "" }],
  "exclusion_list": [{ "item_id": "7783", "item_name": "0GUTRN.BLADE", "reason": "..." }],
  "embargo_list": [{ "item_id": "XXXX", "item_name": "...", "embargo_date": "YYYY-MM-DD" }],
  "overrides_list": [{ "item_id": "1116", "location": "verrayes", "today_avail": 50, "next_avail": "2026-07-01", "next_qty": 100 }]
}
```

- **planning_horizon_days (120):** PO receipt dates beyond this window are excluded from the staircase (SO dates always extend to the last open order)
- **verrayes_pad_days (7):** Manufacturing lead time buffer added to availability display dates
- **usa3pl_pad_days (7):** Transit/handling buffer for USA-3PL availability dates
- **usa3pl_embargo_pad_days (90):** Additional pad on embargo dates for USA-3PL (~3 months Italy→US supply chain lead time)
- **overdue_po_buffer_days (7):** Overdue POs rescheduled to today + this many days

---

## 10. Refresh Workflow

### Routine Refresh (no config change)
```
cd C:\GRV-Availability
python refresh_availability.py
copy availability.json docs\
copy metadata.json docs\
```
Upload 2 files from `docs\` to GitHub. App updates within 60 seconds.

### Config Update (FG list, exclusion, embargo, or settings change)
1. Open the app → ⚙ Admin Panel → upload CSV(s) → Download Updated config.json
2. Save config.json to `C:\GRV-Availability\` (replacing old one)
3. Run the routine refresh (above)
4. Also copy config.json to docs\: `copy config.json docs\`
5. Upload 3 files (availability.json, metadata.json, config.json) to GitHub

---

## 11. NetSuite API Setup

- **Integration:** "GRV Availability Refresh" (TBA enabled)
- **Custom role:** "GRV API Access" with permissions: User Access Tokens, Log in using Access Tokens, REST Web Services, SuiteScript, SuiteAnalytics Workbook, Sales Order, Purchase Order, Items, Bill of Materials, Find Transaction, Inventory
- **Access Token:** Davide Pezzia + GRV API Access role
- **Account ID:** 11140593
- **Auth:** OAuth 1.0 with HMAC-SHA256
- **Credentials:** stored in `credentials.json` (LOCAL ONLY)

---

## 12. Deployment

- **Repository:** github.com/GrivelSC/GRV-Item-Availability (public)
- **Hosting:** GitHub Pages, branch `main`, folder `/docs`
- **Upload method:** GitHub web interface (Add file → Upload files)
- **SharePoint integration:** Link/bookmark on company intranet pointing to GitHub Pages URL

---

## 13. Known Limitations (v1)

- **No cross-SKU component allocation:** Each FG evaluated independently. If two FGs share a component, both count its full on-hand. Slightly optimistic.
- **No work-in-progress:** Manufacturing orders/work orders not factored into supply.
- **Manual refresh:** Requires human to run Python script + upload to GitHub.
- **Exclusion list is global:** Same excluded items apply to all FGs. No per-FG exclusion.

---

## 14. Future Enhancements (v2 Candidates)

- **Automated refresh:** GitHub Actions or scheduled task triggers refresh_availability.py + auto-commit
- **Cross-SKU allocation:** Component stock allocated proportionally across FGs by demand weight
- **Work order inclusion:** WIP factored into supply calculations
- **Additional product categories:** Expand FG list beyond current scope
- **Per-FG exclusion rules:** Different exclusion lists per product family
- **Alerting:** Email/Slack notification when a FG drops below a threshold
- **GitHub MCP connector:** Claude pushes directly to GitHub for ad-hoc refreshes
- **ASP fallback for the Value view:** when no open-SO-derived price exists, fall back to a NetSuite list/base price so value coverage approaches 100% (would also let the Value toggle extend to charts and the staircase)
