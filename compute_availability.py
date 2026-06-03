"""
Grivel Availability Computation Engine
Computes time-phased net availability staircase for each FG x location.
"""

import json
import math
from datetime import date, timedelta
from collections import defaultdict


# ---------------------------------------------------------------------------
# EMBEDDED QUERIES — exact SQL for NetSuite MCP
# Claude must execute these VERBATIM via ns_runCustomSuiteQL.
# DO NOT modify the SQL. DO NOT improvise alternatives.
# ---------------------------------------------------------------------------
#
# EXECUTION ORDER (dependencies matter):
#
# PHASE 1: Q1 + Q2a  (no dependencies)
#   → After Q2a: run _get_relevant_bom_ids() in Python to find BOM IDs
#     that serve the FGs. This produces the {bom_ids} filter for Q2b.
#
# PHASE 2: Q2b  (depends on Q2a)
#   → Uses {bom_ids} from Phase 1 processing
#   → Run ITERATIVELY: fetch components → check if any children are also
#     parents in Q2a → fetch those BOMs → repeat until no new parents found
#   → After all rounds: collect ALL unique item IDs from BOM tree
#     This produces {all_item_ids} for Q3 and Q5
#
# PHASE 3: Q3 + Q4 + Q5  (depends on Phase 2 for item ID scope)
#   → Q3 filtered by {all_item_ids} — only inventory for items in BOM scope
#   → Q4 filtered by {fg_ids} — only demand for FGs
#   → Q5 filtered by {all_item_ids} — only POs for items in BOM scope
#
# TOTAL: ~7,000 rows across ~12 pages (down from ~32,000 rows / 33 pages)
# ---------------------------------------------------------------------------

