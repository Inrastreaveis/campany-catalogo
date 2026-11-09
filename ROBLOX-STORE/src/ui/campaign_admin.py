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
