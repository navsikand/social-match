"""Background + on-demand sweep. Read-time expiry is enforced in every query; this cron only
materialises that state (deletes rows and stamps derived ones void). FROZEN interface."""
import asyncio
import contextlib
from server import config, db


def run_sweep(conn=None) -> dict:
    own = conn is None
    if own:
        conn = db.connect()
    now = db.utcnow()
    try:
        counts = {}
        with db.immediate(conn):
            cur = conn.execute(
                "DELETE FROM availability WHERE expires_at <= ?", (now,)
            )
            counts["availability_deleted"] = cur.rowcount
            cur = conn.execute(
                "UPDATE candidates SET state='voided', closed_at=?"
                " WHERE state IN ('open') AND expires_at <= ?",
                (now, now),
            )
            counts["candidates_voided"] = cur.rowcount
            cur = conn.execute(
                "UPDATE proposals SET state='voided'"
                " WHERE state IN ('proposed','a_confirmed') AND expires_at <= ?",
                (now,),
            )
            counts["proposals_voided"] = cur.rowcount
            cur = conn.execute(
                "UPDATE proposals SET state='closed' WHERE state='both_confirmed' AND window_end < ?",
                (now,),
            )
            counts["proposals_closed"] = cur.rowcount
            cur = conn.execute(
                "UPDATE candidates SET state='closed', closed_at=?"
                " WHERE state='matched' AND id IN ("
                "  SELECT candidate_id FROM proposals WHERE state='closed'"
                " )",
                (now,),
            )
            counts["candidates_closed"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM blocks WHERE expires_at <= ?", (now,)
            )
            counts["blocks_deleted"] = cur.rowcount
            # Retention promised by docs/privacy.md: terminal-state candidates — INCLUDING
            # reported/hidden ones (reviewer P1 #8; the reports row keeps what moderation
            # needs) — and swipes are deleted CLOSED_MATCH_DAYS after they closed (P1 #7:
            # the epoch is closed_at, i.e. "30 days after the match closed", not creation).
            cutoff = db.utcnow(days=-config.closed_match_days())
            cur = conn.execute(
                "DELETE FROM candidates WHERE state IN ('closed','voided','declined','hidden')"
                " AND COALESCE(closed_at, created_at) <= ?",
                (cutoff,),
            )
            counts["terminal_candidates_deleted"] = cur.rowcount  # cascades proposals + confirmations
            cur = conn.execute(
                "DELETE FROM swipes WHERE created_at <= ?", (cutoff,)
            )
            counts["swipes_deleted"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM consent_log WHERE purge_after IS NOT NULL AND purge_after <= ?",
                (now,),
            )
            counts["consent_purged"] = cur.rowcount
        return counts
    finally:
        if own:
            conn.close()


async def loop() -> None:
    while True:
        with contextlib.suppress(Exception):
            run_sweep()
        await asyncio.sleep(config.poll_sweep_seconds())
