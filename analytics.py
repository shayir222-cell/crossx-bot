"""
CrossX Analytics — автоматически обновляет Obsidian vault данными бота.
Запуск:  python analytics.py
Авто:    Task Scheduler каждый день в 01:00
"""
import os, json, hmac, hashlib, base64, time, requests
from datetime import datetime, timezone, timedelta

BOT_URL  = "https://crossx-bot.onrender.com"
VAULT    = r"C:\Users\Дастан\Documents\CrossX-Trading-Vault"

API_KEY     = "bg_44ca2b953e3465988c37dffe12433b0e"
API_SECRET  = "e668c323999c2f0b546a657b83ce9cfb33c22ed7d4be434945e4262800d3dae2"
PASSPHRASE  = "Dastan108112"
BASE_BG     = "https://api.bitget.com"

# ──────────────────────────────────────────────────────────────
# Bitget helpers
# ──────────────────────────────────────────────────────────────
def _bg_ts():
    r = requests.get(f"{BASE_BG}/api/v2/public/time", timeout=5)
    return str(r.json()["data"]["serverTime"])

def _bg_sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def _bg_headers(method, path, body=""):
    ts = _bg_ts()
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": _bg_sign(ts, method, path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

def bitget_get(path):
    try:
        r = requests.get(BASE_BG + path, headers=_bg_headers("GET", path), timeout=10)
        return r.json()
    except Exception as e:
        print(f"[Bitget error] {e}")
        return {}


# ──────────────────────────────────────────────────────────────
# Bot API
# ──────────────────────────────────────────────────────────────
def bot_get(endpoint):
    try:
        r = requests.get(f"{BOT_URL}/{endpoint}", timeout=20)
        return r.json()
    except Exception as e:
        print(f"[Bot {endpoint} error] {e}")
        return {}


# ──────────────────────────────────────────────────────────────
# Bitget position history (fallback for after bot restarts)
# ──────────────────────────────────────────────────────────────
def fetch_bitget_history():
    d = bitget_get("/api/v2/mix/position/history-position?productType=USDT-FUTURES&symbol=BTCUSDT&limit=100")
    raw = d.get("data", {})
    if isinstance(raw, dict):
        raw = raw.get("list", [])
    elif not isinstance(raw, list):
        raw = []

    trades = []
    for p in raw:
        try:
            entry   = float(p.get("openAvgPrice", 0) or 0)
            exit_   = float(p.get("closeAvgPrice", 0) or 0)
            side    = "long" if p.get("holdSide", "") == "long" else "short"
            pnl     = float(p.get("netProfit", 0) or 0)
            size    = float(p.get("openTotalPos", 0) or 0)
            ts_ms   = int(p.get("utime", 0) or 0)
            dt      = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)
            pnl_pct = ((exit_ - entry) / entry * 100) if side == "long" and entry else \
                      ((entry - exit_) / entry * 100) if entry else 0
            trades.append({
                "id": f"bg_{ts_ms}",
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M"),
                "side": side,
                "entry": entry,
                "exit": exit_,
                "size": size,
                "score": 0,
                "session": "",
                "atr": 0,
                "sl": 0,
                "tp1_hit": False,
                "tp2_hit": False,
                "peak_pnl": 0,
                "pnl_pct": round(pnl_pct, 3),
                "won": pnl > 0,
                "exit_reason": "Closed",
                "duration_min": 0,
                "source": "bitget",
            })
        except Exception:
            continue
    return trades


# ──────────────────────────────────────────────────────────────
# Merge bot trades + bitget history (deduplicate by date+side+entry)
# ──────────────────────────────────────────────────────────────
def merge_trades(bot_trades, bg_trades):
    seen = set()
    result = []
    for t in bot_trades:
        key = (t["date"], t["side"], round(t["entry"], 0))
        seen.add(key)
        result.append(t)
    for t in bg_trades:
        key = (t["date"], t["side"], round(t["entry"], 0))
        if key not in seen:
            result.append(t)
    result.sort(key=lambda x: (x["date"], x["time"]))
    return result


