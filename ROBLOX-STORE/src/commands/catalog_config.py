from __future__ import annotations

import logging
from uuid import uuid4
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.models import Category, Product
from src.security import require_admin_context
from src.services.store import (
    get_categories,
    get_settings,
    store_stats,
    sync_product_from_roblox,
)
from src.ui.views import create_panel_payload, create_panel_banner_file, format_robux

log = logging.getLogger(__name__)
VIEW_TIMEOUT = 900
PAGE_SIZE = 25


class AdminView(discord.ui.View):
    """Base for private configuration views: only the invoking administrator can use them."""

    def __init__(self, bot, owner_id: int, guild_id: int, timeout: int = VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"painel:{self.__class__.__name__}",
        )


async def dashboard_embed(bot, guild_id: int) -> discord.Embed:
    async with bot.db.sessions() as session:
        categories = await get_categories(session, guild_id)
        products_count, favorites = await store_stats(session, guild_id)
        settings = await get_settings(session, guild_id)
        title = settings.store_title or "ROBLOX STORE"
        color = settings.accent_color or 0x5865F2
    embed = discord.Embed(
        title="⚙️ Configuração do catálogo",
        description=(
            f"Administração de **{title}**\n\n"
            "Escolha uma área abaixo. Todas as alterações são feitas por botões, menus e formulários."
        ),
        color=color,
    )
    embed.add_field(name="Categorias", value=str(len(categories)), inline=True)
    embed.add_field(name="Produtos ativos", value=str(products_count), inline=True)
    embed.add_field(name="Favoritos somados", value=f"❤️ {favorites:,}".replace(",", "."), inline=True)
    embed.add_field(
        name="Banner do painel",
        value="🖼️ Configurado" if (getattr(settings, "panel_banner_data", None) or getattr(settings, "panel_banner_url", None)) else "Sem banner",
        inline=True,
    )
    embed.set_footer(text="Painel privado • somente administradores • alterações salvas no banco de dados")
    return embed


async def refresh_public_panel(bot, guild_id: int) -> bool:
    try:
        return await bot.update_guild_panel(guild_id)
    except Exception:
        log.exception("Falha ao atualizar painel público do servidor %s", guild_id)
        return False


async def load_categories(bot, guild_id: int) -> list[Category]:
    async with bot.db.sessions() as session:
        return await get_categories(session, guild_id)


async def load_products(bot, guild_id: int) -> list[Product]:
    async with bot.db.sessions() as session:
        result = await session.execute(
            select(Product)
            .where(Product.guild_id == guild_id)
            .order_by(Product.name, Product.asset_id)
        )
        return list(result.scalars().all())


async def get_product_with_category(bot, guild_id: int, product_id: int):
    async with bot.db.sessions() as session:
        result = await session.execute(
            select(Product, Category.name)
            .join(Category, Product.category_id == Category.id)
            .where(Product.guild_id == guild_id, Product.id == product_id, Category.guild_id == guild_id)
        )
        return result.one_or_none()


def product_admin_embed(product: Product, category_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🛍️ {product.display_name}"[:256],
        description=(product.description or "Sem descrição cadastrada.")[:3000],
        color=0x5865F2,
        url=product.roblox_url,
    )
    embed.add_field(name="ID Roblox", value=str(product.asset_id), inline=True)
    embed.add_field(name="Preço", value=format_robux(product.price_robux), inline=True)
    favorites = (
        f"{product.favorite_count:,}".replace(",", ".")
        if product.favorite_count is not None else "Indisponível"
    )
    embed.add_field(name="Favoritos", value=f"❤️ {favorites}", inline=True)
    embed.add_field(name="Categoria", value=category_name, inline=True)
    embed.add_field(name="Status", value="🟢 Ativo" if product.is_active else "⚫ Inativo", inline=True)
    if product.thumbnail_url:
        embed.set_thumbnail(url=product.thumbnail_url)
    return embed


