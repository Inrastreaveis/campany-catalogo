from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import select, func

from src.models import Campaign, CampaignParticipant, CampaignWinner

log = logging.getLogger(__name__)


def campaign_embed(campaign: Campaign, participant_count: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎁 SORTEIO — {campaign.item_name}"[:256],
        description=(
            f"**Prêmio:** {campaign.prize_text}\n\n"
            f"❤️ **Favoritos confirmados**\n**{campaign.favorite_count:,}**\n\n"
            f"🎯 **Meta de favoritos**\n**{campaign.target_favorites:,}**\n\n"
            f"👥 **Participantes:** {participant_count:,}"
        ).replace(",", "."),
        color=0x5865F2,
        url=f"https://www.roblox.com/catalog/{campaign.asset_id}",
    )
    if campaign.thumbnail_url:
        embed.set_image(url=campaign.thumbnail_url)
    footer = f"Item Roblox: {campaign.asset_id} • Inscrição mediante favorito confirmado"
    if not campaign.is_active:
        footer = f"ENCERRADA • {footer}"
    embed.set_footer(text=footer)
    return embed


async def participant_count(session, campaign_id: int) -> int:
    result = await session.execute(
        select(func.count(CampaignParticipant.id)).where(
            CampaignParticipant.campaign_id == campaign_id
        )
    )
    return int(result.scalar() or 0)


async def refresh_campaign_message(bot, campaign_id: int) -> bool:
    async with bot.db.sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None or not campaign.message_id:
            return False
        count = await participant_count(session, campaign_id)
        guild_id, channel_id, message_id = campaign.guild_id, campaign.channel_id, campaign.message_id
        embed = campaign_embed(campaign, count)

    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed, view=CampaignPublicView(bot, campaign_id, campaign.asset_id))
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.exception("Falha ao atualizar painel da campanha %s", campaign_id)
        return False


class RobloxUsernameModal(discord.ui.Modal, title="Confirmar participação"):
    username = discord.ui.TextInput(
        label="Seu username do Roblox",
        placeholder="Digite seu nome de usuário Roblox",
        min_length=3,
        max_length=20,
        required=True,
    )

    def __init__(self, bot, campaign_id: int):
        super().__init__()
        self.bot = bot
        self.campaign_id = campaign_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or not campaign.is_active:
                await interaction.followup.send("Esta campanha não está mais recebendo participantes.", ephemeral=True)
                return
            asset_id = campaign.asset_id
            asset_type_id = campaign.asset_type_id

        try:
            user = await self.bot.roblox.resolve_username(str(self.username.value).strip())
            roblox_id = int(user["id"])
            canonical_username = str(user.get("name") or self.username.value).strip()
            verified = await self.bot.roblox.verify_public_favorite(
                canonical_username, asset_id, asset_type_id, max_pages=20
            )
        except Exception as exc:
            log.warning("Falha ao validar participante Roblox: %s", exc)
            await interaction.followup.send(
                f"Não foi possível validar essa conta Roblox: {exc}", ephemeral=True
            )
            return

        if verified is not True:
            if verified is False:
                message = (
                    "Não encontrei o item nos favoritos públicos dessa conta. "
                    "Favorite o item no Roblox e tente novamente."
                )
            else:
                message = (
                    "O Roblox não permitiu confirmar o favorito com segurança. "
                    "Tente novamente mais tarde; sua inscrição ainda não foi registrada."
                )
            await interaction.followup.send(message, ephemeral=True)
            return

        try:
            async with self.bot.db.sessions() as session:
                campaign = await session.get(Campaign, self.campaign_id)
                if campaign is None or not campaign.is_active:
                    await interaction.followup.send("Esta campanha foi encerrada durante a verificação.", ephemeral=True)
                    return
                existing_discord = await session.execute(
                    select(CampaignParticipant).where(
                        CampaignParticipant.campaign_id == self.campaign_id,
                        CampaignParticipant.discord_user_id == interaction.user.id,
                    )
                )
                if existing_discord.scalar_one_or_none():
                    await interaction.followup.send("Você já está inscrito nesta campanha.", ephemeral=True)
                    return
                existing_roblox = await session.execute(
                    select(CampaignParticipant).where(
                        CampaignParticipant.campaign_id == self.campaign_id,
                        CampaignParticipant.roblox_user_id == roblox_id,
                    )
                )
                if existing_roblox.scalar_one_or_none():
                    await interaction.followup.send(
                        "Essa conta Roblox já está vinculada a uma participação nesta campanha.", ephemeral=True
                    )
                    return
                session.add(CampaignParticipant(
                    campaign_id=self.campaign_id,
                    guild_id=campaign.guild_id,
                    discord_user_id=interaction.user.id,
                    roblox_user_id=roblox_id,
                    roblox_username=canonical_username,
                ))
                await session.commit()
        except Exception:
            log.exception("Erro ao registrar participante da campanha %s", self.campaign_id)
            await interaction.followup.send(
                "Não consegui salvar sua participação. Tente novamente em instantes.", ephemeral=True
            )
            return

        await refresh_campaign_message(self.bot, self.campaign_id)
        await interaction.followup.send(
            f"✅ **Participação confirmada!**\nConta Roblox: **{canonical_username}**\n"
            "Você já está na lista de participantes do sorteio.",
            ephemeral=True,
        )


