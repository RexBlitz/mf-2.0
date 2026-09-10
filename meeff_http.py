import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class MeeffClientSession(aiohttp.ClientSession):
    """Client session that applies the configured proxy to every request."""

    def __init__(self, proxy: str | None = None, **kwargs: Any) -> None:
        self._meeff_proxy = proxy
        super().__init__(**kwargs)

    async def _request(self, method: str, url: Any, **kwargs: Any) -> aiohttp.ClientResponse:
        if self._meeff_proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self._meeff_proxy
        return await super()._request(method, url, **kwargs)


def create_meeff_session(**kwargs: Any) -> aiohttp.ClientSession:
    """Create an aiohttp session for Meeff traffic using the configured proxy."""
    proxy = os.getenv("PROXY")
    if proxy:
        logger.info("Meeff proxy enabled")
    else:
        logger.warning("PROXY is not set; Meeff requests will connect directly")
    return MeeffClientSession(proxy=proxy, **kwargs)
