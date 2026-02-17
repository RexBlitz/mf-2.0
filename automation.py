"""
Automation Module - True DB-Based Scheduling.
- Independent cycles per account (24h loop).
- Precise follow-up waves (20m, 1h, 6h) per user.
- Persistence across restarts.
"""

import asyncio
import aiohttp
import logging
import time
from datetime import datetime, timedelta
from typing import List, Dict

from db import (
    get_automation_settings, get_active_tokens, get_tokens, 
    get_user_filters, bulk_add_sent_ids, is_already_sent,
    get_blocked_users, add_automation_log, get_individual_spam_filter,
    set_automation_enabled, is_automation_running as db_is_running,
    # New DB functions for timing
    set_automation_last_request_time, get_automation_pending_followups,
    set_automation_add_time, mark_lounge_sent, mark_chatroom_sent,
    get_automation_last_request_time
)
from filters import apply_filter_for_account

logger = logging.getLogger(__name__)

# --- GLOBAL STATE ---
# We keep track of the main monitoring task
monitor_tasks: Dict[int, asyncio.Task] = {}
status_messages: Dict[int, object] = {}

# --- TIMINGS ---
PER_USER_DELAY = 0.5  # Fast speed
PER_BATCH_DELAY = 1

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

# --- API HELPERS ---

async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[Dict]:
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    params = {"lng": "71.9140141", "unreachableUserIds": "", "lat": "29.6264544", "locale": "en"}
    if filters and filters.get("filterNationalityCode"):
        params["filterNationalityCode"] = filters["filterNationalityCode"]
    try:
        async with session.get(url, headers=headers, params=params, timeout=10) as resp:
            return (await resp.json()).get("users", []) if resp.status == 200 else []
    except: return []

async def _send_friend_request(session, token, person_id):
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    try:
        async with session.get(url, headers={**BASE_HEADERS, 'meeff-access-token': token}, timeout=10) as resp:
            data = await resp.json()
            if data.get("errorCode") == "LikeExceeded": return "LIMIT"
            return "FAIL" if data.get("errorCode") else "OK"
    except: return "FAIL"

async def _send_msg(session, token, person_id, msg, type="lounge"):
    if not msg: return False
    url = "https://api.meeff.com/lounge/create/v1/" if type == "lounge" else "https://api.meeff.com/chat/send/v2"
    headers = {**BASE_HEADERS, 'meeff-access-token': token, 'Content-Type': "application/json"}
    
    try:
        if type == "chat":
            # Open Room First
            async with session.post("https://api.meeff.com/chatroom/open/v2", headers=headers, json={"waitingRoomId": person_id, "locale": "en"}, timeout=10) as resp:
                if resp.status == 412: return "DISABLED"
                if resp.status != 200: return False
                cid = (await resp.json()).get("chatRoom", {}).get("_id")
                if not cid: return False
            # Send
            payload = {"chatRoomId": cid, "message": msg, "locale": "en"}
        else:
            payload = {"targetUserId": person_id, "content": msg, "locale": "en"}

        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
            return resp.status == 200 or not (await resp.json()).get("errorCode")
    except: return False

# --- LOGIC HANDLERS ---

async def process_account_cycle(user_id: int, token_obj: dict, settings: dict, session: aiohttp.ClientSession, db_data: dict):
    """Checks checks if an account needs to run Requests or Follow-ups."""
    token = token_obj["token"]
    name = token_obj.get("name", "Account")[:10]
    
    # 1. CHECK REQUEST CYCLE (Every 24 Hours)
    last_req_str = db_data.get("request_times", {}).get(token)
    should_run_requests = False
    
    if not last_req_str:
        should_run_requests = True # Never ran
    else:
        last_req = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        if (datetime.utcnow() - last_req).total_seconds() > 24 * 3600:
            should_run_requests = True

    if should_run_requests:
        await add_automation_log(user_id, f"[{name}] Starting Daily Requests")
        await run_requests_task(user_id, token_obj, session)
        # Update DB timestamp immediately after finishing
        await set_automation_last_request_time(user_id, token)
        return # Priority to requests

    # 2. CHECK FOLLOW-UPS (Waves)
    # Get all users added by this token
    added_users = db_data.get("add_times", {}).get(token, {})
    lounge_history = db_data.get("lounge_sent", {}).get(token, {})
    
    # Configuration
    lounge_msg = settings.get("lounge_message")
    chat_msg = settings.get("chatroom_message")
    
    if not lounge_msg or not chat_msg: return

    now = datetime.utcnow()
    
    for pid, add_time_str in added_users.items():
        add_time = add_time_str if isinstance(add_time_str, datetime) else datetime.fromisoformat(str(add_time_str))
        elapsed_mins = (now - add_time).total_seconds() / 60
        
        user_history = lounge_history.get(pid, {})
        
        # WAVE 1: 20 Minutes
        if elapsed_mins >= 20 and "wave_1" not in user_history:
            if elapsed_mins < 120: # Expiry: don't send if older than 2 hours (stale)
                await execute_wave(user_id, token, pid, lounge_msg, chat_msg, session, 1)
        
        # WAVE 2: 1 Hour (60 mins)
        elif elapsed_mins >= 60 and "wave_2" not in user_history:
            if elapsed_mins < 300: # Expiry: 5 hours
                await execute_wave(user_id, token, pid, lounge_msg, chat_msg, session, 2)

        # WAVE 3: 6 Hours (360 mins)
        elif elapsed_mins >= 360 and "wave_3" not in user_history:
            if elapsed_mins < 1500: # Expiry: 25 hours
                await execute_wave(user_id, token, pid, lounge_msg, chat_msg, session, 3)

