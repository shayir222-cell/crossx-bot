# SYSTEM_STATE.md

## Purpose
Хранит текущее глобальное состояние проекта, статус бота и важные runtime-параметры. Это главный документ для быстрого восстановления текущей operational картины.

## Structure
- `Last Updated` — дата и время последнего обновления
- `Bot Status` — active / halted / maintenance
- `Current Session` — набор активных торговых сессий и время
- `Symbols` — торгуемые инструменты и статус
- `Risk State` — дневной стоп, equity, потеря дня
- `Pending Alerts` — текущие предупреждения или блокировки
- `Last Sync` — когда последний раз обновлялся контекст

## Example Entry
```
Last Updated: 2026-05-09 18:20 UTC

Bot Status: active
Current Session: London/NY Overlap

Symbols:
- BTCUSDT: active
- ETHUSDT: paused
- BNBUSDT: active

Risk State:
- Daily Loss Limit: 10%
- Current Equity: $12,542.80
- Daily PnL: -0.8%
- Halted: false

Pending Alerts:
- news block expected at 20:00 UTC
- signal pause for SOLUSDT until 2026-05-09 19:35 UTC

Last Sync: 2026-05-09 18:15 UTC
```

## Usage Instructions
1. Обновляй этот файл при каждом значимом изменении runtime-статуса.
2. Перед началом новой AI-сессии загружай `SYSTEM_STATE.md` первым.
3. Используй его как «карточку состояния» для быстрой диагностики.
4. В автоматике можно генерировать этот файл через `analytics.py` или bot endpoint.