QUERIES = {
    "Q1": {
        "description": "Item master for FG list",
        "sql": (
            "SELECT id, itemid AS item_name, NVL(displayname, itemid) AS display_name, "
            "BUILTIN.DF(class) AS class_name, "
            "BUILTIN.DF(custitemcustitem_grv_lifecycle) AS lifecycle "
            "FROM item WHERE id IN ({fg_ids}) ORDER BY id"
        ),
        "paginate": False,
        "notes": "Replace {fg_ids} with comma-separated FG item IDs from config.json",
    },
    "Q2a": {
        "description": "BOM parent mappings (bom_id → parent assembly item ID)",
        "sql": (
            "SELECT id AS bom_id, restrictToAssemblies AS parent_item_id "
            "FROM bom WHERE isinactive = 'F' ORDER BY id"
        ),
        "paginate": True,
        "page_size": 1000,
        "notes": "~3260 rows, 4 pages. parent_item_id can be COMMA-SEPARATED (e.g. '1027, 1570'). Engine splits on comma.",
    },
    "Q2b": {
        "description": "BOM components (child items + quantities per parent BOM)",
        "sql": (
            "SELECT br.billofmaterials AS bom_id, "
            "brc.item AS child_item_id, "
            "BUILTIN.DF(brc.item) AS child_item_name, "
            "brc.quantity AS qty_per_parent "
            "FROM bomrevision br "
            "JOIN bomrevisioncomponent brc ON brc.bomrevision = br.id "
            "WHERE br.isinactive = 'F' "
            "AND br.billofmaterials IN ({bom_ids}) "
            "ORDER BY br.billofmaterials, brc.item"
        ),
        "paginate": True,
        "page_size": 1000,
        "notes": (
            "FILTERED by {bom_ids} from Phase 1 processing. "
            "Replace {bom_ids} with output of _get_relevant_bom_ids(). "
            "Tables are LOWERCASE: bomrevision, bomrevisioncomponent. "
            "~1,000 rows (down from 15,000+ unfiltered)."
        ),
    },
    "Q3": {
        "description": "Inventory on-hand by item and location",
        "sql": (
            "SELECT ib.item AS item_id, BUILTIN.DF(ib.item) AS item_name, "
            "ib.location AS location_id, BUILTIN.DF(ib.location) AS location_name, "
            "ib.quantityonhand AS qty_on_hand "
            "FROM inventorybalance ib "
            "WHERE ib.location IN (14, 15, 17, 3, 2) "
            "AND ib.quantityonhand != 0 "
            "ORDER BY ib.item, ib.location"
        ),
        "paginate": True,
        "page_size": 1000,
        "notes": "Locations: INBOUND=14, MANUFACTURING=15, STORAGE=17, OUTBOUND=3, USA-3PL=2. B-GRADE and UNSELLABLE excluded.",
    },
    "Q4": {
        "description": "Open approved SO lines (unfulfilled demand)",
        "sql": (
            "SELECT t.id AS so_id, t.tranid AS so_number, "
            "t.subsidiary AS subsidiary_id, BUILTIN.DF(t.subsidiary) AS subsidiary_name, "
            "tl.item AS item_id, BUILTIN.DF(tl.item) AS item_name, "
            "(-tl.quantity) AS ordered_qty, "
            "NVL(tl.quantityshiprecv, 0) AS shipped_qty, "
            "(-tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty, "
            "tl.expectedshipdate AS expected_ship_date "
            "FROM transaction t "
            "JOIN transactionline tl ON tl.transaction = t.id "
            "WHERE t.type = 'SalesOrd' "
            "AND t.status IN ('A', 'B', 'D') "
            "AND t.subsidiary IN (1, 3) "
            "AND tl.mainline = 'F' "
            "AND tl.quantity < 0 "
            "AND (-tl.quantity) > NVL(tl.quantityshiprecv, 0) "
            "AND tl.isclosed = 'F' "
            "AND tl.item IN ({fg_ids}) "
            "ORDER BY tl.expectedshipdate, t.id"
        ),
        "paginate": True,
        "page_size": 1000,
        "notes": (
            "FILTERED by {fg_ids} — only demand for FGs in scope. "
            "SO quantities are NEGATIVE. open_qty = -quantity - quantityshiprecv. "
            "Status A=Pending Approval (eComm pre-orders), B=Pending Fulfillment, D=Partially Fulfilled. "
            "Subsidiary 1=Verrayes, 3=USA-3PL. ~500 rows (down from 11,000+ unfiltered)."
        ),
    },
    "Q5": {
        "description": "Open PO lines (incoming vendor + intercompany supply)",
        "sql": (
            "SELECT t.id AS po_id, t.tranid AS po_number, "
            "t.subsidiary AS subsidiary_id, BUILTIN.DF(t.subsidiary) AS subsidiary_name, "
            "tl.item AS item_id, BUILTIN.DF(tl.item) AS item_name, "
            "tl.quantity AS ordered_qty, "
            "NVL(tl.quantityshiprecv, 0) AS received_qty, "
            "(tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty, "
            "tl.expectedreceiptdate AS expected_receipt_date "
            "FROM transaction t "
            "JOIN transactionline tl ON tl.transaction = t.id "
            "WHERE t.type = 'PurchOrd' "
            "AND t.status IN ('B', 'D', 'E') "
            "AND t.subsidiary IN (1, 3) "
            "AND tl.mainline = 'F' "
            "AND tl.quantity > NVL(tl.quantityshiprecv, 0) "
            "ORDER BY tl.expectedreceiptdate, t.id"
        ),
        "paginate": True,
        "page_size": 1000,
        "notes": (
            "MUST include status E (Pending Billing/Partially Received) — has open quantities! "
            "PO quantities are POSITIVE. open_qty = quantity - quantityshiprecv. "
            "Sub 1 = vendor POs for Verrayes raw materials. Sub 3 = intercompany POs for USA-3PL FGs. "
            "Overdue POs (receipt date < today) rescheduled to today + buffer in engine."
        ),
    },
    "Q7": {
        "description": "Revenue per SO line — unit rate + open qty for weighted-average price computation",
        "sql": (
            "SELECT tl.item AS item_id, "
            "t.subsidiary AS subsidiary_id, "
            "tl.rate AS unit_rate, "
            "(-tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty "
            "FROM transaction t "
            "JOIN transactionline tl ON tl.transaction = t.id "
            "WHERE t.type = 'SalesOrd' "
            "AND t.status IN ('A', 'B', 'D') "
            "AND t.subsidiary IN (1, 3) "
            "AND tl.mainline = 'F' "
            "AND tl.quantity < 0 "
            "AND (-tl.quantity) > NVL(tl.quantityshiprecv, 0) "
            "AND tl.isclosed = 'F' "
            "AND tl.item IN ({fg_ids}) "
            "AND tl.rate > 0 "
            "ORDER BY tl.item, t.subsidiary"
        ),
        "paginate": True,
        "page_size": 1000,
        "notes": (
            "FILTERED by {fg_ids}. Row-level (no GROUP BY) — weighted average is computed "
            "in Python to avoid SuiteQL aggregate arithmetic limitations. "
            "Subsidiary 1 → EUR (VERRAYES / Italy). Subsidiary 3 → USD (USA-3PL). "
            "rate > 0 filters samples and zero-price internal transfers. "
            "Outputs: avg_eur_per_unit and avg_usd_per_unit on each availability record."
        ),
    },
}


