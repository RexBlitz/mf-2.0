"""
Automation Module - Sequential Processing with Multi-Message Support (Comma Split).
- Runs one account at a time.
- 24h Cycle + Follow-up Waves.
- Feature: Splits messages by comma (",") and sends them sequentially.
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

# Exact functions from lounge.py and chatroom.py
from lounge import fetch_lounge_users, open_chatroom_and_send
from chatroom import fetch_chatrooms, send_message

logger = logging.getLogger(__name__)

# --- GLOBAL STATE ---
monitor_task: asyncio.Task = None
status_messages: Dict[int, object] = {} 
user_bots: Dict[int, object] = {}       
ui_stats_state: Dict[int, Dict[str, Dict]] = {}

# --- TIMINGS ---
PER_USER_DELAY = 0.5 
PER_BATCH_DELAY = 1
MULTI_MSG_DELAY = 1.5

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


# --- DETAILED UI MANAGER ---

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
            if k in stats:
                stats[k] += v
        if status:
            stats['status'] = status

async def update_ui(user_id, force_new=False):
    if user_id not in ui_stats_state or not ui_stats_state[user_id]: return

    text = "🔄 <b>Friend Request Automation</b>\n\n"
    total_req_s, total_req_f = 0, 0
    total_lng_s, total_lng_f = 0, 0
    total_chat_s, total_chat_f = 0, 0

    for token, stats in ui_stats_state[user_id].items():
        total_req_s += stats['req_s']; total_req_f += stats['req_f']
        total_lng_s += stats['lng_s']; total_lng_f += stats['lng_f']
        total_chat_s += stats['chat_s']; total_chat_f += stats['chat_f']

        text += f"<b>{stats['name']}</b> ({stats['status']})\n"
        text += f"Req: {stats['req_s']} / {stats['req_f']} | Lng: {stats['lng_s']} / {stats['lng_f']} | Chat: {stats['chat_s']} / {stats['chat_f']}\n\n"

    text += "-------\n"
    text += f"<b>Total Request:</b> Sent: {total_req_s} | Filtered: {total_req_f}\n"
    text += f"<b>Total Lounge:</b> Sent: {total_lng_s} | Filtered: {total_lng_f}\n"
    text += f"<b>Total Chatroom:</b> Sent: {total_chat_s} | Filtered: {total_chat_f}"

    bot = user_bots.get(user_id)
    msg = status_messages.get(user_id)

    if force_new or not msg:
        if bot:
            try:
                new_msg = await bot.send_message(user_id, text, parse_mode="HTML")
                status_messages[user_id] = new_msg 
            except Exception: pass
        return

    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        if bot:
            try:
                new_msg = await bot.send_message(user_id, text, parse_mode="HTML")
                status_messages[user_id] = new_msg
            except: pass

def reset_ui(user_id):
    ui_stats_state[user_id] = {}


# --- SEQUENTIAL PROCESSOR ---

async def process_account_sequence(user_id: int, token_obj: dict, settings: dict, session: aiohttp.ClientSession):
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:15]

    init_account_stats(user_id, token, name)

    # Fresh fetch per account
    db_data = await get_automation_pending_followups(user_id)

    # 1. CHECK REQUEST CYCLE (24h)
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

    # 2. LOUNGE FOLLOW-UP
    # fetch_lounge_users → DB se filter → open_chatroom_and_send (exact lounge.py flow)
    lounge_msg = settings.get("lounge_message")
    chat_msg   = settings.get("chatroom_message")

    if not lounge_msg and not chat_msg:
        update_account_stats(user_id, token, {}, "No Msg Set")
        await update_ui(user_id)
        return

    is_lounge_spam_on = await get_individual_spam_filter(user_id, "lounge")
    is_chat_spam_on   = await get_individual_spam_filter(user_id, "chatroom")

    lounge_sent_ids: set = (
        await is_already_sent(user_id, "lounge", None, bulk=True)
        if is_lounge_spam_on else set()
    )
    chatroom_sent_ids: set = (
        await is_already_sent(user_id, "chatroom", None, bulk=True)
        if is_chat_spam_on else set()
    )

    new_lounge_ids:   List[str] = []
    new_chatroom_ids: List[str] = []

    # --- LOUNGE --- (lounge.py: fetch_lounge_users + open_chatroom_and_send)
    if lounge_msg:
        update_account_stats(user_id, token, {}, "Fetching Lounge...")
        await update_ui(user_id)

        lounge_users = await fetch_lounge_users(session, token, user_id)

        for user_obj in lounge_users:
            pid = user_obj.get("user", {}).get("_id")
            if not pid:
                continue

            # Already sent? → filtered
            if is_lounge_spam_on and pid in lounge_sent_ids:
                update_account_stats(user_id, token, {'lng_f': 1})
                continue

            # Naya user → send (exact lounge.py ka open_chatroom_and_send)
            update_account_stats(user_id, token, {}, "Sending Lounge...")
            result = await open_chatroom_and_send(session, token, pid, lounge_msg, user_id)

            if result:
                update_account_stats(user_id, token, {'lng_s': 1})
                if is_lounge_spam_on:
                    lounge_sent_ids.add(pid)
                    new_lounge_ids.append(pid)
            else:
                update_account_stats(user_id, token, {'lng_f': 1})

            await update_ui(user_id)
            await asyncio.sleep(PER_USER_DELAY)

        if is_lounge_spam_on and new_lounge_ids:
            await bulk_add_sent_ids(user_id, "lounge", new_lounge_ids)

        if new_lounge_ids:
            await add_automation_log(user_id, f"[{name}] Lounge: {len(new_lounge_ids)} sent")

    # --- CHATROOM --- (chatroom.py: fetch_chatrooms + send_message)
    if chat_msg:
        update_account_stats(user_id, token, {}, "Fetching Chatrooms...")
        await update_ui(user_id)

        from_date = None
        while True:
            rooms, next_from = await fetch_chatrooms(session, token, from_date, user_id)
            if not rooms:
                break

            for room in rooms:
                room_id = room.get("_id")
                if not room_id:
                    continue

                # Already sent? → filtered
                if is_chat_spam_on and room_id in chatroom_sent_ids:
                    update_account_stats(user_id, token, {'chat_f': 1})
                    continue

                # Naya room → send (exact chatroom.py ka send_message)
                update_account_stats(user_id, token, {}, "Sending Chat...")
                result = await send_message(session, token, room_id, chat_msg, user_id)

                if result:
                    update_account_stats(user_id, token, {'chat_s': 1})
                    if is_chat_spam_on:
                        chatroom_sent_ids.add(room_id)
                        new_chatroom_ids.append(room_id)
                else:
                    update_account_stats(user_id, token, {'chat_f': 1})

                await update_ui(user_id)
                await asyncio.sleep(PER_USER_DELAY)

            if not next_from:
                break
            from_date = next_from

        if is_chat_spam_on and new_chatroom_ids:
            await bulk_add_sent_ids(user_id, "chatroom", new_chatroom_ids)

        if new_chatroom_ids:
            await add_automation_log(user_id, f"[{name}] Chatroom: {len(new_chatroom_ids)} sent")

    update_account_stats(user_id, token, {}, "Done")
    await update_ui(user_id)


async def run_requests_task(user_id, token_obj, session):
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:10]

    is_spam_on = await get_individual_spam_filter(user_id, "request")
    blocked = await get_blocked_users(user_id)
    sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_on else set()

    await apply_filter_for_account(token, user_id)
    filters = await get_user_filters(user_id, token) or {}

    ids_to_save = []

    while True:
        users = await _discover_users(session, token, filters)
        if not users: break

        limit_hit = False
        for user in users:
            pid = user.get("_id")
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

            async with aiohttp.ClientSession() as session:
                for token_obj in target_tokens:
                    if not (await get_automation_settings(user_id)).get("enabled"): break
                    await process_account_sequence(user_id, token_obj, settings, session)

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
