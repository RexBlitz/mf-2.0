import asyncio
import logging
from collections import deque

from meeff_http import (
    MEEFF_DEFAULT_LOCALE,
    build_filter_payload,
    build_meeff_headers,
    create_meeff_session,
)

countries = [
    "AF", "AL", "DZ", "AD", "AO", "AG", "AR", "AM", "AU", "AT", "AZ", "BS", "BH", "BD",
    "BB", "BY", "BE", "BZ", "BJ", "BT", "BO", "BA", "BW", "BR", "BN", "BG", "BF", "BI",
    "KH", "CM", "CA", "CV", "CF", "TD", "CL", "CN", "CO", "KM", "CG", "CD", "CR", "HR",
    "CU", "CY", "CZ", "DK", "DJ", "DM", "DO", "EC", "EG", "SV", "GQ", "ER", "EE", "SZ",
    "ET", "FJ", "FI", "FR", "GA", "GM", "GE", "DE", "GH", "GR", "GD", "GT", "GN", "GW",
    "GY", "HT", "HN", "HU", "IS", "IN", "ID", "IR", "IQ", "IE", "IL", "IT", "JM", "JP",
    "JO", "KZ", "KE", "KI", "KR", "KW", "KG", "LA", "LV", "LB", "LS", "LR", "LY", "LI",
    "LT", "LU", "MG", "MW", "MY", "MV", "ML", "MT", "MH", "MR", "MU", "MX", "FM", "MD",
    "MC", "MN", "ME", "MA", "MZ", "MM", "NA", "NR", "NP", "NL", "NZ", "NI", "NE", "NG",
    "MK", "NO", "OM", "PK", "PW", "PA", "PG", "PY", "PE", "PH", "PL", "PT", "QA", "RO",
    "RU", "RW", "KN", "LC", "VC", "WS", "SM", "ST", "SA", "SN", "RS", "SC", "SL", "SG",
    "SK", "SI", "SB", "SO", "ZA", "SS", "ES", "LK", "SD", "SR", "SE", "CH", "SY", "TJ",
    "TZ", "TH", "TL", "TG", "TO", "TT", "TN", "TR", "TM", "TV", "UG", "UA", "AE", "GB",
    "US", "UY", "UZ", "VU", "VA", "VE", "VN", "YE", "ZM", "ZW",
]

REQUESTS_PER_COUNTRY = 2
DEFAULT_LAT = -3.7895238
DEFAULT_LNG = -38.5327365


async def update_country_filter(session, token, country_code, denied_codes=None):
    url = "https://api.meeff.com/user/updateFilter/v1"
    headers = build_meeff_headers(token=token, json_body=True)
    data = build_filter_payload(
        {
            "filterGenderType": 5,
            "filterBirthYearFrom": 1981,
            "filterBirthYearTo": 2007,
            "filterDistance": 510,
            "filterLanguageCodes": "",
            "filterNationalityBlock": 0,
            "filterNationalityCodes": country_code,
        },
        global_exclude_codes=denied_codes,
    )
    try:
        async with session.post(url, json=data, headers=headers, timeout=15) as response:
            if response.status == 200:
                logging.info("Switched to country: %s", country_code)
            else:
                logging.error("Failed to update country %s, status=%s", country_code, response.status)
    except Exception as e:
        logging.error("Exception updating country filter for %s: %s", country_code, e)


async def fetch_users(session, token, unreachable_ids):
    url = "https://api.meeff.com/user/explore/v4"
    headers = build_meeff_headers(token=token, json_body=False)
    params = {
        "lat": str(DEFAULT_LAT),
        "lng": str(DEFAULT_LNG),
        "unreachableUserIds": ",".join(unreachable_ids) if unreachable_ids else "",
        "locale": MEEFF_DEFAULT_LOCALE,
    }
    try:
        async with session.get(url, params=params, headers=headers, timeout=15) as response:
            if response.status != 200:
                logging.error("Failed to fetch users, status=%s", response.status)
                return []
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                return []
            if data.get("errorCode") == "RequestExceeded":
                logging.warning("Explore rate-limited: %s", data.get("errorMessage"))
                await asyncio.sleep(15)
                return []
            return data.get("users", []) or []
    except Exception as e:
        logging.error("Exception fetching users: %s", e)
        return []


