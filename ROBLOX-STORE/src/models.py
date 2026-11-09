from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class GuildSettings(Base):
    __tablename__ = "guild_settings"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_title: Mapped[str] = mapped_column(String(100), default="ROBLOX STORE")
    store_description: Mapped[str] = mapped_column(Text, default="Escolha uma categoria para explorar nossos itens.")
    accent_color: Mapped[int] = mapped_column(Integer, default=0x5865F2)
    panel_banner_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    panel_banner_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    panel_banner_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    panel_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    panel_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("guild_id", "name", name="uq_category_guild_name"),
        Index("ix_categories_guild_position", "guild_id", "position"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(80))
    emoji: Mapped[str] = mapped_column(String(32), default="🛍️")
    description: Mapped[str] = mapped_column(String(100), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    products: Mapped[list["Product"]] = relationship(back_populates="category", cascade="all, delete-orphan")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("guild_id", "asset_id", name="uq_product_guild_asset"),
        Index("ix_products_guild_category_active", "guild_id", "category_id", "is_active"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(200))
    custom_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    price_robux: Mapped[int | None] = mapped_column(Integer, nullable=True)
    favorite_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    category: Mapped[Category] = relationship(back_populates="products")

    @property
    def display_name(self) -> str:
        return self.custom_name or self.name

    @property
    def roblox_url(self) -> str:
        return f"https://www.roblox.com/catalog/{self.asset_id}"



class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_guild_active", "guild_id", "is_active"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    asset_id: Mapped[int] = mapped_column(BigInteger)
    asset_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_name: Mapped[str] = mapped_column(String(200))
    item_description: Mapped[str] = mapped_column(Text, default="")
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    favorite_count: Mapped[int] = mapped_column(BigInteger, default=0)
    target_favorites: Mapped[int] = mapped_column(BigInteger, default=100)
    prize_text: Mapped[str] = mapped_column(String(500), default="Prêmio a definir")
    winner_count: Mapped[int] = mapped_column(Integer, default=1)
    response_timeout_minutes: Mapped[int] = mapped_column(Integer, default=60)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CampaignParticipant(Base):
    __tablename__ = "campaign_participants"
    __table_args__ = (
        UniqueConstraint("campaign_id", "discord_user_id", name="uq_campaign_discord_participant"),
        UniqueConstraint("campaign_id", "roblox_user_id", name="uq_campaign_roblox_participant"),
        Index("ix_campaign_participants_campaign", "campaign_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger)
    roblox_user_id: Mapped[int] = mapped_column(BigInteger)
    roblox_username: Mapped[str] = mapped_column(String(30))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignWinner(Base):
    __tablename__ = "campaign_winners"
    __table_args__ = (
        UniqueConstraint("campaign_id", "position", "attempt", name="uq_campaign_winner_attempt"),
        Index("ix_campaign_winners_campaign_position", "campaign_id", "position"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("campaign_participants.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    drawn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    response_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
