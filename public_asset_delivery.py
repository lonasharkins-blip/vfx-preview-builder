"""Resolve assets públicos antes de recorrer ao Open Cloud autenticado.

As bibliotecas VFX contêm texturas públicas de muitos criadores diferentes. Uma
ROBLOX_API_KEY válida pode não ter permissão Open Cloud sobre um asset de outro
criador, mesmo quando esse asset é publicamente entregável pela Roblox.

Este módulo mantém o fluxo Open Cloud original como fallback, mas tenta primeiro o
Asset Delivery público v2 sem cookie e sem API key. A chave nunca é enviada para
assetdelivery.roblox.com nem para a CDN.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import aiohttp

import gallery_layout
import generator


PUBLIC_ASSET_DELIVERY_URL = "https://assetdelivery.roblox.com/v2/assetId/{asset_id}"
_ORIGINAL_DELIVERY_LOCATION = generator.RobloxAssetClient._delivery_location


def _pick_public_location(payload: object) -> str | None:
    """Extrai somente uma URL HTTPS da CDN Roblox de uma resposta pública válida."""

    if not isinstance(payload, dict):
        return None

    # O endpoint v2 normalmente retorna uma lista `locations`. Aceitar também
    # `location` torna o parser tolerante sem relaxar a validação de domínio.
    direct = payload.get("location")
    if isinstance(direct, str) and generator.RobloxAssetClient._allowed_cdn_url(direct):
        return direct

    locations = payload.get("locations")
    if not isinstance(locations, list):
        return None

    for entry in locations:
        if not isinstance(entry, dict):
            continue
        location = entry.get("location")
        if isinstance(location, str) and generator.RobloxAssetClient._allowed_cdn_url(location):
            return location
    return None


async def _public_delivery_location(
    client: generator.RobloxAssetClient,
    asset_id: int,
) -> str | None:
    """Tenta resolver um asset público sem usar nenhuma credencial."""

    assert client.session is not None
    url = PUBLIC_ASSET_DELIVERY_URL.format(asset_id=asset_id)

    for attempt in range(client.config.http_retries):
        try:
            async with client.session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=client.config.delivery_timeout_seconds),
                allow_redirects=False,
            ) as response:
                if response.status == 200:
                    raw = await response.content.read(generator.MAX_DELIVERY_RESPONSE_BYTES + 1)
                    if not raw or len(raw) > generator.MAX_DELIVERY_RESPONSE_BYTES:
                        return None
                    try:
                        payload: Any = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeError):
                        return None
                    return _pick_public_location(payload)

                if response.status == 429 or response.status >= 500:
                    if attempt + 1 < client.config.http_retries:
                        await client._sleep_for_retry(response, attempt)
                        continue
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt + 1 >= client.config.http_retries:
                return None
            await asyncio.sleep(min(2 ** attempt, 10))

    return None


async def _delivery_location(
    client: generator.RobloxAssetClient,
    asset_id: int,
) -> str:
    """Usa público v2 primeiro e preserva o Open Cloud original como fallback."""

    public_location = await _public_delivery_location(client, asset_id)
    if public_location is not None:
        return public_location

    # Nenhuma URL ou segredo entra no log. O método original continua responsável
    # por autenticação, retries e mensagens de erro quando o fallback também falha.
    generator.LOGGER.debug(
        "Asset %s não foi resolvido pelo Asset Delivery público; tentando Open Cloud.",
        asset_id,
    )
    return await _ORIGINAL_DELIVERY_LOCATION(client, asset_id)


def install() -> None:
    """Instala o layout e, em seguida, a resolução pública de assets."""

    gallery_layout.install()
    generator.RobloxAssetClient._delivery_location = _delivery_location

    # Inclui esta camada no fingerprint para que um novo teste não reutilize uma
    # página gerada antes da correção de disponibilidade.
    resolver_source = Path(__file__).read_bytes()
    generator.GENERATOR_SOURCE_SHA256 = hashlib.sha256(
        generator.GENERATOR_SOURCE_SHA256.encode("ascii")
        + b"\0public-asset-delivery\0"
        + resolver_source
    ).hexdigest()


def main() -> int:
    install()
    return generator.main()


if __name__ == "__main__":
    raise SystemExit(main())
