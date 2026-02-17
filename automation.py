"""
Automation Module
=================
Simply calls the original functions. No custom logic.

Single token  → send_lounge(), send_message_to_everyone()
All tokens    → send_lounge_all_tokens(), send_message_to_everyone_all_tokens()
Requests      → run_requests_task() per token

"""

import asyncio
import aiohttp
import logging
from datetime import datetime
from typing import List, Dict, Set

from db import (
    get_current_account,
    get_automation_settings, get_active_tokens, get_tokens,
    get_user_filters, bulk_add_sent_ids, is_already_sent,
    get_blocked_users, add_automation_log, get_individual_spam_filter,
    set_automation_enabled, set_automation_last_request_time,
    get_automation_pending_followups, set_automation_add_time,
)
from filters import apply_filter_for_account
from lounge import send_lounge, send_lounge_all_tokens
from chatroom import send_message_to_everyone, send_message_to_everyone_all_tokens

logger = logging.getLogger(__name__)

# --- GLOBAL STATE ---
monitor_task: asyncio.Task = None
status_messages: Dict[int, object] = {}
user_bots: Dict[int, object] = {}
ui_stats_state: Dict[int, Dict[str, Dict]] = {}

BASE_HEADERS = {'User-Agent': "okhttp/5.1.0", 'Accept-Encoding': "gzip"}
PER_USER_DELAY = 0.5
PER_BATCH_DELAY = 1

WAVES = [
    ("wave_1_lounge", True,  False, 15,  59),
    ("wave_1_chat",   False, True,  16,  60),
    ("wave_2_lounge", True,  False, 60,  299),
    ("wave_3_lounge", True,  True,  300, 1440),
]


# =============================================================================
# UI (original)
# =============================================================================

