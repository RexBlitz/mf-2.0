import asyncio
import html
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Optional

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dateutil import parser

from db import (
    bulk_add_sent_ids,
    get_active_tokens,
    get_already_sent_ids,
    get_blocked_users,
    get_current_account,
    get_exclude_filter,
    get_exclude_filter_enabled,
    get_individual_spam_filter,
    get_token_location,
)
from filters import apply_filter_for_account, is_request_filter_enabled_async
from meeff_http import MEEFF_DEFAULT_LOCALE, build_meeff_headers, create_meeff_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Native Meeff ExploreFragment / AnswerQueueHandler timings
PER_USER_DELAY = 0.35          # AnswerQueueHandler uses 100ms; 350ms stays safely above flood threshold
PER_BATCH_DELAY = 1.0          # ExploreFragment.onUpdateFirstUser uses 1000ms delay before next queryExplore
EMPTY_BATCH_DELAY = 2.0
PER_ERROR_DELAY = 5.0          # ExploreHandler$2$1.onError generic retry = 5000ms
REQUEST_EXCEEDED_BACKOFF = 20.0
DEFAULT_LAT = 29.6264544
DEFAULT_LNG = 71.9140141
MAX_UNREACHABLE_WINDOW = 150   # Rolling IDs sent in unreachableUserIds to avoid re-fetching skipped users


class AuthRequiredError(Exception):
    """Token expired or logged out (401 / errorCode AuthRequired)."""
    def __init__(self, message: str = "AuthRequired", short_label: str = "AuthRequired"):
        super().__init__(message)
        self.short_label = short_label


class LikeExceededError(Exception):
    """Daily like quota reached."""
    def __init__(self, message: str = "LikeExceeded", short_label: str = "LikeExceeded"):
        super().__init__(message)
        self.short_label = short_label


class RequestExceededError(Exception):
    """Server rate limit / RequestExceeded."""
    def __init__(self, message: str = "RequestExceeded", short_label: str = "ReqExceeded"):
        super().__init__(message)
        self.short_label = short_label


class ApiResponseError(Exception):
    """Unexpected non-200 HTTP status or API errorCode."""
    def __init__(self, message: str, short_label: str):
        super().__init__(message)
        self.short_label = short_label


class NoMoreUsersError(Exception):
    """API returned empty list with hasMore=False."""


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


def _recent_at_ts(user: dict) -> float:
    raw = user.get("recentAt")
    if not raw:
        return 0.0
    try:
        return parser.isoparse(raw).timestamp()
    except Exception:
        return 0.0


async def fetch_users(
    session: aiohttp.ClientSession,
    token: str,
    user_id: int,
    unreachable_ids: Optional[Deque[str]] = None,
    lat: float = DEFAULT_LAT,
    lng: float = DEFAULT_LNG,
):
    """
    Fetch explore batch using native v7.1.7 GET /user/explore/v4.
    Sorts returned users by recentAt descending so most recently active users are prioritized.
    """
    url = "https://api.meeff.com/user/explore/v4"
    params = {
        "lat": str(lat),
        "lng": str(lng),
        "unreachableUserIds": ",".join(unreachable_ids) if unreachable_ids else "",
        "locale": MEEFF_DEFAULT_LOCALE,
    }
    headers = build_meeff_headers(token=token, json_body=False)

    try:
        async with session.get(url, params=params, headers=headers, timeout=15) as response:
            if response.status == 401:
                raise AuthRequiredError(f"HTTP 401 on explore/v4 ({token[:10]}...)", short_label="401 Unauth")
            if response.status == 429:
                raise RequestExceededError("HTTP 429 on explore/v4", short_label="429 Limit")

            body = await response.json(content_type=None)
            if not isinstance(body, dict):
                if response.status != 200:
                    raise ApiResponseError(f"HTTP {response.status} on explore/v4", short_label=f"HTTP {response.status}")
                return []

            error_code = body.get("errorCode")
            error_msg = body.get("errorMessage") or body.get("message") or ""
            if error_code == "AuthRequired":
                raise AuthRequiredError(error_msg or "AuthRequired", short_label="AuthReq")
            if error_code == "AuthInfoRequired":
                await asyncio.sleep(1.0)
                return []
            if error_code == "LikeExceeded":
                raise LikeExceededError(error_msg or "LikeExceeded", short_label="LikeExceed")
            if error_code == "RequestExceeded":
                raise RequestExceededError(error_msg or "RequestExceeded", short_label="ReqExceed")
            if error_code:
                raise ApiResponseError(
                    f"{error_code}: {error_msg}".strip(": "),
                    short_label=str(error_code)[:12],
                )

            if response.status != 200:
                logging.error("Failed to fetch users (%s): %s", response.status, body)
                raise ApiResponseError(f"HTTP {response.status} on explore/v4", short_label=f"HTTP {response.status}")

            users = body.get("users", []) or []
            if not users and not body.get("hasMore", True):
                raise NoMoreUsersError()

            # Prioritize genuinely recently-active users within each batch
            users.sort(key=_recent_at_ts, reverse=True)
            return users

    except (AuthRequiredError, NoMoreUsersError, LikeExceededError, RequestExceededError, ApiResponseError):
        raise
    except Exception as e:
        logging.error("Fetch users failed: %s", e)
        raise ApiResponseError(str(e) or type(e).__name__, short_label=type(e).__name__[:12]) from e


