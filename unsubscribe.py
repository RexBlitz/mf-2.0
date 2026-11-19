import asyncio
import aiohttp
import logging
from meeff_http import create_meeff_session
from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest
from device_info import get_or_create_device_info_for_token, get_headers_with_device_info


UNSUBSCRIBE_URL = "https://api.meeff.com/chatroom/unsubscribe/v1"
CHATROOM_URL = "https://api.meeff.com/chatroom/dashboard/v1"
MORE_CHATROOMS_URL = "https://api.meeff.com/chatroom/more/v1"
BASE_HEADERS = {
    'User-Agent': "okhttp/4.12.0",
    'Accept-Encoding': "gzip",
    'content-type': "application/json; charset=utf-8"
}
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# --- Helper Functions ---

async def _fetch_chatroom_batch(session, token, from_date=None, user_id=None):
    """Fetches a single batch of chatrooms using a persistent session."""
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    if user_id:
        device_info = get_or_create_device_info_for_token(user_id, token)
        headers = get_headers_with_device_info(headers, device_info)

    try:
        if from_date:
            payload = {"fromDate": from_date, "locale": "en"}
            async with session.post(MORE_CHATROOMS_URL, json=payload, headers=headers, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch more chatrooms: {response.status}")
                    return [], None
                data = await response.json()
                return data.get("rooms", []), data.get("next")
        else:
            params = {'locale': "en"}
            async with session.get(CHATROOM_URL, params=params, headers=headers, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch initial chatrooms: {response.status}")
                    return [], None
                data = await response.json()
                return data.get("rooms", []), data.get("next")
    except Exception as e:
        logger.error(f"Exception while fetching chatrooms: {e}")
        return [], None


async def _unsubscribe_from_room(session, token, chatroom_id, user_id=None):
    """Unsubscribes from a single chatroom using a persistent session."""
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    if user_id:
        device_info = get_or_create_device_info_for_token(user_id, token)
        headers = get_headers_with_device_info(headers, device_info)
    
    payload = {"chatRoomId": chatroom_id, "locale": "en"}
    try:
        async with session.post(UNSUBSCRIBE_URL, json=payload, headers=headers, timeout=10) as response:
            if response.status == 200:
                return True
            logger.warning(f"Failed to unsubscribe from {chatroom_id}: Status {response.status}")
            return False
    except Exception as e:
        logger.error(f"Exception while unsubscribing from {chatroom_id}: {e}")
        return False


# --- Main Function ---

async def unsubscribe_everyone(token: str, status_message: types.Message = None, bot: Bot = None, chat_id: int = None, user_id: int = None):
    """
    Efficiently unsubscribes from all chatrooms by processing batches concurrently.
    """
    total_unsubscribed = 0
    from_date = None
    running = True

    async def _update_ui():
        """A background task to safely update the Telegram message once per second."""
        last_update_text = ""
        while running:
            current_text = f"🔄 Unsubscribing... Total: {total_unsubscribed}"
            if current_text != last_update_text:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_message.message_id,
                        text=current_text
                    )
                    last_update_text = current_text
                except TelegramBadRequest as e:
                    if "message is not modified" not in str(e):
                        logger.error(f"UI update failed: {e}")
            await asyncio.sleep(1)

    ui_task = None
    if bot and chat_id and status_message:
        ui_task = asyncio.create_task(_update_ui())

    try:
        async with create_meeff_session() as session:
            while True:
                chatrooms, next_from_date = await _fetch_chatroom_batch(session, token, from_date, user_id)
                if not chatrooms:
                    logger.info("No more chatrooms found.")
                    break

                # Create and run unsubscribe tasks concurrently for the entire batch
                tasks = [_unsubscribe_from_room(session, token, room["_id"], user_id) for room in chatrooms]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Count successes
                batch_success_count = sum(1 for result in results if result is True)
                total_unsubscribed += batch_success_count
                logger.info(f"Unsubscribed from {batch_success_count} chatrooms in this batch.")

                if not next_from_date:
                    break
                from_date = next_from_date
                await asyncio.sleep(0.5) # Small delay between fetching batches

    finally:
        # Cleanly stop the UI task
        running = False
        if ui_task:
            await asyncio.sleep(1.1) # Allow for a final update
            ui_task.cancel()

        final_message = f"✅ Finished unsubscribing. Total: {total_unsubscribed}"
        logger.info(final_message)
        if bot and chat_id and status_message:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message.message_id,
                    text=final_message
                )
            except TelegramBadRequest:
                pass # It might have already been updated
