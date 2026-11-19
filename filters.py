import asyncio
import html
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import (
    get_active_tokens,
    get_request_filter_enabled,
    get_tokens,
    get_user_filters,
    set_request_filter_enabled,
    set_user_filters,
)
from meeff_http import (
    MEEFF_DEFAULT_LOCALE,
    build_filter_payload,
    build_meeff_headers,
    create_meeff_session,
    normalize_single_country,
)

logger = logging.getLogger(__name__)

# In-memory cache synced with MongoDB settings.request_filter_enabled
user_filter_states: Dict[int, Dict[str, Any]] = {}


def _format_gender(gender_type: Any) -> str:
    try:
        val = int(gender_type)
    except (TypeError, ValueError):
        val = 5
    if val == 6:
        return "Male"
    if val == 7:
        return "All"
    return "Female"


def _format_age(uf: Dict[str, Any]) -> int:
    current_year = datetime.now().year
    try:
        y_from = int(uf.get("filterBirthYearFrom", 1979))
    except (TypeError, ValueError):
        y_from = 1979
    return max(18, current_year - y_from)


async def is_request_filter_enabled_async(user_id: int) -> bool:
    enabled = await get_request_filter_enabled(user_id)
    user_filter_states.setdefault(user_id, {})["request_filter_enabled"] = enabled
    return enabled


def is_request_filter_enabled(user_id: int) -> bool:
    """Synchronous cached check kept in sync with DB by async helpers."""
    return user_filter_states.get(user_id, {}).get("request_filter_enabled", True)


async def get_meeff_filter_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Main Meeff Filter menu showing accounts and Request Filter ON/OFF toggle."""
    tokens = await get_tokens(user_id)
    filter_enabled = await is_request_filter_enabled_async(user_id)
    filter_status = "✅ Enabled" if filter_enabled else "❌ Disabled"

    keyboard: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"Request Filter: {filter_status}",
                callback_data="toggle_request_filter",
            )
        ]
    ]

    row: List[InlineKeyboardButton] = []
    for i, token_data in enumerate(tokens):
        account_name = token_data.get("name", f"Account {i + 1}")
        filters = token_data.get("filters", {})
        nat = normalize_single_country(
            filters.get("filterNationalityCode")
            if filters.get("filterNationalityCode") is not None
            else filters.get("filterNationalityCodes", "")
        )
        suffix = f" ({nat})" if nat else ""
        row.append(
            InlineKeyboardButton(
                text=f"{account_name[:12]}{suffix}",
                callback_data=f"account_filter_{i}",
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text="Back", callback_data="settings_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_account_filter_keyboard(account_index: int, user_filters: Optional[Dict[str, Any]] = None) -> InlineKeyboardMarkup:
    """Free-tier account filter menu: Gender, Age, Nationality, Reset."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Gender", callback_data=f"account_filter_gender_{account_index}"),
                InlineKeyboardButton(text="Age", callback_data=f"account_filter_age_{account_index}"),
                InlineKeyboardButton(text="Nationality", callback_data=f"account_filter_nationality_{account_index}"),
            ],
            [
                InlineKeyboardButton(text="Reset Filter", callback_data=f"account_filter_reset_{account_index}"),
            ],
            [
                InlineKeyboardButton(text="Back to Accounts", callback_data="meeff_filter_main"),
            ],
        ]
    )


def get_account_gender_keyboard(account_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Male", callback_data=f"account_gender_male_{account_index}"),
                InlineKeyboardButton(text="Female", callback_data=f"account_gender_female_{account_index}"),
                InlineKeyboardButton(text="All", callback_data=f"account_gender_all_{account_index}"),
            ],
            [InlineKeyboardButton(text="Back", callback_data=f"account_filter_back_{account_index}")],
        ]
    )


def get_account_age_keyboard(account_index: int) -> InlineKeyboardMarkup:
    keyboard: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for age in range(18, 41):
        row.append(InlineKeyboardButton(text=str(age), callback_data=f"account_age_{age}_{account_index}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="Back", callback_data=f"account_filter_back_{account_index}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_account_nationality_keyboard(account_index: int) -> InlineKeyboardMarkup:
    countries = [
        ("RU", "🇷🇺"), ("UA", "🇺🇦"), ("BY", "🇧🇾"), ("IR", "🇮🇷"), ("PH", "🇵🇭"),
        ("PK", "🇵🇰"), ("US", "🇺🇸"), ("IN", "🇮🇳"), ("DE", "🇩🇪"), ("FR", "🇫🇷"),
        ("BR", "🇧🇷"), ("CN", "🇨🇳"), ("JP", "🇯🇵"), ("KR", "🇰🇷"), ("CA", "🇨🇦"),
        ("AU", "🇦🇺"), ("IT", "🇮🇹"), ("ES", "🇪🇸"), ("ZA", "🇿🇦"), ("TR", "🇹🇷"),
        ("ID", "🇮🇩"), ("VN", "🇻🇳"), ("TH", "🇹🇭"), ("GB", "🇬🇧"),
    ]
    keyboard: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="All Countries", callback_data=f"account_nationality_all_{account_index}")]
    ]
    row: List[InlineKeyboardButton] = []
    for code, flag in countries:
        row.append(
            InlineKeyboardButton(
                text=f"{flag} {code}",
                callback_data=f"account_nationality_{code}_{account_index}",
            )
        )
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="Back", callback_data=f"account_filter_back_{account_index}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_nationality_keyboard(account_index: int = 0, *args, **kwargs) -> InlineKeyboardMarkup:
    """Compatibility helper used by signup.py."""
    return get_account_nationality_keyboard(account_index)


