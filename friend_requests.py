import asyncio
import aiohttp
import logging
import html
from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import get_individual_spam_filter, is_already_sent, add_sent_id, get_active_tokens, get_current_account, get_already_sent_ids # Import the new function
from collections import defaultdict
import time
from dateutil import parser
import random
# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ✅ Speed configuration 
PER_USER_DELAY = 2     # Delay between each user added
PER_BATCH_DELAY = 2       # Delay between batches
EMPTY_BATCH_DELAY = 2     # Delay after empty batch
PER_ERROR_DELAY = 5       # Delay after errors


# Global state variables for friend requests
user_states = defaultdict(lambda: {
    "running": False,
    "status_message_id": None,
    "pinned_message_id": None,
    "total_added_friends": 0,
    "batch_index": 0
})

# Inline keyboards for friend request operations
stop_markup = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Stop Requests", callback_data="stop")]
])

US_LOCATIONS = [
    {"lat": 40.7128, "lng": -74.0060},   # New York
    {"lat": 34.0522, "lng": -118.2437},  # Los Angeles
    {"lat": 41.8781, "lng": -87.6298},   # Chicago
    {"lat": 29.7604, "lng": -95.3698},   # Houston
    {"lat": 25.7617, "lng": -80.1918},   # Miami
]



async def fetch_users(session, token):
    """Fetch users from the API for friend requests"""
    loc = random.choice(US_LOCATIONS)  # pick a random US city each batch
    url = f"https://api.meeff.com/user/explore/v2?lng={loc['lng']}&lat={loc['lat']}&unreachableUserIds=&locale=en"
    headers = {"meeff-access-token": token, "Connection": "keep-alive"}
    try:
        async with session.get(url, headers=headers) as response:
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
        if not dt_str:
            return "N/A"
        try:
            dt = parser.isoparse(dt_str)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            diff = now - dt
            minutes = int(diff.total_seconds() // 60)
            if minutes < 1:
                return "just now"
            elif minutes < 60:
                return f"{minutes} min ago"
            hours = minutes // 60
            if hours < 24:
                return f"{hours} hr ago"
            days = hours // 24
            return f"{days} day(s) ago"
        except Exception:
            return "unknown"
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
        f"<b>Last Active:</b> {last_active}\n"
        "Photos: " + ' '.join([f"<a href='{html.escape(url)}'>Photo</a>" for url in user.get('photoUrls', [])])
    )

def format_time_used(start_time, end_time):
    delta = end_time - start_time
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