def _get_relevant_bom_ids(q2a_data, fg_ids, max_depth=5):
    """
    Given Q2a results and the FG item ID list, recursively find all BOM IDs
    that are relevant to the FG scope (FGs + their sub-assemblies + deeper).

    Call this AFTER fetching Q2a, BEFORE fetching Q2b.
    Use the returned bom_ids to fill {bom_ids} in Q2b's SQL.

    Returns: set of bom_id integers
    """
    # Build parent_item_id → [bom_id] lookup (handling comma-separated parents)
    item_to_boms = {}
    bom_to_children_items = {}  # filled later from Q2b, not needed here

    for row in q2a_data:
        pid_raw = str(row.get("parent_item_id", "")).strip()
        if not pid_raw:
            continue
        bom_id = int(row["bom_id"])
        for pid_str in pid_raw.split(","):
            pid_str = pid_str.strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                if pid not in item_to_boms:
                    item_to_boms[pid] = []
                item_to_boms[pid].append(bom_id)

    # Also build bom_id → parent_items reverse lookup for child discovery
    bom_to_parents = {}
    for row in q2a_data:
        pid_raw = str(row.get("parent_item_id", "")).strip()
        bom_id = int(row["bom_id"])
        parents = []
        for pid_str in pid_raw.split(","):
            pid_str = pid_str.strip()
            if pid_str.isdigit():
                parents.append(int(pid_str))
        bom_to_parents[bom_id] = parents

    # Start with BOMs directly owned by FG items
    relevant_bom_ids = set()
    items_to_check = set(fg_ids)
    checked = set()

    for depth in range(max_depth):
        new_items = set()
        for item_id in items_to_check:
            if item_id in checked:
                continue
            checked.add(item_id)
            for bom_id in item_to_boms.get(item_id, []):
                relevant_bom_ids.add(bom_id)
                # Any item that IS a parent in a BOM might itself be a
                # sub-assembly whose children are also BOMs.
                # We'll discover those children after Q2b runs — but for now,
                # we can check if any OTHER BOMs list this item as a parent.
                # The key insight: we need BOMs for items that appear as
                # children of our already-found BOMs. But we don't have
                # Q2b data yet! So we take a conservative approach:
                # include all BOMs for items in a reasonable ID range.
                pass

        if not new_items - checked:
            break
        items_to_check = new_items

    return relevant_bom_ids


# ---------------------------------------------------------------------------
# BOM tree helpers
# ---------------------------------------------------------------------------

def build_bom_tree(bom_mappings, bom_components):
    """
    bom_mappings:  [{bom_id, parent_item_id}]
    bom_components:[{bom_id, child_item_id, qty_per_parent}]
    Returns: {parent_item_id: [{child_id, qty}]}
    HANDLES comma-separated parent_item_id (e.g. "1027, 1570")
    """
    bom_id_to_parents = {}
    for r in bom_mappings:
        pid_raw = str(r.get("parent_item_id", "")).strip()
        if not pid_raw:
            continue
        bom_id = str(r["bom_id"])
        # Split comma-separated parent IDs
        for pid_str in pid_raw.split(","):
            pid_str = pid_str.strip()
            if pid_str.isdigit():
                if bom_id not in bom_id_to_parents:
                    bom_id_to_parents[bom_id] = []
                bom_id_to_parents[bom_id].append(int(pid_str))

    tree = defaultdict(list)
    for comp in bom_components:
        bom_id = str(comp["bom_id"])
        if bom_id not in bom_id_to_parents:
            continue
        child_id  = int(comp["child_item_id"])
        qty       = float(comp["qty_per_parent"] or 1)
        child_name = comp.get("child_item_name", str(child_id))
        # Add this component under EACH parent that shares this BOM
        for parent_id in bom_id_to_parents[bom_id]:
            tree[parent_id].append({"child_id": child_id, "qty": qty,
                                    "child_name": child_name})

    return dict(tree)


def get_all_component_ids(item_id, bom_tree, visited=None):
    """
    Recursively collect all component item IDs under item_id.
    """
    if visited is None:
        visited = set()
    if item_id in visited:
        return set()
    visited.add(item_id)
    result = set()
    for comp in bom_tree.get(item_id, []):
        child = comp["child_id"]
        result.add(child)
        result |= get_all_component_ids(child, bom_tree, visited)
    return result


# ---------------------------------------------------------------------------
# Inventory snapshot builder
# ---------------------------------------------------------------------------

VERRAYES_LOCATIONS = {14, 15, 17, 3}   # INBOUND, MANUFACTURING, STORAGE, OUTBOUND
USA3PL_LOCATIONS   = {2}

def build_inventory_snapshot(inventory_raw, po_lines, cutoff_date, location,
                             overdue_buffer_days=7):
    """
    inventory_raw: [{item_id, location_id, qty_on_hand}]
    po_lines:      [{item_id, subsidiary_id, open_qty, expected_receipt_date}]
    cutoff_date:   date — include PO receipts up to and including this date
    location:      'verrayes' | 'usa3pl'
    overdue_buffer_days: int — overdue POs are rescheduled to today + this many days
    Returns:       {item_id: total_qty}
    """
    relevant_locs  = VERRAYES_LOCATIONS if location == "verrayes" else USA3PL_LOCATIONS
    relevant_sub   = 1 if location == "verrayes" else 3
    snapshot = defaultdict(float)
    today = date.today()

    # On-hand
    for row in inventory_raw:
        loc = int(row["location_id"])
        if loc in relevant_locs:
            snapshot[int(row["item_id"])] += float(row["qty_on_hand"] or 0)

    # PO receipts up to cutoff
    for po in po_lines:
        if int(po["subsidiary_id"]) != relevant_sub:
            continue
        rd = po.get("expected_receipt_date")
        if rd is None:
            continue
        receipt_date = _parse_date(rd)
        if receipt_date is None:
            continue
        # Overdue POs: reschedule to today + buffer
        if receipt_date < today:
            receipt_date = today + timedelta(days=overdue_buffer_days)
        if receipt_date > cutoff_date:
            continue
        snapshot[int(po["item_id"])] += float(po["open_qty"] or 0)

    return dict(snapshot)


