from pymongo import MongoClient
import datetime

# MongoDB connection
client = MongoClient("mongodb+srv://irexanon:xUf7PCf9cvMHy8g6@rexdb.d9rwo.mongodb.net/?retryWrites=true&w=majority&appName=RexDB")
db = client.meeff_bot

# Helper function to get a user's collection
def _get_user_collection(telegram_user_id):
    """Get the collection for a user"""
    collection_name = f"user_{telegram_user_id}"
    return db[collection_name]

# Helper function to ensure collection exists with basic structure
def _ensure_user_collection_exists(telegram_user_id):
    """Make sure user collection exists with default documents"""
    user_db = _get_user_collection(telegram_user_id)
    
    # Check if the collection is empty
    if user_db.count_documents({}) == 0:
        # Initialize with basic structure
        user_db.insert_one({"type": "metadata", "created_at": datetime.datetime.utcnow(), "user_id": telegram_user_id})
        user_db.insert_one({"type": "tokens", "items": []})
        user_db.insert_one({"type": "settings", "current_token": None, "spam_filter": False})
        user_db.insert_one({"type": "sent_records", "data": {}})
        user_db.insert_one({"type": "filters", "data": {}})
        user_db.insert_one({"type": "info_cards", "data": {}})
    return True

# Enhanced DB Collection Management Functions
def list_all_collections():
    """List all user collections with detailed data summary"""
    collection_names = db.list_collection_names()
    user_collections = []
    
    for name in collection_names:
        if name.startswith("user_") and name != "user_":
            try:
                user_id = name[5:]  # Remove "user_" prefix
                summary = get_collection_summary(name)
                user_collections.append({
                    "collection_name": name,
                    "user_id": user_id,
                    "display_name": f"user_{user_id}",
                    "summary": summary
                })
            except Exception as e:
                print(f"Error processing collection {name}: {e}")
                continue
    
    return sorted(user_collections, key=lambda x: x.get("summary", {}).get("created_at") or datetime.datetime.min, reverse=True)

def get_collection_summary(collection_name):
    """Get detailed summary of data in a collection"""
    try:
        collection = db[collection_name]
        
        # Get tokens count and details
        tokens_doc = collection.find_one({"type": "tokens"})
        tokens_count = 0
        active_tokens = 0
        if tokens_doc and "items" in tokens_doc:
            tokens_count = len(tokens_doc["items"])
            active_tokens = sum(1 for token in tokens_doc["items"] if token.get("active", True))
        
        # Get sent records count by category
        sent_doc = collection.find_one({"type": "sent_records"})
        sent_records = {"total": 0, "categories": {}}
        if sent_doc and "data" in sent_doc:
            for category, ids in sent_doc["data"].items():
                count = len(ids) if isinstance(ids, list) else 0
                sent_records["categories"][category] = count
                sent_records["total"] += count
        
        # Get info cards count
        info_doc = collection.find_one({"type": "info_cards"})
        info_cards_count = len(info_doc.get("data", {})) if info_doc else 0
        
        # Get settings
        settings_doc = collection.find_one({"type": "settings"})
        current_token = settings_doc.get("current_token") if settings_doc else None
        spam_filter = settings_doc.get("spam_filter", False) if settings_doc else False
        
        # Get creation date
        metadata_doc = collection.find_one({"type": "metadata"})
        created_at = metadata_doc.get("created_at") if metadata_doc else None
        
        return {
            "tokens_count": tokens_count,
            "active_tokens": active_tokens,
            "sent_records": sent_records,
            "info_cards_count": info_cards_count,
            "has_current_token": bool(current_token),
            "current_token_preview": current_token[:10] + "..." if current_token else None,
            "spam_filter_enabled": spam_filter,
            "created_at": created_at,
            "total_documents": collection.count_documents({})
        }
    except Exception as e:
        return {"error": str(e)}

