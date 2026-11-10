import asyncio
import json
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)
CATALOG = "https://catalog.roblox.com"
THUMBNAILS = "https://thumbnails.roblox.com"
USERS = "https://users.roblox.com"


class RobloxAPIError(Exception):
    pass


class RobloxNotFound(RobloxAPIError):
    pass


class RobloxRateLimited(RobloxAPIError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RobloxClient:
    """Cliente público de leitura para o catálogo, thumbnails e favoritos Roblox."""

    def __init__(self, timeout_seconds: int = 15):
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(4)

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": "RobloxStoreDiscordBot/1.0"},
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        # Some Roblox experimental endpoints return a scalar or list instead
        # of an object. Only callers that explicitly opt in may receive it.
        allow_non_object_json = bool(kwargs.pop("_allow_non_object_json", False))
        await self.start()
        assert self.session is not None
        async with self._semaphore:
            try:
                async with self.session.request(method, url, **kwargs) as response:
                    if response.status == 403 and response.headers.get("x-csrf-token"):
                        csrf = response.headers["x-csrf-token"]
                        headers = dict(kwargs.pop("headers", {}) or {})
                        headers["x-csrf-token"] = csrf
                        async with self.session.request(method, url, headers=headers, **kwargs) as retry:
                            body = await retry.text()
                            if retry.status >= 400:
                                raise RobloxAPIError(f"Roblox respondeu HTTP {retry.status}: {body[:200]}")
                            return self._decode_json(body, allow_non_object=allow_non_object_json)
                    body = await response.text()
                    if response.status == 404:
                        raise RobloxNotFound("O item ou usuário não foi encontrado no Roblox.")
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            retry_after_seconds = float(retry_after) if retry_after else None
                        except (TypeError, ValueError):
                            retry_after_seconds = None
                        raise RobloxRateLimited(
                            "O Roblox limitou temporariamente as consultas.",
                            retry_after=retry_after_seconds,
                        )
                    if response.status >= 400:
                        raise RobloxAPIError(f"Roblox respondeu HTTP {response.status}: {body[:200]}")
                    return self._decode_json(body, allow_non_object=allow_non_object_json)
            except asyncio.TimeoutError as exc:
                raise RobloxAPIError("A API do Roblox demorou demais para responder.") from exc
            except aiohttp.ClientError as exc:
                raise RobloxAPIError(f"Falha de comunicação com o Roblox: {exc}") from exc

    @staticmethod
    def _decode_json(body: str, allow_non_object: bool = False) -> Any:
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise RobloxAPIError("O Roblox retornou uma resposta inválida.") from exc
        if not isinstance(data, dict) and not allow_non_object:
            raise RobloxAPIError("Formato inesperado de resposta do Roblox.")
        return data

    async def get_asset_details(self, asset_id: int) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"{CATALOG}/v1/catalog/items/details",
            json={"items": [{"itemType": "Asset", "id": int(asset_id)}]},
            headers={"Content-Type": "application/json"},
        )
        rows = data.get("data") or []
        if not rows:
            raise RobloxNotFound(f"Não foi possível localizar o ativo {asset_id} no catálogo.")
        item = rows[0]
        if item.get("id") and int(item["id"]) != int(asset_id):
            raise RobloxNotFound("O Roblox retornou um produto diferente do ID solicitado.")
        return item

    @staticmethod
    def _extract_favorite_count(payload: Any) -> int | None:
        """Extract a non-negative favorite count from known Roblox response shapes."""
        count_keys = (
            "count",
            "favorites",
            "favoriteCount",
            "favoritesCount",
            "totalFavorites",
            "total_favorites",
            "favorite_count",
            "totalCount",
            "Count",
            "value",
        )
        envelope_keys = ("data", "result", "response")

        if isinstance(payload, bool) or payload is None:
            return None

        if isinstance(payload, int):
            return payload if payload >= 0 else None

        if isinstance(payload, float):
            return int(payload) if payload >= 0 and payload.is_integer() else None

        if isinstance(payload, str):
            value = payload.strip()
            if value.isdecimal():
                return int(value)
            return None

        if isinstance(payload, dict):
            for key in count_keys:
                if key in payload:
                    found = RobloxClient._extract_favorite_count(payload[key])
                    if found is not None:
                        return found
            for key in envelope_keys:
                if key in payload:
                    found = RobloxClient._extract_favorite_count(payload[key])
                    if found is not None:
                        return found
            return None

        if isinstance(payload, list):
            for item in payload:
                found = RobloxClient._extract_favorite_count(item)
                if found is not None:
                    return found

        return None

    async def get_favorite_count(self, asset_id: int, fallback: int | None = None) -> int | None:
        # Retry rate-limited count requests. This is useful when several
        # active campaign panels are refreshed in the same cycle.
        for attempt in range(3):
            try:
                data = await self._request(
                    "GET",
                    f"{CATALOG}/v1/favorites/assets/{asset_id}/count",
                    _allow_non_object_json=True,
                )
                count = self._extract_favorite_count(data)
                if count is not None:
                    log.info(
                        "Favoritos atualizados: asset_id=%s count=%s tentativa=%s",
                        asset_id, count, attempt + 1,
                    )
                    return count

                shape = (
                    f"object keys={list(data.keys())[:20]}"
                    if isinstance(data, dict)
                    else f"type={type(data).__name__}"
                )
                log.warning(
                    "Resposta de favoritos sem contador reconhecível: asset_id=%s (%s) body=%r",
                    asset_id, shape, data,
                )
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                return fallback

            except RobloxRateLimited as exc:
                if attempt >= 2:
                    log.warning(
                        "Limite do Roblox persistiu após 3 tentativas: asset_id=%s; mantendo último valor=%s",
                        asset_id, fallback,
                    )
                    return fallback

                delay = exc.retry_after
                if delay is None or delay <= 0:
                    delay = 1.5 * (attempt + 1)
                delay = min(max(delay, 0.75), 8.0)
                log.warning(
                    "Roblox HTTP 429 para asset_id=%s; nova tentativa %s/3 em %.1fs",
                    asset_id, attempt + 2, delay,
                )
                await asyncio.sleep(delay)

            except RobloxAPIError as exc:
                log.warning(
                    "Falha ao atualizar favoritos: asset_id=%s erro=%s; mantendo último valor=%s",
                    asset_id, exc, fallback,
                )
                return fallback

        return fallback

    async def get_thumbnail(self, asset_id: int) -> str | None:
        data = await self._request(
            "GET",
            f"{THUMBNAILS}/v1/assets",
            params={"assetIds": str(asset_id), "size": "420x420", "format": "Png", "isCircular": "false"},
        )
        rows = data.get("data") or []
        return rows[0].get("imageUrl") if rows else None

    async def get_product(self, asset_id: int) -> dict[str, Any]:
        details = await self.get_asset_details(asset_id)
        favorites = await self.get_favorite_count(asset_id, details.get("favoriteCount"))
        try:
            thumbnail = await self.get_thumbnail(asset_id)
        except RobloxAPIError as exc:
            log.warning("Não foi possível obter thumbnail do item %s: %s", asset_id, exc)
            thumbnail = None
        price = details.get("price")
        asset_type = details.get("assetType")
        return {
            "asset_id": int(asset_id),
            "name": str(details.get("name") or f"Item {asset_id}"),
            "description": str(details.get("description") or ""),
            "price_robux": int(price) if price is not None else None,
            "favorite_count": int(favorites) if favorites is not None else None,
            "thumbnail_url": thumbnail,
            "asset_type_id": int(asset_type) if asset_type is not None else None,
            "is_off_sale": bool(details.get("isOffSale", False)),
        }

    async def resolve_username(self, username: str) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"{USERS}/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": True},
            headers={"Content-Type": "application/json"},
        )
        rows = data.get("data") or []
        if not rows:
            raise RobloxNotFound("Não encontrei esse username Roblox.")
        return rows[0]

    async def verify_public_favorite(
        self,
        username: str,
        asset_id: int,
        asset_type_id: int | None,
        max_pages: int = 20,
    ) -> bool | None:
        """
        Confere se o ativo está na lista pública de favoritos do usuário.

        True  = encontrado na lista.
        False = lista consultada até o fim e item não encontrado.
        None  = não foi possível concluir com segurança (API/limite/paginação).
        """
        if asset_type_id is None:
            log.warning(
                "Verificação de favorito impossível: asset_type_id ausente "
                "(username=%s asset_id=%s)",
                username,
                asset_id,
            )
            return None

        try:
            asset_type_id = int(asset_type_id)
            if asset_type_id <= 0:
                raise ValueError("asset_type_id deve ser positivo")
        except (TypeError, ValueError):
            log.warning(
                "asset_type_id inválido na verificação de favorito: "
                "username=%s asset_id=%s asset_type_id=%r",
                username,
                asset_id,
                asset_type_id,
            )
            return None

        try:
            user = await self.resolve_username(username)
            user_id = int(user["id"])
        except (RobloxAPIError, KeyError, TypeError, ValueError) as exc:
            log.warning(
                "Não foi possível resolver o usuário Roblox %s para verificar "
                "o favorito do item %s: %s",
                username,
                asset_id,
                exc,
            )
            return None

        cursor: str | None = None
        seen_cursors: set[str] = set()

        for page_number in range(1, max(1, int(max_pages)) + 1):
            # The public favorites endpoint uses cursor pagination. Keep only
            # documented/used pagination parameters; sortOrder is not needed.
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor

            try:
                data = await self._request(
                    "GET",
                    f"{CATALOG}/v1/favorites/users/{user_id}/favorites/{asset_type_id}/assets",
                    params=params,
                )
            except RobloxAPIError as exc:
                log.warning(
                    "Falha na API de favoritos: username=%s user_id=%s "
                    "asset_id=%s asset_type_id=%s página=%s: %s",
                    username,
                    user_id,
                    asset_id,
                    asset_type_id,
                    page_number,
                    exc,
                )
                return None

            rows = data.get("data")
            if not isinstance(rows, list):
                log.warning(
                    "Resposta inesperada ao listar favoritos: username=%s "
                    "user_id=%s asset_type_id=%s página=%s chaves=%s",
                    username,
                    user_id,
                    asset_type_id,
                    page_number,
                    list(data.keys())[:20] if isinstance(data, dict) else type(data).__name__,
                )
                return None

            for item in rows:
                if not isinstance(item, dict):
                    continue
                raw_item_id = item.get("id", item.get("assetId", item.get("asset_id")))
                try:
                    if raw_item_id is not None and int(raw_item_id) == int(asset_id):
                        log.info(
                            "Favorito confirmado: username=%s user_id=%s "
                            "asset_id=%s asset_type_id=%s página=%s",
                            username,
                            user_id,
                            asset_id,
                            asset_type_id,
                            page_number,
                        )
                        return True
                except (TypeError, ValueError):
                    continue

            next_cursor = data.get("nextPageCursor") or data.get("nextPageToken")
            if not next_cursor:
                log.info(
                    "Favorito não encontrado após consultar todas as páginas: "
                    "username=%s user_id=%s asset_id=%s asset_type_id=%s páginas=%s",
                    username,
                    user_id,
                    asset_id,
                    asset_type_id,
                    page_number,
                )
                return False

            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors or next_cursor == cursor:
                log.warning(
                    "Paginação repetida ao consultar favoritos: username=%s "
                    "user_id=%s asset_id=%s asset_type_id=%s página=%s",
                    username,
                    user_id,
                    asset_id,
                    asset_type_id,
                    page_number,
                )
                return None

            seen_cursors.add(next_cursor)
            cursor = next_cursor

            # Small spacing reduces bursts when a user has many favorites.
            if page_number < max_pages:
                await asyncio.sleep(0.15)

        log.warning(
            "Limite de páginas atingido sem conseguir confirmar o favorito: "
            "username=%s user_id=%s asset_id=%s asset_type_id=%s max_pages=%s",
            username,
            user_id,
            asset_id,
            asset_type_id,
            max_pages,
        )
        return None
