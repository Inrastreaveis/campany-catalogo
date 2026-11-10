from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, func

from src.models import Campaign, CampaignParticipant, CampaignWinner
from src.security import require_admin_context
from src.services.campaigns import (
    CampaignPublicView, ClaimPrizeView, campaign_embed, participant_count,
    draw_campaign, refresh_campaign_message,
)

log = logging.getLogger(__name__)


async def active_campaign(session, guild_id: int):
    """Compatibility helper for legacy code; prefer explicit campaign IDs."""
    result = await session.execute(
        select(Campaign).where(
            Campaign.guild_id == guild_id,
            Campaign.is_active.is_(True),
        ).order_by(Campaign.created_at.desc())
    )
    return result.scalars().first()


async def campaign_for_guild(session, guild_id: int, campaign_id: int, active_only: bool = True):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.guild_id != guild_id:
        return None
    if active_only and not campaign.is_active:
        return None
    return campaign


class CampaignCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _admin(self, interaction: discord.Interaction) -> bool:
        return await require_admin_context(
            interaction, guild_id=interaction.guild_id, action="campanha"
        )

    @app_commands.command(name="campanha_criar", description="Cria e publica uma campanha de favoritos e sorteio.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        item_id="ID do item Roblox que será favoritado",
        meta="Quantidade de favoritos necessária para liberar o sorteio",
        premio="Descrição do prêmio que será entregue",
        ganhadores="Quantidade de posições que serão sorteadas",
        prazo_resposta_minutos="Tempo que cada ganhador terá para responder",
        canal="Canal onde o painel será publicado",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        item_id: app_commands.Range[int, 1, 9_007_199_254_740_991],
        meta: app_commands.Range[int, 1, 2_000_000_000],
        premio: str,
        ganhadores: app_commands.Range[int, 1, 20],
        prazo_resposta_minutos: app_commands.Range[int, 1, 10080],
        canal: discord.TextChannel,
    ):
        if not await self._admin(interaction):
            return
        if not premio.strip() or len(premio) > 500:
            await interaction.response.send_message("O prêmio deve ter entre 1 e 500 caracteres.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self.bot.roblox.get_product(item_id)
        except Exception as exc:
            await interaction.followup.send(f"Não consegui consultar o item Roblox: {exc}", ephemeral=True)
            return

        if not data.get("asset_type_id"):
            await interaction.followup.send(
                "O Roblox não informou o tipo deste item. Não consigo verificar favoritos com segurança.",
                ephemeral=True,
            )
            return

        try:
            async with self.bot.db.sessions() as session:
                # Each campaign owns its own public panel and participant list.
                # Creating a new campaign must not deactivate existing campaigns.
                campaign = Campaign(
                    guild_id=interaction.guild_id,
                    asset_id=item_id,
                    asset_type_id=data["asset_type_id"],
                    item_name=data["name"],
                    item_description=data.get("description", ""),
                    thumbnail_url=data.get("thumbnail_url"),
                    favorite_count=data.get("favorite_count") or 0,
                    target_favorites=meta,
                    prize_text=premio,
                    winner_count=ganhadores,
                    response_timeout_minutes=prazo_resposta_minutos,
                    channel_id=canal.id,
                    is_active=True,
                )
                session.add(campaign)
                await session.flush()
                count = await participant_count(session, campaign.id)
                message = await canal.send(
                    embed=campaign_embed(campaign, count),
                    view=CampaignPublicView(self.bot, campaign.id, item_id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                campaign.message_id = message.id
                await session.commit()
                campaign_id = campaign.id
            self.bot.add_view(CampaignPublicView(self.bot, campaign_id, item_id), message_id=message.id)
            await interaction.followup.send(
                f"✅ Campanha criada e publicada em {canal.mention}.\n"
                f"**Item:** {data['name']} (`{item_id}`)\n"
                f"**Meta:** {meta:,} favoritos\n**Prêmio:** {premio}\n"
                f"**Vagas:** {ganhadores}".replace(",", "."),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Não tenho permissão para enviar mensagens e inserir links nesse canal.", ephemeral=True
            )
        except Exception:
            log.exception("Falha ao criar campanha")
            await interaction.followup.send("Erro ao criar a campanha. Consulte os logs do bot.", ephemeral=True)

    @app_commands.command(name="campanha_editar", description="Edita uma campanha específica pelo ID.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        campanha_id="ID da campanha que deseja editar (visível no painel Gerenciar campanhas)",
        item_id="Novo ID Roblox (opcional)",
        meta="Nova meta de favoritos",
        premio="Novo prêmio",
        ganhadores="Nova quantidade de ganhadores",
        prazo_resposta_minutos="Novo prazo para resposta",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        campanha_id: app_commands.Range[int, 1, 9_007_199_254_740_991],
        item_id: app_commands.Range[int, 1, 9_007_199_254_740_991] | None = None,
        meta: app_commands.Range[int, 1, 2_000_000_000] | None = None,
        premio: str | None = None,
        ganhadores: app_commands.Range[int, 1, 20] | None = None,
        prazo_resposta_minutos: app_commands.Range[int, 1, 10080] | None = None,
    ):
        if not await self._admin(interaction):
            return
        if all(value is None for value in (item_id, meta, premio, ganhadores, prazo_resposta_minutos)):
            await interaction.response.send_message("Informe ao menos uma configuração para alterar.", ephemeral=True)
            return
        if premio is not None and (not premio.strip() or len(premio) > 500):
            await interaction.response.send_message("O prêmio deve ter entre 1 e 500 caracteres.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_item = None
        if item_id is not None:
            try:
                new_item = await self.bot.roblox.get_product(item_id)
                if not new_item.get("asset_type_id"):
                    raise ValueError("O Roblox não informou o tipo do item.")
            except Exception as exc:
                await interaction.followup.send(f"Não consegui validar o novo item: {exc}", ephemeral=True)
                return

        async with self.bot.db.sessions() as session:
            campaign = await campaign_for_guild(session, interaction.guild_id, campanha_id)
            if campaign is None:
                await interaction.followup.send("Campanha não encontrada ou não está ativa neste servidor.", ephemeral=True)
                return
            winners_result = await session.execute(
                select(func.count(CampaignWinner.id)).where(CampaignWinner.campaign_id == campaign.id)
            )
            if (winners_result.scalar() or 0) > 0:
                await interaction.followup.send(
                    "Esta campanha já teve sorteio iniciado. Encerre-a e crie outra para alterar as regras com segurança.",
                    ephemeral=True,
                )
                return
            if new_item and new_item["asset_id"] != campaign.asset_id:
                participants_result = await session.execute(
                    select(func.count(CampaignParticipant.id)).where(
                        CampaignParticipant.campaign_id == campaign.id
                    )
                )
                if (participants_result.scalar() or 0) > 0:
                    await interaction.followup.send(
                        "Não posso trocar o item depois de existirem participantes, pois as inscrições foram verificadas para o item anterior. Crie uma nova campanha.",
                        ephemeral=True,
                    )
                    return
            if new_item:
                campaign.asset_id = item_id
                campaign.asset_type_id = new_item["asset_type_id"]
                campaign.item_name = new_item["name"]
                campaign.item_description = new_item.get("description", "")
                campaign.thumbnail_url = new_item.get("thumbnail_url")
                campaign.favorite_count = new_item.get("favorite_count") or 0
            if meta is not None:
                campaign.target_favorites = meta
            if premio is not None:
                campaign.prize_text = premio
            if ganhadores is not None:
                campaign.winner_count = ganhadores
            if prazo_resposta_minutos is not None:
                campaign.response_timeout_minutes = prazo_resposta_minutos
            campaign.updated_at = datetime.now(timezone.utc)
            campaign_id = campaign.id
            await session.commit()
        await refresh_campaign_message(self.bot, campaign_id)
        await interaction.followup.send("✅ Configurações da campanha atualizadas. O painel existente foi editado.", ephemeral=True)

    @app_commands.command(name="campanha_participantes", description="Mostra participantes de uma campanha específica.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(campanha_id="ID da campanha que deseja consultar")
    async def participants(
        self,
        interaction: discord.Interaction,
        campanha_id: app_commands.Range[int, 1, 9_007_199_254_740_991],
    ):
        if not await self._admin(interaction):
            return
        async with self.bot.db.sessions() as session:
            campaign = await campaign_for_guild(session, interaction.guild_id, campanha_id)
            if campaign is None:
                await interaction.response.send_message("Campanha não encontrada ou não está ativa neste servidor.", ephemeral=True)
                return
            result = await session.execute(
                select(CampaignParticipant).where(
                    CampaignParticipant.campaign_id == campaign.id
                ).order_by(CampaignParticipant.joined_at)
            )
            rows = list(result.scalars().all())
        if not rows:
            await interaction.response.send_message("Ainda não há participantes confirmados.", ephemeral=True)
            return
        lines = []
        for i, participant in enumerate(rows[:60], start=1):
            entry = f"`{i}.` **{participant.roblox_username}**"
            if participant.discord_user_id is not None:
                entry += f" — <@{participant.discord_user_id}>"
            lines.append(entry)
        if len(rows) > 60:
            lines.append(f"... e mais {len(rows) - 60} participantes.")
        embed = discord.Embed(
            title=f"👥 Participantes confirmados: {len(rows)}",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="campanha_sortear", description="Sorteia uma campanha específica quando a meta for atingida.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(campanha_id="ID da campanha que deseja sortear")
    async def draw(
        self,
        interaction: discord.Interaction,
        campanha_id: app_commands.Range[int, 1, 9_007_199_254_740_991],
    ):
        if not await self._admin(interaction):
            return
        async with self.bot.db.sessions() as session:
            campaign = await campaign_for_guild(session, interaction.guild_id, campanha_id)
            if campaign is None:
                await interaction.response.send_message("Campanha não encontrada ou não está ativa neste servidor.", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await draw_campaign(self.bot, campaign_id)
        await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(name="campanha_substituir", description="Sorteia um substituto em uma campanha específica.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        campanha_id="ID da campanha",
        posicao="Número da posição que precisa de substituto (1º, 2º, 3º...)",
    )
    async def replace_winner(
        self,
        interaction: discord.Interaction,
        campanha_id: app_commands.Range[int, 1, 9_007_199_254_740_991],
        posicao: app_commands.Range[int, 1, 20],
    ):
        if not await self._admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.db.sessions() as session:
            campaign = await campaign_for_guild(session, interaction.guild_id, campanha_id)
            if campaign is None:
                await interaction.followup.send("Campanha não encontrada ou não está ativa neste servidor.", ephemeral=True)
                return
            result = await session.execute(
                select(CampaignWinner).where(
                    CampaignWinner.campaign_id == campaign.id,
                    CampaignWinner.position == posicao,
                    CampaignWinner.status.in_(["pending", "expired"]),
                ).order_by(CampaignWinner.attempt.desc())
            )
            previous = result.scalars().first()
            if previous is None:
                await interaction.followup.send(
                    "Não encontrei uma convocação pendente ou expirada para essa posição.", ephemeral=True
                )
                return
            deadline = previous.response_deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if previous.status == "pending" and datetime.now(timezone.utc) <= deadline:
                await interaction.followup.send(
                    "O prazo desse ganhador ainda não terminou. Aguarde o tempo configurado.", ephemeral=True
                )
                return
            if previous.status == "pending":
                previous.status = "expired"
            campaign_id = campaign.id
            old_attempt = previous.attempt
            previous.status = "replaced"
            excluded_result = await session.execute(
                select(CampaignWinner.participant_id).where(CampaignWinner.campaign_id == campaign_id)
            )
            excluded = set(excluded_result.scalars().all())
            participant_result = await session.execute(
                select(CampaignParticipant).where(CampaignParticipant.campaign_id == campaign_id)
            )
            eligible = [p for p in participant_result.scalars().all() if p.id not in excluded]
            if not eligible:
                previous.status = "expired"
                await session.commit()
                await interaction.followup.send("Não há participantes elegíveis restantes para substituir o ganhador.", ephemeral=True)
                return
            selected = secrets.SystemRandom().choice(eligible)
            new_winner = CampaignWinner(
                campaign_id=campaign_id,
                participant_id=selected.id,
                position=posicao,
                attempt=old_attempt + 1,
                status="pending",
                drawn_at=datetime.now(timezone.utc),
                response_deadline=datetime.now(timezone.utc) + timedelta(minutes=campaign.response_timeout_minutes),
            )
            session.add(new_winner)
            await session.flush()
            winner_id = new_winner.id
            discord_user_id = selected.discord_user_id
            username = selected.roblox_username
            item_name = campaign.item_name
            prize_text = campaign.prize_text
            timeout = campaign.response_timeout_minutes
            channel_id = campaign.channel_id
            await session.commit()

        if discord_user_id is not None:
            try:
                user = await self.bot.fetch_user(discord_user_id)
                view = ClaimPrizeView(self.bot, winner_id, discord_user_id)
                await user.send(
                    f"🎉 Você foi selecionado(a) como substituto(a) do **{posicao}º lugar** "
                    f"na campanha **{item_name}**!\n**Roblox:** {username}\n"
                    f"**Prêmio:** {prize_text}\nVocê tem **{timeout} minutos** para responder.",
                    view=view,
                )
                self.bot.add_view(view)
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Não foi possível enviar DM ao vencedor substituto %s", discord_user_id)

        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.send(
                f"@here\n🔁 **NOVO GANHADOR — {discord.utils.escape_mentions(item_name)}**\n**{posicao}º lugar:** `{discord.utils.escape_mentions(username)}`",
                allowed_mentions=discord.AllowedMentions(
                    everyone=True, users=False, roles=False, replied_user=False
                ),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.exception(
                "Não foi possível anunciar o ganhador substituto da campanha %s",
                campaign_id,
            )

        await interaction.followup.send(
            f"✅ Novo participante sorteado para o **{posicao}º lugar**: **{username}**. "
            "A convocação foi registrada.",
            ephemeral=True,
        )

    @app_commands.command(name="campanha_encerrar", description="Encerra uma campanha específica.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(campanha_id="ID da campanha que deseja encerrar")
    async def close_campaign(
        self,
        interaction: discord.Interaction,
        campanha_id: app_commands.Range[int, 1, 9_007_199_254_740_991],
    ):
        if not await self._admin(interaction):
            return
        async with self.bot.db.sessions() as session:
            campaign = await campaign_for_guild(session, interaction.guild_id, campanha_id)
            if campaign is None:
                await interaction.response.send_message("Campanha não encontrada ou não está ativa neste servidor.", ephemeral=True)
                return
            campaign.is_active = False
            await session.commit()
        await refresh_campaign_message(self.bot, campaign_id)
        await interaction.response.send_message("Campanha encerrada. O painel foi mantido no canal, mas as inscrições foram fechadas.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CampaignCommands(bot))
