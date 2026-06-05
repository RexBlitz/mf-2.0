import asyncio
import aiohttp
import logging
import html
from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import (
    get_individual_spam_filter,
    bulk_add_sent_ids,
    get_active_tokens,
    get_current_account,
    get_already_sent_ids,
    get_exclude_filter,
    get_exclude_filter_enabled,
    get_user_filters
)
from filters import is_request_filter_enabled
from collections import defaultdict
from dateutil import parser
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

PER_USER_DELAY = 0.5
PER_BATCH_DELAY = 1
EMPTY_BATCH_DELAY = 2
PER_ERROR_DELAY = 5

async def _push_filters(session, user_id, token):
    """Push stored filter settings to Meeff API using existing session."""
    try:
        user_filters = await get_user_filters(user_id, token) or {}
        filter_data = {
            "filterGenderType": user_filters.get("filterGenderType", 5),
            "filterBirthYearFrom": user_filters.get("filterBirthYearFrom", 1979),
            "filterBirthYearTo": 2006,
            "filterDistance": 510,
            "filterLanguageCodes": user_filters.get("filterLanguageCodes", ""),
            "filterNationalityBlock": user_filters.get("filterNationalityBlock", 0),
            "filterNationalityCode": user_filters.get("filterNationalityCode", ""),
            "locale": "en"
        }
        headers = {
            'User-Agent': "okhttp/5.1.0",
            'Accept-Encoding': "gzip",
            'meeff-access-token': token,
            'content-type': "application/json; charset=utf-8"
        }
        async with session.post("https://api.meeff.com/user/updateFilter/v1", json=filter_data, headers=headers) as resp:
            if resp.status != 200:
                logging.warning(f"Filter push failed: {resp.status}")
    except Exception as e:
        logging.warning(f"Filter push error: {e}")

_FILTER_PUSH_INTERVAL = 7  # push filters every N successful sends

# ─── Custom exceptions (from mauto) ───────────────────────────────────────────
class AuthRequiredError(Exception):
    """Token expired or logged out (401 / errorCode AuthRequired)."""

class LikeExceededError(Exception):
    """Daily like quota reached."""

class NoMoreUsersError(Exception):
    """API returned empty list with no more users."""
# ──────────────────────────────────────────────────────────────────────────────

user_states = defaultdict(lambda: {
    "running": False,
    "status_message_id": None,
    "pinned_message_id": None,
    "total_added_friends": 0,
    "batch_index": 0,
    "stopped": False,
})

stop_markup = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Stop Requests", callback_data="stop")]
])


async def fetch_users(session, token, user_id):
    """Fetch users from the API. Raises AuthRequiredError on 401, NoMoreUsersError on empty."""
    url = "https://api.meeff.com/user/explore/v2?lng=71.9140141&unreachableUserIds=&lat=29.6264544&locale=en"
    headers = {
        'User-Agent': "okhttp/5.1.0",
        'meeff-access-token': token
    }
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 401:
                raise AuthRequiredError(f"Token {token[:10]}... is invalid or expired")
            if response.status == 429:
                raise LikeExceededError("429 rate limit")
            if response.status != 200:
                logging.error(f"Failed to fetch users: {response.status}")
                return []
            body = await response.json(content_type=None)
            error_code = body.get("errorCode")
            if error_code == "AuthRequired":
                raise AuthRequiredError("AuthRequired from API")
            users = body.get("users", [])
            if not users and not body.get("hasMore", True):
                raise NoMoreUsersError()
            return users
    except (AuthRequiredError, NoMoreUsersError, LikeExceededError):
        raise
    except Exception as e:
        logging.error(f"Fetch users failed: {e}")
        return []


