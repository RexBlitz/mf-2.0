"""
Automation Module — Pure Scheduler
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Dict

from db import (
    get_current_account,
    get_automation_settings, get_active_tokens, get_tokens,
    is_already_sent,
    add_automation_log, get_individual_spam_filter,
    set_automation_enabled, set_automation_last_request_time,
    get_automation_pending_followups,
)
from lounge import send_lounge, send_lounge_all_tokens
from chatroom import send_message_to_everyone, send_message_to_everyone_all_tokens
from requests import run_requests, process_all_tokens

logger = logging.getLogger(__name__)

# --- GLOBAL STATE ---
monitor_task: asyncio.Task = None
user_bots: Dict[int, object] = {}

WAVES = [
    ("wave_1_lounge", True,  False, 15,  59),
    ("wave_1_chat",   False, True,  16,  60),
    ("wave_2_lounge", True,  False, 60,  299),
    ("wave_3_lounge", True,  True,  300, 1440),
]


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
# MAIN PROCESSOR
# =============================================================================

async def process_account(user_id: int, token_obj: dict, settings: dict, target_tokens: List[dict], force_run: bool = False):
    token  = token_obj["token"]
    name   = token_obj.get("name", "Acc")[:15]
    bot    = user_bots.get(user_id)
    is_all = settings.get("selected_accounts") == "active_only"

    db_data = await get_automation_pending_followups(user_id)

    # ── REQUESTS (24h gate) ────────────────────────────────────────────────
    last_req_str = db_data.get("request_times", {}).get(token)
    should_req   = force_run or not last_req_str
    if not should_req:
        last_req   = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        should_req = (datetime.utcnow() - last_req).total_seconds() > 24 * 3600

    if should_req and bot:
        if is_all:
            await process_all_tokens(user_id, target_tokens, bot, user_id)
            for t in target_tokens:
                await set_automation_last_request_time(user_id, t["token"])
            await add_automation_log(user_id, "All tokens requests done")
        else:
            await run_requests(user_id, bot, user_id)
            await set_automation_last_request_time(user_id, token)
            await add_automation_log(user_id, f"[{name}] Requests done")

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
            if not force_run and await _is_wave_done(db_data, token, pid, wave_key): continue

            # LOUNGE
            if do_lounge and lounge_msg and bot:
                lounge_status = await bot.send_message(user_id, "⏳ Lounge...", parse_mode="HTML")
                if is_all:
                    await send_lounge_all_tokens(
                        tokens_data=target_tokens, message=lounge_msg,
                        status_message=lounge_status, bot=bot,
                        chat_id=user_id, spam_enabled=lounge_spam, user_id=user_id,
                    )
                else:
                    await send_lounge(
                        token=token, message=lounge_msg,
                        status_message=lounge_status, bot=bot,
                        chat_id=user_id, spam_enabled=lounge_spam, user_id=user_id,
                    )

            # CHATROOM
            if do_chat and chat_msg and bot:
                chat_status = await bot.send_message(user_id, "⏳ Chatroom...", parse_mode="HTML")
                if is_all:
                    await send_message_to_everyone_all_tokens(
                        tokens=[t["token"] for t in target_tokens], message=chat_msg,
                        status_message=chat_status, bot=bot, chat_id=user_id,
                        spam_enabled=chat_spam,
                        token_names={t["token"]: t.get("name", "Acc") for t in target_tokens},
                        use_in_memory_deduplication=chat_spam, user_id=user_id,
                    )
                else:
                    sent_ids      = await is_already_sent(user_id, "chatroom", None, bulk=True) if chat_spam else set()
                    sent_ids_lock = asyncio.Lock()
                    await send_message_to_everyone(
                        token=token, message=chat_msg, chat_id=user_id,
                        spam_enabled=chat_spam, user_id=user_id,
                        sent_ids=sent_ids, sent_ids_lock=sent_ids_lock,
                    )

            await _mark_wave_done(user_id, token, pid, wave_key)
            await add_automation_log(user_id, f"[{name}] {wave_key} done")
            break


# =============================================================================
# MONITOR LOOP
# =============================================================================

async def monitor_loop(user_id: int, force_run: bool = False):
    logger.info(f"Automation started for {user_id}")

    bot = user_bots.get(user_id)
    if bot:
        try:
            await bot.send_message(user_id, "🤖 <b>Automation Started</b>", parse_mode="HTML")
        except: pass

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"): break

            selected   = settings.get("selected_accounts", "active_only")
            all_tokens = await get_tokens(user_id)

            if selected == "active_only":
                target_tokens = await get_active_tokens(user_id)
                if target_tokens:
                    await process_account(user_id, target_tokens[0], settings, target_tokens, force_run=force_run)
            else:
                current_token = await get_current_account(user_id)
                target_tokens = [t for t in all_tokens if t["token"] == current_token] if current_token else []
                if target_tokens:
                    await process_account(user_id, target_tokens[0], settings, target_tokens, force_run=force_run)

            force_run = False
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
    """Run Action Now button — force bypass timestamps."""
    global monitor_task
    if hasattr(status_msg, 'bot'):
        user_bots[user_id] = status_msg.bot
    await set_automation_enabled(user_id, True)
    if monitor_task and not monitor_task.done(): monitor_task.cancel()
    monitor_task = asyncio.create_task(monitor_loop(user_id, force_run=True))

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

def is_automation_running(user_id: int) -> bool:
    global monitor_task
    return monitor_task is not None and not monitor_task.done()