class DashboardView(AdminView):
    @discord.ui.button(label="Categorias", emoji="🗂️", style=discord.ButtonStyle.primary, row=0)
    async def categories(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="**Gerenciamento de categorias**\nCrie, edite, organize ou remova as categorias do catálogo.",
            embed=None,
            view=CategoryMenuView(self.bot, self.owner_id, self.guild_id),
        )

    @discord.ui.button(label="Produtos", emoji="🛍️", style=discord.ButtonStyle.primary, row=0)
    async def products(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="**Gerenciamento de produtos**\nAdicione pelo ID do Roblox ou selecione um produto cadastrado para editar.",
            embed=None,
            view=ProductMenuView(self.bot, self.owner_id, self.guild_id),
        )

    @discord.ui.button(label="Personalização", emoji="🎨", style=discord.ButtonStyle.secondary, row=0)
    async def appearance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="**Personalização do painel público**\nAltere texto, cor e imagem de banner exibidos para os clientes.",
            embed=None,
            view=PersonalizationMenuView(self.bot, self.owner_id, self.guild_id),
        )

    @discord.ui.button(label="Publicar painel", emoji="📢", style=discord.ButtonStyle.secondary, row=1)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Selecione o canal onde o painel público da loja deve ser publicado ou atualizado.",
            embed=None,
            view=PublishChannelView(self.bot, self.owner_id, self.guild_id),
        )

    @discord.ui.button(label="Sincronizar Roblox", emoji="🔄", style=discord.ButtonStyle.success, row=1)
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bot.refresh_guild_products(self.guild_id)
        panel_updated = await refresh_public_panel(self.bot, self.guild_id)
        await interaction.followup.send(
            f"Sincronização concluída.\n"
            f"✅ Atualizados: **{result['updated']}**\n"
            f"⚠️ Falhas: **{result['failed']}**\n"
            f"Painel público: **{'atualizado' if panel_updated else 'não publicado ou indisponível'}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Estatísticas", emoji="📊", style=discord.ButtonStyle.secondary, row=1)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            categories = await get_categories(session, self.guild_id)
            total_products, total_favorites = await store_stats(session, self.guild_id)
            lines = []
            for category in categories[:20]:
                count_result = await session.execute(
                    select(func.count(Product.id)).where(
                        Product.guild_id == self.guild_id,
                        Product.category_id == category.id,
                        Product.is_active.is_(True),
                    )
                )
                lines.append(f"{category.emoji} **{category.name}** — {count_result.scalar() or 0} produtos")
        embed = discord.Embed(title="📊 Estatísticas do catálogo", color=0x5865F2)
        embed.add_field(name="Categorias", value=str(len(categories)), inline=True)
        embed.add_field(name="Produtos ativos", value=str(total_products), inline=True)
        embed.add_field(
            name="Favoritos somados",
            value=f"❤️ {total_favorites:,}".replace(",", "."),
            inline=True,
        )
        embed.add_field(name="Produtos por categoria", value="\n".join(lines) or "Nenhuma categoria.", inline=False)
        embed.set_footer(text="Favoritos somados por produto; não representa usuários únicos.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fechar", emoji="🔒", style=discord.ButtonStyle.danger, row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Painel de configuração fechado.", embed=None, view=None)


class CategoryMenuView(AdminView):
    @discord.ui.button(label="Adicionar categoria", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCategoryModal(self.bot, self.owner_id, self.guild_id))

    @discord.ui.button(label="Editar categoria", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_category_picker(interaction, self.bot, self.owner_id, self.guild_id, "edit")

    @discord.ui.button(label="Excluir categoria", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_category_picker(interaction, self.bot, self.owner_id, self.guild_id, "delete")

    @discord.ui.button(label="Organizar ordem", emoji="↕️", style=discord.ButtonStyle.secondary, row=1)
    async def organize(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_category_picker(interaction, self.bot, self.owner_id, self.guild_id, "organize")

    @discord.ui.button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=await dashboard_embed(self.bot, self.guild_id),
            view=DashboardView(self.bot, self.owner_id, self.guild_id),
        )


class ProductMenuView(AdminView):
    @discord.ui.button(label="Adicionar produto", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ProductAddModal(self.bot, self.owner_id, self.guild_id))

    @discord.ui.button(label="Editar / gerenciar produtos", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_product_picker(interaction, self.bot, self.owner_id, self.guild_id)

    @discord.ui.button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=await dashboard_embed(self.bot, self.guild_id),
            view=DashboardView(self.bot, self.owner_id, self.guild_id),
        )


async def show_category_picker(interaction, bot, owner_id: int, guild_id: int, action: str, page: int = 0):
    categories = await load_categories(bot, guild_id)
    if not categories:
        await interaction.response.send_message("Ainda não existem categorias cadastradas.", ephemeral=True)
        return
    await interaction.response.edit_message(
        content=f"Selecione a categoria que deseja **{'editar' if action == 'edit' else 'excluir' if action == 'delete' else 'reordenar'}**.",
        embed=None,
        view=CategoryPickerView(bot, owner_id, guild_id, categories, action, page),
    )


class CategoryPickerSelect(discord.ui.Select):
    def __init__(self, view: "CategoryPickerView"):
        self.parent_view = view
        start = view.page * PAGE_SIZE
        chunk = view.categories[start:start + PAGE_SIZE]
        options = [
            discord.SelectOption(
                label=category.name[:100],
                value=str(category.id),
                description=(category.description or f"Posição {category.position}")[:100],
                emoji=category.emoji or "🛍️",
            )
            for category in chunk
        ]
        super().__init__(placeholder="Selecione uma categoria...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category_id = int(self.values[0])
        if self.parent_view.action == "edit":
            async with self.parent_view.bot.db.sessions() as session:
                result = await session.execute(
                    select(Category).where(Category.id == category_id, Category.guild_id == self.parent_view.guild_id)
                )
                category = result.scalar_one_or_none()
                if category is None:
                    await interaction.response.send_message("Categoria não encontrada.", ephemeral=True)
                    return
                defaults = {"name": category.name, "emoji": category.emoji, "description": category.description}
            await interaction.response.send_modal(
                EditCategoryModal(self.parent_view.bot, self.parent_view.owner_id, self.parent_view.guild_id, category_id, defaults)
            )
        elif self.parent_view.action == "delete":
            await interaction.response.edit_message(
                content="Confirme a exclusão desta categoria. Categorias com produtos cadastrados não podem ser excluídas.",
                embed=None,
                view=ConfirmCategoryDeleteView(
                    self.parent_view.bot, self.parent_view.owner_id, self.parent_view.guild_id, category_id
                ),
            )
        else:
            await interaction.response.send_modal(
                CategoryPositionModal(
                    self.parent_view.bot,
                    self.parent_view.owner_id,
                    self.parent_view.guild_id,
                    category_id,
                )
            )


class CategoryPickerView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int, categories: list[Category], action: str, page: int = 0):
        super().__init__(bot, owner_id, guild_id)
        self.categories = categories
        self.action = action
        self.page = max(0, min(page, (len(categories) - 1) // PAGE_SIZE))
        self.add_item(CategoryPickerSelect(self))
        self.add_item(discord.ui.Button(
            label="Anterior", style=discord.ButtonStyle.secondary, disabled=self.page == 0,
            row=1, custom_id=f"admin:cat:prev:{self.page}",
        ))
        self.children[-1].callback = self.previous_page
        self.add_item(discord.ui.Button(
            label="Próxima", style=discord.ButtonStyle.secondary,
            disabled=(self.page + 1) * PAGE_SIZE >= len(categories),
            row=1, custom_id=f"admin:cat:next:{self.page}",
        ))
        self.children[-1].callback = self.next_page
        back = discord.ui.Button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def previous_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=CategoryPickerView(self.bot, self.owner_id, self.guild_id, self.categories, self.action, self.page - 1)
        )

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=CategoryPickerView(self.bot, self.owner_id, self.guild_id, self.categories, self.action, self.page + 1)
        )

    async def back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="**Gerenciamento de categorias**",
            view=CategoryMenuView(self.bot, self.owner_id, self.guild_id),
        )


class AddCategoryModal(discord.ui.Modal, title="Adicionar categoria"):
    name = discord.ui.TextInput(label="Nome da categoria", placeholder="Ex.: UGC", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji", placeholder="🛍️", default="🛍️", required=False, max_length=32)
    description = discord.ui.TextInput(
        label="Descrição curta", placeholder="Descrição exibida na configuração", required=False, max_length=100
    )

    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"formulario:{self.__class__.__name__}",
        ):
            return
        async with self.bot.db.sessions() as session:
            max_position = await session.scalar(
                select(func.max(Category.position)).where(Category.guild_id == self.guild_id)
            )
            category = Category(
                guild_id=self.guild_id,
                name=str(self.name.value).strip(),
                emoji=str(self.emoji.value).strip() or "🛍️",
                description=str(self.description.value).strip(),
                position=(max_position or 0) + 1,
            )
            session.add(category)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await interaction.response.send_message("Já existe uma categoria com esse nome.", ephemeral=True)
                return
        panel_updated = await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.send_message(
            f"Categoria **{self.name.value}** criada. "
            f"{'Painel público atualizado.' if panel_updated else 'O painel público ainda não foi publicado.'}",
            ephemeral=True,
        )


class EditCategoryModal(discord.ui.Modal, title="Editar categoria"):
    name = discord.ui.TextInput(label="Nome", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji", max_length=32, required=False)
    description = discord.ui.TextInput(label="Descrição", max_length=100, required=False)

    def __init__(self, bot, owner_id: int, guild_id: int, category_id: int, defaults: dict[str, str]):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id, self.category_id = bot, owner_id, guild_id, category_id
        self.name.default = defaults["name"]
        self.emoji.default = defaults["emoji"]
        self.description.default = defaults["description"] or ""

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"formulario:{self.__class__.__name__}",
        ):
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.id == self.category_id, Category.guild_id == self.guild_id)
            )
            category = result.scalar_one_or_none()
            if category is None:
                await interaction.response.send_message("Categoria não encontrada.", ephemeral=True)
                return
            category.name = str(self.name.value).strip()
            category.emoji = str(self.emoji.value).strip() or "🛍️"
            category.description = str(self.description.value).strip()
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await interaction.response.send_message("Já existe outra categoria com esse nome.", ephemeral=True)
                return
        panel_updated = await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.send_message(
            f"Categoria atualizada. {'Painel público atualizado.' if panel_updated else 'Publique o painel para exibir as alterações.'}",
            ephemeral=True,
        )


