# Инструкция по передаче проекта

Документ для передачи личного кабинета сотрудника Ленты новому
владельцу/разработчику. Полное техническое описание — в [DOCUMENTATION.md](DOCUMENTATION.md).

---

## 0. Что это и где работает

- **Назначение:** сотрудники входят по номеру телефона и смотрят смены, выплаты,
  авансы, штрафы, акцию «Приведи друга».
- **Сайт (frontend):** GitHub Pages — `https://danilov11.github.io/lenta-payments/`
- **API (backend):** Railway — FastAPI-сервис.
- **База:** PostgreSQL 16 на Railway.
- **Код:** https://github.com/Danilov11/lenta-payments

---

## 1. Доступы, которые нужно передать

Запросить у текущего владельца и переоформить на нового:

| Сервис | Что передать | Действие при передаче |
|--------|--------------|----------------------|
| **GitHub** | Репозиторий `Danilov11/lenta-payments` | Передать владение репозиторием или добавить нового владельца как admin |
| **Railway** | Проект с backend + PostgreSQL | Добавить нового владельца в проект, затем убрать старого |
| **Google Sheets** | Таблицы смен, рефералов, авансов | Поделиться доступом; для авансов — доступ на редактирование (Apps Script) |
| **Bitrix24** | Портал `b24-ku4v54.bitrix24.ru`, вебхук | Передать админ-доступ; перевыпустить вебхук |
| **Telegram** | Бот (`@BotFather`) | Передать токен или владельца бота |
| **Домен** (если есть) | — | Перепривязать |

---

## 2. ⚠️ Безопасность — сделать СРАЗУ при передаче

Часть секретов ранее передавалась в переписке. Перед передачей **обязательно**:

1. **Сменить пароль PostgreSQL** в Railway (Postgres → Settings → сбросить пароль),
   затем обновить `DATABASE_URL`:
   - в переменных backend на Railway,
   - в GitHub Secrets (публичный URL),
   - в локальном `.env`.
2. **Перевыпустить вебхук Bitrix24** (старый токен скомпрометирован) и обновить
   `BITRIX_WEBHOOK`.
3. **Сменить `SECRET_KEY`** (JWT) и `ADMIN_KEY` (пароль панели менеджера) на Railway.
4. **Перевыпустить токен Telegram-бота** через `@BotFather` при необходимости.

После любой смены `SECRET_KEY` все выданные JWT станут недействительны — сотрудники
просто войдут заново по телефону.

---

## 3. Переменные окружения (Railway → Variables)

```
DATABASE_URL                # внутренний URL Railway для backend
SECRET_KEY                  # подпись JWT — сменить
ACCESS_TOKEN_EXPIRE_HOURS=24
ADMIN_KEY                   # пароль панели менеджера — сменить
TELEGRAM_BOT_TOKEN          # если используется бот
BITRIX_WEBHOOK              # перевыпустить
BITRIX_SECRET              # соль для bitrix_hash
```

**GitHub → Settings → Secrets and variables → Actions:**
```
DATABASE_URL        # ПУБЛИЧНЫЙ URL (*.proxy.rlwy.net) — иначе ETL из Actions не подключится
SHIFTS_SHEET_URL    # ссылка на Google Sheet смен
```

> Важно: backend на Railway использует **внутренний** `DATABASE_URL`
> (`*.railway.internal`), а GitHub Actions и локальная машина — **публичный**
> (`*.proxy.rlwy.net`). Это разные строки для одной базы.

---

## 4. Как развернуть с нуля (если переносить на новый аккаунт)

### Backend + БД (Railway)

1. Создать проект на Railway, добавить **PostgreSQL**.
2. Подключить GitHub-репозиторий как сервис (Deploy from GitHub).
3. Прописать переменные окружения (раздел 3). `DATABASE_URL` Railway подставит сам
   при линковке базы — проверить, что это внутренний URL.
4. Старт описан в `Procfile` / `railway.toml`, healthcheck — `/health`.
5. При первом запуске `init_db()` сам создаст все таблицы и применит миграции.

### Frontend (GitHub Pages)

