import sqlite3
from pathlib import Path

DB_PATH = Path("matcher.db")
SCHEMA_PATH = Path("db/schema.sql")

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          
    conn.execute("PRAGMA foreign_keys = ON") 
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()