def connect_to_collection(collection_name, target_user_id):
    """Connect to existing collection by transferring all data"""
    try:
        # Check if source collection exists
        if collection_name not in db.list_collection_names():
            return False, f"Collection '{collection_name}' not found"
        
        # Ensure target collection exists
        _ensure_user_collection_exists(target_user_id)
        
        from_collection = db[collection_name]
        to_collection = _get_user_collection(target_user_id)
        
        # Get all documents from source collection
        all_docs = list(from_collection.find({}))
        
        if not all_docs:
            return False, "Source collection is empty"
        
        # Clear target collection first
        to_collection.delete_many({})
        
        # Update metadata for target collection
        for doc in all_docs:
            if doc.get("type") == "metadata":
                doc["user_id"] = target_user_id
                doc["connected_at"] = datetime.datetime.utcnow()
                doc["original_collection"] = collection_name
        
        # Insert all documents to target collection
        to_collection.insert_many(all_docs)
        
        return True, f"Successfully connected to '{collection_name}' with {len(all_docs)} documents"
        
    except Exception as e:
        return False, f"Connection failed: {str(e)}"

def rename_user_collection(user_id, new_collection_name):
    """Rename a user's collection"""
    try:
        old_collection_name = f"user_{user_id}"
        
        # Check if old collection exists
        if old_collection_name not in db.list_collection_names():
            return False, "Your collection not found"
        
        # Validate new collection name
        if not new_collection_name.startswith("user_"):
            new_collection_name = f"user_{new_collection_name}"
        
        # Check if new collection name already exists
        if new_collection_name in db.list_collection_names():
            return False, "Target collection name already exists"
        
        # Get all documents from old collection
        old_collection = db[old_collection_name]
        all_docs = list(old_collection.find({}))
        
        if not all_docs:
            return False, "Your collection is empty"
        
        # Create new collection and insert documents
        new_collection = db[new_collection_name]
        
        # Update metadata
        for doc in all_docs:
            if doc.get("type") == "metadata":
                doc["renamed_at"] = datetime.datetime.utcnow()
                doc["original_name"] = old_collection_name
        
        new_collection.insert_many(all_docs)
        
        # Delete old collection
        old_collection.drop()
        
        return True, f"Successfully renamed to '{new_collection_name}'"
        
    except Exception as e:
        return False, f"Rename failed: {str(e)}"

def transfer_to_user(from_user_id, to_user_id):
    """Transfer all data from one user to another"""
    try:
        from_collection_name = f"user_{from_user_id}"
        
        # Check if source collection exists
        if from_collection_name not in db.list_collection_names():
            return False, "Your collection not found"
        
        # Ensure target collection exists
        _ensure_user_collection_exists(to_user_id)
        
        from_collection = db[from_collection_name]
        to_collection = _get_user_collection(to_user_id)
        
        # Get all documents from source collection
        all_docs = list(from_collection.find({}))
        
        if not all_docs:
            return False, "Your collection is empty"
        
        # Clear target collection first
        to_collection.delete_many({})
        
        # Update metadata for target collection
        for doc in all_docs:
            if doc.get("type") == "metadata":
                doc["user_id"] = to_user_id
                doc["transferred_at"] = datetime.datetime.utcnow()
                doc["transferred_from"] = from_user_id
        
        # Insert all documents to target collection
        to_collection.insert_many(all_docs)
        
        return True, f"Successfully transferred {len(all_docs)} documents to user {to_user_id}"
        
    except Exception as e:
        return False, f"Transfer failed: {str(e)}"

def get_current_collection_info(user_id):
    """Get current user's collection information"""
    collection_name = f"user_{user_id}"
    if collection_name in db.list_collection_names():
        summary = get_collection_summary(collection_name)
        return {
            "collection_name": collection_name,
            "exists": True,
            "summary": summary
        }
    else:
        return {
            "collection_name": collection_name,
            "exists": False,
            "summary": None
        }

