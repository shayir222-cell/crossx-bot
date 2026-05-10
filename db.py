"""SQLite persistence layer for CrossX Bot.

Design:
- Single shared connection (WAL + autocommit), threading.Lock for serialized writes.
- All public functions are exception-safe: a DB failure must NEVER crash the bot.
  On failure they log + return a safe default (None / empty list / empty dict).
- Schema is created on init_db() if missing — backwards compatible (new columns
  require manual migration; we don't have any yet).
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

_conn = None
_path = None
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id           INTEGER PRIMARY KEY,
  symbol       TEXT,
  date         TEXT,
  time         TEXT,
  side         TEXT,
  entry        REAL,
  exit         REAL,
  size         REAL,
  score        INTEGER,
  session      TEXT,
  atr          REAL,
  sl           REAL,
  tp1_hit      INTEGER,
  tp2_hit      INTEGER,
  peak_pnl     REAL,
  pnl_pct      REAL,
  won          INTEGER,
  exit_reason  TEXT,
  duration_min INTEGER,
  ts_utc       TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);

CREATE TABLE IF NOT EXISTS signals (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc    TEXT,
  date      TEXT,
  time      TEXT,
  symbol    TEXT,
  action    TEXT,
  score     INTEGER,
  breakdown TEXT,
  status    TEXT,
  reason    TEXT,
  session   TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);

CREATE TABLE IF NOT EXISTS metrics (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc       TEXT,
  symbol       TEXT,
  side         TEXT,
  latency_ms   INTEGER,
  slippage_pct REAL,
  requested    REAL,
  filled       REAL,
  status       TEXT,
  reason       TEXT
);

CREATE TABLE IF NOT EXISTS positions (
  symbol     TEXT PRIMARY KEY,
  active     INTEGER,
  side       TEXT,
  entry      REAL,
  size       REAL,
  remaining  REAL,
  sl         REAL,
  atr        REAL,
  r          REAL,
  tp1_hit    INTEGER,
  tp2_hit    INTEGER,
  peak_pnl   REAL,
  trail_sl   REAL,
  entry_time TEXT,
  ref_price  REAL,
  ref_time   TEXT,
  score      INTEGER,
  session    TEXT,
  be_set     INTEGER,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS state_global (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS state_symbol (
  symbol              TEXT PRIMARY KEY,
  win_streak          INTEGER,
  loss_streak         INTEGER,
  pause_until         TEXT,
  sl_cooldown_until   TEXT,
  pair_cooldown_until TEXT
);

-- ── Analytics tables (Phase 2 observability) ────────────────────
-- These tables are ADDITIVE. They never alter execution behavior.
-- Purpose: research/replay infrastructure with rich per-event context.

CREATE TABLE IF NOT EXISTS analytics_signals (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc             TEXT,
  symbol             TEXT,
  side               TEXT,
  action             TEXT,
  status             TEXT,
  reason             TEXT,
  session            TEXT,
  score              INTEGER,
  breakdown          TEXT,
  rsi                REAL,
  atr                REAL,
  atr_pct            REAL,
  volatility_state   TEXT,
  loss_streak        INTEGER,
  win_streak         INTEGER,
  pair_cooldown_left INTEGER,
  global_cooldown_on INTEGER,
  hour_count         INTEGER,
  session_count      INTEGER,
  daily_pnl_pct      REAL
);
CREATE INDEX IF NOT EXISTS idx_asignals_ts ON analytics_signals(ts_utc);
CREATE INDEX IF NOT EXISTS idx_asignals_symbol ON analytics_signals(symbol);

CREATE TABLE IF NOT EXISTS analytics_trades (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id           INTEGER,
  ts_open_utc        TEXT,
  ts_close_utc       TEXT,
  symbol             TEXT,
  side               TEXT,
  entry              REAL,
  exit               REAL,
  size               REAL,
  score              INTEGER,
  session            TEXT,
  atr                REAL,
  sl                 REAL,
  tp1_hit            INTEGER,
  tp2_hit            INTEGER,
  be_set             INTEGER,
  peak_pnl_pct       REAL,
  pnl_pct            REAL,
  won                INTEGER,
  exit_reason        TEXT,
  duration_min       INTEGER,
  execution_latency_ms INTEGER,
  loss_streak_at_open  INTEGER,
  daily_pnl_at_open    REAL
);
CREATE INDEX IF NOT EXISTS idx_atrades_symbol ON analytics_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_atrades_close ON analytics_trades(ts_close_utc);

CREATE TABLE IF NOT EXISTS analytics_alerts (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc    TEXT,
  severity  TEXT,
  msg       TEXT,
  symbol    TEXT,
  action    TEXT,
  details   TEXT
);
CREATE INDEX IF NOT EXISTS idx_aalerts_severity ON analytics_alerts(severity);
"""


