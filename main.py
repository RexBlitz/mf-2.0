import asyncio
import logging
from datetime import datetime
from typing import Dict
import aiohttp
import html
from auth import ADMIN_USER_IDS, TEMP_PASSWORD, password_access, is_admin, has_valid_access, grant_password_access
from meeff_http import MEEFF_APP_VERSION, MEEFF_DEFAULT_LOCALE, build_meeff_headers, create_meeff_session, normalize_codes_csv
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, CallbackQuery
from collections import defaultdict
from aiogram.exceptions import TelegramBadRequest


from db import (
    set_token, resign_token_at_position, get_tokens, set_current_account, get_current_account, delete_token,
    get_exclude_filter, set_exclude_filter, get_exclude_filter_enabled, set_exclude_filter_enabled,
    get_custom_exclude_enabled, set_custom_exclude_enabled,
    set_user_filters, get_user_filters, get_all_user_filters, set_spam_filter, get_spam_filter,
    is_already_sent, toggle_token_status, get_active_tokens,
    get_token_status, set_account_active, get_info_card,
    set_individual_spam_filter, get_individual_spam_filter, get_all_spam_filters,get_spam_menu_data,
    list_all_collections, get_collection_summary, connect_to_collection,
    rename_user_collection, transfer_to_user, get_current_collection_info,
    get_spam_record_count, clear_spam_records,
    get_batches, create_batch, toggle_batch_status, set_batch_filter, get_batch_by_name,
    add_token_to_auto_batch, block_user, get_blocked_users, clear_blocked_users,
    get_automation_settings, set_automation_enabled, set_automation_lounge_message, 
    set_automation_chatroom_message, set_automation_accounts, add_automation_log, get_automation_log
)
# Make sure these other local modules are compatible if they also perform I/O
from lounge import send_lounge, send_lounge_all_tokens
from chatroom import send_message_to_everyone, send_message_to_everyone_all_tokens
from unsubscribe import unsubscribe_everyone
from filters import (
    meeff_filter_command, set_account_filter, get_meeff_filter_main_keyboard,
    set_filter, apply_filter_for_account, handle_filter_text_input, sync_all_active_filters
)
from allcountry import run_all_countries
from signup import signup_command, signup_callback_handler, signup_message_handler, signup_settings_command, user_signup_states
from friend_requests import run_requests, process_all_tokens, user_states, stop_markup
# --- NEW IMPORT ---
from automation import start_automation
from premium import premium_router

# --- Configuration & Setup ---
TARGET_CHANNEL_ID = -1002610862940
ACCOUNTS_PER_PAGE = 10 # Adjusted for extra button row

db_operation_states: Dict[int, Dict[str, str]] = defaultdict(dict)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN)
router = Router()
dp = Dispatcher()

# --- Constants ---
NATIONALITY_LIST = [
    ("RU", "🇷🇺 Russia"), ("UA", "🇺🇦 Ukraine"), ("BY", "🇧🇾 Belarus"), ("IR", "🇮🇷 Iran"), ("PH", "🇵🇭 Philippines"),
    ("PK", "🇵🇰 Pakistan"), ("US", "🇺🇸 USA"), ("IN", "🇮🇳 India"), ("DE", "🇩🇪 Germany"), ("FR", "🇫🇷 France"),
    ("BR", "🇧🇷 Brazil"), ("CN", "🇨🇳 China"), ("JP", "🇯🇵 Japan"), ("KR", "🇰🇷 Korea"), ("CA", "🇨🇦 Canada"),
    ("AU", "🇦🇺 Australia"), ("IT", "🇮🇹 Italy"), ("ES", "🇪🇸 Spain"), ("ZA", "🇿🇦 South Africa"), ("TR", "🇹🇷 Turkey")
]

async def get_settings_menu(user_id: int) -> InlineKeyboardMarkup:
    spam_filters = await get_all_spam_filters(user_id)
    any_spam_on = any(spam_filters.values())
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Manage Accounts", callback_data="manage_accounts|0"), InlineKeyboardButton(text="Meeff Filters", callback_data="show_filters")],
        [InlineKeyboardButton(text="Batch Management", callback_data="batch_management")],
        [InlineKeyboardButton(text=f"Spam Filters: {'ON' if any_spam_on else 'OFF'}", callback_data="spam_filter_menu")],
        [InlineKeyboardButton(text="🚫 Exclude Filter", callback_data="exclude_filter_menu")],
        [InlineKeyboardButton(text="DB Settings", callback_data="db_settings"), InlineKeyboardButton(text="Back", callback_data="back_to_menu")]
    ])

async def _build_exclude_filter_menu(user_id: int, sync_note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    codes = await get_exclude_filter(user_id)
    native_enabled = await get_exclude_filter_enabled(user_id)
    custom_enabled = await get_custom_exclude_enabled(user_id)
    codes_text = ", ".join(codes) if codes else "None"

    native_label = "✅ Native Exclude (Server): ON" if native_enabled else "❌ Native Exclude (Server): OFF"
    custom_label = "🛡️ Custom Exclude (Bot Skip): ON" if custom_enabled else "🚫 Custom Exclude (Bot Skip): OFF"

    sync_line = f"\n<b>Last Sync:</b> {sync_note}" if sync_note else ""
    text = (
        f"<b>🚫 Exclude Filter Controls</b>\n\n"
        f"• <b>Native Exclude (<code>filterDeniedNationality</code>):</b> "
        f"{'<b>ON</b> (Server-side)' if native_enabled else '<b>OFF</b>'}\n"
        f"• <b>Custom Exclude (Bot Client Skip):</b> "
        f"{'<b>ON</b> (Fallback skip before request)' if custom_enabled else '<b>OFF</b> (Pure Native test mode)'}\n"
        f"• <b>Global Excluded:</b> <code>{codes_text}</code>"
        f"{sync_line}\n\n"
        f"<i>Tip: Pure Native test ke liye <b>Native ON</b> aur <b>Custom OFF</b> rakhein.</i>\n"
        f"Send country codes to set (e.g. <code>US, KR, JP</code>) or <code>clear</code>."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=native_label, callback_data="toggle_exclude_filter")],
        [InlineKeyboardButton(text=custom_label, callback_data="toggle_custom_exclude_filter")],
        [InlineKeyboardButton(text="🚀 Sync Exclude to All Active Accounts", callback_data="sync_exclude_all")],
        [InlineKeyboardButton(text="Back", callback_data="settings_menu")],
    ])
    return text, markup

def get_db_settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Connect DB", callback_data="db_connect"), InlineKeyboardButton(text="Rename DB", callback_data="db_rename")], [InlineKeyboardButton(text="View DB", callback_data="db_view"), InlineKeyboardButton(text="Transfer DB", callback_data="db_transfer")], [InlineKeyboardButton(text="Back", callback_data="settings_menu")]])

def get_unsubscribe_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Unsubscribe Current", callback_data="unsub_current"), InlineKeyboardButton(text="Unsubscribe All", callback_data="unsub_all")], [InlineKeyboardButton(text="Back", callback_data="back_to_menu")]])

async def get_spam_filter_menu(user_id: int) -> InlineKeyboardMarkup:
    # Fetch spam data and blocked users in parallel
    menu_data, blocked_users = await asyncio.gather(
        get_spam_menu_data(user_id),
        get_blocked_users(user_id)
    )
    spam_filters = menu_data["filters"]
    counts = menu_data["counts"]
    blocked_count = len(blocked_users)

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"Chatroom: {'ON' if spam_filters['chatroom'] else 'OFF'}", callback_data="toggle_spam_chatroom"),
            InlineKeyboardButton(text=f"({counts['chatroom']})", callback_data="noop_count"),
            InlineKeyboardButton(text="Clear", callback_data="confirm_clear_spam_chatroom")
        ],
        [
            InlineKeyboardButton(text=f"Requests: {'ON' if spam_filters['request'] else 'OFF'}", callback_data="toggle_spam_request"),
            InlineKeyboardButton(text=f"({counts['request']})", callback_data="noop_count"),
            InlineKeyboardButton(text="Clear", callback_data="confirm_clear_spam_request")
        ],
        [
            InlineKeyboardButton(text=f"Lounge: {'ON' if spam_filters['lounge'] else 'OFF'}", callback_data="toggle_spam_lounge"),
            InlineKeyboardButton(text=f"({counts['lounge']})", callback_data="noop_count"),
            InlineKeyboardButton(text="Clear", callback_data="confirm_clear_spam_lounge")
        ],
        [
            InlineKeyboardButton(text=f"Block User ({blocked_count})", callback_data="view_blocked_users"),
            InlineKeyboardButton(text="Clear", callback_data="confirm_clear_blocked")
        ],
        [
            InlineKeyboardButton(text="Toggle All", callback_data="toggle_spam_all"),
            InlineKeyboardButton(text="Back", callback_data="settings_menu")
        ]
    ])