def format_user(user):
    def time_ago(dt_str):
        if not dt_str:
            return "N/A"
        try:
            dt = parser.isoparse(dt_str)
            now = datetime.now(timezone.utc)
            diff = now - dt
            minutes = int(diff.total_seconds() // 60)
            if minutes < 1:
                return "just now"
            if minutes < 60:
                return f"{minutes} min ago"
            hours = minutes // 60
            if hours < 24:
                return f"{hours} hr ago"
            days = hours // 24
            return f"{days} day(s) ago"
        except Exception:
            return "unknown"

    last_active = time_ago(user.get("recentAt"))
    nationality = html.escape(user.get("nationalityCode", "N/A"))
    height = html.escape(str(user.get("height", "N/A")))
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


async def _dispatch_tg_preview(bot: Bot, user_id: int, user: dict) -> None:
    """Send Telegram match preview asynchronously without stalling AnswerQueue pacing."""
    try:
        details = format_user(user)
        photos = user.get("photoUrls") or []
        first_photo_url = photos[0] if photos else None
        if first_photo_url:
            await bot.send_photo(chat_id=user_id, photo=first_photo_url, caption=details, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=user_id, text=details, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logging.debug("Telegram preview send failed: %s", e)


async def _release_claim(cross_seen: Optional[set], cross_lock: Optional[asyncio.Lock], uid: str) -> None:
    if cross_seen is not None and cross_lock is not None:
        async with cross_lock:
            cross_seen.discard(uid)


async def process_users(
    session: aiohttp.ClientSession,
    users: list,
    token: str,
    user_id: int,
    bot: Bot,
    token_name: str,
    already_sent_ids: set,
    lock: asyncio.Lock,
    exclude_codes=None,
    cross_seen=None,
    cross_lock=None,
    unreachable_ids: Optional[Deque[str]] = None,
    blocked_ids: Optional[set] = None,
    persisted_sent_ids: Optional[set] = None,
    is_spam_filter_enabled: Optional[bool] = None,
):
    """
    Process a batch sequentially like Meeff's AnswerQueueHandler (/user/undoableAnswer/v5).
    Handles LikeExceeded, RequestExceeded, AuthRequired, ChatRoomExist, and AlreadyAnswered.
    Guarantees cross-account deduplication across parallel workers without false lockouts on send failure.
    """
    state = user_states[user_id]
    added_count = 0
    filtered_count = 0

    if is_spam_filter_enabled is None:
        is_spam_filter_enabled = await get_individual_spam_filter(user_id, "request")
    ids_to_persist = []
    headers = build_meeff_headers(token=token, json_body=False)
    notify_tasks = []

    for user in users:
        if not state["running"]:
            break

        user_id_to_check = user.get("_id")
        if not user_id_to_check:
            continue

        if unreachable_ids is not None and user_id_to_check not in unreachable_ids:
            unreachable_ids.append(user_id_to_check)

        if blocked_ids and user_id_to_check in blocked_ids:
            filtered_count += 1
            continue

        user_nationality = user.get("nationalityCode", "")
        if exclude_codes and user_nationality in exclude_codes:
            filtered_count += 1
            continue

        if persisted_sent_ids and user_id_to_check in persisted_sent_ids:
            filtered_count += 1
            continue

        async with lock:
            if user_id_to_check in already_sent_ids:
                filtered_count += 1
                continue

        if cross_seen is not None and cross_lock is not None:
            async with cross_lock:
                if user_id_to_check in cross_seen:
                    filtered_count += 1
                    continue
                cross_seen.add(user_id_to_check)

        url = "https://api.meeff.com/user/undoableAnswer/v5"
        params = {
            "userId": user_id_to_check,
            "isOkay": "1",
            "locale": MEEFF_DEFAULT_LOCALE,
        }

        try:
            async with session.get(url, params=params, headers=headers, timeout=15) as response:
                if response.status == 401:
                    await _release_claim(cross_seen, cross_lock, user_id_to_check)
                    raise AuthRequiredError("HTTP 401 on undoableAnswer", short_label="401 Unauth")
                if response.status == 429:
                    await _release_claim(cross_seen, cross_lock, user_id_to_check)
                    raise RequestExceededError("HTTP 429 on undoableAnswer", short_label="429 Limit")

                data = await response.json(content_type=None)
                data = data if isinstance(data, dict) else {}
                error_code = data.get("errorCode")
                error_msg = data.get("errorMessage") or data.get("message") or ""

                if error_code == "LikeExceeded":
                    await _release_claim(cross_seen, cross_lock, user_id_to_check)
                    logging.info("Daily like limit reached for %s.", token_name)
                    raise LikeExceededError(error_msg or "LikeExceeded", short_label="LikeExceed")
                if error_code == "RequestExceeded":
                    await _release_claim(cross_seen, cross_lock, user_id_to_check)
                    logging.warning("RequestExceeded on %s: %s", token_name, error_msg)
                    raise RequestExceededError(error_msg or "RequestExceeded", short_label="ReqExceed")
                if error_code == "AuthRequired":
                    await _release_claim(cross_seen, cross_lock, user_id_to_check)
                    raise AuthRequiredError(error_msg or "AuthRequired", short_label="AuthReq")
                if error_code in ("AlreadyAnswered", "ChatRoomExist"):
                    async with lock:
                        already_sent_ids.add(user_id_to_check)
                    if is_spam_filter_enabled:
                        ids_to_persist.append(user_id_to_check)
                    filtered_count += 1
                    await asyncio.sleep(0.1)
                    continue

                if response.status != 200 or error_code:
                    await _release_claim(cross_seen, cross_lock, user_id_to_check)
                    err_label = str(error_code)[:12] if error_code else f"HTTP {response.status}"
                    err_detail = f"{error_code}: {error_msg}".strip(": ") if error_code else f"HTTP {response.status} on undoableAnswer"
                    logging.warning("Answer failed (%s) for %s: %s", response.status, token_name, data)
                    raise ApiResponseError(err_detail, short_label=err_label)

                async with lock:
                    already_sent_ids.add(user_id_to_check)
                if is_spam_filter_enabled:
                    ids_to_persist.append(user_id_to_check)

                notify_tasks.append(asyncio.create_task(_dispatch_tg_preview(bot, user_id, user)))
                added_count += 1
                state["total_added_friends"] += 1
                await asyncio.sleep(PER_USER_DELAY)

        except (LikeExceededError, RequestExceededError, AuthRequiredError, ApiResponseError):
            if is_spam_filter_enabled and ids_to_persist:
                await bulk_add_sent_ids(user_id, "request", ids_to_persist)
            if notify_tasks:
                await asyncio.gather(*notify_tasks, return_exceptions=True)
            raise
        except Exception as e:
            await _release_claim(cross_seen, cross_lock, user_id_to_check)
            logging.error("Error processing user with %s: %s", token_name, e)
            await asyncio.sleep(PER_ERROR_DELAY)

    if is_spam_filter_enabled and ids_to_persist:
        await bulk_add_sent_ids(user_id, "request", ids_to_persist)

    if notify_tasks:
        await asyncio.gather(*notify_tasks, return_exceptions=True)

    return added_count, filtered_count


async def run_requests(user_id, bot, target_channel_id):
    """Main function to run the request process for a single token using native explore/v4 flow."""
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
    blocked_ids = await get_blocked_users(user_id)
    exclude_codes = set(await get_exclude_filter(user_id)) if await get_exclude_filter_enabled(user_id) else set()
    loc = await get_token_location(user_id, token, DEFAULT_LAT, DEFAULT_LNG)
    user_lat, user_lng = loc["lat"], loc["lng"]

    lock = asyncio.Lock()
    unreachable_ids: Deque[str] = deque(maxlen=MAX_UNREACHABLE_WINDOW)
    need_filter_refresh = True
    consecutive_empty = 0

    filter_enabled = await is_request_filter_enabled_async(user_id)
    connector = aiohttp.TCPConnector(limit=10)
    async with create_meeff_session(connector=connector) as session:
        while state["running"]:
            try:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["status_message_id"],
                        text=f"{token_name}: Requests sent: {state['total_added_friends']}",
                        reply_markup=stop_markup,
                    )
                except Exception as e:
                    if "message is not modified" not in str(e):
                        logging.error("Status update error: %s", e)

                # Custom logic: apply filter before each 7-user explore batch to refresh pool
                if filter_enabled and need_filter_refresh:
                    await apply_filter_for_account(token, user_id, session=session)
                    await asyncio.sleep(0.3)

                users = await fetch_users(
                    session,
                    token,
                    user_id,
                    unreachable_ids=unreachable_ids,
                    lat=user_lat,
                    lng=user_lng,
                )
                state["batch_index"] += 1

                if not users:
                    consecutive_empty += 1
                    need_filter_refresh = (consecutive_empty % 2 == 0)
                    if consecutive_empty > 8:
                        await bot.edit_message_text(
                            chat_id=user_id,
                            message_id=state["status_message_id"],
                            text=f"{token_name}: No more users found. Total: {state['total_added_friends']}",
                        )
                        state["running"] = False
                        break
                    await asyncio.sleep(EMPTY_BATCH_DELAY)
                    continue

                consecutive_empty = 0
                batch_added, _ = await process_users(
                    session,
                    users,
                    token,
                    user_id,
                    bot,
                    token_name,
                    already_sent_ids,
                    lock,
                    exclude_codes=exclude_codes,
                    unreachable_ids=unreachable_ids,
                    blocked_ids=blocked_ids,
                    is_spam_filter_enabled=is_spam_enabled,
                )
                need_filter_refresh = batch_added > 0
                await asyncio.sleep(PER_BATCH_DELAY)

            except AuthRequiredError as e:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["status_message_id"],
                    text=f"🔒 <b>{html.escape(token_name)}: {html.escape(str(e))}</b>\n\nPlease re-sign in and update the token.",
                    parse_mode="HTML",
                )
                state["running"] = False
                break

            except NoMoreUsersError:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["status_message_id"],
                    text=f"✅ <b>{html.escape(token_name)}: No more users available.</b>\n\nTotal sent: {state['total_added_friends']}",
                    parse_mode="HTML",
                )
                state["running"] = False
                break

            except LikeExceededError as e:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["status_message_id"],
                    text=f"⏳ <b>{html.escape(token_name)}: {html.escape(str(e))}</b>\n\nTotal sent: {state['total_added_friends']}",
                    parse_mode="HTML",
                )
                state["running"] = False
                break

            except RequestExceededError as e:
                logging.warning("%s hit %s — backing off %.0fs", token_name, e, REQUEST_EXCEEDED_BACKOFF)
                await asyncio.sleep(REQUEST_EXCEEDED_BACKOFF)

            except ApiResponseError as e:
                logging.warning("%s API error: %s", token_name, e)
                await asyncio.sleep(PER_ERROR_DELAY)

            except Exception as e:
                logging.error("Error during processing: %s", e)
                await asyncio.sleep(PER_ERROR_DELAY)

    if state.get("pinned_message_id"):
        try:
            await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        except Exception:
            pass

    status = "Stopped" if state.get("stopped") else "Completed"
    await bot.send_message(user_id, f"✅ {status}! Total Added: {state.get('total_added_friends', 0)}")


