from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Final, Iterable, List, Optional
from urllib.parse import urlparse

import latest_user_agents
import user_agents
import zendriver
from zendriver import cdp
from zendriver.cdp.emulation import UserAgentBrandVersion, UserAgentMetadata
from zendriver.cdp.fetch import AuthChallengeResponse, AuthRequired, RequestPaused
from zendriver.cdp.network import T_JSON_DICT, Cookie
from zendriver.cdp.network import T_JSON_DICT, Cookie
from zendriver.core.element import Element


def get_chrome_user_agent() -> str:
    """
    Get a random up-to-date Chrome user agent string.

    Returns
    -------
    str
        The user agent string.
    """
    chrome_user_agents = [
        user_agent
        for user_agent in latest_user_agents.get_latest_user_agents()
        if "Chrome" in user_agent and "Edg" not in user_agent
    ]

    return random.choice(chrome_user_agents)


class ChallengePlatform(Enum):
    """Cloudflare challenge platform types."""

    JAVASCRIPT = "non-interactive"
    MANAGED = "managed"
    INTERACTIVE = "interactive"


@dataclass
class Proxy:
    """A class representing a proxy server."""

    scheme: str
    host: str
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None

    @classmethod
    def from_url(cls, proxy_url: str) -> Proxy:
        """
        Create a Proxy instance from a proxy URL.

        Parameters
        ----------
        proxy_url : str
            The proxy server URL.

        Returns
        -------
        Proxy
            The Proxy instance.
        """
        parsed = urlparse(proxy_url)

        return cls(
            scheme=parsed.scheme,
            host=parsed.hostname or "",
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
        )

    @property
    def url(self) -> str:
        """
        Get the proxy URL without authentication.

        Returns
        -------
        str
            The proxy URL.
        """
        host = self.host

        if self.port:
            host += f":{self.port}"

        return f"{self.scheme}://{host}"


class CloudflareSolver:
    """
    A class for solving Cloudflare challenges with Zendriver.

    Parameters
    ----------
    user_agent : Optional[str]
        The user agent string to use for the browser requests.
    timeout : float
        The timeout in seconds to use for browser actions and solving challenges.
    http2 : bool
        Enable or disable the usage of HTTP/2 for the browser requests.
    http3 : bool
        Enable or disable the usage of HTTP/3 for the browser requests.
    headless : bool
        Enable or disable headless mode for the browser (not supported on Windows).
    proxy : Optional[str]
        The proxy server URL to use for the browser requests.
    """

    def __init__(
        self,
        *,
        user_agent: Optional[str],
        timeout: float,
        http2: bool,
        http3: bool,
        headless: bool,
        proxy: Optional[str],
    ) -> None:
        config = zendriver.Config(headless=headless, sandbox=False)

        if user_agent is not None:
            config.add_argument(f"--user-agent={user_agent}")

        if not http2:
            config.add_argument("--disable-http2")

        if not http3:
            config.add_argument("--disable-quic")

        self._proxy = Proxy.from_url(proxy) if proxy is not None else None

        if self._proxy is not None:
            config.add_argument(f"--proxy-server={self._proxy.url}")

        self.driver = zendriver.Browser(config)
        self._timeout = timeout

    async def __aenter__(self) -> CloudflareSolver:
        await self.driver.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.driver.stop()

    async def _on_auth_required(self, event: AuthRequired) -> None:
        """
        Handle authentication requests for the proxy server.

        Parameters
        ----------
        event : AuthRequired
            The authentication required event.
        """
        if event.auth_challenge.source == "Proxy":
            await self.driver.main_tab.send(
                cdp.fetch.continue_with_auth(
                    event.request_id,
                    AuthChallengeResponse(
                        response="ProvideCredentials",
                        username=self._proxy.username,
                        password=self._proxy.password,
                    ),
                )
            )
        else:
            await self.driver.main_tab.send(
                cdp.fetch.continue_with_auth(
                    event.request_id, AuthChallengeResponse(response="Default")
                )
            )

    async def _continue_request(self, event: RequestPaused) -> None:
        """
        Continue a paused request.

        Parameters
        ----------
        event : RequestPaused
            The request paused event.
        """
        await self.driver.main_tab.send(
            cdp.fetch.continue_request(request_id=event.request_id)
        )

    @staticmethod
    def _format_cookies(cookies: Iterable[Cookie]) -> List[T_JSON_DICT]:
        """
        Format cookies into a list of JSON cookies.

        Parameters
        ----------
        cookies : Iterable[Cookie]
            List of cookies.

        Returns
        -------
        List[T_JSON_DICT]
            List of JSON cookies.
        """
        return [cookie.to_json() for cookie in cookies]

    @staticmethod
    def extract_clearance_cookie(
        cookies: Iterable[T_JSON_DICT],
    ) -> Optional[T_JSON_DICT]:
        """
        Extract the Cloudflare clearance cookie from a list of cookies.

        Parameters
        ----------
        cookies : Iterable[T_JSON_DICT]
            List of cookies.

        Returns
        -------
        Optional[T_JSON_DICT]
            The Cloudflare clearance cookie. Returns None if the cookie is not found.
        """

        for cookie in cookies:
            if cookie["name"] == "cf_clearance":
                return cookie

        return None

    async def get_user_agent(self) -> str:
        """
        Get the current user agent string.

        Returns
        -------
        str
            The user agent string.
        """
        return await self.driver.main_tab.evaluate("navigator.userAgent")

    async def get_cookies(self) -> List[T_JSON_DICT]:
        """
        Get all cookies from the current page.

        Returns
        -------
        List[T_JSON_DICT]
            List of cookies.
        """
        return self._format_cookies(await self.driver.cookies.get_all())

    async def set_user_agent_metadata(self, user_agent: str) -> None:
        """
        Set the user agent metadata for the browser.

        Parameters
        ----------
        user_agent : str
            The user agent string to parse information from.
        """
        device = user_agents.parse(user_agent)

        metadata = UserAgentMetadata(
            architecture="x86",
            bitness="64",
            brands=[
                UserAgentBrandVersion(brand="Not)A;Brand", version="8"),
                UserAgentBrandVersion(
                    brand="Chromium", version=str(device.browser.version[0])
                ),
                UserAgentBrandVersion(
                    brand="Google Chrome",
                    version=str(device.browser.version[0]),
                ),
            ],
            full_version_list=[
                UserAgentBrandVersion(brand="Not)A;Brand", version="8"),
                UserAgentBrandVersion(
                    brand="Chromium", version=str(device.browser.version[0])
                ),
                UserAgentBrandVersion(
                    brand="Google Chrome",
                    version=str(device.browser.version[0]),
                ),
            ],
            mobile=device.is_mobile,
            model=device.device.model or "",
            platform=device.os.family,
            platform_version=device.os.version_string,
            full_version=device.browser.version_string,
            wow64=False,
        )

        self.driver.main_tab.feed_cdp(
            cdp.network.set_user_agent_override(
                user_agent, user_agent_metadata=metadata
            )
        )

    async def request_page(self, url: str) -> None:
        """
        Request a page with the browser,
        setting up handlers for proxy authentication if a proxy is being used.

        Parameters
        ----------
        url : str
            The URL of the page to request.
        """
        if self._proxy is not None:
            await self.driver.get()
            self.driver.main_tab.add_handler(AuthRequired, self._on_auth_required)
            self.driver.main_tab.add_handler(RequestPaused, self._continue_request)
            self.driver.main_tab.feed_cdp(cdp.fetch.enable(handle_auth_requests=True))

        await self.driver.get(url)

    async def detect_challenge(self) -> Optional[ChallengePlatform]:
        """
        Detect the Cloudflare challenge platform on the current page.

        Returns
        -------
        Optional[ChallengePlatform]
            The Cloudflare challenge platform.
        """
        html = await self.driver.main_tab.get_content()

        for platform in ChallengePlatform:
            if f"cType: '{platform.value}'" in html:
                return platform

        return None

    async def solve_challenge(self) -> None:
        """Solve the Cloudflare challenge on the current page."""
        start_timestamp = datetime.now()

        while (
            self.extract_clearance_cookie(await self.get_cookies()) is None
            and await self.detect_challenge() is not None
            and (datetime.now() - start_timestamp).seconds < self._timeout
        ):
            await self.driver.main_tab.verify_cf()