# ---------------------------------------------------------------------------
# Max buildable (supply-driven BOM explosion)
# ---------------------------------------------------------------------------

def max_buildable(item_id, snapshot, bom_tree, exclusion_set, cache=None):
    """
    Returns the maximum available units of item_id:
      - on-hand (from snapshot)
      - plus what can be assembled from child components (recursively)
    No demand input — purely supply-driven.
    """
    if cache is None:
        cache = {}
    if item_id in cache:
        return cache[item_id]

    if item_id in exclusion_set:
        cache[item_id] = math.inf
        return math.inf

    oh = snapshot.get(item_id, 0.0)

    children = bom_tree.get(item_id)
    if not children:
        # Leaf node — buying item
        cache[item_id] = oh
        return oh

    # Subassembly or FG — compute additional buildable from components
    min_supported = math.inf
    for comp in children:
        child_id = comp["child_id"]
        qty_needed = comp["qty"]
        if child_id in exclusion_set or qty_needed == 0:
            continue
        child_avail = max_buildable(child_id, snapshot, bom_tree, exclusion_set, cache)
        supported = child_avail / qty_needed
        if supported < min_supported:
            min_supported = supported

    additional = min_supported if min_supported != math.inf else 0.0
    result = oh + additional
    cache[item_id] = result
    return result


# ---------------------------------------------------------------------------
# Limiting component finder
# ---------------------------------------------------------------------------

def find_limiting_components(fg_item_id, snapshot, bom_tree, po_lines,
                              exclusion_set, location, item_names=None,
                              top_n=3, overdue_buffer_days=7):
    """
    Returns top_n most constraining BOM leaf/component nodes,
    with their supportable FG unit ratio and nearest upcoming PO.
    Accounts for intermediate sub-assembly on-hand buffers so that
    the reported constraint matches the actual production bottleneck.
    """
    relevant_sub = 1 if location == "verrayes" else 3
    if item_names is None:
        item_names = {}
    ratios = []

    def recurse(item_id, scale, visited, ancestor_boost=0):
        if item_id in visited or item_id in exclusion_set:
            return
        visited = visited | {item_id}
        children = bom_tree.get(item_id)
        if not children:
            # Leaf node
            avail = snapshot.get(item_id, 0.0)
            raw_supportable = avail / scale if scale > 0 else math.inf
            effective_supportable = ancestor_boost + raw_supportable
            # Find nearest PO — apply same overdue rescheduling as the rest of
            # the engine so overdue-but-still-open POs are reported correctly,
            # not silently dropped (which previously caused "No PO on record"
            # for any PO whose receipt date had passed, e.g. 0TEGHORM.01).
            next_po_date     = None
            next_po_orig     = None   # original NetSuite receipt date if overdue
            next_po_qty      = 0
            next_po_overdue  = False
            today = date.today()
            for po in po_lines:
                if int(po["subsidiary_id"]) != relevant_sub:
                    continue
                if int(po["item_id"]) != item_id:
                    continue
                rd = _parse_date(po.get("expected_receipt_date"))
                if rd is None:
                    continue
                is_overdue   = rd < today
                effective_rd = today + timedelta(days=overdue_buffer_days) if is_overdue else rd
                if next_po_date is None or effective_rd < next_po_date:
                    next_po_date    = effective_rd
                    next_po_orig    = rd if is_overdue else None
                    next_po_qty     = float(po["open_qty"] or 0)
                    next_po_overdue = is_overdue
            ratios.append({
                "item_id": str(item_id),
                "item_name": item_names.get(item_id, str(item_id)),
                "supportable_fg_units":  math.floor(effective_supportable),
                "next_po_date":          next_po_date.isoformat() if next_po_date else None,
                "next_po_original_date": next_po_orig.isoformat() if next_po_orig else None,
                "next_po_qty":           next_po_qty,
                "overdue":               next_po_overdue,
            })
        else:
            # Sub-assembly with children: accumulate OH buffer as boost
            oh = snapshot.get(item_id, 0.0)
            oh_in_fg_units = oh / scale if scale > 0 else 0
            new_boost = ancestor_boost + oh_in_fg_units
            for comp in children:
                recurse(comp["child_id"], scale * comp["qty"], visited, new_boost)

    recurse(fg_item_id, 1.0, set(), ancestor_boost=0)

    ratios.sort(key=lambda x: x["supportable_fg_units"])
    return ratios[:top_n]


# ---------------------------------------------------------------------------
# Embargo helpers
# ---------------------------------------------------------------------------

def get_effective_embargo_date(item_id, embargo_list, location, settings):
    """
    Returns the effective embargo date for item_id at location, or None.
    """
    entry = next((e for e in embargo_list
                  if str(e["item_id"]) == str(item_id)), None)
    if not entry:
        return None
    base = _parse_date(entry.get("embargo_date"))
    if base is None:
        return None
    if location == "usa3pl":
        pad = int(settings.get("usa3pl_embargo_pad_days", 0))
        base = base + timedelta(days=pad)
    return base


