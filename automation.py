"""
Automation Module - Sequential Processing with Multi-Message Support (Comma Split).
- Runs one account at a time.
- 24h Cycle + Follow-up Waves.
- Feature: Splits messages by comma (",") and sends them sequentially.

ROOT CAUSE FIXES:
  1. lng_f inflating / nothing sent to lounge:
       The original used /lounge/create/v1/ (post on someone's lounge WALL) which
       FAILS for matched/added users — that endpoint only works for discovery users.
       After a friend request is accepted the correct API is the chatroom flow
       (/chatroom/open/v2 + /chat/send/v2), exactly what lounge.py does.
       Follow-up now sends lounge_message THEN chatroom_message both via chatroom,
       in one open session — identical to lounge.py's open_chatroom_and_send().

  2. Spam filter for follow-up messages was completely missing:
       Added is_already_sent check keyed on "chatroom" category (all follow-up
       messages go via chatroom API). Individual spam_filter_chatroom flag respected.
       Successful IDs are bulk-saved back to sent_records after each account run.

  3. db_data was fetched once and reused stale across all accounts in the loop:
       db_data is now fetched fresh inside process_account_sequence so each account
       sees up-to-date lounge_sent / chatroom_sent wave history.

  4. chatroom_sent history not checked before sending:
       chat_hist (from automation_timers) is checked so chatroom is sent at most once
       per person regardless of which wave fires.
"""

import asyncio
import aiohttp
import logging
from datetime import datetime
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

ui_stats_state: Dict[int, Dict[str, Dict]] = {}

# --- TIMINGS ---
PER_USER_DELAY = 0.5
PER_BATCH_DELAY = 1
MULTI_MSG_DELAY = 1.5  # Delay between comma-separated message parts

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}


# ---------------------------------------------------------------------------
# API HELPERS
# ---------------------------------------------------------------------------

async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[Dict]:
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    params = {"lng": "71.9140141", "unreachableUserIds": "", "lat": "29.6264544", "locale": "en"}
    if filters and filters.get("filterNationalityCode"):
        params["filterNationalityCode"] = filters["filterNationalityCode"]
    try:
        async with session.get(url, headers=headers, params=params, timeout=10) as resp:
            return (await resp.json()).get("users", []) if resp.status == 200 else []
    except Exception as e:
        logger.error(f"Discover users error: {e}")
        return []


async def _send_friend_request(session, token, person_id):
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    try:
        async with session.get(url, headers={**BASE_HEADERS, 'meeff-access-token': token}, timeout=10) as resp:
            data = await resp.json()
            if data.get("errorCode") == "LikeExceeded":
                return "LIMIT"
            return "FAIL" if data.get("errorCode") else "OK"
    except Exception as e:
        logger.error(f"Friend request error: {e}")
        return "FAIL"


