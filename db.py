from pymongo import MongoClient
import datetime
from motor.motor_asyncio import AsyncIOMotorClient
import os
from typing import Dict, Optional, Tuple, Any


# MongoDB connection using the asynchronous Motor client
client = AsyncIOMotorClient("mongodb+srv://irexanon:12312320Pk..@rexdb.d9rwo.mongodb.net/?retryWrites=true&w=majority&appName=RexDB")
db = client.meeff_bot

async def get_user_collection(user_id: int):
    """
    Retrieves the correct MongoDB collection for a given user.
    This is the async version required by other functions.
    """
    collection_name = f"user_{user_id}"
    return db[collection_name]

# Helper function to get a user's collection (synchronous version for internal use if needed)
def _get_user_collection(telegram_user_id):
    """Get the collection for a user"""
    collection_name = f"user_{telegram_user_id}"
    return db[collection_name]

# Helper function to ensure collection exists with basic structure
_initialized_users: set = set()

async def _ensure_user_collection_exists(telegram_user_id):
    """Make sure user collection exists with default documents — skips DB check if already seen this session"""
    if telegram_user_id in _initialized_users:
        return
    user_db = _get_user_collection(telegram_user_id)
    if await user_db.count_documents({"type": "metadata"}) == 0:
        await user_db.insert_many([
            {"type": "metadata", "created_at": datetime.datetime.utcnow(), "user_id": telegram_user_id},
            {"type": "tokens", "items": []},
            {"type": "settings", "current_token": None, "spam_filter": False},
            {"type": "sent_records", "data": {}},
            {"type": "filters", "data": {}},
            {"type": "info_cards", "data": {}},
            {"type": "batches", "items": []}
        ])
    _initialized_users.add(telegram_user_id)

async def get_all_user_filters(user_id: int):
    """
    Efficiently fetches all filter documents for a user and returns a dictionary
    mapping token to its filter data.
    """
    collection = await get_user_collection(user_id)
    tokens_doc = await collection.find_one({"type": "tokens"})
    if not tokens_doc or "items" not in tokens_doc:
        return {}
    
    return {
        token_item.get("token"): token_item.get("filters", {})
        for token_item in tokens_doc.get("items", [])
        if "token" in token_item
    }

# Enhanced DB Collection Management Functions
async def list_all_collections():
    collection_names = await db.list_collection_names()
    user_collections = []
    for name in filter(lambda n: n.startswith("user_") and n != "user_", collection_names):
        try:
            summary = await get_collection_summary(name)
            user_collections.append({"collection_name": name, "user_id": name[5:], "summary": summary})
        except Exception as e:
            print(f"Error processing collection {name}: {e}")
    return sorted(user_collections, key=lambda x: x.get("summary", {}).get("created_at") or datetime.datetime.min, reverse=True)

async def get_collection_summary(collection_name):
    collection = db[collection_name]
    query_types = ["tokens", "sent_records", "info_cards", "settings", "metadata"]
    all_docs = await collection.find({"type": {"$in": query_types}}).to_list(length=None)
    docs_by_type = {doc.get("type"): doc for doc in all_docs}
    tokens_doc = docs_by_type.get("tokens", {})
    sent_doc = docs_by_type.get("sent_records", {})
    info_doc = docs_by_type.get("info_cards", {})
    settings_doc = docs_by_type.get("settings", {})
    metadata_doc = docs_by_type.get("metadata", {})
    tokens_count = len(tokens_doc.get("items", []))
    active_tokens = sum(1 for token in tokens_doc.get("items", []) if token.get("active", True))
    sent_total = sum(len(ids) for ids in sent_doc.get("data", {}).values() if isinstance(ids, list))
    current_token = settings_doc.get("current_token")
    return {
        "tokens_count": tokens_count,
        "active_tokens": active_tokens,
        "sent_records": {"total": sent_total},
        "info_cards_count": len(info_doc.get("data", {})),
        "has_current_token": bool(current_token),
        "spam_filter_enabled": settings_doc.get("spam_filter", False),
        "created_at": metadata_doc.get("created_at"),
        "total_documents": await collection.count_documents({})
    }