def init_account_stats(user_id, token, name):
    if user_id not in ui_stats_state: ui_stats_state[user_id] = {}
    if token not in ui_stats_state[user_id]:
        ui_stats_state[user_id][token] = {
            'name': name, 'req_s': 0, 'req_f': 0,
            'lng_s': 0, 'lng_f': 0, 'chat_s': 0, 'chat_f': 0,
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
    total_req_s, total_req_f = 0, 0
    total_lng_s, total_lng_f = 0, 0
    total_chat_s, total_chat_f = 0, 0

    for token, stats in ui_stats_state[user_id].items():
        total_req_s  += stats['req_s'];  total_req_f  += stats['req_f']
        total_lng_s  += stats['lng_s'];  total_lng_f  += stats['lng_f']
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
    except Exception as e:
        err = str(e).lower()
        if "not modified" in err: return
        if "message to edit not found" in err or "message_id_invalid" in err:
            if bot:
                try:
                    new_msg = await bot.send_message(user_id, text, parse_mode="HTML")
                    status_messages[user_id] = new_msg
                except: pass

def reset_ui(user_id):
    ui_stats_state[user_id] = {}


# =============================================================================
# WAVE TRACKING
# =============================================================================

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


# =============================================================================
# REQUESTS
# =============================================================================

async def _discover_users(session, token, filters=None):
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    params  = {"lng": "71.9140141", "unreachableUserIds": "", "lat": "29.6264544", "locale": "en"}
    if filters and filters.get("filterNationalityCode"):
        params["filterNationalityCode"] = filters["filterNationalityCode"]
    try:
        async with session.get("https://api.meeff.com/user/explore/v2/", headers=headers, params=params, timeout=10) as resp:
            return (await resp.json()).get("users", []) if resp.status == 200 else []
    except: return []

async def _send_friend_request(session, token, person_id):
    try:
        async with session.get(
            f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1",
            headers={**BASE_HEADERS, 'meeff-access-token': token}, timeout=10
        ) as resp:
            data = await resp.json()
            if data.get("errorCode") == "LikeExceeded": return "LIMIT"
            return "FAIL" if data.get("errorCode") else "OK"
    except: return "FAIL"

async def run_requests_task(user_id: int, token_obj: dict) -> tuple[int, int]:
    token    = token_obj["token"]
    is_spam  = await get_individual_spam_filter(user_id, "request")
    blocked  = await get_blocked_users(user_id)
    sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam else set()
    await apply_filter_for_account(token, user_id)
    filters  = await get_user_filters(user_id, token) or {}

    req_sent, req_filt = 0, 0
    ids_save: List[str] = []

    async with aiohttp.ClientSession() as session:
        while True:
            users = await _discover_users(session, token, filters)
            if not users: break
            limit_hit = False
            for user in users:
                pid = user.get("_id")
                if pid in blocked or pid in sent_ids:
                    req_filt += 1; continue
                res = await _send_friend_request(session, token, pid)
                if res == "LIMIT":
                    limit_hit = True; break
                if res == "OK":
                    req_sent += 1
                    sent_ids.add(pid)
                    ids_save.append(pid)
                    await set_automation_add_time(user_id, token, pid)
                    await asyncio.sleep(PER_USER_DELAY)
            if is_spam and ids_save:
                await bulk_add_sent_ids(user_id, "request", ids_save)
                ids_save = []
            if limit_hit: break
            await asyncio.sleep(PER_BATCH_DELAY)

    return req_sent, req_filt


# =============================================================================
# MAIN PROCESSOR
# =============================================================================

async def process_account(user_id: int, token_obj: dict, settings: dict, target_tokens: List[dict]):
    token  = token_obj["token"]
    name   = token_obj.get("name", "Acc")[:15]
    bot    = user_bots.get(user_id)
    msg    = status_messages.get(user_id)
    is_all = settings.get("selected_accounts") == "active_only"

    init_account_stats(user_id, token, name)

    db_data = await get_automation_pending_followups(user_id)

    # ── REQUESTS (24h gate) ────────────────────────────────────────────────
    last_req_str = db_data.get("request_times", {}).get(token)
    should_req   = not last_req_str
    if not should_req:
        last_req   = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        should_req = (datetime.utcnow() - last_req).total_seconds() > 24 * 3600

    if should_req:
        update_account_stats(user_id, token, {}, "Sending Requests...")
        await update_ui(user_id)
        req_sent, req_filt = await run_requests_task(user_id, token_obj)
        update_account_stats(user_id, token, {'req_s': req_sent, 'req_f': req_filt}, "Requests Done")
        await set_automation_last_request_time(user_id, token)
        await update_ui(user_id)
        await add_automation_log(user_id, f"[{name}] Requests: {req_sent} sent, {req_filt} filtered")
        db_data = await get_automation_pending_followups(user_id)

    # ── WAVES ──────────────────────────────────────────────────────────────
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
            if elapsed_mins < min_m or elapsed_mins >= max_m: continue
            if await _is_wave_done(db_data, token, pid, wave_key): continue

            # LOUNGE
            if do_lounge and lounge_msg and bot:
                update_account_stats(user_id, token, {}, f"Lounge {wave_key}...")
                await update_ui(user_id)

                # Send on a separate temp message so automation UI is not overwritten
                tmp_msg = await bot.send_message(user_id, "⏳ Lounge sending...", parse_mode="HTML")

                if is_all:
                    await send_lounge_all_tokens(
                        tokens_data=target_tokens,
                        message=lounge_msg,
                        status_message=tmp_msg,
                        bot=bot,
                        chat_id=user_id,
                        spam_enabled=lounge_spam,
                        user_id=user_id,
                    )
                else:
                    await send_lounge(
                        token=token,
                        message=lounge_msg,
                        status_message=tmp_msg,
                        bot=bot,
                        chat_id=user_id,
                        spam_enabled=lounge_spam,
                        user_id=user_id,
                    )
                update_account_stats(user_id, token, {'lng_s': 1})
                await update_ui(user_id)

            # CHATROOM
            if do_chat and chat_msg and bot:
                update_account_stats(user_id, token, {}, f"Chat {wave_key}...")
                await update_ui(user_id)

                # Send on a separate temp message so automation UI is not overwritten
                tmp_msg = await bot.send_message(user_id, "⏳ Chatroom sending...", parse_mode="HTML")

                if is_all:
                    await send_message_to_everyone_all_tokens(
                        tokens=[t["token"] for t in target_tokens],
                        message=chat_msg,
                        status_message=tmp_msg,
                        bot=bot,
                        chat_id=user_id,
                        spam_enabled=chat_spam,
                        token_names={t["token"]: t.get("name", "Acc") for t in target_tokens},
                        use_in_memory_deduplication=chat_spam,
                        user_id=user_id,
                    )
                else:
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
                await update_ui(user_id)

            await _mark_wave_done(user_id, token, pid, wave_key)
            await add_automation_log(user_id, f"[{name}] {wave_key} done")
            break

    update_account_stats(user_id, token, {}, "Done")
    await update_ui(user_id)


# =============================================================================
# MONITOR LOOP
# =============================================================================

async def monitor_loop(user_id: int):
    logger.info(f"Monitor started for {user_id}")

    bot = user_bots.get(user_id)
    if bot:
        try:
            new_msg = await bot.send_message(
                user_id, "🔄 <b>Friend Request Automation</b>\n\nStarting...", parse_mode="HTML"
            )
            status_messages[user_id] = new_msg
        except Exception as e:
            logger.error(f"Initial msg failed: {e}")

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"): break

            selected   = settings.get("selected_accounts", "active_only")
            all_tokens = await get_tokens(user_id)

            if selected == "active_only":
                target_tokens = await get_active_tokens(user_id)
            else:
                # "current" or "all" → use current account only
                current_token = await get_current_account(user_id)
                target_tokens = [t for t in all_tokens if t["token"] == current_token] if current_token else []

            for token_obj in target_tokens:
                if not (await get_automation_settings(user_id)).get("enabled"): break
                await process_account(user_id, token_obj, settings, target_tokens)

            await update_ui(user_id)
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor Error: {e}")
            await asyncio.sleep(60)


# =============================================================================
# CONTROL
# =============================================================================

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
    reset_ui(user_id)
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
