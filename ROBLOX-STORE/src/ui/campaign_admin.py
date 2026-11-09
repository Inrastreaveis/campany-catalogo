from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import select, func

from src.models import Campaign, CampaignParticipant, CampaignWinner
from src.security import require_admin_context
from src.services.campaigns import (
    CampaignPublicView, ClaimPrizeView, campaign_embed, draw_campaign, participant_count,
    refresh_campaign_message,
)

log = logging.getLogger(__name__)


async def active_campaign(session, guild_id: int):
    result = await session.execute(
        select(Campaign).where(
            Campaign.guild_id == guild_id,
            Campaign.is_active.is_(True),
        ).order_by(Campaign.created_at.desc())
    )
    return result.scalars().first()


class CampaignAdminView(discord.ui.View):
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
            "Primeiro, escolha o canal onde o painel da campanha será publicado.",
            view=CampaignChannelSelectView(self.bot, self.owner_id, self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Editar campanha", emoji="⚙️", style=discord.ButtonStyle.primary, row=0)
    async def edit_campaign(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaign = await active_campaign(session, self.guild_id)
        if campaign is None:
            await interaction.response.send_message("Não existe campanha ativa para editar.", ephemeral=True)
            return
        await interaction.response.send_modal(EditCampaignModal(self.bot, self.owner_id, self.guild_id, campaign))

    @discord.ui.button(label="Participantes", emoji="👥", style=discord.ButtonStyle.secondary, row=0)
    async def participants(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaign = await active_campaign(session, self.guild_id)
            if campaign is None:
                await interaction.response.send_message("Não existe campanha ativa.", ephemeral=True)
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
        lines = [
            f"`{i}.` **{p.roblox_username}** — <@{p.discord_user_id}> (`{p.roblox_user_id}`)"
            for i, p in enumerate(rows[:60], start=1)
        ]
        if len(rows) > 60:
            lines.append(f"... e mais {len(rows) - 60} participantes.")
        embed = discord.Embed(
            title=f"👥 Participantes confirmados: {len(rows)}",
            description="\n".join(lines)[:4000],
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fazer sorteio", emoji="🏆", style=discord.ButtonStyle.primary, row=1)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaign = await active_campaign(session, self.guild_id)
            campaign_id = campaign.id if campaign else None
        if campaign_id is None:
            await interaction.response.send_message("Não existe campanha ativa.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await draw_campaign(self.bot, campaign_id)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="Sortear substituto", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def substitute(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SubstituteWinnerModal(self.bot, self.owner_id, self.guild_id))

    @discord.ui.button(label="Encerrar campanha", emoji="⛔", style=discord.ButtonStyle.danger, row=1)
    async def close_campaign(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db.sessions() as session:
            campaign = await active_campaign(session, self.guild_id)
            if campaign is None:
                await interaction.response.send_message("Não existe campanha ativa.", ephemeral=True)
                return
            campaign.is_active = False
            campaign_id = campaign.id
            await session.commit()
        await refresh_campaign_message(self.bot, campaign_id)
        await interaction.response.send_message(
            "Campanha encerrada. O painel público foi atualizado e as inscrições foram fechadas.",
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
    timeout = discord.ui.TextInput(label="Prazo para resposta (minutos)", default="60", max_length=5)

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
            timeout = int(str(self.timeout.value).strip())
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
                old = await active_campaign(session, self.guild_id)
                old_id = old.id if old else None
                if old:
                    old.is_active = False
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
            if old_id:
                await refresh_campaign_message(self.bot, old_id)
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
    timeout = discord.ui.TextInput(label="Prazo em minutos (vazio = manter)", required=False, max_length=5)

    def __init__(self, bot, owner_id: int, guild_id: int, campaign: Campaign):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id
        self.item_id.default = str(campaign.asset_id)
        self.target.default = str(campaign.target_favorites)
        self.prize.default = campaign.prize_text
        self.winners.default = str(campaign.winner_count)
        self.timeout.default = str(campaign.response_timeout_minutes)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin_context(interaction, guild_id=self.guild_id, owner_id=self.owner_id, action="editar campanha pelo configcatalogo"):
            return
        values = [str(x.value).strip() for x in (self.item_id, self.target, self.prize, self.winners, self.timeout)]
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
            campaign = await active_campaign(session, self.guild_id)
            if campaign is None:
                await interaction.followup.send("Não existe campanha ativa.", ephemeral=True)
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

    def __init__(self, bot, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.bot, self.owner_id, self.guild_id = bot, owner_id, guild_id

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
            campaign = await active_campaign(session, self.guild_id)
            if campaign is None:
                await interaction.followup.send("Não existe campanha ativa.", ephemeral=True)
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
