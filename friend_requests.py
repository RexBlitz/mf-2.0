import asyncio
import aiohttp
import logging
import html
from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import get_individual_spam_filter, bulk_add_sent_ids, get_active_tokens, get_current_account, get_already_sent_ids
from filters import apply_filter_for_account, is_request_filter_enabled
from collections import defaultdict
from dateutil import parser
from datetime import datetime, timezone

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ✅ Speed configuration
PER_USER_DELAY = 0.5        # Delay can be fast again because we send photos directly
PER_BATCH_DELAY = 1         # Delay between fetching new batches of users
EMPTY_BATCH_DELAY = 2       # Delay after receiving an empty batch
PER_ERROR_DELAY = 5         # Delay after a network or API error

# Global state variables for friend requests
user_states = defaultdict(lambda: {
    "running": False,
    "status_message_id": None,
    "pinned_message_id": None,
    "total_added_friends": 0,
    "batch_index": 0,
    "stopped": False,
})

# Inline keyboards for friend request operations
stop_markup = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Stop Requests", callback_data="stop")]
])

async def fetch_users(session, token, user_id):
    """Fetch users from the API for friend requests."""
    # Using the endpoint structure provided, with example location data
    url = "https://api.meeff.com/user/explore/v2?lng=71.9140141&unreachableUserIds=&lat=29.6264544&locale=en"
    
    # --- SIMPLIFIED HEADERS ---
    headers = {
        'User-Agent': "okhttp/5.1.0",
        'meeff-access-token': token
    }
    # --------------------------------------------------------
    
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 401:
                logging.error(f"Failed to fetch users: 401 Unauthorized (Token: {token[:10]}... is likely invalid)")
                return None
            if response.status == 429:
                logging.error("Request limit exceeded while fetching users.")
                return None
            if response.status != 200:
                logging.error(f"Failed to fetch users: {response.status}")
                return []
            return (await response.json()).get("users", [])
    except Exception as e:
        logging.error(f"Fetch users failed: {e}")
        return []