async def _send_messages_via_chatroom(
    session: aiohttp.ClientSession,
    token: str,
    person_id: str,
    lounge_msg: str,
    chat_msg: str,
):
    """
    Opens a chatroom with person_id ONCE, then sends:
      - all comma-parts of lounge_msg
      - all comma-parts of chat_msg
    in sequence — identical to lounge.py's open_chatroom_and_send().

    Returns:
      True        — at least one message delivered
      "DISABLED"  — user has disabled chatroom (HTTP 412)
      False       — connection / API failure
    """
    headers = {
        **BASE_HEADERS,
        'meeff-access-token': token,
        'Content-Type': "application/json",
    }

    # 1. Open chatroom ONCE
    chatroom_id = None
    try:
        async with session.post(
            "https://api.meeff.com/chatroom/open/v2",
            headers=headers,
            json={"waitingRoomId": person_id, "locale": "en"},
            timeout=10,
        ) as resp:
            if resp.status == 412:
                return "DISABLED"
            if resp.status != 200:
                logger.warning(f"Open chatroom failed for {person_id}: status {resp.status}")
                return False
            chatroom_id = (await resp.json()).get("chatRoom", {}).get("_id")
    except Exception as e:
        logger.error(f"Open chatroom exception for {person_id}: {e}")
        return False

    if not chatroom_id:
        logger.warning(f"No chatroom_id returned for {person_id}")
        return False

    # 2. Build ordered list of all message parts
    all_parts: List[str] = []
    for raw in (lounge_msg, chat_msg):
        if raw:
            all_parts.extend([p.strip() for p in raw.split(',') if p.strip()])

    if not all_parts:
        return False

    # 3. Send each part
    any_success = False
    for i, part in enumerate(all_parts):
        try:
            async with session.post(
                "https://api.meeff.com/chat/send/v2",
                headers=headers,
                json={"chatRoomId": chatroom_id, "message": part, "locale": "en"},
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    any_success = True
                else:
                    logger.warning(f"Chat send part {i+1} failed for {person_id}: status {resp.status}")
        except Exception as e:
            logger.error(f"Chat send part {i+1} exception for {person_id}: {e}")

        if i < len(all_parts) - 1:
            await asyncio.sleep(MULTI_MSG_DELAY)

    return any_success


# ---------------------------------------------------------------------------
# UI MANAGER
# ---------------------------------------------------------------------------

def init_account_stats(user_id, token, name):
    if user_id not in ui_stats_state:
        ui_stats_state[user_id] = {}
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
    if user_id not in ui_stats_state or not ui_stats_state[user_id]:
        return

    text = "🔄 <b>Friend Request Automation</b>\n\n"
    total_req_s = total_req_f = 0
    total_lng_s = total_lng_f = 0
    total_chat_s = total_chat_f = 0

    for token, stats in ui_stats_state[user_id].items():
        total_req_s += stats['req_s'];  total_req_f += stats['req_f']
        total_lng_s += stats['lng_s'];  total_lng_f += stats['lng_f']
        total_chat_s += stats['chat_s']; total_chat_f += stats['chat_f']

        text += f"<b>{stats['name']}</b> ({stats['status']})\n"
        text += (
            f"Req: {stats['req_s']} / {stats['req_f']} | "
            f"Lng: {stats['lng_s']} / {stats['lng_f']} | "
            f"Chat: {stats['chat_s']} / {stats['chat_f']}\n\n"
        )

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
            except Exception:
                pass
        return

    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        if bot:
            try:
                new_msg = await bot.send_message(user_id, text, parse_mode="HTML")
                status_messages[user_id] = new_msg
            except Exception:
                pass


def reset_ui(user_id):
    ui_stats_state[user_id] = {}


# ---------------------------------------------------------------------------
# SEQUENTIAL PROCESSOR
# ---------------------------------------------------------------------------

async def process_account_sequence(
    user_id: int, token_obj: dict, settings: dict, session: aiohttp.ClientSession
):
    """
    Handles one account per cycle:
      - If 24 h has passed  → run friend requests
      - Otherwise           → run follow-up waves via chatroom
    """
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:15]

    init_account_stats(user_id, token, name)

    # FIX: Fetch fresh db_data per-account so wave history is never stale.
    db_data = await get_automation_pending_followups(user_id)

    # ------------------------------------------------------------------ #
    # 1. 24-HOUR REQUEST CYCLE CHECK
    # ------------------------------------------------------------------ #
    last_req_str = db_data.get("request_times", {}).get(token)
    should_run_requests = False

    if not last_req_str:
        should_run_requests = True
    else:
        last_req = (
            last_req_str
            if isinstance(last_req_str, datetime)
            else datetime.fromisoformat(str(last_req_str))
        )
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

    # ------------------------------------------------------------------ #
    # 2. FOLLOW-UP WAVES
    # ------------------------------------------------------------------ #
    lounge_msg = settings.get("lounge_message") or ""
    chat_msg   = settings.get("chatroom_message") or ""

    if not lounge_msg and not chat_msg:
        update_account_stats(user_id, token, {}, "No Msg Set")
        await update_ui(user_id)
        return

    # Spam filter: uses "chatroom" category because all messages go via chatroom API
    is_chat_spam_on = await get_individual_spam_filter(user_id, "chatroom")
    chatroom_sent_ids: set = (
        await is_already_sent(user_id, "chatroom", None, bulk=True)
        if is_chat_spam_on
        else set()
    )

    added_users = db_data.get("add_times", {}).get(token, {})
    lounge_hist = db_data.get("lounge_sent", {}).get(token, {})   # wave gate
    chat_hist   = db_data.get("chatroom_sent", {}).get(token, {}) # one-time gate

    now = datetime.utcnow()
    messages_sent = 0
    new_chatroom_ids: List[str] = []

    for pid, add_time_str in added_users.items():
        add_time = (
            add_time_str
            if isinstance(add_time_str, datetime)
            else datetime.fromisoformat(str(add_time_str))
        )
        elapsed_mins = (now - add_time).total_seconds() / 60
        user_wave_hist = lounge_hist.get(pid, {})

        # Determine which wave is due
        wave_to_run = 0
        if   elapsed_mins >= 20  and "wave_1" not in user_wave_hist and elapsed_mins < 120:
            wave_to_run = 1
        elif elapsed_mins >= 60  and "wave_2" not in user_wave_hist and elapsed_mins < 300:
            wave_to_run = 2
        elif elapsed_mins >= 360 and "wave_3" not in user_wave_hist and elapsed_mins < 1500:
            wave_to_run = 3

        if wave_to_run == 0:
            continue

        update_account_stats(user_id, token, {}, f"Wave {wave_to_run}")
        if messages_sent % 5 == 0:
            await update_ui(user_id)

        # ---------------------------------------------------------------- #
        # SPAM FILTER: skip pid if already messaged globally
        # ---------------------------------------------------------------- #
        if is_chat_spam_on and pid in chatroom_sent_ids:
            update_account_stats(user_id, token, {'lng_f': 1, 'chat_f': 1})
            continue

        # ---------------------------------------------------------------- #
        # SEND: lounge_msg + chat_msg both via chatroom (one open session)
        # This is the CORRECT API for matched users — same as lounge.py
        # ---------------------------------------------------------------- #
        result = await _send_messages_via_chatroom(
            session, token, pid, lounge_msg, chat_msg
        )

        if result == "DISABLED":
            # User disabled chat — filtered, not failed
            update_account_stats(user_id, token, {'lng_f': 1, 'chat_f': 1})

        elif result:
            update_account_stats(user_id, token, {'lng_s': 1, 'chat_s': 1})

            # Mark wave in automation_timers (prevents re-sending same wave)
            await mark_lounge_sent(user_id, token, pid, wave_to_run)

            # Mark chatroom sent in automation_timers (one-per-person gate)
            if pid not in chat_hist:
                await mark_chatroom_sent(user_id, token, pid)

            # Keep global sent_records in sync for spam filter
            if is_chat_spam_on:
                chatroom_sent_ids.add(pid)
                new_chatroom_ids.append(pid)

            messages_sent += 1
            await asyncio.sleep(PER_USER_DELAY)

        else:
            # Real API / network failure
            update_account_stats(user_id, token, {'lng_f': 1, 'chat_f': 1})

    # Bulk-save new IDs so spam filter is consistent on next cycle
    if is_chat_spam_on and new_chatroom_ids:
        await bulk_add_sent_ids(user_id, "chatroom", new_chatroom_ids)

    if messages_sent > 0:
        await add_automation_log(user_id, f"[{name}] Follow-ups: {messages_sent} sent")

    update_account_stats(user_id, token, {}, "Done")
    await update_ui(user_id)


