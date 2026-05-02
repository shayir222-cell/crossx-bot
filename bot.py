"""
CrossX Pro Bot v3.0 — Multi-Symbol Elite Trading System
BTCUSDT | ETHUSDT | BNBUSDT | SOLUSDT | XRPUSDT
Bitget Futures | 5m Timeframe
"""
import os, json, time, hmac, hashlib, base64, threading
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, date, timezone, timedelta

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

BASE_URL = 'https://api.bitget.com'

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']

# ─── Min ATR % per symbol (filters "sleeping" markets) ───
MIN_ATR_PCT = {
    'BTCUSDT': 0.00080,  # 0.08% → ~$62 min ATR at $78k (fee-efficient at 7x)
    'ETHUSDT': 0.00090,  # 0.09% → ~$3.4 min ATR at $3800
    'BNBUSDT': 0.00120,  # 0.12% → ~$0.74 min ATR at $615
    'SOLUSDT': 0.00130,  # 0.13% → ~$0.20 min ATR at $155
    'XRPUSDT': 0.00150,  # 0.15% → ~$0.003 min ATR at $2.3
}

# ─── Risk ────────────────────────────────────────────────
BASE_RISK_PCT    = 0.01
HIGH_VOL_RISK    = 0.005
STREAK_RISK      = 0.0125
LEVERAGE         = 7     # 10→7: reduces notional 30% → fees 30% lower
DAILY_LOSS_LIMIT = 0.10

# ─── ATR levels ──────────────────────────────────────────
SL_ATR_MULT    = 1.5
TP1_R          = 2.5   # 1.5→2.5: first TP profit must outpace open+close fees
TP2_R          = 5.0   # 3.0→5.0: let full winners run
TRAIL_ATR_MULT = 1.5   # 1.0→1.5: tighter trail protects more peak profit
MAX_GIVEBACK   = 0.30  # 0.35→0.30: give back less at peak
TP1_SIZE_PCT   = 0.40  # 0.30→0.40: lock in more at first TP
TP2_SIZE_PCT   = 0.30  # unchanged

# ─── Score ───────────────────────────────────────────────
MIN_SCORE = 82  # 75→82: only high-confidence setups

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
    {'SOLUSDT', 'BNBUSDT'},        # altcoin group
]

# ─── Time stop ───────────────────────────────────────────
TIME_STOP_MIN  = 30
TIME_STOP_MOVE = 0.003


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
    }

def _make_sym():
    return {'win_streak': 0, 'loss_streak': 0, 'pause_until': None, 'sl_cooldown_until': None}

positions   = {s: _make_pos() for s in SYMBOLS}
sym_state   = {s: _make_sym() for s in SYMBOLS}
trades_log  = []
signals_log = []  # every incoming webhook: taken or filtered, with full reason

