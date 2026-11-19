import random
from typing import Dict, Optional
from db import _get_user_collection, _ensure_user_collection_exists


def _sanitize_email_for_key(email: str) -> str:
    """Replaces characters that are invalid in MongoDB field names."""
    return email.replace('.', '_')


def generate_device_unique_id() -> str:
    """Generate a unique device ID (16 hex characters)."""
    return ''.join(random.choices('0123456789abcdef', k=16))


def generate_push_token() -> str:
    """Generate push token placeholder (empty until FCM registration)."""
    return ""


# Realistic pool of Android device profiles so each newly registered/initialized
# account gets an independent device fingerprint (existing DB profiles are preserved).
ANDROID_APP_VERSIONS = ["7.1.7"]
ANDROID_DEVICE_PROFILES = [
    {
        "brand": "INFINIX",
        "model": "Infinix X6858",
        "device": "Infinix-X6858",
        "product": "X6858-OP",
        "display": "X6858-15.1.0.138SP01(OP001PF001AZ)",
        "device_name": "Infinix note 50",
        "os": "Android v15",
    },
    {
        "brand": "samsung",
        "model": "SM-A546E",
        "device": "a54x",
        "product": "a54xnsxx",
        "display": "UP1A.231005.007.A546EXXS9CXH1",
        "device_name": "Galaxy A54 5G",
        "os": "Android v14",
    },
    {
        "brand": "samsung",
        "model": "SM-S911B",
        "device": "dm1q",
        "product": "dm1qxxx",
        "display": "UP1A.231005.007.S911BXXU6CXH7",
        "device_name": "Galaxy S23",
        "os": "Android v14",
    },
    {
        "brand": "Redmi",
        "model": "23124RA7EO",
        "device": "sapphire",
        "product": "sapphire_global",
        "display": "UKQ1.231003.002",
        "device_name": "Redmi Note 13",
        "os": "Android v14",
    },
    {
        "brand": "google",
        "model": "Pixel 8",
        "device": "shiba",
        "product": "shiba",
        "display": "AP2A.240905.003",
        "device_name": "Pixel 8",
        "os": "Android v14",
    },
    {
        "brand": "TECNO",
        "model": "TECNO KL7",
        "device": "TECNO-KL7",
        "product": "KL7-OP",
        "display": "KL7-H8912ABC-U-OP-240815V312",
        "device_name": "TECNO Spark 30",
        "os": "Android v14",
    },
    {
        "brand": "realme",
        "model": "RMX3840",
        "device": "RE5C4DL1",
        "product": "RMX3840",
        "display": "UKQ1.230924.001",
        "device_name": "realme 12 Pro",
        "os": "Android v14",
    },
    {
        "brand": "OnePlus",
        "model": "CPH2581",
        "device": "OP5958L1",
        "product": "CPH2581",
        "display": "CPH2581_14.0.0.810(EX01)",
        "device_name": "OnePlus 12",
        "os": "Android v14",
    },
]


def generate_device_info() -> Dict[str, str]:
    """Generate a complete, unique Android device fingerprint."""
    profile = random.choice(ANDROID_DEVICE_PROFILES)
    model = profile["model"]
    os_version = profile["os"]
    app_version = random.choice(ANDROID_APP_VERSIONS)

    device_string = (
        f"BRAND: {profile['brand']}, MODEL: {model}, DEVICE: {profile['device']}, "
        f"PRODUCT: {profile['product']}, DISPLAY: {profile['display']}"
    )

    return {
        "device_model": model,
        "device_name": profile["device_name"],
        "os_version": os_version,
        "app_version": app_version,
        "device_unique_id": generate_device_unique_id(),
        "push_token": generate_push_token(),
        "device_info_header": f"{model}-{os_version}-{app_version}",
        "device_string": device_string,
        "os": os_version,
        "platform": "android",
        "device_language": "en",
        "device_region": "US",
        "sim_region": "PK",
        "device_gmt_offset": "+0500",
        "device_rooted": 0,
        "device_emulator": 0,
    }


def get_headers_with_device_info(base_headers: Dict[str, str], device_info: Dict[str, str]) -> Dict[str, str]:
    """Return headers compatible with Meeff v7.1.7 OkHttp transport."""
    return base_headers.copy()