# ──────────────────────────────────────────────────────────────
# Analytics
# ──────────────────────────────────────────────────────────────
def analyze(trades):
    if not trades:
        return None
    total = len(trades)
    wins  = sum(1 for t in trades if t["won"])
    pnls  = [t["pnl_pct"] for t in trades]
    total_pnl = sum(pnls)

    by_score = {}
    for t in trades:
        s = t.get("score", 0)
        b = "95+" if s >= 95 else ("90-94" if s >= 90 else ("85-89" if s >= 85 else ("80-84" if s >= 80 else "75-79")))
        if b not in by_score:
            by_score[b] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_score[b]["trades"] += 1
        if t["won"]:
            by_score[b]["wins"] += 1
        by_score[b]["pnl"] = round(by_score[b]["pnl"] + t["pnl_pct"], 3)

    by_session = {}
    for t in trades:
        k = t.get("session") or "Unknown"
        if k not in by_session:
            by_session[k] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_session[k]["trades"] += 1
        if t["won"]:
            by_session[k]["wins"] += 1
        by_session[k]["pnl"] = round(by_session[k]["pnl"] + t["pnl_pct"], 3)

    by_reason = {}
    for t in trades:
        r = t.get("exit_reason", "Unknown")
        if r not in by_reason:
            by_reason[r] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_reason[r]["trades"] += 1
        if t["won"]:
            by_reason[r]["wins"] += 1
        by_reason[r]["pnl"] = round(by_reason[r]["pnl"] + t["pnl_pct"], 3)

    best  = max(trades, key=lambda t: t["pnl_pct"])
    worst = min(trades, key=lambda t: t["pnl_pct"])

    return {
        "total": total, "wins": wins, "losses": total - wins,
        "win_rate": round(wins / total * 100, 1),
        "total_pnl": round(total_pnl, 3),
        "avg_pnl": round(total_pnl / total, 3),
        "best": best, "worst": worst,
        "by_score": by_score,
        "by_session": by_session,
        "by_reason": by_reason,
    }


# ──────────────────────────────────────────────────────────────
# Suggestions engine
# ──────────────────────────────────────────────────────────────
def generate_suggestions(a, trades):
    sugg = []
    if not a or a["total"] < 5:
        return ["Нужно минимум 5 сделок для автоматического анализа. Продолжай торговать!"]

    wr = a["win_rate"]

    # Score thresholds
    for bucket, d in a["by_score"].items():
        if d["trades"] >= 3:
            bwr = d["wins"] / d["trades"] * 100
            if bucket == "75-79" and bwr < 40:
                sugg.append(
                    f"SCORE: Score 75-79 даёт win rate {bwr:.0f}% — поднять MIN_SCORE с 75 до 80. "
                    f"Это отфильтрует слабые сигналы и улучшит точность."
                )
            if bucket in ("90-94", "95+") and bwr > 65 and d["trades"] >= 3:
                sugg.append(
                    f"SCORE: Score {bucket} даёт win rate {bwr:.0f}% — при таких сигналах "
                    f"можно поднять риск до 1.5% (сейчас STREAK_RISK=1.25%)."
                )

    # Sessions
    for session, d in a["by_session"].items():
        if d["trades"] >= 3:
            swr = d["wins"] / d["trades"] * 100
            avg_p = d["pnl"] / d["trades"]
            if "Asian" in session and swr < 45:
                sugg.append(
                    f"СЕССИЯ: Азиатская сессия win rate {swr:.0f}% — "
                    f"снизить размер позиции с 60% до 30% или полностью отключить."
                )
            if ("London" in session or "New York" in session) and swr > 60:
                sugg.append(
                    f"СЕССИЯ: {session} win rate {swr:.0f}%, avg PnL {avg_p:+.3f}% — "
                    f"лучшее время. Можно увеличить базовый риск до 1.25% в эту сессию."
                )

    # Exit reasons
    total = a["total"]
    for reason, d in a["by_reason"].items():
        pct = d["trades"] / total * 100
        avg_p = d["pnl"] / d["trades"] if d["trades"] else 0
        if reason == "SL" and pct > 45:
            sugg.append(
                f"SL: {pct:.0f}% сделок закрываются по Stop Loss — "
                f"ATR×1.5 может быть слишком тесным для текущей волатильности. "
                f"Попробовать ATR×2.0."
            )
        if reason == "Time Stop" and d["trades"] >= 3:
            sugg.append(
                f"TIME STOP: {d['trades']} сделок закрылись по тайм-стопу (30 мин без движения) — "
                f"рынок часто в боковике при входах. Добавить фильтр ADX > 20."
            )
        if reason == "Trailing Stop" and d["trades"] >= 3 and avg_p > 1.0:
            sugg.append(
                f"TRAILING: {d['trades']} сделок закрылись по трейлингу со средним PnL {avg_p:+.2f}% — "
                f"трейлинг работает хорошо. ATR×1.0 оставить."
            )

    # Overall win rate
    if wr < 45:
        sugg.append(
            f"ОБЩЕЕ: Win rate {wr}% ниже 45% — "
            f"рекомендую поднять MIN_SCORE до 80 и добавить D1 EMA фильтр."
        )
    elif wr >= 60:
        sugg.append(
            f"ОБЩЕЕ: Win rate {wr}% — отличный результат! "
            f"Можно аккуратно поднять базовый риск с 1% до 1.25%."
        )

    # Avg trade duration
    durations = [t.get("duration_min", 0) for t in trades if t.get("duration_min", 0) > 0]
    if durations:
        avg_dur = sum(durations) / len(durations)
        if avg_dur < 10:
            sugg.append(
                f"ДЛИТЕЛЬНОСТЬ: Средняя длительность сделки {avg_dur:.0f} мин — "
                f"сделки очень короткие. Возможно SL слишком тесный."
            )

    if not sugg:
        sugg.append(
            f"Все показатели в норме. Win rate {wr}%, {total} сделок. "
            f"Продолжай накапливать статистику (цель: 30+ сделок для надёжных выводов)."
        )
    return sugg


