import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, func

from src.models import Category, Product


class CategoryCommands(commands.GroupCog, group_name="categoria", group_description="Gerencie as categorias da loja"):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="criar", description="Cria uma categoria na loja.")
    @app_commands.describe(nome="Nome da categoria", emoji="Emoji da categoria", descricao="Descrição curta")
    @app_commands.checks.has_permissions(administrator=True)
    async def create(
        self,
        interaction: discord.Interaction,
        nome: app_commands.Range[str, 1, 80],
        emoji: str = "🛍️",
        descricao: app_commands.Range[str, 0, 100] = "",
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(func.max(Category.position)).where(Category.guild_id == interaction.guild_id)
            )
            position = (result.scalar() or 0) + 1
            session.add(Category(
                guild_id=interaction.guild_id,
                name=nome.strip(),
                emoji=emoji[:32],
                description=descricao,
                position=position,
            ))
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                await interaction.response.send_message(
                    "Não foi possível criar. Talvez já exista uma categoria com esse nome.",
                    ephemeral=True,
                )
                return
        await interaction.response.send_message(f"Categoria {emoji} **{nome}** criada.", ephemeral=True)

    @app_commands.command(name="editar", description="Altera nome, emoji ou descrição de uma categoria.")
    @app_commands.describe(
        categoria_id="ID interno da categoria",
        nome="Novo nome",
        emoji="Novo emoji",
        descricao="Nova descrição",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def edit(
        self,
        interaction: discord.Interaction,
        categoria_id: int,
        nome: app_commands.Range[str, 1, 80] | None = None,
        emoji: str | None = None,
        descricao: app_commands.Range[str, 0, 100] | None = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.id == categoria_id, Category.guild_id == interaction.guild_id)
            )
            category = result.scalar_one_or_none()
            if category is None:
                await interaction.response.send_message("Categoria não encontrada neste servidor.", ephemeral=True)
                return
            if nome is not None:
                category.name = nome.strip()
            if emoji is not None:
                category.emoji = emoji[:32]
            if descricao is not None:
                category.description = descricao
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                await interaction.response.send_message(
                    "Não foi possível salvar. Verifique se o nome já existe.",
                    ephemeral=True,
                )
                return
        await interaction.response.send_message(
            "Categoria atualizada. Use `/loja atualizar` para atualizar o painel.",
            ephemeral=True,
        )

    @app_commands.command(name="excluir", description="Exclui uma categoria vazia.")
    @app_commands.describe(categoria_id="ID interno da categoria")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete(self, interaction: discord.Interaction, categoria_id: int):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.id == categoria_id, Category.guild_id == interaction.guild_id)
            )
            category = result.scalar_one_or_none()
            if category is None:
                await interaction.response.send_message("Categoria não encontrada.", ephemeral=True)
                return
            count_result = await session.execute(
                select(func.count(Product.id)).where(Product.category_id == categoria_id)
            )
            if (count_result.scalar() or 0) > 0:
                await interaction.response.send_message(
                    "Esta categoria ainda possui produtos. Mova ou exclua os produtos antes de removê-la.",
                    ephemeral=True,
                )
                return
            await session.delete(category)
            await session.commit()
        await interaction.response.send_message("Categoria excluída.", ephemeral=True)

    @app_commands.command(name="listar", description="Lista as categorias e seus IDs.")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_categories(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.guild_id == interaction.guild_id).order_by(Category.position)
            )
            categories = list(result.scalars().all())
        if not categories:
            text = "Nenhuma categoria cadastrada."
        else:
            text = "\n".join(
                f"`{c.id}` {c.emoji} **{c.name}** — {c.description or 'Sem descrição'}"
                for c in categories
            )
        await interaction.response.send_message(text[:1900], ephemeral=True)

    @app_commands.command(name="organizar", description="Define a posição de uma categoria no catálogo.")
    @app_commands.describe(categoria_id="ID interno da categoria", posicao="Posição desejada (1 aparece primeiro)")
    @app_commands.checks.has_permissions(administrator=True)
    async def organize(self, interaction: discord.Interaction, categoria_id: int, posicao: app_commands.Range[int, 1, 10000]):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.id == categoria_id, Category.guild_id == interaction.guild_id)
            )
            category = result.scalar_one_or_none()
            if category is None:
                await interaction.response.send_message("Categoria não encontrada.", ephemeral=True)
                return
            category.position = posicao
            await session.commit()
        await interaction.response.send_message(
            "Posição atualizada. Use `/loja atualizar` para atualizar o painel.",
            ephemeral=True,
        )

async def setup(bot):
    await bot.add_cog(CategoryCommands(bot))
