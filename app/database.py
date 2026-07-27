from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


async def create_schema() -> None:
    # MVP bootstrap. The small compatibility upgrade keeps existing installations working.
    # Replace this with Alembic before the schema starts changing frequently.
    from app import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all only adds indexes for a new database. Keep existing MVP
        # installations fast until schema changes are managed by Alembic.
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_supply_tasks_type_completed_at "
                "ON supply_tasks (supply_type, completed_at DESC)"
            )
        )
        if connection.dialect.name == "postgresql":
            await connection.execute(
                text("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'admin_alert'")
            )
            await connection.execute(
                text("ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS 'group_error'")
            )
            await connection.execute(
                text(
                    "ALTER TABLE food_assignments "
                    "ADD COLUMN IF NOT EXISTS reported_done_at TIMESTAMP WITH TIME ZONE"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE food_assignments "
                    "ADD COLUMN IF NOT EXISTS notification_revision INTEGER NOT NULL DEFAULT 0"
                )
            )
        elif connection.dialect.name == "sqlite":
            result = await connection.execute(text("PRAGMA table_info(food_assignments)"))
            columns = result.mappings().all()
            if "reported_done_at" not in {column["name"] for column in columns}:
                await connection.execute(
                    text("ALTER TABLE food_assignments ADD COLUMN reported_done_at DATETIME")
                )
            if "notification_revision" not in {column["name"] for column in columns}:
                await connection.execute(
                    text("ALTER TABLE food_assignments ADD COLUMN notification_revision INTEGER NOT NULL DEFAULT 0")
                )


async def close_database() -> None:
    await engine.dispose()
