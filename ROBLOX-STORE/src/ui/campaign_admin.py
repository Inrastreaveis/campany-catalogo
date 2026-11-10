from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from src.models import Campaign, CampaignParticipant, CampaignWinner
from src.security import require_admin_context
from src.roblox.client import RobloxAPIError, RobloxNotFound, RobloxRateLimited
from src.services.campaigns import (
    CampaignPublicView, ClaimPrizeView, campaign_embed, draw_campaign, participant_count,
    refresh_campaign_message,
)

log = logging.getLogger(__name__)

# Only members who hold this Discord role can use the manual Roblox tools.
CAMPAIGN_MANAGER_ROLE_ID = 1567021603580149810


async def require_campaign_manager_access(
    interaction: discord.Interaction,
    *,
    guild_id: int,
    owner_id: int,
    action: str,
) -> bool:
    """Require the original admin panel owner plus the configured manager role."""
    if not await require_admin_context(
        interaction,
        guild_id=guild_id,
        owner_id=owner_id,
        action=action,
    ):
        return False

    roles = getattr(interaction.user, "roles", ())
    if any(getattr(role, "id", None) == CAMPAIGN_MANAGER_ROLE_ID for role in roles):
        return True

    message = (
        "Acesso negado. Esta função exige o cargo autorizado "
        f"(ID: {CAMPAIGN_MANAGER_ROLE_ID})."
    )
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    log.warning(
        "Acesso manual de campanha bloqueado: action=%s guild_id=%s user_id=%s role_id=%s",
        action,
        guild_id,
        getattr(interaction.user, "id", None),
        CAMPAIGN_MANAGER_ROLE_ID,
    )
    return False


async def active_campaign(session, guild_id: int):
    """Compatibility helper: returns the newest active campaign."""
    result = await session.execute(
        select(Campaign).where(
            Campaign.guild_id == guild_id,
            Campaign.is_active.is_(True),
        ).order_by(Campaign.created_at.desc())
    )
    return result.scalars().first()


async def active_campaigns(session, guild_id: int):
    result = await session.execute(
        select(Campaign).where(
            Campaign.guild_id == guild_id,
            Campaign.is_active.is_(True),
        ).order_by(Campaign.created_at.desc())
    )
    return list(result.scalars().all())


