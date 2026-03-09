"""One-time migration: add exit_fail_count column + mark ARRY position ABANDONED.

Usage:
    # Stop service first!
    sudo systemctl stop momentum-agent
    .venv/bin/python -m scripts.fix_arry
    sudo systemctl start momentum-agent
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path("data/momentum.db")
ARRY_POSITION_ID = "a605fa9a063e427d"
ARRY_SYMBOL = "ARRY260320C00012000"


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # 1. Add exit_fail_count column if it doesn't exist
    cursor.execute("PRAGMA table_info(positions)")
    columns = {row[1] for row in cursor.fetchall()}
    if "exit_fail_count" not in columns:
        cursor.execute("ALTER TABLE positions ADD COLUMN exit_fail_count INTEGER DEFAULT 0")
        print("Added exit_fail_count column to positions table")
    else:
        print("exit_fail_count column already exists")

    # 2. Mark ARRY position as ABANDONED
    cursor.execute(
        "SELECT position_id, status, entry_price, quantity FROM positions WHERE position_id = ?",
        (ARRY_POSITION_ID,),
    )
    row = cursor.fetchone()
    if not row:
        print(f"WARNING: Position {ARRY_POSITION_ID} not found — skipping")
    else:
        pos_id, status, entry_price, quantity = row
        if status == "ABANDONED":
            print(f"Position {pos_id} already ABANDONED")
        else:
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                "UPDATE positions SET status = 'ABANDONED', closed_at = ?, exit_fail_count = 10 WHERE position_id = ?",
                (now, ARRY_POSITION_ID),
            )
            print(f"Marked position {pos_id} as ABANDONED")

            # 3. Create TradeLog recording the -100% loss
            pnl_dollars = -entry_price * quantity * 100
            cursor.execute(
                """INSERT INTO trade_log
                   (position_id, ticker, action, entry_price, exit_price, quantity,
                    pnl_dollars, pnl_pct, hold_duration_hours, entry_thesis,
                    exit_reason, opened_at, closed_at)
                   VALUES (?, 'ARRY', 'CALL', ?, 0.0, ?, ?, -100.0, 0.0,
                           'Adopted from broker — orphan position',
                           'ABANDONED — illiquid, no buyers after 10+ exit failures',
                           ?, ?)""",
                (ARRY_POSITION_ID, entry_price, quantity, round(pnl_dollars, 2), now, now),
            )
            print(f"Created TradeLog: P&L ${pnl_dollars:.2f} (-100%)")

    # 4. Delete stuck exit intent
    cursor.execute(
        "DELETE FROM order_intents WHERE idempotency_key = ?",
        (f"exit-{ARRY_POSITION_ID}",),
    )
    deleted = cursor.rowcount
    if deleted:
        print(f"Deleted stuck exit intent (exit-{ARRY_POSITION_ID})")
    else:
        print("No stuck exit intent found")

    conn.commit()
    conn.close()
    print("\nDone. Restart the service: sudo systemctl start momentum-agent")


if __name__ == "__main__":
    main()
