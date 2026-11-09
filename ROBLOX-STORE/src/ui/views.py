import io

import discord
from sqlalchemy import select

from src.models import Category, Product
from src.services.store import (
    get_categories, get_products, category_stats, store_stats, get_settings
)


def format_robux(value: int | None) -> str:
    return f"{value:,}".replace(",", ".") + " Robux" if value is not None else "Indisponível"


def build_store_embed(settings, categories, stats, total_products: int, total_favorites: int) -> discord.Embed:
    description = settings.store_description or "Escolha uma categoria para explorar nossos itens."
    embed = discord.Embed(
        title=settings.store_title or "ROBLOX STORE",
        description=(
            f"{description}\n\n"
            f"❤️ **Favoritos totais:** {total_favorites:,}".replace(",", ".")
            + f"\n🛍️ **Produtos ativos:** {total_products}"
        ),
        color=settings.accent_color or 0x5865F2,
    )
    if categories:
        lines = []
        for category in categories[:10]:
            count, favorites = stats.get(category.id, (0, 0))
            lines.append(
                f"{category.emoji or '🛍️'} **{category.name}** — {count} itens · ❤️ {favorites:,}".replace(",", ".")
            )
        if len(categories) > 10:
            lines.append(f"... e mais {len(categories) - 10} categorias. Clique em **Explorar catálogo**.")
        embed.add_field(name="Categorias", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Categorias", value="Nenhuma categoria cadastrada ainda.", inline=False)
    banner_data = getattr(settings, "panel_banner_data", None)
    banner_filename = getattr(settings, "panel_banner_filename", None)
    if banner_data and banner_filename:
        embed.set_image(url=f"attachment://{banner_filename}")
    elif getattr(settings, "panel_banner_url", None):
        embed.set_image(url=settings.panel_banner_url)
    embed.set_footer(
        text="Favoritos totais = soma dos contadores dos produtos ativos; não representa usuários únicos."
    )
    return embed


def build_product_embed(product: Product, category_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=product.display_name[:256],
        description=product.description[:3500] if product.description else "Item do catálogo Roblox.",
        color=0x5865F2,
        url=product.roblox_url,
    )
    embed.add_field(name="Preço", value=format_robux(product.price_robux), inline=True)
    favorites = f"{product.favorite_count:,}".replace(",", ".") if product.favorite_count is not None else "Indisponível"
    embed.add_field(name="Favoritos no Roblox", value=f"❤️ {favorites}", inline=True)
    embed.add_field(name="Categoria", value=category_name, inline=True)
    embed.add_field(name="ID do item", value=str(product.asset_id), inline=True)
    if product.last_synced_at:
        embed.set_footer(text=f"Última sincronização: {product.last_synced_at.strftime('%d/%m/%Y %H:%M UTC')}")
    if product.thumbnail_url:
        embed.set_thumbnail(url=product.thumbnail_url)
    return embed


class BuyLinkView(discord.ui.View):
    def __init__(self, product_url: str):
        super().__init__(timeout=180)
        self.add_item(discord.ui.Button(
            label="Comprar no Roblox",
            style=discord.ButtonStyle.link,
            url=product_url,
            emoji="🛒",
        ))


class FavoriteUsernameModal(discord.ui.Modal, title="Verificar favorito Roblox"):
    username = discord.ui.TextInput(
        label="Seu username Roblox",
        placeholder="Ex.: Builderman",
        min_length=3,
        max_length=20,
        required=True,
    )

    def __init__(self, bot, product: Product):
        super().__init__()
        self.bot = bot
        self.asset_id = product.asset_id
        self.asset_type_id = product.asset_type_id
        self.product_url = product.roblox_url

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.bot.roblox.verify_public_favorite(
                str(self.username.value).strip(), self.asset_id, self.asset_type_id
            )
        except Exception as exc:
            await interaction.followup.send(f"Não foi possível consultar essa conta: {exc}", ephemeral=True)
            return

        if result is True:
            await interaction.followup.send(
                "❤️ **Favorito encontrado!** O item aparece na lista pública de favoritos desse username.",
                view=BuyLinkView(self.product_url),
                ephemeral=True,
            )
        elif result is False:
            await interaction.followup.send(
                "Ainda não encontrei esse item nos favoritos públicos desse username. "
                "Confira se favoritou o item correto e tente novamente.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Não consegui confirmar com segurança. A lista de favoritos pode estar indisponível, "
                "privada ou ter mais páginas do que foi possível consultar. Tente novamente mais tarde.",
                ephemeral=True,
            )


class ProductDetailView(discord.ui.View):
    def __init__(self, bot, product: Product):
        super().__init__(timeout=300)
        self.bot = bot
        self.product = product
        self.add_item(discord.ui.Button(
            label="Abrir para favoritar",
            style=discord.ButtonStyle.link,
            url=product.roblox_url,
            emoji="❤️",
        ))
        self.add_item(discord.ui.Button(
            label="Comprar no Roblox",
            style=discord.ButtonStyle.link,
            url=product.roblox_url,
            emoji="🛒",
        ))

    @discord.ui.button(label="Já favoritei — verificar", style=discord.ButtonStyle.success, emoji="✅")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FavoriteUsernameModal(self.bot, self.product))


