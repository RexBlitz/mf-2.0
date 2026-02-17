"""Automation Module - 24h Cycle with Targeted Follow-ups & Database Persistence."""

import asyncio
import aiohttp
import logging
import time
from typing import List, Dict, Set

from db import (
    get_automation_settings, get_active_tokens, get_tokens, 
    get_user_filters, bulk_add_sent_ids, is_already_sent,
    get_blocked_users, add_automation_log, get_individual_spam_filter,
    set_automation_enabled, is_automation_running as db_is_running
)
from filters import apply_filter_for_account

logger = logging.getLogger(__name__)

# --- GLOBAL STATE ---
automation_tasks: Dict[int, asyncio.Task] = {}
automation_status_msgs: Dict[int, object] = {}

# --- TIMINGS (Original Fast Speed) ---
PER_USER_DELAY = 0.5  #
PER_BATCH_DELAY = 1
EMPTY_BATCH_DELAY = 2

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

# --- DISCOVERY & API HELPERS ---

async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[Dict]:
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    params = {"lng": "71.9140141", "unreachableUserIds": "", "lat": "29.6264544", "locale": "en"}
    
    if filters and filters.get("filterNationalityCode"):
        params["filterNationalityCode"] = filters["filterNationalityCode"]
        
    try:
        async with session.get(url, headers=headers, params=params, timeout=10) as resp:
            if resp.status != 200: return []
            data = await resp.json()
            return data.get("users", [])
    except Exception: return []

async def _send_friend_request(session, token, person_id):
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            data = await resp.json()
            if data.get("errorCode") == "LikeExceeded": return "LIMIT"
            if data.get("errorCode"): return "FAIL"
            return "OK"
    except: return "FAIL"

async def _send_lounge_msg(session, token, person_id, message):
    url = "https://api.meeff.com/lounge/create/v1/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token, 'Content-Type': "application/json"}
    try:
        async with session.post(url, headers=headers, json={"targetUserId": person_id, "content": message, "locale": "en"}, timeout=10) as resp:
            data = await resp.json()
            return not data.get("errorCode")
    except: return False

async def _send_chatroom_msg(session, token, person_id, message):
    headers = {**BASE_HEADERS, 'meeff-access-token': token, 'Content-Type': "application/json"}
    try:
        # Open Room
        async with session.post("https://api.meeff.com/chatroom/open/v2", headers=headers, json={"waitingRoomId": person_id, "locale": "en"}, timeout=10) as resp:
            if resp.status == 412: return "DISABLED"
            if resp.status != 200: return False
            data = await resp.json()
            cid = data.get("chatRoom", {}).get("_id")
            if not cid: return False
        
        # Send Message
        async with session.post("https://api.meeff.com/chat/send/v2", headers=headers, json={"chatRoomId": cid, "message": message, "locale": "en"}, timeout=10) as resp:
            return resp.status == 200
    except: return False

# --- LIVE STATUS UPDATER ---
async def update_live_status(status_msg, header, sub_header, details):
    """Updates Telegram message with live stats."""
    if not status_msg: return
    try:
        text = f"{header}\n\n{sub_header}\n{details}"
        await status_msg.edit_text(text, parse_mode="HTML")
    except Exception:
        pass

# --- AUTOMATION CYCLE ENGINE ---

