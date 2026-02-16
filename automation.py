"""
Automation Module for Meeff Bot
- Auto send friend requests every 24 hours
- Auto send lounge messages after adding (20min, 1hr, 3hr)
- Auto send chatroom messages 5 min after each lounge message
"""

import asyncio
import logging
import html
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiohttp

from db import (
    get_automation_settings, set_automation_enabled,
    get_active_tokens, get_tokens, get_user_filters,
    get_individual_spam_filter, is_already_sent, add_sent_id,
    add_automation_log, get_automation_pending_followups,
    set_automation_last_request_time, get_automation_last_request_time,
    set_automation_add_time, mark_lounge_sent, get_lounge_sent_waves,
    mark_chatroom_sent, is_chatroom_sent,
)

logger = logging.getLogger(__name__)

# Store running automation tasks per user
automation_tasks: Dict[int, asyncio.Task] = {}

# Intervals
REQUEST_INTERVAL = timedelta(hours=24)
LOUNGE_WAVE_1 = timedelta(minutes=20)
LOUNGE_WAVE_2 = timedelta(hours=1)
LOUNGE_WAVE_3 = timedelta(hours=3)
CHATROOM_AFTER_LOUNGE = timedelta(minutes=5)

# -------------------------------------------------------------------
# Helper: send a single friend request
# -------------------------------------------------------------------
async def _send_friend_request(session: aiohttp.ClientSession, token: str, person_id: str) -> bool:
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    headers = {"meeff-access-token": token, "Connection": "keep-alive", "User-Agent": "okhttp/5.0.0-alpha.14"}
    try:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if data.get("errorCode") == "LikeExceeded":
                return False  # daily limit
            if data.get("errorCode"):
                return False
            return True
    except Exception as e:
        logger.error(f"[Automation] Friend request error: {e}")
        return False


# -------------------------------------------------------------------
# Helper: discover new users (same as run_requests logic)
# -------------------------------------------------------------------
async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[str]:
    """Fetch a page of recommended users and return their IDs."""
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {"meeff-access-token": token, "Connection": "keep-alive", "User-Agent": "okhttp/5.0.0-alpha.14"}
    params = {"locale": "en"}
    if filters:
        if filters.get("filterNationalityCode"):
            params["filterNationalityCode"] = filters["filterNationalityCode"]

    try:
        async with session.get(url, headers=headers, params=params) as resp:
            data = await resp.json(content_type=None)
            if data.get("errorCode"):
                return []
            users = data.get("result", {}).get("users", [])
            return [u.get("_id") for u in users if u.get("_id")]
    except Exception as e:
        logger.error(f"[Automation] Discover error: {e}")
        return []


# -------------------------------------------------------------------
# Helper: send lounge message to a person
# -------------------------------------------------------------------
async def _send_lounge_to_person(session: aiohttp.ClientSession, token: str, person_id: str, message: str) -> bool:
    url = "https://api.meeff.com/lounge/create/v1/"
    headers = {"meeff-access-token": token, "Content-Type": "application/json", "User-Agent": "okhttp/5.0.0-alpha.14"}
    payload = {"targetUserId": person_id, "content": message, "locale": "en"}
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json(content_type=None)
            return not data.get("errorCode")
    except Exception as e:
        logger.error(f"[Automation] Lounge send error: {e}")
        return False


# -------------------------------------------------------------------
# Helper: send chatroom message to a person
# -------------------------------------------------------------------
async def _send_chatroom_to_person(session: aiohttp.ClientSession, token: str, person_id: str, message: str) -> bool:
    url = "https://api.meeff.com/chatroom/send/v1/"
    headers = {"meeff-access-token": token, "Content-Type": "application/json", "User-Agent": "okhttp/5.0.0-alpha.14"}
    payload = {"targetUserId": person_id, "content": message, "locale": "en"}
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json(content_type=None)
            return not data.get("errorCode")
    except Exception as e:
        logger.error(f"[Automation] Chatroom send error: {e}")
        return False