def init_db(path):
    """Open connection (WAL, autocommit) and ensure schema. Idempotent."""
    global _conn, _path
    _path = path
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    except Exception:
        pass
    _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10.0)
    _conn.execute('PRAGMA journal_mode=WAL')
    _conn.execute('PRAGMA synchronous=NORMAL')
    with _lock:
        _conn.executescript(_SCHEMA)
    print(f'[DB] initialized at {path}')


def is_ready():
    return _conn is not None


def db_path():
    return _path


def _safe(default):
    """Decorator: swallow DB errors. Bot continues, log emitted."""
    def deco(fn):
        def wrap(*a, **kw):
            if _conn is None:
                return default() if callable(default) else default
            try:
                with _lock:
                    return fn(*a, **kw)
            except Exception as e:
                print(f'[DB ERROR] {fn.__name__}: {e}')
                return default() if callable(default) else default
        return wrap
    return deco


def _iso(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ─── Writers ──────────────────────────────────────────────────
@_safe(None)
def save_trade(t):
    _conn.execute(
        'INSERT OR REPLACE INTO trades '
        '(id,symbol,date,time,side,entry,exit,size,score,session,atr,sl,'
        ' tp1_hit,tp2_hit,peak_pnl,pnl_pct,won,exit_reason,duration_min,ts_utc) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            t.get('id'), t.get('symbol'), t.get('date'), t.get('time'), t.get('side'),
            t.get('entry'), t.get('exit'), t.get('size'), t.get('score'), t.get('session'),
            t.get('atr'), t.get('sl'),
            int(bool(t.get('tp1_hit'))), int(bool(t.get('tp2_hit'))),
            t.get('peak_pnl'), t.get('pnl_pct'), int(bool(t.get('won'))),
            t.get('exit_reason'), t.get('duration_min'),
            datetime.now(timezone.utc).isoformat(),
        )
    )


@_safe(None)
def save_signal(s):
    _conn.execute(
        'INSERT INTO signals(ts_utc,date,time,symbol,action,score,breakdown,status,reason,session) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (
            datetime.now(timezone.utc).isoformat(),
            s.get('date', ''), s.get('time', ''),
            s.get('symbol', ''), s.get('action', ''), s.get('score', 0),
            json.dumps(s.get('breakdown', {})),
            s.get('status', ''), s.get('reason', ''), s.get('session', ''),
        )
    )


@_safe(None)
def save_metric(m):
    _conn.execute(
        'INSERT INTO metrics(ts_utc,symbol,side,latency_ms,slippage_pct,requested,filled,status,reason) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (
            m.get('time'), m.get('symbol'), m.get('side'),
            m.get('latency_ms'), m.get('slippage_pct'),
            m.get('requested'), m.get('filled'),
            m.get('status'), m.get('reason'),
        )
    )


@_safe(None)
def upsert_position(symbol, p):
    _conn.execute(
        'INSERT OR REPLACE INTO positions '
        '(symbol,active,side,entry,size,remaining,sl,atr,r,tp1_hit,tp2_hit,'
        ' peak_pnl,trail_sl,entry_time,ref_price,ref_time,score,session,be_set,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            symbol, int(bool(p.get('active'))), p.get('side'),
            p.get('entry'), p.get('size'), p.get('remaining'),
            p.get('sl'), p.get('atr'), p.get('r'),
            int(bool(p.get('tp1_hit'))), int(bool(p.get('tp2_hit'))),
            p.get('peak_pnl'), p.get('trail_sl'),
            _iso(p.get('entry_time')), p.get('ref_price'), _iso(p.get('ref_time')),
            p.get('score'), p.get('session'), int(bool(p.get('be_set'))),
            datetime.now(timezone.utc).isoformat(),
        )
    )


@_safe(None)
def clear_position(symbol):
    _conn.execute('DELETE FROM positions WHERE symbol=?', (symbol,))


@_safe(None)
def save_state_global(daily_dict):
    cur = _conn.cursor()
    for k, v in daily_dict.items():
        if isinstance(v, datetime):
            v = v.isoformat()
        cur.execute('INSERT OR REPLACE INTO state_global(k,v) VALUES (?,?)',
                    (k, json.dumps(v)))


