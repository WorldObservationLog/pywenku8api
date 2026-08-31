import unittest
from unittest.mock import AsyncMock, patch

from wenku8.api import Wenku8API


CHALLENGE = '<html><head><title>Just a moment...</title></head></html>'
NORMAL = '<html><head><title>Wenku8</title></head><body>ok</body></html>'


class FakeTab:
    def __init__(self, contents):
        self.get_content = AsyncMock(side_effect=contents)
        self.wait_for_ready_state = AsyncMock()
        self.verify_cf = AsyncMock()
        self.reload = AsyncMock()


class CloudflareWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_noninteractive_challenge_is_allowed_to_complete_without_reload(self):
        tab = FakeTab([CHALLENGE, CHALLENGE, NORMAL, NORMAL])
        api = Wenku8API()
        with (
            patch('wenku8.api.cf_is_interactive_challenge_present', new=AsyncMock(return_value=False)) as detect,
            patch('wenku8.api.asyncio.sleep', new=AsyncMock()),
            patch('wenku8.api.time.monotonic', return_value=0.0),
        ):
            result = await api._wait_cf(tab)

        self.assertEqual(result, NORMAL)
        self.assertEqual(detect.await_count, 2)
        tab.verify_cf.assert_not_awaited()
        tab.reload.assert_not_awaited()

    async def test_interactive_challenge_is_verified_once_without_reload(self):
        tab = FakeTab([CHALLENGE, NORMAL, NORMAL])
        api = Wenku8API()
        with (
            patch('wenku8.api.cf_is_interactive_challenge_present', new=AsyncMock(return_value=True)),
            patch('wenku8.api.asyncio.sleep', new=AsyncMock()),
            patch('wenku8.api.time.monotonic', return_value=0.0),
        ):
            result = await api._wait_cf(tab)

        self.assertEqual(result, NORMAL)
        tab.verify_cf.assert_awaited_once_with(timeout=15.0)
        tab.reload.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
