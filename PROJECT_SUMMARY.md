# GRV Item Availability — Supply Chain Control Tower
## Project Summary & Technical Reference

**Last updated:** Jun 3, 2026  
**Live dashboard:** https://grivelsc.github.io/GRV-Item-Availability/  
**Repository:** GrivelSC/GRV-Item-Availability

---

## 1. Purpose

A web-based ATP/ATS availability dashboard for Grivel's finished-goods catalog.  
Surfaces manufacturing and fulfilment availability across two subsidiaries:

| Code | Subsidiary | Location | Logic |
|---|---|---|---|
| `verrayes` | Grivel S.r.l. Italy (sub 1) | Verrayes facility | Full multi-level BOM explosion |
| `usa3pl` | Grivel Corp. USA (sub 3) | USA-3PL warehouse | FG-only, no BOM; intercompany POs as supply |

**Design philosophy:** *"Availability is an output — not a target. This tool shifts daily focus onto the inputs that produce it: demand signals, procurement discipline, production execution. When those are managed well, availability follows."*

---

## 2. Architecture

```
NetSuite ERP (OAuth 1.0 TBA)
        ↓  SuiteQL  (7 queries)
refresh_availability.py   [runs locally on Windows machine]
        ↓  q1–q5, q7.json
compute_availability.py   [run immediately after]
        ↓  availability.json + metadata.json
GitHub Pages (docs/)      [manual upload]
        ↓  HTTP GET
index.html                [client-side app, no server]
```

**Files:**
- `refresh_availability.py` — NetSuite OAuth session, executes all queries, saves JSON to `data/`
- `compute_availability.py` — loads `data/*.json` + `config.json`, runs computation, writes `availability.json` + `metadata.json`
- `index.html` — single-file dashboard; loads `availability.json` and `config.json` at boot
- `config.json` — source of truth for FG list, exclusion list, embargo list, overrides, settings
- `credentials.json` — OAuth secrets (local only, never committed)

**NetSuite auth:** OAuth 1.0 Token-Based Authentication, account ID `11140593`, role `GRV API Access` (REST Web Services + User Access Tokens + SuiteAnalytics Workbook + transaction/item/BOM permissions)

---

## 3. Configuration (`config.json`)

```json
{
  "settings": {
    "verrayes_pad_days": 7,
    "usa3pl_pad_days": 7,
    "usa3pl_embargo_pad_days": 90,
    "overdue_po_buffer_days": 7
  },
  "fg_list":       [{ "item_id": "6895", "item_name": "" }],
  "exclusion_list":[{ "item_id": "419",  "item_name": "9DP490_ADESEPO", "reason": "" }],
  "embargo_list":  [{ "item_id": "XXXX", "item_name": "...", "embargo_date": "YYYY-MM-DD" }],
  "overrides_list":[{ "item_id": "1116", "location": "verrayes", "today_avail": 50,
                      "next_avail": "2026-07-01", "next_qty": 100 }]
}
```

**Settings explained:**

| Setting | Value | Meaning |
|---|---|---|
| `verrayes_pad_days` | 7 | Days added to PO/event dates in the Verrayes staircase display |
| `usa3pl_pad_days` | 7 | Same for USA-3PL |
| `usa3pl_embargo_pad_days` | 90 | Extra days added to embargo dates at USA-3PL (Italy→USA lead time) |
| `overdue_po_buffer_days` | 7 | Overdue POs (receipt date < today) rescheduled to today + 7 |

**Current counts (as of last refresh):** 316 FGs, 389 exclusions, 0 embargoes, 0 overrides.

**Note:** there is no `planning_horizon_days` setting. The staircase uses all future PO/SO dates with no cap — this was a deliberate design decision to avoid asymmetric handling of PO vs SO dates (see code comment in `compute_staircase`).

---

## 4. Admin Panel (in-app)

Accessed via the lock icon. Allows CSV upload for all four config lists without touching files directly. After changes, click **⬇ Download Updated config.json**, save locally, run a refresh, and upload the outputs to GitHub.

| Section | CSV format | Notes |
|---|---|---|
| FG List | `item_id, item_name` — no header | Drives the entire computation scope |
| Exclusion List | `item_id, item_name, reason` — no header | Items treated as infinite supply (excluded from BOM constraints) |
| Embargo List | `item_id, item_name, embargo_date` — no header | Date format `YYYY-MM-DD`; USA-3PL auto-extended by 90 days |
| Overrides | `Internal ID, Location, Today Avail, Next Avail, Next Qty` — no header | Location = `Verrayes` or `USA - 3PL`; blank field → overridden to empty; see §6 |

---