def format_user(user):
    def time_ago(dt_str):
        if not dt_str: return "N/A"
        try:
            dt = parser.isoparse(dt_str)
            now = datetime.now(timezone.utc)
            diff = now - dt
            minutes = int(diff.total_seconds() // 60)
            if minutes < 1: return "just now"
            if minutes < 60: return f"{minutes} min ago"
            hours = minutes // 60
            if hours < 24: return f"{hours} hr ago"
            days = hours // 24
            return f"{days} day(s) ago"
        except Exception: return "unknown"

    last_active = time_ago(user.get("recentAt"))
    nationality = html.escape(user.get('nationalityCode', 'N/A'))
    height = html.escape(str(user.get('height', 'N/A')))
    if "|" in height:
        height_val, height_unit = height.split("|", 1)
        height = f"{height_val.strip()} {height_unit.strip()}"
        
    return (
        f"<b>Name:</b> {html.escape(user.get('name', 'N/A'))}\n"
        f"<b>ID:</b> <code>{html.escape(user.get('_id', 'N/A'))}</code>\n"
        f"<b>Nationality:</b> {nationality}\n"
        f"<b>Height:</b> {height}\n"
        f"<b>Description:</b> {html.escape(user.get('description', 'N/A'))}\n"
        f"<b>Birth Year:</b> {html.escape(str(user.get('birthYear', 'N/A')))}\n"
        f"<b>Platform:</b> {html.escape(user.get('platform', 'N/A'))}\n"
        f"<b>Profile Score:</b> {html.escape(str(user.get('profileScore', 'N/A')))}\n"
        f"<b>Distance:</b> {html.escape(str(user.get('distance', 'N/A')))} km\n"
        f"<b>Language Codes:</b> {html.escape(', '.join(user.get('languageCodes', [])))}\n"
        f"<b>Last Active:</b> {last_active}"
    )

async def process_users(session, users, token, user_id, bot, token_name, already_sent_ids, lock):
    """
    Process a batch of users.
    Checks 'already_sent_ids' ALWAYS (for session deduplication),
    but only saves to DB if the spam filter setting is ON.
    """
    state = user_states[user_id]
    added_count = 0
    filtered_count = 0
    limit_reached = False
    
    # Check if we should SAVE to the database for future runs
    is_spam_filter_enabled = await get_individual_spam_filter(user_id, "request")
    ids_to_persist = []

    for user in users:
        if not state["running"]: break

        user_id_to_check = user["_id"]

        # --- SESSION DEDUPLICATION ---
        # Check for duplicates ALWAYS. 
        # If filter is OFF, 'already_sent_ids' is just an empty set for this session.
        # If filter is ON, 'already_sent_ids' contains the DB history.
        async with lock:
            if user_id_to_check in already_sent_ids:
                filtered_count += 1
                continue
            already_sent_ids.add(user_id_to_check)
        # -----------------------------
        
        url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={user_id_to_check}&isOkay=1"
        
        headers = {
            'User-Agent': "okhttp/5.1.0",
            'meeff-access-token': token
        }

        try:
            async with session.get(url, headers=headers) as response:
                data = await response.json()

                if data.get("errorCode") == "LikeExceeded":
                    logging.info(f"Daily like limit reached for {token_name}.")
                    limit_reached = True
                    break

                # Only mark for SAVING to DB if the setting is ON
                if is_spam_filter_enabled:
                    ids_to_persist.append(user_id_to_check)

                # --- Send Telegram Notification ---
                details = format_user(user)
                first_photo_url = user.get('photoUrls', [None])[0]

                if first_photo_url:
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=first_photo_url,
                        caption=details,
                        parse_mode="HTML"
                    )
                else:
                    await bot.send_message(
                        chat_id=user_id,
                        text=details,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                
                added_count += 1
                state["total_added_friends"] += 1
                await asyncio.sleep(PER_USER_DELAY)
        
        except Exception as e:
            logging.error(f"Error processing user with {token_name}: {e}")
            await asyncio.sleep(PER_ERROR_DELAY)
    
    # Only write to the database if the user actually wants to save history (Filter is ON)
    if is_spam_filter_enabled and ids_to_persist:
        await bulk_add_sent_ids(user_id, "request", ids_to_persist)

    return limit_reached, added_count, filtered_count


async def run_requests(user_id, bot, target_channel_id):
    """Main function to run the request process for a single token."""
    state = user_states[user_id]
    state.update({"total_added_friends": 0, "batch_index": 0, "running": True, "stopped": False})
    
    token = await get_current_account(user_id)
    if not token:
        await bot.edit_message_text(chat_id=user_id, message_id=state["status_message_id"], text="No active account found.")
        state["running"] = False
        return

    tokens = await get_active_tokens(user_id)
    token_name = next((t.get("name", "Default") for t in tokens if t["token"] == token), "Default")
    
    # --- INITIALIZATION ---
    # Check filter status for SPAM (Deduplication)
    is_spam_enabled = await get_individual_spam_filter(user_id, "request")
    if is_spam_enabled:
        already_sent_ids = await get_already_sent_ids(user_id, "request")
    else:
        already_sent_ids = set()
    # ----------------------

    lock = asyncio.Lock()

    async with aiohttp.ClientSession() as session:
        while state["running"]:
            try:
                # --- APPLY FILTERS ---
                if is_request_filter_enabled(user_id):
                    await apply_filter_for_account(token, user_id)
                    await asyncio.sleep(1)
                # ---------------------
                
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["status_message_id"],
                    text=f"{token_name}: Requests sent: {state['total_added_friends']}",
                    reply_markup=stop_markup
                )

                users = await fetch_users(session, token, user_id)
                state["batch_index"] += 1
                
                if users is None:
                    await bot.edit_message_text(
                        chat_id=user_id, message_id=state["status_message_id"],
                        text=f"{token_name}: Token is invalid (401 Unauthorized). Stopping."
                    )
                    state["running"] = False
                    break

                if not users:
                    logging.info(f"No users found for batch {state['batch_index']}.")
                    if state["batch_index"] > 10:
                        await bot.edit_message_text(
                            chat_id=user_id, message_id=state["status_message_id"],
                            text=f"{token_name}: No more users found. Total: {state['total_added_friends']}"
                        )
                        state["running"] = False
                        break
                    await asyncio.sleep(EMPTY_BATCH_DELAY)
                    continue
                
                limit_reached, _, _ = await process_users(session, users, token, user_id, bot, token_name, already_sent_ids, lock)
                
                if limit_reached:
                    state["running"] = False
                    break
                
                await asyncio.sleep(PER_BATCH_DELAY)
            
            except Exception as e:
                logging.error(f"Error during processing: {e}")
                await asyncio.sleep(PER_ERROR_DELAY)

    if state.get("pinned_message_id"):
        try: await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        except Exception: pass
    
    status = "Stopped" if state.get("stopped") else "Completed"
    await bot.send_message(user_id, f"✅ {status}! Total Added: {state.get('total_added_friends', 0)}")


