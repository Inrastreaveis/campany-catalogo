from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import inspect, text
from src.models import Base


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def initialize(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._run_migrations(conn)

    async def _run_migrations(self, conn) -> None:
        columns = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("guild_settings")}
        )
        migrations = {
            "panel_banner_url": "TEXT",
            "panel_banner_data": "BYTEA" if conn.dialect.name == "postgresql" else "BLOB",
            "panel_banner_filename": "VARCHAR(255)",
        }
        for column_name, column_type in migrations.items():
            if column_name not in columns:
                await conn.execute(
                    text(f"ALTER TABLE guild_settings ADD COLUMN {column_name} {column_type}")
                )

    async def close(self) -> None:
        await self.engine.dispose()
