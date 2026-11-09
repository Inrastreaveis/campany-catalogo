from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Category, Product, GuildSettings


async def get_categories(session: AsyncSession, guild_id: int) -> list[Category]:
    result = await session.execute(
        select(Category).where(Category.guild_id == guild_id).order_by(Category.position, Category.id)
    )
    return list(result.scalars().all())


async def get_products(
    session: AsyncSession, guild_id: int, category_id: int | None = None, active_only: bool = True
) -> list[Product]:
    query = select(Product).where(Product.guild_id == guild_id)
    if category_id is not None:
        query = query.where(Product.category_id == category_id)
    if active_only:
        query = query.where(Product.is_active.is_(True))
    query = query.order_by(Product.name, Product.id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def category_stats(session: AsyncSession, guild_id: int, category_id: int) -> tuple[int, int]:
    result = await session.execute(
        select(func.count(Product.id), func.coalesce(func.sum(Product.favorite_count), 0)).where(
            Product.guild_id == guild_id,
            Product.category_id == category_id,
            Product.is_active.is_(True),
        )
    )
    count, favorites = result.one()
    return int(count or 0), int(favorites or 0)


async def store_stats(session: AsyncSession, guild_id: int) -> tuple[int, int]:
    result = await session.execute(
        select(func.count(Product.id), func.coalesce(func.sum(Product.favorite_count), 0)).where(
            Product.guild_id == guild_id,
            Product.is_active.is_(True),
        )
    )
    count, favorites = result.one()
    return int(count or 0), int(favorites or 0)


async def get_settings(session: AsyncSession, guild_id: int) -> GuildSettings:
    result = await session.execute(
        select(GuildSettings).where(GuildSettings.guild_id == guild_id)
    )
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = GuildSettings(guild_id=guild_id)
        session.add(settings)
        await session.flush()
    return settings


async def sync_product_from_roblox(product: Product, data: dict) -> None:
    product.name = data["name"]
    product.description = data.get("description") or ""
    product.price_robux = data.get("price_robux")
    product.favorite_count = data.get("favorite_count")
    product.thumbnail_url = data.get("thumbnail_url")
    product.asset_type_id = data.get("asset_type_id")
    product.last_synced_at = datetime.now(timezone.utc)