class ProductSelect(discord.ui.Select):
    def __init__(self, bot, products: list[Product], page: int, category_name: str):
        self.bot = bot
        self.category_name = category_name
        chunk = products[page * 25:page * 25 + 25]
        options = [
            discord.SelectOption(
                label=(product.display_name[:100] or f"Item {product.asset_id}"),
                value=str(product.id),
                description=(
                    f"{format_robux(product.price_robux)} · "
                    f"❤️ {product.favorite_count if product.favorite_count is not None else '?'}"
                )[:100],
                emoji="🛍️",
            )
            for product in chunk
        ]
        super().__init__(
            placeholder="Selecione um produto...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="store:product:select",
        )

    async def callback(self, interaction: discord.Interaction):
        product_id = int(self.values[0])
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Product).where(
                    Product.id == product_id,
                    Product.guild_id == interaction.guild_id,
                    Product.is_active.is_(True),
                )
            )
            product = result.scalar_one_or_none()
            if product is None:
                await interaction.response.send_message("Esse produto não está mais disponível.", ephemeral=True)
                return
            category_name = self.category_name
            await interaction.response.send_message(
                embed=build_product_embed(product, category_name),
                view=ProductDetailView(self.bot, product),
                ephemeral=True,
            )


class ProductListView(discord.ui.View):
    def __init__(self, bot, products: list[Product], category_name: str, page: int = 0):
        super().__init__(timeout=300)
        self.bot = bot
        self.products = products
        self.category_name = category_name
        self.page = page
        self.max_page = max(0, (len(products) - 1) // 25)
        if products:
            self.add_item(ProductSelect(bot, products, page, category_name))
        if self.max_page > 0:
            previous = discord.ui.Button(
                label="Anterior", style=discord.ButtonStyle.secondary,
                disabled=page <= 0, custom_id=f"store:products:prev:{page}"
            )
            following = discord.ui.Button(
                label="Próxima", style=discord.ButtonStyle.secondary,
                disabled=page >= self.max_page, custom_id=f"store:products:next:{page}"
            )
            previous.callback = self.previous_page
            following.callback = self.next_page
            self.add_item(previous)
            self.add_item(following)

    async def previous_page(self, interaction: discord.Interaction):
        await self.change_page(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction):
        await self.change_page(interaction, self.page + 1)

    async def change_page(self, interaction: discord.Interaction, page: int):
        view = ProductListView(self.bot, self.products, self.category_name, page)
        embed = discord.Embed(
            title=f"🛍️ {self.category_name}",
            description=f"Página {page + 1}/{self.max_page + 1} · {len(self.products)} produtos",
            color=0x5865F2,
        )
        await interaction.response.edit_message(embed=embed, view=view)


class CategorySelect(discord.ui.Select):
    def __init__(self, bot, categories: list[Category], stats: dict[int, tuple[int, int]], page: int):
        self.bot = bot
        chunk = categories[page * 25:page * 25 + 25]
        options = []
        for category in chunk:
            count, favorites = stats.get(category.id, (0, 0))
            options.append(discord.SelectOption(
                label=category.name[:100],
                value=str(category.id),
                description=f"{count} itens · ❤️ {favorites:,}".replace(",", ".")[:100],
                emoji=category.emoji or "🛍️",
            ))
        super().__init__(
            placeholder="Escolha uma categoria...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="store:category:select",
        )

    async def callback(self, interaction: discord.Interaction):
        category_id = int(self.values[0])
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.id == category_id, Category.guild_id == interaction.guild_id)
            )
            category = result.scalar_one_or_none()
            if category is None:
                await interaction.response.send_message("Categoria não encontrada.", ephemeral=True)
                return
            products = await get_products(session, interaction.guild_id, category_id)
            if not products:
                await interaction.response.send_message(
                    f"A categoria **{category.name}** ainda não possui produtos ativos.",
                    ephemeral=True,
                )
                return
            embed = discord.Embed(
                title=f"{category.emoji} {category.name}",
                description=f"{len(products)} produtos disponíveis. Selecione um item para ver os detalhes.",
                color=0x5865F2,
            )
            await interaction.response.send_message(
                embed=embed,
                view=ProductListView(self.bot, products, category.name),
                ephemeral=True,
            )


