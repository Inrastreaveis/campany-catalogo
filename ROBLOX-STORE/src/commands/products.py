import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from src.models import Category, Product
from src.services.store import sync_product_from_roblox


class ProductCommands(commands.GroupCog, group_name="item", group_description="Gerencie os produtos Roblox"):
    def __init__(self, bot):
        self.bot = bot

    async def category_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(
                    Category.guild_id == interaction.guild_id,
                    Category.name.ilike(f"%{current}%"),
                ).order_by(Category.position).limit(25)
            )
            categories = list(result.scalars().all())
        return [
            app_commands.Choice(name=f"{c.emoji} {c.name}"[:100], value=str(c.id))
            for c in categories
        ]

    @app_commands.command(name="adicionar", description="Adiciona um item Roblox usando o ID do catálogo.")
    @app_commands.describe(categoria="Escolha a categoria", asset_id="ID numérico do item Roblox")
    @app_commands.autocomplete(categoria=category_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    async def add(
        self,
        interaction: discord.Interaction,
        categoria: str,
        asset_id: app_commands.Range[int, 1, 9_007_199_254_740_991],
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with self.bot.db.sessions() as session:
            category_result = await session.execute(
                select(Category).where(
                    Category.id == int(categoria),
                    Category.guild_id == interaction.guild_id,
                )
            )
            category = category_result.scalar_one_or_none()
            if category is None:
                await interaction.followup.send(
                    "Categoria inválida. Crie uma categoria primeiro.",
                    ephemeral=True,
                )
                return

            duplicate_result = await session.execute(
                select(Product).where(
                    Product.guild_id == interaction.guild_id,
                    Product.asset_id == asset_id,
                )
            )
            if duplicate_result.scalar_one_or_none():
                await interaction.followup.send(
                    "Esse item já está cadastrado neste servidor.",
                    ephemeral=True,
                )
                return

            try:
                data = await self.bot.roblox.get_product(asset_id)
            except Exception as exc:
                await interaction.followup.send(
                    f"Não consegui buscar esse item no Roblox: {exc}",
                    ephemeral=True,
                )
                return

            product = Product(
                guild_id=interaction.guild_id,
                category_id=category.id,
                asset_id=asset_id,
                name=data["name"],
                description=data.get("description") or "",
                price_robux=data.get("price_robux"),
                favorite_count=data.get("favorite_count"),
                thumbnail_url=data.get("thumbnail_url"),
                asset_type_id=data.get("asset_type_id"),
            )
            session.add(product)
            await session.commit()

        favorites = (
            f"{data['favorite_count']:,}".replace(",", ".")
            if data.get("favorite_count") is not None else "indisponível"
        )
        price = (
            f"{data['price_robux']:,}".replace(",", ".") + " Robux"
            if data.get("price_robux") is not None else "indisponível"
        )
        embed = discord.Embed(title="Produto cadastrado", color=discord.Color.green())
        embed.add_field(name="Nome obtido do Roblox", value=data["name"], inline=False)
        embed.add_field(name="Preço", value=price, inline=True)
        embed.add_field(name="Favoritos", value=f"❤️ {favorites}", inline=True)
        embed.add_field(name="Categoria", value=f"{category.emoji} {category.name}", inline=True)
        embed.add_field(name="ID", value=str(asset_id), inline=True)
        if data.get("thumbnail_url"):
            embed.set_thumbnail(url=data["thumbnail_url"])
        warning = (
            "\n⚠️ O Roblox informa que este item está fora de venda."
            if data.get("is_off_sale") else None
        )
        await interaction.followup.send(embed=embed, content=warning, ephemeral=True)

    @app_commands.command(name="sincronizar", description="Atualiza nome, preço, imagem e favoritos de um item.")
    @app_commands.describe(asset_id="ID do item Roblox")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_one(self, interaction: discord.Interaction, asset_id: int):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Product).where(
                    Product.guild_id == interaction.guild_id,
                    Product.asset_id == asset_id,
                )
            )
            product = result.scalar_one_or_none()
            if product is None:
                await interaction.followup.send("Item não cadastrado neste servidor.", ephemeral=True)
                return
            try:
                data = await self.bot.roblox.get_product(asset_id)
            except Exception as exc:
                await interaction.followup.send(f"Falha ao sincronizar: {exc}", ephemeral=True)
                return
            await sync_product_from_roblox(product, data)
            await session.commit()
        await interaction.followup.send(
            f"Item **{data['name']}** sincronizado. Favoritos: "
            f"{data.get('favorite_count', 'indisponível')}. Use `/loja atualizar` para refletir no painel.",
            ephemeral=True,
        )

    @app_commands.command(name="editar", description="Edita o nome exibido ou ativa/desativa um item.")
    @app_commands.describe(
        asset_id="ID do item Roblox",
        nome_exibido="Nome personalizado (opcional)",
        ativo="Ativar ou desativar o item",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def edit(
        self,
        interaction: discord.Interaction,
        asset_id: int,
        nome_exibido: app_commands.Range[str, 1, 200] | None = None,
        ativo: bool | None = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Product).where(
                    Product.guild_id == interaction.guild_id,
                    Product.asset_id == asset_id,
                )
            )
            product = result.scalar_one_or_none()
            if product is None:
                await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
                return
            if nome_exibido is not None:
                product.custom_name = nome_exibido
            if ativo is not None:
                product.is_active = ativo
            await session.commit()
        await interaction.response.send_message(
            "Produto atualizado. Use `/loja atualizar` para atualizar o painel.",
            ephemeral=True,
        )

    @app_commands.command(name="remover", description="Remove um produto da loja.")
    @app_commands.describe(asset_id="ID do item Roblox")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove(self, interaction: discord.Interaction, asset_id: int):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Product).where(
                    Product.guild_id == interaction.guild_id,
                    Product.asset_id == asset_id,
                )
            )
            product = result.scalar_one_or_none()
            if product is None:
                await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
                return
            await session.delete(product)
            await session.commit()
        await interaction.response.send_message("Produto removido da loja.", ephemeral=True)

    @app_commands.command(name="mover", description="Move um produto para outra categoria.")
    @app_commands.describe(asset_id="ID do item Roblox", categoria="Nova categoria")
    @app_commands.autocomplete(categoria=category_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    async def move(self, interaction: discord.Interaction, asset_id: int, categoria: str):
        if not interaction.guild:
            await interaction.response.send_message("Use este comando em um servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            category_result = await session.execute(
                select(Category).where(
                    Category.id == int(categoria),
                    Category.guild_id == interaction.guild_id,
                )
            )
            target = category_result.scalar_one_or_none()
            if target is None:
                await interaction.response.send_message("Categoria de destino não encontrada.", ephemeral=True)
                return
            product_result = await session.execute(
                select(Product).where(
                    Product.guild_id == interaction.guild_id,
                    Product.asset_id == asset_id,
                )
            )
            product = product_result.scalar_one_or_none()
            if product is None:
                await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
                return
            product.category_id = target.id
            await session.commit()
        await interaction.response.send_message(
            f"Produto movido para {target.emoji} **{target.name}**. Use `/loja atualizar` para atualizar o painel.",
            ephemeral=True,
        )

async def setup(bot):
    await bot.add_cog(ProductCommands(bot))