async def apply_filter_for_account(token: str, user_id: int, session=None) -> bool:
    """Apply free-tier compatible filter for an account via /user/updateFilter/v1."""
    try:
        user_filters = await get_user_filters(user_id, token) or {}
        filter_data = build_filter_payload(user_filters)
        url = "https://api.meeff.com/user/updateFilter/v1"
        headers = build_meeff_headers(token=token, json_body=True)

        async def _do_post(sess) -> bool:
            async with sess.post(url, json=filter_data, headers=headers, timeout=15) as response:
                body = {}
                try:
                    body = await response.json(content_type=None) or {}
                except Exception:
                    pass
                err = body.get("errorCode") or body.get("errorMessage")
                if response.status == 200 and not body.get("errorCode"):
                    logger.info("Filter applied successfully for token %s...", token[:10])
                    return True
                logger.warning(
                    "Failed to apply filter for token %s... Status=%s Error=%s",
                    token[:10],
                    response.status,
                    err,
                )
                return False

        if session is not None:
            return await _do_post(session)
        async with create_meeff_session() as own_session:
            return await _do_post(own_session)

    except Exception as e:
        logger.error("Error applying filter: %s", e)
        return False


async def reset_filter_for_account(token: str, user_id: int, session=None) -> bool:
    """Reset account filter on Meeff server using {"reset": True}."""
    try:
        url = "https://api.meeff.com/user/updateFilter/v1"
        headers = build_meeff_headers(token=token, json_body=True)
        payload = {"reset": True, "locale": MEEFF_DEFAULT_LOCALE}

        async def _do_post(sess) -> bool:
            async with sess.post(url, json=payload, headers=headers, timeout=15) as response:
                body = {}
                try:
                    body = await response.json(content_type=None) or {}
                except Exception:
                    pass
                return response.status == 200 and not body.get("errorCode")

        if session is not None:
            ok = await _do_post(session)
        else:
            async with create_meeff_session() as own_session:
                ok = await _do_post(own_session)

        if ok:
            await set_user_filters(user_id, token, {"filterNationalityCode": "", "filterNationalityCodes": ""})
        return ok
    except Exception as e:
        logger.error("Error resetting filter: %s", e)
        return False


async def sync_all_active_filters(user_id: int) -> Tuple[int, int]:
    """Push current saved free-tier filters to all active accounts."""
    tokens = await get_active_tokens(user_id)
    if not tokens:
        return 0, 0
    async with create_meeff_session() as session:
        results = await asyncio.gather(
            *(apply_filter_for_account(t["token"], user_id, session=session) for t in tokens),
            return_exceptions=True,
        )
    ok = sum(1 for r in results if r is True)
    return ok, len(tokens) - ok


async def handle_filter_text_input(message: types.Message) -> bool:
    """Stub kept for compatibility with main.py message router."""
    return False


async def _render_account_filter(
    callback_query: types.CallbackQuery,
    user_id: int,
    tokens: List[Dict[str, Any]],
    account_index: int,
    status_line: str = "",
):
    if account_index < 0 or account_index >= len(tokens):
        await callback_query.answer("Account not found.", show_alert=True)
        return
    tok = tokens[account_index]
    uf = await get_user_filters(user_id, tok["token"]) or {}
    nat = normalize_single_country(
        uf.get("filterNationalityCode")
        if uf.get("filterNationalityCode") is not None
        else uf.get("filterNationalityCodes", "")
    )
    gender_str = _format_gender(uf.get("filterGenderType", 5))
    max_age = _format_age(uf)
    extra = f"\n\n{status_line}" if status_line else ""
    text = (
        f"<b>Filter Settings for {html.escape(tok.get('name', f'Account {account_index + 1}'))}</b>\n\n"
        f"• <b>Gender:</b> {gender_str}\n"
        f"• <b>Age:</b> 18–{max_age}\n"
        f"• <b>Country:</b> <code>{nat or 'All Countries'}</code>"
        f"{extra}"
    )
    await callback_query.message.edit_text(
        text,
        reply_markup=get_account_filter_keyboard(account_index, uf),
        parse_mode="HTML",
    )