# -------------------------------------------------------------------
# Main automation loop (per user)
# -------------------------------------------------------------------
async def automation_loop(user_id: int, bot):
    """
    Main loop that runs continuously while automation is enabled.
    Checks every 60 seconds what needs to be done.
    """
    logger.info(f"[Automation] Started for user {user_id}")
    await add_automation_log(user_id, "Automation started")

    try:
        while True:
            settings = await get_automation_settings(user_id)
            if not settings["enabled"]:
                logger.info(f"[Automation] Disabled for user {user_id}, stopping loop.")
                break

            lounge_msg = settings.get("lounge_message", "")
            chatroom_msg = settings.get("chatroom_message", "")
            selected = settings.get("selected_accounts", "all")

            # Get tokens based on selection
            if selected == "all":
                token_list = await get_active_tokens(user_id)
            else:
                all_tokens = await get_tokens(user_id)
                token_list = []
                for idx in selected:
                    if isinstance(idx, int) and 0 <= idx < len(all_tokens):
                        tok = all_tokens[idx]
                        if tok.get("active", True):
                            token_list.append(tok)

            now = datetime.utcnow()

            async with aiohttp.ClientSession() as session:
                for token_obj in token_list:
                    token = token_obj["token"]
                    token_name = token_obj.get("name", "Unknown")

                    # ---- STEP 1: Auto friend requests every 24h ----
                    last_request = await get_automation_last_request_time(user_id, token)
                    should_request = False
                    if last_request is None:
                        should_request = True
                    elif isinstance(last_request, datetime) and (now - last_request) >= REQUEST_INTERVAL:
                        should_request = True

                    if should_request:
                        filters = await get_user_filters(user_id, token) or {}
                        user_ids = await _discover_users(session, token, filters)
                        sent_count = 0
                        added_ids = []

                        for pid in user_ids:
                            success = await _send_friend_request(session, token, pid)
                            if success:
                                sent_count += 1
                                added_ids.append(pid)
                                # Record add time for lounge follow-up
                                await set_automation_add_time(user_id, token, pid)
                            else:
                                break  # likely hit daily limit
                            await asyncio.sleep(1)  # rate limiting

                        await set_automation_last_request_time(user_id, token)
                        if sent_count > 0:
                            await add_automation_log(user_id, f"[{token_name}] Sent {sent_count} friend requests")
                        logger.info(f"[Automation] {token_name}: Sent {sent_count} requests")

                    # ---- STEP 2: Lounge messages (20min, 1hr, 3hr after adding) ----
                    if lounge_msg:
                        followups = await get_automation_pending_followups(user_id)
                        add_times = followups.get("add_times", {}).get(token, {})

                        for person_id, add_time in add_times.items():
                            if not isinstance(add_time, datetime):
                                continue

                            elapsed = now - add_time
                            waves_sent = await get_lounge_sent_waves(user_id, token, person_id)

                            # Wave 1: 20 minutes after add
                            if elapsed >= LOUNGE_WAVE_1 and "wave_1" not in waves_sent:
                                ok = await _send_lounge_to_person(session, token, person_id, lounge_msg)
                                if ok:
                                    await mark_lounge_sent(user_id, token, person_id, 1)
                                    await add_automation_log(user_id, f"[{token_name}] Lounge wave 1 -> {person_id[:8]}...")

                                    # Schedule chatroom 5 min later
                                    if chatroom_msg:
                                        asyncio.create_task(
                                            _delayed_chatroom(user_id, token, token_name, person_id, chatroom_msg, session)
                                        )

                            # Wave 2: 1 hour after add
                            elif elapsed >= LOUNGE_WAVE_2 and "wave_1" in waves_sent and "wave_2" not in waves_sent:
                                ok = await _send_lounge_to_person(session, token, person_id, lounge_msg)
                                if ok:
                                    await mark_lounge_sent(user_id, token, person_id, 2)
                                    await add_automation_log(user_id, f"[{token_name}] Lounge wave 2 -> {person_id[:8]}...")

                                    if chatroom_msg:
                                        asyncio.create_task(
                                            _delayed_chatroom(user_id, token, token_name, person_id, chatroom_msg, session)
                                        )

                            # Wave 3: 3 hours after add
                            elif elapsed >= LOUNGE_WAVE_3 and "wave_2" in waves_sent and "wave_3" not in waves_sent:
                                ok = await _send_lounge_to_person(session, token, person_id, lounge_msg)
                                if ok:
                                    await mark_lounge_sent(user_id, token, person_id, 3)
                                    await add_automation_log(user_id, f"[{token_name}] Lounge wave 3 -> {person_id[:8]}...")

                                    if chatroom_msg:
                                        asyncio.create_task(
                                            _delayed_chatroom(user_id, token, token_name, person_id, chatroom_msg, session)
                                        )

                    await asyncio.sleep(2)  # small delay between tokens

            # Sleep 60 seconds before next check cycle
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info(f"[Automation] Cancelled for user {user_id}")
        await add_automation_log(user_id, "Automation stopped")
    except Exception as e:
        logger.error(f"[Automation] Error for user {user_id}: {e}", exc_info=True)
        await add_automation_log(user_id, f"Automation error: {str(e)[:100]}")


async def _delayed_chatroom(user_id: int, token: str, token_name: str, person_id: str, message: str, session: aiohttp.ClientSession):
    """Wait 5 minutes after lounge, then send chatroom message."""
    try:
        await asyncio.sleep(CHATROOM_AFTER_LOUNGE.total_seconds())
        already = await is_chatroom_sent(user_id, token, person_id)
        if already:
            return

        # Need a new session since the parent one may be closed
        async with aiohttp.ClientSession() as new_session:
            ok = await _send_chatroom_to_person(new_session, token, person_id, message)
            if ok:
                await mark_chatroom_sent(user_id, token, person_id)
                await add_automation_log(user_id, f"[{token_name}] Chatroom -> {person_id[:8]}...")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[Automation] Delayed chatroom error: {e}")


# -------------------------------------------------------------------
# Start / Stop functions
# -------------------------------------------------------------------
def start_automation(user_id: int, bot):
    """Start the automation loop for a user."""
    if user_id in automation_tasks:
        task = automation_tasks[user_id]
        if not task.done():
            return False  # Already running
    task = asyncio.create_task(automation_loop(user_id, bot))
    automation_tasks[user_id] = task
    return True


def stop_automation(user_id: int):
    """Stop the automation loop for a user."""
    if user_id in automation_tasks:
        task = automation_tasks[user_id]
        if not task.done():
            task.cancel()
            del automation_tasks[user_id]
            return True
    return False


def is_automation_running(user_id: int) -> bool:
    """Check if automation is currently running for a user."""
    if user_id in automation_tasks:
        return not automation_tasks[user_id].done()
    return False
 