# Info card functions
def set_info_card(telegram_user_id, token, info_text, email=None):
    """Store info card for a token"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "info_cards"},
        {"$set": {f"data.{token}": {"info": info_text, "email": email, "updated_at": datetime.datetime.utcnow()}}},
        upsert=True
    )
    return True

def get_info_card(telegram_user_id, token):
    """Get info card for a token"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return None
    
    user_db = _get_user_collection(telegram_user_id)
    cards_doc = user_db.find_one({"type": "info_cards"})
    if cards_doc and "data" in cards_doc and token in cards_doc["data"]:
        return cards_doc["data"][token].get("info")
    return None

# Set or update token for a user
def set_token(telegram_user_id, token, meeff_user_id, email=None, filters=None):
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)

    # Get current tokens
    tokens_doc = user_db.find_one({"type": "tokens"})
    tokens = tokens_doc.get("items", []) if tokens_doc else []

    # Check if token exists
    token_exists = False
    for i, t in enumerate(tokens):
        if t.get("token") == token:
            # Update existing token
            tokens[i]["name"] = meeff_user_id
            if email:
                tokens[i]["email"] = email
            if filters:
                tokens[i]["filters"] = filters
            # Keep existing active status or default to True
            if "active" not in tokens[i]:
                tokens[i]["active"] = True
            token_exists = True
            break

    # Add new token if not exists
    if not token_exists:
        token_data = {"token": token, "name": meeff_user_id, "active": True}
        if email:
            token_data["email"] = email
        if filters:
            token_data["filters"] = filters
        tokens.append(token_data)

    # Update tokens in database
    user_db.update_one(
        {"type": "tokens"},
        {"$set": {"items": tokens}},
        upsert=True
    )
    return True

# Add new functions to toggle token active status
def toggle_token_status(telegram_user_id, token):
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)

    # Get current tokens
    tokens_doc = user_db.find_one({"type": "tokens"})
    if not tokens_doc:
        return False

    tokens = tokens_doc.get("items", [])
    status_changed = False

    # Find and toggle the token's active status
    for i, t in enumerate(tokens):
        if t.get("token") == token:
            current_status = t.get("active", True)
            tokens[i]["active"] = not current_status
            status_changed = True
            break

    if status_changed:
        # Update tokens in database
        user_db.update_one(
            {"type": "tokens"},
            {"$set": {"items": tokens}}
        )
        return True
    return False

