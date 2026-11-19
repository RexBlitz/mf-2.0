import asyncio
import html
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import (
    get_active_tokens,
    get_exclude_filter,
    get_exclude_filter_enabled,
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
    normalize_codes_csv,
)

logger = logging.getLogger(__name__)

# In-memory cache synced with MongoDB settings.request_filter_enabled
user_filter_states: Dict[int, Dict[str, Any]] = {}
# Pending custom text input state for CSV country codes (user_id -> {"mode": "include"|"exclude", "idx": int})
filter_input_states: Dict[int, Dict[str, Any]] = {}

COUNTRY_CATALOG: List[Tuple[str, str]] = [
    ("KR", "🇰🇷"), ("JP", "🇯🇵"), ("US", "🇺🇸"), ("GB", "🇬🇧"), ("CA", "🇨🇦"),
    ("AU", "🇦🇺"), ("DE", "🇩🇪"), ("FR", "🇫🇷"), ("ES", "🇪🇸"), ("IT", "🇮🇹"),
    ("RU", "🇷🇺"), ("UA", "🇺🇦"), ("BY", "🇧🇾"), ("TR", "🇹🇷"), ("BR", "🇧🇷"),
    ("PH", "🇵🇭"), ("IN", "🇮🇳"), ("PK", "🇵🇰"), ("ID", "🇮🇩"), ("VN", "🇻🇳"),
    ("TH", "🇹🇭"), ("CN", "🇨🇳"), ("TW", "🇹🇼"), ("IR", "🇮🇷"), ("ZA", "🇿🇦"),
]
COUNTRIES_PER_PAGE = 15

PURPOSE_OPTIONS: List[Tuple[str, str]] = [
    ("PA000000", "💘 Serious"),
    ("PB000000", "👻 Casual"),
    ("PC000000", "🗣️ Language"),
    ("PD000000", "🌎 Friends"),
    ("PE000000", "✈️ Trip Mate"),
    ("PF000000", "🔍 Open Mind"),
]
PURPOSE_LABEL_MAP = dict(PURPOSE_OPTIONS)

LANGUAGE_OPTIONS: List[Tuple[str, str]] = [
    ("en", "🇬🇧 EN"), ("ko", "🇰🇷 KO"), ("ja", "🇯🇵 JA"), ("zh", "🇨🇳 ZH"),
    ("es", "🇪🇸 ES"), ("fr", "🇫🇷 FR"), ("de", "🇩🇪 DE"), ("ru", "🇷🇺 RU"),
    ("ar", "🇸🇦 AR"), ("tr", "🇹🇷 TR"), ("pt", "🇧🇷 PT"), ("hi", "🇮🇳 HI"),
    ("ur", "🇵🇰 UR"), ("id", "🇮🇩 ID"), ("vi", "🇻🇳 VI"), ("th", "🇹🇭 TH"),
]

DISTANCE_OPTIONS: List[Tuple[int, str]] = [
    (5, "5 km"), (10, "10 km"), (50, "50 km"), (100, "100 km"),
    (200, "200 km"), (300, "300 km"), (500, "500 km"), (510, "🌍 Global"),
]


def _csv_to_list(val: Any, *, lowercase: bool = False) -> List[str]:
    csv_str = normalize_codes_csv(val, lowercase=lowercase)
    return [c for c in csv_str.split(",") if c]


def _toggle_code_csv(current_val: Any, code: str, *, lowercase: bool = False, max_items: int = 0) -> Tuple[str, bool]:
    """Toggle a code inside a comma-separated string. Returns (new_csv, limit_blocked)."""
    norm_code = code.strip().lower() if lowercase else code.strip().upper()
    items = _csv_to_list(current_val, lowercase=lowercase)
    if norm_code in items:
        items.remove(norm_code)
        return ",".join(items), False
    if max_items > 0 and len(items) >= max_items:
        return ",".join(items), True
    items.append(norm_code)
    return ",".join(items), False


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


def _format_age_range(uf: Dict[str, Any]) -> str:
    current_year = datetime.now().year
    y_from = int(uf.get("filterBirthYearFrom", 1979))
    y_to = int(uf.get("filterBirthYearTo", current_year - 18))
    if y_from > y_to:
        y_from, y_to = y_to, y_from
    min_age = max(18, current_year - y_to)
    max_age = max(min_age, current_year - y_from)
    return f"{min_age}–{max_age}"


