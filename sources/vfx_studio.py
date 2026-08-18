"""Parser pequeno e independente para o VFXData.json do VFX Studio."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

SOURCE_KEY = "vfx-studio"
SOURCE_NAME = "VFX Studio"
SUPPORTED_GRID_SIZES = frozenset({2, 4, 8})
INTERFACE_FILTER_NAMES = frozenset({"All", "User", "Static", "Flipbook"})

# Mantém a ordem de categorias usada atualmente pelo /ro-flipbooks library.
CATEGORY_ORDER = (
    "Other",
    "Smoke",
    "Lightning",
    "Slashes",
    "Fire",
    "Impact",
    "Text",
    "Flare",
    "Water",
    "Circle",
    "Star",
    "Swirl",
    "Energy",
    "Form",
    "Ground",
    "Spec",
)
CATEGORY_ORDER_INDEX = {
    category: position for position, category in enumerate(CATEGORY_ORDER)
}


class InvalidCatalogError(RuntimeError):
    """O VFXData.json não possui a estrutura esperada."""


@dataclass(frozen=True, slots=True)
class VFXItem:
    """Um flipbook animado normalizado do catálogo do VFX Studio."""

    asset_id: int
    name: str
    category: str
    grid: int
    resolution: int | None
    record_hash: str

    @property
    def frame_count(self) -> int:
        return self.grid * self.grid


@dataclass(frozen=True, slots=True)
class VFXCatalog:
    """Catálogo já validado, ordenado e separado por categoria."""

    categories: dict[str, tuple[VFXItem, ...]]
    total_unique_flipbooks: int
    catalog_version: str


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:limit]


def _record_hash(asset_id: int, record: dict[str, object]) -> str:
    """Detecta mudanças relevantes no registro sem depender do JSON bruto inteiro."""

    canonical = json.dumps(
        {"asset_id": asset_id, "record": record},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_catalog_bytes(raw_catalog: bytes) -> VFXCatalog:
    """Converte o VFXData.json bruto em apenas flipbooks 2x2/4x4/8x8."""

    if not raw_catalog:
        raise InvalidCatalogError("O catálogo está vazio.")
    try:
        payload = json.loads(raw_catalog)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise InvalidCatalogError("O catálogo não contém JSON válido.") from error

    catalog_version = f"sha256-{hashlib.sha256(raw_catalog).hexdigest()[:20]}"
    return parse_catalog(payload, catalog_version=catalog_version)


def parse_catalog(payload: object, *, catalog_version: str) -> VFXCatalog:
    if not isinstance(payload, dict):
        raise InvalidCatalogError("O catálogo não é um objeto JSON.")

    categories: dict[str, list[VFXItem]] = {}
    unique_asset_ids: set[int] = set()

    for raw_asset_id, raw_record in payload.items():
        asset_id = _positive_int(raw_asset_id)
        if asset_id is None or not isinstance(raw_record, dict):
            continue

        grid = _positive_int(raw_record.get("Grid"))
        if grid not in SUPPORTED_GRID_SIZES:
            # Esta primeira versão gera somente a biblioteca de Flipbooks.
            continue

        raw_keywords = raw_record.get("Keywords")
        if not isinstance(raw_keywords, list):
            continue

        record_categories: list[str] = []
        for raw_keyword in raw_keywords:
            category = _clean_text(raw_keyword, 100)
            if (
                category is not None
                and category not in INTERFACE_FILTER_NAMES
                and category not in record_categories
            ):
                record_categories.append(category)
        if not record_categories:
            continue

        name = _clean_text(raw_record.get("Name"), 200) or f"Asset {asset_id}"
        resolution = _positive_int(raw_record.get("Resolution"))
        fingerprint = _record_hash(asset_id, raw_record)

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
        unique_asset_ids.add(asset_id)

    if not categories:
        raise InvalidCatalogError("Nenhum flipbook 2x2, 4x4 ou 8x8 foi encontrado.")

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
        total_unique_flipbooks=len(unique_asset_ids),
        catalog_version=catalog_version,
    )
