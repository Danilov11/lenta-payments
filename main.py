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

from database import get_db, fetchone, fetchall
from auth import create_access_token, get_current_employee_id

app = FastAPI(
    title="Lenta Payments API",
    description="Сервис просмотра смен и выплат для сотрудников",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# ─── Здоровье ────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
def health():
    try:
        with get_db() as conn:
            fetchone(conn, "SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "ok", "db": "unavailable", "detail": str(e)}