async def set_account_filter(callback_query: types.CallbackQuery):
    """Handle free-tier account filter callbacks."""
    user_id = callback_query.from_user.id
    data = callback_query.data
    tokens = await get_tokens(user_id)

    if data in ("toggle_request_filter", "flt_tog"):
        current_status = await is_request_filter_enabled_async(user_id)
        new_status = not current_status
        await set_request_filter_enabled(user_id, new_status)
        user_filter_states.setdefault(user_id, {})["request_filter_enabled"] = new_status
        await callback_query.message.edit_text(
            "<b>Meeff Filter Settings</b>\n\nSelect an account to configure filters:",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML",
        )
        await callback_query.answer(f"Request filter {'enabled' if new_status else 'disabled'}!")
        return True

    if data in ("meeff_filter_main", "flt_main"):
        await callback_query.message.edit_text(
            "<b>Meeff Filter Settings</b>\n\nSelect an account to configure filters:",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML",
        )
        await callback_query.answer()
        return True

    if data.startswith("flt_acc:"):
        idx = int(data.split(":")[1])
        await _render_account_filter(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    data_parts = data.split("_")

    if data.startswith("account_filter_reset_"):
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            ok = await reset_filter_for_account(tokens[idx]["token"], user_id)
            await _render_account_filter(
                callback_query,
                user_id,
                tokens,
                idx,
                status_line="✅ Filter reset to default!" if ok else "⚠️ Reset failed",
            )
            await callback_query.answer("Filter reset!" if ok else "Reset failed")
        return True

    if data.startswith("account_filter_") and not any(
        x in data for x in ["gender", "age", "nationality", "recent", "face", "back", "reset"]
    ):
        idx = int(data_parts[2])
        await _render_account_filter(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    if data.startswith("account_filter_back_"):
        idx = int(data_parts[3])
        await _render_account_filter(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    if data.startswith("account_filter_gender_"):
        idx = int(data_parts[3])
        await callback_query.message.edit_text(
            "<b>Select Gender Filter:</b>",
            reply_markup=get_account_gender_keyboard(idx),
            parse_mode="HTML",
        )
        await callback_query.answer()
        return True

    if data.startswith("account_filter_age_"):
        idx = int(data_parts[3])
        await callback_query.message.edit_text(
            "<b>Select Age Filter:</b>",
            reply_markup=get_account_age_keyboard(idx),
            parse_mode="HTML",
        )
        await callback_query.answer()
        return True

    if data.startswith("account_filter_nationality_"):
        idx = int(data_parts[3])
        await callback_query.message.edit_text(
            "<b>Select Nationality Filter:</b>",
            reply_markup=get_account_nationality_keyboard(idx),
            parse_mode="HTML",
        )
        await callback_query.answer()
        return True

    if data.startswith("account_gender_"):
        gender = data_parts[2]
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            uf["filterGenderType"] = {"male": 6, "female": 5, "all": 7}.get(gender, 5)
            await set_user_filters(user_id, tok["token"], uf)
            ok = await apply_filter_for_account(tok["token"], user_id)
            await _render_account_filter(
                callback_query,
                user_id,
                tokens,
                idx,
                status_line=f"✅ Gender set to {gender.capitalize()}" if ok else "⚠️ Saved locally (API failed)",
            )
            await callback_query.answer(f"Gender set to {gender.capitalize()}")
        return True

    if data.startswith("account_age_"):
        age = int(data_parts[2])
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            uf["filterBirthYearFrom"] = datetime.now().year - age
            await set_user_filters(user_id, tok["token"], uf)
            ok = await apply_filter_for_account(tok["token"], user_id)
            await _render_account_filter(
                callback_query,
                user_id,
                tokens,
                idx,
                status_line=f"✅ Age filter set to 18–{age}" if ok else "⚠️ Saved locally (API failed)",
            )
            await callback_query.answer(f"Age set to {age}")
        return True

    if data.startswith("account_nationality_"):
        nationality = data_parts[2]
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            nat_val = "" if nationality == "all" else nationality.upper()
            uf["filterNationalityCode"] = nat_val
            uf["filterNationalityCodes"] = nat_val
            await set_user_filters(user_id, tok["token"], uf)
            ok = await apply_filter_for_account(tok["token"], user_id)
            await _render_account_filter(
                callback_query,
                user_id,
                tokens,
                idx,
                status_line=f"✅ Country set to {nat_val or 'All Countries'}" if ok else "⚠️ Saved locally (API failed)",
            )
            await callback_query.answer(f"Country: {nat_val or 'All'}")
        return True

    return False


async def meeff_filter_command(message: types.Message):
    """Main command to show Meeff Filter settings."""
    user_id = message.from_user.id
    await message.answer(
        "<b>Meeff Filter Settings</b>\n\nSelect an account to configure filters:",
        reply_markup=await get_meeff_filter_main_keyboard(user_id),
        parse_mode="HTML",
    )


async def set_filter(callback_query: types.CallbackQuery):
    return await set_account_filter(callback_query)


async def filter_command(message: types.Message):
    return await meeff_filter_command(message)
