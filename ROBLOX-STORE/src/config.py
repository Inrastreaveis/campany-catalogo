from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


def normalize_database_url(value: str) -> str:
    value = value.strip()
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    if value.startswith("sqlite:///") and not value.startswith("sqlite+aiosqlite:///"):
        return value.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return value


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_url: str
    dev_guild_id: int | None
    refresh_interval_minutes: int
    log_level: str
    roblox_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "COLOQUE_SEU_TOKEN_AQUI":
            raise RuntimeError("Configure DISCORD_TOKEN no .env ou nas variáveis do Railway.")
        raw_db = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./roblox_store.db")
        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        return cls(
            discord_token=token,
            database_url=normalize_database_url(raw_db),
            dev_guild_id=int(dev_guild) if dev_guild else None,
            refresh_interval_minutes=max(5, int(os.getenv("REFRESH_INTERVAL_MINUTES", "30"))),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            roblox_timeout_seconds=max(5, int(os.getenv("ROBLOX_TIMEOUT_SECONDS", "15"))),
        )