async def connect_to_collection(collection_name, target_user_id):
    if collection_name not in await db.list_collection_names():
        return False, f"Collection '{collection_name}' not found"
    await _ensure_user_collection_exists(target_user_id)
    from_collection, to_collection = db[collection_name], _get_user_collection(target_user_id)
    all_docs = await from_collection.find({}).to_list(length=None)
    if not all_docs: return False, "Source collection is empty"
    await to_collection.delete_many({})
    for doc in all_docs:
        if doc.get("type") == "metadata":
            doc.update({"user_id": target_user_id, "connected_at": datetime.datetime.utcnow(), "original_collection": collection_name})
    await to_collection.insert_many(all_docs)
    return True, f"Successfully connected to '{collection_name}' with {len(all_docs)} documents"

async def rename_user_collection(user_id, new_collection_name):
    old_name = f"user_{user_id}"
    if old_name not in await db.list_collection_names(): return False, "Your collection not found"
    new_name = f"user_{new_collection_name}" if not new_collection_name.startswith("user_") else new_collection_name
    if new_name in await db.list_collection_names(): return False, "Target collection name already exists"
    old_collection = db[old_name]
    all_docs = await old_collection.find({}).to_list(length=None)
    if not all_docs: return False, "Your collection is empty"
    for doc in all_docs:
        if doc.get("type") == "metadata":
            doc.update({"renamed_at": datetime.datetime.utcnow(), "original_name": old_name})
    await db[new_name].insert_many(all_docs)
    await old_collection.drop()
    return True, f"Successfully renamed to '{new_name}'"

async def transfer_to_user(from_user_id, to_user_id):
    from_name = f"user_{from_user_id}"
    if from_name not in await db.list_collection_names(): return False, "Your collection not found"
    return await connect_to_collection(from_name, to_user_id)

async def get_current_collection_info(user_id):
    collection_name = f"user_{user_id}"
    collection = db[collection_name]
    tokens_doc = await collection.find_one({"type": "tokens"}, {"items": 1})
    if tokens_doc:
        tokens_count = len(tokens_doc.get("items", []))
        return {"collection_name": collection_name, "exists": True, "summary": {"tokens_count": tokens_count}}
    return {"collection_name": collection_name, "exists": False, "summary": None}

async def set_info_card(telegram_user_id, token, info_text, email=None):
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    await user_db.update_one(
        {"type": "info_cards"},
        {"$set": {f"data.{token}": {"info": info_text, "email": email, "updated_at": datetime.datetime.utcnow()}}},
        upsert=True
    )

async def get_info_card(telegram_user_id, token):
    await _ensure_user_collection_exists(telegram_user_id)
    cards_doc = await _get_user_collection(telegram_user_id).find_one({"type": "info_cards"})
    if cards_doc and token in cards_doc.get("data", {}):
        return cards_doc["data"][token].get("info")
    return None

async def migrate_info_card(telegram_user_id, old_token: str, new_token: str):
    """
    Move the info card keyed by old_token to new_token.
    Called whenever a token string is replaced (re-signin / email-duplicate path)
    so the profile card survives the token rotation.
    """
    if old_token == new_token:
        return
    user_db = _get_user_collection(telegram_user_id)
    cards_doc = await user_db.find_one({"type": "info_cards"})
    if not cards_doc:
        return
    old_card = cards_doc.get("data", {}).get(old_token)
    if old_card is None:
        return
    # Write the card under the new token key and remove the old key atomically
    await user_db.update_one(
        {"type": "info_cards"},
        {
            "$set":   {f"data.{new_token}": old_card},
            "$unset": {f"data.{old_token}": ""},
        }
    )

async def set_token(telegram_user_id, token, name, email=None, password=None, filters=None, active=True) -> int:
    """
    Saves or updates a Meeff token. 
    Added 'password' to arguments to fix NameError.
    """
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)

    tokens_doc = await user_db.find_one({"type": "tokens"})
    tokens_list = tokens_doc.get("items", []) if tokens_doc else []

    token_index = -1
    email_index = -1

    # 1. Check if email already exists and find existing token index
    for i, t in enumerate(tokens_list):
        if email and t.get("email") == email:
            email_index = i
        if t["token"] == token:
            token_index = i

    # 2. Prevent email-based duplicates
    if email and email_index != -1 and token_index != email_index:
        # Capture old token string BEFORE the pull so we can migrate its info card
        old_token_for_email = tokens_list[email_index]["token"]
        await user_db.update_one(
            {"type": "tokens"},
            {"$pull": {"items": {"email": email}}}
        )
        # Migrate info card: old token key -> new token key so profile card survives rotation
        await migrate_info_card(telegram_user_id, old_token_for_email, token)
        # Refresh the list and recalculate index
        tokens_doc = await user_db.find_one({"type": "tokens"})
        tokens_list = tokens_doc.get("items", []) if tokens_doc else []
        token_index = next((i for i, t in enumerate(tokens_list) if t["token"] == token), -1)

    if token_index != -1:
        # 3. Update existing token
        update_fields = {
            "items.$.name": name,
            "items.$.active": active
        }
        if email: update_fields["items.$.email"] = email
        if password: update_fields["items.$.password"] = password # Now correctly defined
        if filters: update_fields["items.$.filters"] = filters

        await user_db.update_one(
            {"type": "tokens", "items.token": token},
            {"$set": update_fields}
        )
    else:
        # 4. Insert new token
        token_index = len(tokens_list) 
        token_data = {
            "token": token,
            "name": name,
            "active": active
        }
        if email: token_data["email"] = email
        if password: token_data["password"] = password # Now correctly defined
        if filters: token_data["filters"] = filters

        await user_db.update_one(
            {"type": "tokens"},
            {"$push": {"items": token_data}},
            upsert=True
        )

    return token_index
    
