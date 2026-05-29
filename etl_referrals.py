"""
ETL: загружает данные с листа «Приведи друга» из Google Sheets → таблицу referrals.

Запуск:
    python etl_referrals.py
    DATABASE_URL=postgresql://... python etl_referrals.py
"""

import os
import re
import csv
import io
import urllib.request
from database import DSN
import psycopg2

SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1ciySadyCKFXLXCuiDwKML0t5Ypf8TZkM5ftVODmG_Eg"
    "/export?format=csv&gid=2497522"
)
# Если есть локальный Excel-файл — используем его (имена там полные)
LOCAL_XLSX = os.path.join(os.path.dirname(__file__), "../Авансы.xlsx")
SHEET_NAME_IN_XLSX = "Приведи друга"

# Маппинг статусов для нормализации
STATUS_MAP = {
    "оплатили": "Оплачено",
    "оплачено": "Оплачено",
    "не оплатили": "Не оплачено",
    "не оплачено": "Не оплачено",
    "в работе": "В работе",
    "отказ": "Отказ",
}


def normalize_phone(phone: str) -> str:
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if digits else None


def normalize_status(status: str) -> str:
    if not status:
        return None
    return STATUS_MAP.get(status.strip().lower(), status.strip())


def load_data() -> list[dict]:
    """Загружает из локального Excel (если есть) или из Google Sheets."""
    xlsx_path = LOCAL_XLSX if os.path.exists(LOCAL_XLSX) else None
    if not xlsx_path:
        xlsx_alt = os.path.expanduser("~/Downloads/Авансы.xlsx")
        if os.path.exists(xlsx_alt):
            xlsx_path = xlsx_alt

    if xlsx_path:
        print(f"↓ Читаем из Excel: {xlsx_path}, лист «{SHEET_NAME_IN_XLSX}»")
        import pandas as pd
        df = pd.read_excel(xlsx_path, sheet_name=SHEET_NAME_IN_XLSX)
        # Нормализуем телефон-как-число
        def ph(v):
            if pd.isna(v): return ""
            return str(int(float(str(v)))) if str(v).replace(".","").isdigit() else str(v).strip()
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "Проект":         str(r.get("Проект","") or "").strip(),
                "Город":          str(r.get("Город","") or "").strip(),
                "Месяц":          str(r.get("Месяц","") or "").strip(),
                "Год":            str(r.get("Год","") or "").strip(),
                "Период":         str(r.get("Период","") or "").strip(),
                "ФИО":            str(r.get("ФИО","") or "").strip(),
                "Номер телефона": ph(r.get("Номер телефона")),
                "От кого пришел": str(r.get("От кого пришел","") or "").strip(),
                "Сумма премии":   str(r.get("Сумма премии","") or "").strip(),
                "Статус":         str(r.get("Статус","") or "").strip(),
            })
        print(f"  Строк в Excel: {len(rows)}")
        return rows

    print(f"↓ Загружаем CSV из Google Sheets…")
    req = urllib.request.Request(SHEET_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    print(f"  Строк в CSV: {len(rows)}")
    return rows


def find_referrer_id(cur, referrer_name: str):
    """
    Ищет employee_id по имени из колонки «От кого пришел».
    Стратегии (по убыванию точности):
      1. Точное совпадение full_name
      2. Первые два слова (имя + отчество)
      3. Первое слово, если уникально
    """
    if not referrer_name:
        return None
    name = referrer_name.strip()
    # 1. Точное совпадение
    cur.execute(
        "SELECT employee_id FROM employees WHERE TRIM(full_name) = %s LIMIT 1",
        (name,)
    )
    row = cur.fetchone()
    if row:
        return row[0]
    # 2. Первые два слова
    parts = name.split()
    if len(parts) >= 2:
        prefix = parts[0] + " " + parts[1]
        cur.execute(
            "SELECT employee_id FROM employees WHERE full_name ILIKE %s LIMIT 2",
            (prefix + "%",)
        )
        results = cur.fetchall()
        if len(results) == 1:
            return results[0][0]
    # 3. Первое слово, если уникально
    first = parts[0] if parts else ""
    if len(first) < 3:
        return None
    cur.execute(
        "SELECT employee_id FROM employees WHERE full_name ILIKE %s LIMIT 2",
        (first + "%",)
    )
    results = cur.fetchall()
    return results[0][0] if len(results) == 1 else None


def run():
    rows = load_data()
    conn = psycopg2.connect(DSN)
    cur  = conn.cursor()

    # Очищаем и перегружаем
    cur.execute("TRUNCATE TABLE referrals RESTART IDENTITY")

    inserted = 0
    skipped  = 0

    for row in rows:
        # Определяем имена колонок (могут немного отличаться)
        project       = (row.get("Проект") or "").strip() or None
        city          = (row.get("Город") or "").strip() or None
        month_raw     = (row.get("Месяц") or "").strip()
        year_raw      = (row.get("Год") or "").strip()
        period        = (row.get("Период") or "").strip() or None
        referred_name = (row.get("ФИО") or "").strip() or None
        referred_phone_raw = (row.get("Номер телефона") or "").strip()
        referrer_name = (row.get("От кого пришел") or "").strip() or None
        amount_raw    = (row.get("Сумма премии") or "").strip()
        status_raw    = (row.get("Статус") or "").strip()

        # Пропускаем пустые строки
        if not referred_name and not referred_phone_raw and not referrer_name:
            skipped += 1
            continue

        referred_phone = normalize_phone(referred_phone_raw)
        status = normalize_status(status_raw)

        try:
            month = int(month_raw) if month_raw else None
        except ValueError:
            month = None
        try:
            year = int(year_raw) if year_raw else None
        except ValueError:
            year = None
        try:
            amount = float(amount_raw.replace(",", ".")) if amount_raw else None
        except ValueError:
            amount = None

        referrer_employee_id = find_referrer_id(cur, referrer_name)

        cur.execute("""
            INSERT INTO referrals
                (project, city, month, year, period,
                 referred_name, referred_phone,
                 referrer_name, referrer_employee_id,
                 amount, status)
            VALUES (%s,%s,%s,%s,%s, %s,%s, %s,%s, %s,%s)
        """, (project, city, month, year, period,
              referred_name, referred_phone,
              referrer_name, referrer_employee_id,
              amount, status))
        inserted += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"✓ Загружено: {inserted}, пропущено: {skipped}")

    # Статистика по совпадениям
    conn2 = psycopg2.connect(DSN)
    cur2  = conn2.cursor()
    cur2.execute("SELECT COUNT(*) FROM referrals WHERE referrer_employee_id IS NOT NULL")
    matched = cur2.fetchone()[0]
    cur2.execute("SELECT COUNT(*) FROM referrals")
    total   = cur2.fetchone()[0]
    cur2.close()
    conn2.close()
    print(f"  Привязано к сотрудникам: {matched} / {total}")


if __name__ == "__main__":
    run()
