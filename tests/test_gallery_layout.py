from __future__ import annotations

import unittest

from PIL import Image

import gallery_layout
from generator import BuilderConfig, DecodedSlot
from sources.vfx_studio import VFXItem


class GalleryLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = BuilderConfig.load()

    def _item(self) -> VFXItem:
        return VFXItem(
            asset_id=123,
            name="Synthetic",
            category="Fire",
            grid=2,
            resolution=256,
            record_hash="synthetic",
        )

    def test_geometry_matches_full_bleed_2x3_layout(self) -> None:
        self.assertEqual(gallery_layout.geometry(self.config), (678, 976, 292))

    def test_preview_reaches_white_border_without_header_area(self) -> None:
        frame = Image.new("RGBA", (292, 292), (220, 40, 40, 255))
        decoded = [DecodedSlot(self._item(), [frame], None)]
        prepared = gallery_layout._build_static_page(
            decoded,
            self.config,
            page_indicator="1/1",
        )
        try:
            rendered = gallery_layout.render_rgba_frame(
                decoded,
                self.config,
                tick=0,
                prepared=prepared,
            )
            try:
                x = gallery_layout.MARGIN
                y = gallery_layout.MARGIN
                # A borda fica branca e o conteúdo começa imediatamente depois dela.
                self.assertEqual(
                    rendered.getpixel((x + 1, y + 20))[:3],
                    (244, 244, 244),
                )
                self.assertEqual(
                    rendered.getpixel(
                        (x + gallery_layout.BORDER_WIDTH + 2, y + 150)
                    )[:3],
                    (220, 40, 40),
                )
                # Não existe cabeçalho separado acima do preview.
                self.assertEqual(
                    rendered.getpixel(
                        (x + 150, y + gallery_layout.BORDER_WIDTH + 2)
                    )[:3],
                    (220, 40, 40),
                )
            finally:
                rendered.close()
        finally:
            prepared.close()
            frame.close()


if __name__ == "__main__":
    unittest.main()