async def process_all_tokens(user_id, tokens, bot, target_channel_id, initial_status_message=None):
    """Process friend requests for all tokens concurrently."""
    state = user_states[user_id]
    state.update({"total_added_friends": 0, "running": True, "stopped": False})

    if not initial_status_message:
        status_message = await bot.send_message(chat_id=user_id, text="🔄 <b>AIO Starting...</b>", parse_mode="HTML", reply_markup=stop_markup)
    else:
        status_message = initial_status_message

    state["status_message_id"] = status_message.message_id
    try:
        await bot.pin_chat_message(chat_id=user_id, message_id=status_message.message_id, disable_notification=True)
        state["pinned_message_id"] = status_message.message_id
    except Exception as e:
        logging.error(f"Failed to pin message: {e}")

    token_status = {
        token_obj["token"]: {
            "name": token_obj.get("name", f"Account {i+1}"),
            "added": 0,
            "filtered": 0,
            "status": "Queued"
        } for i, token_obj in enumerate(tokens)
    }
    
    # --- INITIALIZATION ---
    # Check filter status for SPAM (Deduplication)
    is_spam_enabled = await get_individual_spam_filter(user_id, "request")
    if is_spam_enabled:
        session_sent_ids = await get_already_sent_ids(user_id, "request")
    else:
        session_sent_ids = set()
    # ----------------------

    lock = asyncio.Lock()

    async def _worker(token_obj):
        token = token_obj["token"]
        name = token_status[token]["name"]
        empty_batches = 0
        
        async with aiohttp.ClientSession() as session:
            while state["running"]:
                try:
                    # --- APPLY FILTERS ---
                    if is_request_filter_enabled(user_id):
                        await apply_filter_for_account(token, user_id)
                        await asyncio.sleep(1)
                    # ---------------------

                    users = await fetch_users(session, token, user_id)
                    
                    if users is None:
                        token_status[token]["status"] = "Invalid (401)"
                        return
                    
                    if not users or len(users) < 5:
                        empty_batches += 1
                        token_status[token]["status"] = f"Waiting ({empty_batches}/10)"
                        await asyncio.sleep(EMPTY_BATCH_DELAY)
                        if empty_batches >= 10:
                            token_status[token]["status"] = "No users"
                            return
                        continue
                    
                    empty_batches = 0
                    token_status[token]["status"] = "Processing"
                    
                    limit_reached, batch_added, batch_filtered = await process_users(session, users, token, user_id, bot, name, session_sent_ids, lock)
                    
                    token_status[token]["added"] += batch_added
                    token_status[token]["filtered"] += batch_filtered
                    
                    if limit_reached:
                        token_status[token]["status"] = "Limit Full"
                        return
                        
                    await asyncio.sleep(PER_BATCH_DELAY)

                except Exception as e:
                    logging.error(f"Error processing {name}: {e}")
                    token_status[token]["status"] = "Retrying..."
                    await asyncio.sleep(PER_ERROR_DELAY)
        
        token_status[token]["status"] = "Stopped"

    async def _refresh_ui():
        last_message = ""
        while state["running"]:
            total_added_now = sum(status["added"] for status in token_status.values())
            header = f"🔄 <b>AIO Requests</b> | <b>Added:</b> {total_added_now}"
            
            lines = [header, "", "<pre>Account   │Added │Filter│Status      </pre>"]
            for status in token_status.values():
                name = status["name"]
                display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
                lines.append(f"<pre>{display} │{status['added']:>5} │{status['filtered']:>6}│{status['status']:<10}</pre>")

            current_message = "\n".join(lines)
            if current_message != last_message:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id, message_id=state["status_message_id"],
                        text=current_message, parse_mode="HTML", reply_markup=stop_markup
                    )
                    last_message = current_message
                except Exception as e:
                    if "message is not modified" not in str(e):
                        logging.error(f"Status update failed: {e}")
            await asyncio.sleep(1)

    ui_task = asyncio.create_task(_refresh_ui())
    worker_tasks = [asyncio.create_task(_worker(token_obj)) for token_obj in tokens]
    await asyncio.gather(*worker_tasks, return_exceptions=True)

    state["running"] = False
    await asyncio.sleep(1.1)
    ui_task.cancel()
    if state.get("pinned_message_id"):
        try: await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        except Exception: pass

    total_added = sum(status["added"] for status in token_status.values())
    completion_status = "⚠️ Process Stopped" if state.get("stopped") else "✅ AIO Requests Completed"
    final_header = f"<b>{completion_status}</b> | <b>Total Added:</b> {total_added}"
    
    final_lines = [final_header, "", "<pre>Account   │Added │Filter│Status      </pre>"]
    for status in token_status.values():
        name = status["name"]
        display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
        final_lines.append(f"<pre>{display} │{status['added']:>5} │{status['filtered']:>6}│{status['status']}</pre>")

    await bot.edit_message_text(
        chat_id=user_id, message_id=state["status_message_id"],
        text="\n".join(final_lines), parse_mode="HTML"
    )