async def process_users(session, users, token, user_id, bot, target_channel_id, token_name=None, token_status=None):
    """Process a batch of users and send friend requests.
    Works with both run_requests and process_all_tokens functions.
    
    Args:
        session: aiohttp client session
        users: list of user data to process
        token: meeff access token
        user_id: Telegram user ID
        bot: Telegram bot instance
        target_channel_id: Target channel ID
        token_name: Optional name of the token (for process_all_tokens)
        token_status: Optional token status dictionary (for process_all_tokens)
    
    Returns:
        A tuple of (limit_reached, added_count, filtered_count)
    """
    state = user_states[user_id]
    added_count = 0
    filtered_count = 0
    limit_reached = False
    
    # Get token name if not provided
    if not token_name:
        tokens = get_active_tokens(user_id)
        token_name = "Default Account"
        for token_obj in tokens:
            if token_obj["token"] == token:
                token_name = token_obj.get("name", "Default Account")
                break

    # Get already sent IDs if spam filter is enabled
    already_sent_ids = set()
    if get_individual_spam_filter(user_id, "request"):
        already_sent_ids = get_already_sent_ids(user_id, "request")

    for user in users:
        if not state["running"]:
            break

        # Skip if already sent and spam filter is enabled
        if get_individual_spam_filter(user_id, "request") and user["_id"] in already_sent_ids:
            filtered_count += 1
            
            # Update token status if provided
            if token_status and token_name in token_status:
                current = token_status[token_name]
                token_status[token_name] = (current[0], current[1] + 1, current[2])
                
            continue

        # Send friend request
        url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={user['_id']}&isOkay=1"
        headers = {"meeff-access-token": token, "Connection": "keep-alive"}

        try:
            async with session.get(url, headers=headers) as response:
                data = await response.json()

                if data.get("errorCode") == "LikeExceeded":
                    logging.info(f"Daily like limit reached for {token_name}.")
                    
                    if token_status and token_name in token_status:
                        token_status[token_name] = (token_status[token_name][0], token_status[token_name][1], "Limit Full")
                    else:
                        await bot.edit_message_text(
                            chat_id=user_id,
                            message_id=state["status_message_id"],
                            text=f"{token_name}: Daily limit reached. Total Added Friends: {state['total_added_friends']}. Try again tomorrow.",
                            reply_markup=None
                        )
                    limit_reached = True
                    break

                # Add to sent IDs if spam filter is enabled
                if get_individual_spam_filter(user_id, "request"):
                    add_sent_id(user_id, "request", user["_id"])

                # Format and send user details
                details = format_user(user) # CHANGED THIS LINE
                await bot.send_message(chat_id=user_id, text=details, parse_mode="HTML")
                
                # Update counters
                added_count += 1
                state["total_added_friends"] += 1

                # Update status message based on which function called this
                if token_status and token_name in token_status:
                    # For process_all_tokens
                    current = token_status[token_name]
                    token_status[token_name] = (current[0] + 1, current[1], "Processing")
                else:
                    # For run_requests
                    if state["running"] and state["status_message_id"]:
                        try:
                            await bot.edit_message_text(
                                chat_id=user_id,
                                message_id=state["status_message_id"],
                                text=f"{token_name}: Friend request sending: {state['total_added_friends']}",
                                reply_markup=stop_markup
                            )
                        except Exception as e:
                            # Ignore "message is not modified" errors
                            if "message is not modified" not in str(e):
                                logging.error(f"Error updating status message: {e}")

                # Apply delay after processing each user
                await asyncio.sleep(PER_USER_DELAY)
                
        except Exception as e:
            logging.error(f"Error processing user with {token_name}: {e}")
            await asyncio.sleep(1)  # Short delay after error

    return limit_reached, added_count, filtered_count


async def run_requests(user_id, bot, target_channel_id):
    """Main function to run the request process for a single token"""
    state = user_states[user_id]
    state["total_added_friends"] = 0  # Reset counter
    state["batch_index"] = 0
    state["running"] = True
    
    async with aiohttp.ClientSession() as session:
        while state["running"]:
            try:
                # Get current token
                token = get_current_account(user_id)
                if not token:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["status_message_id"],
                        text="No active account found. Please set an account before starting requests.",
                        reply_markup=None
                    )
                    state["running"] = False
                    return

                # Get token name
                tokens = get_active_tokens(user_id)
                token_name = "Default Account"
                for token_obj in tokens:
                    if token_obj["token"] == token:
                        token_name = token_obj.get("name", "Default Account")
                        break

                # Update status with token name - always try to update for live feedback
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["status_message_id"],
                        text=f"{token_name}: Friend request sending: {state['total_added_friends']}",
                        reply_markup=stop_markup
                    )
                except Exception as e:
                    # Ignore "message is not modified" errors
                    if "message is not modified" not in str(e):
                        logging.error(f"Error updating status message: {e}")

                # Fetch users
                try:
                    users = await fetch_users(session, token)
                    state["batch_index"] += 1
                    
                    if not users or len(users) == 0:
                        logging.info(f"No users found for batch {state['batch_index']}. Trying again...")
                        await asyncio.sleep(EMPTY_BATCH_DELAY)  # Wait a bit before trying again
                        
                        # After several attempts with no users, we might need to stop
                        if state["batch_index"] > 10:  # Try up to 10 empty batches before giving up
                            try:
                                await bot.edit_message_text(
                                    chat_id=user_id,
                                    message_id=state["status_message_id"],
                                    text=f"{token_name}: No more users found. Total Added: {state['total_added_friends']}",
                                    reply_markup=None
                                )
                            except Exception as e:
                                # Ignore "message is not modified" errors
                                if "message is not modified" not in str(e):
                                    logging.error(f"Error updating status message: {e}")
                            state["running"] = False
                            break
                        continue
                    
                    # Process users - calling the shared function without token_status
                    limit_reached, _, _ = await process_users(session, users, token, user_id, bot, target_channel_id)
                    if limit_reached:
                        # Rate limit reached
                        state["running"] = False
                        break
                        
                except Exception as e:
                    logging.error(f"Error fetching users: {e}")
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"Error fetching users: {str(e)[:200]}. Trying again..."
                    )
                    await asyncio.sleep(PER_ERROR_DELAY)  # Wait a bit longer after an error
                    continue
                        
                await asyncio.sleep(PER_BATCH_DELAY)  # Wait between batches
                
            except Exception as e:
                logging.error(f"Error during processing: {e}")
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["status_message_id"],
                        text=f"An error occurred: {str(e)[:200]}. Attempting to continue...",
                        reply_markup=stop_markup
                    )
                except Exception as edit_error:
                    # Ignore "message is not modified" errors
                    if "message is not modified" not in str(edit_error):
                        logging.error(f"Error updating status message: {edit_error}")
                await asyncio.sleep(PER_ERROR_DELAY)  # Wait a bit before continuing after an error
                 
        # Always try to unpin when finished
        try:
            if state.get("pinned_message_id"):
                await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
                state["pinned_message_id"] = None
        except Exception as e:
            logging.error(f"Failed to unpin message: {e}")
            
        # Send completion message
        await bot.send_message(
            user_id,
            f"✅ All done!\nTotal Added: {state.get('total_added_friends', 0)}"
        )


