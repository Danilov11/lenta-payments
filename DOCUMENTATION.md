# Лента — Личный кабинет сотрудника. Документация

Сервис самообслуживания для сотрудников Ленты (офлайн). Сотрудник входит по номеру
телефона и видит свои смены, выплаты, авансы, штрафы и статус по акции «Приведи друга».

---

## 1. Обзор архитектуры

```
┌────────────────────────┐        ┌──────────────────────────┐
│  Frontend (статика)    │        │  Backend (FastAPI)       │
│  GitHub Pages          │  HTTPS │  Railway                 │
│  danilov11.github.io   │ ─────► │  *.up.railway.app        │
│  static/index.html     │        │  main.py                 │
│  static/admin.html     │        └──────────┬───────────────┘
└────────────────────────┘                   │
                                              │ psycopg2
                                   ┌──────────▼───────────────┐
                                   │  PostgreSQL 16 (Railway) │
                                   └──────────▲───────────────┘
                                              │
         ┌────────────────────────────────────┼───────────────────────┐
         │ ETL (загрузка данных)               │                       │
         │  • etl_advances.py    (авансы)      │  • Apps Script        │
         │  • etl_shifts.py      (смены)       │    (авансы из Google  │
         │  • etl_referrals.py   (рефералы)    │     Sheets → /admin)  │
         │  GitHub Actions cron (etl_cron.yml) │                       │
         └─────────────────────────────────────┴───────────────────────┘

Интеграции: Bitrix24 (онлайн-чат «Написать менеджеру»), Telegram-бот (привязка номера).
```

| Слой | Технология | Где работает |
|------|-----------|--------------|
| Backend | Python 3.11, FastAPI, uvicorn | Railway |
| База | PostgreSQL 16 | Railway |
| Frontend | Vanilla JS (SPA), без сборки | GitHub Pages |
| Аутентификация | JWT (HS256), вход по телефону | — |
| CI/CD | GitHub Actions | GitHub |
| Чат с менеджером | Bitrix24 Open Channel | внешний |
| Telegram | python-telegram-bot | отдельный процесс |

**Репозиторий:** https://github.com/Danilov11/lenta-payments

---

## 2. Структура проекта

```
lenta_api/
├── main.py              # FastAPI-приложение: все эндпоинты + init_db()
├── auth.py              # JWT: создание/проверка токена
├── database.py          # Подключение к PostgreSQL (DSN, get_db, fetchone/fetchall)
├── bot.py               # Telegram-бот (привязка телефона ↔ chat_id)
├── etl_advances.py      # ETL: авансы из Авансы.xlsx → таблица advances
├── etl_shifts.py        # ETL: смены из Google Sheets → таблица shifts
├── etl_referrals.py     # ETL: «Приведи друга» из Google Sheets → referrals
├── requirements.txt     # Python-зависимости backend
├── Procfile             # Команда запуска для Railway/Heroku
├── railway.toml         # Конфиг деплоя Railway (nixpacks, healthcheck)
├── .env                 # Секреты (НЕ в git)
├── .env.example         # Шаблон секретов
├── .github/workflows/
│   ├── pages.yml        # Деплой static/ на GitHub Pages при push в main
│   └── etl_cron.yml     # Ежедневный ETL (смены + рефералы) в 02:00 UTC
└── static/
    ├── index.html       # Личный кабинет сотрудника (SPA, i18n ru/tg/uz/ky)
    ├── admin.html       # Панель менеджера (поиск, авансы)
    └── .nojekyll        # Отключает Jekyll на GitHub Pages
```

---

## 3. База данных