def get_account_view_menu(account_idx: int, page_idx: int) -> InlineKeyboardMarkup:
    # MODIFIED: Added page_idx to return button
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete Account", callback_data=f"confirm_delete_{account_idx}|{page_idx}")],
        [InlineKeyboardButton(text="Back", callback_data=f"manage_accounts|{page_idx}")]
    ])

def get_confirmation_menu(action_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes", callback_data=f"confirm_{action_type}"), InlineKeyboardButton(text="Cancel", callback_data="back_to_menu")]])

async def get_batch_management_menu(user_id: int) -> InlineKeyboardMarkup:
    batches = await get_batches(user_id)
    tokens = await get_tokens(user_id)
    buttons = []
    for batch in batches:
        batch_name = batch.get("name", "Unnamed")
        is_active = batch.get("active", True)
        status = "ON" if is_active else "OFF"
        filter_nat = batch.get("filter_nationality", "")
        nat_display = f" ({filter_nat})" if filter_nat else " (All)"

        buttons.append([
            InlineKeyboardButton(text=f"{batch_name}{nat_display}", callback_data=f"view_batch_{batch_name}"),
            InlineKeyboardButton(text=status, callback_data=f"toggle_batch_{batch_name}"),
            InlineKeyboardButton(text="Filter", callback_data=f"batch_filter_{batch_name}")
        ])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="settings_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_batch_filter_menu(batch_name: str, current_csv: str = "") -> InlineKeyboardMarkup:
    countries = [
        ("KR", "KR"), ("JP", "JP"), ("US", "US"), ("GB", "GB"),
        ("RU", "RU"), ("UA", "UA"), ("BY", "BY"), ("TR", "TR"),
        ("DE", "DE"), ("FR", "FR"), ("ES", "ES"), ("IT", "IT"),
        ("BR", "BR"), ("CA", "CA"), ("AU", "AU"), ("PH", "PH"),
        ("PK", "PK"), ("IN", "IN"), ("ID", "ID"), ("VN", "VN"),
        ("TH", "TH"), ("CN", "CN"), ("IR", "IR"), ("ZA", "ZA"),
    ]
    selected = {c.strip().upper() for c in (current_csv or "").split(",") if c.strip()}
    all_mark = "✅ " if not selected else ""
    buttons = [[InlineKeyboardButton(text=f"{all_mark}All Countries (Clear)", callback_data=f"batch_nat_all|{batch_name}")]]
    row = []
    for i, (code, label) in enumerate(countries):
        mark = "✅ " if code in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"batch_nat|{code}|{batch_name}"))
        if len(row) == 4 or i == len(countries) - 1:
            buttons.append(row)
            row = []
    buttons.append([InlineKeyboardButton(text="🚀 Apply to Batch", callback_data=f"batch_apply|{batch_name}")])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="batch_management")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

start_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Current Request", callback_data="send_request_menu"), InlineKeyboardButton(text="Request All", callback_data="start_all")]])
send_request_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Start Request", callback_data="start"), InlineKeyboardButton(text="All Countries", callback_data="all_countries")], [InlineKeyboardButton(text="Back", callback_data="back_to_menu")]])
back_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="back_to_menu")]])

# --- Command Handlers ---
@router.message(Command("password"))
async def password_command(message: Message):
    try:
        if message.text.split()[1] == TEMP_PASSWORD:
            grant_password_access(message.chat.id, hours=1)
            await message.reply("Access granted for one hour.")
        else:
            await message.reply("Incorrect password.")
    except IndexError: await message.reply("Usage: /password <password>")
    finally:
        try: await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception as e: logger.error(f"Failed to delete password message: {e}")

@router.message(Command("start"))
async def start_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized. Use /password to get access.")
    state = user_states.setdefault(message.chat.id, {})
    status = await message.reply("<b>Meeff Bot Dashboard</b>...", reply_markup=start_markup, parse_mode="HTML")
    state.update({"status_message_id": status.message_id, "pinned_message_id": None})

