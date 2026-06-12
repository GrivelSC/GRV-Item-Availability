"""
GRV Item Availability — Local Refresh Script
Connects to NetSuite REST API, runs all queries, computes availability.

SETUP (one-time):
  1. Install dependencies:     pip install requests requests_oauthlib
  2. In NetSuite: Setup → Integrations → New
     - Name: "GRV Availability Refresh"
     - State: Enabled
     - Token-Based Authentication: checked
     - Save → copy Consumer Key + Consumer Secret
  3. In NetSuite: Setup → Users/Roles → Access Tokens → New
     - Application: "GRV Availability Refresh"
     - User: your user
     - Role: your role (must have SuiteQL permissions)
     - Save → copy Token ID + Token Secret
  4. Create credentials.json next to this script (see template below)

USAGE:
  python refresh_availability.py
  python refresh_availability.py --config path/to/config.json

OUTPUT:
  availability.json  — computed availability for all FGs
  metadata.json      — freshness timestamp
  Upload both to GitHub docs/ folder.
"""

import json
import os
import sys
import time
from datetime import date, datetime

# ---------------------------------------------------------------------------
# NetSuite REST API client
# ---------------------------------------------------------------------------

NETSUITE_ACCOUNT_ID = "11140593"
NETSUITE_BASE_URL = f"https://{NETSUITE_ACCOUNT_ID}.suitetalk.api.netsuite.com"
SUITEQL_ENDPOINT = f"{NETSUITE_BASE_URL}/services/rest/query/v1/suiteql"

def load_credentials(path="credentials.json"):
    """
    Load OAuth 1.0 credentials from a JSON file.
    Template:
    {
        "consumer_key":    "your_consumer_key",
        "consumer_secret": "your_consumer_secret",
        "token_key":       "your_token_id",
        "token_secret":    "your_token_secret"
    }
    """
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.")
        print("Create it with your NetSuite OAuth credentials. See script header for setup instructions.")
        sys.exit(1)
    with open(path) as f:
        creds = json.load(f)
    for key in ("consumer_key", "consumer_secret", "token_key", "token_secret"):
        if key not in creds:
            print(f"ERROR: '{key}' missing from {path}")
            sys.exit(1)
    return creds


def create_oauth_session(creds):
    """Create an OAuth 1.0 session for NetSuite REST API."""
    from requests_oauthlib import OAuth1Session
    return OAuth1Session(
        client_key=creds["consumer_key"],
        client_secret=creds["consumer_secret"],
        resource_owner_key=creds["token_key"],
        resource_owner_secret=creds["token_secret"],
        realm=NETSUITE_ACCOUNT_ID,
        signature_method="HMAC-SHA256",
    )


def run_suiteql(session, sql, description="", page_size=1000):
    """
    Execute a SuiteQL query with automatic pagination.
    Returns: list of all result rows.
    """
    all_rows = []
    offset = 0
    total = None

    while True:
        resp = session.post(
            SUITEQL_ENDPOINT,
            headers={
                "Content-Type": "application/json",
                "Prefer": "transient",
            },
            params={"limit": page_size, "offset": offset},
            json={"q": sql},
        )

        if resp.status_code != 200:
            print(f"  ERROR {resp.status_code}: {resp.text[:300]}")
            raise Exception(f"SuiteQL query failed: {resp.status_code}")

        data = resp.json()
        items = data.get("items", [])
        all_rows.extend(items)

        if total is None:
            total = data.get("totalResults", len(items))
            pages = (total + page_size - 1) // page_size
            print(f"  {description}: {total} rows, {pages} pages")

        has_more = data.get("hasMore", False)
        if not has_more or len(items) == 0:
            break

        offset += page_size
        page = offset // page_size
        print(f"    page {page}/{pages}...")
        time.sleep(0.3)  # rate limiting

    return all_rows


# ---------------------------------------------------------------------------
# Query execution — waterfall cascade
# ---------------------------------------------------------------------------

