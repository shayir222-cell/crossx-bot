# SESSION_CONTEXT.md

## Purpose
Сохраняет последние данные сессии, чтобы AI мог восстановить контекст работы бота между встречами. Содержит краткое состояние, последние события и текущие настройки.

## Structure
- `Last Updated`
- `Current Context` — краткий обзор текущей рабочей сессии
- `Active Positions` — список открытых позиций и статусы
- `Recent Signals` — последние принятые или отфильтрованные сигналы
- `Recent Trades` — последние сделки с ключевыми результатами
- `Bot Settings` — параметры score, risk, ATR и т.д.

## Example Entry
```
Last Updated: 2026-05-09 18:20 UTC

Current Context:
- Bot running normally
- Weekly report scheduled for Sunday 20:00 UTC
- News filter active for US CPI

Active Positions:
- BTCUSDT long @ 68,450, SL 67,200, TP1 69,950
- ETHUSDT none

Recent Signals:
- BTCUSDT buy | score 87 | taken
- SOLUSDT sell | score 74 | filtered (low ATR)

Recent Trades:
- 2026-05-09 16:40 | BTCUSDT long | TP1 | +2.5R
- 2026-05-09 15:10 | XRPUSDT short | SL | -1.1R

Bot Settings:
- Min Score: 80
- Leverage: 7x
- SL: ATR×2.0
- TP1: +2.5R, TP2: +5.0R
```

## Usage Instructions
1. Обновляй файл после каждого анализа и после запуска бота.
2. При возобновлении работы AI загружай `SESSION_CONTEXT.md` сразу после `SYSTEM_STATE.md`.
3. Используй его для сохранения оперативной картины, чтобы не терять состояние между сессиями.
4. Дополняй только актуальными данными; устаревшие записи удаляй или помечай.