def set_account_active(telegram_user_id, token, active_status):
    """Set account active/inactive status"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    tokens_doc = user_db.find_one({"type": "tokens"})
    if not tokens_doc:
        return False

    tokens = tokens_doc.get("items", [])
    for i, t in enumerate(tokens):
        if t.get("token") == token:
            tokens[i]["active"] = active_status
            break

    user_db.update_one(
        {"type": "tokens"},
        {"$set": {"items": tokens}}
    )
    return True

# Add function to get active tokens only
def get_active_tokens(telegram_user_id):
    if not _ensure_user_collection_exists(telegram_user_id):
        return []
    
    user_db = _get_user_collection(telegram_user_id)

    tokens_doc = user_db.find_one({"type": "tokens"})
    if not tokens_doc:
        return []

    # Filter tokens that are active (or where active field doesn't exist)
    return [t for t in tokens_doc.get("items", []) if t.get("active", True)]

# Add function to get token status
def get_token_status(telegram_user_id, token):
    if not _ensure_user_collection_exists(telegram_user_id):
        return None
    
    user_db = _get_user_collection(telegram_user_id)

    tokens_doc = user_db.find_one({"type": "tokens"})
    if tokens_doc:
        for t in tokens_doc.get("items", []):
            if t.get("token") == token:
                return t.get("active", True)
    return None

# Get all tokens for a user
def get_tokens(telegram_user_id):
    if not _ensure_user_collection_exists(telegram_user_id):
        return []
    
    user_db = _get_user_collection(telegram_user_id)
    tokens_doc = user_db.find_one({"type": "tokens"})
    return tokens_doc.get("items", []) if tokens_doc else []

def get_all_tokens(telegram_user_id):
    """Alias for get_tokens for compatibility"""
    return get_tokens(telegram_user_id)

# List all tokens in the database
def list_tokens():
    result = []
    # Get all collection names
    collection_names = db.list_collection_names()

    # Filter for user collections
    user_collections = [name for name in collection_names if name.startswith("user_")]

    for collection_name in user_collections:
        user_id = collection_name.split("_", 1)[1]  # Extract user ID from collection name
        user_db = db[collection_name]

        tokens_doc = user_db.find_one({"type": "tokens"})
        if tokens_doc:
            for token in tokens_doc.get("items", []):
                result.append({
                    "user_id": user_id,
                    "token": token.get("token"),
                    "name": token.get("name")
                })

    return result

# Set current account token for a user
def set_current_account(telegram_user_id, token):
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)

    user_db.update_one(
        {"type": "settings"},
        {"$set": {"current_token": token}},
        upsert=True
    )
    return True

# Get current account token for a user
def get_current_account(telegram_user_id):
    if not _ensure_user_collection_exists(telegram_user_id):
        return None
    
    user_db = _get_user_collection(telegram_user_id)

    settings = user_db.find_one({"type": "settings"})
    return settings.get("current_token") if settings else None

# Delete a token for a user
def delete_token(telegram_user_id, token):
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)

    # Get current tokens and filter out the one to delete
    tokens_doc = user_db.find_one({"type": "tokens"})
    if tokens_doc:
        tokens = tokens_doc.get("items", [])
        updated_tokens = [t for t in tokens if t.get("token") != token]

        # Update tokens in database
        user_db.update_one(
            {"type": "tokens"},
            {"$set": {"items": updated_tokens}}
        )

    # Check if this was the current token
    settings = user_db.find_one({"type": "settings"})
    if settings and settings.get("current_token") == token:
        user_db.update_one(
            {"type": "settings"},
            {"$set": {"current_token": None}}
        )
    
    # Also delete info card
    user_db.update_one(
        {"type": "info_cards"},
        {"$unset": {f"data.{token}": ""}}
    )
    return True

# Set filters for a specific user and token
def set_user_filters(telegram_user_id, token, filters):
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)

    # Get current tokens
    tokens_doc = user_db.find_one({"type": "tokens"})
    tokens = tokens_doc.get("items", []) if tokens_doc else []

    # Update the token's filters
    for i, t in enumerate(tokens):
        if t.get("token") == token:
            tokens[i]["filters"] = filters
            break

    # Update tokens in database
    user_db.update_one(
        {"type": "tokens"},
        {"$set": {"items": tokens}}
    )
    return True

# Get filters for a specific user and token
def get_user_filters(telegram_user_id, token):
    if not _ensure_user_collection_exists(telegram_user_id):
        return None
    
    user_db = _get_user_collection(telegram_user_id)

    tokens_doc = user_db.find_one({"type": "tokens"})
    if tokens_doc:
        for t in tokens_doc.get("items", []):
            if t.get("token") == token:
                return t.get("filters")
    return None

# Enable or disable spam filter for a user
def set_spam_filter(telegram_user_id, status: bool):
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)

    user_db.update_one(
        {"type": "settings"},
        {"$set": {"spam_filter": status}},
        upsert=True
    )
    return True

# Set individual spam filter for specific features
def set_individual_spam_filter(telegram_user_id, filter_type: str, status: bool):
    """Set spam filter for individual features: chatroom, request, lounge"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "settings"},
        {"$set": {f"spam_filter_{filter_type}": status}},
        upsert=True
    )
    return True

