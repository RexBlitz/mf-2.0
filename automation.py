"""
Automation Module
=================
Per account, one by one:

  Day start  → Friend Requests (once per 24h)
  +15 min    → Lounge msg       (wave_1_lounge)
  +16 min    → Chatroom msg     (wave_1_chat)
  +60 min    → Lounge msg       (wave_2_lounge)
  +300 min   → Lounge msg       (wave_3_lounge)
             → Chatroom msg     (wave_3_chat)
  Next day   → repeat

Uses exact original functions:
  send_lounge()              from lounge.py
  send_message_to_everyone() from chatroom.py

UI: original ui_stats_state system — per account stats, one message edited live.
"""

import asyncio
import aiohttp
import logging
from datetime import datetime
from typing import List, Dict, Set

from db import (
    get_automation_settings, get_active_tokens, get_tokens,
    get_user_filters, bulk_add_sent_ids, is_already_sent,
    get_blocked_users, add_automation_log, get_individual_spam_filter,
    set_automation_enabled, set_automation_last_request_time,
    get_automation_pending_followups, set_automation_add_time,
)
from filters import apply_filter_for_account
from lounge import send_lounge
from chatroom import send_message_to_everyone

logger = logging.getLogger(__name__)

# --- GLOBAL STATE ---
monitor_task: asyncio.Task = None
status_messages: Dict[int, object] = {}
user_bots: Dict[int, object] = {}
ui_stats_state: Dict[int, Dict[str, Dict]] = {}

# --- TIMINGS ---
PER_USER_DELAY = 0.5
PER_BATCH_DELAY = 1

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

# Wave schedule: (wave_key, do_lounge, do_chat, min_minutes, max_minutes)
WAVES = [
    ("wave_1_lounge", True,  False, 15,  59),    # +15 min → lounge only
    ("wave_1_chat",   False, True,  16,  60),    # +16 min → chatroom only
    ("wave_2_lounge", True,  False, 60,  299),   # +1 hour → lounge only
    ("wave_3_lounge", True,  True,  300, 1440),  # +5 hour → lounge + chatroom
]


# --- UI MANAGER (exact original) ---

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


# --- WAVE TRACKING ---

async def _is_wave_done(db_data: dict, token: str, pid: str, wave_key: str) -> bool:
    return wave_key in db_data.get("lounge_sent", {}).get(token, {}).get(pid, {})

async def _mark_wave_done(user_id: int, token: str, pid: str, wave_key: str):
    from db import _get_user_collection, _ensure_user_collection_exists
    import datetime as dt
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"lounge_sent.{token}.{pid}.{wave_key}": dt.datetime.utcnow()}},
        upsert=True
    )


# --- FRIEND REQUEST HELPERS ---

async def _discover_users(session, token, filters=None):
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    params = {"lng": "71.9140141", "unreachableUserIds": "", "lat": "29.6264544", "locale": "en"}
    if filters and filters.get("filterNationalityCode"):
        params["filterNationalityCode"] = filters["filterNationalityCode"]
    try:
        async with session.get(url, headers=headers, params=params, timeout=10) as resp:
            return (await resp.json()).get("users", []) if resp.status == 200 else []
    except:
        return []

async def _send_friend_request(session, token, person_id):
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    try:
        async with session.get(url, headers={**BASE_HEADERS, 'meeff-access-token': token}, timeout=10) as resp:
            data = await resp.json()
            if data.get("errorCode") == "LikeExceeded": return "LIMIT"
            return "FAIL" if data.get("errorCode") else "OK"
    except:
        return "FAIL"


# --- MAIN PROCESSOR (one account at a time) ---

