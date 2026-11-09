import discord
from discord import app_commands
from discord.ext import commands

from src.services.store import get_settings
from src.ui.views import create_panel_payload, create_panel_banner_file


class StoreCommands(commands.GroupCog, group_name="loja", group_description="Administração da loja Roblox"):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="publicar", description="Publica o painel da loja em um canal.")
    @app_commands.describe(canal="Canal onde o painel será publicado")
    @app_commands.checks.has_permissions(administrator=True)
    async def publish(self, interaction: discord.Interaction, canal: discord.TextChannel):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando dentro de um servidor.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, view = await create_panel_payload(self.bot, interaction.guild_id)
        banner_file = await create_panel_banner_file(self.bot, interaction.guild_id)
        try:
            if banner_file:
                message = await canal.send(embed=embed, view=view, file=banner_file)
            else:
                message = await canal.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                "Não tenho permissão para enviar mensagens e embeds nesse canal.",
                ephemeral=True,
            )
            return

        async with self.bot.db.sessions() as session:
            settings = await get_settings(session, interaction.guild_id)
            settings.panel_channel_id = canal.id
            settings.panel_message_id = message.id
            await session.commit()
        await interaction.followup.send(f"Painel publicado em {canal.mention}.", ephemeral=True)

    @app_commands.command(name="atualizar", description="Atualiza os dados Roblox e o painel publicado.")
    @app_commands.checks.has_permissions(administrator=True)
    async def update(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando dentro de um servidor.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bot.refresh_guild_products(interaction.guild_id)
        updated = await self.bot.update_guild_panel(interaction.guild_id)
        await interaction.followup.send(
            f"Sincronização concluída. Produtos atualizados: **{result['updated']}**; "
            f"falhas: **{result['failed']}**. Painel: {'atualizado' if updated else 'não configurado ou indisponível'}.",
            ephemeral=True,
        )

    @app_commands.command(name="aparencia", description="Personaliza o título e a descrição do painel.")
    @app_commands.describe(titulo="Título exibido no painel", descricao="Texto abaixo do título")
    @app_commands.checks.has_permissions(administrator=True)
    async def appearance(
        self,
        interaction: discord.Interaction,
        titulo: app_commands.Range[str, 1, 100],
        descricao: app_commands.Range[str, 1, 500],
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando dentro de um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            settings = await get_settings(session, interaction.guild_id)
            settings.store_title = titulo
            settings.store_description = descricao
            await session.commit()
        await interaction.response.send_message(
            "A aparência foi salva. Use `/loja atualizar` para atualizar o painel publicado.",
            ephemeral=True,
        )

    @app_commands.command(name="cor", description="Altera a cor de destaque do painel.")
    @app_commands.describe(hexadecimal="Cor hexadecimal, por exemplo #5865F2")
    @app_commands.checks.has_permissions(administrator=True)
    async def color(self, interaction: discord.Interaction, hexadecimal: str):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando dentro de um servidor.", ephemeral=True)
            return
        value = hexadecimal.strip().removeprefix("#")
        if len(value) != 6:
            await interaction.response.send_message("Informe uma cor hexadecimal com 6 caracteres, por exemplo #5865F2.", ephemeral=True)
            return
        try:
            color_value = int(value, 16)
        except ValueError:
            await interaction.response.send_message("Cor hexadecimal inválida.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            settings = await get_settings(session, interaction.guild_id)
            settings.accent_color = color_value
            await session.commit()
        await interaction.response.send_message(
            "Cor salva. Use `/loja atualizar` para atualizar o painel publicado.",
            ephemeral=True,
        )

async def setup(bot):
    await bot.add_cog(StoreCommands(bot))