def format_user(user):
    def time_ago(dt_str):
        if not dt_str: return "N/A"
        try:
            dt = parser.isoparse(dt_str)
            now = datetime.now(timezone.utc)
            diff = now - dt
            minutes = int(diff.total_seconds() // 60)
            if minutes < 1: return "just now"
            if minutes < 60: return f"{minutes} min ago"
            hours = minutes // 60
            if hours < 24: return f"{hours} hr ago"
            days = hours // 24
            return f"{days} day(s) ago"
        except Exception: return "unknown"

    last_active = time_ago(user.get("recentAt"))
    nationality = html.escape(user.get('nationalityCode', 'N/A'))
    height = html.escape(str(user.get('height', 'N/A')))
    if "|" in height:
        height_val, height_unit = height.split("|", 1)
        height = f"{height_val.strip()} {height_unit.strip()}"

    return (
        f"<b>Name:</b> {html.escape(user.get('name', 'N/A'))}\n"
        f"<b>ID:</b> <code>{html.escape(user.get('_id', 'N/A'))}</code>\n"
        f"<b>Nationality:</b> {nationality}\n"
        f"<b>Height:</b> {height}\n"
        f"<b>Description:</b> {html.escape(user.get('description', 'N/A'))}\n"
        f"<b>Birth Year:</b> {html.escape(str(user.get('birthYear', 'N/A')))}\n"
        f"<b>Platform:</b> {html.escape(user.get('platform', 'N/A'))}\n"
        f"<b>Profile Score:</b> {html.escape(str(user.get('profileScore', 'N/A')))}\n"
        f"<b>Distance:</b> {html.escape(str(user.get('distance', 'N/A')))} km\n"
        f"<b>Language Codes:</b> {html.escape(', '.join(user.get('languageCodes', [])))}\n"
        f"<b>Last Active:</b> {last_active}"
    )


async def process_users(session, users, token, user_id, bot, token_name, already_sent_ids, lock, exclude_codes=None, cross_seen=None, cross_lock=None, filter_counter=None):
    """
    Process a batch of users.
    Raises LikeExceededError when daily limit is hit.
    """
    state = user_states[user_id]
    added_count = 0
    filtered_count = 0

    is_spam_filter_enabled = await get_individual_spam_filter(user_id, "request")
    ids_to_persist = []

    headers = {
        'User-Agent': "okhttp/5.1.0",
        'meeff-access-token': token
    }

    for user in users:
        if not state["running"]: break

        user_id_to_check = user["_id"]

        # --- EXCLUDE NATIONALITY FILTER ---
        user_nationality = user.get("nationalityCode", "")
        if exclude_codes and user_nationality in exclude_codes:
            filtered_count += 1
            continue
        # ----------------------------------

        # --- CROSS-ACCOUNT DEDUP (same user won't get req from 2 accounts) ---
        if cross_seen is not None and cross_lock is not None:
            async with cross_lock:
                if user_id_to_check in cross_seen:
                    filtered_count += 1
                    continue
                cross_seen.add(user_id_to_check)
        # -----------------------------------------------------------------------

        # --- PER-ACCOUNT SPAM HISTORY DEDUP ---
        async with lock:
            if user_id_to_check in already_sent_ids:
                filtered_count += 1
                continue
            already_sent_ids.add(user_id_to_check)
        # --------------------------------------

        url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={user_id_to_check}&isOkay=1"

        try:
            async with session.get(url, headers=headers) as response:
                data = await response.json(content_type=None)

                if data.get("errorCode") == "LikeExceeded":
                    logging.info(f"Daily like limit reached for {token_name}.")
                    raise LikeExceededError()

                if is_spam_filter_enabled:
                    ids_to_persist.append(user_id_to_check)

                details = format_user(user)
                first_photo_url = user.get('photoUrls', [None])[0]

                if first_photo_url:
                    await bot.send_photo(chat_id=user_id, photo=first_photo_url, caption=details, parse_mode="HTML")
                else:
                    await bot.send_message(chat_id=user_id, text=details, parse_mode="HTML", disable_web_page_preview=True)

                added_count += 1
                state["total_added_friends"] += 1
                await asyncio.sleep(PER_USER_DELAY)
                # push filters every N sends
                if filter_counter is not None:
                    filter_counter[0] += 1
                    if is_request_filter_enabled(user_id) and filter_counter[0] >= _FILTER_PUSH_INTERVAL:
                        filter_counter[0] = 0
                        await _push_filters(session, user_id, token)

        except LikeExceededError:
            if is_spam_filter_enabled and ids_to_persist:
                await bulk_add_sent_ids(user_id, "request", ids_to_persist)
            raise
        except Exception as e:
            logging.error(f"Error processing user with {token_name}: {e}")
            await asyncio.sleep(PER_ERROR_DELAY)

    if is_spam_filter_enabled and ids_to_persist:
        await bulk_add_sent_ids(user_id, "request", ids_to_persist)

    return added_count, filtered_count


async def run_requests(user_id, bot, target_channel_id):
    """Main function to run the request process for a single token."""
    state = user_states[user_id]
    state.update({"total_added_friends": 0, "batch_index": 0, "running": True, "stopped": False})

    token = await get_current_account(user_id)
    if not token:
        await bot.edit_message_text(chat_id=user_id, message_id=state["status_message_id"], text="No active account found.")
        state["running"] = False
        return

    tokens = await get_active_tokens(user_id)
    token_name = next((t.get("name", "Default") for t in tokens if t["token"] == token), "Default")

    is_spam_enabled = await get_individual_spam_filter(user_id, "request")
    already_sent_ids = await get_already_sent_ids(user_id, "request") if is_spam_enabled else set()

    exclude_codes = set(await get_exclude_filter(user_id)) if await get_exclude_filter_enabled(user_id) else set()

    lock = asyncio.Lock()
    filter_counter = [0]  # mutable counter shared with process_users

    # Single session for the entire run
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Push filters once at start
        if is_request_filter_enabled(user_id):
            await _push_filters(session, user_id, token)
        while state["running"]:
            try:


                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["status_message_id"],
                        text=f"{token_name}: Requests sent: {state['total_added_friends']}",
                        reply_markup=stop_markup
                    )
                except Exception as e:
                    if "message is not modified" not in str(e):
                        logging.error(f"Status update error: {e}")

                users = await fetch_users(session, token, user_id)
                state["batch_index"] += 1

                if not users:
                    if state["batch_index"] > 10:
                        await bot.edit_message_text(
                            chat_id=user_id, message_id=state["status_message_id"],
                            text=f"{token_name}: No more users found. Total: {state['total_added_friends']}"
                        )
                        state["running"] = False
                        break
                    await asyncio.sleep(EMPTY_BATCH_DELAY)
                    continue

                await process_users(session, users, token, user_id, bot, token_name, already_sent_ids, lock, exclude_codes, filter_counter=filter_counter)
                await asyncio.sleep(PER_BATCH_DELAY)

            except AuthRequiredError:
                await bot.edit_message_text(
                    chat_id=user_id, message_id=state["status_message_id"],
                    text=f"🔒 <b>{token_name}: Token expired / logged out.</b>\n\nPlease re-sign in and update the token.",
                    parse_mode="HTML"
                )
                state["running"] = False
                break

            except NoMoreUsersError:
                await bot.edit_message_text(
                    chat_id=user_id, message_id=state["status_message_id"],
                    text=f"✅ <b>{token_name}: No more users available.</b>\n\nTotal sent: {state['total_added_friends']}",
                    parse_mode="HTML"
                )
                state["running"] = False
                break

            except LikeExceededError:
                await bot.edit_message_text(
                    chat_id=user_id, message_id=state["status_message_id"],
                    text=f"⏳ <b>{token_name}: Daily request quota reached.</b>\n\nTotal sent: {state['total_added_friends']}",
                    parse_mode="HTML"
                )
                state["running"] = False
                break

            except Exception as e:
                logging.error(f"Error during processing: {e}")
                await asyncio.sleep(PER_ERROR_DELAY)

    if state.get("pinned_message_id"):
        try: await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        except Exception: pass

    status = "Stopped" if state.get("stopped") else "Completed"
    await bot.send_message(user_id, f"✅ {status}! Total Added: {state.get('total_added_friends', 0)}")


