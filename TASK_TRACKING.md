# TASK_TRACKING.md

## Purpose
Трекер задач для долговременного управления развитием проекта и AI-координации. Отражает приоритеты, прогресс и статус работ.

## Structure
- `Last Updated`
- `Active Tasks` — текущие задачи в работе
- `Backlog` — очередь задач
- `Completed` — недавно завершённые задачи
- `Blocked` — задачи, требующие внешнего вмешательства

Каждая задача содержит:
- `ID`
- `Title`
- `Owner`
- `Priority`
- `Status`
- `Notes`

## Example Entry
```
Last Updated: 2026-05-09 18:20 UTC

Active Tasks:
- TASK-001 | Refactor Bitget API client | Claude Code | High | in-progress | split API calls and add retry logic
- TASK-002 | Add persistent trade storage | Data Agent | High | todo | SQLite or JSON storage

Backlog:
- TASK-003 | Improve webhook validation | Claude Code | Medium | todo
- TASK-004 | Add CI test suite | Operations Agent | Low | todo

Blocked:
- TASK-005 | Update Telegram notifications flow | Manual | Medium | blocked | needs new bot token

Completed:
- TASK-000 | Initial memory structure creation | Claude Code | High | done | foundation files created
```

## Usage Instructions
1. Добавляй новую задачу в `Backlog` и переводи её в `Active Tasks` при старте.
2. Обновляй статус задачи при любом прогрессе.
3. Используй `Blocked` для любых задач, требующих внешнего решения.
4. AI должен читать этот файл перед началом работы, чтобы понять приоритеты.
