import os
import sys
from logging.config import fileConfig

from dotenv import load_dotenv

from alembic import context

# So `import app.*` resolves the same way it does when running the app
# itself, regardless of the working directory `alembic` is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app/database.py reads DATABASE_URL directly from the environment at
# import time -- app/main.py calls load_dotenv() before importing it, but
# running `alembic` directly (not through the app) skips that entirely,
# so this would otherwise silently fall back to the sqlite default even
# when a real DATABASE_URL is configured in .env.
load_dotenv()

from app.database import engine  # noqa: E402
from app.models import Base  # noqa: E402 -- imports every model, populating Base.metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Deliberately NOT config.set_main_option("sqlalchemy.url", ...) here --
# configparser applies %-style interpolation to values from the .ini file,
# and this project's real DATABASE_URL (Supabase) contains a URL-encoded
# %40 in the password, which configparser rejects as invalid interpolation
# syntax. Using the app's own already-configured `engine` directly below
# sidesteps the whole problem, and as a bonus keeps Alembic's connection
# behavior identical to the real app's (same pool_pre_ping/pool_recycle
# settings, same SQLite WAL pragma listener) instead of a second,
# separately-configured connection path that could drift from it.

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# This project's Supabase project also hosts a separate application's
# tables (the portfolio site's visitor analytics/chatbot cache/LinkedIn
# reply tool/contact-form messages -- confirmed by their columns, not
# leftover cruft from this project's own old prototype). autogenerate's
# very first run proposed DROP TABLE for all four purely because they
# aren't in this app's models.py -- excluding them here so no future
# `alembic revision --autogenerate` can ever propose that again, not
# just a one-time manual edit to the baseline migration.
_FOREIGN_TABLES = {"portfolio_messages", "linkedin_comments", "chatbot_cache", "portfolio_visits"}


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name in _FOREIGN_TABLES:
        return False
    return True

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode, against the app's own real
    engine (see the note above target_metadata for why)."""
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_object=include_object
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