@_safe(None)
def save_state_symbol(symbol, st):
    _conn.execute(
        'INSERT OR REPLACE INTO state_symbol '
        '(symbol,win_streak,loss_streak,pause_until,sl_cooldown_until,pair_cooldown_until) '
        'VALUES (?,?,?,?,?,?)',
        (
            symbol, st.get('win_streak', 0), st.get('loss_streak', 0),
            _iso(st.get('pause_until')),
            _iso(st.get('sl_cooldown_until')),
            _iso(st.get('pair_cooldown_until')),
        )
    )


# ─── Readers ──────────────────────────────────────────────────
@_safe(list)
def load_trades(limit=1000):
    cur = _conn.execute(
        'SELECT id,symbol,date,time,side,entry,exit,size,score,session,atr,sl,'
        ' tp1_hit,tp2_hit,peak_pnl,pnl_pct,won,exit_reason,duration_min '
        'FROM trades ORDER BY id DESC LIMIT ?', (limit,))
    out = []
    for r in cur.fetchall():
        out.append({
            'id': r[0], 'symbol': r[1], 'date': r[2], 'time': r[3], 'side': r[4],
            'entry': r[5], 'exit': r[6], 'size': r[7], 'score': r[8], 'session': r[9],
            'atr': r[10], 'sl': r[11],
            'tp1_hit': bool(r[12]), 'tp2_hit': bool(r[13]),
            'peak_pnl': r[14], 'pnl_pct': r[15], 'won': bool(r[16]),
            'exit_reason': r[17], 'duration_min': r[18],
        })
    return list(reversed(out))


@_safe(list)
def load_signals(limit=500):
    cur = _conn.execute(
        'SELECT date,time,symbol,action,score,breakdown,status,reason,session '
        'FROM signals ORDER BY id DESC LIMIT ?', (limit,))
    out = []
    for r in cur.fetchall():
        try:
            bd = json.loads(r[5] or '{}')
        except Exception:
            bd = {}
        out.append({
            'date': r[0], 'time': r[1], 'symbol': r[2], 'action': r[3],
            'score': r[4], 'breakdown': bd,
            'status': r[6], 'reason': r[7], 'session': r[8],
        })
    return list(reversed(out))


@_safe(list)
def load_metrics(limit=500):
    cur = _conn.execute(
        'SELECT ts_utc,symbol,side,latency_ms,slippage_pct,requested,filled,status,reason '
        'FROM metrics ORDER BY id DESC LIMIT ?', (limit,))
    out = []
    for r in cur.fetchall():
        out.append({
            'time': r[0], 'symbol': r[1], 'side': r[2],
            'latency_ms': r[3], 'slippage_pct': r[4],
            'requested': r[5], 'filled': r[6],
            'status': r[7], 'reason': r[8],
        })
    return list(reversed(out))


@_safe(dict)
def load_active_positions():
    cur = _conn.execute(
        'SELECT symbol,active,side,entry,size,remaining,sl,atr,r,'
        ' tp1_hit,tp2_hit,peak_pnl,trail_sl,entry_time,ref_price,ref_time,'
        ' score,session,be_set FROM positions WHERE active=1')
    out = {}
    for r in cur.fetchall():
        sym = r[0]
        out[sym] = {
            'active': bool(r[1]), 'side': r[2],
            'entry': r[3], 'size': r[4], 'remaining': r[5],
            'sl': r[6], 'atr': r[7], 'r': r[8],
            'tp1_hit': bool(r[9]), 'tp2_hit': bool(r[10]),
            'peak_pnl': r[11], 'trail_sl': r[12],
            'entry_time': _parse_iso(r[13]),
            'ref_price': r[14], 'ref_time': _parse_iso(r[15]),
            'score': r[16], 'session': r[17],
            'be_set': bool(r[18]),
        }
    return out


@_safe(dict)
def load_state_global():
    cur = _conn.execute('SELECT k,v FROM state_global')
    out = {}
    for k, v in cur.fetchall():
        try:
            out[k] = json.loads(v) if v is not None else None
        except Exception:
            out[k] = v
    return out


@_safe(lambda: None)
def load_state_symbol(symbol):
    cur = _conn.execute(
        'SELECT win_streak,loss_streak,pause_until,sl_cooldown_until,pair_cooldown_until '
        'FROM state_symbol WHERE symbol=?', (symbol,))
    r = cur.fetchone()
    if not r:
        return None
    return {
        'win_streak': r[0] or 0,
        'loss_streak': r[1] or 0,
        'pause_until': _parse_iso(r[2]),
        'sl_cooldown_until': _parse_iso(r[3]),
        'pair_cooldown_until': _parse_iso(r[4]),
    }