async def resign_token_at_position(
    user_id: int, position: int, new_token: str, 
    name: str, email: str = None, password: str = None, filters: dict = None
):
    """
    Replace token at specific position without changing order.
    Used for re-signing expired accounts in signup menu.
    
    Raises:
        ValueError: If position is out of range
    """
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    
    tokens_doc = await user_db.find_one({"type": "tokens"})
    tokens_list = tokens_doc.get("items", []) if tokens_doc else []
    
    if position < 0 or position >= len(tokens_list):
        raise ValueError(f"Invalid position {position}, tokens list length is {len(tokens_list)}")
    
    # Capture the old token string BEFORE overwriting so we can migrate its info card
    old_token = tokens_list[position].get("token")

    # Build new token entry
    token_data = {
        "token": new_token,
        "name": name,
        "active": True
    }
    if email:
        token_data["email"] = email
    if password:
        token_data["password"] = password
    if filters:
        token_data["filters"] = filters
    
    # Replace at exact position
    tokens_list[position] = token_data
    
    await user_db.update_one(
        {"type": "tokens"},
        {"$set": {"items": tokens_list}},
        upsert=True
    )

    # Migrate info card from old token key to new token key so profile card survives re-signin
    if old_token and old_token != new_token:
        await migrate_info_card(user_id, old_token, new_token)
async def toggle_token_status(telegram_user_id, token):
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    token_obj = await user_db.find_one({"type": "tokens", "items.token": token}, {"items.$": 1})
    if token_obj and token_obj.get("items"):
        current_status = token_obj["items"][0].get("active", True)
        await user_db.update_one({"type": "tokens", "items.token": token}, {"$set": {"items.$.active": not current_status}})

async def set_account_active(telegram_user_id, token, active_status):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "tokens", "items.token": token}, {"$set": {"items.$.active": active_status}})

async def get_active_tokens(telegram_user_id):
    await _ensure_user_collection_exists(telegram_user_id)
    tokens_doc = await _get_user_collection(telegram_user_id).find_one({"type": "tokens"})
    return [t for t in tokens_doc.get("items", []) if t.get("active", True)] if tokens_doc else []

async def get_token_status(telegram_user_id, token):
    await _ensure_user_collection_exists(telegram_user_id)
    token_obj = await _get_user_collection(telegram_user_id).find_one({"type": "tokens", "items.token": token}, {"items.$": 1})
    if token_obj and token_obj.get("items"):
        return token_obj["items"][0].get("active", True)
    return None

async def get_tokens(telegram_user_id):
    await _ensure_user_collection_exists(telegram_user_id)
    tokens_doc = await _get_user_collection(telegram_user_id).find_one({"type": "tokens"})
    return tokens_doc.get("items", []) if tokens_doc else []

get_all_tokens = get_tokens

async def list_tokens():
    result = []
    collection_names = await db.list_collection_names()
    for name in filter(lambda n: n.startswith("user_"), collection_names):
        tokens_doc = await db[name].find_one({"type": "tokens"})
        if tokens_doc:
            for token in tokens_doc.get("items", []):
                result.append({"user_id": name[5:], "token": token.get("token"), "name": token.get("name")})
    return result

async def set_current_account(telegram_user_id, token):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "settings"}, {"$set": {"current_token": token}}, upsert=True)

async def get_current_account(telegram_user_id):
    await _ensure_user_collection_exists(telegram_user_id)
    settings = await _get_user_collection(telegram_user_id).find_one({"type": "settings"})
    return settings.get("current_token") if settings else None