async def process_all_tokens(user_id, tokens, bot, target_channel_id):
    """Process friend requests for all tokens concurrently"""
    state = user_states[user_id]
    state["total_added_friends"] = 0
    state["running"] = True
    state["stopped"] = False

    # Initialize status message
    if not state.get("status_message_id"):
        status_message = await bot.send_message(
            chat_id=user_id,
            text="🔄 <b>Friend Requests AIO Starting</b>",
            parse_mode="HTML",
            reply_markup=stop_markup
        )
        state["status_message_id"] = status_message.message_id
        try:
            await bot.pin_chat_message(chat_id=user_id, message_id=status_message.message_id, disable_notification=True)
            state["pinned_message_id"] = status_message.message_id
        except Exception as e:
            logging.error(f"Failed to pin message: {e}")

    token_status = {}

    async def _worker(token_obj, idx):
        name = token_obj.get("name", f"Account {idx}")
        token = token_obj["token"]
        added_count = 0
        filtered_count = 0
        empty_batches = 0
        status = "Processing"

        token_status[name] = (added_count, filtered_count, status)

        try:
            async with aiohttp.ClientSession() as session:
                while state["running"]:
                    try:
                        users = await fetch_users(session, token)
                        
                        if users is None:
                            token_status[name] = (added_count, filtered_count, "Rate limited")
                            return added_count
                            
                        if not users or len(users) < 5:
                            empty_batches += 1
                            token_status[name] = (added_count, filtered_count, f"Waiting ({empty_batches}/10)")
                            await asyncio.sleep(EMPTY_BATCH_DELAY)
                            if empty_batches >= 10:
                                token_status[name] = (added_count, filtered_count, "No users")
                                return added_count
                            continue
                        
                        empty_batches = 0
                        token_status[name] = (added_count, filtered_count, "Processing")
                        
                        # Use the shared process_users function with token_status
                        limit_reached, batch_added, batch_filtered = await process_users(
                            session, users, token, user_id, bot, target_channel_id, 
                            token_name=name, token_status=token_status
                        )
                        
                        added_count += batch_added
                        filtered_count += batch_filtered
                        
                        if limit_reached:
                            return added_count
                            
                        await asyncio.sleep(PER_BATCH_DELAY)

                    except Exception as e:
                        logging.error(f"Error processing {name}: {e}")
                        token_status[name] = (added_count, filtered_count, "Retrying...")
                        await asyncio.sleep(PER_ERROR_DELAY)

                token_status[name] = (added_count, filtered_count, "Stopped")
                return added_count

        except Exception as e:
            logging.error(f"Worker failed for {name}: {e}")
            token_status[name] = (added_count, filtered_count, f"Failed: {str(e)[:20]}...")
            return added_count

    async def _refresh_ui():
        last_message = ""
        update_count = 0
        update_interval = 1  # Update every 1 second
        force_update_interval = 3  # Force update every 3 iterations

        while state["running"]:
            try:
                # Simplified header format
                header = f"🔄 <b>AIO  Requests </b> | <b> Added:</b> {state['total_added_friends']}"
                
                lines = [
                    header,
                    "",  # Empty line after header
                    "<pre>Account   │Added │Filter│Status     </pre>"
                ]

                for name, (added, filtered, status) in token_status.items():
                    display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
                    lines.append(f"<pre>{display} │{added:>5} │{filtered:>6}│{status:>10}</pre>")

                spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                spinner = spinners[update_count % len(spinners)]
                lines.append(f"\n{spinner} <i>Processing...</i>")

                current_message = "\n".join(lines)
                update_count += 1
                force_update = (update_count % force_update_interval == 0)

                if current_message != last_message or force_update:
                    try:
                        await bot.edit_message_text(
                            chat_id=user_id,
                            message_id=state["status_message_id"],
                            text=current_message,
                            parse_mode="HTML",
                            reply_markup=stop_markup
                        )
                        last_message = current_message
                    except Exception as e:
                        if "message is not modified" not in str(e):
                            logging.error(f"Status update failed: {e}")

            except Exception as e:
                logging.error(f"UI updater exception: {e}")

            await asyncio.sleep(update_interval)

    # Initialize status for each token
    for idx, token_obj in enumerate(tokens, 1):
        name = token_obj.get("name", f"Account {idx}")
        token_status[name] = (0, 0, "Queued")

    # Show initial table before starting workers
    # Simplified initial header
    initial_header = "🔄 <b>Friend Requests Status</b> | <b>Total Added:</b> 0"
    
    initial_lines = [
        initial_header,
        "",  # Empty line after header
        "<pre>Account   │Added │Filter│Status</pre>"
    ]
    
    for name, (added, filtered, status) in token_status.items():
        display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
        initial_lines.append(f"<pre>{display} │{added:>5} │{filtered:>6}│{status:>10}</pre>")
    
    await bot.edit_message_text(
        chat_id=user_id,
        message_id=state["status_message_id"],
        text="\n".join(initial_lines),
        parse_mode="HTML",
        reply_markup=stop_markup
    )

    # Start UI updater and workers
    ui_task = asyncio.create_task(_refresh_ui())
    worker_tasks = [asyncio.create_task(_worker(token_obj, idx)) for idx, token_obj in enumerate(tokens, 1)]
    results = await asyncio.gather(*worker_tasks, return_exceptions=True)

    # Set state to not running
    state["running"] = False
    
    # This ensures we keep track of whether the process was stopped by user
    was_stopped = state.get("stopped", False)
    
    await asyncio.sleep(1)
    ui_task.cancel()
    try:
        await ui_task
    except asyncio.CancelledError:
        pass

    # Unpin message
    try:
        if state.get("pinned_message_id"):
            await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
            state["pinned_message_id"] = None
    except Exception as e:
        logging.error(f"Unpin failed: {e}")

    # Final status
    total_added = sum(result for result in results if isinstance(result, int))
    total_filtered = sum(filtered for _, (added, filtered, _) in token_status.items())
    
    # Check if process was stopped by user
    was_stopped = state.get("stopped", False)
    
    # Set completion message based on whether process was stopped
    if was_stopped:
        completion_status = "⚠️ Process Stopped"
        final_message = "⚠️ Process stopped!"
    else:
        completion_status = "✅AIO Friend Requests Completed"
        final_message = "✅ AIO Friend requests completed!"
    
    # Simplified final header
    final_header = f"<b>{completion_status}</b> | <b>Total Added:</b> {total_added}"
    
    final_lines = [
        final_header,
        "",  # Empty line after header
        "<pre>Account   │Added │Filter│Status      </pre>"
    ]
    
    for name, (added, filtered, status) in token_status.items():
        display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
        final_lines.append(f"<pre>{display} │{added:>5} │{filtered:>6}│{status}</pre>")

    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state["status_message_id"],
            text="\n".join(final_lines),
            parse_mode="HTML"
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            logging.error(f"Final status update failed: {e}")

    # Send final message to user with explicit check of stopped state
    if state.get("stopped", False):
        await bot.send_message(
            user_id,
            f"⚠️ Process stopped!\nTotal Added: {total_added}\nTotal Filtered: {total_filtered}"
        )
    else:
        await bot.send_message(
            user_id,
            f"✅ AIO requests completed!\nTotal Added: {total_added}\nTotal Filtered: {total_filtered}"
        )
