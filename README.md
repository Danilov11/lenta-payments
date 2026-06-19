# Лента — Личный кабинет сотрудника

Сервис самообслуживания: сотрудник входит по номеру телефона и видит свои смены,
выплаты, авансы, штрафы и статус по акции «Приведи друга».

- **Сайт:** https://danilov11.github.io/lenta-payments/
- **Backend:** FastAPI на Railway · **База:** PostgreSQL 16 · **Frontend:** GitHub Pages

## Документация

- 📘 [DOCUMENTATION.md](DOCUMENTATION.md) — полное техническое описание (архитектура,
  БД, API, ETL, интеграции, деплой).
- 🤝 [HANDOVER.md](HANDOVER.md) — инструкция по передаче проекта (доступы,
  безопасность, чек-лист).

## Быстрый старт (локально)

```bash
pip install -r requirements.txt
cp .env.example .env        # заполнить DATABASE_URL и секреты
uvicorn main:app --reload --port 8000
# http://127.0.0.1:8000  ·  Swagger: /docs
```

## Структура

```
main.py            # FastAPI: эндпоинты + init_db()
auth.py            # JWT
database.py        # подключение к PostgreSQL
bot.py             # Telegram-бот (привязка номера)
etl_*.py           # загрузка смен / авансов / рефералов
static/            # frontend (index.html, admin.html)
.github/workflows/ # деплой Pages + ежедневный ETL
```
