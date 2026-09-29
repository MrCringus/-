# ААГ — Telegram-бот расписания

Подготовлено для локального запуска и деплоя на Render + Neon.

## Локально

1. Создай виртуальное окружение.
2. Установи зависимости:

```bash
pip install -r requirements.txt
```

3. Скопируй `.env.example` в `.env` и заполни `BOT_TOKEN`.
4. Если `DATABASE_URL` пустой, используется локальный SQLite.
5. Запуск:

```bash
python bot.py
```

При пустом `BASE_URL` бот работает через Telegram polling.

## Render + Neon

На Render задай:

- `BOT_TOKEN` — токен BotFather
- `DATABASE_URL` — connection string Neon
- `TIMEZONE=Asia/Barnaul`
- `WEBHOOK_PATH=/telegram/webhook`
- `WEBHOOK_SECRET` — случайная секретная строка
- `SYNC_INTERVAL_HOURS=6`

`RENDER_EXTERNAL_URL` Render предоставляет автоматически. Если он есть, бот запускается в webhook-режиме.

Health endpoint: `/health`.

## Важно

`.env`, локальная SQLite-база, виртуальное окружение и секреты не должны попадать в GitHub.