async def automation_cycle(user_id: int):
    """The 24h Cycle: Req -> 20m -> L/C -> 1h -> L/C -> 6h -> L/C -> Sleep."""
    try:
        status_msg = automation_status_msgs.get(user_id)
        
        while True:
            cycle_start = time.time()
            
            # --- 1. SEND REQUESTS ---
            successful_targets = await run_auto_requests(user_id, status_msg)
            
            if not successful_targets:
                await update_live_status(status_msg, "⚠️ <b>Cycle Paused</b>", "No requests sent.", "Retrying in 1 hour...")
                await asyncio.sleep(3600)
                continue

            # --- 2. WAIT 20 MINUTES ---
            await wait_with_countdown(user_id, status_msg, 20 * 60, "Follow-up Wave 1 (20m)")

            # --- 3. WAVE 1: LOUNGE -> CHAT ---
            await run_target_lounge(user_id, status_msg, successful_targets, "Wave 1: Lounge")
            await run_target_chat(user_id, status_msg, successful_targets, "Wave 1: Chat")

            # --- 4. WAIT 1 HOUR ---
            await wait_with_countdown(user_id, status_msg, 60 * 60, "Follow-up Wave 2 (1h)")

            # --- 5. WAVE 2: LOUNGE -> CHAT ---
            await run_target_lounge(user_id, status_msg, successful_targets, "Wave 2: Lounge")
            await run_target_chat(user_id, status_msg, successful_targets, "Wave 2: Chat")

            # --- 6. WAIT 6 HOURS ---
            await wait_with_countdown(user_id, status_msg, 6 * 60 * 60, "Follow-up Wave 3 (6h)")

            # --- 7. WAVE 3: LOUNGE -> CHAT ---
            await run_target_lounge(user_id, status_msg, successful_targets, "Wave 3: Lounge")
            await run_target_chat(user_id, status_msg, successful_targets, "Wave 3: Chat")

            # --- 8. FINISH 24H CYCLE ---
            elapsed = time.time() - cycle_start
            remaining = (24 * 60 * 60) - elapsed
            if remaining > 0:
                await wait_with_countdown(user_id, status_msg, int(remaining), "Next Daily Cycle")

    except asyncio.CancelledError:
        logger.info(f"Automation stopped for {user_id}")
    except Exception as e:
        logger.error(f"Cycle Error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ <b>Error:</b> {str(e)[:50]}", parse_mode="HTML")

async def wait_with_countdown(user_id, status_msg, seconds, phase_name):
    """Sleeps with a countdown UI."""
    end_time = time.time() + seconds
    while time.time() < end_time:
        if user_id not in automation_tasks: raise asyncio.CancelledError
        
        remaining = int(end_time - time.time())
        m, s = divmod(remaining, 60)
        h, m = divmod(m, 60)
        
        await update_live_status(
            status_msg, 
            "😴 <b>Automation Sleeping</b>", 
            f"<b>Waiting For:</b> {phase_name}", 
            f"⏳ Resuming in: {h}h {m}m {s}s"
        )
        await asyncio.sleep(min(remaining, 60))

# --- WORKER FUNCTIONS ---

async def run_auto_requests(user_id, status_msg) -> List[Dict]:
    """Sends requests and returns list of {token, pid} for follow-ups."""
    settings = await get_automation_settings(user_id)
    token_list = await get_target_tokens(user_id, settings)
    
    total_sent, total_filtered = 0, 0
    successful_targets = [] # Stores (token, pid) pairs
    
    blocked = await get_blocked_users(user_id)
    is_spam_on = await get_individual_spam_filter(user_id, "request")
    
    header = f"📨 <b>Sending Requests</b>\nAccounts: {len(token_list)}"

    async with aiohttp.ClientSession() as session:
        for idx, token_obj in enumerate(token_list, 1):
            if user_id not in automation_tasks: break
            
            token, name = token_obj["token"], token_obj.get("name", f"Acc {idx}")
            await apply_filter_for_account(token, user_id)
            filters = await get_user_filters(user_id, token) or {}
            
            acc_sent = 0
            # Run batches until limit or empty
            while True:
                if user_id not in automation_tasks: break
                
                users = await _discover_users(session, token, filters)
                if not users: break 
                
                sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_on else set()
                ids_to_save = []
                limit_reached = False
                
                for user in users:
                    if user_id not in automation_tasks: break
                    pid = user.get("_id")
                    if not pid or pid in blocked or pid in sent_ids:
                        total_filtered += 1
                        continue
                    
                    res = await _send_friend_request(session, token, pid)
                    if res == "LIMIT": 
                        limit_reached = True; break
                    
                    if res == "OK":
                        acc_sent += 1; total_sent += 1
                        sent_ids.add(pid); ids_to_save.append(pid)
                        successful_targets.append({"token": token, "pid": pid})
                        
                        if acc_sent % 5 == 0:
                            await update_live_status(status_msg, header, f"Processing: {name}", f"⚡ Sent: {total_sent} | Filtered: {total_filtered}")
                        
                        await asyncio.sleep(PER_USER_DELAY)
                
                if is_spam_on and ids_to_save: await bulk_add_sent_ids(user_id, "request", ids_to_save)
                if limit_reached: break
                await asyncio.sleep(PER_BATCH_DELAY)
            
            if acc_sent > 0: await add_automation_log(user_id, f"[{name}] Requests: {acc_sent}")

    return successful_targets