@_safe(None)
def save_analytics_signal(s):
    """Rich analytics row for every signal — separate from execution-critical
    `signals` table. Never blocks signal processing on failure."""
    _conn.execute(
        'INSERT INTO analytics_signals '
        '(ts_utc,symbol,side,action,status,reason,session,score,breakdown,'
        ' rsi,atr,atr_pct,volatility_state,loss_streak,win_streak,'
        ' pair_cooldown_left,global_cooldown_on,hour_count,session_count,daily_pnl_pct) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            datetime.now(timezone.utc).isoformat(),
            s.get('symbol'), s.get('side'), s.get('action'),
            s.get('status'), s.get('reason'), s.get('session'),
            s.get('score'), json.dumps(s.get('breakdown', {})),
            s.get('rsi'), s.get('atr'), s.get('atr_pct'),
            s.get('volatility_state'),
            s.get('loss_streak'), s.get('win_streak'),
            s.get('pair_cooldown_left'), int(bool(s.get('global_cooldown_on'))),
            s.get('hour_count'), s.get('session_count'),
            s.get('daily_pnl_pct'),
        )
    )


@_safe(None)
def save_analytics_trade(t):
    """Rich analytics row at trade close — for replay/backtest infrastructure."""
    _conn.execute(
        'INSERT INTO analytics_trades '
        '(trade_id,ts_open_utc,ts_close_utc,symbol,side,entry,exit,size,score,'
        ' session,atr,sl,tp1_hit,tp2_hit,be_set,peak_pnl_pct,pnl_pct,won,'
        ' exit_reason,duration_min,execution_latency_ms,loss_streak_at_open,'
        ' daily_pnl_at_open) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            t.get('trade_id'),
            _iso(t.get('ts_open_utc')),
            _iso(t.get('ts_close_utc')) or datetime.now(timezone.utc).isoformat(),
            t.get('symbol'), t.get('side'),
            t.get('entry'), t.get('exit'), t.get('size'), t.get('score'),
            t.get('session'), t.get('atr'), t.get('sl'),
            int(bool(t.get('tp1_hit'))), int(bool(t.get('tp2_hit'))),
            int(bool(t.get('be_set'))),
            t.get('peak_pnl_pct'), t.get('pnl_pct'), int(bool(t.get('won'))),
            t.get('exit_reason'), t.get('duration_min'),
            t.get('execution_latency_ms'),
            t.get('loss_streak_at_open'),
            t.get('daily_pnl_at_open'),
        )
    )


@_safe(None)
def save_analytics_alert(a):
    _conn.execute(
        'INSERT INTO analytics_alerts(ts_utc,severity,msg,symbol,action,details) '
        'VALUES (?,?,?,?,?,?)',
        (
            datetime.now(timezone.utc).isoformat(),
            a.get('severity'), a.get('msg'), a.get('symbol'),
            a.get('action'), json.dumps(a.get('details', {})) if a.get('details') else None,
        )
    )


@_safe(list)
def load_analytics_trades(limit=200):
    cur = _conn.execute(
        'SELECT trade_id,ts_open_utc,ts_close_utc,symbol,side,entry,exit,size,'
        ' score,session,pnl_pct,won,exit_reason,duration_min,execution_latency_ms '
        'FROM analytics_trades ORDER BY id DESC LIMIT ?', (limit,))
    out = []
    for r in cur.fetchall():
        out.append({
            'trade_id': r[0], 'ts_open_utc': r[1], 'ts_close_utc': r[2],
            'symbol': r[3], 'side': r[4],
            'entry': r[5], 'exit': r[6], 'size': r[7], 'score': r[8],
            'session': r[9], 'pnl_pct': r[10], 'won': bool(r[11]),
            'exit_reason': r[12], 'duration_min': r[13],
            'execution_latency_ms': r[14],
        })
    return list(reversed(out))


@_safe(dict)
def stats():
    cur = _conn.cursor()
    out = {}
    for tbl in ('trades', 'signals', 'metrics', 'positions',
                'state_global', 'state_symbol',
                'analytics_signals', 'analytics_trades', 'analytics_alerts'):
        try:
            out[tbl] = cur.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
        except Exception:
            out[tbl] = -1
    return out