async def like_user(session, token, target_user_id):
    """Returns 'ok', 'skip', 'rate_limit', 'limit', or 'auth'."""
    url = "https://api.meeff.com/user/undoableAnswer/v5"
    headers = build_meeff_headers(token=token, json_body=False)
    params = {"userId": target_user_id, "isOkay": "1", "locale": MEEFF_DEFAULT_LOCALE}
    try:
        async with session.get(url, params=params, headers=headers, timeout=15) as response:
            if response.status == 401:
                return "auth"
            if response.status == 429:
                return "rate_limit"
            data = await response.json(content_type=None)
            data = data if isinstance(data, dict) else {}
            err = data.get("errorCode")
            if err == "LikeExceeded":
                return "limit"
            if err == "RequestExceeded":
                return "rate_limit"
            if err == "AuthRequired":
                return "auth"
            if err in ("AlreadyAnswered", "ChatRoomExist"):
                return "skip"
            return "ok" if response.status == 200 else "skip"
    except Exception as e:
        logging.error("Exception liking user %s: %s", target_user_id, e)
        return "skip"


async def run_all_countries(user_id, state, bot, get_current_account):
    from db import (
        bulk_add_sent_ids,
        get_already_sent_ids,
        get_blocked_users,
        get_custom_exclude_enabled,
        get_exclude_filter,
        get_exclude_filter_enabled,
        get_individual_spam_filter,
    )

    token = await get_current_account(user_id)
    if not token:
        state["running"] = False
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state["status_message_id"],
            text="No active account found. Please set an account before starting All Countries feature.",
        )
        return

    is_spam_enabled = await get_individual_spam_filter(user_id, "request")
    already_sent_ids = await get_already_sent_ids(user_id, "request") if is_spam_enabled else set()
    blocked_ids = await get_blocked_users(user_id)
    native_exclude_codes = set(await get_exclude_filter(user_id)) if await get_exclude_filter_enabled(user_id) else set()
    custom_exclude_on = await get_custom_exclude_enabled(user_id)
    client_exclude_codes = native_exclude_codes if custom_exclude_on else set()

    active_countries = [c for c in countries if c not in client_exclude_codes]
    if not active_countries:
        state["running"] = False
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state["status_message_id"],
            text="All countries are currently excluded by your Exclude Filter.",
        )
        return

    unreachable_ids = deque(maxlen=80)
    async with create_meeff_session() as session:
        country_index = 0
        state["total_added_friends"] = 0
        state["country_batch_index"] = 0
        status_message_id = state["status_message_id"]

        while state["running"]:
            current_country = active_countries[country_index]
            await update_country_filter(session, token, current_country, denied_codes=native_exclude_codes)
            users = await fetch_users(session, token, unreachable_ids)
            request_count = 0
            state["country_batch_index"] += 1

            progress_text = (
                f"All Countries Feature Progress\n"
                f"Current Country: {current_country}\n"
                f"Batch: {state['country_batch_index']}\n"
                f"Users Fetched: {len(users)}\n"
                f"Total Liked: {state['total_added_friends']}\n"
            )
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=status_message_id,
                text=progress_text,
                reply_markup=state.get("stop_markup"),
            )

            batch_persisted = []
            for user in users:
                if request_count >= REQUESTS_PER_COUNTRY or not state["running"]:
                    break
                uid = user.get("_id")
                if not uid:
                    continue
                if uid not in unreachable_ids:
                    unreachable_ids.append(uid)
                if uid in blocked_ids or uid in already_sent_ids:
                    continue
                if client_exclude_codes and user.get("nationalityCode", "") in client_exclude_codes:
                    continue

                result = await like_user(session, token, uid)
                if result == "limit":
                    state["running"] = False
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=status_message_id,
                        text=f"Daily like limit reached.\nAll Countries stopped. Total Liked: {state['total_added_friends']}\n",
                        reply_markup=None,
                    )
                    break
                if result == "auth":
                    state["running"] = False
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=status_message_id,
                        text=f"Token expired / AuthRequired.\nTotal Liked: {state['total_added_friends']}\n",
                        reply_markup=None,
                    )
                    break
                if result == "rate_limit":
                    await asyncio.sleep(15)
                    continue
                if result == "skip":
                    already_sent_ids.add(uid)
                    if is_spam_enabled:
                        batch_persisted.append(uid)
                    continue

                already_sent_ids.add(uid)
                if is_spam_enabled:
                    batch_persisted.append(uid)
                state["total_added_friends"] += 1
                request_count += 1

                progress_text = (
                    f"All Countries Feature Progress\n"
                    f"Current Country: {current_country}\n"
                    f"Batch: {state['country_batch_index']}\n"
                    f"Liked in this Batch: {request_count}\n"
                    f"Total Liked: {state['total_added_friends']}\n"
                )
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=status_message_id,
                    text=progress_text,
                    reply_markup=state.get("stop_markup"),
                )
                await asyncio.sleep(2)

            if is_spam_enabled and batch_persisted:
                await bulk_add_sent_ids(user_id, "request", batch_persisted)

            if not state["running"]:
                break
            country_index = (country_index + 1) % len(active_countries)
            await asyncio.sleep(1)

        if state["running"]:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=status_message_id,
                text=f"All Countries feature stopped. Total Liked: {state['total_added_friends']}",
                reply_markup=None,
            )
