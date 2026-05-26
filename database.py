import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from dotenv import load_dotenv
import os

load_dotenv()

DSN = os.getenv("DATABASE_URL", "postgresql://admin@localhost/lenta_payments")

@contextmanager
def get_db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def fetchone(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def fetchall(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def execute(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