daily = {
    'date': '', 'start_balance': None, 'halted': False,
    'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0,
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
        return {}

def api_post(path, body):
    try:
        b = json.dumps(body)
        return requests.post(BASE_URL + path, headers=_h('POST', path, b), data=b, timeout=10).json()
    except Exception as e:
        print(f'[API POST] {path}: {e}')
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
    if ob_os_raw:
        ob_val = ob_os_raw
    else:
        rsi = calc_rsi(candles_5m)
        if rsi < 35:   ob_val = 'oversold'
        elif rsi > 65: ob_val = 'overbought'
        else:          ob_val = 'neutral'

    ob_pts = 10 if (is_long and ob_val in ('oversold', 'neutral')) or \
                   (not is_long and ob_val in ('overbought', 'neutral')) else 0
    score += ob_pts
    breakdown['ob_os'] = f'{ob_val} ({ob_pts}/10)'

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
    if in_ld and in_ny: return 'London/NY Overlap', 1.0
    elif in_ld:         return 'London', 1.0
    elif in_ny:         return 'New York', 1.0
    elif 0 <= h < 7:    return 'Asian', 0.0   # blocked: low liquidity + fee drain
    else:               return 'Off-session', 0.0  # blocked: pre-London dead zone

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
    today = str(date.today())
    if daily['date'] != today:
        daily.update(date=today, start_balance=None, halted=False,
                     trades=0, wins=0, losses=0, pnl=0.0)
        print(f'[DAY] New day: {today}')

def check_daily_loss(balance):
    equity = get_equity() or balance  # use total equity, not just available
    if daily['start_balance'] is None:
        daily['start_balance'] = equity
        return False
    lost = (daily['start_balance'] - equity) / daily['start_balance']
    if lost >= DAILY_LOSS_LIMIT:
        daily['halted'] = True
        tg(f'🛑 <b>DAILY STOP</b>\nLoss: <b>-{lost*100:.1f}%</b>\nEquity: ${equity:.2f}\nStopped until tomorrow.')
        return True
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

def get_risk_pct(score, atr, price):
    if is_high_volatility(atr, price):
        return HIGH_VOL_RISK
    # Scale risk by score quality
    if score >= 90:
        return STREAK_RISK      # 1.25%
    elif score >= 82:
        return BASE_RISK_PCT    # 1.0%
    else:
        return BASE_RISK_PCT * 0.5  # 0.5% for borderline signals

def record_result(symbol, won, pnl_pct):
    daily['trades'] += 1
    daily['pnl'] += pnl_pct
    st = sym_state[symbol]
    if won:
        daily['wins'] += 1
        st['win_streak'] += 1
        st['loss_streak'] = 0
    else:
        daily['losses'] += 1
        st['loss_streak'] += 1
        st['win_streak'] = 0
        # SL cooldown after every loss
        st['sl_cooldown_until'] = datetime.now(timezone.utc) + timedelta(minutes=SL_COOLDOWN)
        if st['loss_streak'] == 2:
            st['pause_until'] = datetime.now(timezone.utc) + timedelta(minutes=PAUSE_2L)
            tg(f'⏸ <b>{symbol}: 2 losses</b> — pausing {PAUSE_2L}min')
        elif st['loss_streak'] >= 3:
            st['pause_until'] = datetime.now(timezone.utc) + timedelta(minutes=PAUSE_3L)
            tg(f'⏸ <b>{symbol}: 3+ losses</b> — pausing {PAUSE_3L}min')


# ════════════════════════════════════════════════════════════
# SIGNAL LOGGING (every webhook — taken or filtered)
# ════════════════════════════════════════════════════════════
def log_signal(symbol, action, score, breakdown, status, reason, session=''):
    signals_log.append({
        'time': datetime.now(timezone.utc).strftime('%H:%M'),
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'symbol': symbol,
        'action': action,
        'score': score,
        'breakdown': breakdown,
        'status': status,   # 'taken', 'filtered', 'paused', 'halted', 'skipped'
        'reason': reason,
        'session': session,
    })
    if len(signals_log) > 500:
        signals_log.pop(0)


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
    print(f'[TRADE #{trade["id"]}] {symbol} {trade["side"].upper()} | {exit_reason} | PnL:{pnl_pct:+.3f}%')


# ════════════════════════════════════════════════════════════
# POSITION RESET
# ════════════════════════════════════════════════════════════
def reset_pos(symbol):
    positions[symbol].update(
        active=False, side=None, entry=0.0, size=0.0, remaining=0.0,
        sl=0.0, atr=0.0, r=0.0, tp1_hit=False, tp2_hit=False,
        peak_pnl=0.0, trail_sl=None, entry_time=None,
        ref_price=0.0, ref_time=None, score=0, session=''
    )


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
                    close_all(symbol, side)
                    tg(f'🔴 <b>SL {symbol}</b>\n${price:,.4f} | SL ${pos["sl"]:,.4f}\nPnL: {pnl_pct:+.2f}%')
                    log_trade(symbol, price, 'SL', False, pnl_pct)
                    record_result(symbol, False, pnl_pct)
                    reset_pos(symbol)
                    continue

                # Max Giveback
                if pos['peak_pnl'] > 0.5:
                    giveback = (pos['peak_pnl'] - pnl_pct) / pos['peak_pnl']
                    if giveback >= MAX_GIVEBACK:
                        close_all(symbol, side)
                        tg(f'💰 <b>MAX GIVEBACK {symbol}</b>\nPeak:{pos["peak_pnl"]:.2f}% → {pnl_pct:.2f}%\n${price:,.4f}')
                        log_trade(symbol, price, 'Max Giveback', pnl_pct > 0, pnl_pct)
                        record_result(symbol, pnl_pct > 0, pnl_pct)
                        reset_pos(symbol)
                        continue

                # Time Stop
                if pos['ref_time'] is None:
                    pos['ref_time'] = now
                    pos['ref_price'] = price
                elif (now - pos['ref_time']).seconds >= TIME_STOP_MIN * 60:
                    move = abs(price - pos['ref_price']) / pos['ref_price']
                    if move < TIME_STOP_MOVE:
                        close_all(symbol, side)
                        tg(f'⏱ <b>TIME STOP {symbol}</b>\n{TIME_STOP_MIN}min, move {move*100:.2f}%\n${price:,.4f}')
                        log_trade(symbol, price, 'Time Stop', pnl_pct > 0, pnl_pct)
                        record_result(symbol, pnl_pct > 0, pnl_pct)
                        reset_pos(symbol)
                        continue
                    else:
                        pos['ref_time'] = now
                        pos['ref_price'] = price

                # TP1: close TP1_SIZE_PCT of position, move SL to BE
                if not pos['tp1_hit'] and float_r >= TP1_R:
                    tp1_size = round(pos['size'] * TP1_SIZE_PCT, 4)
                    if tp1_size >= 0.001:
                        close_side = 'sell' if side == 'long' else 'buy'
                        place_order(symbol, close_side, tp1_size, reduce_only=True)
                        pos['remaining'] -= tp1_size
                        pos['tp1_hit'] = True
                        pos['sl'] = entry
                        tg(f'✂️ <b>TP1 {symbol} +{TP1_R}R</b>\nClosed {int(TP1_SIZE_PCT*100)}% at ${price:,.4f}\nSL → BE ${entry:,.4f}')

                # TP2: close TP2_SIZE_PCT of position
                if pos['tp1_hit'] and not pos['tp2_hit'] and float_r >= TP2_R:
                    tp2_size = round(pos['size'] * TP2_SIZE_PCT, 4)
                    if tp2_size >= 0.001:
                        close_side = 'sell' if side == 'long' else 'buy'
                        place_order(symbol, close_side, tp2_size, reduce_only=True)
                        pos['remaining'] -= tp2_size
                        pos['tp2_hit'] = True
                        tg(f'✂️ <b>TP2 {symbol} +{TP2_R}R</b>\nClosed {int(TP2_SIZE_PCT*100)}% at ${price:,.4f}\n{int((1-TP1_SIZE_PCT-TP2_SIZE_PCT)*100)}% trailing...')

                # Trailing Stop (after TP1)
                if pos['tp1_hit']:
                    trail_dist = atr * TRAIL_ATR_MULT
                    if side == 'long':
                        new_trail = price - trail_dist
                        if pos['trail_sl'] is None or new_trail > pos['trail_sl']:
                            pos['trail_sl'] = new_trail
                        if price <= pos['trail_sl']:
                            close_all(symbol, 'long')
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
                            close_all(symbol, 'short')
                            tg(f'📉 <b>TRAIL {symbol}</b>\n${price:,.4f}\nPnL:{pnl_pct:+.2f}%')
                            log_trade(symbol, price, 'Trailing Stop', pnl_pct > 0, pnl_pct)
                            record_result(symbol, pnl_pct > 0, pnl_pct)
                            reset_pos(symbol)

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
            close_all(symbol, side)
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

    # Guards
    session_name, _ = get_session()

    if daily['halted']:
        log_signal(symbol, action, 0, {}, 'halted', 'Daily loss limit', session_name)
        return JSONResponse({'status': 'halted', 'reason': 'Daily loss limit'})

    if is_session_blocked():
        log_signal(symbol, action, 0, {}, 'filtered', f'{session_name} — low liquidity', session_name)
        return JSONResponse({'status': 'filtered', 'reason': f'{session_name} — trading blocked (low liquidity)'})

    paused, pause_reason = is_paused(symbol)
    if paused:
        log_signal(symbol, action, 0, {}, 'paused', pause_reason, session_name)
        return JSONResponse({'status': 'paused', 'reason': pause_reason})

    if pos['active']:
        log_signal(symbol, action, 0, {}, 'skipped', f'{symbol} position already open', session_name)
        return JSONResponse({'status': 'skipped', 'reason': f'{symbol} position already open'})

    side = 'long' if action == 'buy' else 'short'
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
        tg(f'❌ Order failed {symbol}: {result}')
        return JSONResponse({'error': result}, status_code=500)

    # Update state
    now = datetime.now(timezone.utc)
    positions[symbol].update(
        active=True, side=side, entry=price, size=size, remaining=size,
        sl=sl_price, atr=atr, r=sl_dist,
        tp1_hit=False, tp2_hit=False, peak_pnl=0.0, trail_sl=None,
        entry_time=now, ref_price=price, ref_time=now,
        score=score, session=session_name,
    )

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
        'daily_loss': f'{loss_pct:.2f}%',
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
            'base_risk': '1%', 'high_vol_risk': '0.5%', 'streak_risk': '1.25%',
            'min_score': MIN_SCORE, 'sl': f'ATR×{SL_ATR_MULT}',
            'tp1': f'+{TP1_R}R ({int(TP1_SIZE_PCT*100)}%)', 'tp2': f'+{TP2_R}R ({int(TP2_SIZE_PCT*100)}%)',
            'trail': f'ATR×{TRAIL_ATR_MULT}', 'daily_stop': f'-{DAILY_LOSS_LIMIT*100}%',
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
    daily.update(date=str(date.today()), start_balance=None, halted=False,
                 trades=0, wins=0, losses=0, pnl=0.0)
    return {'status': 'daily stats reset', 'date': daily['date']}

@app.get('/')
async def home():
    return {'status': 'running', 'bot': 'CrossX Pro Bot v3.0', 'symbols': SYMBOLS}


# ════════════════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════════════════
@app.on_event('startup')
async def startup():
    _sync_time()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    print('[BOT] CrossX Pro Bot v3.0 started —', ', '.join(SYMBOLS))
    tg(
        '🚀 <b>CrossX Pro Bot v3.0 Online</b>\n'
        '━━━━━━━━━━━━━━━━━━━\n'
        f'Пары: {" | ".join(SYMBOLS)}\n'
        f'TF: 5m entry | 15m/1h/4h confirm\n'
        f'Risk: 1% base | 0.5% high-vol\n'
        f'SL: ATR×{SL_ATR_MULT} | TP1: +{TP1_R}R ({int(TP1_SIZE_PCT*100)}%) | TP2: +{TP2_R}R | Lev: {LEVERAGE}x\n'
        f'Filter: Score ≥75/100 | Daily stop: -10%'
    )


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8080))
    uvicorn.run(app, host='0.0.0.0', port=port)
