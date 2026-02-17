"""
Automation Module - Sequential Processing with Daily UI Refresh.
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
    set_automation_enabled,
    set_automation_last_request_time, get_automation_pending_followups,
    set_automation_add_time, mark_lounge_sent, mark_chatroom_sent,
    get_automation_last_request_time
)
from filters import apply_filter_for_account

logger = logging.getLogger(__name__)

# --- GLOBAL STATE ---
monitor_task: asyncio.Task = None
status_messages: Dict[int, object] = {} # Stores the Message object
user_bots: Dict[int, object] = {}       # Stores the Bot object per user

# Stores the LIVE UI rows: { user_id: { token: "Account 1: Sent 5..." } }
ui_rows_state: Dict[int, Dict[str, str]] = {}
ui_totals_state: Dict[int, Dict[str, int]] = {}

# --- TIMINGS ---
PER_USER_DELAY = 0.5  # Fast
PER_BATCH_DELAY = 1

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

# --- API HELPERS (Same as before) ---

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
            async with session.post("https://api.meeff.com/chatroom/open/v2", headers=headers, json={"waitingRoomId": person_id, "locale": "en"}, timeout=10) as resp:
                if resp.status == 412: return "DISABLED"
                if resp.status != 200: return False
                cid = (await resp.json()).get("chatRoom", {}).get("_id")
                if not cid: return False
            payload = {"chatRoomId": cid, "message": msg, "locale": "en"}
        else:
            payload = {"targetUserId": person_id, "content": msg, "locale": "en"}

        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
            return resp.status == 200 or not (await resp.json()).get("errorCode")
    except: return False

# --- INTELLIGENT UI MANAGER ---

async def update_ui(user_id, force_new=False):
    """
    Updates the UI.
    If 'force_new' is True OR if editing fails (message deleted), it sends a NEW message.
    """
    rows = ui_rows_state.get(user_id, {})
    totals = ui_totals_state.get(user_id, {"sent": 0, "filtered": 0})
    
    text = "🔄 <b>Friend Request Automation</b>\n\n"
    for token, row_text in rows.items():
        text += f"{row_text}\n"
    text += f"\n-------\n<b>Total Sent: {totals['sent']}</b> | <b>Total Filtered: {totals['filtered']}</b>"
    
    bot = user_bots.get(user_id)
    msg = status_messages.get(user_id)

    # If we need a new message or don't have one yet
    if force_new or not msg:
        if bot:
            try:
                new_msg = await bot.send_message(user_id, text, parse_mode="HTML")
                status_messages[user_id] = new_msg # Update reference
            except Exception as e:
                logger.error(f"Failed to send new status: {e}")
        return

    # Try to edit existing
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception as e:
        # If edit fails (message deleted/too old), send a new one
        if bot:
            try:
                new_msg = await bot.send_message(user_id, text, parse_mode="HTML")
                status_messages[user_id] = new_msg
            except: pass

def add_to_ui_row(user_id, token, name, sent, filtered, status="Running"):
    if user_id not in ui_rows_state: ui_rows_state[user_id] = {}
    row = f"<b>{name}:</b> Sent {sent} | Filtered {filtered}"
    if status: row += f" ({status})"
    ui_rows_state[user_id][token] = row

def update_totals(user_id, new_sent=0, new_filtered=0):
    if user_id not in ui_totals_state: ui_totals_state[user_id] = {"sent": 0, "filtered": 0}
    ui_totals_state[user_id]["sent"] += new_sent
    ui_totals_state[user_id]["filtered"] += new_filtered

def reset_ui(user_id):
    """Clears the UI counts for a new day."""
    ui_rows_state[user_id] = {}
    ui_totals_state[user_id] = {"sent": 0, "filtered": 0}

# --- SEQUENTIAL PROCESSOR ---

async def process_account_sequence(user_id: int, token_obj: dict, settings: dict, session: aiohttp.ClientSession, db_data: dict):
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:10]
    
    if user_id not in ui_rows_state or token not in ui_rows_state[user_id]:
        add_to_ui_row(user_id, token, name, 0, 0, "Checking...")
        await update_ui(user_id)

    # 1. CHECK REQUEST CYCLE (24 Hours)
    last_req_str = db_data.get("request_times", {}).get(token)
    should_run_requests = False
    
    if not last_req_str:
        should_run_requests = True
    else:
        last_req = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        if (datetime.utcnow() - last_req).total_seconds() > 24 * 3600:
            should_run_requests = True

    if should_run_requests:
        # === NEW DAY DETECTED ===
        # Reset UI counts and send a FRESH message for visibility
        if not ui_totals_state.get(user_id, {}).get("sent", 0): 
             # Only reset if we haven't already started a run for another account in this loop
             pass 
        
        # Force a new message if it's the first account starting a new day
        add_to_ui_row(user_id, token, name, 0, 0, "Sending Requests...")
        await update_ui(user_id, force_new=True) # <--- SENDS NEW MESSAGE HERE
        
        await run_requests_task(user_id, token_obj, session)
        
        await set_automation_last_request_time(user_id, token)
        
        current_row = ui_rows_state[user_id][token].split("(")[0].strip()
        ui_rows_state[user_id][token] = f"{current_row} (Waiting 20m)"
        await update_ui(user_id)
        return

    # 2. CHECK FOLLOW-UPS (Waves)
    added_users = db_data.get("add_times", {}).get(token, {})
    lounge_history = db_data.get("lounge_sent", {}).get(token, {})
    
    lounge_msg = settings.get("lounge_message")
    chat_msg = settings.get("chatroom_message")
    
    if not lounge_msg or not chat_msg: return

    now = datetime.utcnow()
    messages_sent = 0
    
    for pid, add_time_str in added_users.items():
        add_time = add_time_str if isinstance(add_time_str, datetime) else datetime.fromisoformat(str(add_time_str))
        elapsed_mins = (now - add_time).total_seconds() / 60
        user_history = lounge_history.get(pid, {})
        wave_to_run = 0

        if elapsed_mins >= 20 and "wave_1" not in user_history and elapsed_mins < 120:
            wave_to_run = 1
        elif elapsed_mins >= 60 and "wave_2" not in user_history and elapsed_mins < 300:
            wave_to_run = 2
        elif elapsed_mins >= 360 and "wave_3" not in user_history and elapsed_mins < 1500:
            wave_to_run = 3
        
        if wave_to_run > 0:
            current_row = ui_rows_state[user_id].get(token, "").split("(")[0].strip()
            ui_rows_state[user_id][token] = f"{current_row} (Wave {wave_to_run})"
            if messages_sent % 5 == 0: await update_ui(user_id)

            if await _send_msg(session, token, pid, lounge_msg, "lounge"):
                await asyncio.sleep(2)
                await _send_msg(session, token, pid, chat_msg, "chat")
                await mark_lounge_sent(user_id, token, pid, wave_to_run)
                messages_sent += 1
                await asyncio.sleep(PER_USER_DELAY)

    if messages_sent > 0:
        await add_automation_log(user_id, f"[{name}] Follow-ups: {messages_sent} sent")
        current_row = ui_rows_state[user_id][token].split("(")[0].strip()
        ui_rows_state[user_id][token] = f"{current_row} (Idle)"
        await update_ui(user_id)
    else:
        current_row = ui_rows_state[user_id][token].split("(")[0].strip()
        ui_rows_state[user_id][token] = f"{current_row} (Idle)"

async def run_requests_task(user_id, token_obj, session):
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:10]
    
    is_spam_on = await get_individual_spam_filter(user_id, "request")
    blocked = await get_blocked_users(user_id)
    sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_on else set()
    
    await apply_filter_for_account(token, user_id)
    filters = await get_user_filters(user_id, token) or {}
    
    session_sent = 0
    session_filtered = 0
    ids_to_save = []
    
    while True:
        users = await _discover_users(session, token, filters)
        if not users: break
        
        limit_hit = False
        for user in users:
            pid = user.get("_id")
            if pid in blocked or pid in sent_ids:
                session_filtered += 1
                update_totals(user_id, new_filtered=1)
                continue
            
            res = await _send_friend_request(session, token, pid)
            if res == "LIMIT": 
                limit_hit = True; break
            
            if res == "OK":
                session_sent += 1
                update_totals(user_id, new_sent=1)
                
                sent_ids.add(pid)
                ids_to_save.append(pid)
                await set_automation_add_time(user_id, token, pid)
                
                add_to_ui_row(user_id, token, name, session_sent, session_filtered, "Running")
                await update_ui(user_id)
                
                await asyncio.sleep(PER_USER_DELAY)
        
        if is_spam_on and ids_to_save:
            await bulk_add_sent_ids(user_id, "request", ids_to_save)
            ids_to_save = []
            
        if limit_hit: 
            add_to_ui_row(user_id, token, name, session_sent, session_filtered, "Limit Reached")
            await update_ui(user_id)
            break
            
        await asyncio.sleep(PER_BATCH_DELAY)

    if session_sent > 0:
        await add_automation_log(user_id, f"[{name}] Requests: {session_sent}")

# --- MAIN MONITOR LOOP ---

async def monitor_loop(user_id: int):
    logger.info(f"Sequential Monitor Started for {user_id}")
    
    # Send initial status message if we have the bot
    await update_ui(user_id, force_new=True)
    
    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"): break
            
            selected = settings.get("selected_accounts", "all")
            all_tokens = await get_tokens(user_id)
            if selected == "all": target_tokens = all_tokens
            elif selected == "active_only": target_tokens = await get_active_tokens(user_id)
            else: target_tokens = [all_tokens[i] for i in selected if 0 <= i < len(all_tokens)]
            
            db_data = await get_automation_pending_followups(user_id)
            
            async with aiohttp.ClientSession() as session:
                for token_obj in target_tokens:
                    if not (await get_automation_settings(user_id)).get("enabled"): break
                    await process_account_sequence(user_id, token_obj, settings, session, db_data)
            
            await update_ui(user_id)
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor Error: {e}")
            await asyncio.sleep(60)

# --- CONTROL ---

async def run_automation_action(user_id: int, status_msg):
    """Called from Manual Trigger."""
    global monitor_task
    
    status_messages[user_id] = status_msg
    # We don't have 'bot' here usually, but status_msg has .bot
    if status_msg and hasattr(status_msg, 'bot'):
        user_bots[user_id] = status_msg.bot

    await set_automation_enabled(user_id, True)
    
    if monitor_task and not monitor_task.done(): monitor_task.cancel()
    
    # Clear UI for fresh manual run
    reset_ui(user_id)
    monitor_task = asyncio.create_task(monitor_loop(user_id))

def start_automation(user_id: int, bot):
    """Called from Main / Startup."""
    global monitor_task
    
    user_bots[user_id] = bot # Store bot for sending messages later
    asyncio.create_task(set_automation_enabled(user_id, True))
    
    if monitor_task and not monitor_task.done(): return
    
    monitor_task = asyncio.create_task(monitor_loop(user_id))

def stop_automation(user_id: int):
    global monitor_task
    asyncio.create_task(set_automation_enabled(user_id, False))
    
    if monitor_task:
        monitor_task.cancel()
        monitor_task = None
    
    msg = status_messages.get(user_id)
    if msg:
        asyncio.create_task(msg.edit_text("🛑 <b>Automation Stopped</b>", parse_mode="HTML"))

def is_automation_running(user_id: int) -> bool:
    global monitor_task
    return monitor_task is not None and not monitor_task.done()