@router.message(Command("signup"))
async def signup_cmd(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    await signup_command(message)

@router.message(Command("signup_settings"))
async def signup_settings_cmd(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    await signup_settings_command(message)

@router.message(Command("signin"))
async def signin_cmd(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    from signup import user_signup_states, BACK_TO_SIGNUP
    # Redirect signin command to the unified email input stage
    user_signup_states[message.from_user.id] = {"stage": "multi_signin_emails"}
    await message.reply("<b>Sign In (Single or Multi)</b>\n\nEnter one or more emails:", reply_markup=BACK_TO_SIGNUP, parse_mode="HTML")

@router.message(Command("skip"))
async def skip_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    await message.reply("<b>Unsubscribe Options</b>...", reply_markup=get_unsubscribe_menu(), parse_mode="HTML")

@router.message(Command("send_lounge_all"))
async def send_lounge_all(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2: return await message.reply("<b>Usage</b>\n<code>/send_lounge_all &lt;message&gt;</code>", parse_mode="HTML")
    
    custom_message = parts[1]
    active_tokens_data = await get_active_tokens(user_id)
    if not active_tokens_data: return await message.reply("No active tokens found.")

    spam_enabled = await get_individual_spam_filter(user_id, "lounge")
    status = await message.reply(f"<b>Starting Lounge Messages</b> for {len(active_tokens_data)} accounts...", parse_mode="HTML")
    await send_lounge_all_tokens(active_tokens_data, custom_message, status, bot, user_id, spam_enabled, user_id)

@router.message(Command("lounge"))
async def lounge_command(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    token = await get_current_account(user_id)
    if not token: return await message.reply("No active account found.")
    
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2: return await message.reply("<b>Usage</b>\n<code>/lounge &lt;message&gt;</code>", parse_mode="HTML")
    
    custom_message = parts[1]
    spam_enabled = await get_individual_spam_filter(user_id, "lounge")
    status_message = await message.reply(f"<b>Starting Lounge Messaging...</b>", parse_mode="HTML")
    await send_lounge(token, custom_message, status_message, bot, user_id, spam_enabled, user_id)


@router.message(Command("chatroom"))
async def send_to_all_command(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    token = await get_current_account(user_id)
    if not token: return await message.reply("No active account found.")

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2: return await message.reply("<b>Usage</b>\n<code>/chatroom &lt;message&gt;</code>", parse_mode="HTML")
    
    custom_message = parts[1]
    spam_enabled = await get_individual_spam_filter(user_id, "chatroom")
    status_message = await message.reply("<b>Starting Chatroom Messages...</b>", parse_mode="HTML")

    sent_ids = await is_already_sent(user_id, "chatroom", None, bulk=True) if spam_enabled else set()
    sent_ids_lock = asyncio.Lock()
    
    total, sent, filtered = await send_message_to_everyone(
        token, custom_message, user_id, spam_enabled, user_id,
        sent_ids, sent_ids_lock
    )
    await status_message.edit_text(f"<b>Complete</b>\nTotal: {total}, Sent: {sent}, Filtered: {filtered}", parse_mode="HTML")

@router.message(Command("send_chat_all"))
async def send_chat_all(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2: return await message.reply("<b>Usage</b>\n<code>/send_chat_all &lt;message&gt;</code>", parse_mode="HTML")

    custom_message = parts[1]
    active_tokens = await get_active_tokens(user_id)
    if not active_tokens: return await message.reply("No active tokens found.")

    tokens = [t["token"] for t in active_tokens]
    token_names = {t["token"]: t["name"] for t in active_tokens}
    spam_enabled = await get_individual_spam_filter(user_id, "chatroom")
    status = await message.reply(f"<b>Starting Multi-Account Chatroom ({len(tokens)})...</b>", parse_mode="HTML")
    
    await send_message_to_everyone_all_tokens(
        tokens, custom_message, status, bot, user_id, spam_enabled, token_names, True, user_id
    )

@router.message(Command("invoke"))
async def invoke_command(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    
    # ... (Same invoke logic as before, abbreviated for space)
    # Default behavior: check active
    active_tokens = await get_active_tokens(user_id)
    if not active_tokens: return await message.reply("No active accounts.")
    status_msg = await message.reply("<b>Checking Active Accounts...</b>", parse_mode="HTML")
    
    # Simple check logic...
    disabled = []
    async with create_meeff_session() as session:
        for t in active_tokens:
            try:
                headers = build_meeff_headers(token=t["token"], json_body=False)
                async with session.get("https://api.meeff.com/facetalk/vibemeet/history/count/v1", params={"locale": MEEFF_DEFAULT_LOCALE}, headers=headers, timeout=15) as resp:
                    data = await resp.json(content_type=None)
                    if data.get("errorCode") == "AuthRequired": disabled.append(t)
            except Exception:
                pass
    
    if disabled:
        for acc in disabled: await delete_token(user_id, acc["token"])
        await status_msg.edit_text(f"<b>Cleanup:</b> Removed {len(disabled)} disabled accounts.", parse_mode="HTML")
    else:
        await status_msg.edit_text("<b>All active accounts operational.</b>", parse_mode="HTML")

@router.message(Command("settings"))
async def settings_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    await message.reply("<b>Settings Menu</b>", reply_markup=await get_settings_menu(message.chat.id), parse_mode="HTML")

async def build_automation_menu(user_id: int) -> InlineKeyboardMarkup:
    from automation import is_automation_running
    settings = await get_automation_settings(user_id)
    running = is_automation_running(user_id)
    enabled = settings["enabled"] and running

    lounge_msg = settings.get("lounge_message", "") or "Not set"
    chatroom_msg = settings.get("chatroom_message", "") or "Not set"
    status_icon = "ON" if enabled else "OFF"

    buttons = [
        [InlineKeyboardButton(text=f"Automation: {status_icon}", callback_data="toggle_automation")],
        [InlineKeyboardButton(text=f"Lounge Msg: {lounge_msg[:20]}...", callback_data="auto_set_lounge")],
        [InlineKeyboardButton(text=f"Chatroom Msg: {chatroom_msg[:20]}...", callback_data="auto_set_chatroom")],
        [InlineKeyboardButton(text="Select Accounts", callback_data="auto_select_accounts")],
        [InlineKeyboardButton(text="Run Action Now", callback_data="auto_run_action")],
        [InlineKeyboardButton(text="View Log", callback_data="auto_view_log")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(Command("automation"))
async def automation_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    user_id = message.chat.id
    markup = await build_automation_menu(user_id)
    await message.reply("<b>Automation Menu</b>", reply_markup=markup, parse_mode="HTML")

@router.message(Command("block"))
async def block_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    args = message.text.split()
    if len(args) < 2: return await message.reply("Usage: /block meeff_user_id")
    await block_user(message.chat.id, args[1])
    await message.reply(f"✅ Blocked user: <code>{html.escape(args[1])}</code>", parse_mode="HTML")

@router.message(Command("add"))
async def add_person_command(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    args = message.text.strip().split()
    if len(args) < 2: return await message.reply("Usage: /add <person_id>")
    
    token = await get_current_account(user_id)
    if not token: return await message.reply("No active account found.")

    person_id = args[1]
    url = "https://api.meeff.com/user/undoableAnswer/v5"
    params = {"userId": person_id, "isOkay": "1", "locale": MEEFF_DEFAULT_LOCALE}
    headers = build_meeff_headers(token=token, json_body=False)
    
    try:
        async with create_meeff_session() as session:
            async with session.get(url, params=params, headers=headers, timeout=15) as response:
                data = await response.json(content_type=None) or {}
                await message.reply(f"Response: {data.get('errorCode') or 'Success'}")
    except Exception as e:
        await message.reply(f"Error: {e}")

@router.message(Command("refresh"))
async def refresh_tokens_command(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")

    from device_info import get_or_create_device_info_for_account, store_device_info_for_token

    all_tokens = await get_tokens(user_id)
    active_tokens = [t for t in all_tokens if t.get("active", True)]
    if not active_tokens:
        return await message.reply("No active accounts found.")

    status_msg = await message.reply(
        f"<b>🔄 Refreshing {len(active_tokens)} active account(s)...</b>",
        parse_mode="HTML"
    )

    success, failed, banned = 0, 0, 0
    failed_names = []
    banned_names = []

    async with create_meeff_session() as session:
        for idx, token_info in enumerate(active_tokens):
            old_token = token_info["token"]
            name = token_info.get("name", f"Account {idx + 1}")
            email = token_info.get("email")

            await status_msg.edit_text(
                f"<b>🔄 Refreshing tokens...</b>\n[{idx+1}/{len(active_tokens)}] Processing: <b>{html.escape(name)}</b>",
                parse_mode="HTML"
            )

            headers = build_meeff_headers(token=old_token, json_body=True)

            # Warm-up call
            try:
                async with session.post(
                    "https://api.meeff.com/api/init/v2",
                    json={"platform": "android", "version": MEEFF_APP_VERSION, "locale": MEEFF_DEFAULT_LOCALE},
                    headers=headers,
                ) as resp:
                    await resp.text()
            except Exception as e:
                logger.warning("Warm-up call failed for %s: %s", name, e)

            # Fetch or create persistent device fingerprint bound to this account/email
            device_info = await get_or_create_device_info_for_account(user_id, old_token, email=email)
            from device_info import get_api_payload_with_device_info
            device_payload = get_api_payload_with_device_info(
                {"appVersion": MEEFF_APP_VERSION, "locale": MEEFF_DEFAULT_LOCALE}, device_info
            )

            # Login call — old token in header, device payload in body
            j: dict = {}
            try:
                async with session.post(
                    "https://api.meeff.com/user/login/v4",
                    json=device_payload,
                    headers=headers,
                ) as resp:
                    j = await resp.json(content_type=None) or {}
            except Exception as e:
                logger.error("Login failed for %s: %s", name, e)
                j = {"errorMessage": str(e)}

            new_token = j.get("accessToken") or j.get("token")
            if new_token:
                # Find position in full token list (preserve order — mf-2.0 style)
                position = next(
                    (i for i, t in enumerate(all_tokens) if t["token"] == old_token), None
                )
                if position is not None:
                    try:
                        await resign_token_at_position(
                            user_id, position, new_token, name,
                            email=email,
                            password=token_info.get("password"),
                            filters=token_info.get("filters"),
                        )
                        # Re-key device info to new token
                        await store_device_info_for_token(user_id, new_token, device_info)
                        success += 1
                    except Exception as e:
                        logger.error("Failed to save refreshed token for %s: %s", name, e)
                        failed += 1
                        failed_names.append(name)
                else:
                    failed += 1
                    failed_names.append(name)
            else:
                err_code = j.get("errorCode", "")
                err_msg = (j.get("errorMessage") or "").lower()
                if any(k in err_msg for k in ("ban", "suspend", "block")):
                    banned += 1
                    banned_names.append(name)
                else:
                    failed += 1
                    failed_names.append(f"{name} ({err_code or err_msg})")

            await asyncio.sleep(1)

    # Final summary
    lines = [f"<b>🔄 Refresh Complete</b>\n\n✅ Refreshed: {success}"]
    if banned:
        lines.append(f"🚫 Banned: {banned} — {', '.join(html.escape(n) for n in banned_names)}")
    if failed:
        lines.append(f"❌ Failed: {failed} — {', '.join(html.escape(n) for n in failed_names)}")
    await status_msg.edit_text("\n".join(lines), parse_mode="HTML")


@router.message()
async def handle_new_token(message: Message):
    if message.text and message.text.startswith("/"): return
    user_id = message.from_user.id
    if message.from_user.is_bot: return
    if await signup_message_handler(message): return

    if await handle_filter_text_input(message): return

    # --- Automation / Exclude filter inputs ---
    signup_state = user_signup_states.get(user_id, {})
    stage = signup_state.get("stage")
    if stage == "set_exclude_filter":
        text = (message.text or "").strip()
        if text.lower() == "clear":
            await set_exclude_filter(user_id, [])
            ok, fail = await sync_all_active_filters(user_id)
            await message.answer(
                f"✅ Native Exclude cleared & synced (<b>{ok}</b> updated, {fail} failed).",
                reply_markup=await get_settings_menu(user_id),
                parse_mode="HTML",
            )
        else:
            csv_codes = normalize_codes_csv(text)
            codes = [c for c in csv_codes.split(",") if c]
            await set_exclude_filter(user_id, codes)
            ok, fail = await sync_all_active_filters(user_id)
            await message.answer(
                f"✅ <b>Native Exclude (`filterDeniedNationality`) updated:</b> <code>{', '.join(codes)}</code>\n"
                f"🚀 Synced to active accounts: <b>{ok}</b> updated, {fail} failed.",
                reply_markup=await get_settings_menu(user_id),
                parse_mode="HTML",
            )
        user_signup_states.pop(user_id, None)
        return

    if stage == "auto_set_lounge" and message.text:
        await set_automation_lounge_message(user_id, message.text.strip())
        user_signup_states.pop(user_id, None)
        await message.answer("✅ <b>Lounge message updated.</b>", reply_markup=await build_automation_menu(user_id), parse_mode="HTML")
        return

    if stage == "auto_set_chatroom" and message.text:
        await set_automation_chatroom_message(user_id, message.text.strip())
        user_signup_states.pop(user_id, None)
        await message.answer("✅ <b>Chatroom message updated.</b>", reply_markup=await build_automation_menu(user_id), parse_mode="HTML")
        return

    state = db_operation_states.get(user_id)
    if state:
        operation, text = state.get("operation"), message.text.strip()
        msg = await message.reply("<b>Processing...</b>", parse_mode="HTML")
        success, result_msg = False, "Invalid operation."
        if operation == "connect_db":
            collection_name = f"user_{text}" if not text.startswith("user_") else text
            success, result_msg = await connect_to_collection(collection_name, user_id)
        elif operation == "rename_db":
            success, result_msg = await rename_user_collection(user_id, text)
        elif operation == "transfer_db":
            try: success, result_msg = await transfer_to_user(user_id, int(text))
            except ValueError: result_msg = "Invalid user ID."
        await msg.edit_text(f"<b>{'Success' if success else 'Failed'}</b>: {result_msg}", parse_mode="HTML")
        db_operation_states.pop(user_id, None)
        return

    if not has_valid_access(user_id): return await message.reply("You are not authorized.")

    if message.text:
        token_data = message.text.strip().split(" ", 1)
        token = token_data[0]
        if len(token) < 100: return await message.reply("Invalid token format.")
        
        status_msg = await message.reply("<b>Saving Token...</b>", parse_mode="HTML")

        account_name = token_data[1] if len(token_data) > 1 else f"Account {len(await get_tokens(user_id)) + 1}"
        token_index = await set_token(user_id, token, account_name)
        if token_index != -1:
            await add_token_to_auto_batch(user_id, token_index)
        await status_msg.edit_text(f"✅ <b>Token Saved</b>: '<code>{html.escape(account_name)}</code>'.", parse_mode="HTML")

async def show_manage_accounts_menu(callback_query: CallbackQuery, page_idx: int = 0):
    user_id = callback_query.from_user.id
    tokens = await get_tokens(user_id)
    total_accounts = len(tokens)
    current_token = await get_current_account(user_id)

    if not tokens:
        return await callback_query.message.edit_text("<b>No Accounts Found</b>...", reply_markup=back_markup, parse_mode="HTML")

    total_pages = (total_accounts + ACCOUNTS_PER_PAGE - 1) // ACCOUNTS_PER_PAGE
    page_idx = max(0, min(page_idx, total_pages - 1)) 
    start_idx = page_idx * ACCOUNTS_PER_PAGE
    end_idx = min(start_idx + ACCOUNTS_PER_PAGE, total_accounts)

    visible_tokens = tokens[start_idx:end_idx]
    all_filters = await get_all_user_filters(user_id)

    buttons = []
    for i, tok in enumerate(visible_tokens):
        global_idx = start_idx + i 
        is_current = "🔹" if tok['token'] == current_token else "▫️"
        
        token_filters = all_filters.get(tok['token'], {})
        nationality_code = normalize_codes_csv(token_filters.get("filterNationalityCodes") or token_filters.get("filterNationalityCode", ""))
        nat_btn = (nationality_code if len(nationality_code) <= 8 else f"{len(nationality_code.split(','))} nat") if nationality_code else "All"

        account_name = html.escape(tok['name'][:15])
        display_name = f"{account_name}"

        # --- UPDATED BUTTON LAYOUT: Added "Nation" button ---
        buttons.append([
            InlineKeyboardButton(text=f"{is_current} {display_name}", callback_data=f"set_account_{global_idx}|{page_idx}"),
            InlineKeyboardButton(text="ON" if tok.get('active', True) else "OFF", callback_data=f"toggle_status_{global_idx}|{page_idx}"),
            InlineKeyboardButton(text=f"Nation: {nat_btn}", callback_data=f"manage_acc_filter|{global_idx}|{page_idx}"),
            InlineKeyboardButton(text="View", callback_data=f"view_account_{global_idx}|{page_idx}")
        ])

    pagination_row = []
    if page_idx > 0:
        pagination_row.append(InlineKeyboardButton(text="« Previous", callback_data=f"manage_accounts|{page_idx - 1}"))
    pagination_row.append(InlineKeyboardButton(text=f"{page_idx + 1}/{total_pages}", callback_data="noop_page"))
    if page_idx < total_pages - 1:
        pagination_row.append(InlineKeyboardButton(text="Next »", callback_data=f"manage_accounts|{page_idx + 1}"))
    
    if pagination_row: buttons.append(pagination_row)
    buttons.append([InlineKeyboardButton(text="Back", callback_data="settings_menu")])
    
    menu_text = f"<b>Manage Accounts (Page {page_idx + 1}/{total_pages})</b>\nCurrently selected: {'Yes' if current_token else 'No'}"

    try:
        await callback_query.message.edit_text(menu_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in e.message: logger.error(f"Error editing message: {e}")
        await callback_query.answer()


async def show_batch_accounts_menu(callback_query: CallbackQuery, batch_name: str):
    user_id = callback_query.from_user.id
    batch = await get_batch_by_name(user_id, batch_name)
    if not batch: return await callback_query.answer("Batch not found.", show_alert=True)

    tokens = await get_tokens(user_id)
    token_indices = batch.get("token_indices", [])
    batch_tokens = [tokens[idx] for idx in token_indices if idx < len(tokens)]
    real_indices = [idx for idx in token_indices if idx < len(tokens)]

    if not batch_tokens:
        return await callback_query.message.edit_text(f"<b>{html.escape(batch_name)}</b>\nNo accounts found.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="batch_management")]]), parse_mode="HTML")

    current_token = await get_current_account(user_id)
    all_filters = await get_all_user_filters(user_id)

    buttons = []
    for i, tok in enumerate(batch_tokens):
        global_index = real_indices[i]
        is_current = "🔹" if tok['token'] == current_token else "▫️"
        token_filters = all_filters.get(tok['token'], {})
        nationality_code = normalize_codes_csv(token_filters.get("filterNationalityCodes") or token_filters.get("filterNationalityCode", ""))
        nat_btn = (nationality_code if len(nationality_code) <= 8 else f"{len(nationality_code.split(','))} nat") if nationality_code else "All"
        account_name = html.escape(tok['name'][:20])

        buttons.append([
            InlineKeyboardButton(text=f"{is_current} {account_name}", callback_data=f"batch_select|{batch_name}|{global_index}"),
            InlineKeyboardButton(text="ON" if tok.get('active', True) else "OFF", callback_data=f"batch_toggle|{batch_name}|{global_index}"),
            InlineKeyboardButton(text=f"Nation: {nat_btn}", callback_data=f"batch_acc_filter|{batch_name}|{global_index}"),
            InlineKeyboardButton(text="View", callback_data=f"batch_view|{batch_name}|{global_index}")
        ])

    buttons.append([
        InlineKeyboardButton(text="Back", callback_data="batch_management")
    ])
    await callback_query.message.edit_text(f"<b>{html.escape(batch_name)} - Manage Accounts</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@router.callback_query()
async def callback_handler(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    page_idx = 0
    original_data = data
    
    # Updated pagination check to exclude multi-part filter/batch commands
    if "|" in data and not data.startswith((
        "batch_select|", "batch_toggle|", "batch_view|", "batch_acc_filter|",
        "batch_acc_nat_", "batch_acc_apply|", "batch_nat|", "batch_nat_all|", "batch_apply|",
        "manage_acc_filter|", "manage_acc_nat_", "manage_acc_apply|"
    )):
        data_parts = data.split("|")
        try:
            if len(data_parts) > 1 and data_parts[-1].isdigit():
                page_idx = int(data_parts[-1])
                data = "|".join(data_parts[:-1])
        except ValueError: pass

    if await signup_callback_handler(callback_query): return
    if not has_valid_access(user_id): return await callback_query.answer("You are not authorized.")
    
    state = user_states.setdefault(user_id, {})
    
    if data == "manage_accounts":
        await show_manage_accounts_menu(callback_query, page_idx)
    
    # --- NEW: Main Menu Account Multi-Country Filter Logic ---
    elif data.startswith("manage_acc_filter|"):
        # Format: manage_acc_filter|{global_index}|{page_idx}
        parts = data.split("|")
        if len(parts) >= 3:
            try:
                global_index = int(parts[1])
                menu_page_idx = int(parts[2])
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    token_filters = await get_user_filters(user_id, tok['token']) or {}
                    current_nat = normalize_codes_csv(token_filters.get("filterNationalityCodes") or token_filters.get("filterNationalityCode", ""))
                    selected = set(c for c in current_nat.split(",") if c)

                    buttons = []
                    all_mark = "✅ " if not selected else ""
                    buttons.append([InlineKeyboardButton(text=f"{all_mark}All Countries (Clear)", callback_data=f"manage_acc_nat_all|{global_index}|{menu_page_idx}")])

                    row = []
                    for i, (code, name) in enumerate(NATIONALITY_LIST):
                        mark = "✅ " if code in selected else ""
                        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"manage_acc_nat_{code}|{global_index}|{menu_page_idx}"))
                        if len(row) == 2 or i == len(NATIONALITY_LIST) - 1:
                            buttons.append(row)
                            row = []

                    buttons.append([
                        InlineKeyboardButton(text="🎛️ Full Native Filters", callback_data=f"flt_acc:{global_index}"),
                        InlineKeyboardButton(text="💾 Apply & Back", callback_data=f"manage_acc_apply|{global_index}|{menu_page_idx}"),
                    ])

                    await callback_query.message.edit_text(
                        f"<b>Multi-Country Filter — {html.escape(tok['name'])}</b>\n\nSelected: <code>{current_nat or 'All Countries'}</code>\nTap to toggle countries, then tap <b>Apply & Back</b>:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                        parse_mode="HTML"
                    )
            except (ValueError, IndexError):
                await callback_query.answer("Invalid account.", show_alert=True)

    elif data.startswith("manage_acc_nat_"):
        # Format: manage_acc_nat_{CODE}|{global_index}|{page_idx}
        parts = data.split("|")
        if len(parts) >= 3:
            nat_code = parts[0].replace("manage_acc_nat_", "")
            try:
                global_index = int(parts[1])
                menu_page_idx = int(parts[2])
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    user_filters = await get_user_filters(user_id, tok['token']) or {}
                    if nat_code == "all":
                        new_csv = ""
                    else:
                        cur_list = [c for c in normalize_codes_csv(user_filters.get("filterNationalityCodes") or user_filters.get("filterNationalityCode", "")).split(",") if c]
                        if nat_code.upper() in cur_list:
                            cur_list.remove(nat_code.upper())
                        else:
                            cur_list.append(nat_code.upper())
                        new_csv = ",".join(cur_list)

                    user_filters["filterNationalityCode"] = new_csv
                    user_filters["filterNationalityCodes"] = new_csv
                    await set_user_filters(user_id, tok['token'], user_filters)

                    selected = set(c for c in new_csv.split(",") if c)
                    buttons = [[InlineKeyboardButton(text=f"{'✅ ' if not selected else ''}All Countries (Clear)", callback_data=f"manage_acc_nat_all|{global_index}|{menu_page_idx}")]]
                    row = []
                    for i, (code, name) in enumerate(NATIONALITY_LIST):
                        mark = "✅ " if code in selected else ""
                        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"manage_acc_nat_{code}|{global_index}|{menu_page_idx}"))
                        if len(row) == 2 or i == len(NATIONALITY_LIST) - 1:
                            buttons.append(row)
                            row = []
                    buttons.append([
                        InlineKeyboardButton(text="🎛️ Full Native Filters", callback_data=f"flt_acc:{global_index}"),
                        InlineKeyboardButton(text="💾 Apply & Back", callback_data=f"manage_acc_apply|{global_index}|{menu_page_idx}"),
                    ])
                    await callback_query.message.edit_text(
                        f"<b>Multi-Country Filter — {html.escape(tok['name'])}</b>\n\nSelected: <code>{new_csv or 'All Countries'}</code>\nTap to toggle countries, then tap <b>Apply & Back</b>:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                        parse_mode="HTML"
                    )
                    await callback_query.answer(new_csv or "All Countries")
            except (ValueError, IndexError):
                await callback_query.answer("Invalid account.", show_alert=True)

    elif data.startswith("manage_acc_apply|"):
        parts = data.split("|")
        if len(parts) >= 3:
            try:
                global_index = int(parts[1])
                menu_page_idx = int(parts[2])
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    ok = await apply_filter_for_account(tok['token'], user_id)
                    uf = await get_user_filters(user_id, tok['token']) or {}
                    nat = uf.get("filterNationalityCodes") or "All Countries"
                    await callback_query.answer(f"Applied: {nat}" if ok else "Saved locally (API failed)", show_alert=not ok)
                    await show_manage_accounts_menu(callback_query, menu_page_idx)
            except (ValueError, IndexError):
                await callback_query.answer("Invalid account.", show_alert=True)
    # -------------------------------------------

    elif data == "db_settings":
        current_info = await get_current_collection_info(user_id)
        info_text = f"<b>DB:</b> <code>{html.escape(current_info['collection_name'])}</code>\nAccounts: {current_info['summary'].get('tokens_count', 0)}" if current_info["exists"] else "No database found."
        await callback_query.message.edit_text(f"<b>Database Settings</b>\n{info_text}", reply_markup=get_db_settings_menu(), parse_mode="HTML")
    elif data in ("db_connect", "db_rename", "db_transfer"):
        prompts = {"db_connect": "Enter collection name", "db_rename": "Enter new name", "db_transfer": "Enter target Telegram user ID"}
        db_operation_states[user_id] = {"operation": data}
        await callback_query.message.edit_text(f"<b>{prompts[data]}:</b>", parse_mode="HTML")
    elif data == "db_view":
        collections = await list_all_collections()
        text = "\n\n".join([f"<b>{i}.</b> <code>{html.escape(c['collection_name'])}</code>\n  Accounts: {c['summary'].get('tokens_count', 0)}" for i, c in enumerate(collections[:10], 1)]) or "No Collections Found."
        await callback_query.message.edit_text(text, reply_markup=get_db_settings_menu(), parse_mode="HTML")
    elif data in ("unsub_current", "unsub_all"):
        confirm_text, count = ("current account", 1) if data == "unsub_current" else (f"all {len(await get_active_tokens(user_id))} active accounts", -1)
        await callback_query.message.edit_text(f"<b>Confirm:</b> Unsubscribe {confirm_text}?", reply_markup=get_confirmation_menu(data), parse_mode="HTML")
    elif data == "confirm_unsub_current":
        token = await get_current_account(user_id)
        if not token: return await callback_query.message.edit_text("No active account found.", reply_markup=back_markup, parse_mode="HTML")
        msg = await callback_query.message.edit_text("<b>Unsubscribing Current Account...</b>", parse_mode="HTML")
        await unsubscribe_everyone(token, status_message=msg, bot=bot, chat_id=user_id, user_id=user_id)
    elif data == "confirm_unsub_all":
        active_tokens = await get_active_tokens(user_id)
        if not active_tokens: return await callback_query.message.edit_text("No active accounts found.", reply_markup=back_markup, parse_mode="HTML")
        msg = await callback_query.message.edit_text(f"<b>Unsubscribing All Accounts ({len(active_tokens)})...</b>", parse_mode="HTML")
        for i, token_obj in enumerate(active_tokens, 1):
            await msg.edit_text(f"Processing account {i}/{len(active_tokens)}: {html.escape(token_obj['name'])}", parse_mode="HTML")
            await unsubscribe_everyone(token_obj["token"], user_id=user_id)
        await msg.edit_text(f"<b>Unsubscribe Complete</b>\nSuccessfully unsubscribed {len(active_tokens)} accounts.", parse_mode="HTML")
    elif data == "send_request_menu":
        await callback_query.message.edit_text("<b>Send Request Options</b>", reply_markup=send_request_markup, parse_mode="HTML")
    elif data == "exclude_filter_menu":
        text, markup = await _build_exclude_filter_menu(user_id)
        await callback_query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        user_signup_states[user_id] = {"stage": "set_exclude_filter"}

    elif data == "toggle_exclude_filter":
        new_enabled = not (await get_exclude_filter_enabled(user_id))
        await set_exclude_filter_enabled(user_id, new_enabled)
        ok, fail = await sync_all_active_filters(user_id)
        text, markup = await _build_exclude_filter_menu(user_id, sync_note=f"{ok} updated, {fail} failed")
        await callback_query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback_query.answer(f"Native Exclude {'ON' if new_enabled else 'OFF'} ({ok} synced)")

    elif data == "toggle_custom_exclude_filter":
        new_custom = not (await get_custom_exclude_enabled(user_id))
        await set_custom_exclude_enabled(user_id, new_custom)
        text, markup = await _build_exclude_filter_menu(user_id)
        await callback_query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback_query.answer(
            f"Custom Exclude {'ON (bot skips locally)' if new_custom else 'OFF (testing pure Meeff Native only)'}"
        )

    elif data == "sync_exclude_all":
        await callback_query.answer("Syncing native exclude filter...")
        ok, fail = await sync_all_active_filters(user_id)
        await callback_query.answer(f"Synced to {ok} active accounts ({fail} failed)", show_alert=True)

    elif data == "settings_menu":
        user_signup_states.pop(user_id, None)
        await callback_query.message.edit_text("<b>Settings Menu</b>", reply_markup=await get_settings_menu(user_id), parse_mode="HTML")
    elif data == "show_filters":
        await callback_query.message.edit_text(
            "🎛️ <b>Meeff Native Filter Settings</b>\n\nSelect an account to configure multi-country include/exclude, age, distance, purposes & more:",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML",
        )
    elif data.startswith("noop_"):
        await callback_query.answer()
    elif data in ("toggle_request_filter", "meeff_filter_main") or data.startswith(("flt_", "account_filter_", "account_gender_", "account_age_", "account_nationality_")):
        await set_account_filter(callback_query)
    
    # --- ACCOUNT MANAGEMENT ---
    elif data.startswith("view_account_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            token_obj = tokens[idx]
            info_card = await get_info_card(user_id, token_obj['token'])
            details = f"<b>Name:</b> <code>{html.escape(token_obj.get('name', 'N/A'))}</code>\n<b>Status:</b> {'Active' if token_obj.get('active', True) else 'Inactive'}\n\n"
            if info_card:
                details += info_card
            else:
                email = token_obj.get('email', '')
                password = token_obj.get('password', '')
                details += f"<code>{html.escape(token_obj['token'])}</code>"
                if email:
                    details += f"\n\n<b>Email:</b> <code>{html.escape(email)}</code>"
                if password:
                    details += f"\n<b>Password:</b> <code>{html.escape(password)}</code>"
            await callback_query.message.edit_text(details, reply_markup=get_account_view_menu(idx, page_idx), parse_mode="HTML", disable_web_page_preview=True)
            
    elif data.startswith("confirm_delete_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await callback_query.message.edit_text(f"<b>Confirm Deletion</b> of <code>{html.escape(tokens[idx]['name'])}</code>?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes, Delete", callback_data=f"delete_account_{idx}|{page_idx}"), InlineKeyboardButton(text="Cancel", callback_data=f"manage_accounts|{page_idx}")]]) , parse_mode="HTML")

    elif data.startswith("toggle_status_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await toggle_token_status(user_id, tokens[idx]["token"])
            await show_manage_accounts_menu(callback_query, page_idx)
    
    elif data.startswith("set_account_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await set_current_account(user_id, tokens[idx]["token"])
            await show_manage_accounts_menu(callback_query, page_idx)
            
    elif data.startswith("delete_account_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await delete_token(user_id, tokens[idx]["token"])
            await show_manage_accounts_menu(callback_query, page_idx)

    elif data == "noop_page":
        await callback_query.answer("You are on this page.")
    
    elif data == "view_blocked_users":
        from db import get_blocked_users
        blocked = await get_blocked_users(user_id)
        if not blocked:
            return await callback_query.message.edit_text("<b>No blocked users</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="spam_filter_menu")]]), parse_mode="HTML")
        blocked_list = "\n".join([f"<code>{uid}</code>" for uid in sorted(list(blocked))[:50]])
        await callback_query.message.edit_text(f"<b>Blocked Users ({len(blocked)})</b>\n\n{blocked_list}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="spam_filter_menu")]]), parse_mode="HTML")
    
    elif data == "confirm_clear_blocked":
        await callback_query.message.edit_text("<b>Clear all blocked users?</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes, Clear", callback_data="clear_blocked_confirm")], [InlineKeyboardButton(text="Cancel", callback_data="spam_filter_menu")]]), parse_mode="HTML")
    
    elif data == "clear_blocked_confirm":
        from db import clear_blocked_users
        await clear_blocked_users(user_id)
        await callback_query.answer("✅ All blocked users cleared!")
        await callback_query.message.edit_text("<b>Spam Filters</b>", reply_markup=await get_spam_filter_menu(user_id), parse_mode="HTML")
    
    # --- AUTOMATION CALLBACKS ---
    elif data.startswith(("toggle_automation", "auto_set_lounge", "auto_set_chatroom", "auto_select_accounts", "auto_acc_", "auto_run_action", "auto_view_log", "back_to_automation")):
        from automation import start_automation, stop_automation, is_automation_running, run_automation_action
        
        if data == "toggle_automation":
            settings = await get_automation_settings(user_id)
            running = is_automation_running(user_id)
            if settings["enabled"] and running:
                await set_automation_enabled(user_id, False)
                stop_automation(user_id)
                await callback_query.answer("Automation stopped.")
            else:
                if not settings.get("lounge_message") or not settings.get("chatroom_message"):
                    return await callback_query.answer("Set both messages first!", show_alert=True)
                await set_automation_enabled(user_id, True)
                start_automation(user_id, bot)
                await callback_query.answer("Automation started!")
        
        elif data == "auto_set_lounge":
            user_signup_states[user_id] = {"stage": "auto_set_lounge"}
            await callback_query.message.edit_text("<b>Set Lounge Message</b>\n\nType the message you want to auto-send in lounge:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="back_to_automation")]]))
        
        elif data == "auto_set_chatroom":
            user_signup_states[user_id] = {"stage": "auto_set_chatroom"}
            await callback_query.message.edit_text("<b>Set Chatroom Message</b>\n\nType the message you want to auto-send in chatroom:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="back_to_automation")]]))
        
        elif data == "auto_select_accounts":
            settings = await get_automation_settings(user_id)
            selected = settings.get("selected_accounts", "all")
            
            mark_current = "✅ " if selected == "current" else ""
            mark_active  = "✅ " if selected in ("all", "active_only") else ""

            buttons = [
                [InlineKeyboardButton(text=f"{mark_current}Current Account", callback_data="auto_acc_current")],
                [InlineKeyboardButton(text=f"{mark_active}All Active Accounts", callback_data="auto_acc_active")],
                [InlineKeyboardButton(text="Back", callback_data="back_to_automation")]
            ]
            await callback_query.message.edit_text("<b>Select Automation Accounts</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        
        elif data == "auto_acc_current":
            await set_automation_accounts(user_id, "current")
            await callback_query.answer("Current account selected!")
            callback_query.data = "auto_select_accounts"
            await callback_handler(callback_query)
        
        elif data == "auto_acc_active":
            await set_automation_accounts(user_id, "all")
            await callback_query.answer("All active accounts selected!")
            callback_query.data = "auto_select_accounts"
            await callback_handler(callback_query)
        
        elif data == "auto_run_action":
            status_msg = await callback_query.message.edit_text("<b>Starting automation...</b>", parse_mode="HTML")
            asyncio.create_task(run_automation_action(user_id, status_msg))
        
        elif data == "auto_view_log":
            log_entries = await get_automation_log(user_id)
            log_text = "\n".join([f"<code>{e['time']}</code> {html.escape(e['text'])}" for e in log_entries[-15:]]) if log_entries else "<i>No automation activity yet.</i>"
            await callback_query.message.edit_text(f"<b>Automation Log</b>\n\n{log_text}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Refresh", callback_data="auto_view_log"), InlineKeyboardButton(text="Back", callback_data="back_to_automation")]]), parse_mode="HTML")
        
        elif data == "back_to_automation":
            user_signup_states.pop(user_id, None)
            await callback_query.message.edit_text("<b>Automation Menu</b>", reply_markup=await build_automation_menu(user_id), parse_mode="HTML")
    
    # --- SPAM FILTER MANAGEMENT ---
    elif data == "spam_filter_menu":
        await callback_query.message.edit_text("<b>Spam Filter Settings</b>", reply_markup=await get_spam_filter_menu(user_id), parse_mode="HTML")
    elif data.startswith("toggle_spam_"):
        filter_type = data.split("_")[-1]
        if filter_type == "all":
            new_status = not any((await get_all_spam_filters(user_id)).values())
            for ft in ["chatroom", "request", "lounge"]: await set_individual_spam_filter(user_id, ft, new_status)
        else:
            await set_individual_spam_filter(user_id, filter_type, not await get_individual_spam_filter(user_id, filter_type))
        await callback_query.message.edit_text("<b>Spam Filter Settings</b>", reply_markup=await get_spam_filter_menu(user_id), parse_mode="HTML")
    elif data == "noop_count":
        await callback_query.answer("This is the count of spam-filtered IDs.")
    elif data.startswith("confirm_clear_spam_"):
        category = data.split("_")[-1]
        await callback_query.message.edit_text(f"<b>Confirm:</b> Clear {category} spam?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes, Clear", callback_data=f"clear_spam_{category}"), InlineKeyboardButton(text="Cancel", callback_data="spam_filter_menu")]]), parse_mode="HTML")
    elif data.startswith("clear_spam_"):
        await clear_spam_records(user_id, data.split("_")[-1])
        await callback_query.answer("Cleared.")
        await callback_query.message.edit_text("<b>Spam Filter Settings</b>", reply_markup=await get_spam_filter_menu(user_id), parse_mode="HTML")
    
    # --- BATCH MANAGEMENT ---
    elif data == "batch_management":
        await callback_query.message.edit_text("<b>Batch Management</b>", reply_markup=await get_batch_management_menu(user_id), parse_mode="HTML")
    elif data.startswith("view_batch_"):
        batch_name = data.replace("view_batch_", "")
        await show_batch_accounts_menu(callback_query, batch_name)
    elif data.startswith("batch_select|"):
        try:
            _, batch_name, idx_str = original_data.split("|", 2)
            idx = int(idx_str)
            tokens = await get_tokens(user_id)
            if 0 <= idx < len(tokens):
                await set_current_account(user_id, tokens[idx]["token"])
                await show_batch_accounts_menu(callback_query, batch_name)
        except Exception: await callback_query.answer("Invalid data.", show_alert=True)
    elif data.startswith("batch_toggle|"):
        try:
            _, batch_name, idx_str = original_data.split("|", 2)
            idx = int(idx_str)
            tokens = await get_tokens(user_id)
            if 0 <= idx < len(tokens):
                await toggle_token_status(user_id, tokens[idx]["token"])
                await show_batch_accounts_menu(callback_query, batch_name)
        except Exception: await callback_query.answer("Invalid data.", show_alert=True)
    elif data.startswith("batch_view|"):
        try:
            _, batch_name, idx_str = original_data.split("|", 2)
            idx = int(idx_str)
            tokens = await get_tokens(user_id)
            if 0 <= idx < len(tokens):
                token_obj = tokens[idx]
                info = await get_info_card(user_id, token_obj["token"])
                text = f"<b>Name:</b> {html.escape(token_obj.get('name','N/A'))}\n<b>Status:</b> {'Active' if token_obj.get('active', True) else 'Inactive'}\n\n{info or '<code>' + html.escape(token_obj['token']) + '</code>'}"
                await callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data=f"view_batch_{batch_name}")]]))
        except Exception: await callback_query.answer("Invalid data.", show_alert=True)
    elif data.startswith("toggle_batch_"):
        batch_name = data.replace("toggle_batch_", "")
        if await toggle_batch_status(user_id, batch_name):
            await callback_query.answer("Toggled.")
            await callback_query.message.edit_text("<b>Batch Management</b>", reply_markup=await get_batch_management_menu(user_id), parse_mode="HTML")
    
    # --- BATCH ACCOUNT MULTI-COUNTRY FILTER LOGIC ---
    elif data.startswith("batch_acc_filter|"):
        parts = data.split("|")
        if len(parts) >= 3:
            batch_name = parts[1]
            try:
                global_index = int(parts[2])
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    token_filters = await get_user_filters(user_id, tok['token']) or {}
                    current_nat = normalize_codes_csv(token_filters.get("filterNationalityCodes") or token_filters.get("filterNationalityCode", ""))
                    selected = set(c for c in current_nat.split(",") if c)

                    buttons = [[InlineKeyboardButton(text=f"{'✅ ' if not selected else ''}All Countries (Clear)", callback_data=f"batch_acc_nat_all|{batch_name}|{global_index}")]]
                    row = []
                    for i, (code, name) in enumerate(NATIONALITY_LIST):
                        mark = "✅ " if code in selected else ""
                        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"batch_acc_nat_{code}|{batch_name}|{global_index}"))
                        if len(row) == 2 or i == len(NATIONALITY_LIST) - 1:
                            buttons.append(row)
                            row = []
                    buttons.append([
                        InlineKeyboardButton(text="🎛️ Full Native Filters", callback_data=f"flt_acc:{global_index}"),
                        InlineKeyboardButton(text="💾 Apply & Back", callback_data=f"batch_acc_apply|{batch_name}|{global_index}"),
                    ])
                    await callback_query.message.edit_text(
                        f"<b>Multi-Country Filter — {html.escape(tok['name'])}</b>\n\nSelected: <code>{current_nat or 'All Countries'}</code>\nTap to toggle countries:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                        parse_mode="HTML",
                    )
            except (ValueError, IndexError):
                await callback_query.answer("Invalid account.", show_alert=True)

    elif data.startswith("batch_acc_nat_"):
        parts = data.split("|")
        if len(parts) >= 3:
            nat_code = parts[0].replace("batch_acc_nat_", "")
            batch_name = parts[1]
            try:
                global_index = int(parts[2])
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    user_filters = await get_user_filters(user_id, tok['token']) or {}
                    if nat_code == "all":
                        new_csv = ""
                    else:
                        cur_list = [c for c in normalize_codes_csv(user_filters.get("filterNationalityCodes") or user_filters.get("filterNationalityCode", "")).split(",") if c]
                        if nat_code.upper() in cur_list:
                            cur_list.remove(nat_code.upper())
                        else:
                            cur_list.append(nat_code.upper())
                        new_csv = ",".join(cur_list)

                    user_filters["filterNationalityCode"] = new_csv
                    user_filters["filterNationalityCodes"] = new_csv
                    await set_user_filters(user_id, tok['token'], user_filters)

                    selected = set(c for c in new_csv.split(",") if c)
                    buttons = [[InlineKeyboardButton(text=f"{'✅ ' if not selected else ''}All Countries (Clear)", callback_data=f"batch_acc_nat_all|{batch_name}|{global_index}")]]
                    row = []
                    for i, (code, name) in enumerate(NATIONALITY_LIST):
                        mark = "✅ " if code in selected else ""
                        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"batch_acc_nat_{code}|{batch_name}|{global_index}"))
                        if len(row) == 2 or i == len(NATIONALITY_LIST) - 1:
                            buttons.append(row)
                            row = []
                    buttons.append([
                        InlineKeyboardButton(text="🎛️ Full Native Filters", callback_data=f"flt_acc:{global_index}"),
                        InlineKeyboardButton(text="💾 Apply & Back", callback_data=f"batch_acc_apply|{batch_name}|{global_index}"),
                    ])
                    await callback_query.message.edit_text(
                        f"<b>Multi-Country Filter — {html.escape(tok['name'])}</b>\n\nSelected: <code>{new_csv or 'All Countries'}</code>\nTap to toggle countries:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                        parse_mode="HTML",
                    )
                    await callback_query.answer(new_csv or "All Countries")
            except (ValueError, IndexError):
                await callback_query.answer("Invalid account.", show_alert=True)

    elif data.startswith("batch_acc_apply|"):
        parts = data.split("|")
        if len(parts) >= 3:
            batch_name = parts[1]
            try:
                global_index = int(parts[2])
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    ok = await apply_filter_for_account(tok['token'], user_id)
                    uf = await get_user_filters(user_id, tok['token']) or {}
                    await callback_query.answer(f"Applied: {uf.get('filterNationalityCodes') or 'All'}" if ok else "Saved locally (API failed)", show_alert=not ok)
                    await show_batch_accounts_menu(callback_query, batch_name)
            except (ValueError, IndexError):
                await callback_query.answer("Invalid account.", show_alert=True)

    elif data.startswith("batch_filter_"):
        batch_name = data.replace("batch_filter_", "")
        batch = await get_batch_by_name(user_id, batch_name)
        cur_csv = normalize_codes_csv(batch.get("filter_nationality", "")) if batch else ""
        await callback_query.message.edit_text(
            f"<b>Multi-Country Filter — {html.escape(batch_name)}</b>\n\nSelected: <code>{cur_csv or 'All Countries'}</code>\nTap countries to toggle, then tap <b>Apply to Batch</b>:",
            reply_markup=get_batch_filter_menu(batch_name, cur_csv),
            parse_mode="HTML",
        )

    elif data.startswith("batch_nat_all|"):
        batch_name = data.split("|", 1)[1]
        await set_batch_filter(user_id, batch_name, "")
        await callback_query.message.edit_text(
            f"<b>Multi-Country Filter — {html.escape(batch_name)}</b>\n\nSelected: <code>All Countries</code>\nTap countries to toggle, then tap <b>Apply to Batch</b>:",
            reply_markup=get_batch_filter_menu(batch_name, ""),
            parse_mode="HTML",
        )
        await callback_query.answer("Cleared!")

    elif data.startswith("batch_nat|"):
        parts = data.split("|", 2)
        if len(parts) == 3:
            code = parts[1].upper()
            batch_name = parts[2]
            batch = await get_batch_by_name(user_id, batch_name)
            cur_list = [c for c in normalize_codes_csv(batch.get("filter_nationality", "") if batch else "").split(",") if c]
            if code in cur_list:
                cur_list.remove(code)
            else:
                cur_list.append(code)
            new_csv = ",".join(cur_list)
            await set_batch_filter(user_id, batch_name, new_csv)
            await callback_query.message.edit_text(
                f"<b>Multi-Country Filter — {html.escape(batch_name)}</b>\n\nSelected: <code>{new_csv or 'All Countries'}</code>\nTap countries to toggle, then tap <b>Apply to Batch</b>:",
                reply_markup=get_batch_filter_menu(batch_name, new_csv),
                parse_mode="HTML",
            )
            await callback_query.answer(new_csv or "All Countries")

    elif data.startswith("batch_apply|"):
        batch_name = data.split("|", 1)[1]
        batch = await get_batch_by_name(user_id, batch_name)
        if batch:
            nat_code = normalize_codes_csv(batch.get("filter_nationality", ""))
            tokens = await get_tokens(user_id)
            token_indices = batch.get("token_indices", [])
            ok_count = 0
            for idx in token_indices:
                if 0 <= idx < len(tokens):
                    tok = tokens[idx]
                    u_filters = await get_user_filters(user_id, tok['token']) or {}
                    u_filters["filterNationalityCode"] = nat_code
                    u_filters["filterNationalityCodes"] = nat_code
                    await set_user_filters(user_id, tok['token'], u_filters)
                    if await apply_filter_for_account(tok['token'], user_id):
                        ok_count += 1
            await callback_query.answer(f"Applied ({nat_code or 'All'}) to {ok_count}/{len(token_indices)} accounts!")
        await callback_query.message.edit_text("<b>Batch Management</b>", reply_markup=await get_batch_management_menu(user_id), parse_mode="HTML")

    elif data.startswith("batch_nat_"):
        parts = data.split("_")
        if len(parts) >= 3:
            nat_code = parts[2]
            batch_name = "_".join(parts[3:])
            if nat_code == "all": nat_code = ""
            await set_batch_filter(user_id, batch_name, nat_code)
            batch = await get_batch_by_name(user_id, batch_name)
            if batch:
                tokens = await get_tokens(user_id)
                token_indices = batch.get("token_indices", [])
                count = 0
                for idx in token_indices:
                    if idx < len(tokens):
                        tok = tokens[idx]
                        u_filters = await get_user_filters(user_id, tok['token']) or {}
                        u_filters["filterNationalityCode"] = nat_code
                        u_filters["filterNationalityCodes"] = nat_code
                        await set_user_filters(user_id, tok['token'], u_filters)
                        await apply_filter_for_account(tok['token'], user_id)
                        count += 1
                await callback_query.answer(f"Applied to {count} accounts!")
            await callback_query.message.edit_text("<b>Batch Management</b>", reply_markup=await get_batch_management_menu(user_id), parse_mode="HTML")

    elif data == "back_to_menu":
        await callback_query.message.edit_text("<b>Meeff Bot Dashboard</b>", reply_markup=start_markup, parse_mode="HTML")
        
    elif data in ("start", "start_all", "stop", "all_countries"):
        if data in ("start", "start_all", "all_countries") and state.get("running"): return await callback_query.answer("Already running!")
        if data == "stop" and not state.get("running"): return await callback_query.answer("Not running!")
        
        if data == "start":
            msg = await callback_query.message.edit_text("<b>Initializing Requests...</b>", reply_markup=stop_markup, parse_mode="HTML")
            state.update({"running": True, "status_message_id": msg.message_id, "pinned_message_id": msg.message_id})
            await bot.pin_chat_message(chat_id=user_id, message_id=msg.message_id)
            asyncio.create_task(run_requests(user_id, bot, TARGET_CHANNEL_ID))
        elif data == "start_all":
            tokens = await get_active_tokens(user_id)
            if not tokens: return await callback_query.answer("No active tokens.", show_alert=True)
            msg = await callback_query.message.edit_text(f"🔄 <b>AIO Starting ({len(tokens)})...</b>", reply_markup=stop_markup, parse_mode="HTML")
            state.update({"running": True, "status_message_id": msg.message_id, "pinned_message_id": msg.message_id})
            asyncio.create_task(process_all_tokens(user_id, tokens, bot, TARGET_CHANNEL_ID, initial_status_message=msg))
        elif data == "stop":
            state.update({"running": False, "stopped": True})
            await callback_query.answer("Stopping... summary will appear shortly.")
        elif data == "all_countries":
            msg = await callback_query.message.edit_text("<b>Starting All Countries...</b>", reply_markup=stop_markup, parse_mode="HTML")
            state.update({"running": True, "status_message_id": msg.message_id, "pinned_message_id": msg.message_id, "stop_markup": stop_markup})
            await bot.pin_chat_message(chat_id=user_id, message_id=msg.message_id)
            asyncio.create_task(run_all_countries(user_id, state, bot, get_current_account))

async def set_bot_commands():
    commands = [BotCommand(command=c, description=d) for c, d in [
        ("start", "Start the bot"), ("lounge", "Send message in the lounge"),
        ("send_lounge_all", "Send lounge message to all accounts"), ("chatroom", "Send message in chatrooms"),
        ("send_chat_all", "Send chatroom message to all accounts"), ("invoke", "Remove disabled accounts"),
        ("skip", "Unsubscribe from chats"), ("settings", "Bot settings"),
        ("add", "Add a person by ID"), ("signup", "Create a Meeff account"),
        ("automation", "Automation settings"), ("block", "Block user from meeff"),
        ("refresh", "Refresh tokens for all active accounts"),
        ("vip_audit", "👑 Audit VIP/Plus status & Rubies"),
        ("claim_attendance", "🎁 Claim daily attendance & rubies"),
        ("spoof_loc", "🌍 Teleport GPS location"),
        ("redeem_promo", "🎟️ Redeem promo / coupon code"),
        ("use_item", "🎒 Activate free item pass"),
        ("password", "Enter password for access")]]
    await bot.set_my_commands(commands)

async def resume_automation_tasks():
    """Checks all users in DB and restarts automation if it was ON."""
    try:
        collections = await list_all_collections()
        count = 0
        for col in collections:
            # Extract user_id from "user_12345"
            try:
                user_id = int(col['collection_name'].replace("user_", ""))
                settings = await get_automation_settings(user_id)
                if settings.get("enabled"):
                    start_automation(user_id, bot)
                    count += 1
            except ValueError:
                continue
        if count > 0:
            logger.info(f"♻️ Resumed automation for {count} users.")
    except Exception as e:
        logger.error(f"Failed to resume tasks: {e}")

async def main():
    try:
        await set_bot_commands()
        
        # --- RESUME TASKS ---
        await resume_automation_tasks()
        
        dp.include_router(router)
        dp.include_router(premium_router)
        logger.info("Starting bot polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
