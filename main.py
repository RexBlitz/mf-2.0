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
    is_already_sent, toggle_token_status, get_active_tokens,
    get_token_status, set_account_active, get_info_card,
    set_individual_spam_filter, get_individual_spam_filter, get_all_spam_filters,get_spam_menu_data,
    list_all_collections, get_collection_summary, connect_to_collection,
    rename_user_collection, transfer_to_user, get_current_collection_info,
    get_spam_record_count, clear_spam_records,
    block_user, get_blocked_users, clear_blocked_users,
    get_automation_settings, set_automation_enabled, set_automation_lounge_message, 
    set_automation_chatroom_message, set_automation_accounts, add_automation_log, get_automation_log
)
# Make sure these other local modules are compatible if they also perform I/O
from lounge import send_lounge, send_lounge_all_tokens
from chatroom import send_message_to_everyone, send_message_to_everyone_all_tokens
from unsubscribe import unsubscribe_everyone
from filters import (
    meeff_filter_command, set_account_filter, get_meeff_filter_main_keyboard, 
    set_filter, apply_filter_for_account
)
from allcountry import run_all_countries
from signup import signup_command, signup_callback_handler, signup_message_handler, signup_settings_command
from friend_requests import run_requests, process_all_tokens, user_states, stop_markup
# --- NEW IMPORT ---
from automation import start_automation

# --- Configuration & Setup ---
API_TOKEN = "7916536914:AAHwtvO8hfGl2U4xcfM1fAjMLNypPFEW5JQ"
ADMIN_USER_IDS = {7405203657, 7725409374, 7691399254, 7795345443}
TEMP_PASSWORD = "11223344"
TARGET_CHANNEL_ID = -1002610862940
ACCOUNTS_PER_PAGE = 10 # Adjusted for extra button row

password_access: Dict[int, datetime] = {}
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
    
    # Get blocked users count
    from db import get_blocked_users
    blocked_users = await get_blocked_users(user_id)
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

def get_account_view_menu(account_idx: int, batch_num: int) -> InlineKeyboardMarkup:
    # Updated to use batch_num instead of page_idx
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete Account", callback_data=f"confirm_delete_{account_idx}|{batch_num}")],
        [InlineKeyboardButton(text="Back", callback_data=f"manage_accounts|{batch_num}")]
    ])

