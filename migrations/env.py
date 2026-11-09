"""Alembic environment. Migrations are plain SQL files wrapped by revision modules."""

from logging.config import fileConfig

from alembic import context

from gawe.db.session import make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_online() -> None:
    engine = make_engine()
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline mode is not supported: migrations are raw SQL files")
run_migrations_online()