async def delete_token(telegram_user_id, token):
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    await user_db.update_one({"type": "tokens"}, {"$pull": {"items": {"token": token}}})
    if (await get_current_account(telegram_user_id)) == token:
        await set_current_account(telegram_user_id, None)
    await user_db.update_one({"type": "info_cards"}, {"$unset": {f"data.{token}": ""}})
    
    # NEW: Re-organize batches after deletion to fix index corruption
    await auto_reorganize_batches_after_deletion(telegram_user_id)

async def cleanup_duplicate_emails(telegram_user_id):
    """Remove duplicate email entries, keeping only the latest token for each email"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)

    tokens_doc = await user_db.find_one({"type": "tokens"})
    if not tokens_doc:
        return {"status": "No tokens found", "removed": 0}

    tokens_list = tokens_doc.get("items", [])
    if not tokens_list:
        return {"status": "No tokens to clean", "removed": 0}

    email_map = {}
    to_remove = []

    # Map each email to its tokens, keep track of all but the last one
    for i, token_obj in enumerate(tokens_list):
        email = token_obj.get("email")
        if email:
            if email not in email_map:
                email_map[email] = []
            email_map[email].append(i)

    # Mark older tokens for deletion (keep only the latest)
    for email, indices in email_map.items():
        if len(indices) > 1:
            # Keep the last one (highest index), mark others for removal
            for idx in indices[:-1]:
                to_remove.append(tokens_list[idx]["token"])

    # Remove duplicates
    removed_count = 0
    for token_to_remove in to_remove:
        await user_db.update_one(
            {"type": "tokens"},
            {"$pull": {"items": {"token": token_to_remove}}}
        )
        removed_count += 1

    return {"status": "Cleanup complete", "removed": removed_count}

async def set_user_filters(telegram_user_id, token, filters):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "tokens", "items.token": token}, {"$set": {"items.$.filters": filters}})

async def get_user_filters(telegram_user_id, token):
    await _ensure_user_collection_exists(telegram_user_id)
    token_obj = await _get_user_collection(telegram_user_id).find_one({"type": "tokens", "items.token": token}, {"items.$": 1})
    if token_obj and token_obj.get("items"):
        return token_obj["items"][0].get("filters")
    return None

async def set_spam_filter(telegram_user_id, status: bool):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "settings"}, {"$set": {"spam_filter": status}}, upsert=True)

async def set_individual_spam_filter(telegram_user_id, filter_type: str, status: bool):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "settings"}, {"$set": {f"spam_filter_{filter_type}": status}}, upsert=True)

async def get_individual_spam_filter(telegram_user_id: int, filter_type: str) -> bool:
    await _ensure_user_collection_exists(telegram_user_id)
    settings = await _get_user_collection(telegram_user_id).find_one({"type": "settings"})
    return settings.get(f"spam_filter_{filter_type}", False) if settings else False

async def get_all_spam_filters(telegram_user_id: int) -> dict:
    await _ensure_user_collection_exists(telegram_user_id)
    settings = await _get_user_collection(telegram_user_id).find_one({"type": "settings"})
    if not settings: return {"chatroom": False, "request": False, "lounge": False}
    return {
        "chatroom": settings.get("spam_filter_chatroom", False),
        "request": settings.get("spam_filter_request", False),
        "lounge": settings.get("spam_filter_lounge", False),
    }

async def get_spam_menu_data(telegram_user_id: int) -> dict:
    """
    Efficiently fetches all data needed for the spam filter menu in a single DB query.
    """
    await _ensure_user_collection_exists(telegram_user_id)
    collection = _get_user_collection(telegram_user_id)
    
    # Fetch both the settings and sent_records documents at the same time
    query_results = await collection.find(
        {"type": {"$in": ["settings", "sent_records"]}}
    ).to_list(length=2)
    
    settings_doc = {}
    records_doc = {}
    for doc in query_results:
        if doc.get("type") == "settings":
            settings_doc = doc
        elif doc.get("type") == "sent_records":
            records_doc = doc.get("data", {})

    # Process the results into a clean dictionary
    data = {
        "filters": {
            "chatroom": settings_doc.get("spam_filter_chatroom", False),
            "request": settings_doc.get("spam_filter_request", False),
            "lounge": settings_doc.get("spam_filter_lounge", False),
        },
        "counts": {
            "chatroom": len(records_doc.get("chatroom", [])),
            "request": len(records_doc.get("request", [])),
            "lounge": len(records_doc.get("lounge", [])),
        }
    }
    return data

async def get_exclude_filter(telegram_user_id: int) -> list:
    """Returns list of excluded nationality codes e.g. ['US', 'KR']"""
    await _ensure_user_collection_exists(telegram_user_id)
    col = _get_user_collection(telegram_user_id)
    doc = await col.find_one({"type": "settings"})
    return doc.get("exclude_nationalities", []) if doc else []

async def set_exclude_filter(telegram_user_id: int, codes: list):
    """Save list of excluded nationality codes"""
    await _ensure_user_collection_exists(telegram_user_id)
    col = _get_user_collection(telegram_user_id)
    await col.update_one(
        {"type": "settings"},
        {"$set": {"exclude_nationalities": codes}},
        upsert=True
    )

async def get_spam_filter(telegram_user_id: int) -> bool:
    await _ensure_user_collection_exists(telegram_user_id)
    settings = await _get_user_collection(telegram_user_id).find_one({"type": "settings"})
    return settings.get("spam_filter", False) if settings else False

async def get_already_sent_ids(telegram_user_id, category):
    await _ensure_user_collection_exists(telegram_user_id)
    records_doc = await _get_user_collection(telegram_user_id).find_one({"type": "sent_records"})
    return set(records_doc.get("data", {}).get(category, [])) if records_doc else set()

async def add_sent_id(telegram_user_id, category, target_id):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "sent_records"}, {"$addToSet": {f"data.{category}": target_id}}, upsert=True)

async def is_already_sent(telegram_user_id, category, target_id=None, bulk=False):
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    if not bulk:
        return await user_db.count_documents({"type": "sent_records", f"data.{category}": target_id}) > 0
    else:
        records_doc = await user_db.find_one({"type": "sent_records"}, {f"data.{category}": 1})
        return set(records_doc.get("data", {}).get(category, [])) if records_doc else set()

async def get_spam_record_count(telegram_user_id: int, category: str) -> int:
    """Gets the count of stored IDs for a specific spam category."""
    await _ensure_user_collection_exists(telegram_user_id)
    records_doc = await _get_user_collection(telegram_user_id).find_one({"type": "sent_records"})
    if not records_doc or "data" not in records_doc or category not in records_doc["data"]:
        return 0
    return len(records_doc["data"][category])

async def clear_spam_records(telegram_user_id: int, category: str):
    """Clears all stored IDs for a specific spam category."""
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one(
        {"type": "sent_records"},
        {"$set": {f"data.{category}": []}}
    )

async def bulk_add_sent_ids(telegram_user_id, category, target_ids):
    if not target_ids: return
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "sent_records"}, {"$addToSet": {f"data.{category}": {"$each": list(target_ids)}}}, upsert=True)

async def has_valid_access(telegram_user_id):
    collection_name = f"user_{telegram_user_id}"
    if collection_name not in await db.list_collection_names(): return False
    return await db[collection_name].count_documents({"type": "metadata"}) > 0

def get_message_delay(telegram_user_id):
    return 2

# Functions for signup, email variations, etc., all converted
async def add_used_email_variation(telegram_user_id, base_email, variation):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "email_variations"}, {"$addToSet": {f"data.{base_email}": variation}}, upsert=True)

async def get_used_email_variations(telegram_user_id, base_email):
    await _ensure_user_collection_exists(telegram_user_id)
    doc = await _get_user_collection(telegram_user_id).find_one({"type": "email_variations"})
    return doc.get("data", {}).get(base_email, []) if doc else []

async def set_auto_signup_enabled(telegram_user_id, enabled):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "settings"}, {"$set": {"auto_signup_enabled": enabled}}, upsert=True)

async def get_auto_signup_enabled(telegram_user_id):
    await _ensure_user_collection_exists(telegram_user_id)
    settings = await _get_user_collection(telegram_user_id).find_one({"type": "settings"})
    return settings.get("auto_signup_enabled", False) if settings else False

async def set_signup_config(telegram_user_id, config):
    await _ensure_user_collection_exists(telegram_user_id)
    await _get_user_collection(telegram_user_id).update_one({"type": "signup_config"}, {"$set": {"data": config}}, upsert=True)

async def get_signup_config(telegram_user_id):
    await _ensure_user_collection_exists(telegram_user_id)
    doc = await _get_user_collection(telegram_user_id).find_one({"type": "signup_config"})
    return doc.get("data") if doc else None

transfer_user_data = transfer_to_user

# Legacy functions converted
async def has_interacted(telegram_user_id, action_type, user_token):
    return await db.interactions.find_one({"user_id": telegram_user_id, "action_type": action_type, "user_token": user_token}) is not None

async def log_interaction(telegram_user_id, action_type, user_token):
    await db.interactions.insert_one({"user_id": telegram_user_id, "action_type": action_type, "user_token": user_token, "timestamp": datetime.datetime.utcnow()})

# --- Batch Management Functions ---

async def get_batches(telegram_user_id: int) -> list:
    """Get all batches with their accounts"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    batches_doc = await user_db.find_one({"type": "batches"})
    return batches_doc.get("items", []) if batches_doc else []