# Get individual spam filter status
def get_individual_spam_filter(telegram_user_id: int, filter_type: str) -> bool:
    """Get spam filter status for individual features: chatroom, request, lounge"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    settings = user_db.find_one({"type": "settings"})
    return settings.get(f"spam_filter_{filter_type}", False) if settings else False

# Get all spam filter settings
def get_all_spam_filters(telegram_user_id: int) -> dict:
    """Get all spam filter settings"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return {"chatroom": False, "request": False, "lounge": False}
    
    user_db = _get_user_collection(telegram_user_id)
    settings = user_db.find_one({"type": "settings"})
    if not settings:
        return {"chatroom": False, "request": False, "lounge": False}
    
    return {
        "chatroom": settings.get("spam_filter_chatroom", False),
        "request": settings.get("spam_filter_request", False),
        "lounge": settings.get("spam_filter_lounge", False)
    }
# Get spam filter status for a user
# Automation settings functions
def set_automation_setting(telegram_user_id: int, automation_type: str, status: bool):
    """Set automation status for individual features: chatroom, request, lounge"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "settings"},
        {"$set": {f"automation_{automation_type}": status}},
        upsert=True
    )
    return True

def get_automation_setting(telegram_user_id: int, automation_type: str) -> bool:
    """Get automation status for individual features: chatroom, request, lounge"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    settings = user_db.find_one({"type": "settings"})
    return settings.get(f"automation_{automation_type}", False) if settings else False

def get_all_automation_settings(telegram_user_id: int) -> dict:
    """Get all automation settings"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return {"chatroom": False, "request": False, "lounge": False}
    
    user_db = _get_user_collection(telegram_user_id)
    settings = user_db.find_one({"type": "settings"})
    if not settings:
        return {"chatroom": False, "request": False, "lounge": False}
    
    return {
        "chatroom": settings.get("automation_chatroom", False),
        "request": settings.get("automation_request", False),
        "lounge": settings.get("automation_lounge", False)
    }

# Automation message settings
def set_automation_message(telegram_user_id: int, message_type: str, message: str):
    """Set automation message for chatroom or lounge"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "settings"},
        {"$set": {f"automation_message_{message_type}": message}},
        upsert=True
    )
    return True

def get_automation_message(telegram_user_id: int, message_type: str) -> str:
    """Get automation message for chatroom or lounge"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return ""
    
    user_db = _get_user_collection(telegram_user_id)
    settings = user_db.find_one({"type": "settings"})
    return settings.get(f"automation_message_{message_type}", "") if settings else ""

# Speed settings functions
def set_speed_setting(telegram_user_id: int, speed_type: str, delay: float):
    """Set speed delay for different operations"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "settings"},
        {"$set": {f"speed_{speed_type}": delay}},
        upsert=True
    )
    return True

def get_speed_setting(telegram_user_id: int, speed_type: str) -> float:
    """Get speed delay for different operations"""
    if not _ensure_user_collection_exists(telegram_user_id):
        # Default speeds
        defaults = {"request": 0.5, "lounge": 2.0, "chatroom": 1.0, "skip": 1.0}
        return defaults.get(speed_type, 1.0)
    
    user_db = _get_user_collection(telegram_user_id)
    settings = user_db.find_one({"type": "settings"})
    if not settings:
        defaults = {"request": 0.5, "lounge": 2.0, "chatroom": 1.0, "skip": 1.0}
        return defaults.get(speed_type, 1.0)
    
    defaults = {"request": 0.5, "lounge": 2.0, "chatroom": 1.0, "skip": 1.0}
    return settings.get(f"speed_{speed_type}", defaults.get(speed_type, 1.0))

def get_all_speed_settings(telegram_user_id: int) -> dict:
    """Get all speed settings"""
    return {
        "request": get_speed_setting(telegram_user_id, "request"),
        "lounge": get_speed_setting(telegram_user_id, "lounge"),
        "chatroom": get_speed_setting(telegram_user_id, "chatroom"),
        "skip": get_speed_setting(telegram_user_id, "skip")
    }

def get_spam_filter(telegram_user_id: int) -> bool:
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)

    settings = user_db.find_one({"type": "settings"})
    return settings.get("spam_filter", False) if settings else False

