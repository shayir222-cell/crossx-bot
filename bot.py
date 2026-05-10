"""
CrossX Pro Bot v3.0 — Multi-Symbol Elite Trading System
BTCUSDT | ETHUSDT | BNBUSDT | SOLUSDT | XRPUSDT
Bitget Futures | 5m Timeframe
"""
import os, json, time, hmac, hashlib, base64, threading
import requests
from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse
from datetime import datetime, date, timezone, timedelta

import db        # SQLite persistence layer
import logger    # structured JSON event logger
import metrics   # in-memory counters/gauges

app = FastAPI(title="CrossX Pro Bot v3.0")

# ════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════
API_KEY       = os.environ['BITGET_API_KEY']
API_SECRET    = os.environ['BITGET_API_SECRET']
PASSPHRASE    = os.environ['BITGET_PASSPHRASE']
WEBHOOK_TOKEN = os.environ.get('WEBHOOK_TOKEN', 'change_me')
TG_TOKEN      = os.environ.get('TG_TOKEN', '')
TG_CHAT_ID    = os.environ.get('TG_CHAT_ID', '')
RENDER_URL    = os.environ.get('RENDER_URL', '')
DB_PATH       = os.environ.get('DB_PATH', './crossx.db')
LOG_DIR       = os.environ.get('LOG_DIR', '/var/data/logs' if os.path.exists('/var/data') else './logs')

BASE_URL = 'https://api.bitget.com'

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT']

# ─── Min ATR % per symbol (filters "sleeping" markets) ───
MIN_ATR_PCT = {
    'BTCUSDT': 0.00080,  # 0.08% → ~$62 min ATR at $78k (fee-efficient at 7x)
    'ETHUSDT': 0.00090,  # 0.09% → ~$3.4 min ATR at $3800
    'BNBUSDT': 0.00120,  # 0.12% → ~$0.74 min ATR at $615
    'XRPUSDT': 0.00150,  # 0.15% → ~$0.003 min ATR at $2.3
}

# ─── Risk ────────────────────────────────────────────────
BASE_RISK_PCT    = 0.0050  # сниженный риск на базовых сделках
HIGH_VOL_RISK    = 0.0030  # более осторожный риск в высокую волатильность
STREAK_RISK      = 0.0075  # уменьшенный риск на сильных сигналах
LEVERAGE         = 7     # 10→7: reduces notional 30% → fees 30% lower
DAILY_LOSS_LIMIT = 0.10

# ─── ATR levels ──────────────────────────────────────────
SL_ATR_MULT    = 2.5   # увеличение стопа для снижения количества ложных SL
TP1_R          = 2.5   # unchanged: first TP at 2.5R
TP2_R          = 5.0   # unchanged: let full winners run
TRAIL_ATR_MULT = 1.0   # 1.5→1.0: tighter trail after TP1 captures more peak
MAX_GIVEBACK   = 0.25  # 0.30→0.25: give back even less at peak
TP1_SIZE_PCT   = 0.50  # 0.40→0.50: lock in half at first TP
TP2_SIZE_PCT   = 0.50  # 0.30→0.50: close full remainder at TP2

# ─── Score ───────────────────────────────────────────────
MIN_SCORE = 92  # повышенный порог качества сигналов

# ─── Sessions (UTC hours) ────────────────────────────────
LONDON_OPEN  = 7
LONDON_CLOSE = 16
NY_OPEN      = 13
NY_CLOSE     = 21

# ─── Streak pauses ───────────────────────────────────────
PAUSE_2L   = 30
PAUSE_3L   = 60
SL_COOLDOWN = 15  # minutes cooldown after any SL hit

# ─── Correlation groups (don't open same-direction in same group) ─
CORR_GROUPS = [
    {'BTCUSDT', 'ETHUSDT'},        # ~0.85 correlation
    {'BNBUSDT', 'XRPUSDT'},        # altcoin group
]

# ─── Time stop ───────────────────────────────────────────
TIME_STOP_MIN  = 90   # 30→90: give trades time to develop
TIME_STOP_MOVE = 0.003

# ─── Cooldowns & Trade Limits (incremental safety patch) ──
GLOBAL_COOLDOWN_MIN = 10   # min between any closed trade and next entry
PAIR_COOLDOWN_MIN   = 15   # min between trades on the same symbol (any outcome)
MAX_TRADES_HOUR     = 2
MAX_TRADES_SESSION  = 5
DEDUPE_TTL_SEC      = 320  # webhook dedupe hash TTL (>5m bucket so retries inside same candle are deduped)
LOSS_STREAK_PAUSE_H = 1    # global pause hours after 3rd loss


# ════════════════════════════════════════════════════════════
# STATE
# ════════════════════════════════════════════════════════════
def _make_pos():
    return {
        'active': False, 'side': None,
        'entry': 0.0, 'size': 0.0, 'remaining': 0.0,
        'sl': 0.0, 'atr': 0.0, 'r': 0.0,
        'tp1_hit': False, 'tp2_hit': False,
        'peak_pnl': 0.0, 'trail_sl': None,
        'entry_time': None, 'ref_price': 0.0, 'ref_time': None,
        'score': 0, 'session': '',
        'be_set': False,
    }

def _make_sym():
    return {'win_streak': 0, 'loss_streak': 0,
            'pause_until': None, 'sl_cooldown_until': None,
            'pair_cooldown_until': None}

positions   = {s: _make_pos() for s in SYMBOLS}
sym_state   = {s: _make_sym() for s in SYMBOLS}
trades_log  = []
signals_log = []  # every incoming webhook: taken or filtered, with full reason
metrics_log = []  # execution latency / slippage / rejection log
_signal_dedupe = {}  # hash -> expiry epoch (anti-duplicate webhook)

daily = {
    'date': '', 'start_balance': None, 'halted': False,
    'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0,
    'loss_streak': 0, 'streak_pause_until': None,
    'last_close_time': None, 'peak_pnl': 0.0,
}


# ════════════════════════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════════════════════════
def tg(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=5
        )
    except Exception as e:
        print(f'[TG] {e}')

def tg_to(chat_id, text: str):
    if not TG_TOKEN:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
            timeout=5
        )
    except Exception as e:
        print(f'[TG] {e}')

def get_tg_updates(offset):
    if not TG_TOKEN:
        return []
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates',
            params={'offset': offset, 'timeout': 20},
            timeout=25
        )
        return r.json().get('result', [])
    except:
        return []

def generate_advisor_comment():
    lines = []
    recent = trades_log[-10:]

    if recent:
        wins = sum(1 for t in recent if t['won'])
        wr = wins / len(recent) * 100
        if wr >= 60:
            lines.append(f'✅ Win rate {wr:.0f}% — стратегия работает хорошо')
        elif wr >= 40:
            lines.append(f'⚠️ Win rate {wr:.0f}% — нейтрально, накапливаем данные')
        else:
            lines.append(f'❌ Win rate {wr:.0f}% — рассмотри снижение MIN_SCORE до 80')

        time_stops = sum(1 for t in recent if t.get('exit_reason') == 'Time Stop')
        if time_stops > len(recent) * 0.4:
            lines.append(f'⏱ {time_stops}/{len(recent)} сделок — Time Stop (рынок боковой)')

        tp1_hits = sum(1 for t in recent if t.get('tp1_hit'))
        if tp1_hits:
            lines.append(f'🎯 TP1 достигнут: {tp1_hits}/{len(recent)} сделок')

        avg_pnl = sum(t.get('pnl_pct', 0) for t in recent) / len(recent)
        lines.append(f'📊 Avg PnL на сделку: {avg_pnl:+.3f}%')
    else:
        lines.append('📊 Нет завершённых сделок — накапливаем статистику')

    if signals_log:
        scored = [s for s in signals_log if s['score'] > 0 and s['status'] == 'filtered']
        if scored:
            avg_sc = sum(s['score'] for s in scored) / len(scored)
            max_sc = max(s['score'] for s in scored)
            lines.append(f'🔍 Отфильтрованные сигналы: avg {avg_sc:.0f} / max {max_sc}/100')
            if max_sc >= 80:
                lines.append('💡 Есть сигналы 80-81 — если мало сделок, рассмотри порог 80')

    active_count = sum(1 for p in positions.values() if p['active'])
    if active_count:
        lines.append(f'📌 Открытых позиций: {active_count}')

    return '\n'.join(lines)

def send_dashboard(chat_id=None):
    target = chat_id or TG_CHAT_ID
    if not target:
        return

    bal = get_balance()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_trades = [t for t in trades_log if t.get('date') == today]
    today_wins   = sum(1 for t in today_trades if t['won'])
    today_losses = len(today_trades) - today_wins
    today_pnl    = sum(t.get('pnl_pct', 0) for t in today_trades)

    session_name, _ = get_session()
    halt_txt = '🛑 HALT' if daily['halted'] else '✅ Активен'

    pos_lines = []
    for sym, p in positions.items():
        if p['active']:
            price = get_price(sym) or p['entry']
            if p['side'] == 'long':
                float_r = (price - p['entry']) / p['r'] if p['r'] else 0
            else:
                float_r = (p['entry'] - price) / p['r'] if p['r'] else 0
            pos_lines.append(
                f"  {'🟢' if p['side']=='long' else '🔴'} {sym} "
                f"@ ${p['entry']:,.1f} | {float_r:+.2f}R | Score {p['score']}"
            )
    pos_text = '\n'.join(pos_lines) if pos_lines else '  Нет активных позиций'

    text = (
        f'📊 <b>CrossX Pro Dashboard</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'💰 Баланс: <b>${bal:,.2f}</b>\n'
        f'📈 PnL сегодня: <b>{today_pnl:+.3f}%</b>\n'
        f'🔄 Сделок: {len(today_trades)}  ✅{today_wins} / ❌{today_losses}\n'
        f'🕐 Сессия: {session_name}\n'
        f'🤖 Статус: {halt_txt}\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'<b>Позиции:</b>\n{pos_text}\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'🧠 <b>Советник:</b>\n{generate_advisor_comment()}'
    )
    tg_to(target, text)


