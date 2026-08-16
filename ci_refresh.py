"""
ci_refresh.py  –  GitHub-Actions version of refresh_dashboard.py

Reads Hazeltree files from  data/  (pushed there by Power Automate),
parses them with the same logic as the local script, injects the
snapshot into the dashboard HTML, and writes the result back.

Git commit/push is handled by the GitHub Actions workflow, not here.

To remove this automation:
  1. Delete this file and .github/workflows/refresh-dashboard.yml
  2. Remove the HTTP steps from your Power Automate flows
  Your local refresh_dashboard.py and bat file are unchanged.
"""

import csv, io, json, os, re, sys
from datetime import datetime
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD  = os.path.join(SCRIPT_DIR, "GCM_LOC_Dashboard.html")
DATA_DIR   = os.path.join(SCRIPT_DIR, "data")

DATA_FILES = {
    "loan_board": os.path.join(DATA_DIR, "loan_board.csv"),
    "payments":   os.path.join(DATA_DIR, "upcoming_payments.txt"),
    "fees":       os.path.join(DATA_DIR, "upcoming_fees.txt"),
}


# ── helpers (identical to refresh_dashboard.py) ──────────────────────

def load_loc_meta(html_text):
    match = re.search(r"const LOC_META = \{(.*?)\};", html_text, re.DOTALL)
    if not match:
        print("ERROR: Could not find LOC_META in dashboard HTML.")
        sys.exit(1)
    body = match.group(1).strip().rstrip(",")
    return json.loads("{" + body + "}")