## 5. NetSuite Queries (Waterfall Cascade)

All queries are embedded in `compute_availability.py` under the `QUERIES` dict and executed verbatim by `refresh_availability.py`. The canonical SQL lives in the QUERIES dict; the inline SQL in `refresh_availability.py` must match it exactly.

### Execution order

```
Phase 1 — Scope Discovery
  Q1   Item master (filtered by {fg_ids})
  Q2a  All active BOM parent mappings (full table, ~3,260 rows)
  → Python builds FG→BOM lookup, identifies initial BOM IDs

Phase 2 — Iterative BOM Explosion
  Q2b  BOM components (filtered by {bom_ids}), repeated until no new sub-assemblies
  → Typically 2–3 rounds; discovers all component item IDs

Phase 3 — Supply, Demand & Inventory
  Q3   Inventory on-hand (filtered by location only — engine filters by item)
  Q4   Open SO lines (filtered by {fg_ids})
  Q5   Open PO lines (all subsidiaries 1 & 3 — engine filters by component)
  Q7   SO-line rate + open qty for pricing (filtered by {fg_ids})
```

### Query details

**Q1 — Item master**
```sql
SELECT id, itemid AS item_name, NVL(displayname, itemid) AS display_name,
  BUILTIN.DF(class) AS class_name,
  BUILTIN.DF(custitemcustitem_grv_lifecycle) AS lifecycle
FROM item WHERE id IN ({fg_ids}) ORDER BY id
```
- Lifecycle field: `custitemcustitem_grv_lifecycle` (doubled prefix — NetSuite quirk)
- Not paginated

**Q2a — BOM parent mappings**
```sql
SELECT id AS bom_id, restrictToAssemblies AS parent_item_id
FROM bom WHERE isinactive = 'F' ORDER BY id
```
- `restrictToAssemblies` can be **comma-separated** (e.g. `"1027, 1570"`) — engine splits on comma
- Cannot filter by `restrictToAssemblies` in WHERE (causes SuiteQL error) — full table scan

**Q2b — BOM components**
```sql
SELECT br.billofmaterials AS bom_id, brc.item AS child_item_id,
  BUILTIN.DF(brc.item) AS child_item_name, brc.quantity AS qty_per_parent
FROM bomrevision br
JOIN bomrevisioncomponent brc ON brc.bomrevision = br.id
WHERE br.isinactive = 'F' AND br.billofmaterials IN ({bom_ids})
ORDER BY br.billofmaterials, brc.item
```
- Tables are **lowercase**: `bomrevision`, `bomrevisioncomponent`
- Run iteratively: after each round, check which children are also parents in Q2a → fetch their BOMs next round → repeat until no new sub-assemblies found

**Q3 — Inventory on-hand**
```sql
SELECT ib.item AS item_id, BUILTIN.DF(ib.item) AS item_name,
  ib.location AS location_id, BUILTIN.DF(ib.location) AS location_name,
  ib.quantityonhand AS qty_on_hand
FROM inventorybalance ib
WHERE ib.location IN (14, 15, 17, 3, 2) AND ib.quantityonhand != 0
ORDER BY ib.item, ib.location
```
- Location IDs: 14=INBOUND, 15=MANUFACTURING, 17=STORAGE/OUTBOUND, 3=OUTBOUND, 2=USA-3PL
- No item filter in SQL — engine filters when building snapshots
- Verrayes locations: {14, 15, 17, 3}; USA-3PL: {2}
- Includes negative quantities (surface data-quality issues)

**Q4 — Open SO lines (demand)**
```sql
SELECT t.id AS so_id, t.tranid AS so_number,
  t.subsidiary AS subsidiary_id, BUILTIN.DF(t.subsidiary) AS subsidiary_name,
  tl.item AS item_id, BUILTIN.DF(tl.item) AS item_name,
  (-tl.quantity) AS ordered_qty,
  NVL(tl.quantityshiprecv, 0) AS shipped_qty,
  (-tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty,
  tl.expectedshipdate AS expected_ship_date
FROM transaction t
JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'SalesOrd'
  AND t.status IN ('A', 'B', 'D')
  AND t.subsidiary IN (1, 3)
  AND tl.mainline = 'F'
  AND tl.quantity < 0
  AND (-tl.quantity) > NVL(tl.quantityshiprecv, 0)
  AND tl.isclosed = 'F'
  AND tl.item IN ({fg_ids})
ORDER BY tl.expectedshipdate, t.id
```
- SO quantities are **NEGATIVE** in NetSuite; `open_qty = -quantity - quantityshiprecv`
- Status: **A=Pending Approval** (eComm pre-orders — real commitments), **B=Pending Fulfillment**, **D=Partially Fulfilled**
- `t.location` is often NULL on SO headers — use `t.subsidiary` to identify location
- `quantityfulfilled` does not exist in SuiteQL — use `quantityshiprecv`
- Filtered by `{fg_ids}` (~500 rows vs 11,000+ unfiltered)