# ════════════════════════════════════════════════════════════
# DAILY REPORT (Telegram fix — full prop-style EOD)
# ════════════════════════════════════════════════════════════
def build_daily_report():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_trades = [t for t in trades_log if t.get('date') == today]
    cur_session, _ = get_session()

    starting = daily.get('start_balance') or 0.0
    balance  = get_balance() or 0.0
    ending   = get_equity() or balance
    net_usd  = (ending - starting) if starting else 0.0
    net_pct  = (net_usd / starting * 100) if starting else 0.0

    wins   = sum(1 for t in today_trades if t['won'])
    losses = len(today_trades) - wins
    wr     = (wins / len(today_trades) * 100) if today_trades else 0

    # R-multiple per trade (from logged sl + entry + pnl_pct)
    rs = []
    for t in today_trades:
        e, sl = t.get('entry', 0), t.get('sl', 0)
        if not e or not sl:
            continue
        r_pct = ((e - sl) / e * 100) if t['side'] == 'long' else ((sl - e) / e * 100)
        if r_pct > 0:
            rs.append(t['pnl_pct'] / r_pct)
    avg_r = (sum(rs) / len(rs)) if rs else 0.0

    best  = max(today_trades, key=lambda t: t['pnl_pct']) if today_trades else None
    worst = min(today_trades, key=lambda t: t['pnl_pct']) if today_trades else None

    # Bitget USDT-M futures taker fee 0.06% × 2 sides
    fees_pct = 0.12 * len(today_trades)

    today_metrics = [m for m in metrics_log if m['time'].startswith(today)]
    slip_pct = (sum(m['slippage_pct'] for m in today_metrics) / len(today_metrics)) if today_metrics else 0.0

    # Intraday equity curve approximation from sequential pnl_pct
    sorted_today = sorted(today_trades, key=lambda t: t.get('time', ''))
    cumul, peak_so_far, dd = [], 0.0, 0.0
    for t in sorted_today:
        s = (cumul[-1] if cumul else 0.0) + t['pnl_pct']
        cumul.append(s)
        if s > peak_so_far:
            peak_so_far = s
        gap = peak_so_far - s
        if gap > dd:
            dd = gap
    peak     = max(cumul) if cumul else 0.0
    last_pt  = cumul[-1] if cumul else 0.0
    giveback = (peak - last_pt) if peak > 0 else 0.0

    by_sess = {}
    by_sym  = {}
    for t in today_trades:
        s = t.get('session', 'Unknown')
        by_sess[s] = by_sess.get(s, 0) + 1
        sym = t['symbol']
        by_sym[sym] = by_sym.get(sym, 0) + 1

    if daily['halted']:
        state = 'halted'
    elif daily.get('loss_streak', 0) >= 2 or daily.get('pnl', 0) >= 5:
        state = 'defensive'
    else:
        state = 'normal'

    risk_mult = 0.5 if daily.get('loss_streak', 0) >= 2 else 1.0
    open_pos  = sum(1 for p in positions.values() if p['active'])
    syms_traded = ', '.join(sorted(by_sym.keys())) if by_sym else '—'

    text = (
        '📊 <b>DAILY REPORT</b>\n'
        '━━━━━━━━━━━━━━━━━━━\n'
        f'Date:      {today}\n'
        f'Session:   {cur_session}\n'
        f'Symbols:   {syms_traded}\n'
        '\n'
        f'Start bal:  ${starting:,.2f}\n'
        f'End bal:    ${ending:,.2f}\n'
        f'Net PnL $:  ${net_usd:+,.2f}\n'
        f'Net PnL %:  {net_pct:+.2f}%\n'
        '\n'
        f'Trades:    {len(today_trades)}\n'
        f'Wins:      {wins}\n'
        f'Losses:    {losses}\n'
        f'Winrate:   {wr:.1f}%\n'
        '\n'
        f'Avg R:     {avg_r:+.2f}R\n'
    )
    if best:
        text += f'Best:      {best["pnl_pct"]:+.2f}% ({best["symbol"]})\n'
    if worst:
        text += f'Worst:     {worst["pnl_pct"]:+.2f}% ({worst["symbol"]})\n'
    text += (
        '\n'
        f'Fees est:  ~{fees_pct:.2f}%\n'
        f'Slip avg:  ~{slip_pct:.3f}%\n'
        '\n'
        f'Max DD:    {dd:.2f}%\n'
        f'Peak:      {peak:+.2f}%\n'
        f'Giveback:  {giveback:.2f}%\n'
        '\n'
        '<b>By session:</b>\n'
        f' Asia:    {by_sess.get("Asian", 0)}\n'
        f' London:  {by_sess.get("London", 0)}\n'
        f' Overlap: {by_sess.get("London/NY Overlap", 0)}\n'
        f' NY:      {by_sess.get("New York", 0)}\n'
        '\n'
        '<b>By symbol:</b>\n'
        f' BTC: {by_sym.get("BTCUSDT", 0)}\n'
        f' ETH: {by_sym.get("ETHUSDT", 0)}\n'
        f' SOL: {by_sym.get("SOLUSDT", 0)}\n'
        f' XRP: {by_sym.get("XRPUSDT", 0)}\n'
        f' BNB: {by_sym.get("BNBUSDT", 0)}\n'
        '\n'
        f'State:     {state}\n'
        f'Risk mult: ×{risk_mult}\n'
        f'Leverage:  {LEVERAGE}x\n'
        f'Open pos:  {open_pos}\n'
    )
    return text


def send_daily_report(chat_id=None):
    target = chat_id or TG_CHAT_ID
    if not target:
        return
    tg_to(target, build_daily_report())


_tg_offset = 0
_last_eod_date = ''

def tg_polling():
    global _tg_offset, _last_eod_date
    while True:
        try:
            updates = get_tg_updates(_tg_offset)
            for upd in updates:
                _tg_offset = upd['update_id'] + 1
                msg  = upd.get('message', {})
                text = msg.get('text', '').strip()
                cid  = str(msg.get('chat', {}).get('id', ''))
                if not cid:
                    continue
                if text in ('/dashboard', '/d', '/status'):
                    send_dashboard(cid)
                elif text in ('/report', '/daily'):
                    tg_to(cid, build_daily_report())
                elif text == '/start':
                    tg_to(cid,
                        '🤖 <b>CrossX Pro Bot</b>\n\n'
                        'Команды:\n'
                        '/dashboard — дашборд + советник\n'
                        '/d — то же самое (короче)\n'
                        '/report — детальный дневной отчёт\n'
                    )
        except Exception as e:
            print(f'[TG POLL] {e}')

        # End-of-day summary in 23:55-23:59 UTC window (>=55 — robust to restarts/stalls)
        now = datetime.now(timezone.utc)
        today = now.strftime('%Y-%m-%d')
        if now.hour == 23 and now.minute >= 55 and _last_eod_date != today:
            _last_eod_date = today
            send_daily_report()

        time.sleep(3)


# ════════════════════════════════════════════════════════════
# BITGET API
# ════════════════════════════════════════════════════════════
_time_offset = 0

def _sync_time():
    global _time_offset
    try:
        r = requests.get('https://api.bitget.com/api/v2/public/time', timeout=5)
        server_ts = int(r.json()['data']['serverTime'])
        _time_offset = server_ts - int(time.time() * 1000)
    except Exception:
        _time_offset = 0

def _ts():
    return str(int(time.time() * 1000) + _time_offset)

def _sign(ts, method, path, body=''):
    msg = ts + method.upper() + path + body
    return base64.b64encode(hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()

def _h(method, path, body=''):
    ts = _ts()
    return {
        'ACCESS-KEY': API_KEY, 'ACCESS-SIGN': _sign(ts, method, path, body),
        'ACCESS-TIMESTAMP': ts, 'ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type': 'application/json', 'locale': 'en-US',
    }

def api_get(path):
    try:
        return requests.get(BASE_URL + path, headers=_h('GET', path), timeout=10).json()
    except Exception as e:
        print(f'[API GET] {path}: {e}')
        metrics.inc('api_errors_total')
        logger.log_error('api_error', method='GET', path=path, error=str(e)[:120])
        return {}

def api_post(path, body):
    try:
        b = json.dumps(body)
        return requests.post(BASE_URL + path, headers=_h('POST', path, b), data=b, timeout=10).json()
    except Exception as e:
        print(f'[API POST] {path}: {e}')
        metrics.inc('api_errors_total')
        logger.log_error('api_error', method='POST', path=path, error=str(e)[:120])
        return {}

def get_balance():
    """Returns available balance for position sizing."""
    d = api_get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    if d.get('code') == '00000' and d.get('data'):
        return float(d['data'][0]['available'])
    return None

def get_equity():
    """Returns total account equity (available + locked margin + unrealized PnL) for daily loss check."""
    d = api_get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    if d.get('code') == '00000' and d.get('data'):
        acc = d['data'][0]
        # usdtEquity = total equity; fallback to available if not present
        return float(acc.get('usdtEquity') or acc.get('equity') or acc.get('available') or 0)
    return None

def get_price(symbol):
    d = api_get(f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES')
    if d.get('code') == '00000':
        return float(d['data'][0]['lastPr'])
    return None

def get_candles(symbol, gran='5m', limit=60):
    d = api_get(f'/api/v2/mix/market/candles?symbol={symbol}&productType=USDT-FUTURES&granularity={gran}&limit={limit}')
    if d.get('code') == '00000':
        return d['data']
    return None

def set_leverage(symbol):
    api_post('/api/v2/mix/account/set-leverage', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'marginCoin': 'USDT', 'leverage': str(LEVERAGE),
    })

def place_order(symbol, side, size, reduce_only=False):
    return api_post('/api/v2/mix/order/place-order', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'marginMode': 'isolated', 'marginCoin': 'USDT',
        'size': str(round(size, 4)), 'side': side, 'orderType': 'market',
        'tradeSide': 'close' if reduce_only else 'open',
    })

def close_all(symbol, hold_side):
    return api_post('/api/v2/mix/order/close-positions', {
        'symbol': symbol, 'productType': 'USDT-FUTURES', 'holdSide': hold_side,
    })

def close_position_safe(symbol, hold_side):
    """Verified close with one retry. Returns (ok: bool, response: dict).

    Bitget code '00000' = success. Anything else = retry once after 1s.
    On final failure, alert TG and write a 'close_failed' execution metric so
    next monitor tick can retry naturally (we do NOT reset_pos on failure).
    """
    res = close_all(symbol, hold_side)
    code = (res or {}).get('code') if isinstance(res, dict) else None
    if code == '00000':
        return True, res

    # one retry after brief pause (rate-limit / transient network)
    time.sleep(1)
    res2 = close_all(symbol, hold_side)
    code2 = (res2 or {}).get('code') if isinstance(res2, dict) else None
    if code2 == '00000':
        return True, res2

    err = ((res2 or res) or {}).get('msg', 'unknown') if isinstance((res2 or res), dict) else 'unknown'
    metrics.inc('close_fail_total')
    logger.log_error('close_failed', symbol=symbol, side=hold_side, error=str(err)[:200])
    tg(f'⚠️ <b>CLOSE FAILED {symbol}</b>\n'
       f'side: {hold_side.upper()} | Bitget: {err}\n'
       f'Will retry on next monitor tick (15s).')
    log_execution(symbol, hold_side, time.time(), time.time(), 0, 0, 'close_failed', str(err)[:120])
    print(f'[CLOSE FAILED] {symbol} {hold_side} -> {err}')
    return False, res2