def format_filter_summary(
    account_name: str,
    uf: Optional[Dict[str, Any]],
    global_excludes: Optional[List[str]] = None,
    global_exclude_enabled: bool = False,
) -> str:
    uf = uf or {}
    nat_csv = normalize_codes_csv(
        uf.get("filterNationalityCodes") if uf.get("filterNationalityCodes") is not None else uf.get("filterNationalityCode", "")
    )
    acc_denied = normalize_codes_csv(uf.get("filterDeniedNationality", ""))
    global_denied = normalize_codes_csv(global_excludes or []) if global_exclude_enabled else ""
    purposes_csv = normalize_codes_csv(uf.get("filterPurposes", ""))
    purpose_names = [PURPOSE_LABEL_MAP.get(p, p) for p in purposes_csv.split(",") if p]
    langs_csv = normalize_codes_csv(uf.get("filterLanguageCodes", ""), lowercase=True, max_items=3)
    dist = int(uf.get("filterDistance", 510))
    dist_str = "Global (510)" if dist > 500 else f"{dist} km"
    min_photos = max(1, int(uf.get("filterMinProfileImageCount", 1)))
    rec = "ON" if uf.get("filterRecentlyActiveOnly") else "OFF"
    face = "ON" if uf.get("filterFaceVerifiedOnly") else "OFF"
    blk = "ON" if uf.get("filterNationalityBlock") else "OFF"

    lines = [
        f"🎛️ <b>Native Filter Settings — {html.escape(account_name)}</b>",
        "",
        f"• <b>Gender:</b> {_format_gender(uf.get('filterGenderType', 5))} | <b>Age:</b> {_format_age_range(uf)}",
        f"• <b>Include Countries:</b> <code>{nat_csv or 'All Countries'}</code>",
        f"• <b>Native Exclude (Account):</b> <code>{acc_denied or 'None'}</code>",
    ]
    if global_denied:
        lines.append(f"• <b>Native Exclude (Global):</b> <code>{global_denied}</code>")
    lines.extend([
        f"• <b>Distance:</b> {dist_str} | <b>Min Photos:</b> {min_photos}+",
        f"• <b>Purposes:</b> {', '.join(purpose_names) if purpose_names else 'Any'}",
        f"• <b>Languages (max 3):</b> <code>{langs_csv or 'Any'}</code>",
        f"• <b>Active Only:</b> {rec} | <b>Face Verified:</b> {face} | <b>Hide My Country:</b> {blk}",
    ])
    return "\n".join(lines)


async def is_request_filter_enabled_async(user_id: int) -> bool:
    enabled = await get_request_filter_enabled(user_id)
    user_filter_states.setdefault(user_id, {})["request_filter_enabled"] = enabled
    return enabled


def is_request_filter_enabled(user_id: int) -> bool:
    """Synchronous cached check kept in sync with DB by async helpers."""
    return user_filter_states.get(user_id, {}).get("request_filter_enabled", True)


