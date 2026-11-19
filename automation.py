"""
Automation Module — Pure Scheduler

Fixes:
1. Per-user monitor_tasks and active_request_tasks prevent duplicate concurrent runs.
2. selected_accounts treats both "all" and "active_only" as fleet mode, while "current" runs the current account only.
3. Fleet mode runs process_all_tokens, send_lounge_all_tokens, and send_message_to_everyone_all_tokens ONCE per trigger (not N times in a loop).
4. Wave dedup is tracked cleanly per token (and fleet waves mark every active token once complete).
5. Transient status messages are deleted after completion to avoid Telegram chat spam.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List

from chatroom import send_message_to_everyone, send_message_to_everyone_all_tokens
from db import (
    _ensure_user_collection_exists,
    _get_user_collection,
    add_automation_log,
    get_active_tokens,
    get_automation_pending_followups,
    get_automation_settings,
    get_current_account,
    get_individual_spam_filter,
    get_tokens,
    is_already_sent,
    set_automation_enabled,
    set_automation_last_request_time,
)
from friend_requests import process_all_tokens, run_requests
from lounge import send_lounge, send_lounge_all_tokens

logger = logging.getLogger(__name__)

# --- GLOBAL STATE (per-user) ---
monitor_tasks: Dict[int, asyncio.Task] = {}         # user_id -> monitor Task
active_request_tasks: Dict[int, asyncio.Task] = {}  # user_id -> active request Task
user_bots: Dict[int, object] = {}                   # user_id -> Bot

WAVES = [
    ("wave_1_lounge", True,  False, 15,  59),
    ("wave_1_chat",   False, True,  16,  60),
    ("wave_2_lounge", True,  False, 60,  299),
    ("wave_3_lounge", True,  True,  300, 1440),
]


def _parse_dt(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except Exception:
        return None


def _is_task_busy(user_id: int) -> bool:
    task = active_request_tasks.get(user_id)
    return task is not None and not task.done()


# =============================================================================
# WAVE TRACKING  (pid = token so each account has independent wave state)
# =============================================================================

async def _is_wave_done(db_data: dict, token: str, wave_key: str) -> bool:
    return wave_key in db_data.get("lounge_sent", {}).get(token, {}).get(token, {})


async def _mark_wave_done(user_id: int, token: str, wave_key: str):
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"lounge_sent.{token}.{token}.{wave_key}": datetime.utcnow()}},
        upsert=True,
    )


async def _run_wave_followups(
    user_id: int,
    bot,
    lounge_msg: str | None,
    chat_msg: str | None,
    lounge_spam: bool,
    chat_spam: bool,
    tokens: List[dict],
    is_all: bool,
    anchor_dt: datetime,
    db_data: dict,
    force_run: bool,
) -> None:
    if not bot or not tokens:
        return

    now = datetime.utcnow()
    elapsed_mins = (now - anchor_dt).total_seconds() / 60.0

    for wave_key, do_lounge, do_chat, min_m, max_m in WAVES:
        if not force_run and (elapsed_mins < min_m or elapsed_mins >= max_m):
            continue

        if not force_run and all(await _is_wave_done(db_data, t["token"], wave_key) for t in tokens):
            continue

        if do_lounge and lounge_msg:
            status_msg = await bot.send_message(user_id, f"⏳ {wave_key} Lounge...", parse_mode="HTML")
            try:
                if is_all and len(tokens) > 1:
                    await send_lounge_all_tokens(tokens, lounge_msg, status_msg, bot, user_id, lounge_spam, user_id)
                else:
                    await send_lounge(tokens[0]["token"], lounge_msg, status_msg, bot, user_id, lounge_spam, user_id)
            finally:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

        if do_chat and chat_msg:
            status_msg = await bot.send_message(user_id, f"⏳ {wave_key} Chatroom...", parse_mode="HTML")
            try:
                if is_all and len(tokens) > 1:
                    token_names = {t["token"]: t.get("name", "Acc") for t in tokens}
                    await send_message_to_everyone_all_tokens(
                        [t["token"] for t in tokens],
                        chat_msg,
                        status_msg,
                        bot,
                        user_id,
                        chat_spam,
                        token_names,
                        chat_spam,
                        user_id,
                    )
                else:
                    sent_ids = await is_already_sent(user_id, "chatroom", None, bulk=True) if chat_spam else set()
                    lock = asyncio.Lock()
                    await send_message_to_everyone(tokens[0]["token"], chat_msg, user_id, chat_spam, user_id, sent_ids, lock)
            finally:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

        for t in tokens:
            await _mark_wave_done(user_id, t["token"], wave_key)
        label = "Fleet" if is_all else tokens[0].get("name", "Acc")[:15]
        await add_automation_log(user_id, f"[{label}] {wave_key} triggered")
        break


# =============================================================================
# MAIN PROCESSORS
# =============================================================================

async def process_fleet(user_id: int, target_tokens: List[dict], settings: dict, force_run: bool = False):
    """Run automation once across the entire active fleet instead of N duplicate passes."""
    if not target_tokens:
        return
    bot = user_bots.get(user_id)
    if not bot:
        return

    db_data = await get_automation_pending_followups(user_id)
    now = datetime.utcnow()
    req_times = db_data.get("request_times", {})

    parsed_times = [_parse_dt(req_times.get(t["token"])) for t in target_tokens]
    needs_requests = force_run or any(
        dt is None or (now - dt).total_seconds() > 24 * 3600 for dt in parsed_times
    )

    if needs_requests and not _is_task_busy(user_id):
        for t in target_tokens:
            await set_automation_last_request_time(user_id, t["token"])
        await add_automation_log(user_id, f"Fleet requests triggered ({len(target_tokens)} accounts)")
        active_request_tasks[user_id] = asyncio.create_task(
            process_all_tokens(user_id, target_tokens, bot, user_id)
        )
        anchor_dt = now
    else:
        valid_dts = [dt for dt in parsed_times if dt is not None]
        if not valid_dts:
            return
        anchor_dt = max(valid_dts)

    lounge_msg = settings.get("lounge_message")
    chat_msg = settings.get("chatroom_message")
    lounge_spam = await get_individual_spam_filter(user_id, "lounge")
    chat_spam = await get_individual_spam_filter(user_id, "chatroom")

    await _run_wave_followups(
        user_id=user_id,
        bot=bot,
        lounge_msg=lounge_msg,
        chat_msg=chat_msg,
        lounge_spam=lounge_spam,
        chat_spam=chat_spam,
        tokens=target_tokens,
        is_all=True,
        anchor_dt=anchor_dt,
        db_data=db_data,
        force_run=force_run,
    )


async def process_account(user_id: int, token_obj: dict, settings: dict, force_run: bool = False):
    """Run automation for a single selected account."""
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:15]
    bot = user_bots.get(user_id)
    if not bot:
        return

    db_data = await get_automation_pending_followups(user_id)
    now = datetime.utcnow()
    last_req_dt = _parse_dt(db_data.get("request_times", {}).get(token))
    should_req = force_run or not last_req_dt or (now - last_req_dt).total_seconds() > 24 * 3600

    if should_req and not _is_task_busy(user_id):
        await set_automation_last_request_time(user_id, token)
        await add_automation_log(user_id, f"[{name}] Requests triggered")
        active_request_tasks[user_id] = asyncio.create_task(run_requests(user_id, bot, user_id))
        last_req_dt = now

    if not last_req_dt:
        return

    lounge_msg = settings.get("lounge_message")
    chat_msg = settings.get("chatroom_message")
    lounge_spam = await get_individual_spam_filter(user_id, "lounge")
    chat_spam = await get_individual_spam_filter(user_id, "chatroom")

    await _run_wave_followups(
        user_id=user_id,
        bot=bot,
        lounge_msg=lounge_msg,
        chat_msg=chat_msg,
        lounge_spam=lounge_spam,
        chat_spam=chat_spam,
        tokens=[token_obj],
        is_all=False,
        anchor_dt=last_req_dt,
        db_data=db_data,
        force_run=force_run,
    )


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

            selected = settings.get("selected_accounts", "all")
            if selected in ("all", "active_only"):
                target_tokens = await get_active_tokens(user_id)
                if target_tokens:
                    await process_fleet(user_id, target_tokens, settings, force_run=force_run)
            else:
                all_tokens = await get_tokens(user_id)
                current_token = await get_current_account(user_id)
                target_tokens = [t for t in all_tokens if t["token"] == current_token] if current_token else []
                if target_tokens:
                    await process_account(user_id, target_tokens[0], settings, force_run=force_run)

            force_run = False
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor Error for {user_id}: {e}")
            await asyncio.sleep(60)


# =============================================================================
# CONTROL
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
        return
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id))


def stop_automation(user_id: int):
    asyncio.create_task(set_automation_enabled(user_id, False))
    existing = monitor_tasks.pop(user_id, None)
    if existing:
        existing.cancel()


def is_automation_running(user_id: int) -> bool:
    task = monitor_tasks.get(user_id)
    return task is not None and not task.done()