async def get_last_batch(user_id: int) -> Tuple[Optional[Dict], int]:
    """Retrieves the last created batch and the total number of tokens."""
    user_db = _get_user_collection(user_id)
    batches_doc = await user_db.find_one({"type": "batches"})
    tokens_doc = await user_db.find_one({"type": "tokens"})
    
    tokens = tokens_doc.get("items", []) if tokens_doc else []
    total_tokens = len(tokens)
    
    if batches_doc and batches_doc.get("items"):
        last_batch = batches_doc["items"][-1]
        return last_batch, total_tokens
    
    return None, total_tokens

async def add_token_to_auto_batch(user_id: int, token_index: int):
    """Adds a newly created token (by index) to the correct batch (batches of 10)."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)

    # Check if this index is already in any batch — avoid duplicates
    batches_doc = await user_db.find_one({"type": "batches"})
    if batches_doc:
        for batch in batches_doc.get("items", []):
            if token_index in batch.get("token_indices", []):
                return  # already tracked, nothing to do

    # Calculate which batch this token belongs to (groups of 10)
    new_batch_number = (token_index // 10) + 1
    new_batch_name = f"Batch {new_batch_number}"

    # Ensure batches doc exists with items array
    await user_db.update_one(
        {"type": "batches"},
        {"$setOnInsert": {"type": "batches", "items": []}},
        upsert=True
    )

    if batches_doc and any(b.get("name") == new_batch_name for b in batches_doc.get("items", [])):
        # Batch exists — append index to it
        await user_db.update_one(
            {"type": "batches", "items.name": new_batch_name},
            {"$push": {"items.$.token_indices": token_index}}
        )
    else:
        # Batch doesn't exist yet — create it
        batch_data = {
            "name": new_batch_name,
            "token_indices": [token_index],
            "active": True,
            "filter_nationality": ""
        }
        await user_db.update_one(
            {"type": "batches"},
            {"$push": {"items": batch_data}}
        )

async def create_batch(telegram_user_id: int, batch_name: str, token_indices: list) -> bool:
    """Create a new batch with specified token indices"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)

    batch_data = {
        "name": batch_name,
        "token_indices": token_indices,
        "active": True,
        "filter_nationality": ""
    }

    await user_db.update_one(
        {"type": "batches"},
        {"$push": {"items": batch_data}},
        upsert=True
    )
    return True

