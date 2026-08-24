import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


def utcnow() -> datetime:
    """Drop-in replacement for the deprecated (Python 3.13+)
    datetime.utcnow(). Every DateTime column in this schema stores a
    naive UTC value (no tzinfo), and the entire codebase compares
    "now" against values read back from those columns -- switching to
    a genuinely timezone-aware datetime.now(timezone.utc) here would
    make every one of those comparisons raise TypeError (can't compare
    offset-naive and offset-aware datetimes). This returns the exact
    same value datetime.utcnow() always did (naive, UTC) via the
    non-deprecated API, so behavior is identical everywhere it
    replaces a direct datetime.utcnow() call or a Column(default=...).
    A real migration to timezone-aware storage is a separate, bigger
    schema change, not this fix."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./crm.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_SQLITE = "sqlite" in DATABASE_URL

if IS_SQLITE:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Managed Postgres poolers (Supabase's included) close idle
    # connections server-side on their own schedule -- a connection this
    # app checked out and held across a long-running operation (e.g. a
    # multi-pass LLM tailoring call taking several minutes) can go stale
    # before the next query on it, surfacing as "server closed the
    # connection unexpectedly". pool_pre_ping tests each pooled
    # connection with a cheap query before handing it out and
    # transparently reconnects if it's dead; pool_recycle proactively
    # retires connections before they'd likely be closed server-side in
    # the first place.
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)

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
