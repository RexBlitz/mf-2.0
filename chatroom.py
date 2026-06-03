import aiohttp
import asyncio
import logging
import html
from aiogram import Bot, types
from typing import List, Dict, Set
from db import is_already_sent, bulk_add_sent_ids

# --- Constants ---
CHATROOM_URL = "https://api.meeff.com/chatroom/dashboard/v1"
MORE_CHATROOMS_URL = "https://api.meeff.com/chatroom/more/v1"
SEND_MESSAGE_URL = "https://api.meeff.com/chat/send/v2"
BASE_HEADERS = {
    # Using the updated User-Agent for better compatibility
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
    'content-type': "application/json; charset=utf-8"
}
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# --- Core API Functions (with Session Management) ---

async def fetch_chatrooms(session: aiohttp.ClientSession, token: str, from_date: str = None, user_id: int = None) -> tuple[List[Dict], str | None]:
    """Fetches the initial list of chatrooms using a provided session and simplified headers."""
    url = CHATROOM_URL if not from_date else MORE_CHATROOMS_URL
    params = {'locale': "en"}
    if from_date:
        params['fromDate'] = from_date
    
    # --- SIMPLIFIED HEADERS ---
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    # --------------------------
    
    try:
        if from_date:
            # Note: The MORE_CHATROOMS_URL endpoint uses a POST request with JSON payload
            async with session.post(url, json=params, headers=headers, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch more chatrooms: {response.status}")
                    return [], None
                data = await response.json()
                return data.get("rooms", []), data.get("next")
        else:
            # The initial CHATROOM_URL endpoint uses a GET request with query params
            async with session.get(url, params=params, headers=headers, timeout=10) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch chatrooms: {response.status}")
                    return [], None
                data = await response.json()
                return data.get("rooms", []), data.get("next")
    except Exception as e:
        logger.error(f"Error fetching chatrooms: {e}")
        return [], None


async def send_single_message(session: aiohttp.ClientSession, token: str, chatroom_id: str, message: str, user_id: int = None) -> bool:
    """Sends a single message to a chatroom using a provided session and simplified headers."""
    
    # --- SIMPLIFIED HEADERS ---
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    # --------------------------
    
    payload = {"chatRoomId": chatroom_id, "message": message, "locale": "en"}
    try:
        async with session.post(SEND_MESSAGE_URL, json=payload, headers=headers, timeout=10) as response:
            if response.status == 200:
                return True
            logger.error(f"Failed to send message to {chatroom_id}: {response.status}")
            return False
    except Exception as e:
        logger.error(f"Error sending message to {chatroom_id}: {e}")
        return False


async def send_message(session: aiohttp.ClientSession, token: str, chatroom_id: str, message: str, user_id: int = None) -> bool:
    """Sends message(s) to a single chatroom, splitting by comma if needed."""
    # Split message by commas and clean up whitespace
    message_parts = [part.strip() for part in message.split(',') if part.strip()]
    
    # If no commas or only one part, send as single message
    if len(message_parts) <= 1:
        return await send_single_message(session, token, chatroom_id, message, user_id)
    
    # Send each part as separate message
    all_successful = True
    for part in message_parts:
        # user_id is passed but unused inside send_single_message now
        success = await send_single_message(session, token, chatroom_id, part, user_id)
        if not success:
            all_successful = False
            # Continue sending other parts even if one fails
    
    return all_successful


# --- Processing Logic ---

async def process_chatroom_batch(
    session: aiohttp.ClientSession, token: str, rooms: List[Dict], message: str,
    chat_id: int, spam_enabled: bool, sent_ids: Set[str], sent_ids_lock: asyncio.Lock, user_id: int = None
) -> tuple[int, int, int]:
    """Processes a batch of chatrooms concurrently."""
    filtered_rooms = []
    if spam_enabled:
        async with sent_ids_lock:
            for room in rooms:
                if room.get('_id') not in sent_ids:
                    filtered_rooms.append(room)
    else:
        filtered_rooms = rooms

    # user_id is passed but unused inside send_message now
    tasks = [send_message(session, token, room.get('_id'), message, user_id) for room in filtered_rooms]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successful_sends = [room.get('_id') for room, result in zip(filtered_rooms, results) if result is True]
    
    if spam_enabled and successful_sends:
        async with sent_ids_lock:
            sent_ids.update(successful_sends)
        await bulk_add_sent_ids(chat_id, "chatroom", successful_sends)
        
    sent_count = len(successful_sends)
    filtered_count = len(rooms) - len(filtered_rooms)
    return len(rooms), sent_count, filtered_count


async def send_message_to_everyone(
    token: str, message: str, chat_id: int, spam_enabled: bool, user_id: int,
    sent_ids: Set[str], sent_ids_lock: asyncio.Lock, status_entry: Dict = None
) -> tuple[int, int, int]:
    """Main logic for sending messages for a single token."""
    total_rooms, sent_count, filtered_count = 0, 0, 0
    from_date = None
    
    async with aiohttp.ClientSession() as session:
        while True:
            # user_id is passed to fetch_chatrooms but is unused inside now
            rooms, next_from = await fetch_chatrooms(session, token, from_date, user_id)
            if not rooms:
                break

            batch_total, batch_sent, batch_filtered = await process_chatroom_batch(
                session, token, rooms, message, chat_id, spam_enabled, sent_ids, sent_ids_lock, user_id
            )
            total_rooms += batch_total
            sent_count += batch_sent
            filtered_count += batch_filtered
            
            if status_entry:
                status_entry.update({'rooms': total_rooms, 'sent': sent_count, 'filtered': filtered_count})

            if not next_from:
                break
            from_date = next_from
            
    return total_rooms, sent_count, filtered_count


# --- AIO (All-In-One) Function for Multiple Tokens ---

async def send_message_to_everyone_all_tokens(
    tokens: List[str], message: str, status_message: types.Message, bot: Bot,
    chat_id: int, spam_enabled: bool, token_names: Dict[str, str],
    use_in_memory_deduplication: bool, user_id: int
) -> None:
    """Sends messages for multiple tokens concurrently with a reliable UI."""
    token_status = {}
    
    # Shared state for all workers to prevent race conditions
    sent_ids = await is_already_sent(chat_id, "chatroom", None, bulk=True) if use_in_memory_deduplication and spam_enabled else set()
    sent_ids_lock = asyncio.Lock()
    running = True

    async def _worker(token: str):
        display_name = token_names.get(token, token[:6])
        status_entry = {'name': display_name, 'rooms': 0, 'sent': 0, 'filtered': 0, 'status': "Processing"}
        token_status[token] = status_entry

        try:
            # user_id is passed but unused inside the API wrappers now
            await send_message_to_everyone(
                token, message, chat_id, spam_enabled, user_id,
                sent_ids, sent_ids_lock, status_entry
            )
            status_entry['status'] = "Done"
        except Exception as e:
            logger.error(f"[{display_name}] worker failed: {e}")
            status_entry['status'] = f"Failed: {str(e)[:20]}..."

    async def _refresh_ui():
        last_message = ""
        while running:
            # Show message parts count in the header if comma-separated
            message_parts = [part.strip() for part in message.split(',') if part.strip()]
            header = "🔄 <b>Chatroom AIO Status</b>"
            if len(message_parts) > 1:
                header += f" ({len(message_parts)} messages per room)"
            
            lines = [header, "<pre>Account   │Rooms │Sent  │Filter│Status</pre>"]
            for status in token_status.values():
                name = status.get('name', 'N/A')
                display_name = name[:10].ljust(10) if len(name) <= 10 else name[:9] + '…'
                lines.append(f"<pre>{display_name}│{status.get('rooms', 0):>5} │{status.get('sent', 0):>5} │{status.get('filtered', 0):>6}│{status.get('status', 'Queued')}</pre>")
            
            current_message = "\n".join(lines)
            if current_message != last_message:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=status_message.message_id,
                        text=current_message, parse_mode="HTML"
                    )
                    last_message = current_message
                except Exception as e:
                    if "message is not modified" not in str(e):
                        logger.error(f"UI refresh error: {e}")
            await asyncio.sleep(1)

    # Start UI and worker tasks
    # NOTE: Initialization of token_status moved into _worker for dynamic setup
    worker_tasks = [asyncio.create_task(_worker(token)) for token in tokens]
    # We start the UI refresh AFTER the workers are initialized to populate token_status
    await asyncio.sleep(0.1) 
    ui_task = asyncio.create_task(_refresh_ui())
    await asyncio.gather(*worker_tasks)

    # Clean up UI task
    running = False
    await asyncio.sleep(1.1) # Allow for a final UI update
    ui_task.cancel()

    # Final Summary
    successful_tokens = sum(1 for s in token_status.values() if s['status'] == 'Done')
    success_rate = (successful_tokens / len(tokens)) * 100 if tokens else 0
    emoji = "✅" if success_rate > 90 else "⚠️" if success_rate > 70 else "❌"
    
    # Show message parts count in final summary
    message_parts = [part.strip() for part in message.split(',') if part.strip()]
    total_users_sent = sum(s.get("sent", 0) for s in token_status.values())
    total_rooms = sum(s.get("rooms", 0) for s in token_status.values())
    header = f"{emoji} <b>Chatroom AIO Completed</b> - {successful_tokens}/{len(tokens)} | Total Sent: {total_users_sent} (Rooms: {total_rooms})"
    
    final_lines = [header, "<pre>Account   │Rooms │Sent  │Filter│Status</pre>"]
    for status in token_status.values():
        name = status.get('name', 'N/A')
        display_name = name[:10].ljust(10) if len(name) <= 10 else name[:9] + '…'
        final_lines.append(f"<pre>{display_name}│{status.get('rooms', 0):>5} │{status.get('sent', 0):>5} │{status.get('filtered', 0):>6}│{status.get('status', 'Done')}</pre>")

    await bot.edit_message_text(chat_id=chat_id, message_id=status_message.message_id, text="\n".join(final_lines), parse_mode="HTML")