def get_all_positions_bitget():
    """Returns dict {symbol: {side, total}} of all open positions on Bitget. Used for startup reconciliation."""
    d = api_get('/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT')
    out = {}
    if d.get('code') == '00000' and d.get('data'):
        for row in d['data']:
            try:
                sym = row.get('symbol', '')
                total = float(row.get('total', 0) or 0)
                if total <= 0:
                    continue
                side = row.get('holdSide', '')  # 'long' or 'short'
                if sym and side:
                    out[sym] = {'side': side, 'total': total}
            except Exception:
                continue
    return out


# ════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS
# ════════════════════════════════════════════════════════════
def calc_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = float(candles[i][2]), float(candles[i][3]), float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

def calc_ema(values, period):
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def calc_rsi(candles, period=14):
    if not candles or len(candles) < period + 1:
        return 50.0
    closes = [float(c[4]) for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))

def get_tf_bias(symbol, gran):
    candles = get_candles(symbol, gran, 55)
    if not candles or len(candles) < 25:
        return 'neutral'
    closes = [float(c[4]) for c in candles]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, min(50, len(closes)))
    price = closes[-1]
    if price > ema20 and ema20 > ema50:
        return 'bullish'
    elif price < ema20 and ema20 < ema50:
        return 'bearish'
    return 'neutral'

def get_volume_bias(candles):
    if not candles or len(candles) < 13:
        return 'neutral'
    vols = [float(c[5]) for c in candles]
    avg_vol = sum(vols[-13:-3]) / 10
    recent_vol = sum(vols[-3:]) / 3
    if recent_vol > avg_vol * 1.3:
        return 'high'
    elif recent_vol < avg_vol * 0.7:
        return 'low'
    return 'normal'

def is_high_volatility(atr, price):
    return (atr / price) > 0.008


# ════════════════════════════════════════════════════════════
# SIGNAL SCORER (0–100)
# ════════════════════════════════════════════════════════════
def score_signal(symbol, side, candles_5m, alert_data=None):
    score = 0
    breakdown = {}

    def crossx(key):
        if alert_data:
            return alert_data.get(key, '').lower()
        return ''

    def points(val, bullish_pts, neutral_pts, side_is_long):
        if side_is_long:
            if val == 'bullish': return bullish_pts
            if val == 'neutral': return neutral_pts
            return 0
        else:
            if val == 'bearish': return bullish_pts
            if val == 'neutral': return neutral_pts
            return 0

    is_long = (side == 'long')

    mt = crossx('macro_trend') or get_tf_bias(symbol, '4H')
    p = points(mt, 25, 12, is_long)
    score += p
    breakdown['macro_trend'] = f'{mt} ({p}/25)'

    mom = crossx('momentum') or get_tf_bias(symbol, '1H')
    p = points(mom, 20, 10, is_long)
    score += p
    breakdown['momentum'] = f'{mom} ({p}/20)'

    struct = crossx('structure') or get_tf_bias(symbol, '15m')
    p = points(struct, 20, 10, is_long)
    score += p
    breakdown['structure'] = f'{struct} ({p}/20)'

    vol_raw = crossx('volume')
    if vol_raw:
        vol_val = vol_raw
    else:
        v = get_volume_bias(candles_5m)
        vol_val = 'bullish' if v == 'high' else ('bearish' if v == 'low' else 'neutral')
    p = points(vol_val, 15, 7, is_long)
    score += p
    breakdown['volume'] = f'{vol_val} ({p}/15)'

    ctf = crossx('current_tf') or get_tf_bias(symbol, '5m')
    p = points(ctf, 10, 5, is_long)
    score += p
    breakdown['current_tf'] = f'{ctf} ({p}/10)'

    ob_os_raw = crossx('ob_os')
    rsi = calc_rsi(candles_5m)
    if ob_os_raw:
        ob_val = ob_os_raw
    else:
        if rsi < 35:   ob_val = 'oversold'
        elif rsi > 65: ob_val = 'overbought'
        else:          ob_val = 'neutral'

    ob_pts = 10 if (is_long and ob_val in ('oversold', 'neutral')) or \
                   (not is_long and ob_val in ('overbought', 'neutral')) else 0
    score += ob_pts

    # Patch 5 — graduated RSI penalty (anti-overextension, NOT a hard block)
    penalty = 0
    if is_long:
        if rsi >= 78:   penalty = -10
        elif rsi >= 70: penalty = -5
    else:
        if rsi <= 22:   penalty = -10
        elif rsi <= 30: penalty = -5
    score += penalty

    breakdown['ob_os'] = f'{ob_val} ({ob_pts}/10) | RSI={rsi:.0f} pen={penalty}'

    return score, breakdown


# ════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME GATE
# ════════════════════════════════════════════════════════════
def check_mtf(symbol, side):
    b4h  = get_tf_bias(symbol, '4H')
    b1h  = get_tf_bias(symbol, '1H')
    b15m = get_tf_bias(symbol, '15m')
    info = f'4h:{b4h} 1h:{b1h} 15m:{b15m}'

    if side == 'long':
        if b4h == 'bearish':  return False, f'BLOCKED: 4h bearish ({info})'
        if b1h == 'bearish':  return False, f'BLOCKED: 1h bearish ({info})'
        if b15m == 'bearish': return False, f'BLOCKED: 15m bearish ({info})'
    else:
        if b4h == 'bullish':  return False, f'BLOCKED: 4h bullish ({info})'
        if b1h == 'bullish':  return False, f'BLOCKED: 1h bullish ({info})'
        if b15m == 'bullish': return False, f'BLOCKED: 15m bullish ({info})'

    return True, info


# ════════════════════════════════════════════════════════════
# SESSION FILTER
# ════════════════════════════════════════════════════════════
def get_session():
    h = datetime.now(timezone.utc).hour
    in_ld = LONDON_OPEN <= h < LONDON_CLOSE
    in_ny = NY_OPEN <= h < NY_CLOSE
    if in_ld and in_ny: return 'London/NY Overlap', 0.0
    elif in_ld:         return 'London', 0.0
    elif in_ny:         return 'New York', 1.0
    elif 0 <= h < 7:    return 'Asian', 1.0   # enabled: score+ATR filters protect quality
    else:               return 'Off-session', 0.0  # blocked: 21-00 UTC dead zone

def is_session_blocked():
    _, mult = get_session()
    return mult == 0.0


# ════════════════════════════════════════════════════════════
# NEWS FILTER
# ════════════════════════════════════════════════════════════
HIGH_IMPACT = ['cpi', 'fomc', 'nfp', 'non-farm', 'federal', 'interest rate', 'inflation', 'gdp', 'ppi']

def check_news():
    try:
        r = requests.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=5)
        now = datetime.now(timezone.utc)
        for ev in r.json():
            if ev.get('impact', '').lower() != 'high': continue
            if ev.get('country', '') not in ('USD', 'US'): continue
            title = ev.get('title', '').lower()
            if not any(kw in title for kw in HIGH_IMPACT): continue
            try:
                et = datetime.fromisoformat(ev['date'].replace('Z', '+00:00'))
                diff = (et - now).total_seconds() / 60
                if -15 <= diff <= 60:
                    return False, f'News block: {ev["title"]} @ {et.strftime("%H:%M")} UTC'
            except Exception:
                pass
        return True, 'No major news'
    except Exception:
        return True, 'News API error (allowed)'


# ════════════════════════════════════════════════════════════
# DAILY STATS & RISK ENGINE
# ════════════════════════════════════════════════════════════
def reset_daily():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if daily['date'] != today:
        daily.update(date=today, start_balance=None, halted=False,
                     trades=0, wins=0, losses=0, pnl=0.0,
                     loss_streak=0, streak_pause_until=None,
                     last_close_time=None, peak_pnl=0.0)
        print(f'[DAY] New day: {today}')
        db.save_state_global(daily)  # dual-write

def check_daily_loss(balance):
    equity = get_equity() or balance  # use total equity, not just available
    if daily['start_balance'] is None:
        daily['start_balance'] = equity
        db.save_state_global(daily)  # persist start_balance immediately
        return False
    lost = (daily['start_balance'] - equity) / daily['start_balance']
    if lost >= DAILY_LOSS_LIMIT:
        daily['halted'] = True
        db.save_state_global(daily)  # persist halt flag
        metrics.inc('daily_dd_halts_total')
        metrics.gauge('current_drawdown_pct', lost * 100)
        logger.log_error('daily_dd_halt', loss_pct=round(lost * 100, 2),
                         equity=round(equity, 2),
                         start_balance=round(daily['start_balance'], 2))
        tg(f'🛑 <b>DAILY STOP</b>\nLoss: <b>-{lost*100:.1f}%</b>\nEquity: ${equity:.2f}\nStopped until tomorrow.')
        return True
    metrics.gauge('current_drawdown_pct', max(lost * 100, 0))
    return False

def is_paused(symbol):
    now = datetime.now(timezone.utc)
    st = sym_state[symbol]
    # Loss streak pause
    pu = st['pause_until']
    if pu and now < pu:
        mins = int((pu - now).total_seconds() / 60)
        return True, f'Pause {mins}m remaining (loss streak)'
    if pu:
        st['pause_until'] = None
    # SL cooldown
    cd = st['sl_cooldown_until']
    if cd and now < cd:
        mins = int((cd - now).total_seconds() / 60)
        return True, f'SL cooldown {mins}m remaining'
    if cd:
        st['sl_cooldown_until'] = None
    return False, ''

def is_correlated_blocked(symbol, side):
    """Returns True if a correlated symbol already has an active position in the same direction."""
    for group in CORR_GROUPS:
        if symbol not in group:
            continue
        for other in group:
            if other == symbol:
                continue
            op = positions[other]
            if op['active'] and op['side'] == side:
                return True, f'Correlated pair {other} already {side}'
    return False, ''


# ════════════════════════════════════════════════════════════
# COOLDOWNS, TRADE LIMITS, ANTI-DUP, EXEC METRICS (safety patch)
# ════════════════════════════════════════════════════════════
def is_global_cooldown_active():
    last = daily.get('last_close_time')
    if not last:
        return False, ''
    elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    if elapsed_min < GLOBAL_COOLDOWN_MIN:
        return True, f'Global cooldown {GLOBAL_COOLDOWN_MIN - int(elapsed_min)}m left'
    return False, ''

def is_pair_cooldown_active(symbol):
    cd = sym_state[symbol].get('pair_cooldown_until')
    if not cd:
        return False, ''
    now = datetime.now(timezone.utc)
    if now < cd:
        mins = int((cd - now).total_seconds() / 60) + 1
        return True, f'Pair cooldown {mins}m left'
    sym_state[symbol]['pair_cooldown_until'] = None
    return False, ''