**Q5 — Open PO lines (supply)**
```sql
SELECT t.id AS po_id, t.tranid AS po_number,
  t.subsidiary AS subsidiary_id, BUILTIN.DF(t.subsidiary) AS subsidiary_name,
  tl.item AS item_id, BUILTIN.DF(tl.item) AS item_name,
  tl.quantity AS ordered_qty,
  NVL(tl.quantityshiprecv, 0) AS received_qty,
  (tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty,
  tl.expectedreceiptdate AS expected_receipt_date
FROM transaction t
JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'PurchOrd'
  AND t.status IN ('A', 'B', 'D', 'E')
  AND t.subsidiary IN (1, 3)
  AND tl.mainline = 'F'
  AND tl.quantity > NVL(tl.quantityshiprecv, 0)
ORDER BY tl.expectedreceiptdate, t.id
```
- Status: **A=Pending Approval**, **B=Pending Receipt**, **D=Partially Received**, **E=Pending Billing** — all four must be included
  - Status E was discovered through validation to carry open quantities; omitting it causes wrong results
  - Status A added to include vendor POs that have not yet cleared approval
- PO quantities are **POSITIVE**
- No item filter in SQL — engine filters by component ID when building snapshots

**Q7 — Selling price (weighted-average ASP)**
```sql
SELECT tl.item AS item_id, t.subsidiary AS subsidiary_id,
  tl.rate AS unit_rate,
  (-tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty
FROM transaction t
JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'SalesOrd'
  AND t.status IN ('A', 'B', 'D')
  AND t.subsidiary IN (1, 3)
  AND tl.mainline = 'F'
  AND tl.quantity < 0
  AND (-tl.quantity) > NVL(tl.quantityshiprecv, 0)
  AND tl.isclosed = 'F'
  AND tl.item IN ({fg_ids})
  AND tl.rate > 0
ORDER BY tl.item, t.subsidiary
```
- Row-level (no SQL GROUP BY) — weighted average computed in Python: `sum(rate×qty) / sum(qty)` per `(item_id, subsidiary_id)`
- `rate > 0` excludes samples and zero-price internal transfers
- Subsidiary 1 → `avg_eur_per_unit`; subsidiary 3 → `avg_usd_per_unit`
- Coverage is demand-driven: only FGs with at least one open SO line with a positive rate have an ASP. FGs with no open demand have `null` price
- Optional: if `q7.json` is absent, price fields are `null` throughout

---

## 6. Computation Logic (`compute_availability.py`)

### BOM explosion and max_buildable

- `build_bom_tree(q2a, q2b)` → `{parent_item_id: [{child_id, qty, child_name}]}`
- `max_buildable(item_id, snapshot, bom_tree, exclusion_set)` — recursive supply-driven: `OH + floor(min over children of child_avail / qty_needed)`. Exclusion set items treated as infinite supply.
- Called once per (FG, staircase date) — snapshot includes OH + all POs received up to that date

### Staircase construction

1. Collect all unique event dates: `{today} ∪ {effective PO dates} ∪ {SO expected ship dates}` (no date cap — all future events included)
2. Overdue POs (receipt date < today) rescheduled to `today + overdue_po_buffer_days` (default 7)
3. For each event date `t`: build snapshot, compute buildable, compute cumulative demand (null-dated SO demand added to every step)
4. `net_available = max(0, buildable − ceil(cumulative_demand))`
5. `display_date = t + pad_days` (if t > today, else today)
6. Deduplicate: remove consecutive steps with no change in buildable, cumulative_demand, or net_available (always keep first and last)
7. Apply embargo: suppress net_available to 0 for steps before effective embargo date

### USA-3PL handling

- No BOM explosion: `buildable = floor(FG on-hand at location 2)`
- Supply events: only POs where `subsidiary_id == 3` and `item_id == fg_item_id`
- `limiting_components` replaced by `_find_usa3pl_incoming()` — nearest intercompany PO(s) for the FG

### Summary fields (per FG × location)

`available_today` — minimum `net_available` across all staircase steps where `buildable ≤ today_buildable`. This looks ahead through near-term demand to prevent false "in_stock" when upcoming SOs consume today's supply before the next PO arrives.

`next_available_date` / `next_available_qty` — from the first future staircase step where `net_available > net_previous` and `min(net from that step to end) > 0`. Fallback to first net-improving step even if ATP = 0 (ensures INCOMING status is shown when supply arrives but demand fully absorbs it).