async def get_cloudflare_clearance(
    url: str,
    timeout: float = 30.0,
    headless: bool = True,
    proxy: Optional[str] = None,
    user_agent: Optional[str] = None,
    disable_http2: bool = False,
    disable_http3: bool = False
) -> tuple[str, dict]:
    """
    通过运行无头浏览器（Zendriver）获取 Cloudflare 的 cf_clearance Cookie 和 User-Agent，
    供程序中 httpx 等 HTTP 客户端使用。

    :param url: 触发 Cloudflare 质询的网址 (例如: `https://www.wenku8.net/`)。
    :param timeout: 浏览器过关尝试的最大超时时间（秒）。
    :param headless: 是否使用无头模式运行浏览器。
    :param proxy: 请求使用的代理（可选）。
    :param user_agent: 指定浏览器 UA。如果为空则随机选用最新的 Chrome UA。
    :param disable_http2: 禁用 HTTP/2。
    :param disable_http3: 禁用 HTTP/3 (QUIC)。

    :return: 一个包含 (User-Agent 字符串, Cookies 字典) 的元组
    """
    logger = logging.getLogger(__name__)

    challenge_messages = {
        ChallengePlatform.JAVASCRIPT: "正在解决 Cloudflare 挑战 [JavaScript]...",
        ChallengePlatform.MANAGED: "正在解决 Cloudflare 挑战 [Managed]...",
        ChallengePlatform.INTERACTIVE: "正在解决 Cloudflare 挑战 [Interactive]...",
    }

    used_user_agent = get_chrome_user_agent() if user_agent is None else user_agent

    async with CloudflareSolver(
        user_agent=used_user_agent,
        timeout=timeout,
        http2=not disable_http2,
        http3=not disable_http3,
        headless=headless,
        proxy=proxy,
    ) as solver:
        logger.debug(f"正在访问 {url}...")

        try:
            await solver.request_page(url)
        except asyncio.TimeoutError as err:
            logger.error("访问页面超时: %s", err)
            raise

        all_cookies = await solver.get_cookies()
        clearance_cookie = solver.extract_clearance_cookie(all_cookies)

        if clearance_cookie is None:
            await solver.set_user_agent_metadata(await solver.get_user_agent())
            challenge_platform = await solver.detect_challenge()

            if challenge_platform is None:
                logger.debug("未检测到 Cloudflare 挑战。可能已经被拉黑或无需质询。")
            else:
                logger.debug(challenge_messages[challenge_platform])

                try:
                    await solver.solve_challenge()
                except asyncio.TimeoutError:
                    logger.warning("解决 Cloudflare 挑战超时。")

            all_cookies = await solver.get_cookies()
            clearance_cookie = solver.extract_clearance_cookie(all_cookies)

        final_user_agent = await solver.get_user_agent()

    if clearance_cookie is None:
        raise RuntimeError("无法获取 Cloudflare `cf_clearance` Cookie。这可能是因为无头浏览器被识别或网络受限。")

    cookies_dict = {
        cookie["name"]: cookie["value"] for cookie in all_cookies
    }
    return final_user_agent, cookies_dict
