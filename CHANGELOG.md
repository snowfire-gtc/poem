# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [Semantic Versioning](https://semver.org/lang/ru/).

## [Unreleased]

### Добавлено
- **Тонкий игровой клиент** (`poem/client.py`, guide_client.md §1/§5): два транспорта —
  `LibraryClient` (in-process через `WorldServer.handle`) и `HttpClient` (stdlib `urllib`,
  Bearer-токен, бинарная загрузка ассетов `/images/{id}/asset`). Полный набор операций:
  хроника/отчёт, дашборд (§16.1, 5 обязательных строк + предупреждение о смене интервала
  хода), цивилизации, законы (карточка, статусы, pending-корзины §15.3, снимок, cost_preview,
  history), черновики (CRUD + валидация), правки (submit/cancel), ходы (open/run), дипломатия,
  очередь иллюстраций с поллингом `wait_for_images`. Структурированные серверные ошибки
  сохраняются дословно (`ApiError`, ui-client.md §6); сетевые сбои — `status=0, is_transport`.
  Экспортируется из пакета `poem`. Тесты — `tests/test_client.py` (13 тестов, оба транспорта).
- **Аутентификация администратора сервера** (`poem/api.py`, §15 security addendum): все
  изменяющие маршруты (`POST`/`PUT`/`DELETE`/`PATCH`) требуют токен из `POEM_ADMIN_TOKEN`;
  сравнение за постоянное время (`hmac.compare_digest`); ответы `401`/`403`; отдельный
  токен GPU-воркера `POEM_IMAGE_WORKER_TOKEN`; публичные только `GET`. Генератор токенов
  `generate_admin_token()`. Тесты — `tests/test_admin_auth.py`. Документация —
  `guide_server.md` §4.4a, README «HTTP API».
- **Опциональная LLM-генерация игровых текстов** (`poem/llm.py`): литературные секции
  отчёта правителя (§13.22) могут переписываться LLM в стиле хроник мира. Выключено по
  умолчанию (`POEM_LLM_ENABLED=1` — включение); транспорты: OpenAI-совместимый HTTP,
  локальный CLI-мост; детерминированные промпты (без wall-clock), кэш по `sha256(prompt)`,
  гарантированный fallback на шаблоны при любом сбое. Зависимостей не добавлено (только
  stdlib). Тесты — `tests/test_llm_texts.py`.
- Файл `LICENSE` (MIT) и этот `CHANGELOG.md`.
- `.gitignore` для артефактов Python (`__pycache__/`, `*.pyc`, `*.log`); отслеживаемые
  `.pyc` удалены из индекса git.

### Исправлено
- `NameError: applied_set` в `run_turn` (`poem/turn.py`): переменная статуса применённых
  ревизий не объявлялась — любой ход с применёнными ревизиями падал (11 тестов).
- Сигнатура вспомогательного дубля `apply_once` в `tests/test_failed_revision_status.py`
  (`TypeError` из-за нового kwarg `turn_id_of_application`).
- «Слово о мире» терялось в отчёте правителя, если война завершилась мирным договором на
  текущем ходу (`poem/turn.py`, `poem/chronicle_report.py`): стороны завершённых войн
  собираются до цикла тиков; договор попадает в секцию дипломатии отчёта; шаблоны стихов
  поддерживают подстановку имени противника (`{enemy}`/`{civ}`).
- Синхронизация README с кодом: число модулей ядра, структура репозитория (16 файлов /
  172 теста), описание новых возможностей.

## [0.4.0]
- Ядро симуляции v0.4.0: хроника мира, динамические интервалы ходов, законы/черновики/
  ревизии, дипломатия и войны, lease-протокол генерации изображений на локальных GPU,
  view-модели и литературные отчёты правителя на шаблонах хроники.

[Unreleased]: https://github.com/snowfire-gtc/poem/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/snowfire-gtc/poem/releases/tag/v0.4.0
