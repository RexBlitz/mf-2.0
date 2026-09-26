"""
Automation Module — Pure Scheduler

Bugs fixed (original pass):
1. monitor_task is now per-user dict (was single global, broke multi-user)
2. selected_accounts logic was inverted ("active_only" vs "all" mismatch)
3. Wave dedup used hardcoded "ACCOUNT_LEVEL" pid — now uses token as pid so
   each account tracks its own waves independently
4. Status messages from waves are now deleted after completion (no chat spam)
5. send_message_to_everyone called with correct signature + shared lock
6. force_run correctly bypasses wave dedup

Reliability pass (this revision):
7. UI now actually writes "all" for "All Active Accounts" (was "active_only",
   which never matched this module's `== "all"` check — see main.py)
8. Wave scheduling no longer silently skips a wave if the 60s loop is late
   (e.g. DB hiccup, slow Telegram call) — waves now fire on a "min elapsed
   reached and not yet done" basis instead of a strict min/max window, with
   a generous staleness cutoff so very old/abandoned cycles don't fire waves
   out of the blue.
9. Fire-and-forget request tasks (run_requests / process_all_tokens) are now
   wrapped so exceptions are logged to the automation log instead of
   vanishing into asyncio's default exception handler.
10. The "last request time" timestamp is only committed to the DB once the
    request task actually succeeds, so a failed cycle retries on the next
    tick instead of silently waiting 24h. An in-memory in-flight guard
    prevents double-firing while a request task is still running.
11. Automation now checks friend_requests.user_states before firing a
    request task, so it won't collide with a manually-triggered run for the
    same user_id (they used to share the same global state dict).
12. Cancelling an existing monitor loop is now awaited before starting a
    new one, removing the brief window where both could tick concurrently.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List

from db import (
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
from lounge import send_lounge, send_lounge_all_tokens
from chatroom import send_message_to_everyone, send_message_to_everyone_all_tokens
from friend_requests import process_all_tokens, run_requests, user_states

logger = logging.getLogger(__name__)

# --- GLOBAL STATE (per-user) ---
monitor_tasks: Dict[int, asyncio.Task] = {}   # user_id -> Task
user_bots:     Dict[int, object]       = {}   # user_id -> Bot

# Guards against firing a second request cycle for the same account while
# the previous one is still in flight (e.g. loop wakes up again before a
# slow run_requests/process_all_tokens call has finished).
_requests_in_flight: Dict[str, bool] = {}

# How far past the last wave's max window we'll still allow catch-up before
# treating the whole request cycle as stale and not worth messaging for.
STALE_CUTOFF_MINUTES = 24 * 60 * 2  # 48h

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
    from db import _ensure_user_collection_exists, _get_user_collection
    import datetime as dt
    await _ensure_user_collection_exists(user_id)
    user_db = _get_user_collection(user_id)
    await user_db.update_one(
        {"type": "automation_timers"},
        {"$set": {f"lounge_sent.{token}.{token}.{wave_key}": dt.datetime.utcnow()}},
        upsert=True
    )


# =============================================================================
# BACKGROUND TASK TRACKING
# =============================================================================

def _fire_and_track(user_id: int, coro, label: str, in_flight_key: str = None,
                     on_success=None):
    """
    Runs `coro` in the background but makes sure exceptions are actually
    logged (instead of silently going to asyncio's default handler), and
    optionally clears an in-flight guard / runs an on_success callback once
    the task completes.
    """
    async def runner():
        try:
            await coro
            if on_success:
                await on_success()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{user_id}] {label} failed: {e}")
            try:
                await add_automation_log(user_id, f"❌ {label} failed: {e}")
            except Exception:
                pass
        finally:
            if in_flight_key is not None:
                _requests_in_flight.pop(in_flight_key, None)

    return asyncio.create_task(runner())


# =============================================================================
# MAIN PROCESSOR
# =============================================================================

async def process_account(user_id: int, token_obj: dict, settings: dict, target_tokens: List[dict], force_run: bool = False):
    token  = token_obj["token"]
    name   = token_obj.get("name", "Acc")[:15]
    bot    = user_bots.get(user_id)

    # "all" means all active tokens; anything else means current account only
    is_all = settings.get("selected_accounts") == "all"

    db_data = await get_automation_pending_followups(user_id)
    now = datetime.utcnow()

    # ── 1. REQUESTS TRIGGER LOGIC ──────────────────────────────────────────
    last_req_str = db_data.get("request_times", {}).get(token)
    should_req   = force_run or not last_req_str

    if not should_req:
        last_req = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
        should_req = (now - last_req).total_seconds() > 24 * 3600

    in_flight_key = f"{user_id}:{'ALL' if is_all else token}"

    if should_req and bot and not _requests_in_flight.get(in_flight_key):
        # Don't stomp on a manually-triggered run using the same global state
        # (run_requests/process_all_tokens key off user_states[user_id]).
        manual_state = user_states.get(user_id)
        if manual_state and manual_state.get("running"):
            await add_automation_log(user_id, f"[{name}] Skipped: manual run already in progress")
        else:
            _requests_in_flight[in_flight_key] = True

            async def _mark_success(tokens_to_stamp=None):
                if tokens_to_stamp:
                    for t in tokens_to_stamp:
                        await set_automation_last_request_time(user_id, t["token"])
                else:
                    await set_automation_last_request_time(user_id, token)

            if is_all:
                await add_automation_log(user_id, "All tokens requests triggered")
                _fire_and_track(
                    user_id,
                    process_all_tokens(user_id, target_tokens, bot, user_id),
                    "All-tokens requests",
                    in_flight_key=in_flight_key,
                    on_success=lambda: _mark_success(target_tokens),
                )
            else:
                await add_automation_log(user_id, f"[{name}] Requests triggered")
                _fire_and_track(
                    user_id,
                    run_requests(user_id, bot, user_id),
                    f"[{name}] Requests",
                    in_flight_key=in_flight_key,
                    on_success=_mark_success,
                )

            # Optimistically treat "now" as the request time for wave timing
            # purposes this tick; if the task actually fails, the DB
            # timestamp won't be written and the next tick will retry
            # should_req from scratch (since request_times.{token} stays
            # unset / stale).
            last_req_str = now

    # ── 2. WAVES LOGIC ─────────────────────────────────────────────────────
    if not last_req_str:
        return

    last_req_dt  = last_req_str if isinstance(last_req_str, datetime) else datetime.fromisoformat(str(last_req_str))
    elapsed_mins = (now - last_req_dt).total_seconds() / 60

    if elapsed_mins > STALE_CUTOFF_MINUTES and not force_run:
        # Request cycle is old enough (loop was likely down/paused for a
        # long stretch) that firing a wave now wouldn't make sense.
        return

    lounge_msg  = settings.get("lounge_message")
    chat_msg    = settings.get("chatroom_message")
    lounge_spam = await get_individual_spam_filter(user_id, "lounge")
    chat_spam   = await get_individual_spam_filter(user_id, "chatroom")

    for wave_key, do_lounge, do_chat, min_m, _max_m in WAVES:
        # Catch-up scheduling: fire the first not-yet-done wave whose
        # minimum delay has elapsed, rather than requiring the loop to have
        # ticked inside a narrow window. This means a late tick (DB hiccup,
        # slow Telegram call, brief downtime) still catches up instead of
        # permanently skipping that wave.
        if elapsed_mins < min_m:
            continue

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
            except Exception as e:
                logger.error(f"[{user_id}] {wave_key} lounge send failed: {e}")
                await add_automation_log(user_id, f"❌ [{name}] {wave_key} lounge failed: {e}")
            finally:
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
                    sent_ids = await is_already_sent(user_id, "chatroom", None, bulk=True) if chat_spam else set()
                    lock = asyncio.Lock()
                    await send_message_to_everyone(token, chat_msg, user_id, chat_spam, user_id, sent_ids, lock)
            except Exception as e:
                logger.error(f"[{user_id}] {wave_key} chatroom send failed: {e}")
                await add_automation_log(user_id, f"❌ [{name}] {wave_key} chatroom failed: {e}")
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

    consecutive_errors = 0

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"):
                break

            selected   = settings.get("selected_accounts", "all")
            all_tokens = await get_tokens(user_id)

            # "all" → use all active tokens; anything else → current account only
            if selected == "all":
                target_tokens = await get_active_tokens(user_id)
                # Process waves for every active token independently
                for token_obj in target_tokens:
                    await process_account(user_id, token_obj, settings, target_tokens, force_run=force_run)
            else:
                current_token = await get_current_account(user_id)
                target_tokens = [t for t in all_tokens if t["token"] == current_token] if current_token else []
                if target_tokens:
                    await process_account(user_id, target_tokens[0], settings, target_tokens, force_run=force_run)

            force_run = False
            consecutive_errors = 0
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Monitor Error for {user_id} (#{consecutive_errors}): {e}")
            # Exponential backoff on repeated failures (e.g. DB down), capped
            # at 10 minutes, so we don't hammer a broken dependency every
            # 60s forever.
            backoff = min(60 * (2 ** (consecutive_errors - 1)), 600)
            await asyncio.sleep(backoff)


# =============================================================================
# CONTROL  (per-user task dict; cancellation is awaited before restarting)
# =============================================================================

async def _cancel_existing(user_id: int):
    existing = monitor_tasks.get(user_id)
    if existing and not existing.done():
        existing.cancel()
        try:
            await existing
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

async def run_automation_action(user_id: int, status_msg):
    """Run Action Now button — force bypass timestamps."""
    if hasattr(status_msg, "bot"):
        user_bots[user_id] = status_msg.bot
    await set_automation_enabled(user_id, True)
    await _cancel_existing(user_id)
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id, force_run=True))

async def start_automation(user_id: int, bot):
    user_bots[user_id] = bot
    await set_automation_enabled(user_id, True)
    existing = monitor_tasks.get(user_id)
    if existing and not existing.done():
        return  # already running for THIS user
    monitor_tasks[user_id] = asyncio.create_task(monitor_loop(user_id))

async def stop_automation(user_id: int):
    await set_automation_enabled(user_id, False)
    await _cancel_existing(user_id)
    monitor_tasks.pop(user_id, None)

def is_automation_running(user_id: int) -> bool:
    task = monitor_tasks.get(user_id)
    return task is not None and not task.done()