Схема создаётся автоматически при старте backend — `init_db()` в `main.py`
(идемпотентно: `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).

### Таблицы

| Таблица | Назначение | Ключевые поля |
|---------|-----------|---------------|
| `employees` | Сотрудники | `employee_id`, `phone` (UNIQUE — логин), `full_name`, `inn`, `citizenship`, `employee_type`, `payment_frequency`, `is_brigadier`, `brigadier_bonus`, `notifications_seen_at` |
| `shifts` | Смены и выплаты | `shift_date`, `employee_id`, `store_id`, `service_id`, `hours_paid`, `rate`, `lunch_compensation`, `total_payment`, `status_customer`, `status_contractor`, `shift_type` |
| `advances` | Авансы и штрафы | `employee_id`, `advance_date`, `amount`, `balance`, `project`, `note`, `manager_note` |
| `referrals` | Акция «Приведи друга» | `referrer_employee_id`, `referred_name`, `referred_phone`, `amount`, `status`, `period` |
| `stores` | Магазины (ТК) | `store_id` (= номер ТК), `store_name`, `city_id`, `format` |
| `services` | Услуги/специальности | `service_id`, `service_name`, `service_level` (1–5) |
| `cities` | Города | `city_id`, `city_name`, `division` |
| `tariffs` | Тарифы (ставки) | `city_id`, `store_format`, `service_id`, `rate`, `effective_date` |
| `suppliers` | Подрядчики | `supplier_id`, `supplier_name` |
| `telegram_links` | Привязка Telegram | `phone`, `chat_id`, `username` |

### Важные соглашения

- **Логин = номер телефона.** Формат хранения `7XXXXXXXXXX` (11 цифр, нормализуется
  из `8…` и `+7…`). Уникален в `employees.phone`.
- **`stores.store_id` — это номер ТК** (бизнес-ключ), а НЕ автоинкремент.
- **Штраф** — это запись в `advances`, где `note` или `manager_note` содержит «штраф».
  Отдельной таблицы штрафов нет.
- **`shifts.rate`** — ставка из ежедневной выгрузки (за июнь заполнена напрямую).
  Для старых смен ставка берётся из `tariffs` через `tariff_id`. Эндпоинт `/me/shifts`
  отдаёт `COALESCE(s.rate, t.rate)`.
- **Уровень услуги** (`service_level`) — берётся из префикса названия `"Nур_…"`
  (например `2ур_Услуга уборки…` → уровень 2).

---

## 4. Backend API

Базовый URL — адрес Railway-сервиса. Интерактивная Swagger-документация: `/docs`.

### Публичные / сотрудник (нужен JWT в заголовке `Authorization: Bearer <token>`)

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/auth/login` | Вход по телефону → возвращает JWT. Тело: `{"phone": "79..."}` |
| GET | `/me` | Профиль: ФИО, телефон, ИНН, гражданство, должность+уровень, бригадир |
| GET | `/me/shifts?from_date&to_date` | Смены за период (часы, ставка, выплата, статусы) |
| GET | `/me/summary?month=YYYY-MM` | Сводка за месяц (смен, часов, выплата, обед) |
| GET | `/me/advances` | Авансы + флаг `штраф` по каждой записи |
| GET | `/me/referrals` | Рефералы + прогресс к 100 ч по каждому другу |
| GET | `/me/notifications` | Новые выплаты с момента последнего просмотра |
| POST | `/me/notifications/read` | Отметить уведомления прочитанными |
| POST | `/me/notify-manager` | Заглушка для кнопки «Написать менеджеру» |
| GET | `/health` | Проверка состояния (БД) |

### Админ / менеджер (заголовок `X-Admin-Key: <ADMIN_KEY>`)

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/admin/login` | Проверка пароля менеджера |
| GET | `/admin/search?q=` | Поиск сотрудника по телефону или ФИО |
| GET | `/admin/employee/{id}/advances` | Авансы сотрудника |
| POST | `/admin/advance` | Создать аванс вручную |
| PATCH | `/admin/advance/{id}` | Изменить комментарий менеджера к авансу |
| POST | `/admin/sync/advances` | Полная перезапись авансов (приёмник Apps Script) |
| POST | `/admin/sync/referrals` | Перезапись рефералов АПД, `project='АПД'` (приёмник Apps Script) |
| GET | `/admin` | HTML-страница панели менеджера |

### Аутентификация

- JWT HS256, срок жизни — `ACCESS_TOKEN_EXPIRE_HOURS` (по умолчанию 24 ч).
- При ответе **401** фронтенд сбрасывает токен и показывает экран входа.
- Rate limit: **10 попыток / 60 секунд** на IP (на эндпоинте `/auth/login`, в памяти процесса).

---

## 5. Frontend

`static/index.html` — одностраничное приложение на чистом JS, без сборки.
Деплоится как есть на GitHub Pages.

### Вкладки

1. **Профиль** —
   - Личные данные: ФИО, бейдж **★ Бригадир**, должность + уровень, телефон, ИНН, гражданство.
   - Кнопка **«Написать менеджеру»** (открывает чат Bitrix24, ленивая загрузка виджета).
   - **Данные за месяц** — сводка (смен, часов, выплата, обед) + остаток по авансам.
   - **История** — лента событий: выплаты (с `часы × ставка`), авансы, **штрафы** (красным).
   - **Частые вопросы** — аккордеон FAQ (в т.ч. «Что такое аванс?»).
2. **АПД 🤝** — акция «Приведи друга»: список приведённых с **прогресс-баром к 100 ч**
   и суммой премии.

### Локализация

4 языка: русский (`ru`), таджикский (`tg`), узбекский (`uz`), киргизский (`ky`).
Строки — в объектах переводов внутри `index.html`; переключение хранится в `localStorage`.

### Ключевая конфигурация во фронтенде

- Адрес backend задаётся константой `API` в начале `<script>` в `index.html`.
- CORS на backend разрешает `https://danilov11.github.io` (см. `main.py`).

---

## 6. ETL — загрузка данных

| Скрипт | Источник | Цель | Запуск |
|--------|----------|------|--------|
| `etl_shifts.py` | Google Sheets (смены) | `shifts` | GitHub Actions (ежедневно) / вручную |
| `etl_referrals.py` | Google Sheets («Приведи друга») | `referrals` | GitHub Actions (ежедневно) / вручную |
| `etl_advances.py` | `Авансы.xlsx` | `advances` | Вручную (или Apps Script → `/admin/sync/advances`) |
| `apps_script_referrals.gs` | Google Sheets (АПД) | `referrals` (`project='АПД'`) | Apps Script → `/admin/sync/referrals` |

**АПД «Приведи друга» (Apps Script).** Лист с колонками: `ФИО рекрутера` (A),
`ФИО кого привёл` (B), `Телефон кто пришёл` (C), `Сумма` (D). Скрипт
`apps_script_referrals.gs` шлёт строки на `/admin/sync/referrals`. Реферрер
сопоставляется с сотрудником **по ФИО** (первые 2 слова), приведённый — **по
телефону** (по нему `/me/referrals` считает прогресс к 100 ч). Реферрер видит
рефералов только если он есть в `employees` (офисные рекрутёры в портал не входят).

- **Смены и рефералы** берутся из публичных Google Sheets (CSV-экспорт) — доступ к
  редактированию не нужен. Расписание: `etl_cron.yml`, **02:00 UTC = 05:00 МСК**.
- **Авансы**: есть доступ к Google Sheet → используется **Apps Script**, который шлёт
  данные на `POST /admin/sync/advances` (полная перезапись таблицы). Альтернатива —
  локальный `etl_advances.py` из `Авансы.xlsx`.
- **Ручная загрузка Excel** (как делалось для июньских выгрузок): отдельные скрипты
  читают xlsx по листам (один лист = один день) и делают upsert в `shifts` по ключу
  `(employee_id, shift_date, store_id)`. См. историю в git и раздел 9.

### Формат ежедневной выгрузки (Excel)

Один лист на дату (`01.06`, `02.06`, …). Ключевые колонки (правая часть листа):
`Телефон 79***`, `Дата.1`, `ФИО`, `ТК` (= store_id), `Специальность в таймбуке`
(услуга с префиксом уровня), `Часы`, `Тариф`, `Компенсация обеда`, `Выплата`,
`Гражданство`, `Тип`, `Регулярность выплат`.

> ⚠️ Загрузку больших файлов в Railway делать **батчами** (bulk insert), а не построчно —
> построчные запросы через интернет очень медленные (см. раздел 9).

---

## 7. Интеграции

### Bitrix24 (чат «Написать менеджеру»)

- Виджет Open Channel грузится **лениво** — только при первом клике на кнопку.
- Идентификация сотрудника менеджеру — через событие `onBitrixLiveChat` →
  `widget.setUserRegisterData({hash, name, lastName, phone})`.
- `hash` = `md5(f"{employee_id}_{BITRIX_SECRET}")`, отдаётся в `/me` как `bitrix_hash`.
- Вебхук/секреты — в переменных окружения (`BITRIX_WEBHOOK`, `BITRIX_SECRET`).

### Telegram-бот (`bot.py`)

- Привязывает номер телефона к `chat_id` (таблица `telegram_links`).
- Запускается отдельным процессом (worker), нужен `TELEGRAM_BOT_TOKEN`.

---

## 8. Переменные окружения

Файл `.env` (НЕ в git; шаблон — `.env.example`):

| Переменная | Назначение |
|-----------|-----------|
| `DATABASE_URL` | Строка подключения PostgreSQL. **Внутренний** URL (`*.railway.internal`) — для backend на Railway; **публичный** (`*.proxy.rlwy.net`) — для ETL из GitHub Actions и с локальной машины |
| `SECRET_KEY` | Подпись JWT |
| `ACCESS_TOKEN_EXPIRE_HOURS` | Срок жизни токена (24) |
| `ADMIN_KEY` | Пароль/ключ панели менеджера (`X-Admin-Key`) |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `BITRIX_WEBHOOK` | Вебхук Bitrix24 REST |
| `BITRIX_SECRET` | Соль для `bitrix_hash` |

GitHub Actions использует **secrets** репозитория: `DATABASE_URL` (публичный!),
`SHIFTS_SHEET_URL` (ссылка на Google Sheet смен).

---

## 9. Локальный запуск и отладка

```bash
cd lenta_api
pip install -r requirements.txt
cp .env.example .env          # заполнить DATABASE_URL и секреты

# Запуск backend (локально БД или публичный Railway URL)
uvicorn main:app --reload --port 8000
# открыть http://127.0.0.1:8000  и  http://127.0.0.1:8000/docs

# ETL вручную
DATABASE_URL="postgresql://...proxy.rlwy.net:PORT/railway" python3 etl_shifts.py

# Telegram-бот
python3 bot.py
```

### Подключение к продовой БД с локальной машины

Использовать **публичный** URL Railway (`zephyr.proxy.rlwy.net:PORT`), а не
`*.railway.internal` (внутренний резолвится только внутри Railway).

### Массовая загрузка Excel — рекомендации

- Загружать справочники (`phone→id`, `store_id`, `service`) в память один раз.
- Делать `psycopg2.extras.execute_values` (bulk) вместо построчных `INSERT`.
- Дедуп по `(employee_id, shift_date, COALESCE(store_id,-1))`; при повторных
  запусках предпочитать строку с заполненной `rate`.

---

## 10. Деплой

| Что | Куда | Триггер |
|-----|------|---------|
| Frontend (`static/`) | GitHub Pages | push в `main` → `pages.yml` |
| Backend (`main.py` …) | Railway | привязка к репозиторию (авто-деплой при push) |
| ETL (смены, рефералы) | GitHub Actions | cron `etl_cron.yml` (02:00 UTC) + вручную |

После изменения **backend** убедиться, что Railway передеплоился (иначе новые поля
API не появятся). Frontend на Pages обновляется за 1–2 минуты.

---

## 11. Известные ограничения / техдолг

- **Связь рефералов неполная.** `referrer_employee_id` заполнен лишь у части записей;
  `referred_phone` часто отсутствует или указывает не на друга. Поэтому прогресс-бар
  по 100 ч считает реальные часы только когда приведённый есть в `shifts`. Большинство
  рефералов — из других проектов (Х5, Самокат и т.д.), их смен в БД нет.
- **Rate limit и `notifications_seen_at`** — состояние логина хранится в памяти процесса
  (сбрасывается при рестарте; не работает при нескольких репликах).
- **`/me/notify-manager`** — заглушка (возвращает `ok`), реальная идентификация идёт
  через `setUserRegisterData` на фронте.
- Секреты ранее засветились в переписке (DB-пароль, вебхук Bitrix). **Сменить пароль
  БД и вебхук** при передаче (см. HANDOVER.md).
