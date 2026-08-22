from __future__ import annotations

import unittest

from public_asset_delivery import PUBLIC_ASSET_DELIVERY_URL, _pick_public_location


class PublicAssetDeliveryTests(unittest.TestCase):
    def test_public_endpoint_does_not_embed_credentials(self) -> None:
        self.assertEqual(
            PUBLIC_ASSET_DELIVERY_URL,
            "https://assetdelivery.roblox.com/v2/assetId/{asset_id}",
        )
        self.assertNotIn("api-key", PUBLIC_ASSET_DELIVERY_URL.casefold())

    def test_pick_location_accepts_valid_rbxcdn_location_list(self) -> None:
        payload = {
            "locations": [
                {"location": "https://t0.rbxcdn.com/example-texture"},
            ]
        }
        self.assertEqual(
            _pick_public_location(payload),
            "https://t0.rbxcdn.com/example-texture",
        )

    def test_pick_location_skips_invalid_entry_before_valid_one(self) -> None:
        payload = {
            "locations": [
                {"location": "https://example.com/not-roblox"},
                {"location": "https://c0.rbxcdn.com/valid"},
            ]
        }
        self.assertEqual(
            _pick_public_location(payload),
            "https://c0.rbxcdn.com/valid",
        )

    def test_pick_location_rejects_non_https_and_non_roblox_hosts(self) -> None:
        self.assertIsNone(
            _pick_public_location(
                {"locations": [{"location": "http://t0.rbxcdn.com/insecure"}]}
            )
        )
        self.assertIsNone(
            _pick_public_location(
                {"locations": [{"location": "https://rbxcdn.com.evil.example/file"}]}
            )
        )

    def test_pick_location_accepts_direct_compatible_location(self) -> None:
        self.assertEqual(
            _pick_public_location({"location": "https://rbxcdn.com/direct"}),
            "https://rbxcdn.com/direct",
        )


if __name__ == "__main__":
    unittest.main()
