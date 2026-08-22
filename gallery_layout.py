"""Layout visual 2x3 usado pelas páginas GIF publicadas do VFX Preview Builder.

O gerador de catálogo/download/publicação continua em ``generator.py``. Este módulo
substitui somente a geometria e a composição visual antes de iniciar o gerador,
para manter o ajuste de layout isolado do restante da infraestrutura.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

import generator


COLUMNS = 2
ROWS = 3
ITEMS_PER_PAGE = COLUMNS * ROWS
CELL_SIZE = 300
COLUMN_GAP = 42
ROW_GAP = 20
MARGIN = 18
BORDER_WIDTH = 4
BORDER_RADIUS = 10
BACKGROUND = (36, 36, 36, 255)
PREVIEW_BACKGROUND = (0, 0, 0, 255)
BORDER = (244, 244, 244, 255)
DIVIDER = (8, 8, 8, 255)
NUMBER = (248, 248, 248, 255)
NUMBER_BADGE = (10, 10, 10, 220)
MUTED = (176, 176, 176, 255)

_ORIGINAL_BUILD_MANIFEST = generator.build_manifest


def content_square_size(_config: generator.BuilderConfig) -> int:
    """Área útil: todo o quadrado, descontando apenas a própria borda."""

    return CELL_SIZE - BORDER_WIDTH * 2


def card_height(_config: generator.BuilderConfig) -> int:
    return CELL_SIZE


def geometry(_config: generator.BuilderConfig) -> tuple[int, int, int]:
    width = MARGIN * 2 + COLUMNS * CELL_SIZE + COLUMN_GAP
    height = MARGIN * 2 + ROWS * CELL_SIZE + (ROWS - 1) * ROW_GAP
    return width, height, content_square_size(_config)


def _slot_xy(slot_index: int) -> tuple[int, int]:
    row, column = divmod(slot_index, COLUMNS)
    x = MARGIN + column * (CELL_SIZE + COLUMN_GAP)
    y = MARGIN + row * (CELL_SIZE + ROW_GAP)
    return x, y


def _rounded_inner_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=max(1, BORDER_RADIUS - BORDER_WIDTH),
        fill=255,
    )
    return mask


def _draw_divider(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    x = width // 2
    dash_height = 8
    dash_gap = 7
    y = MARGIN
    while y < height - MARGIN:
        draw.line(
            (x, y, x, min(y + dash_height, height - MARGIN)),
            fill=DIVIDER,
            width=3,
        )
        y += dash_height + dash_gap


def _build_static_page(
    decoded: list[generator.DecodedSlot],
    config: generator.BuilderConfig,
    *,
    page_indicator: str,
) -> generator.PreparedPage:
    """Cria somente fundo, bordas e placeholders; sem cabeçalho/Texture ID."""

    del page_indicator  # A paginação já existe nos controles da Sha5.
    width, height, content_size = geometry(config)
    canvas = Image.new("RGBA", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    placeholder_font = generator._font(15)
    dash_font = generator._font(18)

    _draw_divider(draw, width, height)
    layouts: list[generator.SlotLayout] = []

    for slot_index in range(ITEMS_PER_PAGE):
        x, y = _slot_xy(slot_index)
        card_box = (x, y, x + CELL_SIZE - 1, y + CELL_SIZE - 1)
        draw.rounded_rectangle(
            card_box,
            radius=BORDER_RADIUS,
            fill=PREVIEW_BACKGROUND,
            outline=BORDER,
            width=BORDER_WIDTH,
        )

        inner_left = x + BORDER_WIDTH
        inner_top = y + BORDER_WIDTH
        inner_right = inner_left + content_size - 1
        inner_bottom = inner_top + content_size - 1
        layouts.append(
            generator.SlotLayout(
                (inner_left, inner_top, inner_right, inner_bottom),
                (inner_left, inner_top),
            )
        )

        if slot_index >= len(decoded):
            generator._centered_text(
                draw,
                card_box,
                "—",
                dash_font,
                MUTED,
            )
            continue

        if not decoded[slot_index].frames:
            generator._centered_text(
                draw,
                (
                    inner_left + 12,
                    inner_top + 12,
                    inner_right - 12,
                    inner_bottom - 12,
                ),
                "Prévia indisponível",
                placeholder_font,
                MUTED,
            )

    return generator.PreparedPage(canvas, layouts)


def _draw_slot_number(
    draw: ImageDraw.ImageDraw,
    slot_index: int,
    config: generator.BuilderConfig,
) -> None:
    del config
    x, y = _slot_xy(slot_index)
    text = str(slot_index + 1)
    font = generator._font(20)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    badge_left = x + BORDER_WIDTH + 8
    badge_top = y + BORDER_WIDTH + 8
    badge_width = max(32, text_width + 18)
    badge_height = max(30, text_height + 12)
    badge_right = badge_left + badge_width
    badge_bottom = badge_top + badge_height
    draw.rounded_rectangle(
        (badge_left, badge_top, badge_right, badge_bottom),
        radius=8,
        fill=NUMBER_BADGE,
    )
    draw.text(
        (
            badge_left + (badge_width - text_width) // 2,
            badge_top + (badge_height - text_height) // 2 - 1,
        ),
        text,
        font=font,
        fill=NUMBER,
    )


def render_rgba_frame(
    decoded: list[generator.DecodedSlot],
    config: generator.BuilderConfig,
    *,
    tick: int,
    prepared: generator.PreparedPage,
) -> Image.Image:
    """Compõe o efeito até a borda branca e desenha a numeração por cima."""

    _width, _height, content_size = geometry(config)
    canvas = prepared.base.copy()
    rounded_mask = _rounded_inner_mask(content_size)
    try:
        for slot_index, slot in enumerate(decoded[:ITEMS_PER_PAGE]):
            if not slot.frames:
                continue
            source_frame = slot.frames[tick % len(slot.frames)]
            if source_frame.size != (content_size, content_size):
                effect_frame = source_frame.resize(
                    (content_size, content_size),
                    Image.Resampling.LANCZOS,
                )
            else:
                effect_frame = source_frame

            try:
                if effect_frame.mode != "RGBA":
                    rgba = effect_frame.convert("RGBA")
                else:
                    rgba = effect_frame
                try:
                    alpha = rgba.getchannel("A")
                    try:
                        combined_mask = ImageChops.multiply(alpha, rounded_mask)
                        try:
                            canvas.paste(
                                rgba,
                                prepared.layouts[slot_index].effect_origin,
                                combined_mask,
                            )
                        finally:
                            combined_mask.close()
                    finally:
                        alpha.close()
                finally:
                    if rgba is not effect_frame:
                        rgba.close()
            finally:
                if effect_frame is not source_frame:
                    effect_frame.close()
    finally:
        rounded_mask.close()

    draw = ImageDraw.Draw(canvas)
    for slot_index in range(min(len(decoded), ITEMS_PER_PAGE)):
        x, y = _slot_xy(slot_index)
        # Repassa a borda sobre o frame para ela permanecer perfeitamente limpa.
        draw.rounded_rectangle(
            (x, y, x + CELL_SIZE - 1, y + CELL_SIZE - 1),
            radius=BORDER_RADIUS,
            outline=BORDER,
            width=BORDER_WIDTH,
        )
        _draw_slot_number(draw, slot_index, config)
    return canvas


def build_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Mantém o manifest original, corrigindo apenas os metadados do layout."""

    manifest = _ORIGINAL_BUILD_MANIFEST(*args, **kwargs)
    generator_info = manifest.get("generator")
    if isinstance(generator_info, dict):
        generator_info["layout"] = {
            "columns": COLUMNS,
            "rows": ROWS,
            "page_header_height": 0,
            "card_width": CELL_SIZE,
            "card_header_height": 0,
            "preview_height": CELL_SIZE,
            "column_gap": COLUMN_GAP,
            "row_gap": ROW_GAP,
            "margin": MARGIN,
            "border_width": BORDER_WIDTH,
            "labels": "1-6 overlay",
            "preview_mode": "full-bleed",
        }
    return manifest


def install() -> None:
    """Instala somente as funções visuais antes de executar o builder normal."""

    generator_source = Path(generator.__file__).read_bytes()
    layout_source = Path(__file__).read_bytes()
    generator.GENERATOR_SOURCE_SHA256 = hashlib.sha256(
        generator_source + b"\0gallery-layout\0" + layout_source
    ).hexdigest()
    generator.content_square_size = content_square_size
    generator.card_height = card_height
    generator.geometry = geometry
    generator._build_static_page = _build_static_page
    generator.render_rgba_frame = render_rgba_frame
    generator.build_manifest = build_manifest


def main() -> int:
    install()
    return generator.main()


if __name__ == "__main__":
    raise SystemExit(main())
