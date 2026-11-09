import asyncio
import logging

import discord
from discord.ext import commands, tasks
from sqlalchemy import select

from src.config import Settings
from src.database import Database
from src.logging_config import configure_logging
from src.models import GuildSettings, Product
from src.roblox.client import RobloxClient
from src.services.store import sync_product_from_roblox
from src.ui.views import create_panel_payload, create_panel_banner_file

log = logging.getLogger(__name__)


class RobloxStoreBot(commands.Bot):
    def __init__(self, settings: Settings):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.settings = settings
        self.db = Database(settings.database_url)
        self.roblox = RobloxClient(settings.roblox_timeout_seconds)

    async def setup_hook(self):
        await self.db.initialize()
        await self.roblox.start()

        # The catalog is administered through one private interactive panel.
        # The previous /loja, /categoria and /item command groups are no longer loaded.
        for extension in (
            "src.commands.catalog_config",
        ):
            await self.load_extension(extension)
            log.info("Extensão carregada: %s", extension)

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            # This dedicated bot now exposes only /configcatalogo.
            # Clear the old guild command groups before registering the new command set.
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Comandos sincronizados no servidor de desenvolvimento %s", self.settings.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Comandos globais sincronizados. A propagação pode demorar.")

        await self.restore_panel_views()
        self.refresh_loop.change_interval(minutes=self.settings.refresh_interval_minutes)
        self.refresh_loop.start()

    async def on_ready(self):
        log.info("Conectado como %s (%s)", self.user, self.user.id if self.user else "sem ID")

    async def restore_panel_views(self):
        async with self.db.sessions() as session:
            result = await session.execute(
                select(GuildSettings).where(
                    GuildSettings.panel_channel_id.is_not(None),
                    GuildSettings.panel_message_id.is_not(None),
                )
            )
            rows = list(result.scalars().all())

        for settings in rows:
            try:
                _, view = await create_panel_payload(self, settings.guild_id)
                self.add_view(view, message_id=settings.panel_message_id)
            except Exception:
                log.exception("Falha ao restaurar painel do servidor %s", settings.guild_id)

    async def refresh_guild_products(self, guild_id: int) -> dict[str, int]:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(Product).where(
                    Product.guild_id == guild_id,
                    Product.is_active.is_(True),
                )
            )
            products = list(result.scalars().all())
            updated = 0
            failed = 0

            for product in products:
                try:
                    data = await self.roblox.get_product(product.asset_id)
                    await sync_product_from_roblox(product, data)
                    updated += 1
                except Exception as exc:
                    failed += 1
                    log.warning(
                        "Falha ao atualizar produto %s do servidor %s: %s",
                        product.asset_id,
                        guild_id,
                        exc,
                    )
            await session.commit()
            return {"updated": updated, "failed": failed}

    async def update_guild_panel(self, guild_id: int) -> bool:
        async with self.db.sessions() as session:
            result = await session.execute(
                select(GuildSettings).where(GuildSettings.guild_id == guild_id)
            )
            settings = result.scalar_one_or_none()
            if not settings or not settings.panel_channel_id or not settings.panel_message_id:
                return False
            channel_id = settings.panel_channel_id
            message_id = settings.panel_message_id

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False

        if getattr(getattr(channel, "guild", None), "id", None) != guild_id:
            log.error(
                "Painel bloqueado por isolamento entre servidores: guild_id=%s channel_id=%s",
                guild_id,
                channel_id,
            )
            return False

        try:
            message = await channel.fetch_message(message_id)
            embed, view = await create_panel_payload(self, guild_id)
            banner_file = await create_panel_banner_file(self, guild_id)
            if banner_file and any(item.filename == banner_file.filename for item in message.attachments):
                banner_file.close()
                attachments = message.attachments
            else:
                attachments = [banner_file] if banner_file else []
            await message.edit(
                embed=embed,
                view=view,
                attachments=attachments,
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Não foi possível atualizar o painel do servidor %s", guild_id)
            return False

    @tasks.loop(minutes=30)
    async def refresh_loop(self):
        async with self.db.sessions() as session:
            result = await session.execute(
                select(GuildSettings.guild_id).where(
                    GuildSettings.panel_channel_id.is_not(None),
                    GuildSettings.panel_message_id.is_not(None),
                )
            )
            guild_ids = list(result.scalars().all())

        for guild_id in guild_ids:
            await self.refresh_guild_products(guild_id)
            await self.update_guild_panel(guild_id)

    @refresh_loop.before_loop
    async def before_refresh_loop(self):
        await self.wait_until_ready()

    async def close(self):
        if self.refresh_loop.is_running():
            self.refresh_loop.cancel()
        await self.roblox.close()
        await self.db.close()
        await super().close()


async def main():
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    bot = RobloxStoreBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
