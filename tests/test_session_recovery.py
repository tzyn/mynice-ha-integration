"""Regression tests for cloud authentication and device reconnection."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import time
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "mynice"
PACKAGE = "_mynice_session_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", COMPONENT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ha_modules = {
    name: ModuleType(name)
    for name in (
        "homeassistant",
        "homeassistant.const",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.dispatcher",
    )
}
ha_modules["homeassistant.const"].Platform = MagicMock()
ha_modules["homeassistant.config_entries"].ConfigEntry = MagicMock()
ha_modules["homeassistant.core"].HomeAssistant = MagicMock()
ha_modules["homeassistant.helpers.dispatcher"].async_dispatcher_send = MagicMock()
with patch.dict(sys.modules, ha_modules):
    api = load_module("api")
    coordinator = load_module("coordinator")


def response(status, payload):
    result = MagicMock()
    result.status = status
    result.json = AsyncMock(return_value=payload)
    result.text = AsyncMock(return_value="rejected")
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=result)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


class CloudRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = MagicMock()
        self.save_token = MagicMock()
        self.cloud = api.NiceCloud(
            self.session,
            username="user",
            password="password",
            token={
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "_expires_at": time.time() + 3600,
            },
            on_token=self.save_token,
        )

    async def test_unauthorized_refreshes_and_retries_with_new_token(self):
        self.session.request.side_effect = [
            response(401, {}), response(200, {"data": "ok"})
        ]
        self.cloud._token_request = AsyncMock(return_value={
            "access_token": "new-access", "expires_in": 3600
        })

        result = await self.cloud._request("GET", "resource")

        self.assertEqual(result, {"data": "ok"})
        self.cloud._token_request.assert_awaited_once_with({
            "grant_type": "refresh_token", "refresh_token": "old-refresh"
        })
        self.assertEqual(self.cloud.token["refresh_token"], "old-refresh")
        self.assertEqual(
            self.session.request.call_args.kwargs["headers"]["Authorization"],
            "Bearer new-access",
        )
        self.save_token.assert_called_once_with(self.cloud.token)

    async def test_expired_token_refreshes_without_password_login(self):
        self.cloud.token["_expires_at"] = 0
        self.cloud._token_request = AsyncMock(return_value={
            "access_token": "new-access", "refresh_token": "new-refresh"
        })

        await self.cloud.async_ensure_token()

        self.cloud._token_request.assert_awaited_once_with({
            "grant_type": "refresh_token", "refresh_token": "old-refresh"
        })
        self.assertEqual(self.cloud.token["refresh_token"], "new-refresh")

    async def test_rejected_refresh_falls_back_to_password(self):
        self.session.request.side_effect = [response(401, {}), response(200, {})]
        self.cloud._token_request = AsyncMock(side_effect=[
            api.NiceAuthError("refresh rejected"),
            {"access_token": "login-access", "refresh_token": "login-refresh"},
        ])

        await self.cloud._request("GET", "resource")

        self.assertEqual(self.cloud._token_request.await_count, 2)
        self.assertEqual(self.cloud._token_request.call_args.args[0], {
            "grant_type": "password", "username": "user", "password": "password"
        })
        self.assertEqual(self.cloud.token["access_token"], "login-access")

    async def test_repeated_unauthorized_is_bounded_auth_failure(self):
        self.session.request.side_effect = [response(401, {}), response(401, {})]
        self.cloud._token_request = AsyncMock(return_value={
            "access_token": "new-access"
        })

        with self.assertRaises(api.NiceAuthError):
            await self.cloud._request("GET", "resource")

        self.assertEqual(self.session.request.call_count, 2)


class DeviceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_creds = {"user": "old-user", "password": "old-password", "controller": "old-id"}
        self.new_creds = {"user": "new-user", "password": "new-password", "controller": "new-id"}
        self.door = {
            "mac": "gate-mac", "device_id": "1", "automation_id": 10,
            "creds": self.old_creds,
        }
        self.cloud = MagicMock()
        self.cloud.async_discover = AsyncMock(return_value=[
            {**self.door, "creds": self.new_creds}
        ])
        self.hub = coordinator.NiceHub(MagicMock(), MagicMock(), self.cloud, [self.door])
        self.client = MagicMock()
        self.client.open = AsyncMock()
        self.client.add_session = AsyncMock()
        self.client.send_status = AsyncMock()
        self.client.sessions = {"gate-mac": MagicMock()}
        self.hub._client = self.client

    async def test_reconnect_uses_rediscovered_device_credentials(self):
        connected = await self.hub._connect_all()

        self.cloud.async_discover.assert_awaited_once()
        self.client.add_session.assert_awaited_once_with(
            "gate-mac", "new-user", "new-password", "new-id"
        )
        self.client.send_status.assert_awaited_once_with("gate-mac")
        self.assertEqual(connected, 1)
        self.assertTrue(self.hub.available_for(10))

    async def test_missing_credentials_do_not_reuse_stale_credentials(self):
        self.cloud.async_discover.return_value = [{**self.door, "creds": None}]

        with self.assertRaises(api.NiceApiError):
            await self.hub._connect_all()

        self.client.open.assert_not_awaited()
        self.client.add_session.assert_not_awaited()

    async def test_cloud_failure_does_not_open_socket_with_stale_credentials(self):
        self.cloud.async_discover.side_effect = api.NiceApiError("cloud unavailable")

        with self.assertRaises(api.NiceApiError):
            await self.hub._connect_all()

        self.client.open.assert_not_awaited()

    async def test_silent_socket_is_closed_after_missing_status_replies(self):
        clock = [0.0]

        async def read_frame(**kwargs):
            clock[0] = coordinator.KEEPALIVE_INTERVAL * 3
            return None

        async def close():
            self.hub._stopping = True

        self.client.read_frame = AsyncMock(side_effect=read_frame)
        self.client.close = AsyncMock(side_effect=close)

        with patch.object(coordinator, "monotonic", side_effect=lambda: clock[0]):
            await self.hub._run()

        self.client.read_frame.assert_awaited_once()
        self.client.close.assert_awaited_once()
        self.assertFalse(self.hub.available_for(10))

    async def test_reconnect_delay_increases_and_is_capped(self):
        self.hub._connect_all = AsyncMock(side_effect=api.NiceApiError("offline"))
        self.client.close = AsyncMock()
        delays = []

        async def retry_sleep(delay):
            delays.append(delay)
            if len(delays) == 8:
                self.hub._stopping = True

        with patch.object(coordinator.asyncio, "sleep", side_effect=retry_sleep):
            await self.hub._run()

        self.assertEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(self.hub._connect_all.await_count, 8)

    async def test_reconnect_delay_resets_only_after_status_reply(self):
        self.hub._connect_all = AsyncMock(side_effect=[
            api.NiceApiError("offline"), 1, 1, api.NiceApiError("offline")
        ])
        self.client.read_frame = AsyncMock(side_effect=[
            api.NiceApiError("socket closed before status"),
            '<Device id="1"><DoorStatus>closed</DoorStatus></Device>',
            api.NiceApiError("socket closed after status"),
        ])
        self.client.route.return_value = "gate-mac"
        self.client.close = AsyncMock()
        delays = []

        async def retry_sleep(delay):
            delays.append(delay)
            if len(delays) == 4:
                self.hub._stopping = True

        with (
            patch.object(coordinator.asyncio, "sleep", side_effect=retry_sleep),
            patch.object(coordinator, "monotonic", return_value=0),
        ):
            await self.hub._run()

        self.assertEqual(delays, [5, 10, 5, 10])
        self.assertEqual(self.hub.state_for(10), "closed")

    async def test_reconnect_wait_can_be_cancelled(self):
        self.hub._connect_all = AsyncMock(side_effect=api.NiceApiError("offline"))
        self.client.close = AsyncMock()

        with patch.object(
            coordinator.asyncio, "sleep", side_effect=coordinator.asyncio.CancelledError
        ):
            with self.assertRaises(coordinator.asyncio.CancelledError):
                await self.hub._run()

        self.hub._connect_all.assert_awaited_once()
        self.client.close.assert_awaited_once()

    async def test_removed_credentials_mark_previously_connected_door_unavailable(self):
        self.hub._available[10] = True
        self.cloud.async_discover.return_value = []

        async def close():
            self.hub._stopping = True

        self.client.close = AsyncMock(side_effect=close)

        await self.hub._run()

        self.assertFalse(self.hub.available_for(10))
        self.client.open.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()