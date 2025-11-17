from pymongo import MongoClient
import datetime
from motor.motor_asyncio import AsyncIOMotorClient
import os
from typing import Dict, Optional, Tuple, Any


# MongoDB connection using the asynchronous Motor client
client = AsyncIOMotorClient("mongodb+srv://irexanon:xUf7PCf9cvMHy8g6@rexdb.d9rwo.mongodb.net/?retryWrites=true&w=majority&appName=RexDB")
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
async def _ensure_user_collection_exists(telegram_user_id):
    """Make sure user collection exists with default documents"""
    user_db = _get_user_collection(telegram_user_id)
    # Check if 'metadata' exists; a simpler check than count_documents({}) == 0
    if await user_db.count_documents({"type": "metadata"}) == 0:
        await user_db.insert_many([
            {"type": "metadata", "created_at": datetime.datetime.utcnow(), "user_id": telegram_user_id},
            {"type": "tokens", "items": []},
            {"type": "settings", "current_token": None, "spam_filter": False},
            {"type": "sent_records", "data": {}},
            {"type": "filters", "data": {}},
            {"type": "info_cards", "data": {}}
        ])

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
    if collection_name in await db.list_collection_names():
        return {"collection_name": collection_name, "exists": True, "summary": await get_collection_summary(collection_name)}
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

# MODIFIED: set_token now returns the token's index and handles email-based duplicates
async def set_token(telegram_user_id, token, name, email=None, filters=None, active=True) -> int:
    await _ensure_user_collection_exists(telegram_user_id)
    user_db = _get_user_collection(telegram_user_id)

    tokens_doc = await user_db.find_one({"type": "tokens"})
    tokens_list = tokens_doc.get("items", []) if tokens_doc else []

    token_index = -1
    email_index = -1

    # 1. Check if email already exists (to prevent duplicates) and if token exists
    for i, t in enumerate(tokens_list):
        if email and t.get("email") == email:
            email_index = i
        if t["token"] == token:
            token_index = i

    # 2. If email exists, remove the old entry first (replace logic)
    if email and email_index != -1 and token_index != email_index:
        # Remove old token with same email
        await user_db.update_one(
            {"type": "tokens"},
            {"$pull": {"items": {"email": email}}}
        )
        # Refresh the list
        tokens_doc = await user_db.find_one({"type": "tokens"})
        tokens_list = tokens_doc.get("items", []) if tokens_doc else []
        # Recalculate token_index
        token_index = -1
        for i, t in enumerate(tokens_list):
            if t["token"] == token:
                token_index = i
                break

    if token_index != -1:
        # 3. Token exists (Update logic)
        update_fields = {
            "items.$.name": name,
            "items.$.active": active
        }
        if email: update_fields["items.$.email"] = email
        if filters: update_fields["items.$.filters"] = filters

        await user_db.update_one(
            {"type": "tokens", "items.token": token},
            {"$set": update_fields}
        )
    else:
        # 4. Token is new (Insertion logic)
        token_index = len(tokens_list) # Assign the new index
        token_data = {
            "token": token,
            "name": name,
            "active": active
        }
        if email: token_data["email"] = email
        if filters: token_data["filters"] = filters

        await user_db.update_one(
            {"type": "tokens"},
            {"$push": {"items": token_data}},
            upsert=True
        )

    return token_index # Return the index

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
    
    last_batch_data, total_tokens = await get_last_batch(user_id)
    
    # Calculate the current batch number (e.g., index 0-9 is Batch 1, index 10-19 is Batch 2)
    new_batch_number = (token_index // 10) + 1
    new_batch_name = f"Batch {new_batch_number}"

    if last_batch_data and last_batch_data.get("name") == new_batch_name:
        # Case 1: Add to existing, non-full batch (should always be the last one)
        await user_db.update_one(
            {"type": "batches", "items.name": new_batch_name},
            {"$push": {"items.$.token_indices": token_index}}
        )
    else:
        # Case 2: Create a brand new batch for this index (e.g., token 0 or token 10, 20, etc.)
        batch_data = {
            "name": new_batch_name,
            "token_indices": [token_index],
            "active": True,
            "filter_nationality": ""
        }
        await user_db.update_one(
            {"type": "batches"},
            {"$push": {"items": batch_data}},
            upsert=True
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
