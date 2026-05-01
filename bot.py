import os
import json
import time
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, jsonify
from datetime import date

app = Flask(__name__)

# ═══════════════════════════════════════════════
# НАСТРОЙКИ (из переменных окружения Railway)
# ═══════════════════════════════════════════════
API_KEY       = os.environ['BITGET_API_KEY']
API_SECRET    = os.environ['BITGET_API_SECRET']
PASSPHRASE    = os.environ['BITGET_PASSPHRASE']
WEBHOOK_TOKEN = os.environ.get('WEBHOOK_TOKEN', 'change_me')
TG_TOKEN      = os.environ.get('TG_TOKEN', '')
TG_CHAT_ID    = os.environ.get('TG_CHAT_ID', '')

BASE_URL = 'https://api.bitget.com'

# ═══════════════════════════════════════════════
# ПАРАМЕТРЫ ТОРГОВЛИ
# ═══════════════════════════════════════════════
RISK_PCT         = 0.05   # 5% риска на сделку
LEVERAGE         = 10     # плечо
SL_PCT           = 0.02   # стоп-лосс 2%
TP_PCT           = 0.04   # тейк-профит 4%
DAILY_LOSS_LIMIT = 0.10   # стоп дня: -10% депозита

# ═══════════════════════════════════════════════
# СОСТОЯНИЕ БОТА
# ═══════════════════════════════════════════════
daily = {
    'date':          '',
    'start_balance': None,
    'halted':        False,
    'trades':        0,
    'pnl':           0.0,
}

# Открытые позиции { 'BTCUSDT': 'long' | 'short' | None }
positions = {}


# ───────────────────────────────────────────────
# TELEGRAM
# ───────────────────────────────────────────────

def tg(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=5
        )
    except Exception:
        pass


# ───────────────────────────────────────────────
# BITGET API
# ───────────────────────────────────────────────

def _sign(ts: str, method: str, path: str, body: str = '') -> str:
    msg = ts + method.upper() + path + body
    mac = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def _headers(method: str, path: str, body: str = '') -> dict:
    ts = str(int(time.time() * 1000))
    return {
        'ACCESS-KEY':        API_KEY,
        'ACCESS-SIGN':       _sign(ts, method, path, body),
        'ACCESS-TIMESTAMP':  ts,
        'ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type':      'application/json',
        'locale':            'en-US',
    }

def api_get(path: str) -> dict:
    r = requests.get(BASE_URL + path, headers=_headers('GET', path), timeout=10)
    return r.json()

def api_post(path: str, body: dict) -> dict:
    b = json.dumps(body)
    r = requests.post(BASE_URL + path, headers=_headers('POST', path, b), data=b, timeout=10)
    return r.json()

def get_balance() -> float | None:
    path = '/api/v2/mix/account/account?productType=USDT-FUTURES&marginCoin=USDT'
    d = api_get(path)
    if d.get('code') == '00000':
        return float(d['data']['available'])
    print(f'[ERR] balance: {d}')
    return None

def get_price(symbol: str) -> float | None:
    path = f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES'
    d = api_get(path)
    if d.get('code') == '00000':
        return float(d['data'][0]['lastPr'])
    print(f'[ERR] price: {d}')
    return None

def set_leverage(symbol: str):
    for side in ('long', 'short'):
        api_post('/api/v2/mix/account/set-leverage', {
            'symbol': symbol, 'productType': 'USDT-FUTURES',
            'marginCoin': 'USDT', 'leverage': str(LEVERAGE), 'holdSide': side,
        })

def calc_size(balance: float, price: float) -> float:
    # Потери при SL = RISK_PCT * balance
    # size * price * SL_PCT = RISK_PCT * balance
    size = (balance * RISK_PCT) / (price * SL_PCT)
    return round(size, 4)

def place_order(symbol: str, side: str, size: float, sl: float, tp: float) -> dict:
    return api_post('/api/v2/mix/order/place-order', {
        'symbol':                symbol,
        'productType':           'USDT-FUTURES',
        'marginMode':            'isolated',
        'marginCoin':            'USDT',
        'size':                  str(size),
        'side':                  side,
        'orderType':             'market',
        'presetStopLossPrice':   str(round(sl, 2)),
        'presetTakeProfitPrice': str(round(tp, 2)),
    })

