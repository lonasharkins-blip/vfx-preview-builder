from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from generator import (
    BuilderConfig,
    DownloadSlot,
    PagePlan,
    adaptive_fps,
    build_page_plans,
    can_reuse_page,
    decode_slot,
    geometry,
    output_frame_count,
    page_hash,
    manifest_asset_names,
    previous_page_index,
    release_asset_name,
    render_page_gif,
    sanitize_category,
)
from sources.vfx_studio import VFXItem, parse_catalog_bytes


class BuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = BuilderConfig.load()

    def _item(self, asset_id: int, grid: int, category: str = "Fire") -> VFXItem:
        return VFXItem(
            asset_id=asset_id,
            name=f"Asset {asset_id}",
            category=category,
            grid=grid,
            resolution=256,
            record_hash=f"record-{asset_id}-{grid}",
        )

    def _spritesheet(self, path: Path, grid: int) -> None:
        frame = 32
        image = Image.new("RGBA", (grid * frame, grid * frame), (0, 0, 0, 0))
        for row in range(grid):
            for column in range(grid):
                # Centro opaco e bordas transparentes para validar alpha no GIF.
                left = column * frame + 6
                top = row * frame + 6
                for x in range(left, left + 20):
                    for y in range(top, top + 20):
                        image.putpixel(
                            (x, y),
                            ((row * 53) % 255, (column * 71) % 255, 180, 255),
                        )
        image.save(path, format="PNG")
        image.close()

    def test_vfx_parser_keeps_only_2_4_8_grids(self) -> None:
        payload = {
            "1": {"Name": "A", "Keywords": ["Fire"], "Grid": 2, "Resolution": 256},
            "2": {"Name": "B", "Keywords": ["Fire"], "Grid": 4},
            "3": {"Name": "C", "Keywords": ["Fire"], "Grid": 8},
            "4": {"Name": "Static", "Keywords": ["Fire"]},
            "5": {"Name": "Invalid", "Keywords": ["Fire"], "Grid": 3},
        }
        catalog = parse_catalog_bytes(json.dumps(payload).encode())
        self.assertEqual([item.asset_id for item in catalog.categories["Fire"]], [1, 2, 3])
        self.assertEqual(catalog.total_unique_flipbooks, 3)

    def test_category_order_matches_bot(self) -> None:
        payload = {
            "1": {"Name": "A", "Keywords": ["Fire"], "Grid": 2},
            "2": {"Name": "B", "Keywords": ["Smoke"], "Grid": 2},
            "3": {"Name": "C", "Keywords": ["AAA New"], "Grid": 2},
        }
        catalog = parse_catalog_bytes(json.dumps(payload).encode())
        self.assertEqual(list(catalog.categories), ["Smoke", "Fire", "AAA New"])

    def test_safe_category_never_contains_path_segments(self) -> None:
        slug = sanitize_category("../../Fogo / Água ⚡")
        self.assertNotIn("..", slug)
        self.assertNotIn("/", slug)
        self.assertRegex(slug, r"^[a-z0-9-]+$")

    def test_page_hash_is_deterministic_and_config_sensitive(self) -> None:
        items = (self._item(1, 2), self._item(2, 4))
        first = page_hash(self.config, "Fire", items)
        second = page_hash(self.config, "Fire", items)
        self.assertEqual(first, second)
        changed = (self._item(1, 2), self._item(2, 8))
        self.assertNotEqual(first, page_hash(self.config, "Fire", changed))

    def test_timeline_never_uses_lcm(self) -> None:
        self.assertEqual(adaptive_fps(self.config, 4), 12)
        self.assertEqual(adaptive_fps(self.config, 16), 20)
        self.assertEqual(adaptive_fps(self.config, 64), 30)
        self.assertEqual(output_frame_count(self.config, 64), 64)

    def test_geometry_is_2_by_3_with_redesigned_gallery(self) -> None:
        width, height, content = geometry(self.config)
        self.assertEqual(width, 696)
        self.assertEqual(height, 958)
        self.assertEqual(content, 206)

    def test_test_mode_selects_only_requested_pages(self) -> None:
        payload = {}
        for asset_id in range(1, 30):
            payload[str(asset_id)] = {
                "Name": f"Asset {asset_id:02d}",
                "Keywords": ["Fire"],
                "Grid": 2,
            }
        catalog = parse_catalog_bytes(json.dumps(payload).encode())
        plans = build_page_plans(self.config, catalog, "test", 2)
        self.assertEqual(len(plans), 2)
        self.assertEqual(len(plans[0].items), 6)
        self.assertEqual(len(plans[1].items), 6)


    def test_frame_cut_order_is_row_major(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "row-major.png"
            # 2x2: vermelho, verde / azul, amarelo.
            image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            colors = ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 0, 255))
            for index, color in enumerate(colors):
                row, column = divmod(index, 2)
                tile = Image.new("RGBA", (16, 16), color)
                image.alpha_composite(tile, (column * 16, row * 16))
                tile.close()
            image.save(path)
            image.close()

            decoded = decode_slot(DownloadSlot(self._item(99, 2), path), self.config)
            try:
                center = geometry(self.config)[2] // 2
                observed = [frame.getpixel((center, center)) for frame in decoded.frames]
                self.assertEqual(observed, list(colors))
            finally:
                decoded.close()

    def test_full_mode_keeps_short_last_page(self) -> None:
        payload = {}
        for asset_id in range(1, 30):
            payload[str(asset_id)] = {
                "Name": f"Asset {asset_id:02d}",
                "Keywords": ["Fire"],
                "Grid": 2,
            }
        catalog = parse_catalog_bytes(json.dumps(payload).encode())
        plans = build_page_plans(self.config, catalog, "full", 2)
        self.assertEqual([len(plan.items) for plan in plans], [6, 6, 6, 6, 5])

    def test_incremental_reuses_clean_page_but_retries_failed_page(self) -> None:
        item = self._item(1, 2)
        digest = page_hash(self.config, "Fire", (item,))
        plan = PagePlan(
            "Fire", sanitize_category("Fire"), 1, 1, (item,), digest,
            "vfx-studio/pages/fire/001.gif",
        )
        previous = {
            "hash": digest,
            "logical_path": plan.logical_path,
            "asset_name": "page--fire--001--abc--def.gif",
            "content_hash": "sha256-deadbeef",
            "size_bytes": 100,
            "failed_asset_ids": [],
        }
        existing = {previous["asset_name"]}
        self.assertTrue(can_reuse_page(previous, plan, existing))
        self.assertFalse(can_reuse_page(previous, plan, set()))
        previous["failed_asset_ids"] = [1]
        self.assertFalse(can_reuse_page(previous, plan, existing))

    def test_release_asset_name_is_flat_safe_and_content_versioned(self) -> None:
        item = self._item(7, 4, "Fire / Magic")
        digest = page_hash(self.config, item.category, (item,))
        plan = PagePlan(
            item.category, sanitize_category(item.category), 3, 9, (item,), digest,
            "vfx-studio/pages/fire/003.gif",
        )
        first = release_asset_name(plan, "sha256-" + "a" * 64)
        second = release_asset_name(plan, "sha256-" + "b" * 64)
        self.assertTrue(first.startswith("page--"))
        self.assertTrue(first.endswith(".gif"))
        self.assertNotIn("/", first)
        self.assertNotEqual(first, second)

    def test_manifest_asset_names_collects_only_published_pages(self) -> None:
        manifest = {
            "categories": [{
                "name": "Fire",
                "pages": [
                    {"number": 1, "asset_name": "page--one.gif"},
                    {"number": 2, "asset_name": "page--two.gif"},
                ],
            }]
        }
        self.assertEqual(
            manifest_asset_names(manifest), {"page--one.gif", "page--two.gif"}
        )

    def test_previous_manifest_is_indexed_once(self) -> None:
        manifest = {
            "categories": [
                {"name": "Fire", "pages": [{"number": 1, "hash": "abc"}]},
                {"name": "Smoke", "pages": [{"number": 2, "hash": "def"}]},
            ]
        }
        index = previous_page_index(manifest)
        self.assertEqual(index[("Fire", 1)]["hash"], "abc")
        self.assertEqual(index[("Smoke", 2)]["hash"], "def")


    def test_gif_static_design_colors_stay_identical_between_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "animated.png"
            self._spritesheet(path, 8)
            item = self._item(77, 8)
            slot = DownloadSlot(item, path)
            output = root / "stable-colors.gif"
            plan = PagePlan(
                "Fire", sanitize_category("Fire"), 1, 1, (item,),
                page_hash(self.config, "Fire", (item,)),
                "vfx-studio/test/pages/fire/001.gif",
            )
            render_page_gif(plan, [slot], output, self.config)

            width, _height, _content = geometry(self.config)
            grid_top = self.config.margin + self.config.page_header_height + self.config.gap
            samples = (
                (1, 1),
                (10, 10),
                (self.config.margin + 4, grid_top + 4),
                (self.config.margin + 4, grid_top + self.config.card_header_height + 4),
                (width - 10, 10),
            )
            observed: list[tuple[tuple[int, int, int], ...]] = []
            with Image.open(output) as gif:
                for frame_index in range(getattr(gif, "n_frames", 1)):
                    gif.seek(frame_index)
                    rgb = gif.convert("RGB")
                    try:
                        observed.append(tuple(rgb.getpixel(point) for point in samples))
                    finally:
                        rgb.close()
            self.assertGreater(len(observed), 1)
            self.assertTrue(all(frame == observed[0] for frame in observed[1:]))

    def test_render_mixed_2_4_8_grid_and_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            slots = []
            for asset_id, grid in ((1, 2), (2, 4), (3, 8)):
                path = root / f"{asset_id}.png"
                self._spritesheet(path, grid)
                slots.append(DownloadSlot(self._item(asset_id, grid), path))
            slots.append(DownloadSlot(self._item(4, 4), None, "falha sintética"))

            output = root / "page.gif"
            render_config = replace(
                self.config, page_header_height=32, card_width=120,
                card_header_height=30, preview_height=88,
                content_padding=4, gap=4, margin=4,
            )
            plan = PagePlan("Fire", sanitize_category("Fire"), 1, 1, tuple(slot.item for slot in slots), page_hash(render_config, "Fire", tuple(slot.item for slot in slots)), "vfx-studio/test/pages/fire/001.gif")
            result = render_page_gif(plan, slots, output, render_config)
            self.assertTrue(output.is_file())
            self.assertEqual(result.failed_asset_ids, (4,))
            self.assertEqual(result.output_frames, 64)
            self.assertEqual(result.fps, 30)
            self.assertEqual((result.width, result.height), geometry(render_config)[:2])

            with Image.open(output) as gif:
                self.assertEqual(getattr(gif, "n_frames", 1), 64)


if __name__ == "__main__":
    unittest.main()
