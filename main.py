"""
Lenta Payments API
Сотрудник входит по номеру телефона и видит свои смены и выплаты.
"""

from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
import re
import os
import secrets
import hashlib
from collections import defaultdict
import threading
import httpx

from database import get_db, fetchone, fetchall, execute, DSN
from auth import create_access_token, get_current_employee_id
import psycopg2

def init_db():
    """Создаёт таблицы если их нет (идемпотентно)."""
    schema = """
    CREATE TABLE IF NOT EXISTS cities (
        city_id   SERIAL PRIMARY KEY,
        city_name VARCHAR(100) UNIQUE NOT NULL,
        division  VARCHAR(100)
    );
    CREATE TABLE IF NOT EXISTS stores (
        store_id        INTEGER PRIMARY KEY,
        store_name      VARCHAR(200),
        city_id         INTEGER REFERENCES cities(city_id),
        format          VARCHAR(20),
        parent_store_id INTEGER REFERENCES stores(store_id)
    );
    CREATE TABLE IF NOT EXISTS suppliers (
        supplier_id   SERIAL PRIMARY KEY,
        supplier_name VARCHAR(200) UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS services (
        service_id    SERIAL PRIMARY KEY,
        service_name  VARCHAR(300) UNIQUE NOT NULL,
        service_level SMALLINT
    );
    CREATE TABLE IF NOT EXISTS tariffs (
        tariff_id      SERIAL PRIMARY KEY,
        city_id        INTEGER REFERENCES cities(city_id),
        store_format   VARCHAR(20),
        service_id     INTEGER REFERENCES services(service_id),
        rate           NUMERIC(10,2) NOT NULL,
        effective_date DATE NOT NULL,
        UNIQUE (city_id, store_format, service_id, effective_date)
    );
    CREATE TABLE IF NOT EXISTS employees (
        employee_id       SERIAL PRIMARY KEY,
        full_name         VARCHAR(300) NOT NULL,
        phone             VARCHAR(30) UNIQUE,
        inn               VARCHAR(20),
        citizenship       VARCHAR(100),
        employee_type     VARCHAR(30),
        payment_frequency VARCHAR(30),
        supervisor        VARCHAR(200)
    );
    CREATE TABLE IF NOT EXISTS shifts (
        shift_id           SERIAL PRIMARY KEY,
        shift_date         DATE NOT NULL,
        employee_id        INTEGER REFERENCES employees(employee_id),
        store_id           INTEGER REFERENCES stores(store_id),
        service_id         INTEGER REFERENCES services(service_id),
        supplier_id        INTEGER REFERENCES suppliers(supplier_id),
        tariff_id          INTEGER REFERENCES tariffs(tariff_id),
        shift_type         VARCHAR(50),
        status_customer    VARCHAR(50),
        status_contractor  VARCHAR(50),
        planned_start      TIME,
        planned_end        TIME,
        actual_start       TIME,
        actual_end         TIME,
        hours_paid         NUMERIC(5,2),
        lunch_compensation NUMERIC(10,2) DEFAULT 0,
        total_payment      NUMERIC(10,2),
        sections           TEXT,
        note               TEXT
    );
    CREATE TABLE IF NOT EXISTS telegram_links (
        id         SERIAL PRIMARY KEY,
        phone      VARCHAR(30) UNIQUE NOT NULL,
        chat_id    BIGINT UNIQUE NOT NULL,
        username   VARCHAR(100),
        linked_at  TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS referrals (
        referral_id          SERIAL PRIMARY KEY,
        project              VARCHAR(100),
        city                 VARCHAR(100),
        month                INTEGER,
        year                 INTEGER,
        period               VARCHAR(100),
        referred_name        VARCHAR(500),
        referred_phone       VARCHAR(30),
        referrer_name        VARCHAR(500),
        referrer_employee_id INTEGER REFERENCES employees(employee_id),
        amount               NUMERIC(10,2),
        status               VARCHAR(100)
    );
    CREATE TABLE IF NOT EXISTS advances (
        advance_id   SERIAL PRIMARY KEY,
        employee_id  INTEGER REFERENCES employees(employee_id),
        advance_date DATE NOT NULL,
        amount       NUMERIC(10,2) NOT NULL,
        balance      NUMERIC(10,2),
        project      VARCHAR(200),
        note         TEXT,
        created_at   TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_employee_id);
    CREATE INDEX IF NOT EXISTS idx_advances_employee  ON advances(employee_id);
    CREATE INDEX IF NOT EXISTS idx_shifts_employee_date ON shifts(employee_id, shift_date);
    CREATE INDEX IF NOT EXISTS idx_employees_phone ON employees(phone);

    -- Миграции (колонки, добавленные после первого релиза; идемпотентны)
    ALTER TABLE employees ADD COLUMN IF NOT EXISTS notifications_seen_at TIMESTAMP;
    ALTER TABLE employees ADD COLUMN IF NOT EXISTS is_brigadier    BOOLEAN DEFAULT FALSE;
    ALTER TABLE employees ADD COLUMN IF NOT EXISTS brigadier_bonus NUMERIC;
    ALTER TABLE advances  ADD COLUMN IF NOT EXISTS manager_note    TEXT;
    ALTER TABLE shifts    ADD COLUMN IF NOT EXISTS rate            NUMERIC;

    -- Тестовый пользователь (для демо)
    INSERT INTO employees (full_name, phone, employee_type)
    VALUES ('Тестовый пользователь', '79059057757', 'физ_лицо')
    ON CONFLICT (phone) DO NOTHING;
    """
    try:
        conn = psycopg2.connect(DSN)
        cur  = conn.cursor()
        cur.execute(schema)
        conn.commit()
        cur.close()
        conn.close()
        print("✓ DB schema ready")
    except Exception as e:
        print(f"✗ DB init error: {e}")