def close_position(symbol: str, hold_side: str) -> dict:
    return api_post('/api/v2/mix/order/close-positions', {
        'symbol': symbol, 'productType': 'USDT-FUTURES', 'holdSide': hold_side,
    })


# ───────────────────────────────────────────────
# ДНЕВНОЙ ЛИМИТ
# ───────────────────────────────────────────────

def reset_daily_if_needed():
    today = str(date.today())
    if daily['date'] != today:
        daily.update(date=today, start_balance=None, halted=False, trades=0, pnl=0.0)
        positions.clear()
        print(f'[DAY] Новый день: {today}')

def check_and_update_daily(balance: float) -> bool:
    if daily['start_balance'] is None:
        daily['start_balance'] = balance
        tg(f'🤖 <b>Бот запущен</b>\nБаланс: <b>${balance:.2f}</b>\nРиск/сделка: {RISK_PCT*100}% | SL: {SL_PCT*100}% | TP: {TP_PCT*100}%\nСтоп дня: -{DAILY_LOSS_LIMIT*100}%')
        return False

    loss_pct = (daily['start_balance'] - balance) / daily['start_balance']
    if loss_pct >= DAILY_LOSS_LIMIT:
        daily['halted'] = True
        msg = f'🛑 <b>СТОП ДНЯ</b>\nПотеряно: <b>{loss_pct*100:.1f}%</b>\nБаланс: ${balance:.2f}\nТорговля остановлена до завтра.'
        tg(msg)
        print(msg)
        return True
    return False