# Get all target IDs for a category for a user
def get_already_sent_ids(telegram_user_id, category):
    """Fetch all target_ids for a given user and category."""
    if not _ensure_user_collection_exists(telegram_user_id):
        return set()
    
    user_db = _get_user_collection(telegram_user_id)
    records_doc = user_db.find_one({"type": "sent_records"}, {"data." + category: 1})
    if records_doc and "data" in records_doc and category in records_doc["data"]:
        return set(records_doc["data"][category])
    return set()

def clear_sent_ids(telegram_user_id, category):
    """Clear all sent IDs for a specific category - useful for testing fresh fetches"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "sent_records"},
        {"$unset": {f"data.{category}": ""}},
        upsert=True
    )
    return True

# Record that we've sent a message/request to a target
def add_sent_id(telegram_user_id, category, target_id):
    """Record a target_id as sent for a user and category."""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
    
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "sent_records"},
        {"$addToSet": {f"data.{category}": target_id}},
        upsert=True
    )
    return True

# Check if we've already sent to this target
async def is_already_sent(telegram_user_id, category, target_id, bulk=False):
    """Check if target_id(s) have already been recorded as sent"""
    if not _ensure_user_collection_exists(telegram_user_id):
        return False if not bulk else set()
    
    user_db = _get_user_collection(telegram_user_id)
    
    if not bulk:
        # Single ID check
        records_doc = user_db.find_one({"type": "sent_records"}, {f"data.{category}": 1})
        if records_doc and "data" in records_doc and category in records_doc["data"]:
            sent_ids = records_doc["data"][category]
            if isinstance(sent_ids, (list, set)):
                return target_id in sent_ids
        return False
    else:
        # Bulk check - returns set of existing IDs
        records_doc = user_db.find_one({"type": "sent_records"}, {f"data.{category}": 1})
        if records_doc and "data" in records_doc and category in records_doc["data"]:
            existing_ids = records_doc["data"][category]
            if isinstance(existing_ids, (list, set)):
                return set(existing_ids)
        return set()

async def bulk_add_sent_ids(telegram_user_id, category, target_ids):
    """Record multiple target_ids as sent for a user and category"""
    if not target_ids:
        return False
    
    if not _ensure_user_collection_exists(telegram_user_id):
        return False
        
    user_db = _get_user_collection(telegram_user_id)
    user_db.update_one(
        {"type": "sent_records"},
        {"$addToSet": {f"data.{category}": {"$each": list(target_ids)}}},
        upsert=True
    )
    return True

async def has_valid_access(telegram_user_id):
    """Check if user has valid access to use the bot"""
    collection_name = f"user_{telegram_user_id}"
    if collection_name not in db.list_collection_names():
        return False
    
    user_db = db[collection_name]
    # Check if collection exists and has basic structure
    if user_db.count_documents({"type": "metadata"}) == 0:
        return False
    return True

def get_message_delay(telegram_user_id):
    # Return the delay in seconds for this user
    # You could store this in your database
    return 2  # Default 2 second delay

def transfer_user_data(from_telegram_id, to_telegram_id):
    """Transfer all user data from one telegram user to another"""
    return transfer_to_user(from_telegram_id, to_telegram_id)

# Legacy functions for backward compatibility
def has_interacted(telegram_user_id, action_type, user_token):
    """Legacy function - checks a separate interactions collection"""
    interaction_record = db.interactions.find_one({
        "user_id": telegram_user_id,
        "action_type": action_type,
        "user_token": user_token
    })
    return interaction_record is not None

def log_interaction(telegram_user_id, action_type, user_token):
    """Legacy function - logs to a separate interactions collection"""
    interaction_data = {
        "user_id": telegram_user_id,
        "action_type": action_type,
        "user_token": user_token,
        "timestamp": datetime.datetime.utcnow()
    }
    db.interactions.insert_one(interaction_data)
