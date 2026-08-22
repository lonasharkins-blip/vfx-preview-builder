"""Resolve Asset Delivery com parsing tolerante e logs seguros.

O VFX Preview Builder precisa lidar com bibliotecas formadas por assets públicos de
vários criadores. Esta camada mantém as mesmas garantias de segurança do gerador:
a ROBLOX_API_KEY só é enviada para o endpoint Open Cloud autenticado e as texturas
só são baixadas de URLs HTTPS da CDN Roblox.

O endpoint legado/público é tentado sem credenciais apenas como oportunidade. O
caminho principal de fallback é o Open Cloud. Em ambos, aceitamos as duas formas de
payload observadas na família Asset Delivery: ``location`` e ``locations``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

import gallery_layout
import generator


PUBLIC_ASSET_DELIVERY_URL = "https://assetdelivery.roblox.com/v2/assetId/{asset_id}"


def _location_candidates(payload: object) -> tuple[str, ...]:
    """Extrai possíveis URLs sem decidir ainda se são seguras."""

    if not isinstance(payload, dict):
        return ()

    candidates: list[str] = []

    direct = payload.get("location")
    if isinstance(direct, str) and direct:
        candidates.append(direct)

    locations = payload.get("locations")
    if isinstance(locations, list):
        for entry in locations:
            if not isinstance(entry, dict):
                continue
            location = entry.get("location")
            if isinstance(location, str) and location:
                candidates.append(location)

    # Mantém a ordem fornecida pela Roblox, mas remove duplicatas.
    return tuple(dict.fromkeys(candidates))


def _pick_cdn_location(payload: object) -> str | None:
    """Retorna somente uma URL HTTPS validada da CDN Roblox."""

    for location in _location_candidates(payload):
        if generator.RobloxAssetClient._allowed_cdn_url(location):
            return location
    return None


def _safe_candidate_origins(payload: object) -> tuple[str, ...]:
    """Resume candidatos para diagnóstico sem expor path, query ou assinatura."""

    origins: list[str] = []
    for location in _location_candidates(payload):
        try:
            parts = urlsplit(location)
        except ValueError:
            origins.append("<url-invalida>")
            continue

        scheme = parts.scheme.lower() or "<sem-scheme>"
        hostname = (parts.hostname or "<sem-host>").lower().rstrip(".")
        origins.append(f"{scheme}://{hostname}")

    return tuple(dict.fromkeys(origins))


def _payload_keys(payload: object) -> tuple[str, ...]:
    """Retorna apenas nomes de campos, nunca valores potencialmente sensíveis."""

    if not isinstance(payload, dict):
        return ()
    return tuple(sorted(str(key) for key in payload.keys()))


async def _read_delivery_payload(response: aiohttp.ClientResponse) -> object:
    """Lê uma resposta pequena do Asset Delivery e valida o JSON."""

    raw = await response.content.read(generator.MAX_DELIVERY_RESPONSE_BYTES + 1)
    if not raw or len(raw) > generator.MAX_DELIVERY_RESPONSE_BYTES:
        raise generator.AssetUnavailableError("resposta inválida do Asset Delivery")

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise generator.AssetUnavailableError(
            "resposta inválida do Asset Delivery"
        ) from error


async def _public_delivery_location(
    client: generator.RobloxAssetClient,
    asset_id: int,
) -> str | None:
    """Tenta o endpoint público sem enviar cookie ou API key."""

    assert client.session is not None
    url = PUBLIC_ASSET_DELIVERY_URL.format(asset_id=asset_id)

    for attempt in range(client.config.http_retries):
        try:
            async with client.session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(
                    total=client.config.delivery_timeout_seconds
                ),
                allow_redirects=False,
            ) as response:
                if response.status == 200:
                    try:
                        payload = await _read_delivery_payload(response)
                    except generator.AssetUnavailableError:
                        return None
                    return _pick_cdn_location(payload)

                if response.status == 429 or response.status >= 500:
                    if attempt + 1 < client.config.http_retries:
                        await client._sleep_for_retry(response, attempt)
                        continue
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt + 1 >= client.config.http_retries:
                return None
            await asyncio.sleep(min(2**attempt, 10))

    return None


async def _open_cloud_delivery_location(
    client: generator.RobloxAssetClient,
    asset_id: int,
) -> str:
    """Resolve pelo Open Cloud aceitando ``location`` e ``locations``.

    Esta função reproduz o tratamento de status/retries do gerador original. A
    diferença é somente o parser de HTTP 200 e um diagnóstico seguro quando a
    Roblox devolve um candidato que não passa na whitelist.
    """

    assert client.session is not None
    url = generator.ASSET_DELIVERY_URL.format(asset_id=asset_id)

    for attempt in range(client.config.http_retries):
        try:
            async with client.session.get(
                url,
                headers={
                    "Accept": "application/json",
                    "x-api-key": client.api_key,
                },
                timeout=aiohttp.ClientTimeout(
                    total=client.config.delivery_timeout_seconds
                ),
                allow_redirects=False,
            ) as response:
                if response.status == 200:
                    payload = await _read_delivery_payload(response)
                    location = _pick_cdn_location(payload)
                    if location is not None:
                        return location

                    # Nunca registra a URL assinada completa. Os nomes dos campos e
                    # os origins são suficientes para descobrir mudanças de formato
                    # ou de hostname sem vazar token da CDN.
                    generator.LOGGER.warning(
                        "Asset %s: Open Cloud HTTP 200 sem CDN utilizável; "
                        "campos=%s origins=%s",
                        asset_id,
                        _payload_keys(payload) or ("<nenhum>",),
                        _safe_candidate_origins(payload) or ("<nenhum>",),
                    )
                    raise generator.AssetUnavailableError(
                        "localização CDN inválida"
                    )

                if response.status == 401:
                    raise generator.RobloxAuthError(
                        "A ROBLOX_API_KEY foi recusada."
                    )
                if response.status == 403:
                    raise generator.AssetUnavailableError("sem permissão")
                if response.status in {404, 410}:
                    raise generator.AssetUnavailableError("textura indisponível")
                if response.status == 429 or response.status >= 500:
                    if attempt + 1 < client.config.http_retries:
                        await client._sleep_for_retry(response, attempt)
                        continue
                    raise generator.NetworkError(
                        f"Asset Delivery HTTP {response.status}"
                    )
                raise generator.AssetUnavailableError(
                    f"Asset Delivery HTTP {response.status}"
                )

        except (
            generator.RobloxAuthError,
            generator.AssetUnavailableError,
            generator.NetworkError,
        ):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            if attempt + 1 >= client.config.http_retries:
                raise generator.NetworkError(
                    "Falha de rede no Asset Delivery."
                ) from error
            await asyncio.sleep(min(2**attempt, 10))

    raise generator.NetworkError("Falha no Asset Delivery.")


async def _delivery_location(
    client: generator.RobloxAssetClient,
    asset_id: int,
) -> str:
    """Tenta público sem segredo e usa Open Cloud autenticado como caminho seguro."""

    public_location = await _public_delivery_location(client, asset_id)
    if public_location is not None:
        return public_location

    return await _open_cloud_delivery_location(client, asset_id)


def install() -> None:
    """Instala o layout e o resolvedor sem alterar o restante do builder."""

    gallery_layout.install()
    generator.RobloxAssetClient._delivery_location = _delivery_location

    # Inclui esta camada no fingerprint para impedir reaproveitamento de páginas
    # produzidas antes desta correção.
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