async def toggle_batch_status(telegram_user_id: int, batch_name: str):
    """Toggle the active status of all accounts in a batch"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)

    batches_doc = await user_db.find_one({"type": "batches"})
    if not batches_doc:
        return False

    tokens = await get_tokens(telegram_user_id)
    batch_found = False
    new_status = True

    for batch in batches_doc.get("items", []):
        if batch["name"] == batch_name:
            batch_found = True
            new_status = not batch.get("active", True)

            for idx in batch.get("token_indices", []):
                if 0 <= idx < len(tokens):
                    await set_account_active(telegram_user_id, tokens[idx]["token"], new_status)

            await user_db.update_one(
                {"type": "batches", "items.name": batch_name},
                {"$set": {"items.$.active": new_status}}
            )
            break

    return batch_found

async def set_batch_filter(telegram_user_id: int, batch_name: str, nationality_code: str):
    """Set nationality filter for all accounts in a batch"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)

    batches_doc = await user_db.find_one({"type": "batches"})
    if not batches_doc:
        return False

    tokens = await get_tokens(telegram_user_id)

    for batch in batches_doc.get("items", []):
        if batch["name"] == batch_name:
            for idx in batch.get("token_indices", []):
                if 0 <= idx < len(tokens):
                    filters = await get_user_filters(telegram_user_id, tokens[idx]["token"]) or {}
                    filters["filterNationalityCode"] = nationality_code
                    await set_user_filters(telegram_user_id, tokens[idx]["token"], filters)

            await user_db.update_one(
                {"type": "batches", "items.name": batch_name},
                {"$set": {"items.$.filter_nationality": nationality_code}}
            )
            return True

    return False

