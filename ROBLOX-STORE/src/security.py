from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def require_admin_context(
    interaction,
    *,
    guild_id: int | None = None,
    owner_id: int | None = None,
    action: str = "ação administrativa",
) -> bool:
    """Allow only the expected server's current administrators and optional panel owner."""
    actual_guild_id = getattr(interaction, "guild_id", None)
    user = getattr(interaction, "user", None)
    user_id = getattr(user, "id", None)
    permissions = getattr(user, "guild_permissions", None)

    valid_guild = actual_guild_id is not None and (guild_id is None or actual_guild_id == guild_id)
    valid_admin = bool(permissions and getattr(permissions, "administrator", False))
    valid_owner = owner_id is None or user_id == owner_id

    if valid_guild and valid_admin and valid_owner:
        return True

    if not valid_guild:
        reason = "guild ausente ou diferente do servidor esperado"
    elif not valid_admin:
        reason = "usuário sem permissão de administrador"
    else:
        reason = "usuário diferente do administrador que abriu o painel"

    log.warning(
        "Interação administrativa bloqueada: action=%s guild_id=%s user_id=%s reason=%s",
        action,
        actual_guild_id,
        user_id,
        reason,
    )
    if owner_id is None:
        message = "Acesso negado. Apenas administradores deste servidor podem usar esta função."
    else:
        message = "Acesso negado. Apenas o administrador que abriu este painel pode usá-lo, enquanto mantiver permissão de administrador."
    response = interaction.response
    if response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await response.send_message(message, ephemeral=True)
    return False
