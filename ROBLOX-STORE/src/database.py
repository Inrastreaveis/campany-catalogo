from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import inspect, text
from src.models import Base


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def initialize(self) -> None:
        # SQLite needs foreign-key checks disabled only while rebuilding the
        # participant table to make discord_user_id nullable. The migration
        # preserves participant IDs, so campaign_winners keep their links.
        if self.engine.dialect.name == "sqlite":
            async with self.engine.connect() as conn:
                await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                await conn.commit()
                async with conn.begin():
                    await conn.run_sync(Base.metadata.create_all)
                    await self._run_migrations(conn)
                await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                await conn.commit()
        else:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await self._run_migrations(conn)

    async def _run_migrations(self, conn) -> None:
        guild_columns = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("guild_settings")}
        )
        migrations = {
            "panel_banner_url": "TEXT",
            "panel_banner_data": "BYTEA" if conn.dialect.name == "postgresql" else "BLOB",
            "panel_banner_filename": "VARCHAR(255)",
        }
        for column_name, column_type in migrations.items():
            if column_name not in guild_columns:
                await conn.execute(
                    text(f"ALTER TABLE guild_settings ADD COLUMN {column_name} {column_type}")
                )

        participant_columns = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_columns("campaign_participants")
        )
        discord_column = next(
            (column for column in participant_columns if column["name"] == "discord_user_id"),
            None,
        )
        if discord_column is not None and not discord_column.get("nullable", True):
            if conn.dialect.name == "sqlite":
                # SQLite cannot directly drop NOT NULL. Rebuild the table with
                # the same primary key, unique constraints and foreign key.
                await conn.execute(text("DROP TABLE IF EXISTS campaign_participants_new"))
                await conn.execute(text("""
                    CREATE TABLE campaign_participants_new (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        campaign_id INTEGER NOT NULL,
                        guild_id BIGINT NOT NULL,
                        discord_user_id BIGINT NULL,
                        roblox_user_id BIGINT NOT NULL,
                        roblox_username VARCHAR(30) NOT NULL,
                        joined_at DATETIME NOT NULL,
                        CONSTRAINT uq_campaign_discord_participant
                            UNIQUE (campaign_id, discord_user_id),
                        CONSTRAINT uq_campaign_roblox_participant
                            UNIQUE (campaign_id, roblox_user_id),
                        FOREIGN KEY(campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
                    )
                """))
                await conn.execute(text("""
                    INSERT INTO campaign_participants_new
                        (id, campaign_id, guild_id, discord_user_id,
                         roblox_user_id, roblox_username, joined_at)
                    SELECT id, campaign_id, guild_id, discord_user_id,
                           roblox_user_id, roblox_username, joined_at
                    FROM campaign_participants
                """))
                await conn.execute(text("DROP TABLE campaign_participants"))
                await conn.execute(text(
                    "ALTER TABLE campaign_participants_new RENAME TO campaign_participants"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_campaign_participants_campaign_id "
                    "ON campaign_participants (campaign_id)"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_campaign_participants_guild_id "
                    "ON campaign_participants (guild_id)"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_campaign_participants_campaign "
                    "ON campaign_participants (campaign_id)"
                ))
            elif conn.dialect.name == "postgresql":
                await conn.execute(text(
                    "ALTER TABLE campaign_participants "
                    "ALTER COLUMN discord_user_id DROP NOT NULL"
                ))

    async def close(self) -> None:
        await self.engine.dispose()