def get_confirmation_menu(action_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes", callback_data=f"confirm_{action_type}"), InlineKeyboardButton(text="Cancel", callback_data="back_to_menu")]])



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
    if not has_valid_access(user_id): return await message.reply("You are not authorized.")
    
    # ... (Same invoke logic as before, abbreviated for space)
    # Default behavior: check active
    active_tokens = await get_active_tokens(user_id)
    if not active_tokens: return await message.reply("No active accounts.")
    status_msg = await message.reply("<b>Checking Active Accounts...</b>", parse_mode="HTML")
    
    # Simple check logic...
    disabled = []
    async with aiohttp.ClientSession() as session:
        for t in active_tokens:
            try:
                headers = {"User-Agent": "okhttp/5.0.0-alpha.14", "meeff-access-token": t["token"]}
                async with session.get("https://api.meeff.com/facetalk/vibemeet/history/count/v1", params={"locale": "en"}, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if data.get("errorCode") == "AuthRequired": disabled.append(t)
            except: pass
    
    if disabled:
        for acc in disabled: await delete_token(user_id, acc["token"])
        await status_msg.edit_text(f"<b>Cleanup:</b> Removed {len(disabled)} disabled accounts.", parse_mode="HTML")
    else:
        await status_msg.edit_text("<b>All active accounts operational.</b>", parse_mode="HTML")

@router.message(Command("settings"))
async def settings_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    await message.reply("<b>Settings Menu</b>", reply_markup=await get_settings_menu(message.chat.id), parse_mode="HTML")

@router.message(Command("automation"))
async def automation_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    user_id = message.chat.id
    from db import get_automation_settings
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
    await message.reply("<b>Automation Menu</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.message(Command("block"))
async def block_command(message: Message):
    if not has_valid_access(message.chat.id): return await message.reply("You are not authorized.")
    args = message.text.split()
    if len(args) < 2: return await message.reply("Usage: /block meeff_user_id")
    await block_user(message.chat.id, args[1])
    await message.reply(f"✅ Blocked user: <code>{args[1]}</code>", parse_mode="HTML")

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
                await message.reply(f"Response: {data.get('errorCode') or 'Success'}")
    except Exception as e:
        await message.reply(f"Error: {e}")

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
        
        status_msg = await message.reply("<b>Saving Token...</b>", parse_mode="HTML")
        account_name = token_data[1] if len(token_data) > 1 else f"Account {len(await get_tokens(user_id)) + 1}"
        token_index = await set_token(user_id, token, account_name)
        if token_index != -1: await add_token_to_auto_batch(user_id, token_index)
        await status_msg.edit_text(f"✅ <b>Token Saved</b>: '<code>{html.escape(account_name)}</code>'.", parse_mode="HTML")

async def show_manage_accounts_menu(callback_query: CallbackQuery):
    """Display list of all batches with batch-level controls"""
    user_id = callback_query.from_user.id
    tokens = await get_tokens(user_id)
    total_accounts = len(tokens)
    
    if not tokens:
        return await callback_query.message.edit_text("<b>No Accounts Found</b>...", reply_markup=back_markup, parse_mode="HTML")
    
    # Calculate batches (10 accounts per batch)
    batch_size = 10
    total_batches = (total_accounts + batch_size - 1) // batch_size
    all_filters = await get_all_user_filters(user_id)
    
    # Build batch list
    buttons = []
    for batch_num in range(1, total_batches + 1):
        start_idx = (batch_num - 1) * batch_size
        end_idx = min(start_idx + batch_size, total_accounts)
        batch_tokens_list = tokens[start_idx:end_idx]
        
        # Check if all accounts in this batch are active
        all_active = all(tok.get('active', True) for tok in batch_tokens_list)
        batch_status = "✅" if all_active else "❌"
        
        # Row 1: Batch name and View button
        buttons.append([
            InlineKeyboardButton(text=f"📦 Batch {batch_num} ({end_idx - start_idx} acc)", callback_data=f"view_batch|{batch_num}")
        ])
        
        # Row 2: Controls for this batch
        buttons.append([
            InlineKeyboardButton(text=f"Status {batch_status}", callback_data=f"batch_toggle_all|{batch_num}"),
            InlineKeyboardButton(text="🌍 Filter", callback_data=f"batch_filter_all|{batch_num}"),
            InlineKeyboardButton(text="🔄 Relogin", callback_data=f"batch_relogin|{batch_num}")
        ])
    
    buttons.append([InlineKeyboardButton(text="Back", callback_data="settings_menu")])
    
    menu_text = f"<b>Manage Accounts</b>\nTotal Accounts: {total_accounts}\nTotal Batches: {total_batches}"
    
    try:
        await callback_query.message.edit_text(menu_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in e.message: logger.error(f"Error editing message: {e}")
        await callback_query.answer()


async def show_batch_accounts_menu(callback_query: CallbackQuery, batch_num: int):
    """Display accounts within a specific batch"""
    user_id = callback_query.from_user.id
    tokens = await get_tokens(user_id)
    total_accounts = len(tokens)
    current_token = await get_current_account(user_id)
    
    if not tokens:
        return await callback_query.message.edit_text("<b>No Accounts Found</b>...", reply_markup=back_markup, parse_mode="HTML")
    
    # Calculate batch indices (10 accounts per batch)
    batch_size = 10
    total_batches = (total_accounts + batch_size - 1) // batch_size
    batch_num = max(1, min(batch_num, total_batches))
    
    start_idx = (batch_num - 1) * batch_size
    end_idx = min(start_idx + batch_size, total_accounts)
    batch_tokens = tokens[start_idx:end_idx]
    batch_indices = list(range(start_idx, end_idx))
    
    all_filters = await get_all_user_filters(user_id)
    
    # Build account list
    buttons = []
    for i, tok in enumerate(batch_tokens):
        global_idx = batch_indices[i]
        is_current = "🔹" if tok['token'] == current_token else "▫️"
        token_filters = all_filters.get(tok['token'], {})
        nationality_code = token_filters.get("filterNationalityCode", "")
        account_name = html.escape(tok['name'][:15])
        
        buttons.append([
            InlineKeyboardButton(text=f"{is_current} {account_name}", callback_data=f"set_account_{global_idx}|{batch_num}"),
            InlineKeyboardButton(text="ON" if tok.get('active', True) else "OFF", callback_data=f"toggle_status_{global_idx}|{batch_num}"),
            InlineKeyboardButton(text=f"Nation: {nationality_code or 'All'}", callback_data=f"manage_acc_filter|{global_idx}|{batch_num}"),
            InlineKeyboardButton(text="View", callback_data=f"view_account_{global_idx}|{batch_num}")
        ])
    
    buttons.append([InlineKeyboardButton(text="Back to Batches", callback_data="manage_accounts")])
    
    menu_text = f"<b>Batch {batch_num} - Accounts {start_idx + 1}-{end_idx} of {total_accounts}</b>\nCurrently selected: {'Yes' if current_token else 'No'}"
    
    try:
        await callback_query.message.edit_text(menu_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in e.message: logger.error(f"Error editing message: {e}")
        await callback_query.answer()





@router.callback_query()
async def callback_handler(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    batch_num = 1
    original_data = data
    
    # Updated pagination check to exclude filter commands - extracts batch_num from callback data
    # Don't extract from view_batch, batch_toggle_all, batch_filter_all, batch_relogin as they handle their own batch_num
    if "|" in data and not data.startswith(("manage_acc_filter|", "manage_acc_nat_", "view_batch|", "batch_toggle_all|", "batch_filter_all|", "batch_filter_apply_all|", "batch_relogin|")):
        data_parts = data.split("|")
        try:
            if len(data_parts) > 1 and data_parts[-1].isdigit():
                batch_num = int(data_parts[-1])
                data = "|".join(data_parts[:-1])
        except ValueError: pass

    if await signup_callback_handler(callback_query): return
    if not has_valid_access(user_id): return await callback_query.answer("You are not authorized.")
    
    state = user_states.setdefault(user_id, {})
    
    if data == "manage_accounts":
        await show_manage_accounts_menu(callback_query)
    
    elif data.startswith("view_batch|"):
        try:
            batch_num = int(data.replace("view_batch|", ""))
            await show_batch_accounts_menu(callback_query, batch_num)
        except ValueError:
            await callback_query.answer("Invalid batch number.", show_alert=True)
    
    # --- NEW: Main Menu Account Filter Logic ---
    elif data.startswith("manage_acc_filter|"):
        # Format: manage_acc_filter|{global_index}|{batch_num}
        parts = data.split("|")
        if len(parts) >= 3:
            try:
                global_index = int(parts[1])
                # Restore batch_num from the button data so we can go back
                menu_batch_num = int(parts[2]) 
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    token_filters = await get_user_filters(user_id, tok['token']) or {}
                    current_nat = token_filters.get("filterNationalityCode", "")
                    
                    buttons = []
                    all_mark = "✅ " if not current_nat else ""
                    # Pass batch_num along to the nation selection
                    buttons.append([InlineKeyboardButton(text=f"{all_mark}All Countries", callback_data=f"manage_acc_nat_all|{global_index}|{menu_batch_num}")])
                    
                    row = []
                    for i, (code, name) in enumerate(NATIONALITY_LIST):
                        mark = "✅ " if current_nat == code else ""
                        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"manage_acc_nat_{code}|{global_index}|{menu_batch_num}"))
                        if len(row) == 2 or i == len(NATIONALITY_LIST) - 1:
                            buttons.append(row)
                            row = []
                    
                    # Back button returns to the correct batch
                    buttons.append([InlineKeyboardButton(text="Back", callback_data=f"manage_accounts|{menu_batch_num}")])
                    
                    await callback_query.message.edit_text(
                        f"<b>Filter for {html.escape(tok['name'])}</b>\n\nCurrent: <b>{current_nat or 'All'}</b>\nSelect nationality:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                        parse_mode="HTML"
                    )
            except (ValueError, IndexError):
                await callback_query.answer("Invalid account.", show_alert=True)

    elif data.startswith("manage_acc_nat_"):
        # Format: manage_acc_nat_{CODE}|{global_index}|{batch_num}
        parts = data.split("|")
        if len(parts) >= 3:
            nat_code = parts[0].replace("manage_acc_nat_", "")
            try:
                global_index = int(parts[1])
                menu_batch_num = int(parts[2])
                tokens = await get_tokens(user_id)
                if 0 <= global_index < len(tokens):
                    tok = tokens[global_index]
                    
                    user_filters = await get_user_filters(user_id, tok['token']) or {}
                    if nat_code == "all":
                        user_filters["filterNationalityCode"] = ""
                        display_text = "All Countries"
                    else:
                        user_filters["filterNationalityCode"] = nat_code
                        display_text = nat_code
                    
                    await set_user_filters(user_id, tok['token'], user_filters)
                    
                    if await apply_filter_for_account(tok['token'], user_id):
                        await callback_query.answer(f"Filter set to {display_text}")
                    else:
                        await callback_query.answer("Saved, but API update failed.", show_alert=True)
                    
                    # Return to the correct batch in Manage Accounts
                    await show_batch_accounts_menu(callback_query, menu_batch_num)
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
    elif data == "settings_menu":
        await callback_query.message.edit_text("<b>Settings Menu</b>", reply_markup=await get_settings_menu(user_id), parse_mode="HTML")
    elif data == "show_filters":
        await callback_query.message.edit_text("<b>Filter Settings</b>", reply_markup=await get_meeff_filter_main_keyboard(user_id), parse_mode="HTML")
    elif data in ("toggle_request_filter", "meeff_filter_main") or data.startswith(("account_filter_", "account_gender_", "account_age_", "account_nationality_")):
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
            details += info_card if info_card else "No profile card found."
            await callback_query.message.edit_text(details, reply_markup=get_account_view_menu(idx, batch_num), parse_mode="HTML", disable_web_page_preview=True)
            
    elif data.startswith("confirm_delete_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await callback_query.message.edit_text(f"<b>Confirm Deletion</b> of <code>{html.escape(tokens[idx]['name'])}</code>?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes, Delete", callback_data=f"delete_account_{idx}|{batch_num}"), InlineKeyboardButton(text="Cancel", callback_data=f"manage_accounts|{batch_num}")]]) , parse_mode="HTML")

    elif data.startswith("toggle_status_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await toggle_token_status(user_id, tokens[idx]["token"])
            await show_batch_accounts_menu(callback_query, batch_num)
    
    elif data.startswith("set_account_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await set_current_account(user_id, tokens[idx]["token"])
            await show_batch_accounts_menu(callback_query, batch_num)
            
    elif data.startswith("delete_account_"):
        try: idx = int(data.split("_")[-1])
        except ValueError: return await callback_query.answer("Invalid account index.", show_alert=True)
        tokens = await get_tokens(user_id)
        if 0 <= idx < len(tokens):
            await delete_token(user_id, tokens[idx]["token"])
            await show_batch_accounts_menu(callback_query, batch_num)

    elif data == "noop_page":
        await callback_query.answer("You are on this batch.")
    
    # --- BATCH-LEVEL CONTROLS ---
    elif data.startswith("batch_toggle_all|"):
        try:
            batch_num_val = int(data.replace("batch_toggle_all|", ""))
            tokens = await get_tokens(user_id)
            batch_size = 10
            start_idx = (batch_num_val - 1) * batch_size
            end_idx = min(start_idx + batch_size, len(tokens))
            batch_tokens_list = tokens[start_idx:end_idx]
            
            # Check if all are active
            all_active = all(tok.get('active', True) for tok in batch_tokens_list)
            new_status = not all_active
            
            # Toggle all accounts in this batch
            for tok in batch_tokens_list:
                await toggle_token_status(user_id, tok["token"])
            
            await callback_query.answer(f"✅ Toggled all accounts in batch to {'ON' if new_status else 'OFF'}")
            await show_manage_accounts_menu(callback_query)
        except (ValueError, IndexError):
            await callback_query.answer("Error toggling batch status.", show_alert=True)
    
    elif data.startswith("batch_filter_all|"):
        try:
            batch_num_val = int(data.replace("batch_filter_all|", ""))
            # Show nationality filter selection for the entire batch
            buttons = []
            all_mark = "✅ "
            buttons.append([InlineKeyboardButton(text=f"{all_mark}All Countries", callback_data=f"batch_filter_apply_all|all|{batch_num_val}")])
            
            row = []
            for i, (code, name) in enumerate(NATIONALITY_LIST):
                row.append(InlineKeyboardButton(text=f"{name}", callback_data=f"batch_filter_apply_all|{code}|{batch_num_val}"))
                if len(row) == 2 or i == len(NATIONALITY_LIST) - 1:
                    buttons.append(row)
                    row = []
            
            buttons.append([InlineKeyboardButton(text="Cancel", callback_data=f"view_batch|{batch_num_val}")])
            await callback_query.message.edit_text(f"<b>Apply Filter to All Accounts in Batch {batch_num_val}</b>\n\nSelect nationality to apply to all {len(NATIONALITY_LIST)} accounts:", 
                                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        except ValueError:
            await callback_query.answer("Invalid batch number.", show_alert=True)
    
    elif data.startswith("batch_filter_apply_all|"):
        parts = data.split("|")
        if len(parts) >= 3:
            nat_code = parts[1]
            try:
                batch_num_val = int(parts[2])
                tokens = await get_tokens(user_id)
                batch_size = 10
                start_idx = (batch_num_val - 1) * batch_size
                end_idx = min(start_idx + batch_size, len(tokens))
                batch_tokens_list = tokens[start_idx:end_idx]
                
                # Apply filter to all accounts in this batch
                applied_count = 0
                for tok in batch_tokens_list:
                    user_filters = await get_user_filters(user_id, tok['token']) or {}
                    if nat_code == "all":
                        user_filters["filterNationalityCode"] = ""
                    else:
                        user_filters["filterNationalityCode"] = nat_code
                    await set_user_filters(user_id, tok['token'], user_filters)
                    await apply_filter_for_account(tok['token'], user_id)
                    applied_count += 1
                
                display_text = "All Countries" if nat_code == "all" else nat_code
                await callback_query.answer(f"✅ Applied filter '{display_text}' to {applied_count} accounts")
                await show_manage_accounts_menu(callback_query)
            except (ValueError, IndexError):
                await callback_query.answer("Error applying filter.", show_alert=True)
    
    elif data.startswith("batch_relogin|"):
        try:
            batch_num_val = int(data.replace("batch_relogin|", ""))
            tokens = await get_tokens(user_id)
            batch_size = 10
            start_idx = (batch_num_val - 1) * batch_size
            end_idx = min(start_idx + batch_size, len(tokens))
            batch_tokens_list = tokens[start_idx:end_idx]
            
            # Collect email/password pairs for relogin
            accounts_to_login = []
            for tok in batch_tokens_list:
                email = tok.get("email")
                password = tok.get("password")
                if email and password:
                    accounts_to_login.append((email, password))
            
            if not accounts_to_login:
                await callback_query.answer("❌ No accounts with email/password found in this batch.", show_alert=True)
                return await show_manage_accounts_menu(callback_query)
            
            # Show processing message
            await callback_query.answer(f"✅ Relogin started for {len(accounts_to_login)} accounts using saved credentials and device info...")
            
            # Import and run the multi-signin function (uses device info stored per email)
            from signup import do_multi_signin
            await do_multi_signin(callback_query.message, user_id, accounts_to_login)
            
        except (ValueError, IndexError) as e:
            logger.error(f"Error relogging in batch: {e}")
            await callback_query.answer("Error relogging in batch.", show_alert=True)
    
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
            await callback_query.message.edit_text("<b>Set Lounge Message</b>\n\nType the message you want to auto-send in lounge:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="back_to_automation")]]))
        
        elif data == "auto_set_chatroom":
            await callback_query.message.edit_text("<b>Set Chatroom Message</b>\n\nType the message you want to auto-send in chatroom:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="back_to_automation")]]))
        
        elif data == "auto_select_accounts":
            settings = await get_automation_settings(user_id)
            selected = settings.get("selected_accounts", "all")
            
            mark_current = "✅ " if selected == "current" else ""
            mark_active  = "✅ " if selected == "active_only" else ""

            buttons = [
                [InlineKeyboardButton(text=f"{mark_current}Current Account", callback_data="auto_acc_current")],
                [InlineKeyboardButton(text=f"{mark_active}All Active Accounts", callback_data="auto_acc_active")],
                [InlineKeyboardButton(text="Back", callback_data="back_to_automation")]
            ]
            await callback_query.message.edit_text("<b>Select Automation Accounts</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        
       # --- OPTION 1: CURRENT ---
        elif data == "auto_acc_current":
            await set_automation_accounts(user_id, "current")
            await callback_query.answer("Current account selected!")
            callback_query.data = "auto_select_accounts"
            await callback_handler(callback_query)
        
       # --- OPTION 2: ALL ACTIVE ---
        elif data == "auto_acc_active":
            await set_automation_accounts(user_id, "active_only")
            await callback_query.answer("Active accounts selected!")
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
            await automation_command(callback_query.message)
    
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
            await callback_query.message.edit_text(f"<b>Stopped.</b>", reply_markup=start_markup, parse_mode="HTML")
            if state.get("pinned_message_id"): await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
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
        logger.info("Starting bot polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