1. В репозитории: Settings → Pages → Source = **GitHub Actions**.
2. Workflow `pages.yml` уже деплоит папку `static/` при push в `main`.
3. В `static/index.html` проверить константу `API` (адрес backend) и в `main.py`
   список разрешённых CORS-origin.

### ETL (GitHub Actions)

1. Добавить секреты `DATABASE_URL` (публичный) и `SHIFTS_SHEET_URL`.
2. Workflow `etl_cron.yml` запускается ежедневно в 02:00 UTC и вручную
   (Actions → ETL → Run workflow).

---

## 5. Регулярные операции (как обслуживать)

### Обновление данных

- **Смены и рефералы** обновляются автоматически (cron в 02:00 UTC). Проверить —
  вкладка Actions в GitHub (зелёные галочки).
- **Авансы** — через Apps Script в Google-таблице авансов (шлёт на
  `/admin/sync/advances`). Если Apps Script отвалился — можно запустить
  `etl_advances.py` локально из `Авансы.xlsx`.
- **Ручная догрузка Excel** (новые дневные выгрузки): см. DOCUMENTATION.md §6, §9.
  Грузить **батчами** в публичный URL Railway.

### Проверить, что сервис жив

```bash
curl https://<railway-домен>/health        # {"status":"ok","db":"connected"}
```

### Дать тестовый вход

В `init_db()` создаётся тестовый пользователь `79059057757`.
Любой реальный сотрудник входит по своему номеру (формат `79XXXXXXXXX`).

### Панель менеджера

`https://<railway-домен>/admin` — вход по `ADMIN_KEY`. Поиск сотрудника,
просмотр/правка авансов.

---

## 6. Типовые проблемы и решения

| Симптом | Причина | Решение |
|---------|---------|---------|
| Сотрудник видит «нет данных за период» | Backend смотрит не на ту БД (локальную) | Проверить `DATABASE_URL` на Railway; локально — публичный URL |
| ETL в Actions падает с `DATABASE_URL empty` / connection refused | Не задан секрет или указан внутренний URL | Задать в GitHub Secrets **публичный** URL |
| Новые поля API не появились на сайте | Railway не передеплоил backend | Проверить деплой Railway; Pages обновляется отдельно |
| Дубли смен после загрузки | Пересеклись две загрузки | Дедуп по `(employee_id, shift_date, store_id)`, оставить строку с `rate` |
| Кнопка «Написать менеджеру» не открывает чат | Виджет Bitrix не загрузился / popup заблокирован | Грузится лениво по клику; не использовать `window.open` в setTimeout |
| Имя сотрудника не видно менеджеру в чате | Не сработал `setUserRegisterData` | Проверить `bitrix_hash` в `/me` и событие `onBitrixLiveChat` |
| Загрузка в Railway очень медленная | Построчные запросы через интернет | Bulk insert (`execute_values`), справочники в память |

---

## 7. Чек-лист передачи

- [ ] Новый владелец добавлен в GitHub-репозиторий (admin/owner)
- [ ] Новый владелец добавлен в проект Railway
- [ ] **Сменён пароль PostgreSQL**, обновлён `DATABASE_URL` в 3 местах
- [ ] **Перевыпущен вебхук Bitrix24**, обновлён `BITRIX_WEBHOOK`
- [ ] Сменены `SECRET_KEY` и `ADMIN_KEY`
- [ ] Переданы/обновлены доступы к Google Sheets (смены, рефералы, авансы)
- [ ] Передан токен Telegram-бота
- [ ] Проверен `/health` — БД отвечает
- [ ] Проверены последние запуски GitHub Actions (Pages + ETL)
- [ ] Выполнен тестовый вход сотрудника по телефону
- [ ] Прочитаны DOCUMENTATION.md и этот файл

---

## 8. Контакты и ссылки

- Репозиторий: https://github.com/Danilov11/lenta-payments
- Сайт: https://danilov11.github.io/lenta-payments/
- Backend (Railway): см. домен в настройках проекта Railway
- Bitrix24: https://b24-ku4v54.bitrix24.ru
- Полная техдокументация: [DOCUMENTATION.md](DOCUMENTATION.md)