# ---------------------------------------------------------------------------
# FRIEND REQUEST TASK
# ---------------------------------------------------------------------------

async def run_requests_task(user_id, token_obj, session):
    token = token_obj["token"]
    name = token_obj.get("name", "Acc")[:10]

    is_spam_on = await get_individual_spam_filter(user_id, "request")
    blocked = await get_blocked_users(user_id)
    sent_ids = (
        await is_already_sent(user_id, "request", None, bulk=True)
        if is_spam_on
        else set()
    )

    await apply_filter_for_account(token, user_id)
    filters = await get_user_filters(user_id, token) or {}

    ids_to_save = []

    while True:
        users = await _discover_users(session, token, filters)
        if not users:
            break

        limit_hit = False
        for user in users:
            pid = user.get("_id")
            if pid in blocked or pid in sent_ids:
                update_account_stats(user_id, token, {'req_f': 1})
                continue

            res = await _send_friend_request(session, token, pid)
            if res == "LIMIT":
                limit_hit = True
                break

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

    req_count = ui_stats_state.get(user_id, {}).get(token, {}).get('req_s', 0)
    if req_count > 0:
        await add_automation_log(user_id, f"[{name}] Requests: {req_count}")


# ---------------------------------------------------------------------------
# MAIN MONITOR LOOP
# ---------------------------------------------------------------------------

async def monitor_loop(user_id: int):
    logger.info(f"Monitor started for user {user_id}")
    await update_ui(user_id, force_new=True)

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"):
                break

            selected = settings.get("selected_accounts", "all")
            all_tokens = await get_tokens(user_id)

            if selected == "all":
                target_tokens = all_tokens
            elif selected == "active_only":
                target_tokens = await get_active_tokens(user_id)
            else:
                target_tokens = [all_tokens[i] for i in selected if 0 <= i < len(all_tokens)]

            async with aiohttp.ClientSession() as session:
                for token_obj in target_tokens:
                    if not (await get_automation_settings(user_id)).get("enabled"):
                        break
                    await process_account_sequence(user_id, token_obj, settings, session)

            await update_ui(user_id)
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor error for user {user_id}: {e}")
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# CONTROL
# ---------------------------------------------------------------------------

async def run_automation_action(user_id: int, status_msg):
    global monitor_task
    status_messages[user_id] = status_msg
    if status_msg and hasattr(status_msg, 'bot'):
        user_bots[user_id] = status_msg.bot
    await set_automation_enabled(user_id, True)
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    reset_ui(user_id)
    monitor_task = asyncio.create_task(monitor_loop(user_id))


def start_automation(user_id: int, bot):
    global monitor_task
    user_bots[user_id] = bot
    asyncio.create_task(set_automation_enabled(user_id, True))
    if monitor_task and not monitor_task.done():
        return
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
            try:
                await msg.edit_text("🛑 <b>Automation Stopped</b>", parse_mode="HTML")
            except Exception:
                pass

    asyncio.create_task(_safe_stop_msg())


def is_automation_running(user_id: int) -> bool:
    global monitor_task
    return monitor_task is not None and not monitor_task.done()