app = FastAPI(
    title="Lenta Payments API",
    description="Сервис просмотра смен и выплат для сотрудников",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://danilov11.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ─── Rate limiting ────────────────────────────────────────────────────────────

_rate_lock    = threading.Lock()
_login_attempts: dict = defaultdict(list)   # ip → [timestamp, ...]
RATE_LIMIT    = 10    # попыток
RATE_WINDOW   = 60    # секунд

def check_rate_limit(ip: str):
    now = datetime.utcnow().timestamp()
    with _rate_lock:
        attempts = [t for t in _login_attempts[ip] if now - t < RATE_WINDOW]
        attempts.append(now)
        _login_attempts[ip] = attempts
    if len(attempts) > RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток. Подождите минуту."
        )


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


# ─── Схемы ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    phone: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ─── Аутентификация ───────────────────────────────────────────────────────────

@app.post("/auth/login", response_model=TokenResponse, summary="Войти по номеру телефона")
def login(body: LoginRequest, request: Request):
    check_rate_limit(request.client.host)
    phone = normalize_phone(body.phone)
    with get_db() as conn:
        emp = fetchone(conn,
            "SELECT employee_id FROM employees WHERE phone = %s", (phone,))
    if not emp:
        raise HTTPException(status_code=404, detail="Сотрудник с таким номером не найден")
    token = create_access_token(emp["employee_id"])
    return {"access_token": token}


# ─── Профиль сотрудника ───────────────────────────────────────────────────────

@app.get("/me", summary="Мой профиль")
def get_my_profile(employee_id: int = Depends(get_current_employee_id)):
    with get_db() as conn:
        emp = fetchone(conn, """
            SELECT employee_id, full_name, phone, inn,
                   citizenship, employee_type, payment_frequency, supervisor,
                   is_brigadier, brigadier_bonus
            FROM employees WHERE employee_id = %s
        """, (employee_id,))
        if not emp:
            raise HTTPException(status_code=404, detail="Сотрудник не найден")
        # Должность: самая частая услуга сотрудника + её уровень
        position = fetchone(conn, """
            SELECT sv.service_name AS услуга, sv.service_level AS уровень,
                   COUNT(*) AS cnt
            FROM shifts s
            JOIN services sv ON sv.service_id = s.service_id
            WHERE s.employee_id = %s AND sv.service_name IS NOT NULL
            GROUP BY sv.service_name, sv.service_level
            ORDER BY MAX(s.shift_date) DESC, cnt DESC
            LIMIT 1
        """, (employee_id,))
        city_row = fetchone(conn, """
            SELECT c.city_name
            FROM shifts s
            JOIN stores st ON st.store_id = s.store_id
            JOIN cities c  ON c.city_id   = st.city_id
            WHERE s.employee_id = %s AND c.city_name IS NOT NULL
            ORDER BY s.shift_date DESC
            LIMIT 1
        """, (employee_id,))
    data = dict(emp)
    data["city"] = city_row["city_name"] if city_row else None
    data["bitrix_hash"] = make_bitrix_hash(employee_id)
    if position:
        # Чистим название от префикса уровня "Nур_"
        name = re.sub(r'^\d+ур_', '', position["услуга"] or '')
        data["position"]       = name
        data["position_level"] = position["уровень"]
    else:
        data["position"]       = None
        data["position_level"] = None
    return data