async def process_all_tokens(user_id, tokens, bot, target_channel_id, initial_status_message=None):
    """Process friend requests for all tokens concurrently."""
    state = user_states[user_id]
    state.update({"total_added_friends": 0, "running": True, "stopped": False})

    if not initial_status_message:
        status_message = await bot.send_message(chat_id=user_id, text="🔄 <b>AIO Starting...</b>", parse_mode="HTML", reply_markup=stop_markup)
    else:
        status_message = initial_status_message

    state["status_message_id"] = status_message.message_id
    try:
        await bot.pin_chat_message(chat_id=user_id, message_id=status_message.message_id, disable_notification=True)
        state["pinned_message_id"] = status_message.message_id
    except Exception as e:
        logging.error(f"Failed to pin message: {e}")

    token_status = {
        token_obj["token"]: {
            "name": token_obj.get("name", f"Account {i+1}"),
            "added": 0,
            "filtered": 0,
            "status": "Queued"
        } for i, token_obj in enumerate(tokens)
    }

    is_spam_enabled = await get_individual_spam_filter(user_id, "request")
    base_sent_ids = await get_already_sent_ids(user_id, "request") if is_spam_enabled else set()
    exclude_codes = set(await get_exclude_filter(user_id)) if await get_exclude_filter_enabled(user_id) else set()

    # Shared set across all accounts — prevents two accounts sending to the same person
    cross_account_seen = set(base_sent_ids)
    cross_lock = asyncio.Lock()

    async def _worker(token_obj):
        token = token_obj["token"]
        name = token_status[token]["name"]
        empty_batches = 0

        # Per-account sent_ids for spam history tracking
        account_sent_ids = set(base_sent_ids)
        lock = asyncio.Lock()

        # Single session per worker for the entire run
        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            while state["running"]:
                try:


                    users = await fetch_users(session, token, user_id)

                    if not users:
                        await asyncio.sleep(EMPTY_BATCH_DELAY)
                        continue

                    token_status[token]["status"] = "Processing"

                    batch_added, batch_filtered = await process_users(
                        session, users, token, user_id, bot, name, account_sent_ids, lock, exclude_codes,
                        cross_seen=cross_account_seen, cross_lock=cross_lock, filter_counter=filter_counter
                    )

                    token_status[token]["added"] += batch_added
                    token_status[token]["filtered"] += batch_filtered
                    await asyncio.sleep(PER_BATCH_DELAY)

                except AuthRequiredError:
                    logging.warning(f"{name}: AuthRequiredError — token expired")
                    token_status[token]["status"] = "Logged Out"
                    return

                except NoMoreUsersError:
                    token_status[token]["status"] = "No Users"
                    return

                except LikeExceededError:
                    token_status[token]["status"] = "Limit Full"
                    return

                except Exception as e:
                    logging.error(f"Error processing {name}: {e}")
                    token_status[token]["status"] = "Retrying..."
                    await asyncio.sleep(PER_ERROR_DELAY)

        token_status[token]["status"] = "Stopped"

    async def _refresh_ui():
        last_message = ""
        while state["running"]:
            total_added_now = sum(s["added"] for s in token_status.values())
            header = f"🔄 <b>AIO Requests</b> | <b>Added:</b> {total_added_now}"
            lines = [header, "", "<pre>Account   │Added │Filter│Status      </pre>"]
            for s in token_status.values():
                name = s["name"]
                display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
                lines.append(f"<pre>{display} │{s['added']:>5} │{s['filtered']:>6}│{s['status']:<10}</pre>")
            current_message = "\n".join(lines)
            if current_message != last_message:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id, message_id=state["status_message_id"],
                        text=current_message, parse_mode="HTML", reply_markup=stop_markup
                    )
                    last_message = current_message
                except Exception as e:
                    if "message is not modified" not in str(e):
                        logging.error(f"Status update failed: {e}")
            await asyncio.sleep(1)

    ui_task = asyncio.create_task(_refresh_ui())
    worker_tasks = [asyncio.create_task(_worker(token_obj)) for token_obj in tokens]
    await asyncio.gather(*worker_tasks, return_exceptions=True)

    state["running"] = False
    await asyncio.sleep(1.1)
    ui_task.cancel()

    # Force final status render after workers done
    total_added_now = sum(s["added"] for s in token_status.values())
    header = f"🔄 <b>AIO Requests</b> | <b>Added:</b> {total_added_now}"
    lines = [header, "", "<pre>Account   │Added │Filter│Status      </pre>"]
    for s in token_status.values():
        name = s["name"]
        display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
        lines.append(f"<pre>{display} │{s['added']:>5} │{s['filtered']:>6}│{s['status']:<10}</pre>")
    try:
        await bot.edit_message_text(
            chat_id=user_id, message_id=state["status_message_id"],
            text="\n".join(lines), parse_mode="HTML", reply_markup=stop_markup
        )
    except Exception:
        pass
    if state.get("pinned_message_id"):
        try: await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        except Exception: pass

    total_added = sum(s["added"] for s in token_status.values())
    completion_status = "⚠️ Process Stopped" if state.get("stopped") else "✅ AIO Requests Completed"
    final_header = f"<b>{completion_status}</b> | <b>Total Added:</b> {total_added}"

    final_lines = [final_header, "", "<pre>Account   │Added │Filter│Status      </pre>"]
    for s in token_status.values():
        name = s["name"]
        display = name[:10] + '…' if len(name) > 10 else name.ljust(10)
        final_lines.append(f"<pre>{display} │{s['added']:>5} │{s['filtered']:>6}│{s['status']}</pre>")

    await bot.edit_message_text(
        chat_id=user_id, message_id=state["status_message_id"],
        text="\n".join(final_lines), parse_mode="HTML"
    )