class CategoryPositionModal(discord.ui.Modal, title="Organizar categoria"):
    position = discord.ui.TextInput(label="Nova posição", placeholder="1 = primeira categoria", max_length=5)

    def __init__(self, bot, owner_id: int, guild_id: int, category_id: int):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id, self.category_id = bot, owner_id, guild_id, category_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"formulario:{self.__class__.__name__}",
        ):
            return
        try:
            position = int(str(self.position.value).strip())
            if position < 1 or position > 10000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Informe uma posição entre 1 e 10000.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.id == self.category_id, Category.guild_id == self.guild_id)
            )
            category = result.scalar_one_or_none()
            if category is None:
                await interaction.response.send_message("Categoria não encontrada.", ephemeral=True)
                return
            category.position = position
            await session.commit()
        await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.send_message("Ordem da categoria atualizada.", ephemeral=True)


class ConfirmCategoryDeleteView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int, category_id: int):
        super().__init__(bot, owner_id, guild_id)
        self.category_id = category_id

    @discord.ui.button(label="Confirmar exclusão", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(Category).where(Category.id == self.category_id, Category.guild_id == self.guild_id)
            )
            category = result.scalar_one_or_none()
            if category is None:
                await interaction.response.edit_message(content="Categoria não encontrada.", view=None)
                return
            count = await session.scalar(select(func.count(Product.id)).where(
                Product.category_id == self.category_id, Product.guild_id == self.guild_id
            ))
            if count:
                await interaction.response.edit_message(
                    content=f"Não é possível excluir **{category.name}**: ela ainda possui {count} produto(s). Mova ou remova os produtos primeiro.",
                    view=None,
                )
                return
            name = category.name
            await session.delete(category)
            await session.commit()
        await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.edit_message(content=f"Categoria **{name}** excluída.", view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Exclusão cancelada.", view=None)


class ProductAddModal(discord.ui.Modal, title="Adicionar item Roblox"):
    asset_id = discord.ui.TextInput(
        label="ID do item no catálogo Roblox",
        placeholder="Ex.: 131315468236907",
        min_length=1,
        max_length=20,
    )

    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"formulario:{self.__class__.__name__}",
        ):
            return
        try:
            asset_id = int(str(self.asset_id.value).strip())
            if asset_id < 1 or asset_id > 9_223_372_036_854_775_807:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Informe um ID numérico válido do Roblox.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.db.sessions() as session:
            duplicate = await session.scalar(
                select(Product.id).where(Product.guild_id == self.guild_id, Product.asset_id == asset_id)
            )
            categories = await get_categories(session, self.guild_id)
        if duplicate:
            await interaction.followup.send("Esse item já está cadastrado neste servidor.", ephemeral=True)
            return
        if not categories:
            await interaction.followup.send("Crie uma categoria antes de adicionar produtos.", ephemeral=True)
            return
        try:
            data = await self.bot.roblox.get_product(asset_id)
        except Exception as exc:
            log.warning("Falha ao buscar ativo Roblox %s: %s", asset_id, exc)
            await interaction.followup.send(f"Não consegui consultar esse item no Roblox: {exc}", ephemeral=True)
            return
        embed = discord.Embed(
            title="Produto encontrado no Roblox",
            description=f"**{data['name']}**\n{data.get('description') or 'Sem descrição.'}"[:3500],
            color=0x5865F2,
            url=f"https://www.roblox.com/catalog/{asset_id}",
        )
        embed.add_field(name="Preço", value=format_robux(data.get("price_robux")), inline=True)
        favorites = data.get("favorite_count")
        embed.add_field(
            name="Favoritos",
            value=f"❤️ {favorites:,}".replace(",", ".") if favorites is not None else "Indisponível",
            inline=True,
        )
        if data.get("thumbnail_url"):
            embed.set_thumbnail(url=data["thumbnail_url"])
        if data.get("is_off_sale"):
            embed.add_field(name="Atenção", value="Este item está fora de venda no Roblox.", inline=False)
        await interaction.followup.send(
            content="Escolha a categoria em que este produto será cadastrado:",
            embed=embed,
            view=AssignProductCategoryView(self.bot, self.owner_id, self.guild_id, data, categories),
            ephemeral=True,
        )