def get_api_payload_with_device_info(base_payload: Dict, device_info: Dict[str, str]) -> Dict:
    """Injects device info into an API request payload."""
    payload = base_payload.copy()
    payload.update({
        "os": device_info["os"],
        "platform": device_info["platform"],
        "device": device_info["device_string"],
        "appVersion": device_info["app_version"],
        "deviceUniqueId": device_info["device_unique_id"],
        "pushToken": device_info["push_token"],
        "deviceLanguage": device_info["device_language"],
        "deviceRegion": device_info["device_region"],
        "simRegion": device_info["sim_region"],
        "deviceGmtOffset": device_info["device_gmt_offset"],
        "deviceRooted": device_info["device_rooted"],
        "deviceEmulator": device_info["device_emulator"],
    })
    return payload


# --- Async DB Functions for Device Info ---

async def store_device_info_for_email(telegram_user_id: int, email: str, device_info: Dict[str, str]):
    """Store device info for a specific email asynchronously."""
    await _ensure_user_collection_exists(telegram_user_id)
    sanitized_email = _sanitize_email_for_key(email)
    user_db = _get_user_collection(telegram_user_id)
    await user_db.update_one(
        {"type": "device_info"},
        {"$set": {f"data.{sanitized_email}": device_info}},
        upsert=True,
    )


async def get_device_info_for_email(telegram_user_id: int, email: str) -> Optional[Dict[str, str]]:
    """Get device info for a specific email asynchronously."""
    await _ensure_user_collection_exists(telegram_user_id)
    sanitized_email = _sanitize_email_for_key(email)
    user_db = _get_user_collection(telegram_user_id)
    device_doc = await user_db.find_one({"type": "device_info"})
    if device_doc and "data" in device_doc and sanitized_email in device_doc["data"]:
        return device_doc["data"][sanitized_email]
    return None


async def get_or_create_device_info_for_email(telegram_user_id: int, email: str) -> Dict[str, str]:
    """Get existing device info for email or create a new one asynchronously."""
    await _ensure_user_collection_exists(telegram_user_id)
    sanitized_email = _sanitize_email_for_key(email)
    user_db = _get_user_collection(telegram_user_id)

    device_doc = await user_db.find_one({"type": "device_info"})
    if device_doc and "data" in device_doc and sanitized_email in device_doc["data"]:
        return device_doc["data"][sanitized_email]

    device_info = generate_device_info()
    await user_db.update_one(
        {"type": "device_info"},
        {
            "$set": {f"data.{sanitized_email}": device_info},
            "$setOnInsert": {"type": "device_info"},
        },
        upsert=True,
    )
    return device_info


async def store_device_info_for_token(telegram_user_id: int, token: str, device_info: Dict[str, str]):
    """Store device info for a specific token asynchronously."""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    await user_db.update_one(
        {"type": "token_device_info"},
        {"$set": {f"data.{token}": device_info}},
        upsert=True,
    )


async def get_device_info_for_token(telegram_user_id: int, token: str) -> Optional[Dict[str, str]]:
    """Get device info for a specific token asynchronously."""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    device_doc = await user_db.find_one({"type": "token_device_info"})
    if device_doc and "data" in device_doc and token in device_doc["data"]:
        return device_doc["data"][token]
    return None


async def get_or_create_device_info_for_token(telegram_user_id: int, token: str) -> Dict[str, str]:
    """Get existing device info for a token or create a new one asynchronously."""
    device_info = await get_device_info_for_token(telegram_user_id, token)
    if not device_info:
        device_info = generate_device_info()
        await store_device_info_for_token(telegram_user_id, token, device_info)
    return device_info


async def get_or_create_device_info_for_account(
    telegram_user_id: int,
    token: str,
    email: Optional[str] = None,
) -> Dict[str, str]:
    """
    Resolve a stable device fingerprint for an account:
    1) check token_device_info
    2) fall back to device_info[email] and link it to token
    3) create and store under both token and email
    """
    device_info = await get_device_info_for_token(telegram_user_id, token)
    if device_info:
        if email and not await get_device_info_for_email(telegram_user_id, email):
            await store_device_info_for_email(telegram_user_id, email, device_info)
        return device_info

    if email:
        email_info = await get_device_info_for_email(telegram_user_id, email)
        if email_info:
            await store_device_info_for_token(telegram_user_id, token, email_info)
            return email_info

    device_info = generate_device_info()
    await store_device_info_for_token(telegram_user_id, token, device_info)
    if email:
        await store_device_info_for_email(telegram_user_id, email, device_info)
    return device_info