def run_all_queries(session, config):
    """
    Execute the full waterfall query cascade:
      Phase 1: Q1 (item master) + Q2a (BOM mappings)
      Phase 2: Q2b iterative (BOM components, level by level)
      Phase 3: Q3 (inventory) + Q4 (SOs) + Q5 (POs)
    """
    fg_ids = [fg["item_id"] for fg in config.get("fg_list", [])]
    fg_ids_str = ",".join(fg_ids)

    print(f"\n{'='*60}")
    print(f"PHASE 1: Scope Discovery ({len(fg_ids)} FGs)")
    print(f"{'='*60}")

    # Q1 — Item master
    q1 = run_suiteql(session,
        f"SELECT id, itemid AS item_name, NVL(displayname, itemid) AS display_name, "
        f"BUILTIN.DF(class) AS class_name, "
        f"BUILTIN.DF(custitemcustitem_grv_lifecycle) AS lifecycle "
        f"FROM item WHERE id IN ({fg_ids_str}) ORDER BY id",
        description="Q1 Item master")

    # Q2a — All BOM parent mappings
    q2a = run_suiteql(session,
        "SELECT id AS bom_id, restrictToAssemblies AS parent_item_id "
        "FROM bom WHERE isinactive = 'F' ORDER BY id",
        description="Q2a BOM mappings")

    # Build item_to_boms lookup (handling comma-separated parents)
    item_to_boms = {}
    for row in q2a:
        pid_raw = str(row.get("parent_item_id", "") or "").strip()
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

    # Find initial FG-level BOM IDs
    fg_id_set = {int(fid) for fid in fg_ids}
    initial_bom_ids = set()
    for fid in fg_id_set:
        for bid in item_to_boms.get(fid, []):
            initial_bom_ids.add(bid)

    print(f"  → {len(initial_bom_ids)} FG-level BOMs found")

    print(f"\n{'='*60}")
    print(f"PHASE 2: Iterative BOM Explosion")
    print(f"{'='*60}")

    # Q2b — Iterative BOM component explosion
    all_q2b = []
    all_known_bom_ids = set()
    pending_bom_ids = initial_bom_ids.copy()
    bom_round = 0

    while pending_bom_ids:
        bom_round += 1
        bom_ids_str = ",".join(str(b) for b in sorted(pending_bom_ids))
        print(f"\n  Round {bom_round}: {len(pending_bom_ids)} BOMs to fetch")

        q2b_round = run_suiteql(session,
            f"SELECT br.billofmaterials AS bom_id, "
            f"brc.item AS child_item_id, "
            f"BUILTIN.DF(brc.item) AS child_item_name, "
            f"brc.quantity AS qty_per_parent "
            f"FROM bomrevision br "
            f"JOIN bomrevisioncomponent brc ON brc.bomrevision = br.id "
            f"WHERE br.isinactive = 'F' "
            f"AND br.billofmaterials IN ({bom_ids_str}) "
            f"ORDER BY br.billofmaterials, brc.item",
            description=f"Q2b round {bom_round}")

        all_q2b.extend(q2b_round)
        all_known_bom_ids |= pending_bom_ids

        # Discover which children are also parents (sub-assemblies)
        child_ids = {int(row["child_item_id"]) for row in q2b_round}
        pending_bom_ids = set()
        for child_id in child_ids:
            for bom_id in item_to_boms.get(child_id, []):
                if bom_id not in all_known_bom_ids:
                    pending_bom_ids.add(bom_id)

        if pending_bom_ids:
            print(f"  → {len(pending_bom_ids)} sub-assembly BOMs discovered")
        else:
            print(f"  → No more sub-assemblies. BOM tree complete.")

        if bom_round > 10:
            print("  WARNING: depth limit reached, stopping BOM explosion")
            break

    # Collect all unique item IDs from BOM tree
    all_item_ids = fg_id_set.copy()
    for row in all_q2b:
        all_item_ids.add(int(row["child_item_id"]))
    print(f"\n  Total unique items in BOM scope: {len(all_item_ids)}")

    print(f"\n{'='*60}")
    print(f"PHASE 3: Supply, Demand & Inventory")
    print(f"{'='*60}")

    # Q3 — Inventory on-hand
    q3 = run_suiteql(session,
        "SELECT ib.item AS item_id, BUILTIN.DF(ib.item) AS item_name, "
        "ib.location AS location_id, BUILTIN.DF(ib.location) AS location_name, "
        "ib.quantityonhand AS qty_on_hand "
        "FROM inventorybalance ib "
        "WHERE ib.location IN (14, 15, 17, 3, 2) "
        "AND ib.quantityonhand != 0 "
        "ORDER BY ib.item, ib.location",
        description="Q3 Inventory")

    # Q4 — Open SOs (filtered by FG IDs)
    q4 = run_suiteql(session,
        f"SELECT t.id AS so_id, t.tranid AS so_number, "
        f"t.subsidiary AS subsidiary_id, BUILTIN.DF(t.subsidiary) AS subsidiary_name, "
        f"tl.item AS item_id, BUILTIN.DF(tl.item) AS item_name, "
        f"(-tl.quantity) AS ordered_qty, "
        f"NVL(tl.quantityshiprecv, 0) AS shipped_qty, "
        f"(-tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty, "
        f"tl.expectedshipdate AS expected_ship_date "
        f"FROM transaction t "
        f"JOIN transactionline tl ON tl.transaction = t.id "
        f"WHERE t.type = 'SalesOrd' "
        f"AND t.status IN ('A', 'B', 'D') "
        f"AND t.subsidiary IN (1, 3) "
        f"AND tl.mainline = 'F' "
        f"AND tl.quantity < 0 "
        f"AND (-tl.quantity) > NVL(tl.quantityshiprecv, 0) "
        f"AND tl.isclosed = 'F' "
        f"AND tl.item IN ({fg_ids_str}) "
        f"ORDER BY tl.expectedshipdate, t.id",
        description="Q4 Open SOs (FG-filtered, incl. status A=Pending Approval for eComm pre-orders)")

    # Q7 — Unit rate + open qty per SO line for weighted-average price computation.
    # Kept as row-level (no GROUP BY / no aggregate arithmetic) to stay within
    # SuiteQL's supported syntax. Weighted average is computed in Python by
    # compute_availability.py when it loads q7.json.
    q7 = run_suiteql(session,
        f"SELECT tl.item AS item_id, "
        f"t.subsidiary AS subsidiary_id, "
        f"tl.rate AS unit_rate, "
        f"(-tl.quantity - NVL(tl.quantityshiprecv, 0)) AS open_qty "
        f"FROM transaction t "
        f"JOIN transactionline tl ON tl.transaction = t.id "
        f"WHERE t.type = 'SalesOrd' "
        f"AND t.status IN ('A', 'B', 'D') "
        f"AND t.subsidiary IN (1, 3) "
        f"AND tl.mainline = 'F' "
        f"AND tl.quantity < 0 "
        f"AND (-tl.quantity) > NVL(tl.quantityshiprecv, 0) "
        f"AND tl.isclosed = 'F' "
        f"AND tl.item IN ({fg_ids_str}) "
        f"AND tl.rate > 0 "
        f"ORDER BY tl.item, t.subsidiary",
        description="Q7 Unit price per SO line")

    # Q5 — Open POs (including status E!)
    q5 = run_suiteql(session,
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
        "AND t.status IN ('A','B', 'D', 'E') "
        "AND t.subsidiary IN (1, 3) "
        "AND tl.mainline = 'F' "
        "AND tl.isclosed = 'F' "
        "AND tl.quantity > NVL(tl.quantityshiprecv, 0) "
        "ORDER BY tl.expectedreceiptdate, t.id",
        description="Q5 Open POs (incl. status E)")

    return {
        "q1": q1,
        "q2a": q2a,
        "q2b": all_q2b,
        "q3": q3,
        "q4": q4,
        "q5": q5,
        "q7": q7,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Parse args
    config_path = os.path.join(script_dir, "config.json")
    creds_path = os.path.join(script_dir, "credentials.json")
    output_dir = script_dir

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i < len(sys.argv) - 1:
            config_path = sys.argv[i + 1]
        elif arg == "--creds" and i < len(sys.argv) - 1:
            creds_path = sys.argv[i + 1]
        elif arg == "--output" and i < len(sys.argv) - 1:
            output_dir = sys.argv[i + 1]

    print("=" * 60)
    print("GRV Item Availability — Local Refresh")
    print("=" * 60)
    print(f"Config:      {config_path}")
    print(f"Credentials: {creds_path}")
    print(f"Output:      {output_dir}")

    # Load config
    with open(config_path) as f:
        config = json.load(f)
    print(f"FG list:     {len(config.get('fg_list', []))} items")

    # Connect to NetSuite
    creds = load_credentials(creds_path)
    session = create_oauth_session(creds)
    print("Connected to NetSuite REST API")

    # Run all queries
    start = time.time()
    data = run_all_queries(session, config)
    query_time = time.time() - start
    print(f"\nQueries complete in {query_time:.1f}s")

    # Save raw data
    data_dir = os.path.join(output_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    for key, rows in data.items():
        path = os.path.join(data_dir, f"{key}.json")
        with open(path, "w") as f:
            json.dump(rows, f)
        print(f"  Saved {key}.json ({len(rows)} rows)")

    # Import and run computation engine
    # compute_availability.py must be in the same directory
    sys.path.insert(0, script_dir)
    import compute_availability as engine

    output_path = os.path.join(output_dir, "availability.json")
    meta_path = os.path.join(output_dir, "metadata.json")

    print(f"\n{'='*60}")
    print("Computing availability...")
    print(f"{'='*60}")

    result = engine.run(config_path, data_dir, output_path, meta_path)

    total_time = time.time() - start
    print(f"\n{'='*60}")
    print(f"DONE in {total_time:.1f}s")
    print(f"{'='*60}")
    print(f"Output:  {output_path}")
    print(f"         {meta_path}")


if __name__ == "__main__":
    main()
