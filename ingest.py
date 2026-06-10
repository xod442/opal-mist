"""
Opal — CSV ingest script.

Usage:
    python3 ingest.py engagement.csv

Reads the Microsoft Forms export, skips rows where the current deployed
architecture starts with "Mist", and upserts into opal.db. Running it
again with the same or an updated CSV will never create duplicate entries.
"""

import csv
import sqlite3
import sys
import os

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "opal.db"))

ARCH_COL = "Current deployed Architecture - Not what they want to get to, but what are they running now"

TEMP_ORDER = {
    "Critical - We are at risk of losing them as a customer": 1,
    "Hot - they are escalating": 2,
    "Concerned - they are complaining": 3,
    "Stable - but needs attention": 4,
}

TEMP_LABEL = {
    "Critical - We are at risk of losing them as a customer": "Critical",
    "Hot - they are escalating": "Hot",
    "Concerned - they are complaining": "Concerned",
    "Stable - but needs attention": "Stable",
}


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id                  INTEGER PRIMARY KEY,
            submission_time     TEXT,
            email               TEXT,
            submitter_name      TEXT,
            customer_name       TEXT,
            location            TEXT,
            account_manager     TEXT,
            sales_engineer      TEXT,
            temperature         TEXT,
            temperature_label   TEXT,
            temperature_order   INTEGER,
            at_risk             TEXT,
            risk_reasons        TEXT,
            architecture        TEXT,
            near_term_goals     TEXT,
            bu_contact          TEXT,
            ask_from_bu         TEXT,
            background          TEXT,
            last_modified       TEXT,
            notes               TEXT,
            state               TEXT,
            category            TEXT,
            bu_plm_sponsor      TEXT,
            bu_tme_sponsor      TEXT,
            current_status      TEXT,
            next_actions        TEXT,
            get_well_plan       TEXT
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(customers)").fetchall()]
    for col in ("last_modified", "notes", "state", "category", "bu_plm_sponsor",
                "bu_tme_sponsor", "current_status", "next_actions", "get_well_plan"):
        if col not in cols:
            conn.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT")
    conn.commit()


def ingest(csv_path: str):
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    inserted = skipped_mist = skipped_dup = 0

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            arch = row.get(ARCH_COL, "").strip()
            if arch.lower().startswith("mist"):
                skipped_mist += 1
                continue

            temp = row.get("Customer Temperature", "").strip()

            try:
                conn.execute("""
                    INSERT OR IGNORE INTO customers (
                        id, submission_time, email, submitter_name,
                        customer_name, location, account_manager, sales_engineer,
                        temperature, temperature_label, temperature_order,
                        at_risk, risk_reasons, architecture,
                        near_term_goals, bu_contact, ask_from_bu, background
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    int(row.get("ID", 0)),
                    row.get("Start time", "").strip(),
                    row.get("Email", "").strip(),
                    row.get("Name", "").strip(),
                    row.get("Customer Name", "").strip(),
                    row.get("Location", "").strip(),
                    row.get("Account Manager", "").strip(),
                    row.get("Sales Engineer", "").strip(),
                    temp,
                    TEMP_LABEL.get(temp, temp),
                    TEMP_ORDER.get(temp, 99),
                    row.get("Is the customer actively at risk?", "").strip(),
                    row.get("Primarily reason for risk", "").strip(),
                    arch,
                    row.get("What are the customers near term goals", "").strip(),
                    row.get("Are you currently working with anyone in the business unit?", "").strip(),
                    row.get("Your specific ask of what you would want from the business unit to make this customer happy? ", "").strip(),
                    row.get("Any other background you want us to know", "").strip(),
                ))
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
                else:
                    skipped_dup += 1
            except Exception as e:
                print(f"  Warning: skipped row ID {row.get('ID')} — {e}")

    conn.commit()
    conn.close()

    print(f"Ingest complete: {inserted} inserted, {skipped_dup} duplicates ignored, {skipped_mist} Mist rows skipped")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 ingest.py <path-to-csv>")
        sys.exit(1)
    ingest(sys.argv[1])