class CategoryListView(discord.ui.View):
    def __init__(self, bot, categories: list[Category], stats: dict[int, tuple[int, int]], page: int = 0):
        super().__init__(timeout=300)
        self.bot = bot
        self.categories = categories
        self.stats = stats
        self.page = page
        self.max_page = max(0, (len(categories) - 1) // 25)
        if categories:
            self.add_item(CategorySelect(bot, categories, stats, page))
        if self.max_page > 0:
            previous = discord.ui.Button(
                label="Anterior", style=discord.ButtonStyle.secondary,
                disabled=page <= 0, custom_id=f"store:categories:prev:{page}"
            )
            following = discord.ui.Button(
                label="Próxima", style=discord.ButtonStyle.secondary,
                disabled=page >= self.max_page, custom_id=f"store:categories:next:{page}"
            )
            previous.callback = self.previous_page
            following.callback = self.next_page
            self.add_item(previous)
            self.add_item(following)

    async def previous_page(self, interaction: discord.Interaction):
        await self.change_page(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction):
        await self.change_page(interaction, self.page + 1)

    async def change_page(self, interaction: discord.Interaction, page: int):
        await interaction.response.edit_message(
            content=f"Escolha uma categoria — página {page + 1}/{self.max_page + 1}.",
            view=CategoryListView(self.bot, self.categories, self.stats, page),
        )


class StorePanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Explorar catálogo",
        style=discord.ButtonStyle.primary,
        emoji="🛍️",
        custom_id="roblox_store:open_catalog",
    )
    async def open_catalog(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild_id:
            await interaction.response.send_message("Abra a loja dentro do servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            categories = await get_categories(session, interaction.guild_id)
            stats = {}
            for category in categories:
                stats[category.id] = await category_stats(session, interaction.guild_id, category.id)
        if not categories:
            await interaction.response.send_message(
                "A loja ainda não possui categorias. Peça a um administrador para configurar o catálogo.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Escolha uma categoria:",
            view=CategoryListView(self.bot, categories, stats),
            ephemeral=True,
        )


async def create_panel_banner_file(bot, guild_id: int):
    """Return a fresh discord.File for the persisted panel banner, if configured."""
    async with bot.db.sessions() as session:
        settings = await get_settings(session, guild_id)
        data = settings.panel_banner_data
        filename = settings.panel_banner_filename
    if not data or not filename:
        return None
    return discord.File(io.BytesIO(bytes(data)), filename=filename)


async def create_panel_payload(bot, guild_id: int):
    async with bot.db.sessions() as session:
        categories = await get_categories(session, guild_id)
        stats = {}
        for category in categories:
            stats[category.id] = await category_stats(session, guild_id, category.id)
        settings = await get_settings(session, guild_id)
        total_products, total_favorites = await store_stats(session, guild_id)
        embed = build_store_embed(settings, categories, stats, total_products, total_favorites)
    return embed, StorePanelView(bot)