async def get_batch_by_name(telegram_user_id: int, batch_name: str):
    """Get a specific batch by name"""
    batches = await get_batches(telegram_user_id)
    for batch in batches:
        if batch["name"] == batch_name:
            return batch
    return None

async def auto_reorganize_batches_after_deletion(user_id: int):
    """
    Clears all existing batches and re-creates them based on the new token indices
    after a deletion event. It attempts to preserve batch filters/status.
    This corrects index corruption and removes empty batches.
    """
    user_db = _get_user_collection(user_id)
    tokens = await get_tokens(user_id)
    
    # 1. Get current batches to preserve filters/status
    old_batches = await get_batches(user_id)
    batch_metadata = {
        batch["name"]: {
            "filter": batch.get("filter_nationality", ""),
            "active": batch.get("active", True)
        } 
        for batch in old_batches
    }

    # 2. Re-create all batches based on current indices
    new_batches = {}
    for index, token in enumerate(tokens):
        # Calculate the batch number (0-9 is Batch 1, 10-19 is Batch 2, etc.)
        batch_number = (index // 10) + 1
        batch_name = f"Batch {batch_number}"
        
        if batch_name not in new_batches:
            # Initialize new batch data, attempting to restore old metadata
            metadata = batch_metadata.get(batch_name, {"active": True, "filter": ""})
            
            new_batches[batch_name] = {
                "name": batch_name,
                "token_indices": [],
                "active": metadata["active"],
                "filter_nationality": metadata["filter"]
            }
            
        new_batches[batch_name]["token_indices"].append(index)

    # 3. Replace the entire 'items' array with the new, corrected list of batches
    new_items_list = list(new_batches.values())
    await user_db.update_one(
        {"type": "batches"},
        {"$set": {"items": new_items_list}},
        upsert=True
    )

# REMOVED auto_organize_batches as it's replaced by automated logic

# --- Pending Signup Accounts Storage ---

async def get_pending_accounts(user_id: int):
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "pending_signup"})
    return doc.get("accounts", []) if doc else []

async def add_pending_accounts(user_id: int, accounts: list):
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "pending_signup"},
        {"$push": {"accounts": {"$each": accounts}}},
        upsert=True
    )

async def clear_pending_accounts(user_id: int):
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "pending_signup"},
        {"$set": {"accounts": []}},
        upsert=True
    )

async def remove_pending_account(user_id: int, email: str):
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "pending_signup"},
        {"$pull": {"accounts": {"email": email}}},
        upsert=True
    )


# --- Automation Settings ---

async def get_automation_settings(user_id: int) -> dict:
    """Get automation settings for a user."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "automation_settings"})
    if doc:
        return {
            "enabled": doc.get("enabled", False),
            "lounge_message": doc.get("lounge_message", ""),
            "chatroom_message": doc.get("chatroom_message", ""),
            "selected_accounts": doc.get("selected_accounts", "all"),  # "all" or list of indices
        }
    return {
        "enabled": False,
        "lounge_message": "",
        "chatroom_message": "",
        "selected_accounts": "all",
    }


async def set_automation_enabled(user_id: int, enabled: bool):
    """Toggle automation on/off."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_settings"},
        {"$set": {"enabled": enabled}},
        upsert=True
    )


async def set_automation_lounge_message(user_id: int, message: str):
    """Set the lounge message for automation."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_settings"},
        {"$set": {"lounge_message": message}},
        upsert=True
    )


async def set_automation_chatroom_message(user_id: int, message: str):
    """Set the chatroom message for automation."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_settings"},
        {"$set": {"chatroom_message": message}},
        upsert=True
    )


async def set_automation_accounts(user_id: int, accounts):
    """Set which accounts to use for automation. 'all' or list of token indices."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_settings"},
        {"$set": {"selected_accounts": accounts}},
        upsert=True
    )


async def get_automation_log(user_id: int) -> list:
    """Get automation activity log entries (last 20)."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "automation_log"})
    if doc:
        entries = doc.get("entries", [])
        return entries[-20:]  # Return last 20 entries
    return []


