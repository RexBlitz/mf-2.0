import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict
import aiohttp
import html
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, CallbackQuery
from collections import defaultdict
from aiogram.exceptions import TelegramBadRequest


from db import (
    set_token, get_tokens, set_current_account, get_current_account, delete_token,
    set_user_filters, get_user_filters, get_all_user_filters, set_spam_filter, get_spam_filter,
    is_already_sent, add_sent_id, toggle_token_status, get_active_tokens,
    get_token_status, set_account_active, get_info_card,
    set_individual_spam_filter, get_individual_spam_filter, get_all_spam_filters,get_spam_menu_data,
    list_all_collections, get_collection_summary, connect_to_collection,
    rename_user_collection, transfer_to_user, get_current_collection_info,
    get_spam_record_count, clear_spam_records,
    get_batches, create_batch, toggle_batch_status, set_batch_filter, get_batch_by_name, # auto_organize_batches removed
    add_token_to_auto_batch 
)
# Make sure these other local modules are compatible if they also perform I/O
from lounge import send_lounge, send_lounge_all_tokens
from chatroom import send_message_to_everyone, send_message_to_everyone_all_tokens
from unsubscribe import unsubscribe_everyone
from filters import meeff_filter_command, set_account_filter, get_meeff_filter_main_keyboard, set_filter
from allcountry import run_all_countries
from signup import signup_command, signup_callback_handler, signup_message_handler, signup_settings_command
from friend_requests import run_requests, process_all_tokens, user_states, stop_markup

# --- Configuration & Setup ---
API_TOKEN = "7916536914:AAHwtvO8hfGl2U4xcfM1fAjMLNypPFEW5JQ"
ADMIN_USER_IDS = {7405203657, 7725409374, 7691399254, 7795345443}
TEMP_PASSWORD = "11223344"
TARGET_CHANNEL_ID = -1002610862940
ACCOUNTS_PER_PAGE = 12 # New constant for pagination

password_access: Dict[int, datetime] = {}
db_operation_states: Dict[int, Dict[str, str]] = defaultdict(dict)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN)
router = Router()
dp = Dispatcher()

# --- Utility & Keyboards ---
def is_admin(user_id: int) -> bool: return user_id in ADMIN_USER_IDS
def has_valid_access(user_id: int) -> bool:
    if is_admin(user_id): return True
    return user_id in password_access and password_access[user_id] > datetime.now()

async def get_settings_menu(user_id: int) -> InlineKeyboardMarkup:
    spam_filters = await get_all_spam_filters(user_id)
    any_spam_on = any(spam_filters.values())
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Manage Accounts", callback_data="manage_accounts|0"), InlineKeyboardButton(text="Meeff Filters", callback_data="show_filters")],
        [InlineKeyboardButton(text="Batch Management", callback_data="batch_management")],
        [InlineKeyboardButton(text=f"Spam Filters: {'ON' if any_spam_on else 'OFF'}", callback_data="spam_filter_menu")],
        [InlineKeyboardButton(text="DB Settings", callback_data="db_settings"), InlineKeyboardButton(text="Back", callback_data="back_to_menu")]
    ])

def get_db_settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Connect DB", callback_data="db_connect"), InlineKeyboardButton(text="Rename DB", callback_data="db_rename")], [InlineKeyboardButton(text="View DB", callback_data="db_view"), InlineKeyboardButton(text="Transfer DB", callback_data="db_transfer")], [InlineKeyboardButton(text="Back", callback_data="settings_menu")]])

def get_unsubscribe_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Unsubscribe Current", callback_data="unsub_current"), InlineKeyboardButton(text="Unsubscribe All", callback_data="unsub_all")], [InlineKeyboardButton(text="Back", callback_data="back_to_menu")]])