# ─── Смены ────────────────────────────────────────────────────────────────────

@app.get("/me/shifts", summary="Мои смены за период")
def get_my_shifts(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    employee_id: int = Depends(get_current_employee_id),
):
    conditions = ["s.employee_id = %s"]
    params: list = [employee_id]
    if from_date:
        conditions.append("s.shift_date >= %s"); params.append(from_date)
    if to_date:
        conditions.append("s.shift_date <= %s"); params.append(to_date)

    with get_db() as conn:
        shifts = fetchall(conn, f"""
            SELECT
                s.shift_date,
                st.store_name   AS магазин,
                c.city_name     AS город,
                st.format       AS формат,
                sv.service_name    AS услуга,
                s.shift_type       AS тип_смены,
                s.status_customer  AS статус_заказчика,
                s.status_contractor AS статус_исполнителя,
                s.planned_start AS начало_план,
                s.planned_end   AS конец_план,
                s.actual_start  AS начало_факт,
                s.actual_end    AS конец_факт,
                s.hours_paid    AS часы_оплата,
                COALESCE(s.rate, t.rate) AS тариф,
                s.lunch_compensation AS компенсация_обеда,
                s.total_payment AS выплата,
                s.note          AS примечание
            FROM shifts s
            LEFT JOIN stores   st ON st.store_id   = s.store_id
            LEFT JOIN cities   c  ON c.city_id     = st.city_id
            LEFT JOIN services sv ON sv.service_id = s.service_id
            LEFT JOIN tariffs  t  ON t.tariff_id   = s.tariff_id
            WHERE {" AND ".join(conditions)}
            ORDER BY s.shift_date DESC
        """, tuple(params))
    return {"total": len(shifts), "shifts": [dict(r) for r in shifts]}


# ─── Сводка ───────────────────────────────────────────────────────────────────

@app.get("/me/summary", summary="Сводка выплат за месяц")
def get_my_summary(
    month: str = Query(..., description="YYYY-MM"),
    employee_id: int = Depends(get_current_employee_id),
):
    try:
        year, mon = map(int, month.split("-"))
        from_date = date(year, mon, 1)
        to_date   = date(year, mon + 1, 1) if mon < 12 else date(year + 1, 1, 1)
    except Exception:
        raise HTTPException(status_code=400, detail="Формат: YYYY-MM")

    with get_db() as conn:
        row = fetchone(conn, """
            SELECT
                COUNT(*)                               AS смен,
                ROUND(SUM(hours_paid)::numeric, 2)     AS часов_всего,
                ROUND(SUM(total_payment)::numeric, 2)  AS выплата_всего,
                ROUND(SUM(lunch_compensation)::numeric, 2) AS компенсация_обеда,
                ROUND(AVG(total_payment)::numeric, 2)  AS средняя_выплата_за_смену
            FROM shifts
            WHERE employee_id=%s AND shift_date>=%s AND shift_date<%s
        """, (employee_id, from_date, to_date))

        by_store = fetchall(conn, """
            SELECT st.store_name AS магазин,
                   COUNT(*)      AS смен,
                   ROUND(SUM(s.total_payment)::numeric, 2) AS выплата
            FROM shifts s
            LEFT JOIN stores st ON st.store_id = s.store_id
            WHERE s.employee_id=%s AND s.shift_date>=%s AND s.shift_date<%s
            GROUP BY st.store_name ORDER BY выплата DESC
        """, (employee_id, from_date, to_date))

    return {"месяц": month, "итого": dict(row), "по_магазинам": [dict(r) for r in by_store]}


# ─── Уведомления ─────────────────────────────────────────────────────────────