async def add_automation_log(user_id: int, entry: str):
    """Add an entry to the automation log."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    log_entry = {
        "text": entry,
        "time": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    }
    await user_db.update_one(
        {"type": "automation_log"},
        {"$push": {"entries": {"$each": [log_entry], "$slice": -50}}},
        upsert=True
    )


async def set_automation_last_request_time(user_id: int, token: str):
    """Record when a request cycle was last run for a token."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"request_times.{token}": datetime.datetime.utcnow()}},
        upsert=True
    )


async def get_automation_last_request_time(user_id: int, token: str):
    """Get when a request cycle was last run for a token."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "automation_timers"})
    if doc:
        return doc.get("request_times", {}).get(token)
    return None


async def set_automation_add_time(user_id: int, token: str, person_id: str):
    """Record when a person was added (for scheduling lounge/chatroom follow-ups)."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"add_times.{token}.{person_id}": datetime.datetime.utcnow()}},
        upsert=True
    )


async def get_automation_pending_followups(user_id: int) -> dict:
    """Get all pending follow-up timers."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "automation_timers"})
    if doc:
        return {
            "request_times": doc.get("request_times", {}),
            "add_times": doc.get("add_times", {}),
            "lounge_sent": doc.get("lounge_sent", {}),
            "chatroom_sent": doc.get("chatroom_sent", {}),
        }
    return {"request_times": {}, "add_times": {}, "lounge_sent": {}, "chatroom_sent": {}}


async def mark_lounge_sent(user_id: int, token: str, person_id: str, wave: int):
    """Mark that a lounge message wave was sent. wave: 1=20min, 2=1hr, 3=3hr"""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"lounge_sent.{token}.{person_id}.wave_{wave}": datetime.datetime.utcnow()}},
        upsert=True
    )


async def get_lounge_sent_waves(user_id: int, token: str, person_id: str) -> dict:
    """Get which lounge waves have been sent for a person."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "automation_timers"})
    if doc:
        return doc.get("lounge_sent", {}).get(token, {}).get(person_id, {})
    return {}


async def mark_chatroom_sent(user_id: int, token: str, person_id: str):
    """Mark that a chatroom message was sent for a person."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"chatroom_sent.{token}.{person_id}": datetime.datetime.utcnow()}},
        upsert=True
    )


async def is_chatroom_sent(user_id: int, token: str, person_id: str) -> bool:
    """Check if chatroom message was already sent for a person."""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "automation_timers"})
    if doc:
        return person_id in doc.get("chatroom_sent", {}).get(token, {})
    return False


# --- Batch Account Nationality Filters ---

async def set_batch_account_filter(telegram_user_id: int, batch_name: str, token_index: int, nationality_code: str):
    """Set nationality filter for a specific account within a batch"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    
    # Initialize batch_account_filters doc if it doesn't exist
    await user_db.update_one(
        {"type": "batch_account_filters"},
        {"$set": {f"{batch_name}.{token_index}": nationality_code}},
        upsert=True
    )


async def get_batch_account_filter(telegram_user_id: int, batch_name: str, token_index: int) -> str:
    """Get nationality filter for a specific account within a batch"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    
    doc = await user_db.find_one({"type": "batch_account_filters"})
    if doc and batch_name in doc:
        return doc[batch_name].get(str(token_index), "")
    return ""


async def get_all_batch_account_filters(telegram_user_id: int, batch_name: str) -> dict:
    """Get all account filters for a batch"""
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)
    
    doc = await user_db.find_one({"type": "batch_account_filters"})
    if doc and batch_name in doc:
        return doc[batch_name]
    return {}


# --- Blocked Users ---

async def block_user(user_id: int, blocked_meeff_id: str):
    """Add a user to the block list (block_meeff_id format: "meeff_id")"""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "blocked_users"},
        {"$addToSet": {"blocked_ids": blocked_meeff_id}},
        upsert=True
    )


async def unblock_user(user_id: int, blocked_meeff_id: str):
    """Remove a user from the block list"""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "blocked_users"},
        {"$pull": {"blocked_ids": blocked_meeff_id}},
        upsert=True
    )


async def get_blocked_users(user_id: int) -> set:
    """Get all blocked user IDs as a set"""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    doc = await user_db.find_one({"type": "blocked_users"})
    if doc:
        return set(doc.get("blocked_ids", []))
    return set()


async def clear_blocked_users(user_id: int):
    """Clear all blocked users"""
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.delete_one({"type": "blocked_users"})
