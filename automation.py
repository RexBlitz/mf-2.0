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
from friend_requests import run_requests, process_all_tokens

logger = logging.getLogger(__name__)

# --- GLOBAL STATE (per-user) ---
monitor_tasks: Dict[int, asyncio.Task] = {}   # user_id -> Task
user_bots:     Dict[int, object]       = {}   # user_id -> Bot

WAVES = [
    ("wave_1_lounge", True,  False, 15,  59),
    ("wave_1_chat",   False, True,  16,  60),
    ("wave_2_lounge", True,  False, 60,  299),
    ("wave_3_lounge", True,  True,  300, 1440),
]


# =============================================================================
# WAVE TRACKING  (pid = token so each account has independent wave state)
# =============================================================================

async def _is_wave_done(db_data: dict, token: str, wave_key: str) -> bool:
    return wave_key in db_data.get("lounge_sent", {}).get(token, {}).get(token, {})

async def _mark_wave_done(user_id: int, token: str, wave_key: str):
    from db import _get_user_collection, _ensure_user_collection_exists
    import datetime as dt
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"lounge_sent.{token}.{token}.{wave_key}": dt.datetime.utcnow()}},
        upsert=True
    )


# =============================================================================
# MAIN PROCESSOR
# =============================================================================

async def process_account(user_id: int, token_obj: dict, settings: dict, target_tokens: List[dict], force_run: bool = False):
    token  = token_obj["token"]
    name   = token_obj.get("name", "Acc")[:15]
    bot    = user_bots.get(user_id)

    # FIX: "all" means all active tokens; anything else means current account only
    is_all = settings.get("selected_accounts") == "all"

    db_data = await get_automation_pending_followups(user_id)
    now = datetime.utcnow()

    # ── 1. REQUESTS TRIGGER LOGIC ──────────────────────────────────────────
    last_req_str = db_data.get("request_times", {}).get(token)
    should_req   = force_run or not last_req_str

    if not should_req:
        last_req = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        should_req = (now - last_req).total_seconds() > 24 * 3600

    if should_req and bot:
        if is_all:
            for t in target_tokens:
                await set_automation_last_request_time(user_id, t["token"])
            await add_automation_log(user_id, "All tokens requests triggered")
            asyncio.create_task(process_all_tokens(user_id, target_tokens, bot, user_id))
        else:
            await set_automation_last_request_time(user_id, token)
            await add_automation_log(user_id, f"[{name}] Requests triggered")
            asyncio.create_task(run_requests(user_id, bot, user_id))

        last_req_str = now

    # ── 2. WAVES LOGIC ─────────────────────────────────────────────────────
    if not last_req_str:
        return

    last_req_dt  = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
    elapsed_mins = (now - last_req_dt).total_seconds() / 60

    lounge_msg  = settings.get("lounge_message")
    chat_msg    = settings.get("chatroom_message")
    lounge_spam = await get_individual_spam_filter(user_id, "lounge")
    chat_spam   = await get_individual_spam_filter(user_id, "chatroom")

    for wave_key, do_lounge, do_chat, min_m, max_m in WAVES:
        if elapsed_mins < min_m or elapsed_mins >= max_m:
            continue

        # FIX: dedup per-token (not hardcoded "ACCOUNT_LEVEL")
        if not force_run and await _is_wave_done(db_data, token, wave_key):
            continue

        # Lounge Trigger
        if do_lounge and lounge_msg and bot:
            status_msg = await bot.send_message(user_id, f"⏳ {wave_key} Lounge...", parse_mode="HTML")
            try:
                if is_all:
                    await send_lounge_all_tokens(target_tokens, lounge_msg, status_msg, bot, user_id, lounge_spam, user_id)
                else:
                    await send_lounge(token, lounge_msg, status_msg, bot, user_id, lounge_spam, user_id)
            finally:
                # FIX: clean up status message so chat doesn't fill up
                try:
                    await status_msg.delete()
                except Exception:
                    pass

        # Chatroom Trigger
        if do_chat and chat_msg and bot:
            status_msg = await bot.send_message(user_id, f"⏳ {wave_key} Chatroom...", parse_mode="HTML")
            try:
                if is_all:
                    token_names = {t["token"]: t.get("name", "Acc") for t in target_tokens}
                    await send_message_to_everyone_all_tokens(
                        [t["token"] for t in target_tokens],
                        chat_msg, status_msg, bot, user_id,
                        chat_spam, token_names, chat_spam, user_id
                    )
                else:
                    # FIX: correct signature — shared lock, no extra sent_ids prefetch
                    sent_ids = await is_already_sent(user_id, "chatroom", None, bulk=True) if chat_spam else set()
                    lock = asyncio.Lock()
                    await send_message_to_everyone(token, chat_msg, user_id, chat_spam, user_id, sent_ids, lock)
            finally:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

        await _mark_wave_done(user_id, token, wave_key)
        await add_automation_log(user_id, f"[{name}] {wave_key} triggered")
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
        except Exception:
            pass

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"):
                break

            selected   = settings.get("selected_accounts", "all")
            all_tokens = await get_tokens(user_id)

            # FIX: "all" → use all active tokens; anything else → current account only
            if selected == "all":
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
            logger.error(f"Monitor Error for {user_id}: {e}")
            await asyncio.sleep(60)


# =============================================================================
# CONTROL  (FIX: per-user task dict instead of single global)
# =============================================================================

async def run_automation_action(user_id: int, status_msg):
    """Run Action Now button — force bypass timestamps."""
    if hasattr(status_msg, "bot"):
        user_bots[user_id] = status_msg.bot
    await set_automation_enabled(user_id, True)
    existing = monitor_tasks.get(user_id)
    if existing and not existing.done():
        existing.cancel()
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id, force_run=True))

def start_automation(user_id: int, bot):
    user_bots[user_id] = bot
    asyncio.create_task(set_automation_enabled(user_id, True))
    existing = monitor_tasks.get(user_id)
    if existing and not existing.done():
        return  # already running for THIS user
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id))

def stop_automation(user_id: int):
    asyncio.create_task(set_automation_enabled(user_id, False))
    existing = monitor_tasks.pop(user_id, None)
    if existing:
        existing.cancel()

def is_automation_running(user_id: int) -> bool:
    task = monitor_tasks.get(user_id)
    return task is not None and not task.done()