@app.get("/me/notifications", summary="Новые выплаты с последнего просмотра")
def get_notifications(employee_id: int = Depends(get_current_employee_id)):
    with get_db() as conn:
        # Берём дату последнего просмотра
        emp = fetchone(conn,
            "SELECT notifications_seen_at FROM employees WHERE employee_id=%s",
            (employee_id,))
        seen_at = emp["notifications_seen_at"] if emp else None

        # Смены с выплатой добавленные после последнего просмотра
        new_payments = fetchall(conn, """
            SELECT
                s.shift_date,
                st.store_name  AS магазин,
                c.city_name    AS город,
                s.total_payment AS выплата,
                s.hours_paid    AS часы
            FROM shifts s
            LEFT JOIN stores st ON st.store_id = s.store_id
            LEFT JOIN cities c  ON c.city_id   = st.city_id
            WHERE s.employee_id = %s
              AND s.total_payment > 0
              AND s.shift_date > %s
            ORDER BY s.shift_date DESC
            LIMIT 20
        """, (employee_id, seen_at))

    return {
        "count": len(new_payments),
        "payments": [dict(r) for r in new_payments],
        "seen_at": seen_at.isoformat() if seen_at else None,
    }


@app.post("/me/notifications/read", summary="Отметить уведомления как прочитанные")
def mark_notifications_read(employee_id: int = Depends(get_current_employee_id)):
    with get_db() as conn:
        execute(conn,
            "UPDATE employees SET notifications_seen_at = NOW() WHERE employee_id = %s",
            (employee_id,))
    return {"status": "ok"}


# ─── Реферальная программа ───────────────────────────────────────────────────

@app.get("/me/referrals", summary="Мои рефералы по акции «Приведи друга»")
def get_my_referrals(employee_id: int = Depends(get_current_employee_id)):
    with get_db() as conn:
        rows = fetchall(conn, """
            SELECT
                r.project      AS проект,
                r.city         AS город,
                r.year         AS год,
                r.month        AS месяц,
                r.period       AS период,
                r.referred_name  AS приведённый,
                r.referred_phone AS телефон,
                r.amount         AS сумма_премии,
                r.status         AS статус,
                COALESCE(SUM(s.hours_paid), 0) AS часы_реферала
            FROM referrals r
            LEFT JOIN employees e
                   ON regexp_replace(e.phone, '\\D', '', 'g')
                    = regexp_replace(r.referred_phone, '\\D', '', 'g')
            LEFT JOIN shifts s ON s.employee_id = e.employee_id
            WHERE r.referrer_employee_id = %s
            GROUP BY r.referral_id
            ORDER BY r.year DESC, r.month DESC
        """, (employee_id,))
    HOURS_GOAL = 100
    result = []
    for r in rows:
        d = dict(r)
        hrs = float(d.pop("часы_реферала") or 0)
        paid = (d.get("статус") or "").lower().find("оплач") >= 0
        d["часы_реферала"]    = round(hrs, 1)
        d["цель_часов"]       = HOURS_GOAL
        # Если премия уже выплачена — цель достигнута (100%)
        d["прогресс_процент"] = 100 if paid else min(round(hrs / HOURS_GOAL * 100), 100)
        d["выполнено"]        = paid or hrs >= HOURS_GOAL
        result.append(d)
    return {"total": len(result), "referrals": result, "hours_goal": HOURS_GOAL}


# ─── Авансы ──────────────────────────────────────────────────────────────────

@app.get("/me/advances", summary="Мои авансы")
def get_my_advances(employee_id: int = Depends(get_current_employee_id)):
    with get_db() as conn:
        rows = fetchall(conn, """
            SELECT
                advance_date  AS дата,
                project       AS проект,
                amount        AS сумма,
                balance       AS остаток,
                note          AS примечание,
                manager_note  AS комментарий_менеджера,
                (COALESCE(note,'') ILIKE '%%штраф%%'
                 OR COALESCE(manager_note,'') ILIKE '%%штраф%%') AS штраф
            FROM advances
            WHERE employee_id = %s
            ORDER BY advance_date DESC
        """, (employee_id,))
        totals = fetchone(conn, """
            SELECT
                COALESCE(SUM(amount), 0)                                   AS сумма_всего,
                COALESCE(SUM(CASE WHEN balance > 0 THEN balance END), 0)   AS остаток_всего
            FROM advances WHERE employee_id = %s
        """, (employee_id,))
    return {
        "total_amount":   float(totals["сумма_всего"]),
        "total_balance":  float(totals["остаток_всего"]),
        "advances": [dict(r) for r in rows]
    }


# ─── Панель менеджера ────────────────────────────────────────────────────────

ADMIN_KEY = os.getenv("ADMIN_KEY", "lenta-admin-2026")

class AdminLoginRequest(BaseModel):
    password: str

class AdvanceNoteRequest(BaseModel):
    manager_note: str

class AdvanceCreateRequest(BaseModel):
    employee_id: int
    advance_date: date
    amount: float
    balance: float
    note: Optional[str] = None
    manager_note: Optional[str] = None
    project: Optional[str] = None