# ──────────────────────────────────────────────────────────────
# Obsidian file writers
# ──────────────────────────────────────────────────────────────
def write_file(rel_path, content):
    full = os.path.join(VAULT, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  [OK] {rel_path}")


def append_file(rel_path, content):
    full = os.path.join(VAULT, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "a", encoding="utf-8") as f:
        f.write(content)
    print(f"  [+]  {rel_path}")


def write_trade_file(trade):
    result  = "WIN" if trade["won"] else "LOSS"
    side    = trade["side"].upper()
    reason  = trade["exit_reason"].replace(" ", "-")
    fname   = f"Trades/{trade['date']}_{side}_{reason}_{trade['time'].replace(':', '')}.md"

    score_line = f"{trade['score']}/100" if trade.get("score") else "— (нет данных)"
    session_line = trade.get("session") or "—"
    atr_line = f"${trade['atr']:.2f}" if trade.get("atr") else "—"
    sl_line  = f"${trade['sl']:,.2f}" if trade.get("sl") else "—"
    dur_line = f"{trade.get('duration_min', '?')} мин"
    tp1_line = "Да" if trade.get("tp1_hit") else "Нет"
    tp2_line = "Да" if trade.get("tp2_hit") else "Нет"
    peak_line = f"{trade.get('peak_pnl', 0):+.3f}%" if trade.get("peak_pnl") else "—"

    content = f"""# {trade['date']} {trade['time']} — {side} — {result}

## Параметры входа
| | |
|---|---|
| Направление | {side} |
| Цена входа | ${trade['entry']:,.2f} |
| Score бота | {score_line} |
| Сессия | {session_line} |
| ATR | {atr_line} |
| Стоп-лосс | {sl_line} |
| Размер | {trade['size']} BTC |

## Результат
| | |
|---|---|
| Цена выхода | ${trade['exit']:,.2f} |
| Причина выхода | {trade['exit_reason']} |
| PnL | {trade['pnl_pct']:+.3f}% |
| Длительность | {dur_line} |
| Макс. прибыль | {peak_line} |
| TP1 достигнут | {tp1_line} |
| TP2 достигнут | {tp2_line} |

## Анализ

_Заполни вручную если хочешь добавить наблюдения_

## Теги
#trade #btcusdt #{side.lower()} #{'win' if trade['won'] else 'loss'} #{trade['exit_reason'].lower().replace(' ', '-')}
"""
    write_file(fname, content)


def write_signal_quality(a):
    if not a:
        return

    score_rows = ""
    for b in ["75-79", "80-84", "85-89", "90-94", "95+"]:
        d = a["by_score"].get(b, {"trades": 0, "wins": 0, "pnl": 0.0})
        wr = f"{d['wins']/d['trades']*100:.0f}%" if d["trades"] else "—"
        avg_p = f"{d['pnl']/d['trades']:+.3f}%" if d["trades"] else "—"
        score_rows += f"| {b} | {d['trades']} | {d['wins']} | {wr} | {avg_p} |\n"

    session_rows = ""
    for sess, d in a["by_session"].items():
        wr = f"{d['wins']/d['trades']*100:.0f}%" if d["trades"] else "—"
        avg_p = f"{d['pnl']/d['trades']:+.3f}%" if d["trades"] else "—"
        session_rows += f"| {sess} | {d['trades']} | {wr} | {avg_p} |\n"

    reason_rows = ""
    for reason, d in a["by_reason"].items():
        wr = f"{d['wins']/d['trades']*100:.0f}%" if d["trades"] else "—"
        pct = f"{d['trades']/a['total']*100:.0f}%"
        reason_rows += f"| {reason} | {d['trades']} ({pct}) | {wr} |\n"

    content = f"""# Аналитика сигналов — обновлено {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC

> Всего сделок в базе: **{a['total']}** | Win Rate: **{a['win_rate']}%** | Total PnL: **{a['total_pnl']:+.3f}%**

## Результаты по Score

| Score | Сделок | Побед | Win% | Avg PnL |
|---|---|---|---|---|
{score_rows}
> Компоненты Score: Macro Trend (25) + Momentum (20) + Structure (20) + Volume (15) + Current TF (10) + OB/OS (10)

## Результаты по сессии

| Сессия | Сделок | Win% | Avg PnL |
|---|---|---|---|
{session_rows}

## Причины закрытия

| Причина | Сделок | Win% |
|---|---|---|
{reason_rows}

## Лучшая / Худшая сделка

- **Лучшая:** {a['best']['date']} {a['best']['side'].upper()} {a['best']['pnl_pct']:+.3f}% ({a['best']['exit_reason']})
- **Худшая:** {a['worst']['date']} {a['worst']['side'].upper()} {a['worst']['pnl_pct']:+.3f}% ({a['worst']['exit_reason']})
"""
    write_file("Analytics/Signal-Quality.md", content)


def write_suggestions(sugg, a, today):
    total = a["total"] if a else 0
    wr    = a["win_rate"] if a else 0

    lines = "\n".join(f"- {s}" for s in sugg)
    content = f"""# Авто-предложения по улучшению — {today}

> Сгенерировано на основе **{total} сделок** | Win Rate: **{wr}%**

## Рекомендации

{lines}

---
*Следующее обновление — завтра в 01:00 UTC*
"""
    write_file(f"Bot-Ideas/Auto-Suggestions-{today}.md", content)


def update_dashboard(status, a, today):
    balance   = status.get("balance", "—")
    daily_pnl = status.get("daily_pnl", "—")
    session   = status.get("session", "—")
    trades_t  = status.get("trades", 0)
    wins_t    = status.get("wins", 0)
    losses_t  = status.get("losses", 0)
    halted    = status.get("halted", False)
    paused    = status.get("paused", False)

    pos_info = ""
    if status.get("position"):
        p = status["position"]
        pos_info = f"""
### Открытая позиция
| | |
|---|---|
| Направление | {p.get('side','').upper()} |
| Цена входа | ${p.get('entry',0):,.2f} |
| Текущая цена | ${p.get('current_price',0):,.2f} |
| Float R | {p.get('float_r',0):+.2f}R |
| SL | ${p.get('sl',0):,.2f} |
| TP1 | {'✅' if p.get('tp1_hit') else '⏳'} |
| TP2 | {'✅' if p.get('tp2_hit') else '⏳'} |
"""

    total_row = ""
    if a:
        total_row = f"| Всего сделок | **{a['total']}** | — | — | **{a['total_pnl']:+.3f}%** |"

    content = f"""# CrossX Pro — Dashboard
> Обновлено: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC | [Статус бота](https://crossx-bot.onrender.com/status)

## Статус бота {'🛑 HALTED' if halted else ('⏸ PAUSED' if paused else '🟢 ACTIVE')}

| Показатель | Значение |
|---|---|
| Баланс | **{balance}** |
| PnL сегодня | {daily_pnl} |
| Сессия | {session} |
| Сделок сегодня | {trades_t} |
| Побед / Потерь | {wins_t} / {losses_t} |
{pos_info}
## Общая статистика (все сделки)

| Период | Сделок | Побед | Win% | PnL |
|---|---|---|---|---|
| Сегодня | {trades_t} | {wins_t} | {'—' if not trades_t else f'{wins_t/trades_t*100:.0f}%'} | {daily_pnl} |
{total_row}

## Навигация

- [[Analytics/Signal-Quality|Аналитика сигналов]]
- [[Analytics/Weekly-Stats|Еженедельная статистика]]
- [[Bot-Ideas/Auto-Suggestions-{today}|Предложения на {today}]]
- [[Observations/Bot-Performance-Log|Лог работы бота]]
- [[Trades/README|Журнал сделок]]

## Настройки бота

| Параметр | Значение |
|---|---|
| Symbol | BTCUSDT Perpetual |
| Timeframe | 5m entry / 15m-1h-4h confirm |
| Min Score | 75/100 |
| Leverage | 10x isolated |
| Base Risk | 1% |
| SL | ATR × 1.5 |
| TP1 | +1.5R → 30% |
| TP2 | +3.0R → 30% |
| Trailing | ATR × 1.0 |
| Daily Stop | -10% |
"""
    write_file("Dashboard.md", content)


def append_perf_log(status, a, today):
    balance   = status.get("balance", "—")
    daily_pnl = status.get("daily_pnl", "—")
    trades_t  = status.get("trades", 0)
    wins_t    = status.get("wins", 0)
    session   = status.get("session", "—")
    total_str = f"Всего в базе: {a['total']} сделок, win rate {a['win_rate']}%" if a else "—"

    entry = f"""
### {today} — {datetime.now(timezone.utc).strftime('%H:%M')} UTC
**Баланс:** {balance} | **PnL:** {daily_pnl}
**Сделок сегодня:** {trades_t} (побед: {wins_t})
**Сессия:** {session}
**{total_str}**
"""
    append_file("Observations/Bot-Performance-Log.md", entry)


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
def run():
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    print(f"\n{'='*55}")
    print(f"  CrossX Analytics — {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*55}")

    # 1. Fetch data
    print("\n[1] Получаю данные...")
    status     = bot_get("status")
    trades_raw = bot_get("trades")
    bot_trades = trades_raw.get("trades", []) if trades_raw else []
    print(f"    Бот: баланс={status.get('balance','?')}, сделок бота={len(bot_trades)}")

    print("    Bitget история...")
    bg_trades = fetch_bitget_history()
    print(f"    Bitget: {len(bg_trades)} позиций")

    # 2. Merge
    all_trades = merge_trades(bot_trades, bg_trades)
    print(f"    Итого уникальных сделок: {len(all_trades)}")

    # 3. Analytics
    print("\n[2] Считаю аналитику...")
    a = analyze(all_trades)
    if a:
        print(f"    Win Rate: {a['win_rate']}% | PnL: {a['total_pnl']:+.3f}%")

    # 4. Suggestions
    sugg = generate_suggestions(a, all_trades)
    print(f"    Предложений: {len(sugg)}")

    # 5. Write Obsidian
    print("\n[3] Обновляю Obsidian vault...")

    # Trade files (only new ones — skip if file already exists)
    new_count = 0
    for trade in all_trades:
        reason = trade["exit_reason"].replace(" ", "-")
        fname  = f"Trades/{trade['date']}_{trade['side'].upper()}_{reason}_{trade['time'].replace(':', '')}.md"
        fpath  = os.path.join(VAULT, fname)
        if not os.path.exists(fpath):
            write_trade_file(trade)
            new_count += 1
    print(f"    Новых файлов сделок: {new_count}")

    write_signal_quality(a)
    write_suggestions(sugg, a, today)
    update_dashboard(status, a, today)
    append_perf_log(status, a, today)

    print(f"\n[4] Предложения по улучшению бота:")
    for s in sugg:
        print(f"    • {s}")

    print(f"\n{'='*55}")
    print(f"  Готово. Vault: {VAULT}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    run()