async def get_meeff_filter_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Main Meeff Filter menu showing multi-country tags and DB-persisted toggle."""
    tokens = await get_tokens(user_id)
    filter_enabled = await is_request_filter_enabled_async(user_id)
    filter_status = "✅ Enabled" if filter_enabled else "❌ Disabled"

    keyboard: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"Sync Filter on Start: {filter_status}",
                callback_data="flt_tog",
            )
        ],
        [
            InlineKeyboardButton(
                text="🚀 Sync All Active Filters to Meeff",
                callback_data="flt_sync_all",
            )
        ],
    ]

    row: List[InlineKeyboardButton] = []
    for i, token_data in enumerate(tokens):
        account_name = token_data.get("name", f"Account {i + 1}")
        filters = token_data.get("filters", {})
        nat = normalize_codes_csv(
            filters.get("filterNationalityCodes")
            if filters.get("filterNationalityCodes") is not None
            else filters.get("filterNationalityCode", "")
        )
        denied = normalize_codes_csv(filters.get("filterDeniedNationality", ""))
        badges = []
        if nat:
            badges.append(nat if len(nat) <= 11 else f"{nat[:9]}…")
        if denied:
            badges.append(f"-{denied}" if len(denied) <= 8 else f"-{denied[:6]}…")
        suffix = f" ({' '.join(badges)})" if badges else ""
        label = f"{account_name[:12]}{suffix}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"flt_acc:{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text="Back", callback_data="settings_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_account_filter_keyboard(account_index: int, user_filters: Optional[Dict[str, Any]] = None) -> InlineKeyboardMarkup:
    uf = user_filters or {}
    recent_label = "🟢 Active Only: ON" if uf.get("filterRecentlyActiveOnly") else "⚪ Active Only: OFF"
    face_label = "🪪 Face Verified: ON" if uf.get("filterFaceVerifiedOnly") else "⚪ Face Verified: OFF"
    block_label = "🛡️ Hide My Country: ON" if uf.get("filterNationalityBlock") else "⚪ Hide My Country: OFF"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌍 Include Countries", callback_data=f"flt_n_m:{account_index}:0"),
                InlineKeyboardButton(text="🚫 Native Exclude", callback_data=f"flt_x_m:{account_index}:0"),
            ],
            [
                InlineKeyboardButton(text="⚧ Gender", callback_data=f"flt_g_m:{account_index}"),
                InlineKeyboardButton(text="🎂 Age Range", callback_data=f"flt_a_m:{account_index}"),
                InlineKeyboardButton(text="📍 Distance", callback_data=f"flt_d_m:{account_index}"),
            ],
            [
                InlineKeyboardButton(text="🖼️ Min Photos", callback_data=f"flt_p_m:{account_index}"),
                InlineKeyboardButton(text="🎯 Purposes", callback_data=f"flt_u_m:{account_index}"),
                InlineKeyboardButton(text="🗣️ Languages", callback_data=f"flt_l_m:{account_index}"),
            ],
            [
                InlineKeyboardButton(text=recent_label, callback_data=f"flt_tg_rec:{account_index}"),
                InlineKeyboardButton(text=face_label, callback_data=f"flt_tg_face:{account_index}"),
            ],
            [
                InlineKeyboardButton(text=block_label, callback_data=f"flt_tg_blk:{account_index}"),
            ],
            [
                InlineKeyboardButton(text="📋 Copy Filter → All Active", callback_data=f"flt_copy:{account_index}"),
                InlineKeyboardButton(text="🔄 Reset Default", callback_data=f"flt_rst:{account_index}"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Back to Accounts", callback_data="flt_main"),
            ],
        ]
    )


def get_gender_keyboard(account_index: int, current_gender: int = 5) -> InlineKeyboardMarkup:
    def _lbl(title: str, code: int) -> str:
        return f"✅ {title}" if int(current_gender or 5) == code else title

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_lbl("Female", 5), callback_data=f"flt_g:{account_index}:5"),
                InlineKeyboardButton(text=_lbl("Male", 6), callback_data=f"flt_g:{account_index}:6"),
                InlineKeyboardButton(text=_lbl("All Gender", 7), callback_data=f"flt_g:{account_index}:7"),
            ],
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"flt_acc:{account_index}")],
        ]
    )


def get_age_keyboard(account_index: int, uf: Optional[Dict[str, Any]] = None) -> InlineKeyboardMarkup:
    uf = uf or {}
    current_year = datetime.now().year
    y_from = int(uf.get("filterBirthYearFrom", 1979))
    y_to = int(uf.get("filterBirthYearTo", current_year - 18))
    cur_min = max(18, current_year - y_to)
    cur_max = max(cur_min, current_year - y_from)

    min_ages = [18, 20, 22, 24, 26, 28, 30]
    max_ages = [22, 25, 28, 30, 35, 40, 50]

    min_row = [
        InlineKeyboardButton(
            text=f"{'✅ ' if cur_min == a else ''}{a}",
            callback_data=f"flt_amin:{account_index}:{a}",
        )
        for a in min_ages
    ]
    max_row = [
        InlineKeyboardButton(
            text=f"{'✅ ' if cur_max == a else ''}{a}",
            callback_data=f"flt_amax:{account_index}:{a}",
        )
        for a in max_ages
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⬇️ Min Age (Current: {cur_min})", callback_data="noop_age_min")],
            min_row[:4],
            min_row[4:],
            [InlineKeyboardButton(text=f"⬆️ Max Age (Current: {cur_max})", callback_data="noop_age_max")],
            max_row[:4],
            max_row[4:],
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"flt_acc:{account_index}")],
        ]
    )


def get_multi_country_keyboard(
    account_index: int,
    selected_csv: str,
    mode: str = "include",
    page: int = 0,
) -> InlineKeyboardMarkup:
    """
    Build multi-select country checkbox keyboard.
    mode: 'include' (filterNationalityCodes) or 'exclude' (filterDeniedNationality)
    """
    selected = set(_csv_to_list(selected_csv))
    total_pages = (len(COUNTRY_CATALOG) + COUNTRIES_PER_PAGE - 1) // COUNTRIES_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    chunk = COUNTRY_CATALOG[page * COUNTRIES_PER_PAGE : (page + 1) * COUNTRIES_PER_PAGE]

    prefix = "flt_n" if mode == "include" else "flt_x"
    clear_label = "🌐 All Countries (Clear)" if mode == "include" else "🧹 Clear Excluded Countries"

    keyboard: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=clear_label, callback_data=f"{prefix}_all:{account_index}:{page}")]
    ]

    row: List[InlineKeyboardButton] = []
    for code, flag in chunk:
        mark = "✅ " if code in selected else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{flag} {code}",
                callback_data=f"{prefix}_t:{account_index}:{code}:{page}",
            )
        )
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Extra custom codes chosen via text input that aren't in COUNTRY_CATALOG
    catalog_codes = {c for c, _ in COUNTRY_CATALOG}
    custom_selected = sorted(selected - catalog_codes)
    if custom_selected:
        extra_row = [
            InlineKeyboardButton(
                text=f"✅ {c}",
                callback_data=f"{prefix}_t:{account_index}:{c}:{page}",
            )
            for c in custom_selected[:5]
        ]
        keyboard.append(extra_row)

    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="« Prev", callback_data=f"{prefix}_m:{account_index}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"Page {page + 1}/{total_pages}", callback_data="noop_flt_pg"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Next »", callback_data=f"{prefix}_m:{account_index}:{page + 1}"))
    keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton(text="⌨️ Type Custom Codes", callback_data=f"{prefix}_txt:{account_index}"),
        InlineKeyboardButton(text="💾 Apply & Back", callback_data=f"{prefix}_save:{account_index}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_distance_keyboard(account_index: int, current_dist: int = 510) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for val, label in DISTANCE_OPTIONS:
        mark = "✅ " if int(current_dist or 510) == val else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"flt_d:{account_index}:{val}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"flt_acc:{account_index}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_photo_count_keyboard(account_index: int, current_count: int = 1) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=f"{'✅ ' if int(current_count or 1) == n else ''}{n}+",
            callback_data=f"flt_p:{account_index}:{n}",
        )
        for n in (1, 2, 3, 4, 5)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row,
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"flt_acc:{account_index}")],
        ]
    )


def get_purposes_keyboard(account_index: int, current_csv: str = "") -> InlineKeyboardMarkup:
    selected = set(_csv_to_list(current_csv))
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🧹 Clear Purposes (Any)", callback_data=f"flt_u_t:{account_index}:CLEAR")]
    ]
    row: List[InlineKeyboardButton] = []
    for code, label in PURPOSE_OPTIONS:
        mark = "✅ " if code in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"flt_u_t:{account_index}:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="💾 Apply & Back", callback_data=f"flt_u_save:{account_index}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_languages_keyboard(account_index: int, current_csv: str = "") -> InlineKeyboardMarkup:
    selected = set(_csv_to_list(current_csv, lowercase=True))
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🧹 Clear Languages (Any)", callback_data=f"flt_l_t:{account_index}:CLEAR")]
    ]
    row: List[InlineKeyboardButton] = []
    for code, label in LANGUAGE_OPTIONS:
        mark = "✅ " if code in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"flt_l_t:{account_index}:{code}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="💾 Apply & Back", callback_data=f"flt_l_save:{account_index}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def apply_filter_for_account(token: str, user_id: int, session=None) -> bool:
    """Apply stored filters + global native excludes using Meeff v7.1.7 /user/updateFilter/v1."""
    try:
        user_filters = await get_user_filters(user_id, token) or {}
        global_excludes: List[str] = []
        if await get_exclude_filter_enabled(user_id):
            global_excludes = await get_exclude_filter(user_id) or []

        filter_data = build_filter_payload(user_filters, global_exclude_codes=global_excludes)
        url = "https://api.meeff.com/user/updateFilter/v1"
        headers = build_meeff_headers(token=token, json_body=True)

        async def _post_payload(sess, payload: Dict[str, Any]) -> Tuple[bool, Optional[str], int]:
            async with sess.post(url, json=payload, headers=headers, timeout=15) as response:
                body = {}
                try:
                    body = await response.json(content_type=None) or {}
                except Exception:
                    pass
                err = body.get("errorCode") or body.get("errorMessage")
                ok = response.status == 200 and not body.get("errorCode")
                return ok, err, response.status

        async def _do_post(sess) -> bool:
            ok, err, status = await _post_payload(sess, filter_data)
            if ok:
                logger.info("Filter applied successfully for token %s...", token[:10])
                return True

            if status == 401 or err == "AuthRequired":
                logger.warning("Failed to apply filter for token %s... Status=%s Error=%s", token[:10], status, err)
                return False

            # If Plus-gated fields (denied nationality, purposes, >1 photos, distance, block) fail on a free account,
            # automatically retry with the core free-safe payload so basic filters still sync.
            has_plus_fields = bool(
                filter_data.get("filterDeniedNationality")
                or filter_data.get("filterPurposes")
                or filter_data.get("filterLanguageCodes")
                or filter_data.get("filterDistance", 510) != 510
                or filter_data.get("filterMinProfileImageCount", 1) > 1
                or filter_data.get("filterNationalityBlock", 0)
            )
            if has_plus_fields:
                fallback_payload = build_filter_payload(
                    {
                        "filterGenderType": user_filters.get("filterGenderType", 5),
                        "filterBirthYearFrom": user_filters.get("filterBirthYearFrom", 1979),
                        "filterBirthYearTo": user_filters.get("filterBirthYearTo", datetime.now().year - 18),
                        "filterNationalityCodes": filter_data.get("filterNationalityCodes", ""),
                        "filterFaceVerifiedOnly": filter_data.get("filterFaceVerifiedOnly", False),
                        "filterRecentlyActiveOnly": filter_data.get("filterRecentlyActiveOnly", False),
                    }
                )
                ok2, err2, status2 = await _post_payload(sess, fallback_payload)
                if ok2:
                    logger.info(
                        "Native Plus filter rejected (%s); applied core fallback filter for %s... (client exclude still active)",
                        err or status,
                        token[:10],
                    )
                    return True
                logger.warning(
                    "Fallback filter also failed for %s... Status=%s Error=%s",
                    token[:10],
                    status2,
                    err2,
                )
                return False

            logger.warning(
                "Failed to apply filter for token %s... Status=%s Error=%s",
                token[:10],
                status,
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
    """Reset account filter on Meeff server using RestClient.userUpdateFilterReset payload."""
    try:
        url = "https://api.meeff.com/user/updateFilter/v1"
        headers = build_meeff_headers(token=token, json_body=True)
        payload = {"reset": True, "locale": MEEFF_DEFAULT_LOCALE}

        async def _do_post(sess):
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
    """Push current saved filters (and global native excludes) to all active accounts."""
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


async def _render_account_dashboard(
    callback_query: types.CallbackQuery,
    user_id: int,
    tokens: List[Dict[str, Any]],
    account_index: int,
):
    if account_index < 0 or account_index >= len(tokens):
        await callback_query.answer("Account not found.", show_alert=True)
        return
    tok = tokens[account_index]
    uf = await get_user_filters(user_id, tok["token"]) or {}
    g_enabled = await get_exclude_filter_enabled(user_id)
    g_codes = await get_exclude_filter(user_id) if g_enabled else []
    text = format_filter_summary(tok.get("name", f"Account {account_index + 1}"), uf, g_codes, g_enabled)
    await callback_query.message.edit_text(
        text,
        reply_markup=get_account_filter_keyboard(account_index, uf),
        parse_mode="HTML",
    )


async def handle_filter_text_input(message: types.Message) -> bool:
    """Handle custom comma-separated country codes typed for an account's include/exclude filter."""
    user_id = message.from_user.id
    pending = filter_input_states.get(user_id)
    if not pending:
        return False

    filter_input_states.pop(user_id, None)
    raw = (message.text or "").strip()
    mode = pending.get("mode", "include")
    account_index = int(pending.get("idx", 0))

    tokens = await get_tokens(user_id)
    if account_index < 0 or account_index >= len(tokens):
        await message.answer("Account no longer exists.")
        return True

    tok = tokens[account_index]
    token = tok["token"]
    uf = await get_user_filters(user_id, token) or {}

    if raw.lower() in ("clear", "all", "none"):
        csv_val = ""
    else:
        csv_val = normalize_codes_csv(raw)

    if mode == "include":
        uf["filterNationalityCodes"] = csv_val
        uf["filterNationalityCode"] = csv_val
    else:
        uf["filterDeniedNationality"] = csv_val

    await set_user_filters(user_id, token, uf)
    applied = await apply_filter_for_account(token, user_id)

    g_enabled = await get_exclude_filter_enabled(user_id)
    g_codes = await get_exclude_filter(user_id) if g_enabled else []
    summary = format_filter_summary(tok.get("name", f"Account {account_index + 1}"), uf, g_codes, g_enabled)
    status_note = "✅ Applied to Meeff" if applied else "⚠️ Saved locally (API sync failed)"
    await message.answer(
        f"{summary}\n\n{status_note}",
        reply_markup=get_account_filter_keyboard(account_index, uf),
        parse_mode="HTML",
    )
    return True