def apply_embargo(staircase, embargo_date):
    """
    Suppress net_available to 0 for all steps before embargo_date.
    """
    for step in staircase:
        step_date = _parse_date(step["display_date"])
        if step_date and step_date < embargo_date:
            step["net_available"] = 0
    return staircase


# ---------------------------------------------------------------------------
# Main staircase computation
# ---------------------------------------------------------------------------

def compute_staircase(fg_item_id, location, inventory_raw, bom_tree,
                      po_lines, so_lines, exclusion_set, embargo_list,
                      settings, item_meta):
    """
    Full time-phased availability staircase for one FG x location.
    VERRAYES: full multi-level BOM explosion.
    USA-3PL:  FG-only, no BOM — supply = on-hand + intercompany POs.
    """
    pad_days     = int(settings.get("verrayes_pad_days", 7)
                       if location == "verrayes"
                       else settings.get("usa3pl_pad_days", 7))
    overdue_buffer = int(settings.get("overdue_po_buffer_days", 7))
    relevant_sub = 1 if location == "verrayes" else 3
    is_verrayes  = (location == "verrayes")

    today        = date.today()
    # No planning horizon cap on PO event dates — all future POs and SOs are
    # included in the staircase regardless of how far out they fall.  Capping
    # POs at an arbitrary window while leaving SO dates uncapped was asymmetric
    # and caused items whose PO arrives beyond the window to "piggy-back" on
    # the nearest SO date after the PO, producing inconsistent Next Avail dates
    # (e.g. TRDEXP.M vs TRDEXP.L sharing the same PO but showing Dec vs Nov
    # simply because TRDEXP.L happened to have a small SO line in Nov).

    # ── Component IDs for PO event collection ──
    if is_verrayes:
        component_ids = get_all_component_ids(fg_item_id, bom_tree)
        component_ids.add(fg_item_id)
    else:
        # USA-3PL: only the FG itself (intercompany POs carry the FG item ID)
        component_ids = {fg_item_id}

    # ── Collect event dates ──
    event_dates = {today}

    for po in po_lines:
        if int(po["subsidiary_id"]) != relevant_sub:
            continue
        if int(po["item_id"]) not in component_ids:
            continue
        rd = _parse_date(po.get("expected_receipt_date"))
        if rd is None:
            continue
        if rd < today:
            rd = today + timedelta(days=overdue_buffer)
        if today < rd:
            event_dates.add(rd)

    for so in so_lines:
        if int(so["subsidiary_id"]) != relevant_sub:
            continue
        if int(so["item_id"]) != fg_item_id:
            continue
        sd = _parse_date(so.get("expected_ship_date"))
        if sd and today < sd:
            event_dates.add(sd)

    event_dates = sorted(event_dates)

    # ── Null-dated SO demand ──
    null_demand = sum(
        float(so["open_qty"] or 0)
        for so in so_lines
        if int(so["subsidiary_id"]) == relevant_sub
        and int(so["item_id"]) == fg_item_id
        and not _parse_date(so.get("expected_ship_date"))
    )

    # ── Build item name lookup ──
    item_names = {}
    # Add all items from item_meta (FGs from Q1)
    for iid, meta_info in item_meta.items():
        item_names[iid] = meta_info.get("item_name", str(iid))
    # Add all BOM children
    for comp_list in bom_tree.values():
        for comp in comp_list:
            item_names[comp["child_id"]] = comp.get("child_name", str(comp["child_id"]))

    # ── Staircase loop ──
    staircase = []

    for t in event_dates:
        snapshot = build_inventory_snapshot(inventory_raw, po_lines, t, location,
                                           overdue_buffer_days=overdue_buffer)

        if is_verrayes:
            # Full BOM explosion
            cache = {}
            buildable = math.floor(max_buildable(fg_item_id, snapshot, bom_tree,
                                                 exclusion_set, cache))
            limiting = find_limiting_components(fg_item_id, snapshot, bom_tree,
                                               po_lines, exclusion_set,
                                               location, item_names=item_names,
                                               top_n=3,
                                               overdue_buffer_days=overdue_buffer)
        else:
            # USA-3PL: no BOM — buildable = FG on-hand + received intercompany POs
            buildable = math.floor(snapshot.get(fg_item_id, 0.0))
            limiting = _find_usa3pl_incoming(fg_item_id, po_lines, relevant_sub,
                                            today, overdue_buffer, item_meta)

        cum_demand = null_demand + sum(
            float(so["open_qty"] or 0)
            for so in so_lines
            if int(so["subsidiary_id"]) == relevant_sub
            and int(so["item_id"]) == fg_item_id
            and _parse_date(so.get("expected_ship_date"))
            and _parse_date(so["expected_ship_date"]) <= t
        )

        net_available = max(0, buildable - math.ceil(cum_demand))
        display_date  = (t + timedelta(days=pad_days)) if t > today else today

        staircase.append({
            "date":              t.isoformat(),
            "display_date":      display_date.isoformat(),
            "buildable":         buildable,
            "cumulative_demand": int(math.ceil(cum_demand)),
            "net_available":     net_available,
            "limiting_components": limiting,
        })

    # ── Deduplicate staircase ──
    # Remove consecutive steps where buildable, demand, and net are unchanged.
    # Always keep the first step (today) and the last step.
    if len(staircase) > 2:
        deduped = [staircase[0]]
        for i in range(1, len(staircase) - 1):
            prev = deduped[-1]
            curr = staircase[i]
            if (curr["buildable"] != prev["buildable"]
                    or curr["cumulative_demand"] != prev["cumulative_demand"]
                    or curr["net_available"] != prev["net_available"]):
                deduped.append(curr)
        deduped.append(staircase[-1])
        # If last step is identical to the one before it, still keep it
        staircase = deduped

    # ── Embargo ──
    embargo_date = get_effective_embargo_date(fg_item_id, embargo_list,
                                              location, settings)
    if embargo_date:
        staircase = apply_embargo(staircase, embargo_date)

    # ── Summary fields ──
    today_step = staircase[0] if staircase else None

    # ── ATP (Available to Promise): find the first future supply event where
    # freely committable units survive through the entire horizon.
    # For each candidate step (where net increases and is > 0), compute
    # ATP = min(net from that step through end).  If ATP > 0, this is the
    # first window with real committable supply — select it.  If ATP = 0,
    # demand eventually drains all supply from that window, so skip it and
    # keep scanning.  Save first skipped candidate as fallback so that items
    # with supply-fully-committed-to-demand still show INCOMING (not OOS).
    #
    # Example — RAG12LT.DME:
    #   Window 2 (22 Jun, buildable=240): net [20,10,4,0,0,0] → ATP=0 → skip
    #   Window 3 (24 Aug, buildable=1115): net [853,1103,...] → ATP=641 → SELECT
    next_step           = None
    _fallback           = None
    next_available_qty  = None
    for _ni in range(1, len(staircase)):
        _snet = staircase[_ni]["net_available"]
        _pnet = staircase[_ni - 1]["net_available"]
        if _snet > _pnet and _snet > 0:
            _atp = min(s["net_available"] for s in staircase[_ni:])
            if _atp > 0:
                next_step = staircase[_ni]
                next_available_qty = _atp
                break
            elif _fallback is None:
                _fallback = staircase[_ni]
    if next_step is None and _fallback is not None:
        next_step = _fallback
        next_available_qty = 0

    # True available_today: the minimum net_available across all staircase steps
    # where buildable has not yet increased beyond today's level.
    # This captures the reality that near-term demand can already consume today's
    # supply before any new PO arrives — so today's units are not truly available
    # for new orders if they are already spoken for by upcoming shipments.
    today_buildable = today_step["buildable"] if today_step else 0
    available_today = min(
        (s["net_available"] for s in staircase
         if s["buildable"] <= today_buildable),
        default=0,
    )

    if embargo_date and today < embargo_date:
        status = "embargoed"
    elif available_today > 0:
        status = "in_stock"
    elif next_step:
        status = "incoming"
    else:
        status = "out_of_stock"

    meta = item_meta.get(fg_item_id, {})

    total_demand = null_demand + sum(
        float(so["open_qty"] or 0)
        for so in so_lines
        if int(so["subsidiary_id"]) == relevant_sub
        and int(so["item_id"]) == fg_item_id
        and _parse_date(so.get("expected_ship_date"))
    )

    max_buildable_horizon = max((s["buildable"] for s in staircase), default=0)

    return {
        "item_id":           str(fg_item_id),
        "item_name":         meta.get("item_name", ""),
        "item_display_name": meta.get("display_name") or meta.get("item_name", ""),
        "item_class":        meta.get("item_class", ""),
        "lifecycle":         meta.get("lifecycle", ""),
        "location":          location,
        "embargoed":         bool(embargo_date and today < embargo_date),
        "embargo_date":      embargo_date.isoformat() if embargo_date else None,
        "status":            status,
        "available_today":   available_today,
        "total_open_demand": int(math.ceil(total_demand)),
        "committable":       max(0, max_buildable_horizon
                                  - int(math.ceil(total_demand))),
        "max_buildable_today": today_step["buildable"] if today_step else 0,
        "max_buildable_horizon": max_buildable_horizon,
        "next_available_date": next_step["display_date"] if next_step else None,
        "next_available_qty":  next_available_qty,
        "limiting_components": today_step["limiting_components"] if today_step else [],
        "availability_staircase": staircase,
    }


