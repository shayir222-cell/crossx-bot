# DECISION_LOG.md

## Purpose
Фиксирует ключевые архитектурные и торговые решения. Позволяет отслеживать, почему было принято то или иное изменение.

## Structure
- `Date`
- `Decision`
- `Context`
- `Options Considered`
- `Chosen Option`
- `Rationale`
- `Follow-up`

## Example Entry
```
Date: 2026-05-09
Decision: Перевести webhook validation на token + strict payload checks
Context: Сейчас webhook принимает JSON без строгой валидации; это риск безопасности.
Options Considered:
- Оставить как есть
- Добавить HMAC-подпись
- Добавить структуру JSONSchema
Chosen Option: Добавить проверку token + базовую JSON-структуру
Rationale: Минимально необходимое изменение с быстрой реализацией.
Follow-up: TASK-003 | Improve webhook validation
```

## Usage Instructions
1. Добавляй запись каждый раз, когда принимается архитектурное решение.
2. Сохраняй контекст и альтернативы.
3. Используй файл как журнал для последующего анализа.
4. При подготовке новой AI-сессии читай последние записи, чтобы понять текущую политику проекта.
