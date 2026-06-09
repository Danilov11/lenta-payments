"""
ETL: загружает смены/выплаты из Google Sheets → таблицу shifts.

Запуск:
    SHIFTS_SHEET_URL="https://docs.google.com/..." python3 etl_shifts.py

Переменные окружения:
    SHIFTS_SHEET_URL  — ссылка на Google Sheets (Файл → Поделиться → Скопировать ссылку)
                        Скрипт сам конвертирует её в CSV-экспорт.
    DATABASE_URL      — строка подключения к PostgreSQL (на Railway уже есть)
"""

import os
import re
import io
import sys
import requests
import pandas as pd
import psycopg2
from datetime import date
from database import DSN

# ── Конфиг ────────────────────────────────────────────────────────────────────
SHEET_URL = os.getenv("SHIFTS_SHEET_URL", "")

# Маппинг колонок Google Sheet → поля БД
# Поменяйте ключи если в вашей таблице другие названия колонок
COL_MAP = {
    "date":        ["дата", "date", "shift_date", "дата смены"],
    "phone":       ["телефон", "phone", "номер", "номер телефона"],
    "full_name":   ["фио", "ф.и.о.", "сотрудник", "имя", "full_name"],
    "store":       ["магазин", "объект", "store", "адрес магазина"],
    "service":     ["услуга", "service", "вид работы"],
    "hours":       ["часы", "hours", "кол-во часов", "часов"],
    "payment":     ["выплата", "сумма", "итого", "payment", "оплата", "итоговая сумма"],
    "shift_type":  ["тип смены", "тип", "shift_type"],
    "status_cust": ["статус заказчика", "статус", "статус_заказчика"],
    "status_cont": ["статус исполнителя", "статус_исполнителя"],
    "lunch":       ["обед", "компенсация обеда", "обед компенсация"],
    "note":        ["примечание", "комментарий", "note"],
}

# ── Утилиты ───────────────────────────────────────────────────────────────────

def sheets_to_csv_url(url: str) -> str:
    """Конвертирует обычную ссылку на Google Sheet в ссылку на CSV-экспорт."""
    # https://docs.google.com/spreadsheets/d/SHEET_ID/edit#gid=GID
    m = re.search(r"/spreadsheets/d/([^/]+)", url)
    if not m:
        raise ValueError(f"Не удалось извлечь ID таблицы из URL: {url}")
    sheet_id = m.group(1)
    gid_m = re.search(r"gid=(\d+)", url)
    gid = gid_m.group(1) if gid_m else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def normalize_phone(phone) -> str | None:
    if pd.isna(phone) or not str(phone).strip():
        return None
    d = re.sub(r"\D", "", str(phone))
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    return d if len(d) >= 10 else None


def find_col(headers: list, variants: list) -> str | None:
    """Ищет колонку по списку возможных названий (без учёта регистра)."""
    h_lower = {h.lower().strip(): h for h in headers}
    for v in variants:
        if v.lower() in h_lower:
            return h_lower[v.lower()]
    return None


def find_employee(cur, phone=None, full_name=None) -> int | None:
    if phone:
        cur.execute("SELECT employee_id FROM employees WHERE phone=%s LIMIT 1", (phone,))
        r = cur.fetchone()
        if r: return r[0]
    if full_name:
        cur.execute("SELECT employee_id FROM employees WHERE TRIM(full_name)=%s LIMIT 1", (full_name.strip(),))
        r = cur.fetchone()
        if r: return r[0]
        parts = full_name.strip().split()
        if parts and len(parts[0]) >= 3:
            cur.execute("SELECT employee_id FROM employees WHERE full_name ILIKE %s LIMIT 2", (parts[0]+"%",))
            rows = cur.fetchall()
            if len(rows) == 1: return rows[0][0]
    return None


def get_or_create_store(cur, store_name: str) -> int | None:
    if not store_name:
        return None
    cur.execute("SELECT store_id FROM stores WHERE store_name=%s LIMIT 1", (store_name,))
    r = cur.fetchone()
    if r: return r[0]
    # Создаём новый магазин
    cur.execute("INSERT INTO stores (store_name) VALUES (%s) RETURNING store_id", (store_name,))
    return cur.fetchone()[0]