class CampaignAdminView(discord.ui.View):
    """Main campaign menu: create a campaign or choose one to manage."""

    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_admin_context(
            interaction, guild_id=self.guild_id, owner_id=self.owner_id,
            action=f"painel:{self.__class__.__name__}"
        )

    @discord.ui.button(label="Criar campanha", emoji="🎁", style=discord.ButtonStyle.success, row=0)
    async def create_campaign(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Primeiro, escolha o canal onde o novo painel será publicado.",
            view=CampaignChannelSelectView(self.bot, self.owner_id, self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Gerenciar campanhas", emoji="🗂️", style=discord.ButtonStyle.primary, row=0)
    async def manage_campaigns(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaigns = await active_campaigns(session, self.guild_id)
        if not campaigns:
            await interaction.response.send_message(
                "Não há campanhas ativas. Use **Criar campanha** para publicar o primeiro painel.",
                ephemeral=True,
            )
            return
        view = CampaignSelectView(self.bot, self.owner_id, self.guild_id, campaigns)
        await interaction.response.send_message(
            f"Selecione qual campanha deseja administrar. **{len(campaigns)} campanha(s) ativa(s).**",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Consultar nick Roblox", emoji="🔎", style=discord.ButtonStyle.secondary, row=1)
    async def lookup_favorite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_campaign_manager_access(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action="consultar favoritos de um username Roblox",
        ):
            return
        await interaction.response.send_modal(
            FavoriteLookupModal(self.bot, self.owner_id, self.guild_id)
        )

    @discord.ui.button(label="Adicionar participante", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add_participant(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_campaign_manager_access(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action="adicionar participante manualmente",
        ):
            return
        async with self.bot.db.sessions() as session:
            campaigns = await active_campaigns(session, self.guild_id)
        if not campaigns:
            await interaction.response.send_message(
                "Não há campanhas ativas para receber participantes.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Selecione a campanha em que deseja inscrever a pessoa. Depois, escolha o membro do Discord e informe o username Roblox.",
            view=ManualAddCampaignSelectView(
                self.bot, self.owner_id, self.guild_id, campaigns
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Voltar ao catálogo", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.commands.catalog_config import DashboardView, dashboard_embed
        await interaction.response.edit_message(
            content=None,
            embed=await dashboard_embed(self.bot, self.guild_id),
            view=DashboardView(self.bot, self.owner_id, self.guild_id),
        )


class FavoriteLookupModal(discord.ui.Modal, title="Consultar favoritos Roblox"):
    username = discord.ui.TextInput(
        label="Username Roblox da pessoa",
        placeholder="Digite o username exato do Roblox",
        min_length=3,
        max_length=20,
        required=True,
    )

    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_campaign_manager_access(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action="consultar favoritos Roblox",
        ):
            return

        username_input = str(self.username.value).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            user = await self.bot.roblox.resolve_username(username_input)
        except RobloxRateLimited as exc:
            wait = max(1, int((exc.retry_after or 30) + 0.999))
            await interaction.followup.send(
                f"O Roblox continua limitando as consultas. Tente novamente em aproximadamente {wait} segundos.",
                ephemeral=True,
            )
            return
        except RobloxNotFound:
            await interaction.followup.send(
                f"Não encontrei o username Roblox **{username_input}**.", ephemeral=True
            )
            return
        except RobloxAPIError as exc:
            await interaction.followup.send(
                f"Não foi possível consultar essa conta Roblox: {exc}", ephemeral=True
            )
            return

        canonical_username = str(user.get("name") or username_input)
        async with self.bot.db.sessions() as session:
            campaigns = await active_campaigns(session, self.guild_id)

        if not campaigns:
            await interaction.followup.send(
                "Não há campanhas ativas neste servidor para consultar.", ephemeral=True
            )
            return

        lines = [f"**Conta Roblox:** {canonical_username} (`{user.get('id')}`)"]
        for index, campaign in enumerate(campaigns):
            try:
                verified = await self.bot.roblox.verify_public_favorite(
                    canonical_username,
                    int(campaign.asset_id),
                    campaign.asset_type_id,
                    max_pages=20,
                    resolved_user=user,
                )
            except RobloxRateLimited as exc:
                wait = max(1, int((exc.retry_after or 30) + 0.999))
                lines.append(
                    f"⚠️ **{campaign.item_name}:** consulta temporariamente limitada pelo Roblox "
                    f"(aguarde cerca de {wait}s)."
                )
                # Do not fire more requests while Roblox is still limiting us.
                for remaining in campaigns[index + 1:]:
                    lines.append(f"⚪ **{remaining.item_name}:** não consultado por limite da API.")
                break
            except RobloxAPIError as exc:
                log.warning("Falha na consulta administrativa de favoritos: %s", exc)
                verified = None

            if verified is True:
                result_text = "✅ Favorito confirmado"
            elif verified is False:
                result_text = "❌ Não está nos favoritos públicos"
            else:
                result_text = "⚠️ Não foi possível confirmar"
            lines.append(f"{result_text} — **{campaign.item_name}** (campanha #{campaign.id})")

        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


class ManualAddCampaignSelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, guild_id: int, campaigns: list[Campaign]):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label=(campaign.item_name or f"Campanha {campaign.id}")[:100],
                description=(
                    f"Campanha #{campaign.id} • {campaign.favorite_count:,}/{campaign.target_favorites:,} favoritos"
                ).replace(",", ".")[:100],
                value=str(campaign.id),
                emoji="🎁",
            )
            for campaign in campaigns[:25]
        ]
        self.campaign_select = discord.ui.Select(
            placeholder="Escolha a campanha...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.campaign_select.callback = self.select_campaign
        self.add_item(self.campaign_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_campaign_manager_access(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action="selecionar campanha para inscrição manual",
        )

    async def select_campaign(self, interaction: discord.Interaction):
        campaign_id = int(self.campaign_select.values[0])
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                await interaction.response.send_message(
                    "Essa campanha não está mais ativa. Abra o menu novamente.", ephemeral=True
                )
                return
            item_name = campaign.item_name

        await interaction.response.edit_message(
            content=(
                f"Campanha selecionada: **{item_name}**. Agora escolha o membro do Discord "
                "que será vinculado ao username Roblox."
            ),
            view=ManualAddDiscordUserSelectView(
                self.bot, self.owner_id, self.guild_id, campaign_id
            ),
        )


class ManualAddDiscordUserSelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, guild_id: int, campaign_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.campaign_id = campaign_id
        self.user_select = discord.ui.UserSelect(
            placeholder="Selecione o membro do Discord...",
            min_values=1,
            max_values=1,
        )
        self.user_select.callback = self.select_user
        self.add_item(self.user_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_campaign_manager_access(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action="selecionar membro para inscrição manual",
        )

    async def select_user(self, interaction: discord.Interaction):
        selected = self.user_select.values[0]
        selected_id = getattr(selected, "id", None)
        if not selected_id or getattr(selected, "bot", False):
            await interaction.response.send_message(
                "Selecione uma pessoa válida do servidor, não um bot.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ManualAddParticipantModal(
                self.bot,
                self.owner_id,
                self.guild_id,
                self.campaign_id,
                int(selected_id),
            )
        )


class ManualAddParticipantModal(discord.ui.Modal, title="Adicionar participante"):
    username = discord.ui.TextInput(
        label="Username Roblox da pessoa",
        placeholder="Digite o username exato do Roblox",
        min_length=3,
        max_length=20,
        required=True,
    )

    def __init__(self, bot, owner_id: int, guild_id: int, campaign_id: int, discord_user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.campaign_id = campaign_id
        self.discord_user_id = discord_user_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_campaign_manager_access(
            interaction,
            guild_id=self.guild_id,
            owner_id=self.owner_id,
            action="adicionar participante manualmente",
        ):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.followup.send("Use esta função dentro do servidor correto.", ephemeral=True)
            return

        member = guild.get_member(self.discord_user_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.discord_user_id)
            except discord.NotFound:
                await interaction.followup.send(
                    "O membro selecionado não está mais neste servidor.", ephemeral=True
                )
                return
            except discord.HTTPException:
                await interaction.followup.send(
                    "Não consegui confirmar o membro selecionado no Discord. Tente novamente.", ephemeral=True
                )
                return
        if member.bot:
            await interaction.followup.send("Bots não podem ser inscritos como participantes.", ephemeral=True)
            return

        username_input = str(self.username.value).strip()
        try:
            user = await self.bot.roblox.resolve_username(username_input)
            roblox_id = int(user["id"])
            canonical_username = str(user.get("name") or username_input).strip()
        except RobloxRateLimited as exc:
            wait = max(1, int((exc.retry_after or 30) + 0.999))
            await interaction.followup.send(
                f"O Roblox continua limitando as consultas. Tente novamente em aproximadamente {wait} segundos.",
                ephemeral=True,
            )
            return
        except RobloxNotFound:
            await interaction.followup.send(
                f"Não encontrei o username Roblox **{username_input}**.", ephemeral=True
            )
            return
        except RobloxAPIError as exc:
            await interaction.followup.send(
                f"Não foi possível consultar essa conta Roblox: {exc}", ephemeral=True
            )
            return

        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                await interaction.followup.send("Esta campanha não está mais ativa.", ephemeral=True)
                return
            asset_id = int(campaign.asset_id)
            asset_type_id = campaign.asset_type_id
            item_name = campaign.item_name

            existing_discord = await session.scalar(
                select(CampaignParticipant).where(
                    CampaignParticipant.campaign_id == self.campaign_id,
                    CampaignParticipant.discord_user_id == self.discord_user_id,
                )
            )
            if existing_discord:
                await interaction.followup.send(
                    f"{member.mention} já está inscrito nesta campanha.", ephemeral=True
                )
                return
            existing_roblox = await session.scalar(
                select(CampaignParticipant).where(
                    CampaignParticipant.campaign_id == self.campaign_id,
                    CampaignParticipant.roblox_user_id == roblox_id,
                )
            )
            if existing_roblox:
                await interaction.followup.send(
                    f"A conta Roblox **{canonical_username}** já está inscrita nesta campanha.",
                    ephemeral=True,
                )
                return

        try:
            verified = await self.bot.roblox.verify_public_favorite(
                canonical_username,
                asset_id,
                asset_type_id,
                max_pages=20,
                resolved_user=user,
            )
        except RobloxRateLimited as exc:
            wait = max(1, int((exc.retry_after or 30) + 0.999))
            await interaction.followup.send(
                f"O Roblox ainda está limitando a verificação. Nenhuma inscrição foi criada. "
                f"Tente novamente em aproximadamente {wait} segundos.",
                ephemeral=True,
            )
            return
        except RobloxAPIError as exc:
            log.warning("Falha ao verificar favorito em inscrição administrativa: %s", exc)
            await interaction.followup.send(
                "Não consegui confirmar o favorito no Roblox. Nenhuma inscrição foi criada.",
                ephemeral=True,
            )
            return

        if verified is not True:
            if verified is False:
                message = (
                    f"A conta **{canonical_username}** não tem o item **{item_name}** "
                    "nos favoritos públicos. A pessoa precisa favoritar o item antes da inscrição."
                )
            else:
                message = (
                    "Não foi possível confirmar o favorito com segurança. Nenhuma inscrição foi criada; "
                    "tente novamente mais tarde."
                )
            await interaction.followup.send(message, ephemeral=True)
            return

        try:
            async with self.bot.db.sessions() as session:
                campaign = await session.get(Campaign, self.campaign_id)
                if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                    await interaction.followup.send("Esta campanha foi encerrada durante a verificação.", ephemeral=True)
                    return
                # Re-check uniqueness after the network verification to prevent
                # simultaneous admin/public signups from creating duplicates.
                duplicate = await session.scalar(
                    select(CampaignParticipant).where(
                        CampaignParticipant.campaign_id == self.campaign_id,
                        (CampaignParticipant.discord_user_id == self.discord_user_id)
                        | (CampaignParticipant.roblox_user_id == roblox_id),
                    )
                )
                if duplicate:
                    await interaction.followup.send(
                        "Essa pessoa ou conta Roblox já está inscrita nesta campanha.", ephemeral=True
                    )
                    return
                session.add(CampaignParticipant(
                    campaign_id=self.campaign_id,
                    guild_id=self.guild_id,
                    discord_user_id=self.discord_user_id,
                    roblox_user_id=roblox_id,
                    roblox_username=canonical_username,
                ))
                await session.commit()
        except IntegrityError:
            await interaction.followup.send(
                "Essa pessoa ou conta Roblox já está inscrita nesta campanha.", ephemeral=True
            )
            return
        except Exception:
            log.exception("Falha ao adicionar participante manualmente à campanha %s", self.campaign_id)
            await interaction.followup.send("Não consegui salvar a inscrição. Consulte os logs do bot.", ephemeral=True)
            return

        await refresh_campaign_message(self.bot, self.campaign_id)
        log.info(
            "Participante adicionado por administrador: campaign_id=%s discord_user_id=%s "
            "roblox_user_id=%s username=%s admin_id=%s",
            self.campaign_id,
            self.discord_user_id,
            roblox_id,
            canonical_username,
            interaction.user.id,
        )
        await interaction.followup.send(
            f"✅ **Participante inscrito com sucesso!**\n"
            f"Membro Discord: {member.mention}\n"
            f"Conta Roblox: **{canonical_username}**\n"
            f"Campanha: **{item_name}**\n"
            "O favorito foi verificado diretamente no Roblox.",
            ephemeral=True,
        )


class CampaignSelectView(discord.ui.View):
    """Paginated selector so every active campaign can be managed independently."""

    def __init__(self, bot, owner_id: int, guild_id: int, campaigns: list[Campaign], page: int = 0):
        super().__init__(timeout=900)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.campaigns = campaigns
        self.page = max(0, min(page, max(0, (len(campaigns) - 1) // 25)))

        start = self.page * 25
        page_campaigns = campaigns[start:start + 25]
        options = [
            discord.SelectOption(
                label=(campaign.item_name or f"Campanha {campaign.id}")[:100],
                description=(
                    f"ID {campaign.id} • {campaign.favorite_count:,}/{campaign.target_favorites:,} favoritos"
                ).replace(",", ".")[:100],
                value=str(campaign.id),
                emoji="🎁",
            )
            for campaign in page_campaigns
        ]
        self.select_menu = discord.ui.Select(
            placeholder="Escolha a campanha que deseja administrar...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.select_menu.callback = self.select_campaign
        self.add_item(self.select_menu)
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * 25 >= len(campaigns)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_admin_context(
            interaction, guild_id=self.guild_id, owner_id=self.owner_id,
            action="selecionar campanha para administrar"
        )

    async def select_campaign(self, interaction: discord.Interaction):
        campaign_id = int(self.select_menu.values[0])
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                await interaction.response.send_message(
                    "Essa campanha não está mais ativa. Atualize a lista e tente novamente.",
                    ephemeral=True,
                )
                return
            count = await participant_count(session, campaign_id)
            embed = campaign_embed(campaign, count)
            item_name = campaign.item_name
        await interaction.response.edit_message(
            content=f"Administrando **{item_name}** (campanha #{campaign_id}). Escolha uma ação:",
            embed=embed,
            view=CampaignManageView(self.bot, self.owner_id, self.guild_id, campaign_id),
        )

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = CampaignSelectView(self.bot, self.owner_id, self.guild_id, self.campaigns, self.page - 1)
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="Próxima", emoji="➡️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = CampaignSelectView(self.bot, self.owner_id, self.guild_id, self.campaigns, self.page + 1)
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Gerencie as campanhas e os sorteios diretamente por este painel.",
            embed=None,
            view=CampaignAdminView(self.bot, self.owner_id, self.guild_id),
        )


class CampaignManageView(discord.ui.View):
    """Actions are always scoped to one explicitly selected campaign ID."""

    def __init__(self, bot, owner_id: int, guild_id: int, campaign_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.campaign_id = campaign_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_admin_context(
            interaction, guild_id=self.guild_id, owner_id=self.owner_id,
            action=f"administrar campanha:{self.campaign_id}"
        )

    @discord.ui.button(label="Editar campanha", emoji="⚙️", style=discord.ButtonStyle.primary, row=0)
    async def edit_campaign(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                await interaction.response.send_message("Esta campanha não está mais ativa.", ephemeral=True)
                return
        await interaction.response.send_modal(
            EditCampaignModal(self.bot, self.owner_id, self.guild_id, campaign)
        )

    @discord.ui.button(label="Participantes", emoji="👥", style=discord.ButtonStyle.secondary, row=0)
    async def participants(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id:
                await interaction.response.send_message("Campanha não encontrada.", ephemeral=True)
                return
            result = await session.execute(
                select(CampaignParticipant).where(
                    CampaignParticipant.campaign_id == self.campaign_id
                ).order_by(CampaignParticipant.joined_at)
            )
            rows = list(result.scalars().all())
        if not rows:
            await interaction.response.send_message("Ainda não há participantes confirmados nesta campanha.", ephemeral=True)
            return
        lines = [
            f"`{i}.` **{p.roblox_username}** — <@{p.discord_user_id}> (`{p.roblox_user_id}`)"
            for i, p in enumerate(rows[:60], start=1)
        ]
        if len(rows) > 60:
            lines.append(f"... e mais {len(rows) - 60} participantes.")
        embed = discord.Embed(
            title=f"👥 Participantes — campanha #{self.campaign_id}: {len(rows)}",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fazer sorteio", emoji="🏆", style=discord.ButtonStyle.primary, row=1)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await draw_campaign(self.bot, self.campaign_id)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="Sortear substituto", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def substitute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            SubstituteWinnerModal(self.bot, self.owner_id, self.guild_id, self.campaign_id)
        )

    @discord.ui.button(label="Encerrar campanha", emoji="⛔", style=discord.ButtonStyle.danger, row=1)
    async def close_campaign(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                await interaction.response.send_message("Esta campanha já está encerrada ou não existe.", ephemeral=True)
                return
            campaign.is_active = False
            await session.commit()
        await refresh_campaign_message(self.bot, self.campaign_id)
        await interaction.response.edit_message(
            content=f"⛔ Campanha **#{self.campaign_id}** encerrada. As outras campanhas continuam ativas.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Escolher outra campanha", emoji="🗂️", style=discord.ButtonStyle.secondary, row=2)
    async def choose_another(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaigns = await active_campaigns(session, self.guild_id)
        if not campaigns:
            await interaction.response.edit_message(
                content="Não há campanhas ativas no momento.",
                embed=None,
                view=CampaignAdminView(self.bot, self.owner_id, self.guild_id),
            )
            return
        await interaction.response.edit_message(
            content=f"Selecione qual campanha deseja administrar. **{len(campaigns)} campanha(s) ativa(s).**",
            embed=None,
            view=CampaignSelectView(self.bot, self.owner_id, self.guild_id, campaigns),
        )


class CampaignChannelSelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id
        self.add_item(CampaignChannelSelect(self))


class CampaignChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent):
        self.admin_panel = parent
        super().__init__(
            placeholder="Selecione o canal de publicação...",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )

    async def callback(self, interaction: discord.Interaction):
        if not await require_admin_context(
            interaction, guild_id=self.admin_panel.guild_id, owner_id=self.admin_panel.owner_id,
            action="selecionar canal da campanha"
        ):
            return
        selected = self.values[0]
        selected_id = getattr(selected, "id", None)
        guild = interaction.guild
        if guild is None or guild.id != self.admin_panel.guild_id or selected_id is None:
            await interaction.response.send_message("Não consegui identificar o canal deste servidor.", ephemeral=True)
            return
        # ChannelSelect normally supplies a resolved channel object. Reuse it
        # instead of making an extra HTTP request before opening the modal.
        channel = selected if isinstance(selected, discord.TextChannel) else guild.get_channel(int(selected_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Selecione um canal de texto deste servidor.", ephemeral=True)
            return
        me = guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None:
            await interaction.response.send_message("Não consegui verificar as permissões do bot neste canal.", ephemeral=True)
            return
        if not perms.view_channel or not perms.send_messages or not perms.embed_links:
            await interaction.response.send_message(
                "Preciso de permissões para visualizar o canal, enviar mensagens e inserir links.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            CreateCampaignModal(self.admin_panel.bot, self.admin_panel.owner_id, self.admin_panel.guild_id, channel)
        )


class CreateCampaignModal(discord.ui.Modal, title="Criar campanha / sorteio"):
    item_id = discord.ui.TextInput(label="ID do item Roblox", placeholder="Ex.: 123456789", max_length=20)
    target = discord.ui.TextInput(label="Meta de favoritos", placeholder="Ex.: 200", max_length=12)
    prize = discord.ui.TextInput(label="Prêmio do sorteio", placeholder="Descreva o prêmio", max_length=500)
    winners = discord.ui.TextInput(label="Quantidade de ganhadores", default="1", max_length=2)
    response_timeout = discord.ui.TextInput(label="Prazo para resposta (minutos)", default="60", max_length=5)

    def __init__(self, bot, owner_id: int, guild_id: int, channel: discord.TextChannel):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id, self.channel = bot, owner_id, guild_id, channel

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(interaction, guild_id=self.guild_id, owner_id=self.owner_id, action="criar campanha pelo configcatalogo"):
            return
        try:
            item_id = int(str(self.item_id.value).strip())
            target = int(str(self.target.value).strip())
            winners = int(str(self.winners.value).strip())
            timeout = int(str(self.response_timeout.value).strip())
            prize = str(self.prize.value).strip()
            if item_id <= 0 or target <= 0 or not 1 <= winners <= 20 or not 1 <= timeout <= 10080 or not prize:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Confira os campos: ID e meta positivos, prêmio preenchido, ganhadores entre 1 e 20 e prazo entre 1 e 10080 minutos.",
                ephemeral=True,
            )
            return
        # Responder ao envio do modal imediatamente. Assim o Discord fecha o
        # formulário e o administrador vê que o processamento começou.
        await interaction.response.send_message(
            "⏳ Recebi os dados. Estou consultando o item no Roblox e preparando a campanha...",
            ephemeral=True,
        )
        channel = self.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild_id:
            await interaction.edit_original_response(
                content="O canal selecionado não é mais válido. Abra o painel e selecione novamente."
            )
            return
        try:
            log.info("Iniciando criação de campanha: guild=%s item_id=%s channel_id=%s",
                     self.guild_id, item_id, channel.id)
            # A consulta ao Roblox faz até três chamadas HTTP sequenciais.
            # Limitar o tempo total evita deixar o formulário aparentemente preso.
            data = await asyncio.wait_for(self.bot.roblox.get_product(item_id), timeout=50)
            log.info("Dados do item Roblox recebidos: item_id=%s name=%s",
                     item_id, data.get("name"))
            if not data.get("asset_type_id"):
                raise ValueError("O Roblox não informou o tipo do item; não é possível validar favoritos.")
            async with self.bot.db.sessions() as session:
                # Campaigns are independent: creating a new panel must not
                # deactivate any existing campaign in this server.
                campaign = Campaign(
                    guild_id=self.guild_id, asset_id=item_id, asset_type_id=data["asset_type_id"],
                    item_name=data["name"], item_description=data.get("description", ""),
                    thumbnail_url=data.get("thumbnail_url"), favorite_count=data.get("favorite_count") or 0,
                    target_favorites=target, prize_text=prize, winner_count=winners,
                    response_timeout_minutes=timeout, channel_id=channel.id, is_active=True,
                )
                session.add(campaign)
                await session.flush()
                count = await participant_count(session, campaign.id)
                message = await channel.send(
                    embed=campaign_embed(campaign, count),
                    view=CampaignPublicView(self.bot, campaign.id, item_id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                campaign.message_id = message.id
                await session.commit()
                campaign_id = campaign.id
            self.bot.add_view(CampaignPublicView(self.bot, campaign_id, item_id), message_id=message.id)
            await interaction.edit_original_response(
                content=f"✅ Campanha publicada em {channel.mention}.\nItem: **{data['name']}** (`{item_id}`)\nMeta: **{target}** • Ganhadores: **{winners}**"
            )
        except asyncio.TimeoutError:
            log.exception("Tempo limite ao consultar Roblox durante criação da campanha: item_id=%s", item_id)
            await interaction.edit_original_response(
                content="⏱️ O Roblox demorou mais de 50 segundos para responder. Nenhuma campanha foi publicada. Tente novamente em alguns instantes."
            )
        except Exception as exc:
            log.exception("Falha ao criar campanha pelo painel administrativo")
            await interaction.edit_original_response(
                content=f"❌ Não foi possível criar a campanha: {str(exc)[:700]}"
            )


class EditCampaignModal(discord.ui.Modal, title="Editar campanha ativa"):
    item_id = discord.ui.TextInput(label="Novo ID do item (vazio = manter)", required=False, max_length=20)
    target = discord.ui.TextInput(label="Nova meta (vazio = manter)", required=False, max_length=12)
    prize = discord.ui.TextInput(label="Novo prêmio (vazio = manter)", required=False, max_length=500)
    winners = discord.ui.TextInput(label="Ganhadores (vazio = manter)", required=False, max_length=2)
    response_timeout = discord.ui.TextInput(label="Prazo em minutos (vazio = manter)", required=False, max_length=5)

    def __init__(self, bot, owner_id: int, guild_id: int, campaign: Campaign):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id
        self.campaign_id = campaign.id
        self.item_id.default = str(campaign.asset_id)
        self.target.default = str(campaign.target_favorites)
        self.prize.default = campaign.prize_text
        self.winners.default = str(campaign.winner_count)
        self.response_timeout.default = str(campaign.response_timeout_minutes)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(interaction, guild_id=self.guild_id, owner_id=self.owner_id, action="editar campanha pelo configcatalogo"):
            return
        values = [str(x.value).strip() for x in (self.item_id, self.target, self.prize, self.winners, self.response_timeout)]
        if not any(values):
            await interaction.response.send_message("Preencha ao menos um campo para alterar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_item = None
        try:
            parsed_item = int(values[0]) if values[0] else None
            parsed_target = int(values[1]) if values[1] else None
            parsed_winners = int(values[3]) if values[3] else None
            parsed_timeout = int(values[4]) if values[4] else None
            if parsed_target is not None and parsed_target <= 0:
                raise ValueError("A meta deve ser maior que zero.")
            if parsed_winners is not None and not 1 <= parsed_winners <= 20:
                raise ValueError("A quantidade de ganhadores deve ficar entre 1 e 20.")
            if parsed_timeout is not None and not 1 <= parsed_timeout <= 10080:
                raise ValueError("O prazo deve ficar entre 1 e 10080 minutos.")
            if values[2] and len(values[2]) > 500:
                raise ValueError("O texto do prêmio é muito longo.")
            if parsed_item is not None:
                new_item = await self.bot.roblox.get_product(parsed_item)
                if not new_item.get("asset_type_id"):
                    raise ValueError("O Roblox não informou o tipo do item.")
        except Exception as exc:
            await interaction.followup.send(f"Verifique os dados informados: {exc}", ephemeral=True)
            return

        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                await interaction.followup.send("Esta campanha não está mais ativa.", ephemeral=True)
                return
            winner_count = await session.scalar(
                select(func.count(CampaignWinner.id)).where(CampaignWinner.campaign_id == campaign.id)
            )
            if winner_count:
                await interaction.followup.send("Esta campanha já iniciou sorteios. Encerre-a para criar outra com novas regras.", ephemeral=True)
                return
            if new_item and parsed_item != campaign.asset_id:
                participants = await session.scalar(
                    select(func.count(CampaignParticipant.id)).where(CampaignParticipant.campaign_id == campaign.id)
                )
                if participants:
                    await interaction.followup.send("Não é possível trocar o item após inscrições. Crie uma nova campanha.", ephemeral=True)
                    return
                campaign.asset_id = parsed_item
                campaign.asset_type_id = new_item["asset_type_id"]
                campaign.item_name = new_item["name"]
                campaign.item_description = new_item.get("description", "")
                campaign.thumbnail_url = new_item.get("thumbnail_url")
                campaign.favorite_count = new_item.get("favorite_count") or 0
            if parsed_target is not None:
                campaign.target_favorites = parsed_target
            if values[2]:
                campaign.prize_text = values[2]
            if parsed_winners is not None:
                campaign.winner_count = parsed_winners
            if parsed_timeout is not None:
                campaign.response_timeout_minutes = parsed_timeout
            campaign.updated_at = datetime.now(timezone.utc)
            campaign_id = campaign.id
            asset_id = campaign.asset_id
            await session.commit()
        await refresh_campaign_message(self.bot, campaign_id)
        await interaction.followup.send("✅ Configurações salvas e painel público atualizado.", ephemeral=True)


class SubstituteWinnerModal(discord.ui.Modal, title="Sortear substituto"):
    position = discord.ui.TextInput(label="Posição que precisa de substituto", placeholder="Ex.: 1", max_length=2)

    def __init__(self, bot, owner_id: int, guild_id: int, campaign_id: int):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id
        self.campaign_id = campaign_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(interaction, guild_id=self.guild_id, owner_id=self.owner_id, action="sortear substituto pelo configcatalogo"):
            return
        try:
            position = int(str(self.position.value).strip())
            if not 1 <= position <= 20:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Informe uma posição entre 1 e 20.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or campaign.guild_id != self.guild_id or not campaign.is_active:
                await interaction.followup.send("Esta campanha não está mais ativa.", ephemeral=True)
                return
            result = await session.execute(
                select(CampaignWinner).where(
                    CampaignWinner.campaign_id == campaign.id,
                    CampaignWinner.position == position,
                    CampaignWinner.status.in_(["pending", "expired"]),
                ).order_by(CampaignWinner.attempt.desc())
            )
            previous = result.scalars().first()
            if previous is None:
                await interaction.followup.send("Não encontrei ganhador pendente ou expirado nessa posição.", ephemeral=True)
                return
            deadline = previous.response_deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if previous.status == "pending" and datetime.now(timezone.utc) <= deadline:
                await interaction.followup.send("O prazo do ganhador ainda não terminou.", ephemeral=True)
                return
            previous.status = "replaced"
            excluded_result = await session.execute(
                select(CampaignWinner.participant_id).where(CampaignWinner.campaign_id == campaign.id)
            )
            excluded = set(excluded_result.scalars().all())
            participant_result = await session.execute(
                select(CampaignParticipant).where(CampaignParticipant.campaign_id == campaign.id)
            )
            eligible = [p for p in participant_result.scalars().all() if p.id not in excluded]
            if not eligible:
                previous.status = "expired"
                await session.commit()
                await interaction.followup.send("Não há participantes elegíveis restantes.", ephemeral=True)
                return
            selected = secrets.SystemRandom().choice(eligible)
            now = datetime.now(timezone.utc)
            winner = CampaignWinner(
                campaign_id=campaign.id, participant_id=selected.id, position=position,
                attempt=previous.attempt + 1, status="pending", drawn_at=now,
                response_deadline=now + timedelta(minutes=campaign.response_timeout_minutes),
            )
            session.add(winner)
            await session.flush()
            winner_id, user_id = winner.id, selected.discord_user_id
            username, item_name, prize, timeout = selected.roblox_username, campaign.item_name, campaign.prize_text, campaign.response_timeout_minutes
            await session.commit()
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(
                f"🎉 Você foi selecionado(a) como substituto(a) do **{position}º lugar** na campanha **{item_name}**!\n"
                f"**Prêmio:** {prize}\nVocê tem **{timeout} minutos** para responder.",
                view=ClaimPrizeView(self.bot, winner_id, user_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Não foi possível enviar DM ao vencedor substituto %s", user_id)
        self.bot.add_view(ClaimPrizeView(self.bot, winner_id, user_id))
        await interaction.followup.send(f"✅ Substituto sorteado: **{username}** para o {position}º lugar.", ephemeral=True)
