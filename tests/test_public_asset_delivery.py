from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from public_asset_delivery import (
    PUBLIC_ASSET_DELIVERY_URL,
    _delivery_location,
    _location_candidates,
    _open_cloud_delivery_location,
    _pick_cdn_location,
    _public_delivery_location,
    _safe_candidate_origins,
)


class _FakeContent:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, _limit: int) -> bytes:
        return self.payload


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.content = _FakeContent(json.dumps(payload).encode("utf-8"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None

    def get(self, url: str, *, headers: dict[str, str], **_kwargs):
        self.last_url = url
        self.last_headers = headers
        return self.response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.session = _FakeSession(response)
        self.config = SimpleNamespace(http_retries=2, delivery_timeout_seconds=2)
        self.api_key = "secret-test-key"

    async def _sleep_for_retry(self, _response, _attempt: int) -> None:
        return None


class PublicAssetDeliveryTests(unittest.TestCase):
    def test_public_endpoint_does_not_embed_credentials(self) -> None:
        self.assertEqual(
            PUBLIC_ASSET_DELIVERY_URL,
            "https://assetdelivery.roblox.com/v2/assetId/{asset_id}",
        )
        self.assertNotIn("api-key", PUBLIC_ASSET_DELIVERY_URL.casefold())

    def test_location_candidates_accept_direct_and_list_forms(self) -> None:
        payload = {
            "location": "https://a.rbxcdn.com/direct",
            "locations": [
                {"location": "https://b.rbxcdn.com/first"},
                {"location": "https://c.rbxcdn.com/second"},
            ],
        }
        self.assertEqual(
            _location_candidates(payload),
            (
                "https://a.rbxcdn.com/direct",
                "https://b.rbxcdn.com/first",
                "https://c.rbxcdn.com/second",
            ),
        )

    def test_pick_location_accepts_valid_rbxcdn_location_list(self) -> None:
        payload = {
            "locations": [
                {"location": "https://t0.rbxcdn.com/example-texture"},
            ]
        }
        self.assertEqual(
            _pick_cdn_location(payload),
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
            _pick_cdn_location(payload),
            "https://c0.rbxcdn.com/valid",
        )

    def test_pick_location_rejects_non_https_and_non_roblox_hosts(self) -> None:
        self.assertIsNone(
            _pick_cdn_location(
                {"locations": [{"location": "http://t0.rbxcdn.com/insecure"}]}
            )
        )
        self.assertIsNone(
            _pick_cdn_location(
                {
                    "locations": [
                        {"location": "https://rbxcdn.com.evil.example/file"}
                    ]
                }
            )
        )

    def test_pick_location_accepts_direct_compatible_location(self) -> None:
        self.assertEqual(
            _pick_cdn_location({"location": "https://rbxcdn.com/direct"}),
            "https://rbxcdn.com/direct",
        )

    def test_safe_diagnostics_never_include_path_or_query(self) -> None:
        payload = {
            "location": (
                "https://sc2.rbxcdn.com/private-path"
                "?__token__=should-never-appear"
            )
        }
        origins = _safe_candidate_origins(payload)
        self.assertEqual(origins, ("https://sc2.rbxcdn.com",))
        rendered = repr(origins)
        self.assertNotIn("private-path", rendered)
        self.assertNotIn("should-never-appear", rendered)

    def test_public_request_uses_no_api_key_header(self) -> None:
        client = _FakeClient(
            _FakeResponse(
                200,
                {"locations": [{"location": "https://t0.rbxcdn.com/public"}]},
            )
        )
        location = asyncio.run(_public_delivery_location(client, 123))
        self.assertEqual(location, "https://t0.rbxcdn.com/public")
        self.assertEqual(
            client.session.last_url,
            "https://assetdelivery.roblox.com/v2/assetId/123",
        )
        self.assertEqual(client.session.last_headers, {"Accept": "application/json"})
        self.assertNotIn("x-api-key", client.session.last_headers)

    def test_open_cloud_accepts_singular_location(self) -> None:
        client = _FakeClient(
            _FakeResponse(
                200,
                {"location": "https://sc2.rbxcdn.com/signed-texture"},
            )
        )
        location = asyncio.run(_open_cloud_delivery_location(client, 321))
        self.assertEqual(location, "https://sc2.rbxcdn.com/signed-texture")
        self.assertEqual(
            client.session.last_headers,
            {
                "Accept": "application/json",
                "x-api-key": "secret-test-key",
            },
        )

    def test_open_cloud_accepts_plural_locations(self) -> None:
        client = _FakeClient(
            _FakeResponse(
                200,
                {
                    "requestId": "example",
                    "locations": [
                        {"location": "https://fts.rbxcdn.com/signed-texture"}
                    ],
                },
            )
        )
        location = asyncio.run(_open_cloud_delivery_location(client, 654))
        self.assertEqual(location, "https://fts.rbxcdn.com/signed-texture")

    def test_delivery_uses_open_cloud_when_public_resolution_fails(self) -> None:
        client = _FakeClient(_FakeResponse(200, {}))

        with patch(
            "public_asset_delivery._public_delivery_location",
            new=AsyncMock(return_value=None),
        ), patch(
            "public_asset_delivery._open_cloud_delivery_location",
            new=AsyncMock(return_value="https://c0.rbxcdn.com/fallback"),
        ) as open_cloud:
            location = asyncio.run(_delivery_location(client, 456))

        self.assertEqual(location, "https://c0.rbxcdn.com/fallback")
        open_cloud.assert_awaited_once_with(client, 456)


if __name__ == "__main__":
    unittest.main()