async def set_account_filter(callback_query: types.CallbackQuery):
    """Handle account-specific & multi-country Meeff native filter callbacks."""
    user_id = callback_query.from_user.id
    data = callback_query.data
    tokens = await get_tokens(user_id)

    # --- Legacy + New Main Menu & Sync Toggle ---
    if data in ("toggle_request_filter", "flt_tog"):
        current_status = await is_request_filter_enabled_async(user_id)
        new_status = not current_status
        await set_request_filter_enabled(user_id, new_status)
        user_filter_states.setdefault(user_id, {})["request_filter_enabled"] = new_status
        await callback_query.message.edit_text(
            "🎛️ <b>Meeff Native Filter Settings</b>\n\n"
            "Select an account to configure multi-country include/exclude, age, distance, purposes & more:",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML",
        )
        await callback_query.answer(f"Sync Filter on Start: {'ON' if new_status else 'OFF'}")
        return True

    if data in ("meeff_filter_main", "flt_main"):
        filter_input_states.pop(user_id, None)
        await callback_query.message.edit_text(
            "🎛️ <b>Meeff Native Filter Settings</b>\n\n"
            "Select an account to configure multi-country include/exclude, age, distance, purposes & more:",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML",
        )
        await callback_query.answer()
        return True

    if data == "flt_sync_all":
        await callback_query.answer("Syncing all active accounts...")
        ok, failed = await sync_all_active_filters(user_id)
        await callback_query.message.edit_text(
            f"🎛️ <b>Meeff Native Filter Settings</b>\n\n"
            f"🚀 Sync complete: <b>{ok}</b> updated, <b>{failed}</b> failed.",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML",
        )
        return True

    # --- New Colon-Separated Callbacks (flt_*) ---
    if data.startswith("flt_acc:"):
        idx = int(data.split(":")[1])
        filter_input_states.pop(user_id, None)
        await _render_account_dashboard(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    if data.startswith("flt_g_m:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            await callback_query.message.edit_text(
                f"⚧ <b>Select Gender — {html.escape(tokens[idx].get('name', 'Account'))}</b>",
                reply_markup=get_gender_keyboard(idx, uf.get("filterGenderType", 5)),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("flt_g:"):
        _, idx_s, g_s = data.split(":")
        idx, g_val = int(idx_s), int(g_s)
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            uf["filterGenderType"] = g_val
            await set_user_filters(user_id, tok["token"], uf)
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer(f"Gender set to {_format_gender(g_val)}")
        return True

    if data.startswith("flt_a_m:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            await callback_query.message.edit_text(
                f"🎂 <b>Select Age Range — {html.escape(tokens[idx].get('name', 'Account'))}</b>\n"
                f"Current: <b>{_format_age_range(uf)}</b>",
                reply_markup=get_age_keyboard(idx, uf),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("flt_amin:") or data.startswith("flt_amax:"):
        parts = data.split(":")
        is_min = parts[0] == "flt_amin"
        idx, age_val = int(parts[1]), int(parts[2])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            current_year = datetime.now().year
            if is_min:
                uf["filterBirthYearTo"] = current_year - age_val
                if int(uf.get("filterBirthYearFrom", 1979)) > uf["filterBirthYearTo"]:
                    uf["filterBirthYearFrom"] = uf["filterBirthYearTo"]
            else:
                uf["filterBirthYearFrom"] = current_year - age_val
                y_to = int(uf.get("filterBirthYearTo", current_year - 18))
                if uf["filterBirthYearFrom"] > y_to:
                    uf["filterBirthYearTo"] = uf["filterBirthYearFrom"]
            await set_user_filters(user_id, tok["token"], uf)
            await apply_filter_for_account(tok["token"], user_id)
            await callback_query.message.edit_text(
                f"🎂 <b>Select Age Range — {html.escape(tok.get('name', 'Account'))}</b>\n"
                f"Current: <b>{_format_age_range(uf)}</b>",
                reply_markup=get_age_keyboard(idx, uf),
                parse_mode="HTML",
            )
            await callback_query.answer(f"Age range: {_format_age_range(uf)}")
        return True

    # Multi-Country Include (flt_n_*) & Native Exclude (flt_x_*)
    if data.startswith(("flt_n_m:", "flt_x_m:")):
        parts = data.split(":")
        mode = "include" if parts[0] == "flt_n_m" else "exclude"
        idx, page = int(parts[1]), int(parts[2])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            field_val = (
                (uf.get("filterNationalityCodes") or uf.get("filterNationalityCode", ""))
                if mode == "include"
                else uf.get("filterDeniedNationality", "")
            )
            csv_val = normalize_codes_csv(field_val)
            title = "🌍 Include Countries (Multi-Select)" if mode == "include" else "🚫 Native Exclude Countries (filterDeniedNationality)"
            await callback_query.message.edit_text(
                f"<b>{title}</b>\nAccount: <b>{html.escape(tok.get('name', 'Account'))}</b>\n"
                f"Selected: <code>{csv_val or ('All Countries' if mode == 'include' else 'None')}</code>\n\n"
                f"Tap countries to toggle, then tap <b>Apply & Back</b>.",
                reply_markup=get_multi_country_keyboard(idx, csv_val, mode=mode, page=page),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith(("flt_n_t:", "flt_x_t:")):
        parts = data.split(":")
        mode = "include" if parts[0] == "flt_n_t" else "exclude"
        idx, code, page = int(parts[1]), parts[2], int(parts[3])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            cur_val = (
                (uf.get("filterNationalityCodes") or uf.get("filterNationalityCode", ""))
                if mode == "include"
                else uf.get("filterDeniedNationality", "")
            )
            new_csv, _ = _toggle_code_csv(cur_val, code)
            if mode == "include":
                uf["filterNationalityCodes"] = new_csv
                uf["filterNationalityCode"] = new_csv
            else:
                uf["filterDeniedNationality"] = new_csv
            await set_user_filters(user_id, tok["token"], uf)
            title = "🌍 Include Countries (Multi-Select)" if mode == "include" else "🚫 Native Exclude Countries (filterDeniedNationality)"
            await callback_query.message.edit_text(
                f"<b>{title}</b>\nAccount: <b>{html.escape(tok.get('name', 'Account'))}</b>\n"
                f"Selected: <code>{new_csv or ('All Countries' if mode == 'include' else 'None')}</code>\n\n"
                f"Tap countries to toggle, then tap <b>Apply & Back</b>.",
                reply_markup=get_multi_country_keyboard(idx, new_csv, mode=mode, page=page),
                parse_mode="HTML",
            )
            await callback_query.answer(new_csv or ("All" if mode == "include" else "Cleared"))
        return True

    if data.startswith(("flt_n_all:", "flt_x_all:")):
        parts = data.split(":")
        mode = "include" if parts[0] == "flt_n_all" else "exclude"
        idx, page = int(parts[1]), int(parts[2])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            if mode == "include":
                uf["filterNationalityCodes"] = ""
                uf["filterNationalityCode"] = ""
            else:
                uf["filterDeniedNationality"] = ""
            await set_user_filters(user_id, tok["token"], uf)
            title = "🌍 Include Countries (Multi-Select)" if mode == "include" else "🚫 Native Exclude Countries (filterDeniedNationality)"
            await callback_query.message.edit_text(
                f"<b>{title}</b>\nAccount: <b>{html.escape(tok.get('name', 'Account'))}</b>\n"
                f"Selected: <code>{'All Countries' if mode == 'include' else 'None'}</code>",
                reply_markup=get_multi_country_keyboard(idx, "", mode=mode, page=page),
                parse_mode="HTML",
            )
            await callback_query.answer("Cleared!")
        return True

    if data.startswith(("flt_n_txt:", "flt_x_txt:")):
        parts = data.split(":")
        mode = "include" if parts[0] == "flt_n_txt" else "exclude"
        idx = int(parts[1])
        filter_input_states[user_id] = {"mode": mode, "idx": idx}
        label = "Include Countries" if mode == "include" else "Native Exclude Countries"
        await callback_query.message.edit_text(
            f"⌨️ <b>Enter {label}</b>\n\n"
            f"Send comma-separated 2-letter ISO codes (e.g. <code>KR, JP, US, TH</code>)\n"
            f"Or send <code>clear</code> to reset.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data=f"flt_acc:{idx}")]]
            ),
            parse_mode="HTML",
        )
        await callback_query.answer()
        return True

    if data.startswith(("flt_n_save:", "flt_x_save:")):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            ok = await apply_filter_for_account(tokens[idx]["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer("✅ Applied to Meeff!" if ok else "⚠️ Saved locally (API failed)")
        return True

    if data.startswith("flt_d_m:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            await callback_query.message.edit_text(
                f"📍 <b>Select Distance — {html.escape(tokens[idx].get('name', 'Account'))}</b>",
                reply_markup=get_distance_keyboard(idx, uf.get("filterDistance", 510)),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("flt_d:"):
        _, idx_s, dist_s = data.split(":")
        idx, dist_val = int(idx_s), int(dist_s)
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            uf["filterDistance"] = dist_val
            await set_user_filters(user_id, tok["token"], uf)
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer("Global" if dist_val > 500 else f"{dist_val} km")
        return True

    if data.startswith("flt_p_m:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            await callback_query.message.edit_text(
                f"🖼️ <b>Select Minimum Profile Photos — {html.escape(tokens[idx].get('name', 'Account'))}</b>",
                reply_markup=get_photo_count_keyboard(idx, uf.get("filterMinProfileImageCount", 1)),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("flt_p:"):
        _, idx_s, cnt_s = data.split(":")
        idx, cnt_val = int(idx_s), int(cnt_s)
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            uf["filterMinProfileImageCount"] = cnt_val
            await set_user_filters(user_id, tok["token"], uf)
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer(f"Min photos: {cnt_val}+")
        return True

    if data.startswith("flt_u_m:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            cur = normalize_codes_csv(uf.get("filterPurposes", ""))
            await callback_query.message.edit_text(
                f"🎯 <b>Select Purposes — {html.escape(tokens[idx].get('name', 'Account'))}</b>",
                reply_markup=get_purposes_keyboard(idx, cur),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("flt_u_t:"):
        _, idx_s, code = data.split(":")
        idx = int(idx_s)
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            if code == "CLEAR":
                new_csv = ""
            else:
                new_csv, _ = _toggle_code_csv(uf.get("filterPurposes", ""), code)
            uf["filterPurposes"] = new_csv
            await set_user_filters(user_id, tok["token"], uf)
            await callback_query.message.edit_text(
                f"🎯 <b>Select Purposes — {html.escape(tok.get('name', 'Account'))}</b>",
                reply_markup=get_purposes_keyboard(idx, new_csv),
                parse_mode="HTML",
            )
            await callback_query.answer("Updated")
        return True

    if data.startswith("flt_u_save:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            ok = await apply_filter_for_account(tokens[idx]["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer("✅ Purposes applied!" if ok else "Saved locally")
        return True

    if data.startswith("flt_l_m:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            cur = normalize_codes_csv(uf.get("filterLanguageCodes", ""), lowercase=True, max_items=3)
            await callback_query.message.edit_text(
                f"🗣️ <b>Select Languages (Max 3) — {html.escape(tokens[idx].get('name', 'Account'))}</b>\n"
                f"Selected: <code>{cur or 'Any'}</code>",
                reply_markup=get_languages_keyboard(idx, cur),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("flt_l_t:"):
        _, idx_s, code = data.split(":")
        idx = int(idx_s)
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            if code == "CLEAR":
                new_csv, limited = "", False
            else:
                new_csv, limited = _toggle_code_csv(uf.get("filterLanguageCodes", ""), code, lowercase=True, max_items=3)
            if limited:
                await callback_query.answer("Meeff allows max 3 languages!", show_alert=True)
                return True
            uf["filterLanguageCodes"] = new_csv
            await set_user_filters(user_id, tok["token"], uf)
            await callback_query.message.edit_text(
                f"🗣️ <b>Select Languages (Max 3) — {html.escape(tok.get('name', 'Account'))}</b>\n"
                f"Selected: <code>{new_csv or 'Any'}</code>",
                reply_markup=get_languages_keyboard(idx, new_csv),
                parse_mode="HTML",
            )
            await callback_query.answer(new_csv or "Any")
        return True

    if data.startswith("flt_l_save:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            ok = await apply_filter_for_account(tokens[idx]["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer("✅ Languages applied!" if ok else "Saved locally")
        return True

    if data.startswith(("flt_tg_rec:", "flt_tg_face:", "flt_tg_blk:")):
        cmd, idx_s = data.split(":")
        idx = int(idx_s)
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            if cmd == "flt_tg_rec":
                uf["filterRecentlyActiveOnly"] = not bool(uf.get("filterRecentlyActiveOnly", False))
                toast = f"Active Only: {'ON' if uf['filterRecentlyActiveOnly'] else 'OFF'}"
            elif cmd == "flt_tg_face":
                uf["filterFaceVerifiedOnly"] = not bool(uf.get("filterFaceVerifiedOnly", False))
                toast = f"Face Verified: {'ON' if uf['filterFaceVerifiedOnly'] else 'OFF'}"
            else:
                uf["filterNationalityBlock"] = 0 if uf.get("filterNationalityBlock", 0) else 1
                toast = f"Hide My Country: {'ON' if uf['filterNationalityBlock'] else 'OFF'}"
            await set_user_filters(user_id, tok["token"], uf)
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer(toast)
        return True

    if data.startswith("flt_copy:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            source_filters = dict(await get_user_filters(user_id, tokens[idx]["token"]) or {})
            active_tokens = await get_active_tokens(user_id)
            for t in active_tokens:
                await set_user_filters(user_id, t["token"], dict(source_filters))
            ok, failed = await sync_all_active_filters(user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer(f"Copied & synced to {ok}/{ok + failed} accounts!", show_alert=True)
        return True

    if data.startswith("flt_rst:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(tokens):
            ok = await reset_filter_for_account(tokens[idx]["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
            await callback_query.answer("Reset to Meeff default!" if ok else "Reset failed", show_alert=not ok)
        return True

    # --- Backward Compatibility with Old account_* Callbacks ---
    data_parts = data.split("_")
    if data.startswith("account_filter_") and not any(
        x in data for x in ["gender", "age", "nationality", "recent", "face", "back"]
    ):
        idx = int(data_parts[2])
        await _render_account_dashboard(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    if data.startswith("account_filter_back_"):
        idx = int(data_parts[3])
        await _render_account_dashboard(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    if data.startswith("account_filter_gender_"):
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            await callback_query.message.edit_text(
                "<b>Select Gender Filter:</b>",
                reply_markup=get_gender_keyboard(idx, uf.get("filterGenderType", 5)),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("account_filter_age_"):
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            await callback_query.message.edit_text(
                "<b>Select Age Filter:</b>",
                reply_markup=get_age_keyboard(idx, uf),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith("account_filter_nationality_"):
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            uf = await get_user_filters(user_id, tokens[idx]["token"]) or {}
            cur = normalize_codes_csv(uf.get("filterNationalityCodes") or uf.get("filterNationalityCode", ""))
            await callback_query.message.edit_text(
                "<b>Include Countries (Multi-Select):</b>",
                reply_markup=get_multi_country_keyboard(idx, cur, mode="include", page=0),
                parse_mode="HTML",
            )
        await callback_query.answer()
        return True

    if data.startswith(("account_filter_recent_", "account_filter_face_")):
        is_recent = data.startswith("account_filter_recent_")
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            key = "filterRecentlyActiveOnly" if is_recent else "filterFaceVerifiedOnly"
            uf[key] = not bool(uf.get(key, False))
            await set_user_filters(user_id, tok["token"], uf)
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
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
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    if data.startswith("account_age_"):
        age = int(data_parts[2])
        idx = int(data_parts[3])
        if 0 <= idx < len(tokens):
            tok = tokens[idx]
            uf = await get_user_filters(user_id, tok["token"]) or {}
            uf["filterBirthYearFrom"] = datetime.now().year - age
            await set_user_filters(user_id, tok["token"], uf)
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
        await callback_query.answer()
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
            await apply_filter_for_account(tok["token"], user_id)
            await _render_account_dashboard(callback_query, user_id, tokens, idx)
        await callback_query.answer()
        return True

    return False


async def meeff_filter_command(message: types.Message):
    """Main command to show Meeff Filter settings."""
    user_id = message.from_user.id
    await message.answer(
        "🎛️ <b>Meeff Native Filter Settings</b>\n\n"
        "Select an account to configure multi-country include/exclude, age, distance, purposes & more:",
        reply_markup=await get_meeff_filter_main_keyboard(user_id),
        parse_mode="HTML",
    )


async def set_filter(callback_query: types.CallbackQuery):
    return await set_account_filter(callback_query)


async def filter_command(message: types.Message):
    return await meeff_filter_command(message)
