"""Admin CLI for IP blocks.

    docker exec silknet-chatbot python -m app.admin list
    docker exec silknet-chatbot python -m app.admin unblock 1.2.3.4
    docker exec silknet-chatbot python -m app.admin block 1.2.3.4 [minutes]

Changes reach the running server within ~15 seconds.
"""

import sys
import time
from datetime import datetime

from . import db


def main(argv: list[str]) -> int:
    db.init()
    cmd = argv[0] if argv else "list"
    with db.connect() as conn:
        if cmd == "list":
            rows = conn.execute("SELECT * FROM ip_blocks WHERE until > ? ORDER BY until", (time.time(),)).fetchall()
            if not rows:
                print("No blocked IPs.")
            for r in rows:
                print(f"{r['ip']:<40} until {datetime.fromtimestamp(r['until']):%Y-%m-%d %H:%M:%S}  ({r['reason']})")
        elif cmd == "unblock" and len(argv) == 2:
            n = conn.execute("DELETE FROM ip_blocks WHERE ip = ?", (argv[1],)).rowcount
            print(f"Unblocked {argv[1]}" if n else f"{argv[1]} was not blocked")
        elif cmd == "block" and len(argv) in (2, 3):
            minutes = int(argv[2]) if len(argv) == 3 else 60
            conn.execute(
                "INSERT INTO ip_blocks (ip, until, reason) VALUES (?, ?, 'manual') "
                "ON CONFLICT(ip) DO UPDATE SET until = excluded.until, reason = 'manual'",
                (argv[1], time.time() + minutes * 60),
            )
            print(f"Blocked {argv[1]} for {minutes} minutes")
        else:
            print(__doc__)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