# ───────────────────────────────────────────────
# WEBHOOK — принимает сигналы от TradingView
# ───────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}

    if data.get('token') != WEBHOOK_TOKEN:
        return jsonify({'error': 'unauthorized'}), 401

    reset_daily_if_needed()

    if daily['halted']:
        return jsonify({'status': 'halted', 'reason': 'Daily loss limit reached'})

    action = data.get('action', '').lower()
    symbol = data.get('symbol', 'BTCUSDT')

    print(f'[SIGNAL] {action} {symbol}')

    balance = get_balance()
    if balance is None:
        return jsonify({'error': 'Cannot get balance'}), 500

    if check_and_update_daily(balance):
        return jsonify({'status': 'halted'})

    # ── LONG ──
    if action == 'buy':
        if positions.get(symbol) == 'long':
            return jsonify({'status': 'skipped', 'reason': 'Long already open'})

        # Если есть шорт — закрыть сначала
        if positions.get(symbol) == 'short':
            close_position(symbol, 'short')
            positions[symbol] = None
            tg(f'🔄 {symbol}: SHORT закрыт по сигналу BUY')

        price = get_price(symbol)
        if price is None:
            return jsonify({'error': 'Cannot get price'}), 500

        set_leverage(symbol)
        size = calc_size(balance, price)
        sl   = round(price * (1 - SL_PCT), 2)
        tp   = round(price * (1 + TP_PCT), 2)

        result = place_order(symbol, 'buy', size, sl, tp)

        if result.get('code') == '00000':
            positions[symbol] = 'long'
            daily['trades'] += 1
            msg = (f'🟢 <b>LONG открыт</b> — {symbol}\n'
                   f'Цена входа: <b>${price:,.2f}</b>\n'
                   f'Размер: {size} BTC (${size*price:,.0f})\n'
                   f'SL: ${sl:,.2f} (-{SL_PCT*100}%)\n'
                   f'TP: ${tp:,.2f} (+{TP_PCT*100}%)\n'
                   f'Баланс: ${balance:.2f} | Риск: ${balance*RISK_PCT:.2f}')
            tg(msg)
            print(msg)
            return jsonify({'status': 'long opened', 'price': price, 'sl': sl, 'tp': tp})
        else:
            tg(f'❌ Ошибка открытия LONG: {result}')
            return jsonify({'error': result}), 500

    # ── SHORT ──
    elif action == 'sell':
        if positions.get(symbol) == 'short':
            return jsonify({'status': 'skipped', 'reason': 'Short already open'})

        # Если есть лонг — закрыть сначала
        if positions.get(symbol) == 'long':
            close_position(symbol, 'long')
            positions[symbol] = None
            tg(f'🔄 {symbol}: LONG закрыт по сигналу SELL')

        price = get_price(symbol)
        if price is None:
            return jsonify({'error': 'Cannot get price'}), 500

        set_leverage(symbol)
        size = calc_size(balance, price)
        sl   = round(price * (1 + SL_PCT), 2)
        tp   = round(price * (1 - TP_PCT), 2)

        result = place_order(symbol, 'sell', size, sl, tp)

        if result.get('code') == '00000':
            positions[symbol] = 'short'
            daily['trades'] += 1
            msg = (f'🔴 <b>SHORT открыт</b> — {symbol}\n'
                   f'Цена входа: <b>${price:,.2f}</b>\n'
                   f'Размер: {size} BTC (${size*price:,.0f})\n'
                   f'SL: ${sl:,.2f} (+{SL_PCT*100}%)\n'
                   f'TP: ${tp:,.2f} (-{TP_PCT*100}%)\n'
                   f'Баланс: ${balance:.2f} | Риск: ${balance*RISK_PCT:.2f}')
            tg(msg)
            print(msg)
            return jsonify({'status': 'short opened', 'price': price, 'sl': sl, 'tp': tp})
        else:
            tg(f'❌ Ошибка открытия SHORT: {result}')
            return jsonify({'error': result}), 500

    # ── ЗАКРЫТЬ ЛОНГ (TR сигнал) ──
    elif action == 'close_long':
        if positions.get(symbol) != 'long':
            return jsonify({'status': 'skipped', 'reason': 'No long position'})
        result = close_position(symbol, 'long')
        positions[symbol] = None
        tg(f'⚪ <b>LONG закрыт</b> — {symbol} (TR сигнал)')
        return jsonify({'status': 'long closed'})

    # ── ЗАКРЫТЬ ШОРТ (BR сигнал) ──
    elif action == 'close_short':
        if positions.get(symbol) != 'short':
            return jsonify({'status': 'skipped', 'reason': 'No short position'})
        result = close_position(symbol, 'short')
        positions[symbol] = None
        tg(f'⚪ <b>SHORT закрыт</b> — {symbol} (BR сигнал)')
        return jsonify({'status': 'short closed'})

    return jsonify({'error': f'Unknown action: {action}'}), 400


# ───────────────────────────────────────────────
# СТАТУС
# ───────────────────────────────────────────────

@app.route('/status', methods=['GET'])
def status():
    reset_daily_if_needed()
    balance = get_balance()
    loss_pct = 0.0
    if daily['start_balance'] and balance:
        loss_pct = (daily['start_balance'] - balance) / daily['start_balance'] * 100
    return jsonify({
        'bot':           'CrossX Pro Bot v1.0',
        'balance':       f'${balance:.2f}' if balance else 'error',
        'start_balance': f'${daily["start_balance"]:.2f}' if daily['start_balance'] else 'not set',
        'daily_loss':    f'{loss_pct:.2f}%',
        'halted':        daily['halted'],
        'trades_today':  daily['trades'],
        'open_positions': positions,
        'date':          daily['date'],
        'settings': {
            'risk_per_trade':  f'{RISK_PCT*100}%',
            'leverage':        f'{LEVERAGE}x',
            'stop_loss':       f'{SL_PCT*100}%',
            'take_profit':     f'{TP_PCT*100}%',
            'daily_stop':      f'-{DAILY_LOSS_LIMIT*100}%',
        },
    })

@app.route('/', methods=['GET'])
def home():
    return jsonify({'status': 'running', 'bot': 'CrossX Pro Bot'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'[BOT] CrossX Pro Bot запущен на порту {port}')
    app.run(host='0.0.0.0', port=port)