def get_or_create_service(cur, service_name: str) -> int | None:
    if not service_name:
        return None
    cur.execute("SELECT service_id FROM services WHERE service_name=%s LIMIT 1", (service_name,))
    r = cur.fetchone()
    if r: return r[0]
    cur.execute("INSERT INTO services (service_name) VALUES (%s) RETURNING service_id", (service_name,))
    return cur.fetchone()[0]


# ── Основная логика ───────────────────────────────────────────────────────────

def run():
    if not SHEET_URL:
        print("✗ Не задана переменная SHIFTS_SHEET_URL")
        sys.exit(1)

    print(f"Загружаем данные из Google Sheets...")
    csv_url = sheets_to_csv_url(SHEET_URL)
    resp = requests.get(csv_url, timeout=30)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))
    print(f"  Загружено строк: {len(df)}")

    headers = df.columns.tolist()
    print(f"  Колонки в таблице: {headers}")

    # Определяем колонки
    cols = {field: find_col(headers, variants) for field, variants in COL_MAP.items()}
    print(f"  Определены колонки: { {k:v for k,v in cols.items() if v} }")

    if not cols["date"]:
        print("✗ Не найдена колонка с датой. Проверьте COL_MAP.")
        sys.exit(1)

    conn = psycopg2.connect(DSN)
    cur  = conn.cursor()

    inserted = 0
    updated  = 0
    skipped  = 0

    for _, row in df.iterrows():
        # Дата
        date_val = row.get(cols["date"]) if cols["date"] else None
        if pd.isna(date_val):
            skipped += 1; continue
        try:
            shift_date = pd.to_datetime(date_val).date()
        except Exception:
            skipped += 1; continue

        # Сотрудник
        phone    = normalize_phone(row.get(cols["phone"])) if cols["phone"] else None
        name     = str(row.get(cols["full_name"], "") or "").strip() or None if cols["full_name"] else None
        emp_id   = find_employee(cur, phone, name)
        if not emp_id:
            skipped += 1; continue

        # Магазин / услуга
        store_name   = str(row.get(cols["store"], "") or "").strip() or None if cols["store"] else None
        service_name = str(row.get(cols["service"], "") or "").strip() or None if cols["service"] else None
        store_id   = get_or_create_store(cur, store_name) if store_name else None
        service_id = get_or_create_service(cur, service_name) if service_name else None

        # Числа
        def safe_float(col):
            if not col: return None
            v = row.get(col)
            if pd.isna(v): return None
            try: return float(str(v).replace(",", ".").replace(" ", ""))
            except: return None

        payment    = safe_float(cols["payment"]) or 0
        hours      = safe_float(cols["hours"])
        lunch      = safe_float(cols["lunch"]) or 0
        shift_type = str(row.get(cols["shift_type"], "") or "").strip() or None if cols["shift_type"] else None
        st_cust    = str(row.get(cols["status_cust"], "") or "").strip() or None if cols["status_cust"] else None
        st_cont    = str(row.get(cols["status_cont"], "") or "").strip() or None if cols["status_cont"] else None
        note       = str(row.get(cols["note"], "") or "").strip() or None if cols["note"] else None

        # Upsert: обновляем если уже есть запись за этот день/сотрудник/магазин
        cur.execute("""
            SELECT shift_id FROM shifts
            WHERE employee_id=%s AND shift_date=%s AND COALESCE(store_id,-1)=COALESCE(%s,-1)
            LIMIT 1
        """, (emp_id, shift_date, store_id))
        existing = cur.fetchone()

        if existing:
            cur.execute("""
                UPDATE shifts SET
                    total_payment=%s, hours_paid=%s, lunch_compensation=%s,
                    shift_type=%s, status_customer=%s, status_contractor=%s, note=%s
                WHERE shift_id=%s
            """, (payment, hours, lunch, shift_type, st_cust, st_cont, note, existing[0]))
            updated += 1
        else:
            cur.execute("""
                INSERT INTO shifts
                    (employee_id, shift_date, store_id, service_id,
                     total_payment, hours_paid, lunch_compensation,
                     shift_type, status_customer, status_contractor, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (emp_id, shift_date, store_id, service_id,
                  payment, hours, lunch, shift_type, st_cust, st_cont, note))
            inserted += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"\n✓ Готово!")
    print(f"  Добавлено новых смен:  {inserted}")
    print(f"  Обновлено существующих: {updated}")
    print(f"  Пропущено (не найден сотрудник / нет даты): {skipped}")


if __name__ == "__main__":
    run()
