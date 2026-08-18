"""Parser independente para o catálogo público do ZonitoVisuals."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .vfx_studio import (
    CATEGORY_ORDER,
    CATEGORY_ORDER_INDEX,
    INTERFACE_FILTER_NAMES,
    SUPPORTED_GRID_SIZES,
    InvalidCatalogError,
    VFXCatalog,
    VFXItem,
)

SOURCE_KEY = "zonito-visuals"
SOURCE_NAME = "ZonitoVisuals"
CATALOG_URL = (
    "https://raw.githubusercontent.com/ZonitoVFX/ZonitoVisuals/"
    "refs/heads/main/ZonitoVisuals3.2Textures"
)
SOURCE_PARSER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _asset_id(value: object) -> int | None:
    parsed = _positive_int(value)
    if parsed is not None:
        return parsed
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*rbxassetid://(\d+)\s*", value, flags=re.IGNORECASE)
    return _positive_int(match.group(1)) if match else None


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] if cleaned else None


def _record_hash(asset_id: int, name: str, record: dict[str, object]) -> str:
    canonical = json.dumps(
        {"asset_id": asset_id, "name": name, "record": record},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_catalog_bytes(raw_catalog: bytes) -> VFXCatalog:
    """Converte somente os flipbooks 2x2/4x4/8x8 do ZonitoVisuals."""

    if not raw_catalog:
        raise InvalidCatalogError("O catálogo do ZonitoVisuals está vazio.")
    try:
        payload = json.loads(raw_catalog)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise InvalidCatalogError(
            "O catálogo do ZonitoVisuals não contém JSON válido."
        ) from error

    catalog_version = f"sha256-{hashlib.sha256(raw_catalog).hexdigest()[:20]}"
    return parse_catalog(payload, catalog_version=catalog_version)


def parse_catalog(payload: object, *, catalog_version: str) -> VFXCatalog:
    if not isinstance(payload, dict):
        raise InvalidCatalogError("O catálogo do ZonitoVisuals não é um objeto JSON.")

    raw_textures = payload.get("Textures")
    if not isinstance(raw_textures, dict):
        raise InvalidCatalogError(
            "O catálogo do ZonitoVisuals não contém a seção Textures."
        )

    categories: dict[str, list[VFXItem]] = {}
    seen_asset_ids: set[int] = set()

    for raw_name, raw_record in raw_textures.items():
        if not isinstance(raw_record, dict):
            continue

        asset_id = _asset_id(raw_record.get("Texture"))
        if asset_id is None or asset_id in seen_asset_ids:
            continue

        raw_type = _clean_text(raw_record.get("Type"), 32)
        if raw_type is None:
            continue
        normalized_type = raw_type.casefold().replace("×", "x").replace(" ", "")
        grid_match = re.fullmatch(r"([248])x\1", normalized_type)
        if grid_match is None:
            # Statics pertencem à outra seção da biblioteca do bot; este builder
            # externo pré-gera somente os flipbooks animados.
            continue
        grid = int(grid_match.group(1))
        if grid not in SUPPORTED_GRID_SIZES:
            continue

        record_categories: list[str] = []
        raw_tags = raw_record.get("Tags")
        if isinstance(raw_tags, list):
            for raw_tag in raw_tags:
                category = _clean_text(raw_tag, 100)
                if (
                    category is not None
                    and category not in INTERFACE_FILTER_NAMES
                    and category not in record_categories
                ):
                    record_categories.append(category)
        if not record_categories:
            record_categories.append("Other")

        name = _clean_text(raw_name, 200) or f"Asset {asset_id}"
        resolution = _positive_int(raw_record.get("Resolution"))
        fingerprint = _record_hash(asset_id, name, raw_record)

        for category in record_categories:
            categories.setdefault(category, []).append(
                VFXItem(
                    asset_id=asset_id,
                    name=name,
                    category=category,
                    grid=grid,
                    resolution=resolution,
                    record_hash=fingerprint,
                )
            )
        seen_asset_ids.add(asset_id)

    if not categories:
        raise InvalidCatalogError(
            "O catálogo do ZonitoVisuals não contém flipbooks 2x2, 4x4 ou 8x8."
        )

    order_length = len(CATEGORY_ORDER)
    sorted_categories = sorted(
        categories,
        key=lambda category: (
            CATEGORY_ORDER_INDEX.get(category, order_length),
            category.casefold(),
        ),
    )
    normalized: dict[str, tuple[VFXItem, ...]] = {}
    for category in sorted_categories:
        items = categories[category]
        items.sort(key=lambda item: (item.name.casefold(), item.asset_id))
        normalized[category] = tuple(items)

    return VFXCatalog(
        categories=normalized,
        total_unique_flipbooks=len(seen_asset_ids),
        catalog_version=catalog_version,
    )