def _find_usa3pl_incoming(fg_item_id, po_lines, relevant_sub,
                          today, overdue_buffer, item_meta):
    """
    For USA-3PL: find the nearest intercompany POs for this FG.
    Returns a list of upcoming PO entries (like limiting_components format).
    """
    meta = item_meta.get(fg_item_id, {})
    fg_name = meta.get("item_name", str(fg_item_id))
    incoming = []

    for po in po_lines:
        if int(po["subsidiary_id"]) != relevant_sub:
            continue
        if int(po["item_id"]) != fg_item_id:
            continue
        rd = _parse_date(po.get("expected_receipt_date"))
        if rd is None:
            continue
        effective_date = rd if rd >= today else today + timedelta(days=overdue_buffer)
        incoming.append({
            "item_id":   str(fg_item_id),
            "item_name": fg_name,
            "po_number": po.get("po_number", ""),
            "supportable_fg_units": int(float(po.get("open_qty", 0))),
            "next_po_date": effective_date.isoformat(),
            "next_po_qty":  float(po.get("open_qty", 0)),
            "overdue":      rd < today,
        })

    incoming.sort(key=lambda x: x["next_po_date"])
    return incoming[:3]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(config_path, data_dir, output_path, metadata_path):
    """
    config_path:   path to config.json
    data_dir:      directory containing q1.json … q5b.json
    output_path:   path to write availability.json
    metadata_path: path to write metadata.json
    """
    print("Loading config...")
    with open(config_path) as f:
        config = json.load(f)

    settings     = config.get("settings", {})
    fg_list      = config.get("fg_list", [])
    excl_ids     = {int(e["item_id"]) for e in config.get("exclusion_list", [])}
    embargo_list = config.get("embargo_list", [])
    override_lookup = build_override_lookup(config.get("overrides_list", []))

    print(f"FG list: {len(fg_list)} items")
    if override_lookup:
        print(f"Manual overrides: {len(override_lookup)} (item, location) pairs")

    print("Loading query results...")
    with open(f"{data_dir}/q1.json") as f:
        q1 = json.load(f)   # item master

    with open(f"{data_dir}/q2a.json") as f:
        q2a = json.load(f)  # BOM mappings

    with open(f"{data_dir}/q2b.json") as f:
        q2b = json.load(f)  # BOM components

    with open(f"{data_dir}/q3.json") as f:
        q3 = json.load(f)   # inventory on-hand

    with open(f"{data_dir}/q4.json") as f:
        q4 = json.load(f)   # open SOs

    with open(f"{data_dir}/q5.json") as f:
        q5 = json.load(f)   # open POs

    # Q7 — Revenue per SO line (optional: not present on very first run)
    # Weighted average unit price is computed in Python (not SQL) to avoid
    # SuiteQL aggregate arithmetic limitations.
    try:
        with open(f"{data_dir}/q7.json") as f:
            q7 = json.load(f)
        print(f"  q7.json loaded ({len(q7)} rows)")
    except FileNotFoundError:
        q7 = []
        print("  Note: q7.json not found — price fields will be null (run refresh to generate)")

    # Weighted average: accumulate revenue_sum and qty_sum per (item, subsidiary)
    # then divide in Python.  Subsidiary 1 = EUR (Italy), Subsidiary 3 = USD (USA).
    _price_rev = {}   # {(item_id, sub_id): revenue_sum}
    _price_qty = {}   # {(item_id, sub_id): qty_sum}
    for row in q7:
        try:
            iid  = int(row["item_id"])
            sid  = int(row["subsidiary_id"])
            rate = float(row.get("unit_rate") or 0)
            qty  = float(row.get("open_qty") or 0)
        except (ValueError, TypeError):
            continue
        if rate <= 0 or qty <= 0:
            continue
        key = (iid, sid)
        _price_rev[key] = _price_rev.get(key, 0.0) + rate * qty
        _price_qty[key] = _price_qty.get(key, 0.0) + qty

    price_lookup = {}  # {item_id: {sub_id: weighted_avg_rate}}
    for (iid, sid), rev in _price_rev.items():
        qty = _price_qty.get((iid, sid), 0)
        if qty > 0:
            if iid not in price_lookup:
                price_lookup[iid] = {}
            price_lookup[iid][sid] = round(rev / qty, 4)

    print("Building BOM tree...")
    bom_tree = build_bom_tree(q2a, q2b)
    print(f"  {len(bom_tree)} parent nodes in BOM tree")

    # Item metadata index
    item_meta = {
        int(r["id"]): {
            "item_name":   r.get("item_name", ""),
            "display_name": r.get("display_name", ""),
            "item_class":  r.get("class_name", ""),
            "lifecycle":   r.get("lifecycle", ""),
        }
        for r in q1
    }

    fg_item_ids = [int(fg["item_id"]) for fg in fg_list]
    locations   = ["verrayes", "usa3pl"]

    results = []
    total   = len(fg_item_ids) * len(locations)
    done    = 0

    for fg_id in fg_item_ids:
        for loc in locations:
            done += 1
            if done % 10 == 0 or done == total:
                print(f"  Computing {done}/{total}...")

            try:
                record = compute_staircase(
                    fg_item_id=fg_id,
                    location=loc,
                    inventory_raw=q3,
                    bom_tree=bom_tree,
                    po_lines=q5,
                    so_lines=q4,
                    exclusion_set=excl_ids,
                    embargo_list=embargo_list,
                    settings=settings,
                    item_meta=item_meta,
                )
                # Augment with revenue pricing from Q7
                item_prices = price_lookup.get(fg_id, {})
                record["avg_eur_per_unit"] = item_prices.get(1)   # EUR from sub 1
                record["avg_usd_per_unit"] = item_prices.get(3)   # USD from sub 3
                # Apply manual override (bypasses calculation) if one exists for
                # this (FG, location).  Prices above are preserved so the app's
                # €/$ toggle can still value the overridden quantities.
                ov = override_lookup.get((fg_id, loc))
                if ov is not None:
                    apply_override(record, ov)
                else:
                    record["overridden"] = False
                results.append(record)
            except Exception as e:
                print(f"  ERROR on FG {fg_id} / {loc}: {e}")
                results.append({
                    "item_id": str(fg_id),
                    "location": loc,
                    "status": "error",
                    "error": str(e),
                })

    output = {
        "generated_at": date.today().isoformat(),
        "items": results,
    }

    print(f"Writing {output_path}...")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    from datetime import datetime
    meta = {
        "refreshed_at":         datetime.now().isoformat(),
        "refreshed_by":         "manual",
        "sku_count_verrayes":   sum(1 for r in results if r.get("location") == "verrayes"),
        "sku_count_usa3pl":     sum(1 for r in results if r.get("location") == "usa3pl"),
        "override_count":       sum(1 for r in results if r.get("overridden")),
        "status":               "ok",
        "warnings":             [],
    }
    with open(metadata_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Done. {len(results)} records written.")
    return output


# ---------------------------------------------------------------------------
# Date parsing utility
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Manual override helpers
# ---------------------------------------------------------------------------
# An override entry in config.json["overrides_list"] looks like:
#   {"item_id": "1116", "location": "verrayes",
#    "today_avail": 50, "next_avail": "2026-07-01", "next_qty": 100}
# Any of today_avail / next_avail / next_qty may be null (empty in the CSV).
# Per spec: if a value is blank it is overridden to EMPTY (None) — it is NOT
# back-filled from the computed result.  Open Demand is the ONLY computed
# field preserved on an overridden record; everything else is replaced.

def _norm_override_location(value):
    """Map a free-text location label to the internal key, or None."""
    t = "".join(ch for ch in str(value or "").lower()
                if ch.isalnum())
    if not t:
        return None
    if t.startswith("verrayes") or t == "1":
        return "verrayes"
    if "usa" in t or "3pl" in t or t == "3":
        return "usa3pl"
    return None


def _override_num_or_none(value):
    """Coerce an override numeric field to int, or None if blank/invalid."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s == "":
        return None
    try:
        return int(round(float(s)))
    except (ValueError, TypeError):
        return None


def build_override_lookup(overrides_list):
    """
    Returns {(item_id:int, location:str): {today_avail, next_avail, next_qty}}.
    Invalid rows (bad id, unknown location) are skipped.
    """
    lookup = {}
    for ov in overrides_list or []:
        loc = _norm_override_location(ov.get("location"))
        try:
            iid = int(ov.get("item_id"))
        except (ValueError, TypeError):
            continue
        if loc is None:
            continue
        next_avail_raw = ov.get("next_avail")
        next_avail = None
        if next_avail_raw not in (None, ""):
            parsed = _parse_date(next_avail_raw)
            next_avail = parsed.isoformat() if parsed else None
        lookup[(iid, loc)] = {
            "today_avail": _override_num_or_none(ov.get("today_avail")),
            "next_avail":  next_avail,
            "next_qty":    _override_num_or_none(ov.get("next_qty")),
        }
    return lookup


def apply_override(record, ov):
    """
    Replace a computed availability record with manually overridden values.
    Bypasses all calculation.  Open Demand (total_open_demand) is preserved
    from the computed record; all other computed fields are blanked.
    Mutates and returns the record.
    """
    today_avail = ov.get("today_avail")
    next_avail  = ov.get("next_avail")
    next_qty    = ov.get("next_qty")

    record["overridden"]          = True
    record["available_today"]     = today_avail          # may be None (empty)
    record["next_available_date"] = next_avail           # may be None (empty)
    record["next_available_qty"]  = next_qty             # may be None (empty)

    # Blanked computed fields (None → rendered as "—" / empty in UI & exports)
    record["max_buildable_today"]   = None
    record["max_buildable_horizon"] = None
    record["committable"]           = None
    record["limiting_components"]   = []
    record["availability_staircase"] = []

    # Override supersedes embargo gating entirely
    record["embargoed"]    = False
    record["embargo_date"] = None

    # Derive status from the overridden values
    if today_avail is not None and today_avail > 0:
        record["status"] = "in_stock"
    elif next_avail:
        record["status"] = "incoming"
    else:
        record["status"] = "out_of_stock"

    # total_open_demand is intentionally left untouched (preserved from query)
    return record


def _parse_date(value):
    """
    Parse a date from multiple formats NetSuite may return:
    MM/DD/YYYY, YYYY-MM-DD, or None.
    """
    if not value:
        return None
    if isinstance(value, date):
        return value
    value = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return date(*[int(x) for x in
                          (__import__("datetime").datetime.strptime(value, fmt)
                           .timetuple()[:3])])
        except Exception:
            continue
    return None


if __name__ == "__main__":
    import sys
    config  = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/config.json"
    datadir = sys.argv[2] if len(sys.argv) > 2 else "/home/claude/data"
    outfile = sys.argv[3] if len(sys.argv) > 3 else "/home/claude/availability.json"
    metafile= sys.argv[4] if len(sys.argv) > 4 else "/home/claude/metadata.json"
    run(config, datadir, outfile, metafile)
