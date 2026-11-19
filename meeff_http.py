import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

import aiohttp

logger = logging.getLogger(__name__)

MEEFF_USER_AGENT = "okhttp/5.3.2"
MEEFF_APP_VERSION = "7.1.7"
MEEFF_DEFAULT_LOCALE = "en-US"


def build_meeff_headers(token: Optional[str] = None, json_body: bool = False) -> Dict[str, str]:
    """Build standard Meeff v7.1.7 OkHttp headers."""
    headers: Dict[str, str] = {
        "User-Agent": MEEFF_USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "application/json",
    }
    if json_body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Meeff-Access-Token"] = token.strip()
    return headers


def normalize_codes_csv(value: Any, *, lowercase: bool = False, max_items: int = 0) -> str:
    """Convert a string or iterable of codes into Meeff's deduplicated comma-separated string."""
    if not value:
        return ""
    if isinstance(value, str):
        raw_parts = value.replace(" ", ",").split(",")
    elif isinstance(value, Iterable):
        raw_parts = [str(x) for x in value]
    else:
        return ""

    seen = set()
    cleaned = []
    for item in raw_parts:
        code = item.strip().lower() if lowercase else item.strip().upper()
        if code and code not in seen:
            seen.add(code)
            cleaned.append(code)
            if max_items > 0 and len(cleaned) >= max_items:
                break
    return ",".join(cleaned)


def build_filter_payload(
    user_filters: Optional[Dict[str, Any]] = None,
    global_exclude_codes: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Build a /user/updateFilter/v1 JSON payload matching Meeff v7.1.7 RestClient.userUpdateFilter."""
    uf = user_filters or {}
    current_year = datetime.now().year
    default_year_to = current_year - 18

    year_from = int(uf.get("filterBirthYearFrom", 1979))
    year_to = int(uf.get("filterBirthYearTo", default_year_to))
    if year_from > year_to:
        year_from, year_to = year_to, year_from

    nat_codes = normalize_codes_csv(
        uf.get("filterNationalityCodes") if uf.get("filterNationalityCodes") is not None else uf.get("filterNationalityCode", "")
    )
    account_denied = normalize_codes_csv(uf.get("filterDeniedNationality", ""))
    global_denied = normalize_codes_csv(list(global_exclude_codes) if global_exclude_codes else "")
    combined_denied = normalize_codes_csv(
        ",".join(part for part in (account_denied, global_denied) if part)
    )
    purposes = normalize_codes_csv(uf.get("filterPurposes", ""))
    lang_codes = normalize_codes_csv(uf.get("filterLanguageCodes", ""), lowercase=True, max_items=3)

    payload: Dict[str, Any] = {
        "filterGenderType": int(uf.get("filterGenderType", 5)),
        "filterBirthYearFrom": year_from,
        "filterBirthYearTo": year_to,
        "filterDistance": int(uf.get("filterDistance", 510)),
        "filterLanguageCodes": lang_codes,
        "filterNationalityBlock": 1 if uf.get("filterNationalityBlock", 0) else 0,
        "filterMinProfileImageCount": max(1, int(uf.get("filterMinProfileImageCount", 1))),
        "filterFaceVerifiedOnly": bool(uf.get("filterFaceVerifiedOnly", False)),
        "filterRecentlyActiveOnly": bool(uf.get("filterRecentlyActiveOnly", False)),
        "locale": MEEFF_DEFAULT_LOCALE,
    }
    if nat_codes:
        payload["filterNationalityCodes"] = nat_codes
    if combined_denied:
        payload["filterDeniedNationality"] = combined_denied
    if purposes:
        payload["filterPurposes"] = purposes
    return payload


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
        logger.debug("Meeff proxy enabled")
    else:
        logger.debug("PROXY is not set; Meeff requests will connect directly")
    return MeeffClientSession(proxy=proxy, **kwargs)
