"""
ETL: загружает авансы из Авансы.xlsx → таблицу advances.

Источники:
  - лист «Авансы»          — основные авансы
  - лист «Авансы  Офис»    — офисные авансы
  - лист «Авансы списание» — списанные авансы
  - лист «Франклинск»      — авансы по Франклину (есть телефон)

Запуск:
    python3 etl_advances.py
    DATABASE_URL=postgresql://... python3 etl_advances.py
"""

import os
import re
import pandas as pd
import psycopg2
from database import DSN

XLSX_PATH = os.path.join(os.path.dirname(__file__), "../Авансы.xlsx")

# Листы с авансами (формат: дата, проект, город, фио, сумма, комментарий, списание, остаток)
ADVANCE_SHEETS = ["Авансы", "Авансы  Офис", "Авансы списание"]
FRANKLIN_SHEET = "Франклинск"


def normalize_phone(phone):
    if pd.isna(phone):
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if len(digits) >= 10 else None


def clean_name(name):
    if pd.isna(name):
        return None
    n = str(name).strip()
    return n if n else None


def find_employee_by_name(cur, full_name: str):
    """Точное совпадение → первое слово (фамилия)."""
    if not full_name:
        return None
    cur.execute(
        "SELECT employee_id FROM employees WHERE TRIM(full_name) = %s LIMIT 1",
        (full_name,)
    )
    row = cur.fetchone()
    if row:
        return row[0]
    # Пробуем по первому слову
    first = full_name.strip().split()[0]
    if len(first) < 3:
        return None
    cur.execute(
        "SELECT employee_id FROM employees WHERE full_name ILIKE %s LIMIT 2",
        (first + "%",)
    )
    results = cur.fetchall()
    return results[0][0] if len(results) == 1 else None


def find_employee_by_phone(cur, phone: str):
    if not phone:
        return None
    cur.execute(
        "SELECT employee_id FROM employees WHERE phone = %s LIMIT 1",
        (phone,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def load_standard_sheet(sheet_name: str, df: pd.DataFrame) -> list[dict]:
    """Парсит листы с колонками: Дата, Проект, Город, ФИО, Сумма, Комментарий, Списание, Остаток."""
    records = []
    for _, row in df.iterrows():
        date_val = row.get("Дата")
        if pd.isna(date_val):
            continue
        try:
            advance_date = pd.to_datetime(date_val).date()
        except Exception:
            continue

        amount_val = row.get("Сумма")
        if pd.isna(amount_val):
            continue
        try:
            amount = float(amount_val)
        except Exception:
            continue
        if amount <= 0:
            continue

        balance_raw = row.get("Остаток")
        try:
            balance = float(balance_raw) if not pd.isna(balance_raw) else None
        except Exception:
            balance = None

        comment_raw = row.get("Комментарий")
        note = None
        if not pd.isna(comment_raw):
            try:
                pd.to_datetime(comment_raw)
                note = None  # Это дата, а не комментарий
            except Exception:
                note = str(comment_raw).strip() or None

        records.append({
            "advance_date": advance_date,
            "project": str(row.get("Проект", "") or "").strip() or None,
            "full_name": clean_name(row.get("ФИО")),
            "phone": None,
            "amount": amount,
            "balance": balance,
            "note": note,
            "manager_note": note,  # «Комментарий» из Excel → колонка менеджера
            "source": sheet_name,
        })
    return records


def load_franklin_sheet(df: pd.DataFrame) -> list[dict]:
    """Лист Франклинск — есть колонка Телефон."""
    records = []
    for _, row in df.iterrows():
        date_val = row.get("Дата")
        if pd.isna(date_val):
            continue
        try:
            advance_date = pd.to_datetime(date_val).date()
        except Exception:
            continue

        amount_val = row.get("Сумма")
        if pd.isna(amount_val):
            continue
        try:
            amount = float(amount_val)
        except Exception:
            continue
        if amount <= 0:
            continue

        balance_raw = row.get("Остаток")
        try:
            balance = float(balance_raw) if not pd.isna(balance_raw) else None
        except Exception:
            balance = None

        records.append({
            "advance_date": advance_date,
            "project": str(row.get("Проект", "") or "").strip() or None,
            "full_name": clean_name(row.get("ФИО")),
            "phone": normalize_phone(row.get("Телефон")),
            "amount": amount,
            "balance": balance,
            "note": str(row.get("Комментарий", "") or "").strip() or None,
            "source": "Франклинск",
        })
    return records


def run():
    xlsx = XLSX_PATH
    if not os.path.exists(xlsx):
        # Try in Downloads
        xlsx = os.path.expanduser("~/Downloads/Авансы.xlsx")
    print(f"Читаем: {xlsx}")

    all_sheets = pd.read_excel(xlsx, sheet_name=None)
    all_records = []

    for sheet_name in ADVANCE_SHEETS:
        if sheet_name not in all_sheets:
            print(f"  ⚠ Лист «{sheet_name}» не найден, пропускаем")
            continue
        df = all_sheets[sheet_name]
        recs = load_standard_sheet(sheet_name, df)
        print(f"  Лист «{sheet_name}»: {len(recs)} записей")
        all_records.extend(recs)

    if FRANKLIN_SHEET in all_sheets:
        recs = load_franklin_sheet(all_sheets[FRANKLIN_SHEET])
        print(f"  Лист «{FRANKLIN_SHEET}»: {len(recs)} записей")
        all_records.extend(recs)

    print(f"Итого записей: {len(all_records)}")

    conn = psycopg2.connect(DSN)
    cur  = conn.cursor()
    cur.execute("TRUNCATE TABLE advances RESTART IDENTITY")

    inserted = 0
    skipped  = 0
    matched  = 0

    for rec in all_records:
        # Находим сотрудника
        emp_id = None
        if rec["phone"]:
            emp_id = find_employee_by_phone(cur, rec["phone"])
        if not emp_id and rec["full_name"]:
            emp_id = find_employee_by_name(cur, rec["full_name"])

        if not emp_id:
            skipped += 1
            continue

        matched += 1
        cur.execute("""
            INSERT INTO advances
                (employee_id, advance_date, amount, balance, project, note, manager_note)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (emp_id, rec["advance_date"], rec["amount"],
              rec["balance"], rec["project"], rec["note"],
              rec.get("manager_note")))
        inserted += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"\n✓ Загружено: {inserted}")
    print(f"  Привязано к сотрудникам: {matched}")
    print(f"  Не найдено в БД: {skipped}")

    # Показываем статистику остатков
    conn2 = psycopg2.connect(DSN)
    cur2  = conn2.cursor()
    cur2.execute("""
        SELECT COUNT(*) total, COUNT(CASE WHEN balance > 0 THEN 1 END) with_balance,
               ROUND(SUM(amount)::numeric, 2) total_amount,
               ROUND(SUM(CASE WHEN balance > 0 THEN balance ELSE 0 END)::numeric, 2) outstanding
        FROM advances
    """)
    r = cur2.fetchone()
    print(f"\n  Всего авансов: {r[0]}, с остатком: {r[1]}")
    print(f"  Сумма авансов: {r[2]} ₽, не погашено: {r[3]} ₽")
    cur2.close()
    conn2.close()


if __name__ == "__main__":
    run()
