"""
Lenta Payments API
Сотрудник входит по номеру телефона и видит свои смены и выплаты.
"""

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from datetime import date
import re
import os

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
    CREATE INDEX IF NOT EXISTS idx_shifts_employee_date ON shifts(employee_id, shift_date);
    CREATE INDEX IF NOT EXISTS idx_employees_phone ON employees(phone);

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
def login(body: LoginRequest):
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
                   citizenship, employee_type, payment_frequency, supervisor
            FROM employees WHERE employee_id = %s
        """, (employee_id,))
    if not emp:
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    return dict(emp)


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
                sv.service_name AS услуга,
                s.shift_type    AS тип_смены,
                s.planned_start AS начало_план,
                s.planned_end   AS конец_план,
                s.actual_start  AS начало_факт,
                s.actual_end    AS конец_факт,
                s.hours_paid    AS часы_оплата,
                t.rate          AS тариф,
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


# ─── Здоровье ────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
def health():
    try:
        with get_db() as conn:
            fetchone(conn, "SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "ok", "db": "unavailable", "detail": str(e)}
