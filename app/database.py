import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./crm.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_SQLITE = "sqlite" in DATABASE_URL

if IS_SQLITE:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)

if IS_SQLITE:
    # Phase 10: this app runs several independent SQLite connections at
    # once by design (the scheduler thread, plus a fresh background
    # thread per manual intake/score/tailor trigger -- see the routers/
    # scheduler.py) -- under SQLite's default rollback-journal mode,
    # one writer holds an exclusive file lock and any other connection
    # attempting to write at the same moment fails immediately with
    # "database is locked" (default busy timeout is 0). WAL mode lets
    # readers and a writer proceed concurrently; busy_timeout makes a
    # genuine writer-vs-writer collision retry for a few seconds instead
    # of failing instantly -- both applied per-connection since PRAGMAs
    # aren't persistent across connections in Python's sqlite3 driver.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