def parse_money(s):
    if not s or s.strip() == "":
        return 0.0
    s = s.strip().replace('"', "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_date(s):
    if not s or s.strip() == "":
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_acct(raw, loc_meta):
    if not raw:
        return None
    raw = raw.strip()
    if raw in loc_meta:
        return raw
    with_acct = raw + "_ACCT"
    if with_acct in loc_meta:
        return with_acct
    lower_map = {k.lower(): k for k in loc_meta}
    if raw.lower() in lower_map:
        return lower_map[raw.lower()]
    if (raw + "_acct").lower() in lower_map:
        return lower_map[(raw + "_acct").lower()]
    return None


def parse_pct(s):
    if not s or s.strip() == "" or s.strip() == "0.00%":
        return None
    return s.strip()


# ── parsers (identical to refresh_dashboard.py) ──────────────────────

def parse_loan_board(csv_text, loc_meta):
    reader = csv.DictReader(io.StringIO(csv_text))
    account_draws = {}
    for row in reader:
        acct = normalize_acct(row.get("Account", ""), loc_meta)
        if acct is None:
            continue
        meta = loc_meta[acct]
        if meta.get("vertical") != "PEREI":
            continue
        if meta.get("placeholder"):
            continue
        drawn = parse_money(row.get("Drawn Amount Base", "0"))
        draw = {
            "security_code": row.get("Security Code", "").strip(),
            "drawn": drawn,
            "open_date": parse_date(row.get("Open Date", "")),
            "maturity_date": parse_date(row.get("Maturity Date", "")),
            "days_to_maturity": parse_money(row.get("Days to Maturity", "")),
            "itd": int(parse_money(row.get("ITD Period", "0"))),
            "extensions": int(parse_money(row.get("Number of Extensions", "0"))),
            "rate": parse_pct(row.get("Rate", "")),
            "spread": parse_pct(row.get("Spread", "")),
            "benchmark": parse_pct(row.get("Benchmark", "")),
            "floor": parse_pct(row.get("Floor", "")),
            "next_reset": parse_date(row.get("NextResetDateInternal", "")),
            "daily_accrual": parse_money(row.get("Daily Accrual Local", "0")),
            "next_payment": parse_money(row.get("Next Interest Payment Local", "0")),
            "ptd": parse_money(row.get("PTD (Net)", "0")),
            "notes": row.get("Internal Notes", "").strip(),
        }
        if acct not in account_draws:
            account_draws[acct] = {
                "account": acct,
                "portfolio": row.get("Portfolio", "").strip(),
                "commitment": meta.get("commitment", 0),
                "draws": [],
            }
        account_draws[acct]["draws"].append(draw)

    facilities = []
    for acct, fac in account_draws.items():
        draws = fac["draws"]
        total_drawn = sum(d["drawn"] for d in draws)
        commitment = fac["commitment"]
        availability = commitment + total_drawn
        util_pct = round(abs(total_drawn) / commitment * 100, 1) if commitment else 0
        itds = [d["itd"] for d in draws if d["drawn"] != 0]
        oldest_itd = max(itds) if itds else 0
        resets = [d["next_reset"] for d in draws if d["next_reset"] and d["drawn"] != 0]
        earliest_reset = min(resets) if resets else None
        facilities.append({
            "account": acct,
            "portfolio": fac["portfolio"],
            "commitment": float(commitment),
            "total_drawn": round(total_drawn, 2),
            "availability": round(availability, 2),
            "utilization_pct": util_pct,
            "oldest_itd": oldest_itd,
            "earliest_reset": earliest_reset,
            "num_draws": len(draws),
            "draws": draws,
        })
    return facilities


def parse_payments(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")
    payments = []
    for row in rows[1:]:
        cells = [c.get_text(separator=" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 7:
            continue
        if cells[0] == "":
            cells = cells[1:]
        amt_text = cells[5] if len(cells) > 5 else "0"
        drawn_text = cells[6] if len(cells) > 6 else "0"
        ccy = "USD"
        amt_clean = amt_text
        for c in ("USD", "GBP", "EUR", "CAD"):
            if c in amt_text:
                ccy = c
                amt_clean = amt_text.replace(c, "").strip()
                break
        drawn_clean = drawn_text
        for c in ("USD", "GBP", "EUR", "CAD"):
            drawn_clean = drawn_clean.replace(c, "").strip()
        payments.append({
            "agreement": cells[0].strip(),
            "lender": cells[1].strip(),
            "security_code": cells[2].strip(),
            "days_to_accrual": int(parse_money(cells[3])) if cells[3].strip() else 0,
            "reset_date": cells[4].strip(),
            "ccy": ccy,
            "payment_amount": parse_money(amt_clean),
            "drawn_amount": parse_money(drawn_clean),
        })
    return payments


def parse_fees(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")
    fees = []
    for row in rows[1:]:
        cells = [c.get_text(separator=" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 7:
            continue
        if cells[0] == "":
            cells = cells[1:]
        fees.append({
            "agreement": cells[0].strip(),
            "days": int(parse_money(cells[1])) if cells[1].strip() else 0,
            "reset_date": cells[2].strip(),
            "amount": parse_money(cells[3]),
            "unused_amount": parse_money(cells[4]),
            "fee_type": cells[5].strip(),
            "rate_type": cells[6].strip(),
            "rate": cells[7].strip() if len(cells) > 7 else "",
        })
    return fees


# ── snapshot injection (identical to refresh_dashboard.py) ───────────

def inject_snapshot(html_text, facilities, payments, fees, data_date):
    fac_json = json.dumps(facilities, ensure_ascii=False)
    pay_json = json.dumps(payments, ensure_ascii=False)
    fee_json = json.dumps(fees, ensure_ascii=False)

    date_obj = datetime.strptime(data_date, "%Y-%m-%d")
    short_date = date_obj.strftime("%b ") + str(date_obj.day)
    long_date  = date_obj.strftime("%B ") + str(date_obj.day) + ", " + str(date_obj.year)

    html_text = re.sub(
        r"const facilities = \[.*?\];",
        lambda m: "const facilities = " + fac_json + ";",
        html_text, count=1, flags=re.DOTALL,
    )
    html_text = re.sub(
        r"const payments = \[.*?\];",
        lambda m: "const payments = " + pay_json + ";",
        html_text, count=1, flags=re.DOTALL,
    )
    html_text = re.sub(
        r"const fees = \[.*?\];",
        lambda m: "const fees = " + fee_json + ";",
        html_text, count=1, flags=re.DOTALL,
    )
    html_text = re.sub(
        r"state\.dataDate = '[^']*';",
        "state.dataDate = '" + data_date + "';",
        html_text,
    )
    html_text = re.sub(
        r"'Snapshot . [^']*'",
        "'Snapshot \u00b7 " + short_date + "'",
        html_text,
    )
    html_text = re.sub(
        r"'Data as of [^']*'",
        "'Data as of " + long_date + " \u00b7 Hit Refresh to load latest'",
        html_text,
    )
    html_text = re.sub(
        r"// Embedded snapshot from Loan Board LD \S+",
        "// Embedded snapshot from Loan Board LD " + data_date,
        html_text,
    )
    return html_text


# ── main ─────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  PEREI LOC Dashboard - CI Refresh (GitHub Actions)")
    print("=" * 60)
    print()

    # 1. Verify data files exist
    print("[1/4] Checking data files...")
    for name, path in DATA_FILES.items():
        if not os.path.exists(path):
            print(f"ERROR: Missing {name} at {path}")
            sys.exit(1)
        size = os.path.getsize(path)
        print(f"      {os.path.basename(path)}: {size} bytes")

    # 2. Read files
    print("[2/4] Reading files...")
    with open(DATA_FILES["loan_board"], "r", encoding="utf-8") as f:
        loan_csv = f.read()
    with open(DATA_FILES["payments"], "r", encoding="utf-8") as f:
        payments_html = f.read()
    with open(DATA_FILES["fees"], "r", encoding="utf-8") as f:
        fees_html = f.read()

    if not os.path.exists(DASHBOARD):
        print(f"ERROR: Dashboard not found at {DASHBOARD}")
        sys.exit(1)
    with open(DASHBOARD, "r", encoding="utf-8") as f:
        html = f.read()

    # 3. Parse
    print("[3/4] Parsing data...")
    loc_meta = load_loc_meta(html)
    facilities = parse_loan_board(loan_csv, loc_meta)
    payments = parse_payments(payments_html)
    fees = parse_fees(fees_html)

    active = [f for f in facilities if f["total_drawn"] != 0]
    print(f"      {len(facilities)} facilities ({len(active)} with active draws)")
    print(f"      {len(payments)} upcoming payments")
    print(f"      {len(fees)} upcoming fees")

    data_date = datetime.now().strftime("%Y-%m-%d")

    # 4. Inject and write
    print(f"[4/4] Injecting snapshot (date: {data_date})...")
    html = inject_snapshot(html, facilities, payments, fees, data_date)
    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(html)
    print("      Dashboard HTML updated.")

    print()
    print("=" * 60)
    print("  SUCCESS - Dashboard updated. Workflow will commit & push.")
    print("=" * 60)


if __name__ == "__main__":
    main()
