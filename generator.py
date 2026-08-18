"""Pré-gera páginas GIF do VFX Studio e publica em GitHub Releases.

Este programa é propositalmente independente do Discord bot. Ele lê configuração
não sensível de preview_config.json e recebe credenciais somente por variáveis de
ambiente do GitHub Actions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import statistics
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from sources.vfx_studio import (
    SOURCE_KEY,
    SOURCE_NAME,
    InvalidCatalogError,
    VFXCatalog,
    VFXItem,
    parse_catalog_bytes,
)

LOGGER = logging.getLogger("vfx_preview_builder")
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "preview_config.json"
# Entra no hash de cada página para que qualquer mudança real no código do
# gerador invalide automaticamente os GIFs antigos. Assim não dependemos de
# lembrar de aumentar manualmente generator_version a cada ajuste visual.
GENERATOR_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
ASSET_DELIVERY_URL = "https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}"
MAX_DELIVERY_RESPONSE_BYTES = 64 * 1024
DOWNLOAD_CHUNK_SIZE = 64 * 1024
SUPPORTED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF", "BMP", "TGA"})
GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_UPLOAD_BASE = "https://uploads.github.com"
RELEASE_TAG_TEST = "vfx-previews-test"
RELEASE_TAG_FULL = "vfx-previews"
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
GITHUB_RELEASE_ASSET_HARD_LIMIT = 1000
GITHUB_RELEASE_ASSET_WARNING_LIMIT = 900


class BuilderError(RuntimeError):
    """Falha esperada e legível do gerador."""


class MissingConfigurationError(BuilderError):
    pass


class NetworkError(BuilderError):
    pass


class RobloxAuthError(BuilderError):
    pass


class AssetUnavailableError(BuilderError):
    pass


class InvalidSpritesheetError(BuilderError):
    pass


@dataclass(frozen=True, slots=True)
class BuilderConfig:
    generator_version: str
    source_key: str
    catalog_url: str
    items_per_page: int
    columns: int
    rows: int
    page_header_height: int
    card_width: int
    card_header_height: int
    preview_height: int
    content_padding: int
    gap: int
    margin: int
    fps_4_frames: int
    fps_16_frames: int
    fps_64_frames: int
    max_output_frames: int
    gif_palette_colors: int
    gif_alpha_threshold: int
    max_concurrent_downloads: int
    max_texture_bytes: int
    max_texture_dimension: int
    max_texture_pixels: int
    catalog_timeout_seconds: int
    delivery_timeout_seconds: int
    download_timeout_seconds: int
    http_retries: int
    significant_failure_ratio: float
    large_gif_warning_bytes: int

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "BuilderConfig":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MissingConfigurationError("preview_config.json está inválido.") from error
        try:
            config = cls(**payload)
        except TypeError as error:
            raise MissingConfigurationError("preview_config.json está incompleto.") from error
        config.validate()
        return config

    def validate(self) -> None:
        if self.source_key != SOURCE_KEY:
            raise MissingConfigurationError("Esta versão aceita somente VFX Studio.")
        if self.items_per_page != self.columns * self.rows:
            raise MissingConfigurationError("items_per_page precisa coincidir com columns × rows.")
        if (self.items_per_page, self.columns, self.rows) != (6, 2, 3):
            raise MissingConfigurationError("A configuração atual deve usar exatamente 6 itens em 2×3.")
        if self.page_header_height < 24:
            raise MissingConfigurationError("page_header_height inválido.")
        if self.card_width < 120:
            raise MissingConfigurationError("card_width inválido.")
        if self.card_header_height < 24:
            raise MissingConfigurationError("card_header_height inválido.")
        if self.preview_height < 120:
            raise MissingConfigurationError("preview_height inválido.")
        if self.max_output_frames < 1 or self.max_output_frames > 64:
            raise MissingConfigurationError("max_output_frames deve ficar entre 1 e 64.")
        if not (2 <= self.gif_palette_colors <= 255):
            raise MissingConfigurationError("gif_palette_colors deve ficar entre 2 e 255.")
        if not (0 <= self.gif_alpha_threshold <= 255):
            raise MissingConfigurationError("gif_alpha_threshold inválido.")
        if self.max_concurrent_downloads < 1 or self.max_concurrent_downloads > 12:
            raise MissingConfigurationError("max_concurrent_downloads deve ficar entre 1 e 12.")
        if not (0.0 <= self.significant_failure_ratio <= 1.0):
            raise MissingConfigurationError("significant_failure_ratio inválido.")

    def render_fingerprint(self) -> dict[str, Any]:
        """Somente parâmetros que realmente alteram os pixels/timing do GIF."""

        return {
            "generator_version": self.generator_version,
            "generator_source_sha256": GENERATOR_SOURCE_SHA256,
            "items_per_page": self.items_per_page,
            "columns": self.columns,
            "rows": self.rows,
            "page_header_height": self.page_header_height,
            "card_width": self.card_width,
            "card_header_height": self.card_header_height,
            "preview_height": self.preview_height,
            "content_padding": self.content_padding,
            "gap": self.gap,
            "margin": self.margin,
            "fps": {
                "4": self.fps_4_frames,
                "16": self.fps_16_frames,
                "64": self.fps_64_frames,
            },
            "max_output_frames": self.max_output_frames,
            "gif_palette_colors": self.gif_palette_colors,
            "gif_alpha_threshold": self.gif_alpha_threshold,
            "timeline": "max-visible-frame-count-capped",
            "transparency": "gif-binary-alpha-threshold",
        }


@dataclass(frozen=True, slots=True)
class Environment:
    roblox_api_key: str
    github_token: str
    github_repository: str
    github_sha: str
    mode: str
    test_page_limit: int

    @property
    def release_tag(self) -> str:
        return RELEASE_TAG_TEST if self.mode == "test" else RELEASE_TAG_FULL

    @property
    def release_name(self) -> str:
        return "VFX previews (test)" if self.mode == "test" else "VFX previews"

    @classmethod
    def load(cls) -> "Environment":
        required = ("ROBLOX_API_KEY", "GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_SHA")
        missing = [name for name in required if not os.getenv(name, "").strip()]
        if missing:
            raise MissingConfigurationError(
                "Faltam variáveis obrigatórias: " + ", ".join(missing)
            )

        mode = os.getenv("BUILD_MODE", "test").strip().lower()
        if mode not in {"test", "full"}:
            raise MissingConfigurationError("BUILD_MODE deve ser test ou full.")
        raw_limit = os.getenv("TEST_PAGE_LIMIT", "2").strip()
        if raw_limit not in {"1", "2"}:
            raise MissingConfigurationError("TEST_PAGE_LIMIT deve ser 1 ou 2.")

        repository = os.environ["GITHUB_REPOSITORY"].strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise MissingConfigurationError("GITHUB_REPOSITORY possui formato inválido.")

        github_sha = os.environ["GITHUB_SHA"].strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", github_sha):
            raise MissingConfigurationError("GITHUB_SHA possui formato inválido.")

        return cls(
            roblox_api_key=os.environ["ROBLOX_API_KEY"].strip(),
            github_token=os.environ["GITHUB_TOKEN"].strip(),
            github_repository=repository,
            github_sha=github_sha,
            mode=mode,
            test_page_limit=int(raw_limit),
        )


@dataclass(frozen=True, slots=True)
class PagePlan:
    category: str
    category_slug: str
    page_number: int
    category_page_count: int
    items: tuple[VFXItem, ...]
    page_hash: str
    logical_path: str


@dataclass(frozen=True, slots=True)
class DownloadSlot:
    item: VFXItem
    path: Path | None
    failure: str | None = None


@dataclass(slots=True)
class DecodedSlot:
    item: VFXItem
    frames: list[Image.Image]
    failure: str | None

    def close(self) -> None:
        for frame in self.frames:
            frame.close()
        self.frames.clear()


@dataclass(frozen=True, slots=True)
class RenderResult:
    path: Path
    width: int
    height: int
    fps: int
    output_frames: int
    size_bytes: int
    failed_asset_ids: tuple[int, ...]


@dataclass(slots=True)
class BuildStats:
    pages_analyzed: int = 0
    pages_reused: int = 0
    pages_generated: int = 0
    pages_failed: int = 0
    assets_failed: int = 0
    bytes_uploaded: int = 0
    generated_sizes: list[int] | None = None
    generated_seconds: list[float] | None = None

    def __post_init__(self) -> None:
        self.generated_sizes = []
        self.generated_seconds = []


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_tag(value: object) -> str:
    return "sha256-" + hashlib.sha256(canonical_json(value)).hexdigest()


def sanitize_category(category: str) -> str:
    """Cria um segmento URL-safe e impossível de transformar em path traversal."""

    normalized = unicodedata.normalize("NFKD", category)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    slug = slug[:48] or "category"
    suffix = hashlib.sha256(category.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{suffix}"


def page_hash(config: BuilderConfig, category: str, items: Iterable[VFXItem]) -> str:
    payload = {
        "source": SOURCE_KEY,
        "category": category,
        "render": config.render_fingerprint(),
        "items": [
            {
                "asset_id": item.asset_id,
                "grid": item.grid,
                "resolution": item.resolution,
                "record_hash": item.record_hash,
            }
            for item in items
        ],
    }
    return sha256_tag(payload)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256-" + digest.hexdigest()


def release_asset_name(plan: PagePlan, content_hash: str) -> str:
    """Nome plano, seguro e imutável para um asset de GitHub Release."""

    plan_part = plan.page_hash.removeprefix("sha256-")[:20]
    content_part = content_hash.removeprefix("sha256-")[:20]
    return (
        f"page--{plan.category_slug}--{plan.page_number:03d}--"
        f"{plan_part}--{content_part}.gif"
    )


def content_square_size(config: BuilderConfig) -> int:
    return max(1, min(
        config.card_width - config.content_padding * 2,
        config.preview_height - config.content_padding * 2,
    ))


def card_height(config: BuilderConfig) -> int:
    return config.card_header_height + config.preview_height


def geometry(config: BuilderConfig) -> tuple[int, int, int]:
    width = (
        config.margin * 2
        + config.columns * config.card_width
        + (config.columns - 1) * config.gap
    )
    height = (
        config.margin * 2
        + config.page_header_height
        + config.gap
        + config.rows * card_height(config)
        + (config.rows - 1) * config.gap
    )
    return width, height, content_square_size(config)


def adaptive_fps(config: BuilderConfig, largest_frame_count: int) -> int:
    """Mesma regra adaptativa 12/20/30 FPS usada pelo bot."""

    if largest_frame_count >= 64:
        return config.fps_64_frames
    if largest_frame_count >= 16:
        return config.fps_16_frames
    return config.fps_4_frames


def output_frame_count(config: BuilderConfig, largest_frame_count: int) -> int:
    """Usa o maior ciclo visível, nunca MMC, e aplica um teto rígido."""

    return max(1, min(largest_frame_count, config.max_output_frames))


def _font(size: int) -> ImageFont.ImageFont:
    # Ubuntu GitHub-hosted runner normalmente possui DejaVu Sans. O fallback
    # mantém o workflow funcional caso a fonte do sistema mude no futuro.
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    left, top, right, bottom = box
    x = left + max(0, (right - left - text_width) // 2)
    y = top + max(0, (bottom - top - text_height) // 2)
    draw.text((x, y), text, font=font, fill=fill)


def _text_size(font: ImageFont.ImageFont, text: str) -> tuple[int, int]:
    left, top, right, bottom = font.getbbox(text)
    return right - left, bottom - top


def _left_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    draw.text(position, text, font=font, fill=fill)


def _texture_label_lines(
    asset_id: int,
    max_width: int,
    label_font: ImageFont.ImageFont,
) -> tuple[str, ...]:
    single = f"Texture ID: {asset_id}"
    if _text_size(label_font, single)[0] <= max_width:
        return (single,)
    return ("Texture ID:", str(asset_id))


def _draw_spark_icon(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    *,
    color: tuple[int, int, int, int],
    accent: tuple[int, int, int, int],
) -> None:
    points = [(x + 7, y), (x + 10, y + 7), (x + 17, y + 10), (x + 10, y + 13), (x + 7, y + 20), (x + 4, y + 13), (x - 3, y + 10), (x + 4, y + 7)]
    draw.polygon(points, fill=color)
    draw.ellipse((x + 17, y + 3, x + 21, y + 7), fill=accent)
    draw.ellipse((x + 13, y + 15, x + 16, y + 18), fill=accent)


def _draw_texture_icon(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    *,
    color: tuple[int, int, int, int],
) -> None:
    top = [(x + 8, y), (x + 16, y + 4), (x + 8, y + 8), (x, y + 4)]
    left = [(x, y + 4), (x + 8, y + 8), (x + 8, y + 17), (x, y + 13)]
    right = [(x + 8, y + 8), (x + 16, y + 4), (x + 16, y + 13), (x + 8, y + 17)]
    draw.polygon(top, outline=color)
    draw.polygon(left, outline=color)
    draw.polygon(right, outline=color)


def _validate_source_image(image: Image.Image, item: VFXItem, config: BuilderConfig) -> None:
    image_format = (image.format or "").upper()
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        raise InvalidSpritesheetError("formato não suportado")
    if getattr(image, "n_frames", 1) != 1:
        raise InvalidSpritesheetError("asset já é animado")
    width, height = image.size
    if (
        width <= 0
        or height <= 0
        or width > config.max_texture_dimension
        or height > config.max_texture_dimension
        or width * height > config.max_texture_pixels
    ):
        raise InvalidSpritesheetError("imagem grande demais")
    if width % item.grid != 0 or height % item.grid != 0:
        raise InvalidSpritesheetError("spritesheet não é divisível pelo grid")


def _frame_to_transparent_canvas(frame: Image.Image, size: int) -> Image.Image:
    """Preserva alpha do frame; a composição final decide o fundo."""

    rgba = frame.convert("RGBA")
    try:
        ratio = min(size / rgba.width, size / rgba.height)
        target_width = max(1, round(rgba.width * ratio))
        target_height = max(1, round(rgba.height * ratio))
        resized = rgba.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
        )
        try:
            result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            left = (size - target_width) // 2
            top = (size - target_height) // 2
            result.alpha_composite(resized, (left, top))
            return result
        finally:
            resized.close()
    finally:
        rgba.close()


def decode_slot(slot: DownloadSlot, config: BuilderConfig) -> DecodedSlot:
    if slot.path is None:
        return DecodedSlot(slot.item, [], slot.failure or "indisponível")

    frames: list[Image.Image] = []
    try:
        with Image.open(slot.path) as source_image:
            _validate_source_image(source_image, slot.item, config)
            source_image.load()
            spritesheet = source_image.convert("RGBA")
        try:
            frame_width = spritesheet.width // slot.item.grid
            frame_height = spritesheet.height // slot.item.grid
            for row in range(slot.item.grid):
                for column in range(slot.item.grid):
                    left = column * frame_width
                    top = row * frame_height
                    cropped = spritesheet.crop((left, top, left + frame_width, top + frame_height))
                    try:
                        frames.append(_frame_to_transparent_canvas(cropped, content_square_size(config)))
                    finally:
                        cropped.close()
        finally:
            spritesheet.close()
        if not frames:
            raise InvalidSpritesheetError("spritesheet sem frames")
        return DecodedSlot(slot.item, frames, None)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        failure = "imagem grande demais"
    except (UnidentifiedImageError, InvalidSpritesheetError, OSError) as error:
        failure = str(error) or "textura inválida"
    except Exception:
        LOGGER.exception("Falha inesperada ao decodificar asset %s.", slot.item.asset_id)
        failure = "preview indisponível"

    for frame in frames:
        frame.close()
    return DecodedSlot(slot.item, [], failure)


@dataclass(frozen=True, slots=True)
class SlotLayout:
    preview_box: tuple[int, int, int, int]
    effect_origin: tuple[int, int]


@dataclass(slots=True)
class PreparedPage:
    base: Image.Image
    layouts: list[SlotLayout]

    def close(self) -> None:
        self.base.close()


def _build_static_page(
    decoded: list[DecodedSlot],
    config: BuilderConfig,
    *,
    page_title: str,
    page_indicator: str,
) -> PreparedPage:
    width, height, content_size = geometry(config)
    card_total_height = card_height(config)
    canvas = Image.new("RGBA", (width, height), (10, 12, 16, 255))
    draw = ImageDraw.Draw(canvas)

    title_font = _font(17)
    page_font = _font(16)
    label_font = _font(14)
    placeholder_font = _font(15)
    dash_font = _font(18)

    background_fill = (10, 12, 16, 255)
    outer_fill = (13, 16, 22, 255)
    outer_border = (52, 60, 74, 255)
    divider_fill = (42, 48, 60, 255)
    card_fill = (17, 20, 27, 255)
    card_border = (61, 70, 86, 255)
    card_strip = (29, 33, 43, 255)
    preview_fill = (0, 0, 0, 255)
    text_fill = (239, 242, 247, 255)
    muted_fill = (176, 184, 197, 255)
    accent_fill = (180, 163, 255, 255)
    accent_dim = (128, 145, 255, 255)
    pill_fill = (26, 31, 40, 255)
    placeholder_fill = (11, 13, 18, 255)

    draw.rectangle((0, 0, width, height), fill=background_fill)
    panel_box = (4, 4, width - 5, height - 5)
    draw.rounded_rectangle(panel_box, radius=16, fill=outer_fill, outline=outer_border, width=1)

    header_top = 4
    header_bottom = config.margin + config.page_header_height
    header_left = config.margin
    header_right = width - config.margin
    draw.line((panel_box[0], header_bottom, panel_box[2], header_bottom), fill=divider_fill, width=1)

    page_text_width, page_text_height = _text_size(page_font, page_indicator)
    pill_height = 30
    pill_width = max(54, page_text_width + 26)
    pill_right = header_right - 2
    pill_left = pill_right - pill_width
    pill_top = header_top + 13
    pill_bottom = pill_top + pill_height

    # O título agora fica sozinho, centralizado visualmente no espaço
    # disponível à esquerda do indicador de página.
    title_area_left = header_left + 14
    title_area_right = pill_left - 16
    if title_area_right <= title_area_left:
        title_area_left = header_left + 14
        title_area_right = header_right - 14
    _centered_text(
        draw,
        (title_area_left, header_top, title_area_right, header_bottom),
        page_title,
        title_font,
        text_fill,
    )

    draw.rounded_rectangle((pill_left, pill_top, pill_right, pill_bottom), radius=14, fill=pill_fill, outline=outer_border, width=1)
    _centered_text(draw, (pill_left, pill_top, pill_right, pill_bottom), page_indicator, page_font, muted_fill)

    layouts: list[SlotLayout] = []
    grid_top = config.margin + config.page_header_height + config.gap
    for slot_index in range(config.items_per_page):
        row = slot_index // config.columns
        column = slot_index % config.columns
        x = config.margin + column * (config.card_width + config.gap)
        y = grid_top + row * (card_total_height + config.gap)
        card_box = (x, y, x + config.card_width - 1, y + card_total_height - 1)
        draw.rounded_rectangle(card_box, radius=14, fill=card_fill, outline=card_border, width=1)

        strip_box = (x + 1, y + 1, x + config.card_width - 2, y + config.card_header_height - 1)
        draw.rounded_rectangle(strip_box, radius=13, fill=card_strip)
        draw.rectangle((x + 1, y + config.card_header_height - 13, x + config.card_width - 2, y + config.card_header_height - 1), fill=card_strip)

        preview_left = x + 1
        preview_top = y + config.card_header_height
        preview_right = x + config.card_width - 2
        preview_bottom = preview_top + config.preview_height - 2
        draw.rounded_rectangle((preview_left, preview_top, preview_right, preview_bottom), radius=10, fill=preview_fill)

        if slot_index >= len(decoded):
            _centered_text(draw, (x, y, x + config.card_width, y + card_total_height), "—", dash_font, muted_fill)
            layouts.append(SlotLayout((preview_left, preview_top, preview_right, preview_bottom), (preview_left, preview_top)))
            continue

        slot = decoded[slot_index]
        _draw_texture_icon(draw, x + 16, y + 12, color=accent_dim)
        label_left = x + 40
        label_top = y + 10
        label_width = config.card_width - 54
        lines = _texture_label_lines(slot.item.asset_id, label_width, label_font)
        line_height = _text_size(label_font, "Texture ID:")[1]
        if len(lines) == 1:
            _left_text(draw, (label_left, y + (config.card_header_height - line_height) // 2 - 1), lines[0], label_font, text_fill)
        else:
            _left_text(draw, (label_left, label_top - 1), lines[0], label_font, muted_fill)
            _left_text(draw, (label_left, label_top + line_height - 1), lines[1], label_font, text_fill)

        effect_left = preview_left + (config.card_width - 2 - content_size) // 2
        effect_top = preview_top + (config.preview_height - 2 - content_size) // 2
        layouts.append(SlotLayout((preview_left, preview_top, preview_right, preview_bottom), (effect_left, effect_top)))

        if not slot.frames:
            inset = 12
            draw.rounded_rectangle((preview_left + inset, preview_top + inset, preview_right - inset, preview_bottom - inset), radius=8, fill=placeholder_fill, outline=(34, 39, 48, 255), width=1)
            _centered_text(draw, (preview_left + 16, preview_top + 16, preview_right - 16, preview_bottom - 16), "Prévia indisponível", placeholder_font, muted_fill)

    return PreparedPage(canvas, layouts)


def render_rgba_frame(
    decoded: list[DecodedSlot],
    config: BuilderConfig,
    *,
    tick: int,
    prepared: PreparedPage,
) -> Image.Image:
    _width, _height, content_size = geometry(config)
    canvas = prepared.base.copy()
    for slot_index, slot in enumerate(decoded):
        if not slot.frames:
            continue
        source_frame = slot.frames[tick % len(slot.frames)]
        if source_frame.size != (content_size, content_size):
            effect_frame = source_frame.resize((content_size, content_size), Image.Resampling.LANCZOS)
        else:
            effect_frame = source_frame
        try:
            canvas.alpha_composite(effect_frame, prepared.layouts[slot_index].effect_origin)
        finally:
            if effect_frame is not source_frame:
                effect_frame.close()
    return canvas


def build_gif_palette(
    image: Image.Image,
    config: BuilderConfig,
) -> Image.Image:
    """Cria uma única paleta para a página inteira.

    O layout final é totalmente opaco. Usar uma paleta global evita que o Pillow
    recalcule os tons escuros em cada frame, o que causava uma mudança sutil de
    cor nos cards/fundo durante a animação.
    """

    rgb = image.convert("RGB")
    try:
        return rgb.quantize(
            colors=min(256, config.gif_palette_colors),
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.FLOYDSTEINBERG,
        )
    finally:
        rgb.close()


def rgba_to_fixed_palette_frame(
    image: Image.Image,
    palette: Image.Image,
) -> Image.Image:
    """Converte um frame usando exatamente a mesma paleta dos demais."""

    rgb = image.convert("RGB")
    try:
        return rgb.quantize(
            palette=palette,
            dither=Image.Dither.FLOYDSTEINBERG,
        )
    finally:
        rgb.close()


def render_page_gif(
    plan: PagePlan,
    slots: list[DownloadSlot],
    output_path: Path,
    config: BuilderConfig,
) -> RenderResult:
    """Renderiza uma página de até 6 slots sem usar MMC entre animações."""

    decoded = [decode_slot(slot, config) for slot in slots]
    prepared = _build_static_page(
        decoded,
        config,
        page_title="VFX Studio Previews",
        page_indicator=f"{plan.page_number}/{plan.category_page_count}",
    )
    try:
        largest_frame_count = max(
            (len(slot.frames) for slot in decoded if slot.frames),
            default=1,
        )
        fps = adaptive_fps(config, largest_frame_count)
        frame_count = output_frame_count(config, largest_frame_count)
        duration_ms = max(1, round(1000 / fps))
        failed_asset_ids = tuple(
            slot.item.asset_id for slot in decoded if not slot.frames
        )

        first_rgba = render_rgba_frame(decoded, config, tick=0, prepared=prepared)
        try:
            palette = build_gif_palette(first_rgba, config)
            first = rgba_to_fixed_palette_frame(first_rgba, palette)
        finally:
            first_rgba.close()

        def remaining_frames():
            previous: Image.Image | None = None
            try:
                for tick in range(1, frame_count):
                    if previous is not None:
                        previous.close()
                    rgba = render_rgba_frame(decoded, config, tick=tick, prepared=prepared)
                    try:
                        previous = rgba_to_fixed_palette_frame(rgba, palette)
                    finally:
                        rgba.close()
                    yield previous
            finally:
                if previous is not None:
                    previous.close()

        try:
            first.save(
                output_path,
                format="GIF",
                save_all=True,
                append_images=remaining_frames(),
                duration=duration_ms,
                loop=0,
                # Frames completos e opacos não precisam de transparência/disposal.
                # optimize=False preserva a mesma paleta global em toda animação.
                disposal=1,
                optimize=False,
            )
        finally:
            first.close()
            palette.close()

        width, height, _ = geometry(config)
        return RenderResult(
            path=output_path,
            width=width,
            height=height,
            fps=fps,
            output_frames=frame_count,
            size_bytes=output_path.stat().st_size,
            failed_asset_ids=failed_asset_ids,
        )
    finally:
        prepared.close()
        for slot in decoded:
            slot.close()


class GitHubReleaseStore:
    """Armazena previews como assets de uma GitHub Release pública."""

    def __init__(self, env: Environment) -> None:
        self.env = env
        self.repository = env.github_repository
        self.release_tag = env.release_tag
        self.release_id: int | None = None
        self.assets: dict[str, dict[str, Any]] = {}
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "GitHubReleaseStore":
        self.session = aiohttp.ClientSession(
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.env.github_token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": "VFX Preview Builder/1.1",
            }
        )
        await self._verify_public_repository()
        await self._ensure_release()
        await self.refresh_assets()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            await self.session.close()

    @property
    def release_url(self) -> str:
        return f"https://github.com/{self.repository}/releases/tag/{quote(self.release_tag, safe='')}"

    def public_url(self, asset_name: str) -> str:
        existing = self.assets.get(asset_name)
        if isinstance(existing, dict):
            browser_url = existing.get("browser_download_url")
            if isinstance(browser_url, str) and browser_url.startswith("https://github.com/"):
                return browser_url
        return (
            f"https://github.com/{self.repository}/releases/download/"
            f"{quote(self.release_tag, safe='')}/{quote(asset_name, safe='')}"
        )

    async def _api_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected: set[int] | None = None,
    ) -> tuple[int, Any]:
        assert self.session is not None
        expected = expected or {200}
        last_status = 0
        for attempt in range(3):
            try:
                async with self.session.request(
                    method,
                    url,
                    json=json_body,
                    timeout=aiohttp.ClientTimeout(total=45),
                    allow_redirects=False,
                ) as response:
                    last_status = response.status
                    if response.status in expected:
                        if response.status == 204:
                            return response.status, None
                        try:
                            return response.status, await response.json(content_type=None)
                        except (json.JSONDecodeError, UnicodeError) as error:
                            raise BuilderError("A API do GitHub retornou JSON inválido.") from error
                    if response.status == 404 and method.upper() == "GET":
                        return response.status, None
                    if response.status in {429, 500, 502, 503, 504} or (
                        response.status == 403 and response.headers.get("Retry-After")
                    ):
                        if attempt < 2:
                            raw_retry = response.headers.get("Retry-After", "")
                            try:
                                delay = max(1.0, min(float(raw_retry), 30.0))
                            except ValueError:
                                delay = float(min(2 ** attempt, 8))
                            await asyncio.sleep(delay)
                            continue
                    detail = (await response.text())[:500]
                    if response.status in {401, 403}:
                        raise MissingConfigurationError(
                            "O GITHUB_TOKEN não possui permissão suficiente para gerenciar a Release "
                            f"(HTTP {response.status}). Garanta contents: write no workflow."
                        )
                    raise BuilderError(
                        f"GitHub API HTTP {response.status}: {detail or 'resposta sem detalhes'}"
                    )
            except (MissingConfigurationError, BuilderError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt >= 2:
                    raise NetworkError("Falha de rede ao acessar a API do GitHub.") from error
                await asyncio.sleep(min(2 ** attempt, 8))
        raise BuilderError(f"GitHub API falhou com HTTP {last_status}.")

    async def _verify_public_repository(self) -> None:
        status, payload = await self._api_json(
            "GET",
            f"{GITHUB_API_BASE}/repos/{self.repository}",
            expected={200},
        )
        if status != 200 or not isinstance(payload, dict):
            raise BuilderError("Não foi possível validar o repositório do GitHub.")
        if payload.get("private") is True:
            raise MissingConfigurationError(
                "O repositório precisa ser público para os GIFs abrirem sem autenticação."
            )

    async def _ensure_release(self) -> None:
        tag = quote(self.release_tag, safe="")
        status, payload = await self._api_json(
            "GET",
            f"{GITHUB_API_BASE}/repos/{self.repository}/releases/tags/{tag}",
            expected={200},
        )
        if status == 404:
            status, payload = await self._api_json(
                "POST",
                f"{GITHUB_API_BASE}/repos/{self.repository}/releases",
                json_body={
                    "tag_name": self.release_tag,
                    "target_commitish": self.env.github_sha,
                    "name": self.env.release_name,
                    "body": (
                        "Arquivos gerados automaticamente pelo VFX Preview Builder. "
                        "Não edite os assets manualmente."
                    ),
                    "draft": False,
                    "prerelease": self.env.mode == "test",
                    "generate_release_notes": False,
                    "make_latest": "false",
                },
                expected={201},
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise BuilderError("A Release do GitHub não pôde ser criada/carregada.")
        self.release_id = payload["id"]

    async def refresh_assets(self) -> None:
        if self.release_id is None:
            raise BuilderError("Release ainda não inicializada.")
        collected: dict[str, dict[str, Any]] = {}
        for page in range(1, 12):
            status, payload = await self._api_json(
                "GET",
                (
                    f"{GITHUB_API_BASE}/repos/{self.repository}/releases/"
                    f"{self.release_id}/assets?per_page=100&page={page}"
                ),
                expected={200},
            )
            if status != 200 or not isinstance(payload, list):
                raise BuilderError("Não foi possível listar os assets da Release.")
            for asset in payload:
                if isinstance(asset, dict) and isinstance(asset.get("name"), str):
                    collected[asset["name"]] = asset
            if len(payload) < 100:
                break
        else:
            raise BuilderError("A Release excedeu a paginação de segurança dos assets.")
        self.assets = collected
        if len(self.assets) >= GITHUB_RELEASE_ASSET_WARNING_LIMIT:
            LOGGER.warning(
                "A Release possui %s assets; o limite do GitHub é %s.",
                len(self.assets),
                GITHUB_RELEASE_ASSET_HARD_LIMIT,
            )

    async def _download_asset_bytes(self, asset: dict[str, Any], max_bytes: int) -> bytes:
        assert self.session is not None
        asset_id = asset.get("id")
        if not isinstance(asset_id, int):
            raise BuilderError("Asset de Release sem ID válido.")
        url = f"{GITHUB_API_BASE}/repos/{self.repository}/releases/assets/{asset_id}"
        async with self.session.get(
            url,
            headers={"Accept": "application/octet-stream"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as response:
            if response.status == 200:
                raw = await response.content.read(max_bytes + 1)
            elif response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                try:
                    parts = urlsplit(location)
                except ValueError as error:
                    raise BuilderError("Redirect inválido ao baixar asset do GitHub.") from error
                hostname = (parts.hostname or "").lower().rstrip(".")
                allowed = hostname == "github.com" or hostname.endswith(".githubusercontent.com")
                if parts.scheme.lower() != "https" or not allowed:
                    raise BuilderError("Redirect inesperado ao baixar asset do GitHub.")
                # Nenhum GITHUB_TOKEN é enviado ao host de download público.
                async with aiohttp.ClientSession(
                    headers={"User-Agent": "VFX Preview Builder/1.1"}
                ) as public_session:
                    async with public_session.get(
                        location,
                        timeout=aiohttp.ClientTimeout(total=45),
                    ) as public_response:
                        if public_response.status != 200:
                            raise NetworkError(
                                f"Download público do GitHub respondeu HTTP {public_response.status}."
                            )
                        raw = await public_response.content.read(max_bytes + 1)
            else:
                raise BuilderError(
                    f"Não foi possível baixar asset da Release (HTTP {response.status})."
                )
        if len(raw) > max_bytes:
            raise BuilderError("Asset da Release ultrapassou o limite de segurança.")
        return raw

    async def get_latest_manifest(self) -> tuple[str | None, dict[str, Any] | None]:
        candidates = [
            asset
            for name, asset in self.assets.items()
            if name.startswith("manifest--") and name.endswith(".json")
        ]
        if not candidates:
            return None, None
        candidates.sort(
            key=lambda item: (str(item.get("created_at", "")), int(item.get("id", 0) or 0)),
            reverse=True,
        )
        asset = candidates[0]
        raw = await self._download_asset_bytes(asset, MAX_MANIFEST_BYTES)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise BuilderError("O manifest anterior no GitHub está inválido.") from error
        if not isinstance(payload, dict):
            raise BuilderError("O manifest anterior no GitHub não é um objeto JSON.")
        return str(asset["name"]), payload

    async def _upload_raw(
        self,
        asset_name: str,
        payload: bytes | Path,
        content_type: str,
    ) -> tuple[dict[str, Any], int]:
        if self.release_id is None or self.session is None:
            raise BuilderError("Release ainda não inicializada.")
        existing = self.assets.get(asset_name)
        size = len(payload) if isinstance(payload, bytes) else payload.stat().st_size
        if (
            existing is not None
            and existing.get("state") == "uploaded"
            and int(existing.get("size", -1)) == size
        ):
            return existing, 0
        if existing is not None:
            await self.delete_asset(existing)

        upload_url = (
            f"{GITHUB_UPLOAD_BASE}/repos/{self.repository}/releases/{self.release_id}/assets"
        )

        for attempt in range(3):
            if len(self.assets) >= GITHUB_RELEASE_ASSET_HARD_LIMIT:
                raise BuilderError("A Release atingiu o limite de 1000 assets do GitHub.")

            headers = {"Content-Type": content_type, "Content-Length": str(size)}
            handle = None
            data: Any
            if isinstance(payload, bytes):
                data = payload
            else:
                handle = payload.open("rb")
                data = handle
            try:
                try:
                    async with self.session.post(
                        upload_url,
                        params={"name": asset_name},
                        data=data,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=180),
                    ) as response:
                        if response.status == 201:
                            try:
                                asset = await response.json(content_type=None)
                            except (json.JSONDecodeError, UnicodeError) as error:
                                raise BuilderError(
                                    "GitHub retornou JSON inválido após upload."
                                ) from error
                            if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
                                raise BuilderError("GitHub não confirmou o asset enviado.")
                            if asset["name"] != asset_name:
                                # Nossos nomes usam somente caracteres simples. Se o GitHub
                                # ainda assim renomear, falhamos cedo para não criar URLs instáveis.
                                raise BuilderError(
                                    f"GitHub renomeou inesperadamente o asset {asset_name!r} "
                                    f"para {asset['name']!r}."
                                )
                            self.assets[asset_name] = asset
                            return asset, size

                        detail = (await response.text())[:500]
                        retryable = response.status in {429, 500, 502, 503, 504}
                        duplicate = response.status == 422
                        if response.status in {401, 403}:
                            raise MissingConfigurationError(
                                "O GITHUB_TOKEN não possui permissão para enviar assets da Release "
                                f"(HTTP {response.status})."
                            )
                        if not retryable and not duplicate:
                            raise BuilderError(
                                f"Upload de asset para GitHub falhou HTTP {response.status}: {detail}"
                            )
                finally:
                    if handle is not None:
                        handle.close()

                # 502 pode deixar um asset vazio em estado starter; 422 pode ser
                # resíduo de uma tentativa anterior. Recarrega a lista uma vez e limpa.
                await self.refresh_assets()
                stuck = self.assets.get(asset_name)
                if stuck is not None:
                    if (
                        stuck.get("state") == "uploaded"
                        and int(stuck.get("size", -1)) == size
                    ):
                        return stuck, 0
                    await self.delete_asset(stuck)
                if attempt < 2:
                    await asyncio.sleep(float(min(2 ** attempt, 8)))
                    continue
            except (MissingConfigurationError, BuilderError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if handle is not None and not handle.closed:
                    handle.close()
                if attempt >= 2:
                    raise NetworkError("Falha de rede ao enviar asset para GitHub.") from error
                await asyncio.sleep(float(min(2 ** attempt, 8)))
                continue

        raise BuilderError(f"Não foi possível enviar o asset {asset_name} após retries.")

    async def upload_file(self, local_path: Path, asset_name: str) -> tuple[dict[str, Any], int]:
        return await self._upload_raw(asset_name, local_path, "image/gif")

    async def upload_manifest(
        self,
        manifest: dict[str, Any],
        mode: str,
    ) -> tuple[str, str, int]:
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        asset_name = f"manifest--{mode}--{digest[:24]}.json"
        _asset, uploaded = await self._upload_raw(
            asset_name,
            payload,
            "application/json; charset=utf-8",
        )
        return asset_name, self.public_url(asset_name), uploaded

    async def delete_asset(self, asset: dict[str, Any]) -> None:
        asset_id = asset.get("id")
        name = asset.get("name")
        if not isinstance(asset_id, int):
            return
        status, _ = await self._api_json(
            "DELETE",
            f"{GITHUB_API_BASE}/repos/{self.repository}/releases/assets/{asset_id}",
            expected={204, 404},
        )
        if status in {204, 404} and isinstance(name, str):
            self.assets.pop(name, None)

    async def cleanup_builder_assets(self, keep_names: set[str]) -> int:
        """Remove somente assets gerados por este builder que não são mais referenciados."""

        removed = 0
        for name, asset in list(self.assets.items()):
            managed = name.startswith("page--") or name.startswith("manifest--")
            if managed and name not in keep_names:
                try:
                    await self.delete_asset(asset)
                    removed += 1
                except Exception:
                    LOGGER.exception("Não foi possível remover asset antigo %s.", name)
        return removed


class RobloxAssetClient:
    """Baixa spritesheets mantendo a API Key somente em apis.roblox.com."""

    def __init__(self, env: Environment, config: BuilderConfig) -> None:
        self.api_key = env.roblox_api_key
        self.config = config
        self.semaphore = asyncio.Semaphore(config.max_concurrent_downloads)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "RobloxAssetClient":
        connector = aiohttp.TCPConnector(limit=8, limit_per_host=4)
        self.session = aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": "VFX Preview Builder/1.0"},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            await self.session.close()

    @staticmethod
    def _allowed_cdn_url(url: str) -> bool:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        hostname = (parts.hostname or "").lower().rstrip(".")
        return (
            parts.scheme.lower() == "https"
            and bool(parts.netloc)
            and parts.username is None
            and parts.password is None
            and port in {None, 443}
            and (hostname == "rbxcdn.com" or hostname.endswith(".rbxcdn.com"))
        )

    async def _sleep_for_retry(self, response: aiohttp.ClientResponse, attempt: int) -> None:
        raw = response.headers.get("Retry-After", "")
        try:
            retry_after = float(raw)
        except ValueError:
            retry_after = min(2 ** attempt, 15)
        await asyncio.sleep(max(1.0, min(retry_after, 60.0)))

    async def _delivery_location(self, asset_id: int) -> str:
        assert self.session is not None
        url = ASSET_DELIVERY_URL.format(asset_id=asset_id)
        for attempt in range(self.config.http_retries):
            try:
                async with self.session.get(
                    url,
                    headers={
                        "Accept": "application/json",
                        "x-api-key": self.api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=self.config.delivery_timeout_seconds),
                    allow_redirects=False,
                ) as response:
                    if response.status == 200:
                        raw = await response.content.read(MAX_DELIVERY_RESPONSE_BYTES + 1)
                        if not raw or len(raw) > MAX_DELIVERY_RESPONSE_BYTES:
                            raise AssetUnavailableError("resposta inválida do Asset Delivery")
                        try:
                            payload = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeError) as error:
                            raise AssetUnavailableError("resposta inválida do Asset Delivery") from error
                        location = payload.get("location") if isinstance(payload, dict) else None
                        if not isinstance(location, str) or not self._allowed_cdn_url(location):
                            raise AssetUnavailableError("localização CDN inválida")
                        return location
                    if response.status == 401:
                        raise RobloxAuthError("A ROBLOX_API_KEY foi recusada.")
                    if response.status == 403:
                        raise AssetUnavailableError("sem permissão")
                    if response.status in {404, 410}:
                        raise AssetUnavailableError("textura indisponível")
                    if response.status == 429 or response.status >= 500:
                        if attempt + 1 < self.config.http_retries:
                            await self._sleep_for_retry(response, attempt)
                            continue
                        raise NetworkError(f"Asset Delivery HTTP {response.status}")
                    raise AssetUnavailableError(f"Asset Delivery HTTP {response.status}")
            except (RobloxAuthError, AssetUnavailableError, NetworkError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt + 1 >= self.config.http_retries:
                    raise NetworkError("Falha de rede no Asset Delivery.") from error
                await asyncio.sleep(min(2 ** attempt, 10))
        raise NetworkError("Falha no Asset Delivery.")

    async def _download_from_cdn(self, asset_id: int, location: str, destination: Path) -> None:
        assert self.session is not None
        current_url = location
        for _redirect in range(4):
            if not self._allowed_cdn_url(current_url):
                raise AssetUnavailableError("redirecionamento fora da CDN Roblox")

            for attempt in range(self.config.http_retries):
                try:
                    async with self.session.get(
                        current_url,
                        headers={"Accept": "image/*,application/octet-stream;q=0.8"},
                        timeout=aiohttp.ClientTimeout(total=self.config.download_timeout_seconds),
                        allow_redirects=False,
                    ) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            target = response.headers.get("Location")
                            if not target:
                                raise AssetUnavailableError("redirecionamento CDN inválido")
                            current_url = urljoin(current_url, target)
                            break
                        if response.status == 200:
                            if (
                                response.content_length is not None
                                and response.content_length > self.config.max_texture_bytes
                            ):
                                raise AssetUnavailableError("textura grande demais")
                            size = 0
                            with destination.open("wb") as handle:
                                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                                    size += len(chunk)
                                    if size > self.config.max_texture_bytes:
                                        raise AssetUnavailableError("textura grande demais")
                                    handle.write(chunk)
                            if size <= 0:
                                raise AssetUnavailableError("textura vazia")
                            return
                        if response.status == 429 or response.status >= 500:
                            if attempt + 1 < self.config.http_retries:
                                await self._sleep_for_retry(response, attempt)
                                continue
                            raise NetworkError(f"CDN Roblox HTTP {response.status}")
                        raise AssetUnavailableError(f"CDN Roblox HTTP {response.status}")
                except (AssetUnavailableError, NetworkError):
                    destination.unlink(missing_ok=True)
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                    destination.unlink(missing_ok=True)
                    if attempt + 1 >= self.config.http_retries:
                        raise NetworkError("Falha de rede na CDN Roblox.") from error
                    await asyncio.sleep(min(2 ** attempt, 10))
            else:
                continue
            # Sai do loop de retries apenas quando recebeu redirect.
            continue
        raise AssetUnavailableError("redirecionamentos demais na CDN Roblox")

    async def download(self, item: VFXItem, directory: Path) -> DownloadSlot:
        async with self.semaphore:
            destination = directory / f"asset-{item.asset_id}.bin"
            try:
                location = await self._delivery_location(item.asset_id)
                # A API Key NÃO é reaproveitada na chamada para a CDN.
                await self._download_from_cdn(item.asset_id, location, destination)
                return DownloadSlot(item, destination)
            except RobloxAuthError:
                raise
            except (AssetUnavailableError, NetworkError) as error:
                destination.unlink(missing_ok=True)
                LOGGER.warning("Asset %s sem preview: %s", item.asset_id, error)
                return DownloadSlot(item, None, str(error))
            except Exception:
                destination.unlink(missing_ok=True)
                LOGGER.exception("Falha inesperada no asset %s.", item.asset_id)
                return DownloadSlot(item, None, "preview indisponível")


async def download_catalog(config: BuilderConfig) -> VFXCatalog:
    """Baixa somente o JSON público; nenhum secret é usado nesta requisição."""

    timeout = aiohttp.ClientTimeout(total=config.catalog_timeout_seconds)
    connector = aiohttp.TCPConnector(limit=2, limit_per_host=2)
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": "VFX Preview Builder/1.0"},
        ) as session:
            async with session.get(
                config.catalog_url,
                headers={"Accept": "application/json"},
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    raise NetworkError(f"VFXData.json respondeu HTTP {response.status}.")
                raw = await response.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise NetworkError("Não foi possível baixar VFXData.json.") from error
    if len(raw) > 5 * 1024 * 1024:
        raise InvalidCatalogError("VFXData.json ultrapassou o limite de segurança.")
    return parse_catalog_bytes(raw)


def build_page_plans(config: BuilderConfig, catalog: VFXCatalog, mode: str, test_limit: int) -> list[PagePlan]:
    plans: list[PagePlan] = []
    prefix = "vfx-studio/test" if mode == "test" else "vfx-studio"

    for category, items in catalog.categories.items():
        slug = sanitize_category(category)
        page_count = math.ceil(len(items) / config.items_per_page)
        for page_index in range(page_count):
            start = page_index * config.items_per_page
            page_items = items[start : start + config.items_per_page]
            digest = page_hash(config, category, page_items)
            logical_path = (
                f"{prefix}/pages/{slug}/{page_index + 1:03d}-{digest.removeprefix('sha256-')[:20]}.gif"
            )
            plans.append(
                PagePlan(
                    category=category,
                    category_slug=slug,
                    page_number=page_index + 1,
                    category_page_count=page_count,
                    items=page_items,
                    page_hash=digest,
                    logical_path=logical_path,
                )
            )

    if mode == "test":
        return plans[:test_limit]
    return plans


def previous_page_index(manifest: dict[str, Any] | None) -> dict[tuple[str, int], dict[str, Any]]:
    if not isinstance(manifest, dict):
        return {}
    index: dict[tuple[str, int], dict[str, Any]] = {}
    categories = manifest.get("categories")
    if not isinstance(categories, list):
        return index
    for category in categories:
        if not isinstance(category, dict) or not isinstance(category.get("name"), str):
            continue
        pages = category.get("pages")
        if not isinstance(pages, list):
            continue
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("number"), int):
                index[(category["name"], page["number"])] = page
    return index


def can_reuse_page(
    previous: dict[str, Any] | None,
    plan: PagePlan,
    existing_asset_names: set[str] | None = None,
) -> bool:
    """Reutiliza apenas página limpa cujo asset ainda existe na Release."""

    if not isinstance(previous, dict):
        return False
    failed_ids = previous.get("failed_asset_ids")
    if isinstance(failed_ids, list) and failed_ids:
        return False
    asset_name = previous.get("asset_name")
    if not isinstance(asset_name, str) or not asset_name.startswith("page--"):
        return False
    if existing_asset_names is not None and asset_name not in existing_asset_names:
        return False
    return (
        previous.get("hash") == plan.page_hash
        and previous.get("logical_path") == plan.logical_path
        and isinstance(previous.get("content_hash"), str)
        and isinstance(previous.get("size_bytes"), int)
        and previous.get("size_bytes", 0) > 0
    )


def make_page_manifest_entry(
    plan: PagePlan,
    store: GitHubReleaseStore,
    *,
    asset_name: str,
    content_hash: str,
    size_bytes: int,
    failed_asset_ids: Iterable[int],
    fps: int,
    output_frames: int,
    width: int,
    height: int,
    reused: bool,
    significant_failure: bool,
) -> dict[str, Any]:
    failed_list = list(failed_asset_ids)
    return {
        "number": plan.page_number,
        "asset_ids": [item.asset_id for item in plan.items],
        "hash": plan.page_hash,
        "content_hash": content_hash,
        "logical_path": plan.logical_path,
        "asset_name": asset_name,
        "url": store.public_url(asset_name),
        "size_bytes": size_bytes,
        "failed_asset_ids": failed_list,
        "failed_asset_count": len(failed_list),
        "failure_ratio": round(len(failed_list) / max(1, len(plan.items)), 4),
        "significant_failure": significant_failure,
        "fps": fps,
        "output_frames": output_frames,
        "width": width,
        "height": height,
        "reused": reused,
    }


def build_manifest(
    config: BuilderConfig,
    env: Environment,
    catalog: VFXCatalog,
    page_entries: list[tuple[PagePlan, dict[str, Any]]],
    stats: BuildStats,
) -> dict[str, Any]:
    categories: dict[str, list[tuple[PagePlan, dict[str, Any]]]] = {}
    for plan, entry in page_entries:
        categories.setdefault(plan.category, []).append((plan, entry))

    category_payloads: list[dict[str, Any]] = []
    for category, entries in categories.items():
        all_items = catalog.categories[category]
        category_payloads.append(
            {
                "name": category,
                "slug": entries[0][0].category_slug,
                "catalog_item_count": len(all_items),
                "catalog_page_count": math.ceil(len(all_items) / config.items_per_page),
                "published_page_count": len(entries),
                "pages": [entry for _plan, entry in entries],
            }
        )

    render_config = config.render_fingerprint()
    return {
        "schema_version": 2,
        "hosting": {
            "provider": "github-releases",
            "repository": env.github_repository,
            "release_tag": env.release_tag,
            "release_url": (
                f"https://github.com/{env.github_repository}/releases/tag/"
                f"{quote(env.release_tag, safe='')}"
            ),
            "asset_urls_are_immutable": True,
        },
        "source": {
            "key": SOURCE_KEY,
            "name": SOURCE_NAME,
            "catalog_version": catalog.catalog_version,
            "catalog_url": config.catalog_url,
            "catalog_total_unique_flipbooks": catalog.total_unique_flipbooks,
        },
        "generator": {
            "version": config.generator_version,
            "config_hash": sha256_tag(render_config),
            "items_per_page": config.items_per_page,
            "layout": {
                "columns": config.columns,
                "rows": config.rows,
                "page_header_height": config.page_header_height,
                "card_width": config.card_width,
                "card_header_height": config.card_header_height,
                "preview_height": config.preview_height,
            },
            "timing": {
                "strategy": "max-visible-frame-count-capped",
                "max_output_frames": config.max_output_frames,
                "adaptive_fps": {
                    "4_frames": config.fps_4_frames,
                    "16_frames": config.fps_16_frames,
                    "64_frames": config.fps_64_frames,
                },
            },
            "transparency": {
                "format": "GIF binary transparency",
                "alpha_threshold": config.gif_alpha_threshold,
            },
            "significant_failure_ratio": config.significant_failure_ratio,
        },
        "build": {
            "mode": env.mode,
            "partial": env.mode == "test",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "test_page_limit": env.test_page_limit if env.mode == "test" else None,
        },
        "categories": category_payloads,
        "totals": {
            "pages_analyzed": stats.pages_analyzed,
            "pages_reused": stats.pages_reused,
            "pages_generated": stats.pages_generated,
            "assets_failed": stats.assets_failed,
            "published_pages": len(page_entries),
        },
    }


def manifest_asset_names(manifest: dict[str, Any] | None) -> set[str]:
    names: set[str] = set()
    if not isinstance(manifest, dict):
        return names
    categories = manifest.get("categories")
    if not isinstance(categories, list):
        return names
    for category in categories:
        if not isinstance(category, dict):
            continue
        pages = category.get("pages")
        if not isinstance(pages, list):
            continue
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("asset_name"), str):
                names.add(page["asset_name"])
    return names


def _format_bytes(value: float | int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GiB"


def summary_markdown(
    stats: BuildStats,
    elapsed: float,
    manifest_published: bool,
    *,
    release_url: str | None = None,
    manifest_url: str | None = None,
) -> str:
    sizes = stats.generated_sizes or []
    durations = stats.generated_seconds or []
    average_size = statistics.fmean(sizes) if sizes else 0
    smallest = min(sizes) if sizes else 0
    largest = max(sizes) if sizes else 0
    average_seconds = statistics.fmean(durations) if durations else 0

    lines = [
        "## VFX Preview Builder",
        "",
        f"- Páginas analisadas: **{stats.pages_analyzed}**",
        f"- Páginas reaproveitadas: **{stats.pages_reused}**",
        f"- Páginas geradas: **{stats.pages_generated}**",
        f"- Páginas com falha: **{stats.pages_failed}**",
        f"- Assets com falha: **{stats.assets_failed}**",
        f"- Bytes enviados nesta execução: **{_format_bytes(stats.bytes_uploaded)}**",
        f"- GIF médio gerado: **{_format_bytes(average_size)}**",
        f"- Menor GIF gerado: **{_format_bytes(smallest)}**",
        f"- Maior GIF gerado: **{_format_bytes(largest)}**",
        f"- Tempo médio por página gerada: **{average_seconds:.2f}s**",
        f"- Tempo total: **{elapsed:.2f}s**",
        f"- Novo manifest publicado: **{'sim' if manifest_published else 'não'}**",
    ]
    if release_url:
        lines.append(f"- Release: {release_url}")
    if manifest_url:
        lines.append(f"- Manifest: {manifest_url}")
    return "\n".join(lines)


def publish_summary(markdown: str) -> None:
    print(markdown)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(markdown)
            handle.write("\n")


async def run_build() -> int:
    started = time.monotonic()
    config = BuilderConfig.load()
    env = Environment.load()

    LOGGER.info("Carregando catálogo público do VFX Studio.")
    catalog = await download_catalog(config)
    plans = build_page_plans(config, catalog, env.mode, env.test_page_limit)
    LOGGER.info(
        "Catálogo %s: %s flipbooks únicos; %s páginas selecionadas para modo %s.",
        catalog.catalog_version,
        catalog.total_unique_flipbooks,
        len(plans),
        env.mode,
    )

    stats = BuildStats()
    page_entries: list[tuple[PagePlan, dict[str, Any]]] = []
    manifest_published = False
    manifest_url: str | None = None
    release_url: str | None = None

    async with GitHubReleaseStore(env) as store:
        release_url = store.release_url
        previous_manifest_name, previous_manifest = await store.get_latest_manifest()
        previous_index = previous_page_index(previous_manifest)

        # Limpa somente sobras antigas do próprio builder. O manifest anterior é o
        # índice de verdade; não fazemos uma consulta HTTP individual por página.
        previous_keep = manifest_asset_names(previous_manifest)
        if previous_manifest_name:
            previous_keep.add(previous_manifest_name)
        removed_before = await store.cleanup_builder_assets(previous_keep)
        if removed_before:
            LOGGER.info("Removidos %s assets antigos antes da geração.", removed_before)
        existing_asset_names = set(store.assets)

        async with RobloxAssetClient(env, config) as roblox:
            for plan in plans:
                stats.pages_analyzed += 1
                previous = previous_index.get((plan.category, plan.page_number))
                if can_reuse_page(previous, plan, existing_asset_names):
                    stats.pages_reused += 1
                    asset_name = str(previous["asset_name"])
                    content_hash = str(previous["content_hash"])
                    width = int(previous.get("width", geometry(config)[0]))
                    height = int(previous.get("height", geometry(config)[1]))
                    entry = make_page_manifest_entry(
                        plan,
                        store,
                        asset_name=asset_name,
                        content_hash=content_hash,
                        size_bytes=int(previous["size_bytes"]),
                        failed_asset_ids=(),
                        fps=int(
                            previous.get(
                                "fps",
                                adaptive_fps(
                                    config,
                                    max(item.frame_count for item in plan.items),
                                ),
                            )
                        ),
                        output_frames=int(
                            previous.get(
                                "output_frames",
                                output_frame_count(
                                    config,
                                    max(item.frame_count for item in plan.items),
                                ),
                            )
                        ),
                        width=width,
                        height=height,
                        reused=True,
                        significant_failure=False,
                    )
                    page_entries.append((plan, entry))
                    LOGGER.info("Reaproveitando %s página %s.", plan.category, plan.page_number)
                    continue

                page_started = time.monotonic()
                try:
                    with tempfile.TemporaryDirectory(prefix="vfx-preview-page-") as temp_dir:
                        page_dir = Path(temp_dir)
                        slots = list(
                            await asyncio.gather(
                                *(roblox.download(item, page_dir) for item in plan.items)
                            )
                        )
                        output_path = page_dir / "preview.gif"
                        render = await asyncio.to_thread(
                            render_page_gif,
                            plan,
                            slots,
                            output_path,
                            config,
                        )
                        failed_count = len(render.failed_asset_ids)
                        stats.assets_failed += failed_count
                        significant_failure = (
                            bool(plan.items)
                            and failed_count / len(plan.items) >= config.significant_failure_ratio
                        )
                        if significant_failure:
                            LOGGER.warning(
                                "Página %s/%s teve %s de %s assets sem preview.",
                                plan.category,
                                plan.page_number,
                                failed_count,
                                len(plan.items),
                            )
                        if render.size_bytes >= config.large_gif_warning_bytes:
                            LOGGER.warning(
                                "Página %s/%s gerou GIF grande: %s bytes.",
                                plan.category,
                                plan.page_number,
                                render.size_bytes,
                            )

                        content_hash = await asyncio.to_thread(file_sha256, render.path)
                        asset_name = release_asset_name(plan, content_hash)
                        _asset, uploaded_bytes = await store.upload_file(render.path, asset_name)
                        stats.bytes_uploaded += uploaded_bytes
                        existing_asset_names.add(asset_name)
                        stats.pages_generated += 1
                        stats.generated_sizes.append(render.size_bytes)
                        stats.generated_seconds.append(time.monotonic() - page_started)
                        entry = make_page_manifest_entry(
                            plan,
                            store,
                            asset_name=asset_name,
                            content_hash=content_hash,
                            size_bytes=render.size_bytes,
                            failed_asset_ids=render.failed_asset_ids,
                            fps=render.fps,
                            output_frames=render.output_frames,
                            width=render.width,
                            height=render.height,
                            reused=False,
                            significant_failure=significant_failure,
                        )
                        page_entries.append((plan, entry))
                        LOGGER.info(
                            "Gerada %s página %s: %s bytes, %s falhas individuais.",
                            plan.category,
                            plan.page_number,
                            render.size_bytes,
                            failed_count,
                        )
                except RobloxAuthError:
                    raise
                except Exception:
                    stats.pages_failed += 1
                    LOGGER.exception(
                        "Falha completa ao gerar/enviar %s página %s.",
                        plan.category,
                        plan.page_number,
                    )

        if stats.pages_failed == 0 and len(page_entries) == len(plans):
            manifest = build_manifest(config, env, catalog, page_entries, stats)
            # Todas as páginas já existem na Release antes de o novo manifest ser
            # publicado. O manifest também ganha nome imutável baseado nos bytes.
            manifest_name, manifest_url, manifest_bytes = await store.upload_manifest(
                manifest,
                env.mode,
            )
            stats.bytes_uploaded += manifest_bytes
            manifest_published = True

            # Só depois do manifest válido existir removemos páginas/manifests velhos.
            keep_names = manifest_asset_names(manifest)
            keep_names.add(manifest_name)
            removed_after = await store.cleanup_builder_assets(keep_names)
            if removed_after:
                LOGGER.info("Removidos %s assets antigos após publicar o manifest.", removed_after)
            LOGGER.info("Manifest publicado com segurança em GitHub Releases: %s", manifest_url)
        else:
            LOGGER.error(
                "O manifest NÃO foi publicado porque houve %s falhas completas de página.",
                stats.pages_failed,
            )

    elapsed = time.monotonic() - started
    publish_summary(
        summary_markdown(
            stats,
            elapsed,
            manifest_published,
            release_url=release_url,
            manifest_url=manifest_url,
        )
    )
    return 0 if manifest_published else 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        return asyncio.run(run_build())
    except (MissingConfigurationError, InvalidCatalogError, NetworkError, RobloxAuthError) as error:
        LOGGER.error("Build interrompido: %s", error)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Build cancelado.")
        return 130
    except Exception:
        LOGGER.exception("Falha inesperada do gerador.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