class AssignCategorySelect(discord.ui.Select):
    def __init__(self, view: "AssignProductCategoryView"):
        self.parent_view = view
        start = view.page * PAGE_SIZE
        chunk = view.categories[start:start + PAGE_SIZE]
        options = [
            discord.SelectOption(
                label=category.name[:100],
                value=str(category.id),
                description=(category.description or "Categoria do catálogo")[:100],
                emoji=category.emoji or "🛍️",
            )
            for category in chunk
        ]
        super().__init__(placeholder="Escolha uma categoria...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category_id = int(self.values[0])
        async with self.parent_view.bot.db.sessions() as session:
            category = await session.scalar(
                select(Category).where(Category.id == category_id, Category.guild_id == self.parent_view.guild_id)
            )
            if category is None:
                await interaction.response.send_message("Categoria não encontrada.", ephemeral=True)
                return
            duplicate = await session.scalar(
                select(Product.id).where(
                    Product.guild_id == self.parent_view.guild_id,
                    Product.asset_id == int(self.parent_view.data["asset_id"]),
                )
            )
            if duplicate:
                await interaction.response.edit_message(content="Esse item já foi cadastrado.", embed=None, view=None)
                return
            product = Product(
                guild_id=self.parent_view.guild_id,
                category_id=category_id,
                asset_id=int(self.parent_view.data["asset_id"]),
                name=self.parent_view.data["name"],
                description=self.parent_view.data.get("description") or "",
                price_robux=self.parent_view.data.get("price_robux"),
                favorite_count=self.parent_view.data.get("favorite_count"),
                thumbnail_url=self.parent_view.data.get("thumbnail_url"),
                asset_type_id=self.parent_view.data.get("asset_type_id"),
            )
            session.add(product)
            await session.commit()
            product_name = product.name
        await refresh_public_panel(self.parent_view.bot, self.parent_view.guild_id)
        await interaction.response.edit_message(
            content=f"✅ Produto **{product_name}** adicionado à categoria **{category.name}**.",
            embed=None,
            view=None,
        )


class AssignProductCategoryView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int, data: dict[str, Any], categories: list[Category], page: int = 0):
        super().__init__(bot, owner_id, guild_id)
        self.data = data
        self.categories = categories
        self.page = max(0, min(page, (len(categories) - 1) // PAGE_SIZE))
        self.add_item(AssignCategorySelect(self))
        if len(categories) > PAGE_SIZE:
            previous = discord.ui.Button(label="Anterior", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
            previous.callback = self.previous_page
            self.add_item(previous)
            following = discord.ui.Button(
                label="Próxima", style=discord.ButtonStyle.secondary,
                disabled=(self.page + 1) * PAGE_SIZE >= len(categories), row=1,
            )
            following.callback = self.next_page
            self.add_item(following)
        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self.cancel
        self.add_item(cancel)

    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Cadastro de produto cancelado.", embed=None, view=None)

    async def previous_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=AssignProductCategoryView(self.bot, self.owner_id, self.guild_id, self.data, self.categories, self.page - 1)
        )

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=AssignProductCategoryView(self.bot, self.owner_id, self.guild_id, self.data, self.categories, self.page + 1)
        )


async def show_product_picker(interaction, bot, owner_id: int, guild_id: int, page: int = 0):
    products = await load_products(bot, guild_id)
    if not products:
        await interaction.response.send_message("Ainda não existem produtos cadastrados.", ephemeral=True)
        return
    await interaction.response.edit_message(
        content="Selecione o produto que deseja administrar:",
        embed=None,
        view=ProductPickerView(bot, owner_id, guild_id, products, page),
    )


class ProductPickerSelect(discord.ui.Select):
    def __init__(self, view: "ProductPickerView"):
        self.parent_view = view
        start = view.page * PAGE_SIZE
        chunk = view.products[start:start + PAGE_SIZE]
        options = []
        for product in chunk:
            state = "Ativo" if product.is_active else "Inativo"
            price = format_robux(product.price_robux)
            options.append(discord.SelectOption(
                label=product.display_name[:100],
                value=str(product.id),
                description=f"{price} • {state} • ID {product.asset_id}"[:100],
                emoji="🛍️",
            ))
        super().__init__(placeholder="Selecione um produto...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        product_id = int(self.values[0])
        row = await get_product_with_category(self.parent_view.bot, self.parent_view.guild_id, product_id)
        if not row:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        product, category_name = row
        await interaction.response.edit_message(
            content="**Gerenciar produto**\nEscolha a ação desejada:",
            embed=product_admin_embed(product, category_name),
            view=ProductActionsView(
                self.parent_view.bot,
                self.parent_view.owner_id,
                self.parent_view.guild_id,
                product.id,
                product.is_active,
            ),
        )


class ProductPickerView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int, products: list[Product], page: int = 0):
        super().__init__(bot, owner_id, guild_id)
        self.products = products
        self.page = max(0, min(page, (len(products) - 1) // PAGE_SIZE))
        self.add_item(ProductPickerSelect(self))
        if len(products) > PAGE_SIZE:
            previous = discord.ui.Button(label="Anterior", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
            previous.callback = self.previous_page
            self.add_item(previous)
            following = discord.ui.Button(
                label="Próxima", style=discord.ButtonStyle.secondary,
                disabled=(self.page + 1) * PAGE_SIZE >= len(products), row=1,
            )
            following.callback = self.next_page
            self.add_item(following)
        back = discord.ui.Button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def previous_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=ProductPickerView(self.bot, self.owner_id, self.guild_id, self.products, self.page - 1))

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=ProductPickerView(self.bot, self.owner_id, self.guild_id, self.products, self.page + 1))

    async def back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="**Gerenciamento de produtos**",
            embed=None,
            view=ProductMenuView(self.bot, self.owner_id, self.guild_id),
        )


class ProductActionsView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int, product_id: int, is_active: bool):
        super().__init__(bot, owner_id, guild_id)
        self.product_id = product_id
        self.is_active = is_active

    @discord.ui.button(label="Renomear", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await get_product_with_category(self.bot, self.guild_id, self.product_id)
        if not row:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        product, _ = row
        await interaction.response.send_modal(
            RenameProductModal(self.bot, self.owner_id, self.guild_id, self.product_id, product.custom_name or "")
        )

    @discord.ui.button(label="Mover categoria", emoji="📂", style=discord.ButtonStyle.secondary, row=0)
    async def move(self, interaction: discord.Interaction, button: discord.ui.Button):
        categories = await load_categories(self.bot, self.guild_id)
        if not categories:
            await interaction.response.send_message("Não existem categorias cadastradas.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="Selecione a nova categoria do produto:",
            embed=None,
            view=MoveProductCategoryView(self.bot, self.owner_id, self.guild_id, self.product_id, categories),
        )

    @discord.ui.button(label="Alternar status", emoji="⏯️", style=discord.ButtonStyle.secondary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            product = await session.scalar(
                select(Product).where(Product.id == self.product_id, Product.guild_id == self.guild_id)
            )
            if product is None:
                await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
                return
            product.is_active = not product.is_active
            is_active = product.is_active
            await session.commit()
        await refresh_public_panel(self.bot, self.guild_id)
        row = await get_product_with_category(self.bot, self.guild_id, self.product_id)
        product, category_name = row
        await interaction.response.edit_message(
            content=f"Produto {'ativado' if is_active else 'desativado'}.",
            embed=product_admin_embed(product, category_name),
            view=ProductActionsView(self.bot, self.owner_id, self.guild_id, self.product_id, is_active),
        )

    @discord.ui.button(label="Sincronizar", emoji="🔄", style=discord.ButtonStyle.success, row=1)
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.db.sessions() as session:
            product = await session.scalar(
                select(Product).where(Product.id == self.product_id, Product.guild_id == self.guild_id)
            )
            if product is None:
                await interaction.followup.send("Produto não encontrado.", ephemeral=True)
                return
            try:
                data = await self.bot.roblox.get_product(product.asset_id)
            except Exception as exc:
                await interaction.followup.send(f"Falha ao consultar o Roblox: {exc}", ephemeral=True)
                return
            await sync_product_from_roblox(product, data)
            name = product.display_name
            await session.commit()
        await refresh_public_panel(self.bot, self.guild_id)
        await interaction.followup.send(f"Produto **{name}** sincronizado com o Roblox.", ephemeral=True)

    @discord.ui.button(label="Excluir", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Tem certeza de que deseja remover este produto do catálogo?",
            embed=None,
            view=ConfirmProductDeleteView(self.bot, self.owner_id, self.guild_id, self.product_id),
        )

    @discord.ui.button(label="Voltar à lista", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_product_picker(interaction, self.bot, self.owner_id, self.guild_id)


class RenameProductModal(discord.ui.Modal, title="Renomear produto"):
    name = discord.ui.TextInput(label="Nome personalizado", placeholder="Deixe em branco para usar o nome do Roblox", required=False, max_length=200)

    def __init__(self, bot, owner_id: int, guild_id: int, product_id: int, current_name: str):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id, self.product_id = bot, owner_id, guild_id, product_id
        self.name.default = current_name

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"formulario:{self.__class__.__name__}",
        ):
            return
        async with self.bot.db.sessions() as session:
            product = await session.scalar(
                select(Product).where(Product.id == self.product_id, Product.guild_id == self.guild_id)
            )
            if product is None:
                await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
                return
            product.custom_name = str(self.name.value).strip() or None
            await session.commit()
        await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.send_message("Nome do produto atualizado.", ephemeral=True)


class MoveCategorySelect(discord.ui.Select):
    def __init__(self, view: "MoveProductCategoryView"):
        self.parent_view = view
        start = view.page * PAGE_SIZE
        chunk = view.categories[start:start + PAGE_SIZE]
        options = [
            discord.SelectOption(label=c.name[:100], value=str(c.id), emoji=c.emoji or "🛍️")
            for c in chunk
        ]
        super().__init__(placeholder="Escolha a categoria de destino...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category_id = int(self.values[0])
        async with self.parent_view.bot.db.sessions() as session:
            category = await session.scalar(
                select(Category).where(Category.id == category_id, Category.guild_id == self.parent_view.guild_id)
            )
            product = await session.scalar(
                select(Product).where(Product.id == self.parent_view.product_id, Product.guild_id == self.parent_view.guild_id)
            )
            if category is None or product is None:
                await interaction.response.send_message("Produto ou categoria não encontrado.", ephemeral=True)
                return
            product.category_id = category_id
            product_name = product.display_name
            category_name = category.name
            await session.commit()
        await refresh_public_panel(self.parent_view.bot, self.parent_view.guild_id)
        await interaction.response.edit_message(
            content=f"**{product_name}** movido para **{category_name}**.", embed=None, view=None
        )


class MoveProductCategoryView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int, product_id: int, categories: list[Category], page: int = 0):
        super().__init__(bot, owner_id, guild_id)
        self.product_id, self.categories = product_id, categories
        self.page = max(0, min(page, (len(categories) - 1) // PAGE_SIZE))
        self.add_item(MoveCategorySelect(self))
        if len(categories) > PAGE_SIZE:
            previous = discord.ui.Button(label="Anterior", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
            previous.callback = self.previous_page
            self.add_item(previous)
            following = discord.ui.Button(
                label="Próxima", style=discord.ButtonStyle.secondary,
                disabled=(self.page + 1) * PAGE_SIZE >= len(categories), row=1,
            )
            following.callback = self.next_page
            self.add_item(following)
        back = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.cancel
        self.add_item(back)

    async def previous_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=MoveProductCategoryView(self.bot, self.owner_id, self.guild_id, self.product_id, self.categories, self.page - 1))

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=MoveProductCategoryView(self.bot, self.owner_id, self.guild_id, self.product_id, self.categories, self.page + 1))

    async def cancel(self, interaction: discord.Interaction):
        row = await get_product_with_category(self.bot, self.guild_id, self.product_id)
        if not row:
            await interaction.response.edit_message(content="Produto não encontrado.", embed=None, view=None)
            return
        product, category_name = row
        await interaction.response.edit_message(
            content="**Gerenciar produto**",
            embed=product_admin_embed(product, category_name),
            view=ProductActionsView(self.bot, self.owner_id, self.guild_id, product.id, product.is_active),
        )


class ConfirmProductDeleteView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int, product_id: int):
        super().__init__(bot, owner_id, guild_id)
        self.product_id = product_id

    @discord.ui.button(label="Confirmar exclusão", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            product = await session.scalar(
                select(Product).where(Product.id == self.product_id, Product.guild_id == self.guild_id)
            )
            if product is None:
                await interaction.response.edit_message(content="Produto não encontrado.", view=None)
                return
            name = product.display_name
            await session.delete(product)
            await session.commit()
        await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.edit_message(content=f"Produto **{name}** removido do catálogo.", view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await get_product_with_category(self.bot, self.guild_id, self.product_id)
        if not row:
            await interaction.response.edit_message(content="Produto não encontrado.", embed=None, view=None)
            return
        product, category_name = row
        await interaction.response.edit_message(
            content="**Gerenciar produto**",
            embed=product_admin_embed(product, category_name),
            view=ProductActionsView(self.bot, self.owner_id, self.guild_id, product.id, product.is_active),
        )


class PersonalizationMenuView(AdminView):
    @discord.ui.button(label="Texto e cor", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            settings = await get_settings(session, self.guild_id)
            defaults = {
                "title": settings.store_title,
                "description": settings.store_description,
                "color": f"#{settings.accent_color:06X}",
            }
        await interaction.response.send_modal(AppearanceModal(self.bot, self.owner_id, self.guild_id, defaults))

    @discord.ui.button(label="Enviar imagem", emoji="🖼️", style=discord.ButtonStyle.success, row=0)
    async def set_banner(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BannerUploadModal(self.bot, self.owner_id, self.guild_id))

    @discord.ui.button(label="Remover banner", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def clear_banner(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            settings = await get_settings(session, self.guild_id)
            settings.panel_banner_url = None
            settings.panel_banner_data = None
            settings.panel_banner_filename = None
            await session.commit()
        updated = await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.send_message(
            f"Banner removido. {'Painel público atualizado.' if updated else 'Publique o painel para aplicar as alterações.'}",
            ephemeral=True,
        )

    @discord.ui.button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=None,
            embed=await dashboard_embed(self.bot, self.guild_id),
            view=DashboardView(self.bot, self.owner_id, self.guild_id),
        )



MAX_PANEL_BANNER_BYTES = 8 * 1024 * 1024


def detect_banner_image(data: bytes) -> tuple[str, str] | None:
    """Validate image bytes by signature, not just the user-provided extension."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


class BannerUploadModal(discord.ui.Modal, title="Enviar banner do painel"):
    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id
        self.image_upload = discord.ui.FileUpload(
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(discord.ui.Label(
            text="Imagem do banner",
            description="Escolha uma imagem JPG, PNG, WEBP ou GIF (máximo 8 MB).",
            component=self.image_upload,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"formulario:{self.__class__.__name__}",
        ):
            return

        if not self.image_upload.values:
            await interaction.response.send_message("Selecione uma imagem para continuar.", ephemeral=True)
            return

        attachment = self.image_upload.values[0]
        if attachment.size and attachment.size > MAX_PANEL_BANNER_BYTES:
            await interaction.response.send_message(
                "A imagem ultrapassa o limite de 8 MB. Escolha uma imagem menor.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            image_data = await attachment.read()
        except (discord.HTTPException, OSError):
            await interaction.followup.send("Não consegui baixar a imagem enviada. Tente novamente.", ephemeral=True)
            return

        if len(image_data) > MAX_PANEL_BANNER_BYTES:
            await interaction.followup.send(
                "A imagem ultrapassa o limite de 8 MB. Escolha uma imagem menor.",
                ephemeral=True,
            )
            return

        image_info = detect_banner_image(image_data)
        if image_info is None:
            await interaction.followup.send(
                "Arquivo inválido. Envie uma imagem JPG, PNG, WEBP ou GIF.",
                ephemeral=True,
            )
            return

        extension, _content_type = image_info
        filename = f"store_banner_{uuid4().hex[:12]}.{extension}"
        async with self.bot.db.sessions() as session:
            settings = await get_settings(session, self.guild_id)
            settings.panel_banner_data = image_data
            settings.panel_banner_filename = filename
            settings.panel_banner_url = None
            await session.commit()

        updated = await refresh_public_panel(self.bot, self.guild_id)
        if updated:
            message = "Banner enviado e painel público atualizado com sucesso."
        else:
            message = "Imagem salva! Publique o painel da loja para exibir o novo banner."
        await interaction.followup.send(message, ephemeral=True)


class AppearanceModal(discord.ui.Modal, title="Personalizar painel da loja"):
    title_input = discord.ui.TextInput(label="Título do painel", max_length=100)
    description_input = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=500)
    color_input = discord.ui.TextInput(label="Cor hexadecimal", placeholder="#5865F2", max_length=7)

    def __init__(self, bot, owner_id: int, guild_id: int, defaults: dict[str, str]):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id
        self.title_input.default = defaults["title"] or "ROBLOX STORE"
        self.description_input.default = defaults["description"] or "Escolha uma categoria para explorar nossos itens."
        self.color_input.default = defaults["color"]

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action=f"formulario:{self.__class__.__name__}",
        ):
            return
        color_text = str(self.color_input.value).strip().removeprefix("#")
        try:
            if len(color_text) != 6:
                raise ValueError
            color = int(color_text, 16)
        except ValueError:
            await interaction.response.send_message("Cor inválida. Use hexadecimal, por exemplo #5865F2.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            settings = await get_settings(session, self.guild_id)
            settings.store_title = str(self.title_input.value).strip() or "ROBLOX STORE"
            settings.store_description = str(self.description_input.value).strip()
            settings.accent_color = color
            await session.commit()
        updated = await refresh_public_panel(self.bot, self.guild_id)
        await interaction.response.send_message(
            f"Personalização salva. {'Painel público atualizado.' if updated else 'Publique o painel para aplicar as alterações.'}",
            ephemeral=True,
        )


class PublishChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view: "PublishChannelView"):
        self.parent_view = view
        super().__init__(
            placeholder="Escolha o canal de texto...",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        if (
            not hasattr(channel, "send")
            or getattr(getattr(channel, "guild", None), "id", None) != self.parent_view.guild_id
        ):
            await interaction.response.send_message("Selecione um canal de texto deste servidor.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, view = await create_panel_payload(self.parent_view.bot, self.parent_view.guild_id)
        banner_file = await create_panel_banner_file(self.parent_view.bot, self.parent_view.guild_id)
        async with self.parent_view.bot.db.sessions() as session:
            settings = await get_settings(session, self.parent_view.guild_id)
            old_channel_id = settings.panel_channel_id
            old_message_id = settings.panel_message_id
        message = None
        if old_channel_id == channel.id and old_message_id:
            try:
                message = await channel.fetch_message(old_message_id)
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
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            try:
                # If the edit attempt failed, create a fresh File because discord.py may have closed it.
                banner_file = await create_panel_banner_file(self.parent_view.bot, self.parent_view.guild_id)
                if banner_file:
                    message = await channel.send(
                        embed=embed,
                        view=view,
                        file=banner_file,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    message = await channel.send(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except discord.Forbidden:
                await interaction.followup.send(
                    "Não tenho permissão para enviar mensagens e embeds nesse canal.", ephemeral=True
                )
                return
        async with self.parent_view.bot.db.sessions() as session:
            settings = await get_settings(session, self.parent_view.guild_id)
            settings.panel_channel_id = channel.id
            settings.panel_message_id = message.id
            await session.commit()
        await interaction.followup.send(f"Painel da loja publicado/atualizado em {channel.mention}.", ephemeral=True)


class PublishChannelView(AdminView):
    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(bot, owner_id, guild_id)
        self.add_item(PublishChannelSelect(self))
        back = discord.ui.Button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=await dashboard_embed(self.bot, self.guild_id),
            view=DashboardView(self.bot, self.owner_id, self.guild_id),
        )


class CatalogConfig(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="configcatalogo",
        description="Abre o painel administrativo do catálogo Roblox.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def config_catalog(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("Use este comando dentro de um servidor.", ephemeral=True)
            return
        if not await require_admin_context(
            interaction, guild_id=interaction.guild_id, action="abrir:/configcatalogo"
        ):
            return
        await interaction.response.send_message(
            embed=await dashboard_embed(self.bot, interaction.guild_id),
            view=DashboardView(self.bot, interaction.user.id, interaction.guild_id),
            ephemeral=True,
        )

    @config_catalog.error
    async def config_catalog_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            message = "Somente administradores do servidor podem usar `/configcatalogo`."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        log.exception("Erro no comando /configcatalogo", exc_info=error)
        message = "Ocorreu um erro ao abrir a configuração do catálogo. Verifique o log do bot."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(CatalogConfig(bot))