async def process_account(user_id: int, token_obj: dict, settings: dict):
    token = token_obj["token"]
    name  = token_obj.get("name", "Acc")[:15]
    bot   = user_bots.get(user_id)
    msg   = status_messages.get(user_id)

    init_account_stats(user_id, token, name)

    db_data = await get_automation_pending_followups(user_id)

    # ── 1. FRIEND REQUESTS (24h gate) ─────────────────────────────────────
    last_req_str = db_data.get("request_times", {}).get(token)
    should_request = False

    if not last_req_str:
        should_request = True
    else:
        last_req = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        if (datetime.utcnow() - last_req).total_seconds() > 24 * 3600:
            should_request = True

    if should_request:
        update_account_stats(user_id, token, {}, "Sending Requests...")
        await update_ui(user_id)

        req_sent, req_filtered = await _run_requests(user_id, token_obj)
        update_account_stats(user_id, token, {'req_s': req_sent, 'req_f': req_filtered}, "Requests Done")
        await set_automation_last_request_time(user_id, token)
        await update_ui(user_id)
        await add_automation_log(user_id, f"[{name}] Requests: {req_sent} sent, {req_filtered} filtered")

        # Refresh db_data after requests so new add_times are visible
        db_data = await get_automation_pending_followups(user_id)

    # ── 2. FOLLOW-UP WAVES ─────────────────────────────────────────────────
    lounge_msg  = settings.get("lounge_message")
    chat_msg    = settings.get("chatroom_message")
    added_users = db_data.get("add_times", {}).get(token, {})
    now         = datetime.utcnow()

    lounge_spam = await get_individual_spam_filter(user_id, "lounge")
    chat_spam   = await get_individual_spam_filter(user_id, "chatroom")

    for pid, add_time_str in added_users.items():
        add_time     = add_time_str if isinstance(add_time_str, datetime) else datetime.fromisoformat(str(add_time_str))
        elapsed_mins = (now - add_time).total_seconds() / 60

        for wave_key, do_lounge, do_chat, min_m, max_m in WAVES:
            if elapsed_mins < min_m or elapsed_mins >= max_m:
                continue
            if await _is_wave_done(db_data, token, pid, wave_key):
                continue

            # ── LOUNGE ────────────────────────────────────────────────────
            if do_lounge and lounge_msg and msg and bot:
                update_account_stats(user_id, token, {}, f"Lounge {wave_key}...")
                await update_ui(user_id)

                await send_lounge(
                    token=token,
                    message=lounge_msg,
                    status_message=msg,
                    bot=bot,
                    chat_id=user_id,
                    spam_enabled=lounge_spam,
                    user_id=user_id,
                )
                update_account_stats(user_id, token, {'lng_s': 1})

            # ── CHATROOM ──────────────────────────────────────────────────
            if do_chat and chat_msg:
                update_account_stats(user_id, token, {}, f"Chat {wave_key}...")
                await update_ui(user_id)

                sent_ids      = await is_already_sent(user_id, "chatroom", None, bulk=True) if chat_spam else set()
                sent_ids_lock = asyncio.Lock()

                await send_message_to_everyone(
                    token=token,
                    message=chat_msg,
                    chat_id=user_id,
                    spam_enabled=chat_spam,
                    user_id=user_id,
                    sent_ids=sent_ids,
                    sent_ids_lock=sent_ids_lock,
                )
                update_account_stats(user_id, token, {'chat_s': 1})

            await _mark_wave_done(user_id, token, pid, wave_key)
            await add_automation_log(user_id, f"[{name}] {wave_key} done")
            break

    update_account_stats(user_id, token, {}, "Done")
    await update_ui(user_id)


async def _run_requests(user_id: int, token_obj: dict):
    token    = token_obj["token"]
    is_spam  = await get_individual_spam_filter(user_id, "request")
    blocked  = await get_blocked_users(user_id)
    sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam else set()
    await apply_filter_for_account(token, user_id)
    filters  = await get_user_filters(user_id, token) or {}

    req_sent = 0
    req_filtered = 0
    ids_to_save: List[str] = []

    async with aiohttp.ClientSession() as session:
        while True:
            users = await _discover_users(session, token, filters)
            if not users: break

            limit_hit = False
            for user in users:
                pid = user.get("_id")
                if pid in blocked or pid in sent_ids:
                    req_filtered += 1
                    continue

                res = await _send_friend_request(session, token, pid)
                if res == "LIMIT":
                    limit_hit = True; break
                if res == "OK":
                    req_sent += 1
                    sent_ids.add(pid)
                    ids_to_save.append(pid)
                    await set_automation_add_time(user_id, token, pid)
                    await asyncio.sleep(PER_USER_DELAY)

            if is_spam and ids_to_save:
                await bulk_add_sent_ids(user_id, "request", ids_to_save)
                ids_to_save = []

            if limit_hit: break
            await asyncio.sleep(PER_BATCH_DELAY)

    return req_sent, req_filtered


# --- MONITOR LOOP ---

async def monitor_loop(user_id: int):
    logger.info(f"Monitor started for {user_id}")
    await update_ui(user_id, force_new=True)

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"): break

            selected   = settings.get("selected_accounts", "all")
            all_tokens = await get_tokens(user_id)

            if selected == "all":
                target_tokens = all_tokens
            elif selected == "active_only":
                target_tokens = await get_active_tokens(user_id)
            else:
                target_tokens = [all_tokens[i] for i in selected if 0 <= i < len(all_tokens)]

            for token_obj in target_tokens:
                if not (await get_automation_settings(user_id)).get("enabled"): break
                await process_account(user_id, token_obj, settings)

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

    async def _safe_stop():
        msg = status_messages.get(user_id)
        if msg:
            try: await msg.edit_text("🛑 <b>Automation Stopped</b>", parse_mode="HTML")
            except: pass
    asyncio.create_task(_safe_stop())

def is_automation_running(user_id: int) -> bool:
    global monitor_task
    return monitor_task is not None and not monitor_task.done()