`total_open_demand` — `sum(open_qty)` across all SO lines for this FG and subsidiary (null-dated + future-dated). Ceiling-rounded.

`committable` — `max(0, max_buildable_horizon − total_open_demand)`, where `max_buildable_horizon` is the highest buildable across the entire (uncapped) staircase.

`status` derivation:
```
if embargo_date and today < embargo_date → "embargoed"
elif available_today > 0               → "in_stock"
elif next_step exists                  → "incoming"
else                                   → "out_of_stock"
```

### Manual overrides

- Loaded from `config.json["overrides_list"]` via `build_override_lookup()`, applied in `run()` **after** price augmentation (so prices are preserved for the €/$ toggle)
- Per `(item_id, location)` pair: replaces `available_today`, `next_available_date`, `next_available_qty`; derives status; blanks `max_buildable_today/horizon`, `committable`, `limiting_components`, `availability_staircase`; forces `embargoed=False`
- **`total_open_demand` is preserved** (from live data)
- Blank CSV field → field overridden to `null` (not back-filled from computation)
- `overridden=True` flag written to record; `metadata.json` reports `override_count`

### Embargo handling

- `get_effective_embargo_date()`: looks up item in `embargo_list`; adds `usa3pl_embargo_pad_days` (90) for USA-3PL
- `apply_embargo()`: sets `net_available = 0` for all staircase steps with `step_date < embargo_date`

### Pricing (Q7)

- Weighted average: `sum(rate × open_qty) / sum(open_qty)` per `(item_id, subsidiary_id)`
- `avg_eur_per_unit` (sub 1), `avg_usd_per_unit` (sub 3) written to every record
- `null` when no Q7 rows exist for that item, or when `q7.json` is absent (first run)

### Exclusion list

Items in `exclusion_list` are treated as **infinite supply** in `max_buildable()`. This means they do not constrain any FG's buildable count and never appear as a top constraint.

---

## 7. Output Fields (per FG × location, in `availability.json`)

| Field | Type | Description |
|---|---|---|
| `item_id` | string | NetSuite internal ID |
| `item_name` | string | NetSuite item code (e.g. `RAATLT.DME`) |
| `item_display_name` | string | Display name or item code if blank |
| `item_class` | string | Product class (e.g. `Crampons`) |
| `lifecycle` | string | `Active`, `New Product`, `Discontinued`, `End Of Life`, etc. |
| `location` | string | `verrayes` or `usa3pl` |
| `embargoed` | bool | True if today < effective embargo date |
| `embargo_date` | string\|null | Effective embargo date (ISO, with USA-3PL pad applied) |
| `status` | string | `in_stock` / `incoming` / `out_of_stock` / `embargoed` |
| `available_today` | int\|null | Net units available today (look-ahead adjusted); null if overridden to empty |
| `total_open_demand` | int | All open SO qty for this FG (preserved even on overridden records) |
| `committable` | int\|null | `max(0, max_buildable_horizon − total_open_demand)`; null if overridden |
| `max_buildable_today` | int\|null | Buildable at t=0 before demand netting; null if overridden |
| `max_buildable_horizon` | int\|null | Peak buildable across full staircase; null if overridden |
| `next_available_date` | string\|null | Display date of first future supply event with real ATP (ISO); null if overridden to empty |
| `next_available_qty` | int\|null | Net units at `next_available_date`; null if overridden to empty |
| `limiting_components` | array | Top 3 constraining BOM nodes (item_name, supportable_fg_units, next_po_date, overdue flag); empty if overridden |
| `availability_staircase` | array | Time-phased steps: date, display_date, buildable, cumulative_demand, net_available, limiting_components; empty if overridden |
| `avg_eur_per_unit` | float\|null | Weighted-average EUR selling price (from Q7, sub 1); null if no priced SO |
| `avg_usd_per_unit` | float\|null | Weighted-average USD selling price (from Q7, sub 3); null if no priced SO |
| `overridden` | bool | True if manual override applied for this FG × location |

---

## 8. SuiteQL Gotchas