async def get_spam_filter_menu(user_id: int) -> InlineKeyboardMarkup:
    # This single call now gets all the data we need, making it much faster
    menu_data = await get_spam_menu_data(user_id)
    
    spam_filters = menu_data["filters"]
    counts = menu_data["counts"]

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

    # Removed manual Auto-Organize/Reorganize buttons for automatic batching
    # if not batches and tokens:
    #     buttons.append([InlineKeyboardButton(text="Auto-Organize Batches (10 per batch)", callback_data="auto_organize_batches")])

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

    # Removed manual Reorganize button
    # if batches:
    #     buttons.append([InlineKeyboardButton(text="Reorganize Batches", callback_data="auto_organize_batches")])

    buttons.append([InlineKeyboardButton(text="Back", callback_data="settings_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_batch_filter_menu(batch_name: str) -> InlineKeyboardMarkup:
    countries = [
        ("RU", "Russia"), ("UA", "Ukraine"), ("BY", "Belarus"), ("IR", "Iran"), ("PH", "Philippines"),
        ("PK", "Pakistan"), ("US", "USA"), ("IN", "India"), ("DE", "Germany"), ("FR", "France"),
        ("BR", "Brazil"), ("CN", "China"), ("JP", "Japan"), ("KR", "Korea"), ("CA", "Canada"),
        ("AU", "Australia"), ("IT", "Italy"), ("ES", "Spain"), ("ZA", "South Africa"), ("TR", "Turkey")
    ]

    buttons = []
    buttons.append([InlineKeyboardButton(text="All Countries", callback_data=f"batch_nat_all_{batch_name}")])

    row = []
    for i, (code, name) in enumerate(countries):
        row.append(InlineKeyboardButton(text=code, callback_data=f"batch_nat_{code}_{batch_name}"))
        if len(row) == 4 or i == len(countries) - 1:
            buttons.append(row)
            row = []

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
            password_access[message.chat.id] = datetime.now() + timedelta(hours=1)
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
    if not has_valid_access(user_id):
        return await message.reply("You are not authorized.")

    parts = message.text.strip().split(maxsplit=1)

    # ---------------------------------------------------------
    # CASE 1: User wrote `/invoke all`
    # ---------------------------------------------------------
    if len(parts) == 2 and parts[1].lower() == "all":
        tokens = await get_tokens(user_id)
        if not tokens:
            return await message.reply("No accounts found.")

        status_msg = await message.reply("<b>Checking ALL Account Status...</b>", parse_mode="HTML")
        disabled = []
        working = []

        async with aiohttp.ClientSession() as session:
            for t in tokens:
                headers = {
                    "User-Agent": "okhttp/5.0.0-alpha.14",
                    "meeff-access-token": t["token"]
                }
                try:
                    async with session.get(
                        "https://api.meeff.com/facetalk/vibemeet/history/count/v1",
                        params={"locale": "en"},
                        headers=headers
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if data.get("errorCode") == "AuthRequired":
                            disabled.append(t)
                        else:
                            working.append(t)
                except:
                    disabled.append(t)

        # Remove disabled accounts
        if disabled:
            for acc in disabled:
                await delete_token(user_id, acc["token"])

            removed_names = "\n".join([f"• {html.escape(a['name'])}" for a in disabled])
            return await status_msg.edit_text(
                f"<b>Invoke ALL Complete</b>\n"
                f"Working: {len(working)}\n"
                f"Removed: {len(disabled)}\n\n"
                f"<b>Removed Accounts:</b>\n{removed_names}",
                parse_mode="HTML"
            )

        return await status_msg.edit_text(
            f"<b>All Accounts Working ({len(working)})</b>",
            parse_mode="HTML"
        )

    # ---------------------------------------------------------
    # CASE 2: User wrote `/invoke <batch_name>`
    # ---------------------------------------------------------
    if len(parts) == 2:
        batch_name = parts[1].strip()
        batch = await get_batch_by_name(user_id, batch_name)
        if batch:
            tokens = await get_tokens(user_id)
            batch_tokens = [
                tokens[idx] for idx in batch.get("token_indices", [])
                if idx < len(tokens)
            ]

            if not batch_tokens:
                return await message.reply(f"No tokens found in batch '{batch_name}'.")

            status_msg = await message.reply(
                f"<b>Checking Batch '{batch_name}'...</b>",
                parse_mode="HTML"
            )

            disabled = []
            working = []

            async with aiohttp.ClientSession() as session:
                for t in batch_tokens:
                    headers = {
                        "User-Agent": "okhttp/5.0.0-alpha.14",
                        "meeff-access-token": t["token"]
                    }
                    try:
                        async with session.get(
                            "https://api.meeff.com/facetalk/vibemeet/history/count/v1",
                            params={"locale": "en"},
                            headers=headers
                        ) as resp:
                            data = await resp.json(content_type=None)
                            if data.get("errorCode") == "AuthRequired":
                                disabled.append(t)
                            else:
                                working.append(t)
                    except:
                        disabled.append(t)

            # Remove disabled accounts
            if disabled:
                for acc in disabled:
                    await delete_token(user_id, acc["token"])

                removed_names = "\n".join([f"• {html.escape(a['name'])}" for a in disabled])
                return await status_msg.edit_text(
                    f"<b>Batch '{batch_name}' Cleanup Complete</b>\n"
                    f"Working: {len(working)}\n"
                    f"Removed: {len(disabled)}\n\n"
                    f"<b>Removed Accounts:</b>\n{removed_names}",
                    parse_mode="HTML"
                )

            return await status_msg.edit_text(
                f"<b>All Accounts in Batch '{batch_name}' Working ({len(working)})</b>",
                parse_mode="HTML"
            )

        # If name doesn’t match any batch → continue to normal invoke
        # (maybe user wrote `/invoke something wrong`)

    # ---------------------------------------------------------
    # CASE 3: Default `/invoke` → check ACTIVE accounts only
    # ---------------------------------------------------------
    active_tokens = await get_active_tokens(user_id)
    if not active_tokens:
        return await message.reply("No active accounts found.")

    status_msg = await message.reply("<b>Checking Active Accounts...</b>", parse_mode="HTML")

    disabled = []
    working = []

    async with aiohttp.ClientSession() as session:
        for t in active_tokens:
            headers = {
                "User-Agent": "okhttp/5.0.0-alpha.14",
                "meeff-access-token": t["token"]
            }
            try:
                async with session.get(
                    "https://api.meeff.com/facetalk/vibemeet/history/count/v1",
                    params={"locale": "en"},
                    headers=headers
                ) as resp:
                    data = await resp.json(content_type=None)
                    if data.get("errorCode") == "AuthRequired":
                        disabled.append(t)
                    else:
                        working.append(t)
            except:
                disabled.append(t)

    if disabled:
        for acc in disabled:
            await delete_token(user_id, acc["token"])

        removed_names = "\n".join([f"• {html.escape(a['name'])}" for a in disabled])
        return await status_msg.edit_text(
            f"<b>Cleanup Complete</b>\n"
            f"Working: {len(working)}\n"
            f"Removed: {len(disabled)}\n\n"
            f"<b>Removed Accounts:</b>\n{removed_names}",
            parse_mode="HTML"
        )

    return await status_msg.edit_text(
        f"<b>All Active Accounts Working ({len(working)})</b>",
        parse_mode="HTML"
    )


@router.message(Command("settings"))
async def settings_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    await message.reply("<b>Settings Menu</b>", reply_markup=await get_settings_menu(message.chat.id), parse_mode="HTML")

@router.message(Command("add"))
async def add_person_command(message: Message):
    user_id = message.chat.id
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    args = message.text.strip().split()
    if len(args) < 2: return await message.reply("Usage: /add <person_id>")
    
    token = await get_current_account(user_id)
    if not token: return await message.reply("No active account found.")

    person_id = args[1]
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    headers = {"meeff-access-token": token, "Connection": "keep-alive"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                data = await response.json()
                if data.get("errorCode") == "LikeExceeded":
                    await message.reply("You've reached the daily like limit.")
                elif data.get("errorCode"):
                    await message.reply(f"Failed: {data.get('errorMessage', 'Unknown error')}")
                else:
                    await message.reply(f"Successfully added person with ID: {person_id}")
    except Exception as e:
        logging.error(f"Error adding person by ID: {e}")
        await message.reply("An error occurred while trying to add this person.")

@router.message()
async def handle_new_token(message: Message):
    if message.text and message.text.startswith("/"): return
    user_id = message.from_user.id
    if message.from_user.is_bot: return
    if await signup_message_handler(message): return

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

        
        # The message will be used as the status update for saving
        status_msg = await message.reply("<b>Saving Token...</b>", parse_mode="HTML")

        account_name = token_data[1] if len(token_data) > 1 else f"Account {len(await get_tokens(user_id)) + 1}"
        
        # Save the token and get its index
        token_index = await set_token(user_id, token, account_name)
        
        # AUTOMATIC BATCH ASSIGNMENT
        if token_index != -1:
            await add_token_to_auto_batch(user_id, token_index)
        
        # Report success without verification status
        await status_msg.edit_text(f"✅ <b>Token Saved</b> and named '<code>{html.escape(account_name)}</code>'.", parse_mode="HTML")

async def show_manage_accounts_menu(callback_query: CallbackQuery, page_idx: int = 0):
    user_id = callback_query.from_user.id
    tokens = await get_tokens(user_id)
    total_accounts = len(tokens)
    current_token = await get_current_account(user_id)

    if not tokens:
        return await callback_query.message.edit_text("<b>No Accounts Found</b>...", reply_markup=back_markup, parse_mode="HTML")

    # Calculate page range
    total_pages = (total_accounts + ACCOUNTS_PER_PAGE - 1) // ACCOUNTS_PER_PAGE
    page_idx = max(0, min(page_idx, total_pages - 1)) # Ensure page_idx is valid
    start_idx = page_idx * ACCOUNTS_PER_PAGE
    end_idx = min(start_idx + ACCOUNTS_PER_PAGE, total_accounts)

    visible_tokens = tokens[start_idx:end_idx]
    all_filters = await get_all_user_filters(user_id)

    buttons = []
    for i, tok in enumerate(visible_tokens):
        # Global index in the full tokens list
        global_idx = start_idx + i 
        
        is_current = "🔹" if tok['token'] == current_token else "▫️"
        
        token_filters = all_filters.get(tok['token'], {})
        nationality_code = token_filters.get("filterNationalityCode", "")
        
        account_name = html.escape(tok['name'][:15])
        display_name = f"{account_name} ({nationality_code})" if nationality_code else account_name

        # Pass page_idx in callbacks to maintain the view state
        buttons.append([
            InlineKeyboardButton(text=f"{is_current} {display_name}", callback_data=f"set_account_{global_idx}|{page_idx}"),
            InlineKeyboardButton(text="ON" if tok.get('active', True) else "OFF", callback_data=f"toggle_status_{global_idx}|{page_idx}"),
            InlineKeyboardButton(text="View", callback_data=f"view_account_{global_idx}|{page_idx}")
        ])

    # --- Pagination Buttons ---
    pagination_row = []
    if page_idx > 0:
        pagination_row.append(InlineKeyboardButton(text="« Previous", callback_data=f"manage_accounts|{page_idx - 1}"))

    pagination_row.append(InlineKeyboardButton(text=f"{page_idx + 1}/{total_pages}", callback_data="noop_page"))

    if page_idx < total_pages - 1:
        pagination_row.append(InlineKeyboardButton(text="Next »", callback_data=f"manage_accounts|{page_idx + 1}"))
    
    if pagination_row:
        buttons.append(pagination_row)

    buttons.append([InlineKeyboardButton(text="Back", callback_data="settings_menu")])
    
    # Text update to show pagination info
    menu_text = f"<b>Manage Accounts (Page {page_idx + 1}/{total_pages})</b>\nCurrently selected: {'Yes' if current_token else 'No'}"

    try:
        await callback_query.message.edit_text(menu_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in e.message: logger.error(f"Error editing message: {e}")
        await callback_query.answer()


# ----------------- NEW: Show accounts inside a batch using Manage Accounts layout -----------------
async def show_batch_accounts_menu(callback_query: CallbackQuery, batch_name: str):
    user_id = callback_query.from_user.id
    batch = await get_batch_by_name(user_id, batch_name)
    if not batch:
        return await callback_query.answer("Batch not found.", show_alert=True)

    tokens = await get_tokens(user_id)
    token_indices = batch.get("token_indices", [])

    # Build list of token objects using the real indices
    batch_tokens = []
    real_indices = []
    for idx in token_indices:
        if idx < len(tokens):
            batch_tokens.append(tokens[idx])
            real_indices.append(idx)

    if not batch_tokens:
        return await callback_query.message.edit_text(
            f"<b>{html.escape(batch_name)}</b>\nNo accounts found in this batch.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="batch_management")]]),
            parse_mode="HTML"
        )

    current_token = await get_current_account(user_id)
    all_filters = await get_all_user_filters(user_id)

    buttons = []
    for i, tok in enumerate(batch_tokens):
        global_index = real_indices[i]
        is_current = "🔹" if tok['token'] == current_token else "▫️"

        token_filters = all_filters.get(tok['token'], {})
        nationality_code = token_filters.get("filterNationalityCode", "")

        account_name = html.escape(tok['name'][:20])
        display_name = f"{account_name} ({nationality_code})" if nationality_code else account_name

        # Use '|' delimiter to avoid ambiguity with underscores inside batch names
        buttons.append([
            InlineKeyboardButton(text=f"{is_current} {display_name}", callback_data=f"batch_select|{batch_name}|{global_index}"),
            InlineKeyboardButton(text="ON" if tok.get('active', True) else "OFF", callback_data=f"batch_toggle|{batch_name}|{global_index}"),
            InlineKeyboardButton(text="View", callback_data=f"batch_view|{batch_name}|{global_index}")
        ])

    buttons.append([InlineKeyboardButton(text="Back", callback_data="batch_management")])

    try:
        await callback_query.message.edit_text(f"<b>{html.escape(batch_name)} - Manage Accounts</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in e.message: logger.error(f"Error editing message: {e}")
        await callback_query.answer()

# -----------------------------------------------------------------------------------------------

@router.callback_query()
async def callback_handler(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    # --- PAGINATION LOGIC EXTRACTION ---
    page_idx = 0
    original_data = data # Store original data for specific checks
    
    if "|" in data and not data.startswith(("batch_select|", "batch_toggle|", "batch_view|")):
        data_parts = data.split("|")
        try:
            # Check if the last part is numeric (the page index)
            if len(data_parts) > 1 and data_parts[-1].isdigit():
                page_idx = int(data_parts[-1])
                data = "|".join(data_parts[:-1]) # Use the rest as the action
        except ValueError:
            pass # Should not happen if isdigit() is checked

    if await signup_callback_handler(callback_query): return
    if not has_valid_access(user_id): return await callback_query.answer("You are not authorized.")
    
    state = user_states.setdefault(user_id, {})
    
    # --- MAIN MENU ITEMS ---
    if data == "db_settings":
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
    elif data == "settings_menu":
        await callback_query.message.edit_text("<b>Settings Menu</b>", reply_markup=await get_settings_menu(user_id), parse_mode="HTML")
    elif data == "show_filters":
        await callback_query.message.edit_text("<b>Filter Settings</b>", reply_markup=await get_meeff_filter_main_keyboard(user_id), parse_mode="HTML")
    elif data in ("toggle_request_filter", "meeff_filter_main") or data.startswith(("account_filter_", "account_gender_", "account_age_", "account_nationality_")):
        await set_account_filter(callback_query)
    
    # --- ACCOUNT MANAGEMENT & PAGINATION ---
    elif data == "manage_accounts":
        # page_idx is already extracted if present in original_data
        await show_manage_accounts_menu(callback_query, page_idx)
        
    elif data.startswith("view_account_"):
        # idx is already extracted in the data
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            token_obj = tokens[idx]
            info_card = await get_info_card(user_id, token_obj['token'])
            details = f"<b>Name:</b> <code>{html.escape(token_obj.get('name', 'N/A'))}</code>\n<b>Status:</b> {'Active' if token_obj.get('active', True) else 'Inactive'}\n\n"
            details += info_card if info_card else "No profile card found."
            # Pass page_idx to the view menu
            await callback_query.message.edit_text(details, reply_markup=get_account_view_menu(idx, page_idx), parse_mode="HTML", disable_web_page_preview=True)
            
    elif data.startswith("confirm_delete_"):
        # idx is already extracted in the data
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            # Pass page_idx to the final delete button
            await callback_query.message.edit_text(f"<b>Confirm Deletion</b> of <code>{html.escape(tokens[idx]['name'])}</code>?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes, Delete", callback_data=f"delete_account_{idx}|{page_idx}"), InlineKeyboardButton(text="Cancel", callback_data=f"manage_accounts|{page_idx}")]]) , parse_mode="HTML")

    elif data.startswith("toggle_status_"):
        # idx is already extracted in the data
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await toggle_token_status(user_id, tokens[idx]["token"])
            # Pass page_idx back to the menu
            await show_manage_accounts_menu(callback_query, page_idx)
    
    elif data.startswith("set_account_"):
        # idx is already extracted in the data
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await set_current_account(user_id, tokens[idx]["token"])
            # Pass page_idx back to the menu
            await show_manage_accounts_menu(callback_query, page_idx)
            
    elif data.startswith("delete_account_"):
        # idx is already extracted in the data
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await delete_token(user_id, tokens[idx]["token"])
            # After deletion, refresh the menu, and pass the current page_idx
            await show_manage_accounts_menu(callback_query, page_idx)

    elif data == "noop_page":
        await callback_query.answer("You are on this page.")
    
    # --- SPAM FILTER MANAGEMENT ---
    elif data == "spam_filter_menu":
        await callback_query.message.edit_text("<b>Spam Filter Settings</b>", reply_markup=await get_spam_filter_menu(user_id), parse_mode="HTML")
    
    elif data.startswith("toggle_spam_"):
        filter_type = data.split("_")[-1]
        if filter_type == "all":
            new_status = not any((await get_all_spam_filters(user_id)).values())
            for ft in ["chatroom", "request", "lounge"]: 
                await set_individual_spam_filter(user_id, ft, new_status)
        else:
            new_status = not await get_individual_spam_filter(user_id, filter_type)
            await set_individual_spam_filter(user_id, filter_type, new_status)
        # Refresh the spam filter menu to show updated status
        await callback_query.message.edit_text("<b>Spam Filter Settings</b>", reply_markup=await get_spam_filter_menu(user_id), parse_mode="HTML")
    
    # --- SPAM CLEAR LOGIC ---
    elif data == "noop_count":
        await callback_query.answer("This is the count of spam-filtered IDs.")
        
    elif data.startswith("confirm_clear_spam_"):
        category = data.split("_")[-1]
        await callback_query.message.edit_text(
            f"<b>Confirm:</b> Are you sure you want to clear all <b>{category}</b> spam records?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Yes, Clear", callback_data=f"clear_spam_{category}"),
                    InlineKeyboardButton(text="Cancel", callback_data="spam_filter_menu")
                ]
            ]),
            parse_mode="HTML"
        )
        
    elif data.startswith("clear_spam_"):
        category = data.split("_")[-1]
        await clear_spam_records(user_id, category)
        await callback_query.answer(f"{category.capitalize()} spam records cleared.")
        # Refresh the menu
        await callback_query.message.edit_text(
            "<b>Spam Filter Settings</b>",
            reply_markup=await get_spam_filter_menu(user_id),
            parse_mode="HTML"
        )
    
    # --- BATCH MANAGEMENT ---
    elif data == "batch_management":
        await callback_query.message.edit_text("<b>Batch Management</b>", reply_markup=await get_batch_management_menu(user_id), parse_mode="HTML")
    
    elif data.startswith("view_batch_"):
        batch_name = data.replace("view_batch_", "")
        await show_batch_accounts_menu(callback_query, batch_name)

    elif data.startswith("batch_select|"):
        # Format: batch_select|{batch_name}|{global_index}
        try:
            _, batch_name, idx_str = original_data.split("|", 2)
            idx = int(idx_str)
        except Exception:
            return await callback_query.answer("Invalid data.", show_alert=True)

        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await set_current_account(user_id, tokens[idx]["token"])
            await show_batch_accounts_menu(callback_query, batch_name)

    elif data.startswith("batch_toggle|"):
        # Format: batch_toggle|{batch_name}|{global_index}
        try:
            _, batch_name, idx_str = original_data.split("|", 2)
            idx = int(idx_str)
        except Exception:
            return await callback_query.answer("Invalid data.", show_alert=True)

        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await toggle_token_status(user_id, tokens[idx]["token"])
            await show_batch_accounts_menu(callback_query, batch_name)

    elif data.startswith("batch_view|"):
        # Format: batch_view|{batch_name}|{global_index}
        try:
            _, batch_name, idx_str = original_data.split("|", 2)
            idx = int(idx_str)
        except Exception:
            return await callback_query.answer("Invalid data.", show_alert=True)

        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            token_obj = tokens[idx]
            info = await get_info_card(user_id, token_obj["token"])
            text = (
                f"<b>Name:</b> {html.escape(token_obj.get('name','N/A'))}\n"
                f"<b>Status:</b> {'Active' if token_obj.get('active', True) else 'Inactive'}\n\n"
                f"{info or 'No profile card found.'}"
            )
            await callback_query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data=f"view_batch_{batch_name}")]])
            )

    elif data.startswith("toggle_batch_"):
        batch_name = data.replace("toggle_batch_", "")
        success = await toggle_batch_status(user_id, batch_name)
        if success:
            await callback_query.answer(f"Toggled batch {batch_name} status!")
            await callback_query.message.edit_text("<b>Batch Management</b>", reply_markup=await get_batch_management_menu(user_id), parse_mode="HTML")
        else:
            await callback_query.answer("Failed to toggle batch status.", show_alert=True)

    elif data.startswith("batch_filter_"):
        batch_name = data.replace("batch_filter_", "")
        await callback_query.message.edit_text(f"<b>Set Filter for {batch_name}</b>\n\nSelect nationality filter:", reply_markup=get_batch_filter_menu(batch_name), parse_mode="HTML")

    elif data.startswith("batch_nat_"):
        parts = data.split("_")
        if len(parts) >= 3:
            nat_code = parts[2]
            batch_name = "_".join(parts[3:])

            if nat_code == "all":
                nat_code = ""

            success = await set_batch_filter(user_id, batch_name, nat_code)
            if success:
                await callback_query.answer(f"Filter updated for {batch_name}!")
                await callback_query.message.edit_text("<b>Batch Management</b>", reply_markup=await get_batch_management_menu(user_id), parse_mode="HTML")
            else:
                await callback_query.answer("Failed to update filter.", show_alert=True)

    elif data == "back_to_menu":
        await callback_query.message.edit_text("<b>Meeff Bot Dashboard</b>", reply_markup=start_markup, parse_mode="HTML")
        
    elif data in ("start", "start_all", "stop", "all_countries"):
        if data in ("start", "start_all", "all_countries") and state.get("running"): return await callback_query.answer("A process is already running!")
        if data == "stop" and not state.get("running"): return await callback_query.answer("No process is running!")
        
        if data == "start":
            msg = await callback_query.message.edit_text("<b>Initializing Requests...</b>", reply_markup=stop_markup, parse_mode="HTML")
            state.update({"running": True, "status_message_id": msg.message_id, "pinned_message_id": msg.message_id})
            await bot.pin_chat_message(chat_id=user_id, message_id=msg.message_id)
            asyncio.create_task(run_requests(user_id, bot, TARGET_CHANNEL_ID))
        elif data == "start_all":
            tokens = await get_active_tokens(user_id)
            if not tokens: return await callback_query.answer("No active tokens found.", show_alert=True)
            
            msg = await callback_query.message.edit_text(f"🔄 <b>AIO Starting ({len(tokens)})...</b>", reply_markup=stop_markup, parse_mode="HTML")
            state.update({"running": True, "status_message_id": msg.message_id, "pinned_message_id": msg.message_id})
            asyncio.create_task(process_all_tokens(user_id, tokens, bot, TARGET_CHANNEL_ID, initial_status_message=msg))
        elif data == "stop":
            state.update({"running": False, "stopped": True})
            await callback_query.message.edit_text(f"<b>Requests Stopped.</b>", reply_markup=start_markup, parse_mode="HTML")
            if state.get("pinned_message_id"): await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        elif data == "all_countries":
            msg = await callback_query.message.edit_text("<b>Starting All Countries Feature...</b>", reply_markup=stop_markup, parse_mode="HTML")
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
        ("password", "Enter password for access")]]
    await bot.set_my_commands(commands)

async def main():
    try:
        await set_bot_commands()
        dp.include_router(router)
        logger.info("Starting bot polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