def verify_admin(request: Request):
    key = request.headers.get("X-Admin-Key", "")
    if not secrets.compare_digest(key, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Доступ запрещён")

@app.post("/admin/login", include_in_schema=False)
def admin_login(body: AdminLoginRequest):
    if not secrets.compare_digest(body.password, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Неверный пароль")
    return {"status": "ok"}

@app.get("/admin/search", include_in_schema=False)
def admin_search(q: str = Query(...), request: Request = None):
    verify_admin(request)
    phone = re.sub(r"\D", "", q)
    if len(phone) == 11 and phone.startswith("8"):
        phone = "7" + phone[1:]
    with get_db() as conn:
        if phone:
            emp = fetchone(conn,
                "SELECT employee_id, full_name, phone FROM employees WHERE phone = %s",
                (phone,))
        else:
            emp = fetchone(conn,
                "SELECT employee_id, full_name, phone FROM employees WHERE full_name ILIKE %s LIMIT 1",
                (f"%{q}%",))
    if not emp:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    return dict(emp)

@app.get("/admin/employee/{employee_id}/advances", include_in_schema=False)
def admin_get_advances(employee_id: int, request: Request):
    verify_admin(request)
    with get_db() as conn:
        rows = fetchall(conn, """
            SELECT advance_id, advance_date AS дата, project AS проект,
                   amount AS сумма, balance AS остаток,
                   note AS примечание, manager_note AS комментарий_менеджера
            FROM advances WHERE employee_id = %s ORDER BY advance_date DESC
        """, (employee_id,))
    return {"advances": [dict(r) for r in rows]}

@app.patch("/admin/advance/{advance_id}", include_in_schema=False)
def admin_update_advance(advance_id: int, body: AdvanceNoteRequest, request: Request):
    verify_admin(request)
    with get_db() as conn:
        execute(conn,
            "UPDATE advances SET manager_note = %s WHERE advance_id = %s",
            (body.manager_note, advance_id))
    return {"status": "ok"}

@app.post("/admin/sync/advances", include_in_schema=False)
def sync_advances_from_sheet(payload: dict, request: Request):
    """Принимает авансы из Google Apps Script и полностью перезаписывает таблицу."""
    verify_admin(request)
    records = payload.get("advances", [])
    inserted = 0
    skipped  = 0

    def norm_phone(p):
        if not p: return None
        d = re.sub(r"\D", "", str(p))
        if len(d) == 11 and d.startswith("8"): d = "7" + d[1:]
        return d if len(d) >= 10 else None

    def find_emp(conn, full_name, phone):
        ph = norm_phone(phone)
        if ph:
            r = fetchone(conn, "SELECT employee_id FROM employees WHERE phone=%s", (ph,))
            if r: return r["employee_id"]
        if full_name:
            r = fetchone(conn, "SELECT employee_id FROM employees WHERE TRIM(full_name)=%s LIMIT 1", (full_name.strip(),))
            if r: return r["employee_id"]
            parts = full_name.strip().split()
            if parts and len(parts[0]) >= 3:
                rows = fetchall(conn, "SELECT employee_id FROM employees WHERE full_name ILIKE %s LIMIT 2", (parts[0]+"%",))
                if len(rows) == 1: return rows[0]["employee_id"]
        return None

    with get_db() as conn:
        execute(conn, "TRUNCATE TABLE advances RESTART IDENTITY")
        for rec in records:
            try:
                adv_date = date.fromisoformat(str(rec.get("date",""))[:10])
            except Exception:
                skipped += 1; continue
            try:
                amount = float(rec.get("amount") or 0)
            except Exception:
                skipped += 1; continue
            if amount <= 0:
                skipped += 1; continue

            emp_id = find_emp(conn, rec.get("full_name"), rec.get("phone"))
            if not emp_id:
                skipped += 1; continue

            balance_raw = rec.get("balance")
            try:    balance = float(balance_raw) if balance_raw not in (None, "") else None
            except: balance = None
            note = rec.get("note") or None

            execute(conn, """
                INSERT INTO advances
                    (employee_id, advance_date, amount, balance, project, note, manager_note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (emp_id, adv_date, amount, balance,
                  rec.get("project") or None, note, note))
            inserted += 1

    return {"status": "ok", "inserted": inserted, "skipped": skipped,
            "total": len(records)}


@app.post("/admin/advance", include_in_schema=False)
def admin_create_advance(body: AdvanceCreateRequest, request: Request):
    verify_admin(request)
    with get_db() as conn:
        row = fetchone(conn, """
            INSERT INTO advances (employee_id, advance_date, amount, balance, note, manager_note, project)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING advance_id
        """, (body.employee_id, body.advance_date, body.amount, body.balance,
              body.note, body.manager_note, body.project))
    return {"advance_id": row["advance_id"]}

@app.post("/admin/sync/referrals", include_in_schema=False)
def sync_referrals_from_sheet(payload: dict, request: Request):
    """Принимает рефералов «Приведи друга» из Google Apps Script.

    Полностью перезаписывает рефералов источника «АПД» (project='АПД'),
    исторические рефералы других проектов не трогает.

    Ожидаемый формат каждой строки:
        {"referrer_name": "...", "referred_name": "...",
         "referred_phone": "79...", "amount": 10000}

    Реферрер сопоставляется по ФИО (первые 2 слова), приведённый — по телефону
    (для расчёта прогресса по часам в /me/referrals).
    """
    verify_admin(request)
    records = payload.get("referrals", [])

    def norm_phone(p):
        if not p:
            return None
        d = re.sub(r"\D", "", str(p))
        if len(d) == 11 and d.startswith("8"):
            d = "7" + d[1:]
        return d if len(d) >= 10 else None

    def find_referrer(conn, name):
        """Ищет реферрера-сотрудника по ФИО (первые 2 слова, без регистра)."""
        if not name:
            return None
        parts = re.sub(r"\s+", " ", name.strip()).split(" ")
        if len(parts) >= 2 and len(parts[0]) >= 3:
            key = f"{parts[0]} {parts[1]}%"
            rows = fetchall(conn,
                "SELECT employee_id FROM employees WHERE full_name ILIKE %s LIMIT 2",
                (key,))
            if len(rows) == 1:
                return rows[0]["employee_id"]
        return None

    inserted = 0
    skipped  = 0
    linked_referrer = 0

    with get_db() as conn:
        execute(conn, "DELETE FROM referrals WHERE project = 'АПД'")
        for rec in records:
            phone = norm_phone(rec.get("referred_phone"))
            referred_name = (rec.get("referred_name") or "").strip() or None
            if not referred_name and not phone:
                skipped += 1
                continue
            try:
                amount = float(rec.get("amount") or 0)
            except Exception:
                amount = 0.0

            referrer_name = (rec.get("referrer_name") or "").strip() or None
            referrer_id   = find_referrer(conn, referrer_name)
            if referrer_id:
                linked_referrer += 1

            execute(conn, """
                INSERT INTO referrals
                    (project, referred_name, referred_phone,
                     referrer_name, referrer_employee_id, amount, status)
                VALUES ('АПД', %s, %s, %s, %s, %s, 'В работе')
            """, (referred_name, phone, referrer_name, referrer_id, amount))
            inserted += 1

    return {"status": "ok", "inserted": inserted, "skipped": skipped,
            "linked_referrer": linked_referrer, "total": len(records)}


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


# ─── Битрикс24 интеграция ────────────────────────────────────────────────────

BITRIX_WEBHOOK  = os.getenv("BITRIX_WEBHOOK",    "https://b24-ku4v54.bitrix24.ru/rest/120298/skj76rcj1h3f3eno")
BITRIX_MANAGER_ID = os.getenv("BITRIX_MANAGER_ID", "120298")
BITRIX_SECRET     = os.getenv("BITRIX_SECRET",     "lenta-b24-2026")

def make_bitrix_hash(employee_id: int) -> str:
    return hashlib.md5(f"{employee_id}_{BITRIX_SECRET}".encode()).hexdigest()

@app.post("/me/notify-manager", summary="Создать лид в Битрикс24")
async def notify_manager(employee_id: int = Depends(get_current_employee_id)):
    """При нажатии «Написать менеджеру» создаёт лид в CRM с данными сотрудника."""
    with get_db() as conn:
        emp = fetchone(conn,
            "SELECT full_name, phone FROM employees WHERE employee_id=%s",
            (employee_id,))
    if not emp:
        raise HTTPException(404, "Сотрудник не найден")

    full_name = emp["full_name"] or ""

    return {"status": "ok"}


# ─── Здоровье ────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
def health():
    try:
        with get_db() as conn:
            fetchone(conn, "SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "ok", "db": "unavailable", "detail": str(e)}