class ClaimPrizeView(discord.ui.View):
    def __init__(self, bot, winner_id: int, expected_user_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.winner_id = winner_id
        self.expected_user_id = expected_user_id
        button = discord.ui.Button(
            label="Confirmar que aceito o prêmio",
            style=discord.ButtonStyle.success,
            emoji="🎁",
            custom_id=f"campaign:claim:{winner_id}",
        )
        button.callback = self.claim
        self.add_item(button)

    async def claim(self, interaction: discord.Interaction):
        if interaction.user.id != self.expected_user_id:
            await interaction.response.send_message(
                "Este botão pertence a outro participante.", ephemeral=True
            )
            return
        async with self.bot.db.sessions() as session:
            winner = await session.get(CampaignWinner, self.winner_id)
            if winner is None or winner.status != "pending":
                await interaction.response.send_message(
                    "Esta convocação não está mais disponível.", ephemeral=True
                )
                return
            now = datetime.now(timezone.utc)
            deadline = winner.response_deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if now > deadline:
                winner.status = "expired"
                await session.commit()
                await interaction.response.send_message(
                    "O prazo para confirmar o prêmio terminou. Entre em contato com a organização.", ephemeral=True
                )
                return
            winner.status = "confirmed"
            winner.responded_at = now
            campaign_id = winner.campaign_id
            participant = await session.get(CampaignParticipant, winner.participant_id)
            username = participant.roblox_username if participant else "Participante"
            position = winner.position
            await session.commit()

        await interaction.response.send_message(
            f"🎉 Confirmação recebida! Você aceitou o **{position}º lugar**. "
            "A organização entrará em contato para entregar o prêmio.",
            ephemeral=True,
        )
        try:
            async with self.bot.db.sessions() as session:
                campaign = await session.get(Campaign, campaign_id)
                channel_id = campaign.channel_id if campaign else None
                guild_id = campaign.guild_id if campaign else None
            guild = self.bot.get_guild(guild_id) if guild_id else None
            channel = guild.get_channel(channel_id) if guild and channel_id else None
            if channel:
                await channel.send(
                    f"🎉 <@{interaction.user.id}> confirmou o prêmio do **{position}º lugar** "
                    f"(Roblox: **{username}**)."
                )
        except Exception:
            log.exception("Não foi possível anunciar confirmação do vencedor")


class CampaignPublicView(discord.ui.View):
    def __init__(self, bot, campaign_id: int, asset_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.campaign_id = campaign_id
        self.add_item(discord.ui.Button(
            label="Favoritar no Roblox",
            emoji="❤️",
            style=discord.ButtonStyle.link,
            url=f"https://www.roblox.com/catalog/{asset_id}",
            row=0,
        ))
        join = discord.ui.Button(
            label="Já favoritei — participar",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"campaign:{campaign_id}:join",
            row=0,
        )
        join.callback = self.join
        self.add_item(join)
        participants = discord.ui.Button(
            label="Ver participantes",
            emoji="👥",
            style=discord.ButtonStyle.secondary,
            custom_id=f"campaign:{campaign_id}:participants",
            row=1,
        )
        participants.callback = self.show_participants
        self.add_item(participants)
    async def join(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("Use este botão dentro do servidor.", ephemeral=True)
            return
        async with self.bot.db.sessions() as session:
            campaign = await session.get(Campaign, self.campaign_id)
            if campaign is None or not campaign.is_active:
                await interaction.response.send_message("Esta campanha foi encerrada.", ephemeral=True)
                return
            already = await session.execute(
                select(CampaignParticipant).where(
                    CampaignParticipant.campaign_id == self.campaign_id,
                    CampaignParticipant.discord_user_id == interaction.user.id,
                )
            )
            if already.scalar_one_or_none():
                await interaction.response.send_message("Você já está inscrito nesta campanha.", ephemeral=True)
                return
        await interaction.response.send_modal(RobloxUsernameModal(self.bot, self.campaign_id))

    async def show_participants(self, interaction: discord.Interaction):
        async with self.bot.db.sessions() as session:
            result = await session.execute(
                select(CampaignParticipant)
                .where(CampaignParticipant.campaign_id == self.campaign_id)
                .order_by(CampaignParticipant.joined_at)
            )
            rows = list(result.scalars().all())
        if not rows:
            await interaction.response.send_message("Ainda não há participantes confirmados.", ephemeral=True)
            return
        lines = [
            f"`{index}.` **{row.roblox_username}** — <@{row.discord_user_id}>"
            for index, row in enumerate(rows[:40], start=1)
        ]
        if len(rows) > 40:
            lines.append(f"... e mais {len(rows) - 40} participantes.")
        embed = discord.Embed(
            title=f"👥 Participantes — {len(rows)}",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def draw_campaign(bot, campaign_id: int) -> str:
    import secrets

    async with bot.db.sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None or not campaign.is_active:
            return "A campanha não está ativa."
        if campaign.favorite_count < campaign.target_favorites:
            return (
                f"A meta ainda não foi atingida: **{campaign.favorite_count:,}** de "
                f"**{campaign.target_favorites:,}** favoritos.".replace(",", ".")
            )
        already_drawn = await session.execute(
            select(func.count(CampaignWinner.id)).where(
                CampaignWinner.campaign_id == campaign_id,
                CampaignWinner.status.in_(["pending", "confirmed"]),
            )
        )
        if (already_drawn.scalar() or 0) > 0:
            return "Já existem ganhadores aguardando resposta ou confirmados. Use `/campanha_substituir` se necessário."
        participants_result = await session.execute(
            select(CampaignParticipant).where(CampaignParticipant.campaign_id == campaign_id)
        )
        participants = list(participants_result.scalars().all())
        excluded_result = await session.execute(
            select(CampaignWinner.participant_id).where(CampaignWinner.campaign_id == campaign_id)
        )
        excluded = set(excluded_result.scalars().all())
        eligible = [p for p in participants if p.id not in excluded]
        if len(eligible) < campaign.winner_count:
            return (
                f"Participantes elegíveis insuficientes. Disponíveis: **{len(eligible)}**; "
                f"vagas solicitadas: **{campaign.winner_count}**."
            )
        selected = secrets.SystemRandom().sample(eligible, campaign.winner_count)
        deadline_minutes = campaign.response_timeout_minutes
        guild_id = campaign.guild_id
        item_name = campaign.item_name
        prize_text = campaign.prize_text
        created = []
        now = datetime.now(timezone.utc)
        for position, participant in enumerate(selected, start=1):
            winner = CampaignWinner(
                campaign_id=campaign_id,
                participant_id=participant.id,
                position=position,
                attempt=1,
                status="pending",
                drawn_at=now,
                response_deadline=now + timedelta(minutes=deadline_minutes),
            )
            session.add(winner)
            await session.flush()
            created.append((winner.id, participant.discord_user_id, participant.roblox_username, position))
        await session.commit()

    for winner_id, discord_user_id, username, position in created:
        try:
            user = await bot.fetch_user(discord_user_id)
            await user.send(
                f"🎉 Você foi sorteado(a) em **{position}º lugar** na campanha **{item_name}**!\n"
                f"**Prêmio:** {prize_text}\n"
                f"Você tem **{deadline_minutes} minutos** para confirmar o recebimento do prêmio.",
                view=ClaimPrizeView(bot, winner_id, discord_user_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Não foi possível enviar DM ao vencedor %s", discord_user_id)
    return (
        f"🎉 Sorteio realizado! **{len(created)} ganhador(es)** selecionado(s). "
        "As notificações foram enviadas por mensagem privada quando permitido."
    )


async def restore_campaign_views(bot):
    async with bot.db.sessions() as session:
        campaigns_result = await session.execute(
            select(Campaign).where(Campaign.is_active.is_(True), Campaign.message_id.is_not(None))
        )
        campaigns = list(campaigns_result.scalars().all())
        winners_result = await session.execute(
            select(CampaignWinner, CampaignParticipant.discord_user_id)
            .join(CampaignParticipant, CampaignWinner.participant_id == CampaignParticipant.id)
            .where(CampaignWinner.status == "pending")
        )
        winners = list(winners_result.all())
    for campaign in campaigns:
        bot.add_view(CampaignPublicView(bot, campaign.id, campaign.asset_id), message_id=campaign.message_id)
    for winner, discord_user_id in winners:
        bot.add_view(ClaimPrizeView(bot, winner.id, discord_user_id))


async def refresh_campaign_counts(bot):
    async with bot.db.sessions() as session:
        result = await session.execute(select(Campaign).where(Campaign.is_active.is_(True)))
        campaigns = list(result.scalars().all())
    for campaign in campaigns:
        try:
            count = await bot.roblox.get_favorite_count(campaign.asset_id, campaign.favorite_count)
            if count is not None:
                async with bot.db.sessions() as session:
                    current = await session.get(Campaign, campaign.id)
                    if current is not None and current.is_active:
                        current.favorite_count = count
                        await session.commit()
            async with bot.db.sessions() as session:
                winners_result = await session.execute(
                    select(CampaignWinner).where(
                        CampaignWinner.campaign_id == campaign.id,
                        CampaignWinner.status == "pending",
                    )
                )
                now = datetime.now(timezone.utc)
                for winner in winners_result.scalars().all():
                    deadline = winner.response_deadline
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    if now > deadline:
                        winner.status = "expired"
                await session.commit()
            await refresh_campaign_message(bot, campaign.id)
        except Exception:
            log.exception("Falha ao atualizar campanha %s", campaign.id)