async def run_requests_task(user_id, token_obj, session):
    """Runs the friend request batch and saves added users to DB for follow-up."""
    token = token_obj["token"]
    
    # Check Spam Filter
    is_spam_on = await get_individual_spam_filter(user_id, "request")
    blocked = await get_blocked_users(user_id)
    sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_on else set()
    
    # Apply Filters
    await apply_filter_for_account(token, user_id)
    filters = await get_user_filters(user_id, token) or {}
    
    count_sent = 0
    ids_to_save = []
    
    # Simple loop: Fetch -> Send -> Repeat until Limit
    while True:
        users = await _discover_users(session, token, filters)
        if not users: break
        
        limit_hit = False
        for user in users:
            pid = user.get("_id")
            if pid in blocked or pid in sent_ids: continue
            
            res = await _send_friend_request(session, token, pid)
            if res == "LIMIT": 
                limit_hit = True; break
            
            if res == "OK":
                count_sent += 1
                sent_ids.add(pid)
                ids_to_save.append(pid)
                # CRITICAL: Save Add Time for Follow-ups
                await set_automation_add_time(user_id, token, pid)
                await asyncio.sleep(PER_USER_DELAY)
        
        if is_spam_on and ids_to_save:
            await bulk_add_sent_ids(user_id, "request", ids_to_save)
            ids_to_save = [] # Clear buffer
            
        if limit_hit: break
        await asyncio.sleep(1)

    await add_automation_log(user_id, f"[{token_obj.get('name')}] Sent {count_sent} requests")

async def execute_wave(user_id, token, pid, l_msg, c_msg, session, wave_num):
    """Sends Lounge AND Chat message for a specific wave."""
    # 1. Send Lounge
    if await _send_msg(session, token, pid, l_msg, "lounge"):
        await asyncio.sleep(2)
        # 2. Send Chat
        res = await _send_msg(session, token, pid, c_msg, "chat")
        
        # 3. Mark Complete in DB
        await mark_lounge_sent(user_id, token, pid, wave_num)
        
        if res != "DISABLED":
            await add_automation_log(user_id, f"Wave {wave_num} sent to ...{pid[-4:]}")

# --- MONITOR LOOP ---

async def monitor_loop(user_id: int):
    """Runs every minute to check if any account needs action."""
    logger.info(f"Automation Monitor Started for {user_id}")
    
    while True:
        try:
            # 1. Check if globally enabled
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"):
                break # Exit loop if disabled
            
            # 2. Get Targets
            selected = settings.get("selected_accounts", "all")
            all_tokens = await get_tokens(user_id)
            if selected == "all": target_tokens = all_tokens
            elif selected == "active_only": target_tokens = await get_active_tokens(user_id)
            else: target_tokens = [all_tokens[i] for i in selected if 0 <= i < len(all_tokens)]
            
            # 3. Fetch all timing data at once (Optimization)
            db_data = await get_automation_pending_followups(user_id)
            
            # 4. Update UI Status
            status_text = f"🔄 <b>Automation Active</b>\nAccounts: {len(target_tokens)}\n"
            
            async with aiohttp.ClientSession() as session:
                for token_obj in target_tokens:
                    # Calculate next request time for UI
                    last_req = db_data.get("request_times", {}).get(token_obj["token"])
                    next_run = "Now"
                    if last_req:
                        elapsed = (datetime.utcnow() - last_req).total_seconds()
                        remaining = (24*3600) - elapsed
                        if remaining > 0:
                            h, rem = divmod(remaining, 3600)
                            m, _ = divmod(rem, 60)
                            next_run = f"in {int(h)}h {int(m)}m"
                    
                    status_text += f"\n• {token_obj.get('name')}: {next_run}"
                    
                    # Run Logic
                    await process_account_cycle(user_id, token_obj, settings, session, db_data)
            
            # Update the message in Telegram
            msg = status_messages.get(user_id)
            if msg:
                try: await msg.edit_text(status_text, parse_mode="HTML")
                except: pass

            # Wait 60 seconds before next check
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor Loop Error: {e}")
            await asyncio.sleep(60)

# --- CONTROL ---

async def run_automation_action(user_id: int, status_msg):
    """Manual Trigger: Forces a check NOW."""
    status_messages[user_id] = status_msg
    await set_automation_enabled(user_id, True)
    
    # Restart the monitor if it's not running
    if user_id in monitor_tasks: monitor_tasks[user_id].cancel()
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id))

def start_automation(user_id: int, bot):
    """Called on bot startup or /start."""
    asyncio.create_task(set_automation_enabled(user_id, True))
    if user_id in monitor_tasks: monitor_tasks[user_id].cancel()
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id))

def stop_automation(user_id: int):
    asyncio.create_task(set_automation_enabled(user_id, False))
    if user_id in monitor_tasks:
        monitor_tasks[user_id].cancel()
        del monitor_tasks[user_id]
        if user_id in status_messages:
            asyncio.create_task(status_messages[user_id].edit_text("🛑 <b>Automation Stopped</b>", parse_mode="HTML"))

def is_automation_running(user_id: int) -> bool:
    return user_id in monitor_tasks
