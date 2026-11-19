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


def normalize_single_country(value: Any) -> str:
    """Extract at most 1 uppercase ISO country code (free-tier Meeff allows only 1 country or empty)."""
    csv_val = normalize_codes_csv(value, max_items=1)
    return csv_val.split(",")[0].strip().upper() if csv_val else ""


def build_filter_payload(user_filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Build a 100% Free-Tier safe /user/updateFilter/v1 JSON payload matching Meeff v7.1.7.
    Avoids all Plus-gated fields that trigger 403 PremiumFilterPermissionDenied:
    - filterMinProfileImageCount MUST be 2 (APK free default const/16 v19, #int 2)
    - filterDistance MUST be 510
    - filterLanguageCodes MUST be ""
    - filterNationalityBlock MUST be 0
    - filterFaceVerifiedOnly MUST be False
    - filterRecentlyActiveOnly MUST be False
    - filterNationalityCodes MUST be at most 1 country code (or omitted for All Countries)
    - filterDeniedNationality & filterPurposes MUST NOT be sent
    """
    uf = user_filters or {}
    current_year = datetime.now().year
    default_year_to = current_year - 18

    try:
        year_from = int(uf.get("filterBirthYearFrom", 1979))
    except (TypeError, ValueError):
        year_from = 1979
    try:
        year_to = int(uf.get("filterBirthYearTo", default_year_to))
    except (TypeError, ValueError):
        year_to = default_year_to
    if year_from > year_to:
        year_from, year_to = year_to, year_from

    try:
        gender_type = int(uf.get("filterGenderType", 5))
    except (TypeError, ValueError):
        gender_type = 5
    if gender_type not in (5, 6, 7):
        gender_type = 5

    raw_nat = (
        uf.get("filterNationalityCode")
        if uf.get("filterNationalityCode") is not None
        else uf.get("filterNationalityCodes", "")
    )
    single_nat = normalize_single_country(raw_nat)

    payload: Dict[str, Any] = {
        "filterGenderType": gender_type,
        "filterBirthYearFrom": year_from,
        "filterBirthYearTo": year_to,
        "filterDistance": 510,
        "filterLanguageCodes": "",
        "filterNationalityBlock": 0,
        "filterMinProfileImageCount": 2,
        "filterFaceVerifiedOnly": False,
        "filterRecentlyActiveOnly": False,
        "locale": MEEFF_DEFAULT_LOCALE,
    }
    if single_nat:
        payload["filterNationalityCodes"] = single_nat
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