1. `item.type` is not exposed in SuiteQL — use `item.itemtype` instead
2. `quantityfulfilled` and `quantityreceived` do not exist — always use `quantityshiprecv`
3. Status codes are short-form: `'B'` not `'SalesOrd:B'` or `'PurchOrd:B'`
4. BOM tables are lowercase: `bom`, `bomrevision`, `bomrevisioncomponent`
5. `bom.restrictToAssemblies` can be comma-separated: `"1027, 1570"` — split in Python
6. Cannot filter `bom` by `restrictToAssemblies` in WHERE — causes "Invalid search" error; full table scan required
7. Complex JOINs with subqueries on `item.itemtype` silently return 0 rows
8. SO line quantities are **NEGATIVE** (inventory debit convention)
9. `t.location` on SO headers is often NULL — use `t.subsidiary` to differentiate locations
10. PO status E (Pending Billing) carries open quantities — validated on real data; omitting it produces fundamentally wrong results
11. PO status A (Pending Approval) must also be included for both SOs and POs — eComm pre-orders and vendor POs in approval represent real supply/demand
12. Q2b must be fetched iteratively (multi-pass) — a single pass misses sub-assembly BOMs and causes incorrect OOS flags
13. Constraint ranking must account for intermediate sub-assembly OH buffers (see `find_limiting_components` docstring)
14. BOM crampon FG IDs cluster around ranges 2972–2993 and 2994–3015+; deeper sub-assembly BOMs around 3143–3144+
15. `BUILTIN.DF()` resolves display names for FK fields; `pageIndex` (0-based) + `pageSize` used for pagination

---

## 9. Dashboard UI (`index.html`)

Single-file app, no build step. External dependencies: Chart.js 4.4.1 (CDN), SheetJS (local `xlsx.full.min.js`).

**Views:**
- **Availability** — main table + summary cards + 3 charts
- **Intelligence** — auto-generated supply chain signals (Demand Shaping, Vendor OpEx, Overstock)

**Filter bar:** Location tabs (VERRAYES / USA-3PL), Lifecycle, Class, Status, Search, **Units/Value toggle**, CSV, XLSX

**Summary cards:** Total SKUs, In Stock (%), Incoming (%), Out of Stock (%), Embargoed (%)

**Charts:**
- Top Bottleneck Components (dual bar: FG count + backlog units)
- Demand Coverage Timeline (stacked area: met demand vs backlogged demand, from staircases)
- Committable Distribution (histogram by bucket: 0 / 1–50 / 51–200 / 201–500 / 500+; overridden SKUs excluded)

**Table columns:** SKU/Name, Class, Status badge, Max Build Today, Avail Today, Next Avail, Next Qty, Open Demand, Committable, Top Constraint, Expand button. All quantity columns (Max Build, Avail Today, Next Qty, Open Demand, Committable) switch to monetary value when the Units/Value toggle is active (€ on VERRAYES, $ on USA-3PL); column headers gain a `(€)` / `($)` suffix; sorting is value-aware. Overridden SKUs: ✎ icon + amber left edge; expanded row shows "calculation bypassed" note.

**CSV / XLSX export:** Always in units regardless of toggle. Exports all items across both locations. Includes `Overridden` column (Yes/blank). Overridden-blanked fields export as empty cells.

**Intelligence view** (`generateIntel`): excludes overridden records (their computed inputs are intentionally blank). Uses `avg_eur_per_unit` / `avg_usd_per_unit` for revenue weighting. Three sections: Demand Shaping (push/prepare/watch/hold signals), Vendor OpEx (component PO alerts + intercompany transfer gaps for USA-3PL), Overstock (weeks-of-cover analysis with EOL flag).

---

## 10. Refresh Workflow

### Routine refresh (data only, no config change)
1. Run `python refresh_availability.py credentials.json config.json data/`
2. Run `python compute_availability.py config.json data/ docs/availability.json docs/metadata.json`
3. Upload `docs/availability.json` and `docs/metadata.json` to GitHub

### Config update (FG list / exclusion / embargo / overrides / settings)
1. In the app Admin Panel: upload relevant CSV(s)
2. Click **⬇ Download Updated config.json** → save to local `C:\GRV-Availability\`
3. Run refresh + compute (as above)
4. Upload `docs/availability.json`, `docs/metadata.json`, **and** `docs/config.json` to GitHub

GitHub Pages serves files within ~60 seconds of upload.

---

## 11. Known Constraints and Future Work

- **ASP coverage is demand-driven**: `avg_eur_per_unit` / `avg_usd_per_unit` are null for any FG without a priced open SO line. A list-price fallback (NetSuite base price field) would extend value-view coverage to near 100% and unlock the €/$ toggle on charts and the staircase
- **Staircase horizon is uncapped**: `committable` includes supply from POs arbitrarily far in the future. Adding a configurable `planning_horizon_days` cap to the committable computation would make the metric more conservative and operationally meaningful
- **Refresh is manual**: a GitHub Actions trigger or a scheduled task on the local machine could automate it
- **GitHub MCP connector**: Claude could push directly to GitHub for ad-hoc refreshes