async def run_target_lounge(user_id, status_msg, targets, phase_name):
    """Sends Lounge messages to specific targets from previous request cycle."""
    settings = await get_automation_settings(user_id)
    msg = settings.get("lounge_message")
    if not msg: return

    header = f"📢 <b>{phase_name}</b>\nTargets: {len(targets)}"
    sent_count = 0
    
    async with aiohttp.ClientSession() as session:
        for i, target in enumerate(targets):
            if user_id not in automation_tasks: break
            
            if await _send_lounge_msg(session, target['token'], target['pid'], msg):
                sent_count += 1
                if sent_count % 5 == 0:
                    await update_live_status(status_msg, header, "Sending messages...", f"⚡ Sent: {sent_count}/{len(targets)}")
                await asyncio.sleep(PER_USER_DELAY)
    
    await add_automation_log(user_id, f"{phase_name}: {sent_count} sent")

async def run_target_chat(user_id, status_msg, targets, phase_name):
    """Sends Chat messages to specific targets."""
    settings = await get_automation_settings(user_id)
    msg = settings.get("chatroom_message")
    if not msg: return

    header = f"💬 <b>{phase_name}</b>\nTargets: {len(targets)}"
    sent_count = 0
    
    async with aiohttp.ClientSession() as session:
        for i, target in enumerate(targets):
            if user_id not in automation_tasks: break
            
            res = await _send_chatroom_msg(session, target['token'], target['pid'], msg)
            if res and res != "DISABLED":
                sent_count += 1
                if sent_count % 5 == 0:
                    await update_live_status(status_msg, header, "Sending messages...", f"⚡ Sent: {sent_count}/{len(targets)}")
            
            await asyncio.sleep(PER_USER_DELAY)

    await add_automation_log(user_id, f"{phase_name}: {sent_count} sent")

async def get_target_tokens(user_id, settings):
    selected = settings.get("selected_accounts", "all")
    if selected == "all": return await get_tokens(user_id)
    if selected == "active_only": return await get_active_tokens(user_id)
    all_toks = await get_tokens(user_id)
    return [all_toks[i] for i in selected if 0 <= i < len(all_toks)]

# --- CONTROL FUNCTIONS ---

async def run_automation_action(user_id: int, status_msg):
    """Entry point."""
    automation_status_msgs[user_id] = status_msg
    if user_id in automation_tasks: automation_tasks[user_id].cancel()
    
    # SAVE STATUS TO DB
    await set_automation_enabled(user_id, True)
    
    task = asyncio.create_task(automation_cycle(user_id))
    automation_tasks[user_id] = task

def start_automation(user_id: int, bot):
    """Starts automation (called from Main)."""
    # Create the task loop
    if user_id not in automation_tasks:
        # Save ON state to DB
        asyncio.create_task(set_automation_enabled(user_id, True))
        
        # NOTE: We don't have the original status message here if called from startup/command.
        # It will be set when 'run_automation_action' is triggered or UI updates.
        task = asyncio.create_task(automation_cycle(user_id))
        automation_tasks[user_id] = task

def stop_automation(user_id: int):
    """Stops automation."""
    # Save OFF state to DB
    asyncio.create_task(set_automation_enabled(user_id, False))
    
    if user_id in automation_tasks:
        automation_tasks[user_id].cancel()
        del automation_tasks[user_id]
        if user_id in automation_status_msgs:
            asyncio.create_task(update_live_status(automation_status_msgs[user_id], "🛑 Stopped", "", ""))

def is_automation_running(user_id: int) -> bool:
    return user_id in automation_tasks