async def process_all_tokens(user_id, tokens, bot, target_channel_id, initial_status_message=None):
    """Process friend requests for all tokens concurrently with custom per-batch (every 7 users) filter refresh."""
    state = user_states[user_id]
    state.update({"total_added_friends": 0, "running": True, "stopped": False})

    if not initial_status_message:
        status_message = await bot.send_message(
            chat_id=user_id,
            text="🔄 <b>AIO Starting...</b>",
            parse_mode="HTML",
            reply_markup=stop_markup,
        )
    else:
        status_message = initial_status_message

    state["status_message_id"] = status_message.message_id
    try:
        await bot.pin_chat_message(chat_id=user_id, message_id=status_message.message_id, disable_notification=True)
        state["pinned_message_id"] = status_message.message_id
    except Exception as e:
        logging.error("Failed to pin message: %s", e)

    token_status = {
        token_obj["token"]: {
            "name": token_obj.get("name", f"Account {i + 1}"),
            "added": 0,
            "filtered": 0,
            "status": "Queued",
            "error": "",
        }
        for i, token_obj in enumerate(tokens)
    }

    is_spam_enabled = await get_individual_spam_filter(user_id, "request")
    persisted_sent_ids = await get_already_sent_ids(user_id, "request") if is_spam_enabled else set()
    blocked_ids = await get_blocked_users(user_id)
    exclude_codes = set(await get_exclude_filter(user_id)) if await get_exclude_filter_enabled(user_id) else set()
    filter_enabled = await is_request_filter_enabled_async(user_id)
    cross_account_seen = set()
    cross_lock = asyncio.Lock()

    async def _worker(token_obj, worker_idx: int = 0):
        if worker_idx > 0:
            await asyncio.sleep(min(worker_idx * 0.4, 3.0))

        token = token_obj["token"]
        name = token_status[token]["name"]
        loc = await get_token_location(user_id, token, DEFAULT_LAT, DEFAULT_LNG)
        user_lat, user_lng = loc["lat"], loc["lng"]
        account_sent_ids = set()
        lock = asyncio.Lock()
        unreachable_ids: Deque[str] = deque(maxlen=MAX_UNREACHABLE_WINDOW)
        consecutive_empty = 0
        req_exceeded_hits = 0
        need_filter_refresh = True

        connector = aiohttp.TCPConnector(limit=10)
        async with create_meeff_session(connector=connector) as session:
            while state["running"]:
                try:
                    # Custom logic: apply filter before every 7-user explore batch to refresh pool
                    if filter_enabled and need_filter_refresh:
                        await apply_filter_for_account(token, user_id, session=session)
                        await asyncio.sleep(0.3)

                    users = await fetch_users(
                        session,
                        token,
                        user_id,
                        unreachable_ids=unreachable_ids,
                        lat=user_lat,
                        lng=user_lng,
                    )

                    if not users:
                        consecutive_empty += 1
                        need_filter_refresh = (consecutive_empty % 2 == 0)
                        if consecutive_empty > 8:
                            token_status[token]["status"] = "No Users"
                            token_status[token]["error"] = ""
                            return
                        await asyncio.sleep(EMPTY_BATCH_DELAY)
                        continue

                    consecutive_empty = 0
                    req_exceeded_hits = 0
                    token_status[token]["status"] = "Processing"
                    token_status[token]["error"] = ""

                    batch_added, batch_filtered = await process_users(
                        session,
                        users,
                        token,
                        user_id,
                        bot,
                        name,
                        account_sent_ids,
                        lock,
                        exclude_codes=exclude_codes,
                        cross_seen=cross_account_seen,
                        cross_lock=cross_lock,
                        unreachable_ids=unreachable_ids,
                        blocked_ids=blocked_ids,
                        persisted_sent_ids=persisted_sent_ids,
                        is_spam_filter_enabled=is_spam_enabled,
                    )

                    token_status[token]["added"] += batch_added
                    token_status[token]["filtered"] += batch_filtered
                    need_filter_refresh = batch_added > 0
                    await asyncio.sleep(PER_BATCH_DELAY)

                except AuthRequiredError as e:
                    logging.warning("%s: AuthRequiredError — %s", name, e)
                    token_status[token]["status"] = e.short_label
                    token_status[token]["error"] = str(e)
                    return

                except NoMoreUsersError:
                    token_status[token]["status"] = "No Users"
                    token_status[token]["error"] = ""
                    return

                except LikeExceededError as e:
                    token_status[token]["status"] = e.short_label
                    token_status[token]["error"] = str(e)
                    return

                except RequestExceededError as e:
                    req_exceeded_hits += 1
                    token_status[token]["status"] = e.short_label
                    token_status[token]["error"] = str(e)
                    if req_exceeded_hits >= 6:
                        return
                    await asyncio.sleep(min(REQUEST_EXCEEDED_BACKOFF * req_exceeded_hits, 60.0))

                except ApiResponseError as e:
                    logging.warning("API error on %s: %s", name, e)
                    token_status[token]["status"] = e.short_label
                    token_status[token]["error"] = str(e)
                    await asyncio.sleep(PER_ERROR_DELAY)

                except Exception as e:
                    logging.error("Error processing %s: %s", name, e)
                    token_status[token]["status"] = type(e).__name__[:12]
                    token_status[token]["error"] = str(e) or type(e).__name__
                    await asyncio.sleep(PER_ERROR_DELAY)

        token_status[token]["status"] = "Stopped"

    def _build_card_text(header: str) -> str:
        lines = [header, "", "<pre>Account   │Added │Filter│Status      </pre>"]
        error_lines = []
        for s in token_status.values():
            name = s["name"]
            display = name[:10] + "…" if len(name) > 10 else name.ljust(10)
            status_col = str(s["status"])[:12].ljust(12)
            lines.append(f"<pre>{display} │{s['added']:>5} │{s['filtered']:>6}│{status_col}</pre>")
            if s.get("error"):
                error_lines.append(f"• <b>{html.escape(name)}:</b> <code>{html.escape(s['error'][:120])}</code>")
        if error_lines:
            lines.append("")
            lines.append("⚠️ <b>Error Details:</b>")
            lines.extend(error_lines)
        return "\n".join(lines)

    async def _refresh_ui():
        last_message = ""
        while state["running"]:
            total_added_now = sum(s["added"] for s in token_status.values())
            header = f"🔄 <b>AIO Requests</b> | <b>Added:</b> {total_added_now}"
            current_message = _build_card_text(header)
            if current_message != last_message:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["status_message_id"],
                        text=current_message,
                        parse_mode="HTML",
                        reply_markup=stop_markup,
                    )
                    last_message = current_message
                except Exception as e:
                    if "message is not modified" not in str(e):
                        logging.error("Status update failed: %s", e)
            await asyncio.sleep(1)

    ui_task = asyncio.create_task(_refresh_ui())
    worker_tasks = [asyncio.create_task(_worker(token_obj, idx)) for idx, token_obj in enumerate(tokens)]
    await asyncio.gather(*worker_tasks, return_exceptions=True)

    state["running"] = False
    await asyncio.sleep(1.1)
    ui_task.cancel()

    if state.get("pinned_message_id"):
        try:
            await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        except Exception:
            pass

    total_added = sum(s["added"] for s in token_status.values())
    completion_status = "⚠️ Process Stopped" if state.get("stopped") else "✅ AIO Requests Completed"
    final_header = f"<b>{completion_status}</b> | <b>Total Added:</b> {total_added}"

    await bot.edit_message_text(
        chat_id=user_id,
        message_id=state["status_message_id"],
        text=_build_card_text(final_header),
        parse_mode="HTML",
    )
