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
                        raise RobloxAPIError("O Roblox limitou temporariamente as consultas. Tente novamente.")
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

    async def get_favorite_count(self, asset_id: int, fallback: int | None = None) -> int | None:
        try:
            data = await self._request(
                "GET",
                f"{CATALOG}/v1/favorites/assets/{asset_id}/count",
                _allow_non_object_json=True,
            )

            # Roblox documents this experimental endpoint as a count model,
            # but responses may be scalar or wrapped depending on the edge.
            candidates = [data]
            if isinstance(data, dict):
                candidates.extend([
                    data.get("count"),
                    data.get("favoriteCount"),
                    data.get("data"),
                    data.get("Count"),
                ])
            elif isinstance(data, list):
                candidates.extend(data[:3])

            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate = candidate.get(
                        "count",
                        candidate.get("favoriteCount", candidate.get("Count")),
                    )
                if candidate is None or isinstance(candidate, bool):
                    continue
                try:
                    count = int(candidate)
                    if count >= 0:
                        return count
                except (TypeError, ValueError):
                    continue

            log.warning(
                "Resposta de favoritos sem contador reconhecível para o item %s: %r",
                asset_id,
                data,
            )
            return fallback
        except RobloxAPIError as exc:
            log.warning("Não foi possível atualizar favoritos do item %s: %s", asset_id, exc)
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
        self, username: str, asset_id: int, asset_type_id: int | None, max_pages: int = 5
    ) -> bool | None:
        if not asset_type_id:
            return None
        user = await self.resolve_username(username)
        user_id = int(user["id"])
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 100, "sortOrder": "Desc"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self._request(
                    "GET",
                    f"{CATALOG}/v1/favorites/users/{user_id}/favorites/{asset_type_id}/assets",
                    params=params,
                )
            except RobloxAPIError as exc:
                log.warning("Falha ao consultar favoritos públicos de %s: %s", username, exc)
                return None
            for item in data.get("data") or []:
                try:
                    if int(item.get("id", 0)) == int(asset_id):
                        return True
                except (TypeError, ValueError):
                    continue
            cursor = data.get("nextPageCursor")
            if not cursor:
                return False
        return None