def is_streak_paused():
    pu = daily.get('streak_pause_until')
    if not pu:
        return False, ''
    now = datetime.now(timezone.utc)
    if now < pu:
        mins = int((pu - now).total_seconds() / 60) + 1
        return True, f'Loss-streak pause {mins}m left'
    daily['streak_pause_until'] = None
    return False, ''

def check_trade_limits():
    """Returns (allowed, reason). Counts only entries (signals_log status='taken')."""
    now = datetime.now(timezone.utc)
    today_str = now.strftime('%Y-%m-%d')

    hour_count = 0
    for s in signals_log:
        if s['status'] != 'taken':
            continue
        try:
            t = datetime.strptime(f"{s['date']} {s['time']}", '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - t).total_seconds() < 3600:
            hour_count += 1
    if hour_count >= MAX_TRADES_HOUR:
        return False, f'Hour cap reached ({MAX_TRADES_HOUR})'

    cur_session, _ = get_session()
    sess_count = sum(
        1 for s in signals_log
        if s['status'] == 'taken'
        and s['date'] == today_str
        and s.get('session') == cur_session
    )
    if sess_count >= MAX_TRADES_SESSION:
        return False, f'Session cap reached ({MAX_TRADES_SESSION} for {cur_session})'

    return True, ''

def candle_open_5m_ts():
    ts = int(time.time())
    return ts - (ts % 300)

def is_duplicate_signal(symbol, side):
    """Hash-based webhook dedupe: symbol + side + 5m candle bucket. TTL ~100s."""
    now_epoch = time.time()
    expired = [k for k, exp in _signal_dedupe.items() if exp < now_epoch]
    for k in expired:
        _signal_dedupe.pop(k, None)
    key = f'{symbol}|{side}|5m|{candle_open_5m_ts()}'
    h = hashlib.md5(key.encode()).hexdigest()
    if h in _signal_dedupe:
        return True
    _signal_dedupe[h] = now_epoch + DEDUPE_TTL_SEC
    return False

def log_execution(symbol, side, t_signal, t_entry, requested_price, fill_price, status, reason=''):
    latency_ms = int((t_entry - t_signal) * 1000) if t_signal else 0
    slippage_pct = abs(fill_price - requested_price) / requested_price * 100 if requested_price else 0.0
    entry = {
        'time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'symbol': symbol, 'side': side,
        'latency_ms': latency_ms,
        'slippage_pct': round(slippage_pct, 4),
        'requested': requested_price,
        'filled': fill_price,
        'status': status,
        'reason': reason,
    }
    metrics_log.append(entry)
    if len(metrics_log) > 500:
        metrics_log.pop(0)
    db.save_metric(entry)  # dual-write

def get_risk_pct(score, atr, price):
    if is_high_volatility(atr, price):
        base = HIGH_VOL_RISK
    elif score >= 90:
        base = STREAK_RISK
    elif score >= 82:
        base = BASE_RISK_PCT
    else:
        base = BASE_RISK_PCT * 0.5

    # Patch 4 — defensive multiplier on loss-streak (≥2 global losses today)
    if daily.get('loss_streak', 0) >= 2:
        base *= 0.5
    return base

def record_result(symbol, won, pnl_pct):
    now = datetime.now(timezone.utc)
    daily['trades'] += 1
    daily['pnl'] += pnl_pct
    daily['last_close_time'] = now  # Patch 1 — global cooldown anchor

    # Patch 2 — pair cooldown after EVERY close (any outcome)
    sym_state[symbol]['pair_cooldown_until'] = now + timedelta(minutes=PAIR_COOLDOWN_MIN)

    st = sym_state[symbol]
    if won:
        daily['wins'] += 1
        daily['loss_streak'] = 0
        st['win_streak'] += 1
        st['loss_streak'] = 0
    else:
        daily['losses'] += 1
        daily['loss_streak'] += 1
        st['loss_streak'] += 1
        st['win_streak'] = 0
        st['sl_cooldown_until'] = now + timedelta(minutes=SL_COOLDOWN)

        # Per-symbol streak (existing behaviour, kept)
        if st['loss_streak'] == 2:
            st['pause_until'] = now + timedelta(minutes=PAUSE_2L)
            tg(f'⏸ <b>{symbol}: 2 losses</b> — pausing {PAUSE_2L}min')
        elif st['loss_streak'] >= 3:
            st['pause_until'] = now + timedelta(minutes=PAUSE_3L)
            tg(f'⏸ <b>{symbol}: 3+ losses</b> — pausing {PAUSE_3L}min')

        # Patch 4 — GLOBAL loss-streak escalation
        gl = daily['loss_streak']
        if gl >= 4:
            daily['halted'] = True
            tg(f'🛑 <b>SESSION HALT</b>\n4 losses in a row — stopped until next day.')
        elif gl == 3:
            daily['streak_pause_until'] = now + timedelta(hours=LOSS_STREAK_PAUSE_H)
            tg(f'⏸ <b>3 LOSSES</b>\nGlobal pause {LOSS_STREAK_PAUSE_H}h.')
        elif gl == 2:
            tg(f'⚠️ <b>2 LOSSES</b>\nDefensive mode: risk × 0.5.')

    # Track peak daily PnL for giveback metric
    if daily['pnl'] > daily.get('peak_pnl', 0.0):
        daily['peak_pnl'] = daily['pnl']

    # Phase B — dual-write state
    db.save_state_global(daily)
    db.save_state_symbol(symbol, sym_state[symbol])

    # Observability — gauges reflecting fresh state
    metrics.gauge('current_loss_streak', daily.get('loss_streak', 0))
    metrics.gauge('current_win_streak', sym_state[symbol].get('win_streak', 0))
    metrics.gauge('daily_pnl_pct', daily.get('pnl', 0.0))
    metrics.gauge('daily_peak_pnl_pct', daily.get('peak_pnl', 0.0))
    metrics.gauge('active_positions', sum(1 for p in positions.values() if p['active']))
    if daily.get('halted'):
        metrics.inc('risk_halts_total')
        logger.log_warning('risk_halt', reason='loss_streak_4',
                           loss_streak=daily.get('loss_streak'),
                           pnl_pct=daily.get('pnl'))


# ════════════════════════════════════════════════════════════
# SIGNAL LOGGING (every webhook — taken or filtered)
# ════════════════════════════════════════════════════════════
def log_signal(symbol, action, score, breakdown, status, reason, session=''):
    entry = {
        'time': datetime.now(timezone.utc).strftime('%H:%M'),
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'symbol': symbol,
        'action': action,
        'score': score,
        'breakdown': breakdown,
        'status': status,   # 'taken', 'filtered', 'paused', 'halted', 'skipped'
        'reason': reason,
        'session': session,
    }
    signals_log.append(entry)
    if len(signals_log) > 500:
        signals_log.pop(0)
    db.save_signal(entry)  # dual-write

    # Observability — counters + structured event
    metrics.inc('signals_received_total')
    if status == 'taken':
        metrics.inc('signals_taken_total')
        logger.log_event('signal_taken', symbol=symbol, action=action, score=score,
                         session=session, reason=reason)
    elif status == 'filtered':
        metrics.inc('signals_rejected_total')
        if 'duplicate' in reason.lower():
            metrics.inc('duplicate_signals_blocked_total')
            logger.log_event('duplicate_prevented', symbol=symbol, action=action)
        elif 'cooldown' in reason.lower():
            metrics.inc('cooldowns_triggered_total')
            logger.log_event('cooldown_triggered', symbol=symbol, reason=reason)
        else:
            logger.log_event('signal_rejected', symbol=symbol, action=action,
                             score=score, reason=reason, session=session)
    elif status == 'halted':
        logger.log_warning('signal_halted', symbol=symbol, reason=reason)
    elif status == 'paused':
        logger.log_event('signal_paused', symbol=symbol, reason=reason)
    elif status == 'skipped':
        logger.log_event('signal_skipped', symbol=symbol, reason=reason)


# ════════════════════════════════════════════════════════════
# TRADE LOGGING
# ════════════════════════════════════════════════════════════
def log_trade(symbol, exit_price, exit_reason, won, pnl_pct):
    pos = positions[symbol]
    trade = {
        'id': len(trades_log) + 1,
        'symbol': symbol,
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'time': datetime.now(timezone.utc).strftime('%H:%M'),
        'side': pos['side'],
        'entry': pos['entry'],
        'exit': round(exit_price, 4),
        'size': pos['size'],
        'score': pos.get('score', 0),
        'session': pos.get('session', ''),
        'atr': round(pos['atr'], 4),
        'sl': pos['sl'],
        'tp1_hit': pos['tp1_hit'],
        'tp2_hit': pos['tp2_hit'],
        'peak_pnl': round(pos['peak_pnl'], 3),
        'pnl_pct': round(pnl_pct, 3),
        'won': won,
        'exit_reason': exit_reason,
        'duration_min': int((datetime.now(timezone.utc) - pos['entry_time']).total_seconds() / 60) if pos['entry_time'] else 0,
    }
    trades_log.append(trade)
    if len(trades_log) > 1000:
        trades_log.pop(0)
    db.save_trade(trade)  # dual-write
    print(f'[TRADE #{trade["id"]}] {symbol} {trade["side"].upper()} | {exit_reason} | PnL:{pnl_pct:+.3f}%')

    # Observability — counters + structured event
    metrics.inc('trades_closed_total')
    if won:
        metrics.inc('trades_won_total')
    else:
        metrics.inc('trades_lost_total')
    reason_l = (exit_reason or '').lower()
    if reason_l == 'sl':
        metrics.inc('sl_hits_total')
        evt = 'sl_hit'
    elif reason_l == 'trailing stop':
        metrics.inc('trail_stops_total')
        evt = 'trailing_stop'
    elif reason_l == 'max giveback':
        metrics.inc('max_giveback_total')
        evt = 'max_giveback'
    elif reason_l == 'time stop':
        metrics.inc('time_stops_total')
        evt = 'time_stop'
    elif reason_l == 'manual close':
        metrics.inc('manual_closes_total')
        evt = 'manual_close'
    else:
        evt = 'trade_closed'
    logger.log_event(evt, symbol=symbol, side=trade['side'],
                     trade_id=trade['id'], score=trade['score'],
                     pnl_pct=trade['pnl_pct'], peak_pnl=trade['peak_pnl'],
                     duration_min=trade['duration_min'], session=trade['session'],
                     entry=trade['entry'], exit=trade['exit'])


# ════════════════════════════════════════════════════════════
# POSITION RESET
# ════════════════════════════════════════════════════════════
def reset_pos(symbol):
    positions[symbol].update(
        active=False, side=None, entry=0.0, size=0.0, remaining=0.0,
        sl=0.0, atr=0.0, r=0.0, tp1_hit=False, tp2_hit=False,
        peak_pnl=0.0, trail_sl=None, entry_time=None,
        ref_price=0.0, ref_time=None, score=0, session='',
        be_set=False,
    )
    db.clear_position(symbol)  # dual-write


# ════════════════════════════════════════════════════════════
# POSITION MONITOR — background thread (every 15s, all symbols)
# ════════════════════════════════════════════════════════════
def monitor():
    print('[MONITOR] Started — watching:', ', '.join(SYMBOLS))
    while True:
        time.sleep(15)
        for symbol in SYMBOLS:
            pos = positions[symbol]
            if not pos['active']:
                continue
            try:
                price = get_price(symbol)
                if not price:
                    continue

                side  = pos['side']
                entry = pos['entry']
                atr   = pos['atr']
                r     = pos['r']
                now   = datetime.now(timezone.utc)

                if side == 'long':
                    pnl_pct = (price - entry) / entry * 100
                    float_r = (price - entry) / r if r else 0
                else:
                    pnl_pct = (entry - price) / entry * 100
                    float_r = (entry - price) / r if r else 0

                if pnl_pct > pos['peak_pnl']:
                    pos['peak_pnl'] = pnl_pct

                # Hard SL
                sl_hit = (side == 'long' and price <= pos['sl']) or \
                         (side == 'short' and price >= pos['sl'])
                if sl_hit:
                    ok, _ = close_position_safe(symbol, side)
                    if not ok:
                        # Position still on Bitget — keep state active so next tick retries
                        continue
                    tg(f'🔴 <b>SL {symbol}</b>\n${price:,.4f} | SL ${pos["sl"]:,.4f}\nPnL: {pnl_pct:+.2f}%')
                    log_trade(symbol, price, 'SL', False, pnl_pct)
                    record_result(symbol, False, pnl_pct)
                    reset_pos(symbol)
                    continue

                # Max Giveback — only after TP1 is hit; before TP1, BE SL handles protection
                if pos['tp1_hit'] and pos['peak_pnl'] > 0.5:
                    giveback = (pos['peak_pnl'] - pnl_pct) / pos['peak_pnl']
                    if giveback >= MAX_GIVEBACK:
                        ok, _ = close_position_safe(symbol, side)
                        if not ok:
                            continue
                        tg(f'💰 <b>MAX GIVEBACK {symbol}</b>\nPeak:{pos["peak_pnl"]:.2f}% → {pnl_pct:.2f}%\n${price:,.4f}')
                        log_trade(symbol, price, 'Max Giveback', pnl_pct > 0, pnl_pct)
                        record_result(symbol, pnl_pct > 0, pnl_pct)
                        reset_pos(symbol)
                        continue

                # Time Stop — only fires when position is at a loss
                if pos['ref_time'] is None:
                    pos['ref_time'] = now
                    pos['ref_price'] = price
                elif (now - pos['ref_time']).seconds >= TIME_STOP_MIN * 60:
                    move = abs(price - pos['ref_price']) / pos['ref_price']
                    if move < TIME_STOP_MOVE:
                        if pnl_pct >= 0:
                            # Profitable trade going sideways — reset clock, let SL/TP handle it
                            pos['ref_time'] = now
                            pos['ref_price'] = price
                        else:
                            # Loss + no movement → exit to limit damage
                            ok, _ = close_position_safe(symbol, side)
                            if not ok:
                                continue
                            tg(f'⏱ <b>TIME STOP {symbol}</b>\n{TIME_STOP_MIN}min, move {move*100:.2f}%\n${price:,.4f} | PnL:{pnl_pct:+.2f}%')
                            log_trade(symbol, price, 'Time Stop', False, pnl_pct)
                            record_result(symbol, False, pnl_pct)
                            reset_pos(symbol)
                            continue
                    else:
                        pos['ref_time'] = now
                        pos['ref_price'] = price

                # Break-Even SL at +1.0R (before TP1) — prevents reversals turning winners into losers
                if not pos['be_set'] and float_r >= 1.0:
                    pos['sl'] = entry
                    pos['be_set'] = True
                    logger.log_event('be_set', symbol=symbol, side=side, entry=entry,
                                     float_r=round(float_r, 2))
                    tg(f'⚡ <b>BE {symbol}</b>\n+1.0R → SL → BE ${entry:,.4f}')

                # TP1: close TP1_SIZE_PCT of position, move SL to BE
                if not pos['tp1_hit'] and float_r >= TP1_R:
                    tp1_size = round(pos['size'] * TP1_SIZE_PCT, 4)
                    if tp1_size >= 0.001:
                        close_side = 'sell' if side == 'long' else 'buy'
                        place_order(symbol, close_side, tp1_size, reduce_only=True)
                        pos['remaining'] -= tp1_size
                        pos['tp1_hit'] = True
                        pos['sl'] = entry
                        metrics.inc('tp_hits_total')
                        logger.log_event('tp_hit', target='TP1', symbol=symbol, side=side,
                                         price=price, size_closed=tp1_size,
                                         float_r=round(float_r, 2))
                        tg(f'✂️ <b>TP1 {symbol} +{TP1_R}R</b>\nClosed {int(TP1_SIZE_PCT*100)}% at ${price:,.4f}\nSL → BE ${entry:,.4f}')

                # TP2: close TP2_SIZE_PCT of position
                if pos['tp1_hit'] and not pos['tp2_hit'] and float_r >= TP2_R:
                    tp2_size = round(pos['size'] * TP2_SIZE_PCT, 4)
                    if tp2_size >= 0.001:
                        close_side = 'sell' if side == 'long' else 'buy'
                        place_order(symbol, close_side, tp2_size, reduce_only=True)
                        pos['remaining'] -= tp2_size
                        pos['tp2_hit'] = True
                        remaining_pct = int((1 - TP1_SIZE_PCT) * (1 - TP2_SIZE_PCT) * 100)
                        metrics.inc('tp_hits_total')
                        logger.log_event('tp_hit', target='TP2', symbol=symbol, side=side,
                                         price=price, size_closed=tp2_size,
                                         float_r=round(float_r, 2))
                        tg(f'✂️ <b>TP2 {symbol} +{TP2_R}R</b>\nClosed {int(TP2_SIZE_PCT*100)}% at ${price:,.4f}\n{remaining_pct}% trailing...')

                # Trailing Stop (after TP1)
                if pos['tp1_hit']:
                    trail_dist = atr * TRAIL_ATR_MULT
                    if side == 'long':
                        new_trail = price - trail_dist
                        if pos['trail_sl'] is None or new_trail > pos['trail_sl']:
                            pos['trail_sl'] = new_trail
                        if price <= pos['trail_sl']:
                            ok, _ = close_position_safe(symbol, 'long')
                            if not ok:
                                continue
                            tg(f'📉 <b>TRAIL {symbol}</b>\n${price:,.4f} | Trail ${pos["trail_sl"]:,.4f}\nPnL:{pnl_pct:+.2f}%')
                            log_trade(symbol, price, 'Trailing Stop', pnl_pct > 0, pnl_pct)
                            record_result(symbol, pnl_pct > 0, pnl_pct)
                            reset_pos(symbol)
                            continue
                    else:
                        new_trail = price + trail_dist
                        if pos['trail_sl'] is None or new_trail < pos['trail_sl']:
                            pos['trail_sl'] = new_trail
                        if price >= pos['trail_sl']:
                            ok, _ = close_position_safe(symbol, 'short')
                            if not ok:
                                continue
                            tg(f'📉 <b>TRAIL {symbol}</b>\n${price:,.4f}\nPnL:{pnl_pct:+.2f}%')
                            log_trade(symbol, price, 'Trailing Stop', pnl_pct > 0, pnl_pct)
                            record_result(symbol, pnl_pct > 0, pnl_pct)
                            reset_pos(symbol)
                            continue

                # Phase B — dual-write current position state on every active monitor tick
                # (peak_pnl, trail_sl, tp1_hit, be_set, sl могут меняться → персистим)
                db.upsert_position(symbol, pos)

            except Exception as e:
                print(f'[MONITOR ERROR] {symbol}: {e}')


# ════════════════════════════════════════════════════════════
# WEEKLY TELEGRAM REPORT
# ════════════════════════════════════════════════════════════
_last_weekly = {'week': ''}

def send_weekly_report():
    week_key = datetime.now(timezone.utc).strftime('%Y-W%U')
    if _last_weekly['week'] == week_key:
        return
    _last_weekly['week'] = week_key

    if not trades_log:
        tg('📊 <b>Недельный отчёт</b>\nСделок не было.')
        return

    total = len(trades_log)
    wins  = sum(1 for t in trades_log if t['won'])
    pnl   = sum(t['pnl_pct'] for t in trades_log)

    by_sym = {}
    for t in trades_log:
        s = t['symbol']
        if s not in by_sym:
            by_sym[s] = {'t': 0, 'w': 0}
        by_sym[s]['t'] += 1
        if t['won']:
            by_sym[s]['w'] += 1

    sym_lines = '\n'.join(
        f'  {s}: {d["t"]} сд | {d["w"]/d["t"]*100:.0f}% WR'
        for s, d in by_sym.items()
    )
    balance = get_balance()

    tg(
        f'📊 <b>Недельный отчёт CrossX Pro v3.0</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Баланс: <b>${balance:.2f}</b>\n'
        f'Сделок: <b>{total}</b> | WR: <b>{wins/total*100:.1f}%</b>\n'
        f'PnL итого: <b>{pnl:+.3f}%</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'По парам:\n{sym_lines}\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Obsidian обновится в 04:00'
    )


# ════════════════════════════════════════════════════════════
# KEEP-ALIVE
# ════════════════════════════════════════════════════════════
def keep_alive():
    while True:
        time.sleep(540)
        if RENDER_URL:
            try:
                requests.get(f'{RENDER_URL}/ping', timeout=5)
            except Exception:
                pass
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 20:
            try:
                send_weekly_report()
            except Exception as e:
                print(f'[WEEKLY] {e}')

@app.get('/ping')
async def ping():
    return {'status': 'ok'}


# ════════════════════════════════════════════════════════════
# WEBHOOK
# ════════════════════════════════════════════════════════════
@app.post('/webhook')
async def webhook(request: Request):
    t_signal = time.time()  # Patch 7 — execution metrics anchor
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({'error': 'invalid JSON'}, status_code=400)

    if data.get('token') != WEBHOOK_TOKEN:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    reset_daily()

    symbol = data.get('symbol', 'BTCUSDT').upper().replace('/', '').replace('-', '')
    if symbol not in SYMBOLS:
        return JSONResponse({'error': f'Unknown symbol: {symbol}. Supported: {SYMBOLS}'}, status_code=400)

    action = data.get('action', '').lower()
    pos    = positions[symbol]

    # Manual close
    if action in ('close_long', 'close_short'):
        if pos['active']:
            side = 'long' if action == 'close_long' else 'short'
            ok, resp = close_position_safe(symbol, side)
            if not ok:
                return JSONResponse({'status': 'close_failed', 'reason': str(resp)[:200]}, status_code=500)
            price = get_price(symbol) or pos['entry']
            pnl_pct = 0.0
            if price and pos['entry']:
                pnl_pct = ((price - pos['entry']) / pos['entry'] * 100) if side == 'long' \
                          else ((pos['entry'] - price) / pos['entry'] * 100)
            tg(f'⚪ <b>Manual Close {symbol} {side.upper()}</b>\n${price:,.4f} | PnL: {pnl_pct:+.2f}%')
            log_trade(symbol, price, 'Manual Close', pnl_pct > 0, pnl_pct)
            record_result(symbol, pnl_pct > 0, pnl_pct)
            reset_pos(symbol)
        return JSONResponse({'status': 'closed'})

    if action not in ('buy', 'sell'):
        return JSONResponse({'error': f'unknown action: {action}'})

    # Patch P9 — define `side` BEFORE any check that uses it
    side = 'long' if action == 'buy' else 'short'

    # Patch 6 — anti-duplicate webhook (hash: symbol+side+5m bucket)
    if is_duplicate_signal(symbol, side):
        log_signal(symbol, action, 0, {}, 'filtered', 'Duplicate webhook (dedupe hash)', '')
        return JSONResponse({'status': 'duplicate', 'reason': 'duplicate hash'})

    # Guards
    session_name, _ = get_session()

    if daily['halted']:
        log_signal(symbol, action, 0, {}, 'halted', 'Daily loss limit / session halt', session_name)
        return JSONResponse({'status': 'halted', 'reason': 'Daily loss limit / session halt'})

    if is_session_blocked():
        log_signal(symbol, action, 0, {}, 'filtered', f'{session_name} — low liquidity', session_name)
        return JSONResponse({'status': 'filtered', 'reason': f'{session_name} — trading blocked (low liquidity)'})

    # Asian session (0-7 UTC): longs only — crypto has upward bias in Asian hours
    if session_name == 'Asian' and side == 'short':
        log_signal(symbol, action, 0, {}, 'filtered', 'Asian session — shorts blocked (upward bias)', session_name)
        return JSONResponse({'status': 'filtered', 'reason': 'Asian session — shorts blocked'})

    # Patch 4 — global loss-streak pause (3 losses → 1h)
    sp_active, sp_reason = is_streak_paused()
    if sp_active:
        log_signal(symbol, action, 0, {}, 'paused', sp_reason, session_name)
        return JSONResponse({'status': 'paused', 'reason': sp_reason})

    # Patch 1 — global cooldown after any closed trade
    gc_active, gc_reason = is_global_cooldown_active()
    if gc_active:
        log_signal(symbol, action, 0, {}, 'filtered', gc_reason, session_name)
        return JSONResponse({'status': 'cooldown', 'reason': gc_reason})

    # Patch 2 — pair cooldown after every closed trade on the symbol
    pc_active, pc_reason = is_pair_cooldown_active(symbol)
    if pc_active:
        log_signal(symbol, action, 0, {}, 'filtered', pc_reason, session_name)
        return JSONResponse({'status': 'cooldown', 'reason': pc_reason})

    # Patch 3 — trade limits (per hour & per session)
    tl_ok, tl_reason = check_trade_limits()
    if not tl_ok:
        log_signal(symbol, action, 0, {}, 'filtered', tl_reason, session_name)
        return JSONResponse({'status': 'filtered', 'reason': tl_reason})

    paused, pause_reason = is_paused(symbol)
    if paused:
        log_signal(symbol, action, 0, {}, 'paused', pause_reason, session_name)
        return JSONResponse({'status': 'paused', 'reason': pause_reason})

    if pos['active']:
        log_signal(symbol, action, 0, {}, 'skipped', f'{symbol} position already open', session_name)
        return JSONResponse({'status': 'skipped', 'reason': f'{symbol} position already open'})

    corr_blocked, corr_reason = is_correlated_blocked(symbol, side)
    if corr_blocked:
        log_signal(symbol, action, 0, {}, 'filtered', corr_reason, session_name)
        return JSONResponse({'status': 'filtered', 'reason': corr_reason})

    # Balance
    balance = get_balance()
    if not balance:
        return JSONResponse({'error': 'balance error'}, status_code=500)

    if check_daily_loss(balance):
        log_signal(symbol, action, 0, {}, 'halted', 'Daily loss limit hit', session_name)
        return JSONResponse({'status': 'halted'})

    # Candles + ATR
    candles_5m = get_candles(symbol, '5m', 60)
    if not candles_5m:
        return JSONResponse({'error': 'candle error'}, status_code=500)

    price = float(candles_5m[-1][4])
    atr   = calc_atr(candles_5m)
    if atr == 0:
        atr = price * 0.005

    # Score
    score, breakdown = score_signal(symbol, side, candles_5m, data)
    if score < MIN_SCORE:
        log_signal(symbol, action, score, breakdown, 'filtered', f'Score {score} < {MIN_SCORE}', session_name)
        print(f'[FILTER] {symbol} Score {score}/100 < {MIN_SCORE}')
        return JSONResponse({'status': 'filtered', 'score': score, 'reason': f'Score {score}/100 < {MIN_SCORE}', 'breakdown': breakdown})

    # MTF gate
    mtf_ok, mtf_info = check_mtf(symbol, side)
    if not mtf_ok:
        log_signal(symbol, action, score, breakdown, 'filtered', f'MTF: {mtf_info}', session_name)
        return JSONResponse({'status': 'filtered', 'reason': mtf_info})

    # News filter
    news_ok, news_reason = check_news()
    if not news_ok:
        log_signal(symbol, action, score, breakdown, 'filtered', f'News: {news_reason}', session_name)
        tg(f'📰 <b>News Filter</b>\n{news_reason}')
        return JSONResponse({'status': 'filtered', 'reason': news_reason})

    # Session risk multiplier
    _, risk_mult = get_session()
    high_vol = is_high_volatility(atr, price)

    # ATR volatility filter — skip if market too quiet (per-symbol threshold)
    atr_pct     = atr / price
    min_atr_pct = MIN_ATR_PCT.get(symbol, 0.0005)
    if atr_pct < min_atr_pct:
        log_signal(symbol, action, score, breakdown, 'filtered', f'ATR {atr_pct*100:.3f}% < min {min_atr_pct*100:.3f}%', session_name)
        print(f'[FILTER] {symbol} ATR {atr_pct*100:.3f}% < {min_atr_pct*100:.3f}% (market too quiet)')
        return JSONResponse({'status': 'filtered', 'reason': f'{symbol} ATR {atr_pct*100:.3f}% < {min_atr_pct*100:.3f}% — market too quiet'})

    # Risk + sizing
    risk_pct = get_risk_pct(score, atr, price) * risk_mult
    risk_usd  = balance * risk_pct
    sl_dist   = atr * SL_ATR_MULT
    size      = round(risk_usd / sl_dist, 4)
    if size < 0.001:
        size = 0.001
    # Cap: never use more than 20% of balance as margin (allows up to 5 concurrent positions)
    max_size = round((balance * 0.20 * LEVERAGE) / price, 4)
    if size > max_size:
        size = max_size
        print(f'[SIZE CAP] {symbol} capped to {size} (20% margin limit)')

    # Levels
    if side == 'long':
        sl_price  = round(price - sl_dist, 4)
        tp1_price = round(price + sl_dist * TP1_R, 4)
        tp2_price = round(price + sl_dist * TP2_R, 4)
    else:
        sl_price  = round(price + sl_dist, 4)
        tp1_price = round(price - sl_dist * TP1_R, 4)
        tp2_price = round(price - sl_dist * TP2_R, 4)

    # Log signal as taken before placing order
    log_signal(symbol, action, score, breakdown, 'taken',
               f'Entry ${price:.4f} | ATR {atr_pct*100:.3f}% | risk {risk_pct*100:.2f}%', session_name)

    # Place order
    set_leverage(symbol)
    result = place_order(symbol, 'buy' if side == 'long' else 'sell', size)

    if result.get('code') != '00000':
        log_execution(symbol, side, t_signal, time.time(), price, price, 'rejected', str(result)[:120])
        metrics.inc('orders_failed_total')
        logger.log_error('order_failed', symbol=symbol, side=side,
                         error=str(result)[:200], score=score)
        tg(f'❌ Order failed {symbol}: {result}')
        return JSONResponse({'error': result}, status_code=500)

    t_entry = time.time()
    log_execution(symbol, side, t_signal, t_entry, price, price, 'success')
    latency_ms = int((t_entry - t_signal) * 1000)
    metrics.inc('orders_submitted_total')
    metrics.inc('trades_opened_total')
    metrics.add_obs('execution_latency_ms', latency_ms)
    metrics.gauge('avg_execution_latency_ms', metrics.avg('execution_latency_ms'))
    logger.log_event('order_submitted', symbol=symbol, side=side, size=size,
                     entry=price, sl=sl_price, score=score, session=session_name,
                     latency_ms=latency_ms, risk_pct=round(risk_pct * 100, 2))

    # Update state
    now = datetime.now(timezone.utc)
    positions[symbol].update(
        active=True, side=side, entry=price, size=size, remaining=size,
        sl=sl_price, atr=atr, r=sl_dist,
        tp1_hit=False, tp2_hit=False, peak_pnl=0.0, trail_sl=None,
        entry_time=now, ref_price=price, ref_time=now,
        score=score, session=session_name,
        be_set=False,
    )
    db.upsert_position(symbol, positions[symbol])  # dual-write

    emoji = '🟢' if side == 'long' else '🔴'
    breakdown_str = '\n'.join([f'  {k}: {v}' for k, v in breakdown.items()])
    tg(
        f'{emoji} <b>{symbol} {"LONG" if side == "long" else "SHORT"}</b>\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Score: <b>{score}/100</b>\n{breakdown_str}\n'
        f'━━━━━━━━━━━━━━━━━━━\n'
        f'Entry: <b>${price:,.4f}</b>\n'
        f'Size:  {size} (${size*price:,.0f})\n'
        f'ATR:   ${atr:,.4f} {"⚠️ HIGH VOL" if high_vol else ""}\n'
        f'SL:    ${sl_price:,.4f}\n'
        f'TP1:   ${tp1_price:,.4f} (+{TP1_R}R → {int(TP1_SIZE_PCT*100)}%)\n'
        f'TP2:   ${tp2_price:,.4f} (+{TP2_R}R → {int(TP2_SIZE_PCT*100)}%)\n'
        f'Risk:  ${risk_usd:.2f} ({risk_pct*100:.1f}%)\n'
        f'Session: {session_name} | MTF: {mtf_info}'
    )
    print(f'[ORDER] {symbol} {side.upper()} | score={score} | price={price}')

    return JSONResponse({
        'status': f'{symbol} {side} opened',
        'score': score, 'price': price,
        'sl': sl_price, 'tp1': tp1_price, 'tp2': tp2_price,
        'atr': round(atr, 4), 'risk_usd': round(risk_usd, 2),
        'session': session_name,
    })


# ════════════════════════════════════════════════════════════
# TRADES & REPORT
# ════════════════════════════════════════════════════════════
@app.get('/trades')
async def get_trades():
    return {'count': len(trades_log), 'trades': trades_log}


@app.get('/report')
async def get_report():
    if not trades_log:
        return {'total_trades': 0, 'message': 'No trades yet (resets on restart).'}

    total = len(trades_log)
    wins  = sum(1 for t in trades_log if t['won'])
    total_pnl = sum(t['pnl_pct'] for t in trades_log)

    by_symbol = {}
    for t in trades_log:
        s = t['symbol']
        if s not in by_symbol:
            by_symbol[s] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        by_symbol[s]['trades'] += 1
        if t['won']: by_symbol[s]['wins'] += 1
        by_symbol[s]['pnl'] = round(by_symbol[s]['pnl'] + t['pnl_pct'], 3)

    by_score = {}
    for t in trades_log:
        s = t['score']
        b = '95+' if s >= 95 else ('90-94' if s >= 90 else ('85-89' if s >= 85 else ('80-84' if s >= 80 else '75-79')))
        if b not in by_score: by_score[b] = {'trades': 0, 'wins': 0}
        by_score[b]['trades'] += 1
        if t['won']: by_score[b]['wins'] += 1

    by_session = {}
    for t in trades_log:
        k = t.get('session') or 'Unknown'
        if k not in by_session: by_session[k] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        by_session[k]['trades'] += 1
        if t['won']: by_session[k]['wins'] += 1
        by_session[k]['pnl'] = round(by_session[k]['pnl'] + t['pnl_pct'], 3)

    by_reason = {}
    for t in trades_log:
        r = t['exit_reason']
        if r not in by_reason: by_reason[r] = {'trades': 0, 'wins': 0}
        by_reason[r]['trades'] += 1
        if t['won']: by_reason[r]['wins'] += 1

    return {
        'total_trades': total, 'wins': wins, 'losses': total - wins,
        'win_rate': round(wins / total * 100, 1),
        'total_pnl_pct': round(total_pnl, 3),
        'avg_pnl_pct': round(total_pnl / total, 3),
        'by_symbol': by_symbol,
        'by_score': by_score,
        'by_session': by_session,
        'by_exit_reason': by_reason,
        'recent_5': trades_log[-5:],
    }


# ════════════════════════════════════════════════════════════
# STATUS
# ════════════════════════════════════════════════════════════
@app.get('/status')
async def status():
    reset_daily()
    balance  = get_balance()
    equity   = get_equity() or balance
    loss_pct = 0.0
    if daily['start_balance'] and equity:
        loss_pct = (daily['start_balance'] - equity) / daily['start_balance'] * 100

    session_name, _ = get_session()

    active_positions = []
    for sym in SYMBOLS:
        p = positions[sym]
        if p['active']:
            price = get_price(sym)
            float_r = 0.0
            if price and p['r']:
                float_r = ((price - p['entry']) / p['r']) if p['side'] == 'long' \
                          else ((p['entry'] - price) / p['r'])
            active_positions.append({
                'symbol': sym, 'side': p['side'],
                'entry': p['entry'], 'current_price': price,
                'float_r': round(float_r, 2), 'sl': p['sl'],
                'trail_sl': p['trail_sl'],
                'tp1_hit': p['tp1_hit'], 'tp2_hit': p['tp2_hit'],
                'peak_pnl': f'{p["peak_pnl"]:.2f}%',
                'score': p['score'],
            })

    sym_info = {}
    for sym in SYMBOLS:
        st = sym_state[sym]
        paused, _ = is_paused(sym)
        sym_info[sym] = {
            'win_streak': st['win_streak'],
            'loss_streak': st['loss_streak'],
            'paused': paused,
            'active': positions[sym]['active'],
        }

    return {
        'bot': 'CrossX Pro Bot v3.0',
        'symbols': SYMBOLS,
        'balance': f'${balance:.2f}' if balance else 'error',
        'equity': f'${equity:.2f}' if equity else 'error',
        'daily_loss': f'{max(loss_pct, 0):.2f}%',
        'daily_pnl': f'${daily["pnl"]:.2f}',
        'halted': daily['halted'],
        'session': session_name,
        'daily_trades': daily['trades'],
        'daily_wins': daily['wins'],
        'daily_losses': daily['losses'],
        'active_positions': active_positions,
        'symbols_state': sym_info,
        'settings': {
            'timeframe': '5m', 'leverage': f'{LEVERAGE}x',
            'base_risk': f'{BASE_RISK_PCT*100:.2f}%',
            'high_vol_risk': f'{HIGH_VOL_RISK*100:.2f}%',
            'streak_risk': f'{STREAK_RISK*100:.2f}%',
            'min_score': MIN_SCORE, 'sl': f'ATR×{SL_ATR_MULT}',
            'tp1': f'+{TP1_R}R ({int(TP1_SIZE_PCT*100)}%)', 'tp2': f'+{TP2_R}R ({int(TP2_SIZE_PCT*100)}%)',
            'trail': f'ATR×{TRAIL_ATR_MULT}', 'daily_stop': f'-{DAILY_LOSS_LIMIT*100:.0f}%',
            'global_cooldown_min': GLOBAL_COOLDOWN_MIN,
            'pair_cooldown_min': PAIR_COOLDOWN_MIN,
            'max_trades_hour': MAX_TRADES_HOUR,
            'max_trades_session': MAX_TRADES_SESSION,
            'dedupe_ttl_sec': DEDUPE_TTL_SEC,
        }
    }

@app.get('/signals')
async def get_signals():
    total = len(signals_log)
    taken = [s for s in signals_log if s['status'] == 'taken']
    filtered = [s for s in signals_log if s['status'] == 'filtered']
    paused = [s for s in signals_log if s['status'] == 'paused']

    # Filter reason breakdown
    reasons = {}
    for s in filtered + paused:
        key = s['reason'].split('—')[0].split(':')[0].strip()[:40]
        reasons[key] = reasons.get(key, 0) + 1

    # Score distribution of filtered signals
    scored_filtered = [s for s in filtered if s['score'] > 0]
    avg_filtered_score = round(sum(s['score'] for s in scored_filtered) / len(scored_filtered), 1) if scored_filtered else 0
    avg_taken_score = round(sum(s['score'] for s in taken) / len(taken), 1) if taken else 0

    # By symbol
    by_symbol = {}
    for sym in SYMBOLS:
        sym_sigs = [s for s in signals_log if s['symbol'] == sym]
        by_symbol[sym] = {
            'total': len(sym_sigs),
            'taken': len([s for s in sym_sigs if s['status'] == 'taken']),
            'filtered': len([s for s in sym_sigs if s['status'] != 'taken']),
        }

    return {
        'total_signals': total,
        'taken': len(taken),
        'filtered': len(filtered) + len(paused),
        'filter_rate': f'{(len(filtered)+len(paused))/total*100:.0f}%' if total else '0%',
        'avg_score_taken': avg_taken_score,
        'avg_score_filtered': avg_filtered_score,
        'filter_reasons': reasons,
        'by_symbol': by_symbol,
        'last_10': signals_log[-10:][::-1],
    }


@app.get('/reset-daily')
async def reset_daily_endpoint():
    """Reset daily halt/stats — call after midnight or after false halt."""
    daily.update(date=datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                 start_balance=None, halted=False,
                 trades=0, wins=0, losses=0, pnl=0.0,
                 loss_streak=0, streak_pause_until=None,
                 last_close_time=None, peak_pnl=0.0)
    db.save_state_global(daily)  # dual-write
    return {'status': 'daily stats reset', 'date': daily['date']}


def _check_auth(token):
    """Validate header token against WEBHOOK_TOKEN. Constant-time compare to prevent timing leaks."""
    if not WEBHOOK_TOKEN or WEBHOOK_TOKEN == 'change_me':
        return False
    return hmac.compare_digest(str(token or ''), WEBHOOK_TOKEN)


@app.get('/backup')
async def backup_endpoint(x_auth_token: str = Header(default='')):
    """JSON snapshot of recent persisted state. Header X-Auth-Token: <WEBHOOK_TOKEN>."""
    if not _check_auth(x_auth_token):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return {
        'db_path': db.db_path(),
        'db_ready': db.is_ready(),
        'counts': db.stats(),
        'trades_recent': db.load_trades(200),
        'signals_recent': db.load_signals(100),
        'metrics_recent': db.load_metrics(100),
        'positions_active': db.load_active_positions(),
        'state_global': db.load_state_global(),
        'state_symbol': {s: db.load_state_symbol(s) for s in SYMBOLS},
    }


@app.get('/db-stats')
async def db_stats_endpoint(x_auth_token: str = Header(default='')):
    if not _check_auth(x_auth_token):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return {'db_path': db.db_path(), 'ready': db.is_ready(), 'counts': db.stats()}


@app.get('/daily-report')
async def daily_report_endpoint():
    return {'report_text': build_daily_report()}


@app.get('/metrics')
async def metrics_endpoint():
    """Combined metrics view: legacy execution log + new counters/gauges."""
    # Legacy execution log (unchanged)
    if metrics_log:
        avg_lat  = sum(m['latency_ms']  for m in metrics_log) / len(metrics_log)
        avg_slip = sum(m['slippage_pct'] for m in metrics_log) / len(metrics_log)
        rejected = sum(1 for m in metrics_log if m['status'] == 'rejected')
        legacy = {
            'count': len(metrics_log),
            'rejected': rejected,
            'avg_latency_ms': round(avg_lat, 1),
            'avg_slippage_pct': round(avg_slip, 4),
            'last_20': metrics_log[-20:],
        }
    else:
        legacy = {'count': 0, 'avg_latency_ms': 0, 'avg_slippage_pct': 0}

    # New counters/gauges (Phase 1 observability)
    snap = metrics.snapshot()
    return {
        'execution': legacy,
        'counters': snap.get('counters', {}),
        'gauges': snap.get('gauges', {}),
        'observations': snap.get('observations', {}),
    }


@app.get('/health')
async def health_endpoint():
    """Quick health snapshot — safe for external uptime monitors (no auth required)."""
    try:
        bal_ok = (get_balance() or 0) > 0
    except Exception:
        bal_ok = False
    return {
        'status': 'ok' if (db.is_ready() and logger.is_ready() and bal_ok) else 'degraded',
        'checks': {
            'db_ready': db.is_ready(),
            'logger_ready': logger.is_ready(),
            'bitget_balance_ok': bal_ok,
            'halted': bool(daily.get('halted')),
            'active_positions': sum(1 for p in positions.values() if p['active']),
            'session': get_session()[0],
        },
        'ts': datetime.now(timezone.utc).isoformat(),
    }


@app.get('/diagnostics')
async def diagnostics_endpoint(x_auth_token: str = Header(default='')):
    """Deep operational diagnostics. Auth-required (leaks runtime state)."""
    if not _check_auth(x_auth_token):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    now = datetime.now(timezone.utc)
    # Session-PnL gauges (computed on-demand from trades_log so always fresh)
    today_str = now.strftime('%Y-%m-%d')
    sess_pnl = {'Asian': 0.0, 'London': 0.0, 'London/NY Overlap': 0.0, 'New York': 0.0}
    for t in trades_log:
        if t.get('date') == today_str:
            s = t.get('session', '')
            if s in sess_pnl:
                sess_pnl[s] += t.get('pnl_pct', 0)
    metrics.gauge('session_pnl_asian', sess_pnl['Asian'])
    metrics.gauge('session_pnl_london', sess_pnl['London'])
    metrics.gauge('session_pnl_overlap', sess_pnl['London/NY Overlap'])
    metrics.gauge('session_pnl_ny', sess_pnl['New York'])

    # Avg signal score (taken signals only)
    taken = [s for s in signals_log if s['status'] == 'taken' and s['score'] > 0]
    metrics.gauge('avg_signal_score',
                  (sum(s['score'] for s in taken) / len(taken)) if taken else 0)

    return {
        'ts': now.isoformat(),
        'version': 'v3.5',
        'uptime_check_only': True,
        'db': {
            'ready': db.is_ready(),
            'path': db.db_path(),
            'counts': db.stats(),
        },
        'logger': {
            'ready': logger.is_ready(),
            'log_dir': logger.log_dir(),
        },
        'state': {
            'halted': bool(daily.get('halted')),
            'loss_streak': daily.get('loss_streak', 0),
            'streak_paused_until': str(daily.get('streak_pause_until')) if daily.get('streak_pause_until') else None,
            'last_close_time': str(daily.get('last_close_time')) if daily.get('last_close_time') else None,
            'daily_pnl_pct': daily.get('pnl', 0),
            'daily_peak_pnl_pct': daily.get('peak_pnl', 0),
            'daily_trades': daily.get('trades', 0),
        },
        'positions_active': sum(1 for p in positions.values() if p['active']),
        'symbols': {s: {
            'win_streak': sym_state[s].get('win_streak', 0),
            'loss_streak': sym_state[s].get('loss_streak', 0),
            'pair_cooldown_until': str(sym_state[s].get('pair_cooldown_until')) if sym_state[s].get('pair_cooldown_until') else None,
            'sl_cooldown_until': str(sym_state[s].get('sl_cooldown_until')) if sym_state[s].get('sl_cooldown_until') else None,
        } for s in SYMBOLS},
        'session_pnl_today': sess_pnl,
        'metrics': metrics.snapshot(),
        'signals_log_size': len(signals_log),
        'trades_log_size': len(trades_log),
        'metrics_log_size': len(metrics_log),
        'dedupe_hash_table_size': len(_signal_dedupe),
    }

@app.get('/')
async def home():
    return {'status': 'running', 'bot': 'CrossX Pro Bot v3.0', 'symbols': SYMBOLS}


# ════════════════════════════════════════════════════════════
# STATE RESTORATION & RECONCILIATION (Phase B — SQLite persistence)
# ════════════════════════════════════════════════════════════
def restore_state():
    """Load persisted state into in-memory globals on startup. Safe if DB empty."""
    if not db.is_ready():
        return

    # Trades — full history needed for /report stats
    persisted_trades = db.load_trades(1000) or []
    if persisted_trades:
        trades_log.extend(persisted_trades)
        print(f'[DB] restored {len(persisted_trades)} trades')

    # Signals — recent only (for /signals analytics + dedupe context)
    persisted_sigs = db.load_signals(500) or []
    if persisted_sigs:
        signals_log.extend(persisted_sigs)
        print(f'[DB] restored {len(persisted_sigs)} signals')

    # Metrics — recent
    persisted_metrics = db.load_metrics(500) or []
    if persisted_metrics:
        metrics_log.extend(persisted_metrics)
        print(f'[DB] restored {len(persisted_metrics)} metrics')

    # Global daily state — restore ONLY if DB date == today UTC, else fresh start
    g = db.load_state_global() or {}
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if g.get('date') == today_utc:
        for k in ('start_balance', 'halted', 'trades', 'wins', 'losses',
                  'pnl', 'loss_streak', 'peak_pnl'):
            if k in g and g[k] is not None:
                daily[k] = g[k]
        for k in ('streak_pause_until', 'last_close_time'):
            v = g.get(k)
            if isinstance(v, str):
                try:
                    daily[k] = datetime.fromisoformat(v)
                except Exception:
                    daily[k] = None
            elif v is None:
                daily[k] = None
        daily['date'] = today_utc
        print(f'[DB] restored daily state for {today_utc} '
              f'(loss_streak={daily.get("loss_streak")}, halted={daily.get("halted")})')
    elif g:
        print(f'[DB] saved daily was for {g.get("date")}; today is {today_utc} — fresh start')

    # Per-symbol state (cooldowns, streaks)
    for sym in SYMBOLS:
        s = db.load_state_symbol(sym)
        if s:
            sym_state[sym].update(s)
    restored_syms = [s for s in SYMBOLS if sym_state[s].get('loss_streak', 0) > 0
                     or sym_state[s].get('pair_cooldown_until')
                     or sym_state[s].get('sl_cooldown_until')
                     or sym_state[s].get('pause_until')]
    if restored_syms:
        print(f'[DB] restored sym_state with active flags: {", ".join(restored_syms)}')


def reconcile_positions():
    """Compare DB positions vs live Bitget. Conservative: never auto-trade unknown."""
    if not db.is_ready():
        return
    metrics.inc('reconcile_runs_total')
    logger.log_event('reconcile_started')
    persisted = db.load_active_positions() or {}
    try:
        live = get_all_positions_bitget()
    except Exception as e:
        print(f'[RECON] live fetch failed: {e}')
        metrics.inc('reconcile_failures_total')
        logger.log_error('reconcile_failure', stage='live_fetch', error=str(e)[:200])
        live = {}

    restored_n = 0
    orphan_n = 0
    unmanaged_n = 0

    # 1. Persisted positions — try to restore those still alive on Bitget
    for sym, pdata in persisted.items():
        if sym not in SYMBOLS:
            continue
        live_p = live.get(sym)
        if live_p and live_p['side'] == pdata['side']:
            # Restore in-memory — bot will resume management via monitor loop
            for k, v in pdata.items():
                if k in positions[sym]:
                    positions[sym][k] = v
            positions[sym]['active'] = True
            tg(f'⚡ <b>POSITION RESTORED {sym}</b>\n'
               f'{pdata["side"].upper()} @ ${pdata["entry"]:,.4f} | SL ${pdata["sl"]:,.4f}\n'
               f'tp1_hit={pdata["tp1_hit"]} | resuming monitoring')
            print(f'[RECON] restored {sym} {pdata["side"]} @ {pdata["entry"]}')
            logger.log_event('reconcile_restored', symbol=sym, side=pdata['side'],
                             entry=pdata['entry'], sl=pdata['sl'],
                             tp1_hit=pdata['tp1_hit'])
            restored_n += 1
        else:
            # DB had open, Bitget says no → mark inactive in DB, alert
            db.clear_position(sym)
            metrics.inc('reconcile_warnings_total')
            logger.log_warning('reconcile_warning', kind='orphan_in_db', symbol=sym,
                               side=pdata.get('side'))
            tg(f'⚠️ <b>RECON {sym}</b>\n'
               f'DB had open {pdata["side"]} but Bitget shows no/different position. '
               f'Cleared from DB, no auto-trade.')
            print(f'[RECON] {sym}: DB had {pdata["side"]}, Bitget=mismatch')
            orphan_n += 1

    # 2. Bitget-only positions (bot doesn't know about them) — alert, don't manage
    unknown = set(live.keys()) - set(persisted.keys())
    unknown &= set(SYMBOLS)
    for sym in unknown:
        metrics.inc('unmanaged_positions_total')
        metrics.inc('reconcile_warnings_total')
        logger.log_warning('reconcile_warning', kind='unmanaged_on_exchange',
                           symbol=sym, side=live[sym]['side'], total=live[sym]['total'])
        tg(f'⚠️ <b>UNMANAGED POSITION {sym}</b>\n'
           f'Bitget has {live[sym]["side"]} {live[sym]["total"]} but bot has no record. '
           f'Manual close recommended; bot will not auto-manage it.')
        print(f'[RECON] unknown live position: {sym} {live[sym]}')
        unmanaged_n += 1

    logger.log_event('reconcile_completed',
                     restored=restored_n, orphans=orphan_n, unmanaged=unmanaged_n,
                     persisted=len(persisted), live=len(live))


# ════════════════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════════════════
@app.on_event('startup')
async def startup():
    _sync_time()

    # Observability — structured logging + metrics
    try:
        logger.setup_logger(LOG_DIR)
        metrics.init_canonical()
        logger.log_event('startup', version='v3.5', symbols=SYMBOLS,
                         min_score=MIN_SCORE, leverage=LEVERAGE,
                         db_path=DB_PATH, log_dir=LOG_DIR)
    except Exception as e:
        print(f'[STARTUP] observability init failed (non-fatal): {e}')

    # Phase B — persistence
    try:
        db.init_db(DB_PATH)
        restore_state()
        reconcile_positions()
    except Exception as e:
        print(f'[STARTUP] DB init/restore failed (non-fatal): {e}')
        logger.log_error('startup_db_failure', error=str(e))

    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=tg_polling, daemon=True).start()
    print('[BOT] CrossX Pro Bot v3.4 started —', ', '.join(SYMBOLS))
    tg(
        '🚀 <b>CrossX Pro Bot v3.4 Online</b>\n'
        '━━━━━━━━━━━━━━━━━━━\n'
        f'Пары: {" | ".join(SYMBOLS)}\n'
        f'Сессии: Asian + London + NY\n'
        f'Risk: {BASE_RISK_PCT*100:.2f}% base | SL: ATR×{SL_ATR_MULT}\n'
        f'TP1: +{TP1_R}R ({int(TP1_SIZE_PCT*100)}%) | TP2: +{TP2_R}R | Lev: {LEVERAGE}x\n'
        f'Score ≥{MIN_SCORE} | Daily stop: -{DAILY_LOSS_LIMIT*100:.0f}%\n'
        f'Persistence: SQLite ({DB_PATH})\n'
        f'Команды: /dashboard /report'
    )


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8080))
    uvicorn.run(app, host='0.0.0.0', port=port)
