"""
Automation Module - Fixed Logic & 1-Minute Delay.
- Logic: Matches lounge.py/chatroom.py for spam checks.
- Fix: Marks filtered users as "Done" in DB to stop infinite counting.
- Timing: 60-second delay between Lounge and Chatroom messages.
- UI: Detailed Table Stats.
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
status_messages: Dict[int, object] = {} 
user_bots: Dict[int, object] = {}       

# STATS STATE: { user_id: { token: { 'name': '...', 'req_s': 0, ... } } }
ui_stats_state: Dict[int, Dict[str, Dict]] = {}

# --- TIMINGS ---
PER_USER_DELAY = 0.5 
PER_BATCH_DELAY = 1.5
MULTI_MSG_DELAY = 1.0
LOUNGE_TO_CHAT_DELAY = 60  # Requested 1 Minute Delay

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

# --- API HELPERS ---

async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None, unreachable_ids: str = "") -> List[Dict]:
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    params = {"lng": "71.9140141", "lat": "29.6264544", "locale": "en", "unreachableUserIds": unreachable_ids}
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

async def _send_msg(session, token, person_id, raw_msg, type="lounge"):
    """Splits message by comma and sends sequentially (Logic from lounge.py)."""
    if not raw_msg: return False
    messages = [m.strip() for m in raw_msg.split(',') if m.strip()]
    if not messages: return False

    headers = {**BASE_HEADERS, 'meeff-access-token': token, 'Content-Type': "application/json"}
    success = True

    try:
        if type == "chat":
            # Open Room Logic
            async with session.post("https://api.meeff.com/chatroom/open/v2", headers=headers, json={"waitingRoomId": person_id, "locale": "en"}, timeout=10) as resp:
                if resp.status == 412: return "DISABLED"
                if resp.status != 200: return False
                cid = (await resp.json()).get("chatRoom", {}).get("_id")
                if not cid: return False
            
            # Send Parts
            for part in messages:
                async with session.post("https://api.meeff.com/chat/send/v2", headers=headers, json={"chatRoomId": cid, "message": part, "locale": "en"}, timeout=10) as resp:
                    if resp.status != 200: success = False
                await asyncio.sleep(MULTI_MSG_DELAY)

        else:
            url = "https://api.meeff.com/lounge/create/v1/"
            for part in messages:
                async with session.post(url, headers=headers, json={"targetUserId": person_id, "content": part, "locale": "en"}, timeout=10) as resp:
                    if (await resp.json()).get("errorCode"): success = False
                await asyncio.sleep(MULTI_MSG_DELAY)

        return success
    except: return False

# --- UI MANAGER ---

def init_account_stats(user_id, token, name):
    if user_id not in ui_stats_state: ui_stats_state[user_id] = {}
    if token not in ui_stats_state[user_id]:
        ui_stats_state[user_id][token] = {
            'name': name,
            'req_s': 0, 'req_f': 0,
            'lng_s': 0, 'lng_f': 0,
            'chat_s': 0, 'chat_f': 0,
            'status': 'Checking...'
        }

def update_account_stats(user_id, token, updates: dict, status=None):
    if user_id in ui_stats_state and token in ui_stats_state[user_id]:
        stats = ui_stats_state[user_id][token]
        for k, v in updates.items():
            if k in stats: stats[k] += v
        if status: stats['status'] = status

async def update_ui(user_id, force_new=False):
    if user_id not in ui_stats_state or not ui_stats_state[user_id]: return

    text = "🔄 <b>Friend Request Automation</b>\n\n"
    totals = {'req_s':0,'req_f':0,'lng_s':0,'lng_f':0,'chat_s':0,'chat_f':0}

    for token, stats in ui_stats_state[user_id].items():
        for k in totals: totals[k] += stats[k]
        text += f"<b>{stats['name']}</b> ({stats['status']})\n"
        text += f"Req: {stats['req_s']} / {stats['req_f']} | Lng: {stats['lng_s']} / {stats['lng_f']} | Chat: {stats['chat_s']} / {stats['chat_f']}\n\n"

    text += "-------\n"
    text += f"<b>Total Request:</b> Sent: {totals['req_s']} | Filtered: {totals['req_f']}\n"
    text += f"<b>Total Lounge:</b> Sent: {totals['lng_s']} | Filtered: {totals['lng_f']}\n"
    text += f"<b>Total Chatroom:</b> Sent: {totals['chat_s']} | Filtered: {totals['chat_f']}"

    bot = user_bots.get(user_id)
    msg = status_messages.get(user_id)

    if force_new or not msg:
        if bot:
            try: status_messages[user_id] = await bot.send_message(user_id, text, parse_mode="HTML")
            except: pass
        return

    try: await msg.edit_text(text, parse_mode="HTML")
    except: pass

def reset_ui(user_id):
    ui_stats_state[user_id] = {}

# --- PROCESSOR ---

async def process_account_sequence(user_id: int, token_obj: dict, settings: dict, session: aiohttp.ClientSession, db_data: dict):
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:15]
    
    init_account_stats(user_id, token, name)

    # 1. REQUEST CYCLE
    last_req_str = db_data.get("request_times", {}).get(token)
    should_run_requests = False
    
    if not last_req_str:
        should_run_requests = True
    else:
        last_req = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        if (datetime.utcnow() - last_req).total_seconds() > 24 * 3600:
            should_run_requests = True

    if should_run_requests:
        update_account_stats(user_id, token, {}, "Sending Requests...")
        await update_ui(user_id)
        await run_requests_task(user_id, token_obj, session)
        await set_automation_last_request_time(user_id, token)
        update_account_stats(user_id, token, {}, "Requests Done")
        await update_ui(user_id)
        return

    # 2. FOLLOW-UPS
    added_users = db_data.get("add_times", {}).get(token, {})
    lounge_history = db_data.get("lounge_sent", {}).get(token, {})
    
    lounge_msg = settings.get("lounge_message")
    chat_msg = settings.get("chatroom_message")
    
    if not lounge_msg or not chat_msg: 
        update_account_stats(user_id, token, {}, "No Msg Set")
        return

    # PRE-FETCH SPAM & BLOCK LISTS (Logic from lounge.py)
    blocked = await get_blocked_users(user_id)
    lounge_spam_on = await get_individual_spam_filter(user_id, "lounge")
    chat_spam_on = await get_individual_spam_filter(user_id, "chatroom")
    
    lounge_sent_ids = await is_already_sent(user_id, "lounge", None, bulk=True) if lounge_spam_on else set()
    chat_sent_ids = await is_already_sent(user_id, "chatroom", None, bulk=True) if chat_spam_on else set()

    now = datetime.utcnow()
    messages_sent = 0
    ids_to_save_lounge = []
    ids_to_save_chat = []
    
    for pid, add_time_str in added_users.items():
        add_time = add_time_str if isinstance(add_time_str, datetime) else datetime.fromisoformat(str(add_time_str))
        elapsed_mins = (now - add_time).total_seconds() / 60
        user_history = lounge_history.get(pid, {})
        wave_to_run = 0

        if elapsed_mins >= 20 and "wave_1" not in user_history and elapsed_mins < 120: wave_to_run = 1
        elif elapsed_mins >= 60 and "wave_2" not in user_history and elapsed_mins < 300: wave_to_run = 2
        elif elapsed_mins >= 360 and "wave_3" not in user_history and elapsed_mins < 1500: wave_to_run = 3
        
        if wave_to_run > 0:
            update_account_stats(user_id, token, {}, f"Sending Wave {wave_to_run}")
            if messages_sent % 5 == 0: await update_ui(user_id)

            # --- LOGIC APPLIED FROM lounge.py ---
            # "if user_meeff_id in sent_ids: filtered += 1"
            
            # Check Blocked
            if pid in blocked:
                update_account_stats(user_id, token, {'lng_f': 1, 'chat_f': 1})
                await mark_lounge_sent(user_id, token, pid, wave_to_run) # Mark Done
                continue

            # Check Spam (Only on Wave 1)
            # Logic: If already sent, skip and count as filtered
            if wave_to_run == 1:
                is_spam = False
                if lounge_spam_on and pid in lounge_sent_ids: is_spam = True
                if chat_spam_on and pid in chat_sent_ids: is_spam = True
                
                if is_spam:
                    update_account_stats(user_id, token, {'lng_f': 1, 'chat_f': 1})
                    # Mark all waves done so we don't check again
                    await mark_lounge_sent(user_id, token, pid, 1)
                    await mark_lounge_sent(user_id, token, pid, 2)
                    await mark_lounge_sent(user_id, token, pid, 3)
                    continue

            # --- EXECUTE SEND SEQUENCE ---
            
            # 1. Send Lounge
            if await _send_msg(session, token, pid, lounge_msg, "lounge"):
                update_account_stats(user_id, token, {'lng_s': 1})
                ids_to_save_lounge.append(pid)
                
                # 2. WAIT 1 MINUTE (Requested)
                await asyncio.sleep(LOUNGE_TO_CHAT_DELAY) 
                
                # 3. Send Chat
                if await _send_msg(session, token, pid, chat_msg, "chat"):
                    update_account_stats(user_id, token, {'chat_s': 1})
                    ids_to_save_chat.append(pid)
                else:
                    update_account_stats(user_id, token, {'chat_f': 1})
                
                await mark_lounge_sent(user_id, token, pid, wave_to_run)
                messages_sent += 1
                await asyncio.sleep(PER_USER_DELAY)
            else:
                # Failed (Privacy/Error)
                update_account_stats(user_id, token, {'lng_f': 1})
                await mark_lounge_sent(user_id, token, pid, wave_to_run) 

    # Update DB with new Spam IDs
    if lounge_spam_on and ids_to_save_lounge: await bulk_add_sent_ids(user_id, "lounge", ids_to_save_lounge)
    if chat_spam_on and ids_to_save_chat: await bulk_add_sent_ids(user_id, "chatroom", ids_to_save_chat)

    if messages_sent > 0:
        await add_automation_log(user_id, f"[{name}] Follow-ups: {messages_sent} sent")
        update_account_stats(user_id, token, {}, "Requests Done") 
        await update_ui(user_id)
    else:
        update_account_stats(user_id, token, {}, "Requests Done")

async def run_requests_task(user_id, token_obj, session):
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:10]
    
    is_spam_on = await get_individual_spam_filter(user_id, "request")
    blocked = await get_blocked_users(user_id)
    sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_on else set()
    
    await apply_filter_for_account(token, user_id)
    filters = await get_user_filters(user_id, token) or {}
    
    ids_to_save = []
    seen_ids = [] 
    
    while True:
        unreachable_str = ",".join(seen_ids[-100:])
        users = await _discover_users(session, token, filters, unreachable_str)
        if not users: break
        
        limit_hit = False
        for user in users:
            pid = user.get("_id")
            if not pid: continue
            seen_ids.append(pid)

            if pid in blocked or pid in sent_ids:
                update_account_stats(user_id, token, {'req_f': 1})
                continue
            
            res = await _send_friend_request(session, token, pid)
            if res == "LIMIT": 
                limit_hit = True; break
            
            if res == "OK":
                update_account_stats(user_id, token, {'req_s': 1})
                sent_ids.add(pid)
                ids_to_save.append(pid)
                await set_automation_add_time(user_id, token, pid)
                await update_ui(user_id)
                await asyncio.sleep(PER_USER_DELAY)
        
        if is_spam_on and ids_to_save:
            await bulk_add_sent_ids(user_id, "request", ids_to_save)
            ids_to_save = []
            
        if limit_hit: 
            update_account_stats(user_id, token, {}, "Limit Reached")
            await update_ui(user_id)
            break
            
        await asyncio.sleep(PER_BATCH_DELAY)

    if ui_stats_state[user_id][token]['req_s'] > 0:
        await add_automation_log(user_id, f"[{name}] Requests: {ui_stats_state[user_id][token]['req_s']}")

# --- MAIN MONITOR LOOP ---

async def monitor_loop(user_id: int):
    logger.info(f"Monitor Started for {user_id}")
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
    global monitor_task
    status_messages[user_id] = status_msg
    if status_msg and hasattr(status_msg, 'bot'):
        user_bots[user_id] = status_msg.bot
    await set_automation_enabled(user_id, True)
    if monitor_task and not monitor_task.done(): monitor_task.cancel()
    reset_ui(user_id)
    monitor_task = asyncio.create_task(monitor_loop(user_id))

def start_automation(user_id: int, bot):
    global monitor_task
    user_bots[user_id] = bot
    asyncio.create_task(set_automation_enabled(user_id, True))
    if monitor_task and not monitor_task.done(): return
    monitor_task = asyncio.create_task(monitor_loop(user_id))

def stop_automation(user_id: int):
    global monitor_task
    asyncio.create_task(set_automation_enabled(user_id, False))
    if monitor_task:
        monitor_task.cancel()
        monitor_task = None
    
    async def _safe_stop_msg():
        msg = status_messages.get(user_id)
        if msg:
            try: await msg.edit_text("🛑 <b>Automation Stopped</b>", parse_mode="HTML")
            except: pass
    asyncio.create_task(_safe_stop_msg())

def is_automation_running(user_id: int) -> bool:
    global monitor_task
    return monitor_task is not None and not monitor_task.done()
