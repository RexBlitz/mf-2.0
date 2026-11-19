import random
import string
from typing import Dict, Optional
from db import _get_user_collection, _ensure_user_collection_exists

def _sanitize_email_for_key(email: str) -> str:
    """Replaces characters that are invalid in MongoDB field names."""
    return email.replace('.', '_')

def generate_device_unique_id() -> str:
    """Generate a unique device ID (16 hex characters)."""
    return ''.join(random.choices('0123456789abcdef', k=16))

def generate_push_token() -> str:
    """Generate a realistic push token. Set to empty string for Android based on user request."""
    return ""

# --- Android Configuration based on user's raw request ---
ANDROID_MODELS = ["Infinix X6858"]
ANDROID_OS_VERSIONS = ["Android v15"]
ANDROID_APP_VERSIONS = ["6.7.1", "6.7.0"]
# The specific, complex device string from the user's request
DEVICE_STRING_TEMPLATE = (
    "BRAND: INFINIX, MODEL: {model}, DEVICE: Infinix-X6858, "
    "PRODUCT: X6858-OP, DISPLAY: X6858-15.1.0.138SP01(OP001PF001AZ)"
)
# --- End Android Configuration ---

def generate_device_info() -> Dict[str, str]:
    """Generate complete device information for Android."""
    
    model = random.choice(ANDROID_MODELS)
    os_version = random.choice(ANDROID_OS_VERSIONS)
    app_version = random.choice(ANDROID_APP_VERSIONS)

    device_string = DEVICE_STRING_TEMPLATE.format(model=model)
    push_token = generate_push_token()

    return {
        "device_model": model, 
        "device_name": "Infinix note 50",
        "os_version": os_version,
        "app_version": app_version, 
        "device_unique_id": generate_device_unique_id(),
        "push_token": push_token, 
        "device_info_header": f"{model}-{os_version}-{app_version}",
        "device_string": device_string,
        "os": os_version, 
        "platform": "android",
        "device_language": "en", 
        "device_region": "US", 
        "sim_region": "PK",
        "device_gmt_offset": "+0500",
        "device_rooted": 0, 
        "device_emulator": 0
    }

def get_headers_with_device_info(base_headers: Dict[str, str], device_info: Dict[str, str]) -> Dict[str, str]:
    """Injects device info into API request headers."""
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
        "deviceEmulator": device_info["device_emulator"]
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
        upsert=True
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
    
    # Try to get existing device info (READ)
    device_doc = await user_db.find_one({"type": "device_info"})
    if device_doc and "data" in device_doc and sanitized_email in device_doc["data"]:
        return device_doc["data"][sanitized_email]
    
    # Create new device info atomically using upsert (WRITE)
    device_info = generate_device_info()
    await user_db.update_one(
        {"type": "device_info"},
        {
            "$set": {f"data.{sanitized_email}": device_info},
            "$setOnInsert": {"type": "device_info"}  # <-- FIX: Removed "data": {}
        },
        upsert=True
    )
    return device_info
async def store_device_info_for_token(telegram_user_id: int, token: str, device_info: Dict[str, str]):
    """Store device info for a specific token asynchronously."""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    await user_db.update_one(
        {"type": "token_device_info"},
        {"$set": {f"data.{token}": device_info}},
        upsert=True
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
