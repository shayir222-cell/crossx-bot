"""SQLite DB backup daemon.

Runs as a background thread. Every N hours, copies the live SQLite DB
file to a timestamped backup. Keeps last K backups, deletes older.

Safe with WAL mode: uses sqlite3 backup API which produces a consistent
snapshot even while the live DB is being written.

Public API:
    start_backup_daemon(db_path, backup_dir, interval_hours=6, keep_last=7)

Failure mode: any error logged, never raises into trading thread.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone


def _list_backups(backup_dir, prefix):
    if not os.path.isdir(backup_dir):
        return []
    files = [f for f in os.listdir(backup_dir)
             if f.startswith(prefix) and f.endswith('.db')]
    files.sort()
    return [os.path.join(backup_dir, f) for f in files]


def _do_backup(db_path, backup_dir, prefix='crossx_'):
    """Single backup pass. Returns (ok: bool, dest_path: str | None, err: str)."""
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except Exception as e:
        return (False, None, f'mkdir: {e}')
    if not os.path.exists(db_path):
        return (False, None, f'source missing: {db_path}')
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(backup_dir, f'{prefix}{ts}.db')
    try:
        # Online backup via sqlite3 API — safe with WAL/active writers.
        src_conn = sqlite3.connect(db_path, timeout=30)
        dst_conn = sqlite3.connect(dest, timeout=30)
        with dst_conn:
            src_conn.backup(dst_conn, pages=0)  # 0 = whole DB in one pass
        src_conn.close()
        dst_conn.close()
        return (True, dest, '')
    except Exception as e:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        return (False, None, str(e))


def _prune_old(backup_dir, prefix, keep_last):
    """Delete all but the most recent `keep_last` backups."""
    try:
        backups = _list_backups(backup_dir, prefix)
        if len(backups) <= keep_last:
            return 0
        to_delete = backups[:-keep_last]
        for path in to_delete:
            try:
                os.remove(path)
            except Exception:
                pass
        return len(to_delete)
    except Exception:
        return 0


def _backup_loop(db_path, backup_dir, interval_hours, keep_last, logger_module=None):
    interval_sec = max(60, int(interval_hours * 3600))
    prefix = 'crossx_'
    # Run first backup ~30s after start to capture early state.
    time.sleep(30)
    while True:
        ok, dest, err = _do_backup(db_path, backup_dir, prefix)
        pruned = _prune_old(backup_dir, prefix, keep_last)
        try:
            if logger_module:
                if ok:
                    logger_module.log_event('db_backup_ok',
                                             dest=dest, pruned=pruned,
                                             keep_last=keep_last)
                else:
                    logger_module.log_warning('db_backup_failed', error=err)
            else:
                print(f'[BACKUP] {"OK" if ok else "FAIL"} dest={dest} pruned={pruned} err={err}')
        except Exception:
            pass
        time.sleep(interval_sec)


def start_backup_daemon(db_path, backup_dir, interval_hours=6, keep_last=7,
                         logger_module=None):
    """Spawn the backup thread. Returns thread handle."""
    t = threading.Thread(
        target=_backup_loop,
        args=(db_path, backup_dir, interval_hours, keep_last, logger_module),
        daemon=True,
        name='db-backup-daemon',
    )
    t.start()
    return t
