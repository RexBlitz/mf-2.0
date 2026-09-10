import aiohttp
import json
from meeff_http import create_meeff_session
import random
import itertools
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dateutil import parser

# local modules (must exist)
from device_info import get_or_create_device_info_for_email, get_api_payload_with_device_info
from db import (
    # Line 15: The error is here, or in the indentation immediately before
    #          `set_token` or between the items.
    set_token, set_info_card, set_signup_config, get_signup_config, set_user_filters,
    get_pending_accounts, add_pending_accounts, remove_pending_account, clear_pending_accounts,
    add_token_to_auto_batch
)
from filters import get_nationality_keyboard

logger = logging.getLogger(__name__)

# -------------------------
# Config / Defaults
# -------------------------
DEFAULT_BIOS = [
    "Love traveling and meeting new people!",
    "Coffee lover and adventure seeker",
    "Passionate about music and good vibes",
    "Foodie exploring new cuisines",
    "Fitness enthusiast and nature lover",
]
DEFAULT_PHOTOS = (
    "https://meeffus.s3.amazonaws.com/profile/2025/06/16/"
    "20250616052423006_profile-1.0-bd262b27-1916-4bd3-9f1d-0e7fdba35268.jpg|"
    "https://meeffus.s3.amazonaws.com/profile/2025/06/16/"
    "20250616052438006_profile-1.0-349bf38c-4555-40cc-a322-e61afe15aa35.jpg"
)

# in-memory state for currently interacting users (UI / wizard state)
user_signup_states: Dict[int, Dict] = {}

# -------------------------
# Keyboard templates
# -------------------------
# MODIFIED: Removed Multi Sign In button
SIGNUP_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="Sign Up", callback_data="signup_go"),
        InlineKeyboardButton(text="Sign In", callback_data="signin_go")
    ],
    [
        InlineKeyboardButton(text="Signup Config", callback_data="signup_settings")
    ],
    [InlineKeyboardButton(text="Back to Main Menu", callback_data="back_to_menu")]
])

VERIFY_AND_BACK_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Verify All Emails", callback_data="verify_accounts")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

VERIFY_AND_SKIP_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Verify All Emails", callback_data="verify_accounts")],
    [InlineKeyboardButton(text="Skip For Now (Save Pending)", callback_data="skip_pending")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

SKIP_VERIFICATION_BUTTON = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Skip For Now (Save Pending)", callback_data="skip_pending")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

PENDING_LOGIN_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Login Pending Accounts", callback_data="login_pending")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

RETRY_VERIFY_BUTTON = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Retry Pending Verification", callback_data="retry_pending")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

BACK_TO_SIGNUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

BACK_TO_CONFIG = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Back", callback_data="signup_settings")]
])

DONE_PHOTOS = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Done", callback_data="signup_photos_done")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

FILTER_NATIONALITY_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="All Countries", callback_data="signup_filter_nationality_all")],
    [
        InlineKeyboardButton(text="🇷🇺 RU", callback_data="signup_filter_nationality_RU"),
        InlineKeyboardButton(text="🇺🇦 UA", callback_data="signup_filter_nationality_UA"),
        InlineKeyboardButton(text="🇧🇾 BY", callback_data="signup_filter_nationality_BY"),
        InlineKeyboardButton(text="🇮🇷 IR", callback_data="signup_filter_nationality_IR"),
        InlineKeyboardButton(text="🇵🇭 PH", callback_data="signup_filter_nationality_PH")
    ],
    [
        InlineKeyboardButton(text="🇵🇰 PK", callback_data="signup_filter_nationality_PK"),
        InlineKeyboardButton(text="🇺🇸 US", callback_data="signup_filter_nationality_US"),
        InlineKeyboardButton(text="🇮🇳 IN", callback_data="signup_filter_nationality_IN"),
        InlineKeyboardButton(text="🇩🇪 DE", callback_data="signup_filter_nationality_DE"),
        InlineKeyboardButton(text="🇫🇷 FR", callback_data="signup_filter_nationality_FR")
    ],
    [
        InlineKeyboardButton(text="🇧🇷 BR", callback_data="signup_filter_nationality_BR"),
        InlineKeyboardButton(text="🇨🇳 CN", callback_data="signup_filter_nationality_CN"),
        InlineKeyboardButton(text="🇯🇵 JP", callback_data="signup_filter_nationality_JP"),
        InlineKeyboardButton(text="🇰🇷 KR", callback_data="signup_filter_nationality_KR"),
        InlineKeyboardButton(text="🇨🇦 CA", callback_data="signup_filter_nationality_CA")
    ],
    [
        InlineKeyboardButton(text="🇦🇺 AU", callback_data="signup_filter_nationality_AU"),
        InlineKeyboardButton(text="🇮🇹 IT", callback_data="signup_filter_nationality_IT"),
        InlineKeyboardButton(text="🇪🇸 ES", callback_data="signup_filter_nationality_ES"),
        InlineKeyboardButton(text="🇿🇦 ZA", callback_data="signup_filter_nationality_ZA"),
        InlineKeyboardButton(text="🇹🇷 TR", callback_data="signup_filter_nationality_TR")
    ],
    [InlineKeyboardButton(text="Back", callback_data="signup_photos_done")]
])


# -------------------------
# Signup Settings Menu
# -------------------------
async def signup_settings_command(message: Message, is_callback: bool = False):
    user_id = message.chat.id if not is_callback else message.chat.id
    cfg = await get_signup_config(user_id) or {}

    email = cfg.get("email", "Not Set")
    password = cfg.get("password", "Not Set")
    gender = cfg.get("gender", "Not Set")
    birth_year = cfg.get("birth_year", "Not Set")
    nationality = cfg.get("nationality", "Not Set")
    auto_signup = cfg.get("auto_signup", False)

    text = (
        "<b>⚙️ Signup Configuration</b>\n\n"
        f"<b>Email:</b> <code>{email}</code>\n"
        f"<b>Password:</b> <code>{password}</code>\n"
        f"<b>Gender:</b> {gender}\n"
        f"<b>Birth Year:</b> {birth_year}\n"
        f"<b>Nationality:</b> {nationality}\n"
        f"<b>Auto Signup:</b> {'ON ✅' if auto_signup else 'OFF ❌'}\n\n"
        "<b>Update settings below:</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Setup / Edit Signup Config", callback_data="setup_signup_config")],
        [InlineKeyboardButton(text=f"Auto Signup: {'Disable' if auto_signup else 'Enable'}", callback_data="toggle_auto_signup")],
        [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
    ])

    if is_callback:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

# -------------------------
# Utilities / Helpers
# -------------------------
def format_user_with_nationality(user: Dict) -> str:
    def time_ago(dt_str: Optional[str]) -> str:
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
    card = (
        f"<b>📱 Account Information</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>👤 Name:</b> {user.get('name', 'N/A')}\n"
        f"<b>🆔 ID:</b> <code>{user.get('_id', 'N/A')}</code>\n"
        f"<b>📝 Bio:</b> {user.get('description', 'N/A')}\n"
        f"<b>🎂 Birth Year:</b> {user.get('birthYear', 'N/A')}\n"
        f"<b>🌍 Country:</b> {user.get('nationalityCode', 'N/A')}\n"
        f"<b>📱 Platform:</b> {user.get('platform', 'N/A')}\n"
        f"<b>⭐ Score:</b> {user.get('profileScore', 'N/A')}\n"
        f"<b>📍 Distance:</b> {user.get('distance', 'N/A')} km\n"
        f"<b>🗣️ Languages:</b> {', '.join(user.get('languageCodes', [])) or 'N/A'}\n"
        f"<b>🕐 Last Active:</b> {last_active}\n"
    )

    if user.get('photoUrls'):
        card += f"<b>📸 Photos:</b> " + ' '.join([f"<a href='{url}'>📷</a>" for url in user.get('photoUrls', [])])

    if "email" in user:
        card += f"\n\n<b>📧 Email:</b> <code>{user['email']}</code>"
    if "password" in user:
        card += f"\n<b>🔐 Password:</b> <code>{user['password']}</code>"
    if "token" in user:
        card += f"\n<b>🔑 Token:</b> <code>{user['token']}</code>"

    return card

def generate_email_variations(base_email: str, count: int = 1000) -> List[str]:
    if '@' not in base_email:
        return []
    username, domain = base_email.split('@', 1)
    variations = {base_email}
    max_dots = min(4, max(0, len(username) - 1))

    for i in range(1, max_dots + 1):
        for positions in itertools.combinations(range(1, len(username)), i):
            if len(variations) >= count:
                return list(variations)
            new_username = list(username)
            for pos in reversed(positions):
                new_username.insert(pos, '.')
            variations.add(''.join(new_username) + '@' + domain)

    return list(variations)[:count]

def get_random_bio() -> str:
    return random.choice(DEFAULT_BIOS)

# -------------------------
# Async HTTP helpers (shared session optional)
# -------------------------
async def _post_json(session: aiohttp.ClientSession, url: str, payload: Dict, headers: Dict = None, timeout: int = 30):
    headers = headers or {}
    try:
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            try:
                return resp.status, await resp.json()
            except aiohttp.ContentTypeError:
                text = await resp.text()
                return resp.status, {"errorMessage": text}
    except Exception as e:
        logger.error(f"_post_json error {e} for {url}")
        return None, {"errorMessage": str(e)}

# -------------------------
# Signup preview / helpers
# -------------------------
async def select_available_emails(base_email: str, num_accounts: int, pending_emails: List[str], used_emails: List[str]) -> List[str]:
    available_emails = []
    used_emails_set = set(used_emails or [])

    # check pending first
    pending_to_check = [e for e in pending_emails if e not in used_emails_set]
    if pending_to_check:
        async with create_meeff_session() as s:
            tasks = [ _post_json(s, "https://api.meeff.com/user/checkEmail/v1", {"email": e, "locale":"en"}) for e in pending_to_check ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, tuple):
                    status, body = r
                    # treat 406 or explicit message as used
                    if body.get("errorMessage") and "already in use" in body.get("errorMessage", "").lower():
                        continue
                    # available
                    if len(available_emails) < num_accounts:
                        available_emails.append(pending_to_check[i])

    # generate new variations if needed
    if len(available_emails) < num_accounts:
        variants = generate_email_variations(base_email, num_accounts * 10)
        candidates = [e for e in variants if e not in pending_emails and e not in available_emails and e not in used_emails_set]
        if candidates:
            async with create_meeff_session() as s:
                tasks = [ _post_json(s, "https://api.meeff.com/user/checkEmail/v1", {"email": e, "locale":"en"}) for e in candidates ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, r in enumerate(results):
                    if len(available_emails) >= num_accounts:
                        break
                    if isinstance(r, tuple):
                        status, body = r
                        if body.get("errorMessage") and "already in use" in body.get("errorMessage", "").lower():
                            continue
                        available_emails.append(candidates[i])

    return available_emails


# -------------------------
# Signup command (shows menu + pending count)
# -------------------------
async def signup_command(message: Message) -> None:
    user_id = message.chat.id
    user_signup_states[user_id] = {"stage": "menu"}
    pending = await get_pending_accounts(user_id)
    pending_count = len(pending) if pending else 0
    
    # build menu copy so we can inject buttons
    menu = [row[:] for row in SIGNUP_MENU.inline_keyboard]
    
    if pending_count > 0:
        menu.insert(0, [InlineKeyboardButton(
            text=f"Login Pending Accounts ({pending_count})",
            callback_data="login_pending"
        )])

    await message.answer(
        "<b>Account Creation</b>\n\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=menu),
        parse_mode="HTML"
    )

# -------------------------
# show preview helper
# -------------------------
async def show_signup_preview(message: Message, user_id: int, state: Dict) -> None:
    config = await get_signup_config(user_id) or {}
    manual_mode = state.get("manual_mode", False)

    # Manual mode only needs password/gender/birth_year/nationality from config;
    # the email itself comes from state["manual_email"], not the auto base email.
    required_keys = ['password', 'gender', 'birth_year', 'nationality'] if manual_mode else \
                     ['email', 'password', 'gender', 'birth_year', 'nationality']
    if not all(k in config for k in required_keys):
        await message.edit_text(
            "<b>Configuration Incomplete</b>\n\nYou must set up all details in 'Signup Config' first.",
            reply_markup=SIGNUP_MENU,
            parse_mode="HTML"
        )
        return

    pending_emails = [acc['email'] for acc in state.get('pending_accounts', [])]

    if manual_mode:
        # Don't touch the auto base email / auto email-generation logic at all.
        manual_email = state.get("manual_email", "")
        state["selected_emails"] = [manual_email] if manual_email else []
        available_emails = state["selected_emails"]
    else:
        await message.edit_text("<b>Checking email availability concurrently...</b> This may take a moment.")

        num_accounts = state.get('num_accounts', 1)
        used_emails = config.get("used_emails", [])

        available_emails = await select_available_emails(config.get("email", ""), num_accounts, pending_emails, used_emails)
        state["selected_emails"] = available_emails

    email_list = '\n'.join([f"{i+1}. <code>{email}</code>{' (Pending)' if email in pending_emails else ''}" for i, email in enumerate(available_emails)]) if available_emails else "No available emails found!"
    preview_text = (
        f"<b>Signup Preview</b>\n\n"
        f"<b>Name:</b> {state.get('name', 'N/A')}\n"
        f"<b>Photos:</b> {len(state.get('photos', []))} uploaded\n"
        f"<b>Number of Accounts:</b> {state.get('num_accounts', 1)}\n"
        f"<b>Gender:</b> {config.get('gender', 'N/A')}\n"
        f"<b>Birth Year:</b> {config.get('birth_year', 'N/A')}\n"
        f"<b>Nationality:</b> {config.get('nationality', 'N/A')}\n"
        f"<b>Filter Nationality:</b> {state.get('filter_nationality', 'All Countries')}\n\n"
        f"<b>Emails to be Used:</b>\n{email_list}\n\n"
        f"<b>Ready to create {len(available_emails)} of {state.get('num_accounts',1)} requested account{'s' if state.get('num_accounts',1) > 1 else ''}?</b>"
    )
    confirm_text = f"Create {len(available_emails)} Account{'s' if len(available_emails) != 1 else ''}"
    menu = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=confirm_text, callback_data="create_accounts_confirm")],
        [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
    ])
    await message.edit_text(preview_text, reply_markup=menu, parse_mode="HTML")
    user_signup_states[user_id] = state

# -------------------------
# Core signing functions
# -------------------------
async def try_signup(state: Dict, telegram_user_id: int) -> Dict:
    # small jitter to avoid bursts
    await asyncio.sleep(random.uniform(0.5, 1.2))

    url = "https://api.meeff.com/user/register/email/v4"
    device_info = await get_or_create_device_info_for_email(telegram_user_id, state["email"])
    logger.info(f"SIGNUP using device_id={device_info.get('device_unique_id')} for email={state['email']}")

    base_payload = {
        "providerId": state["email"],
        "providerToken": state["password"],
        "name": state["name"],
        "gender": state["gender"],
        "birthYear": state.get("birth_year", 2004),
        "nationalityCode": state.get("nationality", "US"),
        "description": state["desc"],
        "photos": "|".join(state.get("photos", [])) or DEFAULT_PHOTOS,
        "locale": "en",
        "color": "777777",
        "birthMonth": 3,
        "birthDay": 1,
        "languages": "en,es,fr",
        "levels": "5,1,1",
        "purpose": "PB000000,PB000001",
        "purposeEtcDetail": "",
        "interest": "IS000001,IS000002,IS000003,IS000004",
    }
    payload = get_api_payload_with_device_info(base_payload, device_info)
    headers = {'User-Agent': "okhttp/5.0.0-alpha.14", 'Content-Type': "application/json; charset=utf-8"}

    async with create_meeff_session() as session:
        status, body = await _post_json(session, url, payload, headers=headers)
        if status is None:
            return {"errorMessage": "Network error during signup"}
        return body

async def try_signin(email: str, password: str, telegram_user_id: int, session: aiohttp.ClientSession = None) -> Dict:
    # small jitter to avoid bursts
    await asyncio.sleep(random.uniform(0.5, 1.2))

    url = "https://api.meeff.com/user/login/v4"
    device_info = await get_or_create_device_info_for_email(telegram_user_id, email)
    logger.info(f"SIGNIN using device_id={device_info.get('device_unique_id')} for email={email}")

    base_payload = {"provider": "email", "providerId": email, "providerToken": password, "locale": "en"}
    payload = get_api_payload_with_device_info(base_payload, device_info)
    headers = {'User-Agent': "okhttp/5.0.0-alpha.14", 'Content-Type': "application/json; charset=utf-8"}

    # allow passing a shared session
    close_session = False
    if session is None:
        session = create_meeff_session()
        close_session = True

    try:
        status, body = await _post_json(session, url, payload, headers=headers)
        if status is None:
            logger.error(f"SIGNIN FAILED (network) email={email}")
            return {"errorMessage": "Network error during signin"}
        if status == 200 and body.get("accessToken"):
            logger.info(f"SIGNIN OK email={email}")
        else:
            err = body.get("errorMessage") or body.get("message") or "unknown"
            logger.warning(f"SIGNIN FAILED email={email} status={status} reason={err!r}")
        return body
    finally:
        if close_session:
            await session.close()

# -------------------------
# New Multi Sign In Logic
# -------------------------
async def do_multi_signin(message: Message, user_id: int, accounts_to_login: List[Tuple[str, str]]) -> None:
    # --- Send a new message from the bot and use it for edits ---
    msg_to_edit = await message.answer(f"<b>Starting Multi-Login for {len(accounts_to_login)} Accounts...</b>\nProcessing in batches of 5.", parse_mode="HTML")

    MAX_CONCURRENT = 4
    MAX_RETRIES = 3
    BACKOFF_BASE = 1.0
    
    # --- BATCHING CONFIG ---
    BATCH_SIZE = 5
    BATCH_DELAY_SECONDS = 60 

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    session = create_meeff_session()

    # Worker takes a tuple: (email, password, retry_count)
    async def worker_login(email, password, current_retries):
        acc = {"email": email, "password": password, "retries": current_retries}
        for attempt in range(1, MAX_RETRIES + 1):
            await sem.acquire()
            try:
                res = await try_signin(email, password, user_id, session=session)
            finally:
                sem.release()

            if isinstance(res, dict) and res.get("accessToken") and res.get("user"):
                return res, acc # Success

            err = (res.get("errorMessage") or "").lower() if isinstance(res, dict) else str(res)
            retryable = ("429" in err) or ("rate" in err) or ("tempor" in err) or ("connection" in err) or ("unverified" in err)
            permanent_error = ("password" in err) or ("invalid" in err) or ("user not found" in err)
            
            if attempt < MAX_RETRIES and retryable and not permanent_error:
                backoff = BACKOFF_BASE * (2 ** (attempt - 1)) + random.random() * 0.4
                await asyncio.sleep(backoff)
                continue
            
            return res, acc

    verified_count = 0
    permanent_failed_emails = []
    
    # Convert incoming list of (email, password) into (email, password, retry_count=0)
    accounts_remaining = [(email, password, 0) for email, password in accounts_to_login]
    total_accounts = len(accounts_to_login)
    
    batch_number = 0
    
    # Pre-calculate total batches based on initial size
    total_batches = (total_accounts // BATCH_SIZE) + (1 if total_accounts % BATCH_SIZE else 0)

    # --- MAIN BATCHING LOGIC WITH LIVE PROGRESS AND RETRIES ---
    while accounts_remaining:
        batch_number += 1
        
        # Determine the batch size, ensuring we don't exceed the number of remaining accounts
        current_batch_size = min(BATCH_SIZE, len(accounts_remaining))
        batch = accounts_remaining[:current_batch_size]
        accounts_remaining = accounts_remaining[current_batch_size:]
        
        # --- Live Update 1: Starting the Batch ---
        await msg_to_edit.edit_text(
            f"<b>Batch {batch_number} of {total_batches} in Progress...</b> ⏳\n"
            f"Accounts in this batch: {len(batch)}\n"
            f"Total Processed: {verified_count + len(permanent_failed_emails)} / {total_accounts}",
            parse_mode="HTML"
        )
        
        # Run current batch concurrently
        tasks = [worker_login(email, password, retries) for email, password, retries in batch]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        
        re_queued_batch = []
        current_batch_verified = 0
        current_batch_permanent_failed = 0
        
        # Process results
        for res, acc in results:
            email = acc.get("email")
            password = acc.get("password")
            retries = acc.get("retries", 0)
            
            if isinstance(res, dict) and res.get("accessToken") and res.get("user"):
                # SUCCESS: Save and count
                token = res["accessToken"]
                token_index = await set_token(user_id, token, res["user"].get("name", email), email, password)
                if token_index != -1:
                    await add_token_to_auto_batch(user_id, token_index)
                await set_user_filters(user_id, token, {"filterNationalityCode": ""}) # set default filter
                user_obj = res.get("user", {})
                user_obj.update({"email": email, "password": password, "token": token})
                await set_info_card(user_id, token, format_user_with_nationality(user_obj), email)
                
                # Crucial for pending logic: Remove from DB if successful
                await remove_pending_account(user_id, email) 
                
                verified_count += 1
                current_batch_verified += 1
            else:
                # FAILURE: Check if it's a permanent failure or needs re-queuing
                err = (res.get("errorMessage") or "").lower()
                
                # Check for common permanent errors (e.g., account blocked, bad password, max retries reached)
                is_permanent = ("password mismatch" in err) or ("invalid provider token" in err) or ("user not found" in err)
                
                # Use a specific retry limit for re-queuing logic (1 re-queue attempt in addition to worker's retries)
                if is_permanent or retries >= 1: 
                    # Permanent failure or max retries reached (1 extra retry on top of internal worker retries)
                    current_batch_permanent_failed += 1
                    permanent_failed_emails.append(f"• <code>{email}</code> (Error: Permanent Failure after {retries} retries: {err})")
                    # Remove permanently failed from DB pending list to prevent future attempts
                    await remove_pending_account(user_id, email) 
                else:
                    # Temporary failure (e.g., 'email not verified') - re-queue
                    re_queued_batch.append((email, password, retries + 1))

        # Re-queue failed accounts at the end of the accounts_remaining list
        accounts_remaining.extend(re_queued_batch)
        
        # --- Live Update 2: After Processing Batch ---
        progress_text = (
            f"<b>Batch {batch_number} Complete.</b> 🎉\n"
            f"Verified in Batch: {current_batch_verified}\n"
            f"Re-queued for Retry: {len(re_queued_batch)}\n"
            f"Permanently Failed: {current_batch_permanent_failed}\n"
            f"--- Progress ---\n"
            f"Verified Total: {verified_count}\n"
            f"Remaining for Next Batch: {len(accounts_remaining)}"
        )
        await msg_to_edit.edit_text(progress_text, parse_mode="HTML")

        # Delay for 1 minute before the next batch, but only if there are accounts left to process
        if accounts_remaining:
            await msg_to_edit.edit_text(
                f"{progress_text}\n\n"
                f"Pausing for **{BATCH_DELAY_SECONDS} seconds** before continuing... 😴",
                parse_mode="HTML"
            )
            await asyncio.sleep(BATCH_DELAY_SECONDS)

            
    await session.close()
    
    # --- Final Result Display ---
    result_summary = f"<b>✅ Sign In Complete!</b>\n\n<b>Total Accounts Logged In:</b> {verified_count} of {total_accounts}"
    if permanent_failed_emails:
        result_summary += f"\n<b>Permanently Failed:</b> {len(permanent_failed_emails)}"

    await msg_to_edit.edit_text(result_summary, reply_markup=SIGNUP_MENU, parse_mode="HTML")

    if permanent_failed_emails:
        details_text = "<b>Detailed Permanently Failed Accounts:</b>\n" + '\n'.join(permanent_failed_emails)
        # Split details into chunks if needed
        for i in range(0, len(details_text), 4000):
            await message.answer(details_text[i:i+4000], parse_mode="HTML")
# -------------------------
# Callback handler
# -------------------------
async def signup_callback_handler(callback: CallbackQuery) -> bool:
    user_id = callback.from_user.id
    state = user_signup_states.get(user_id, {})
    data = callback.data

    # ---------- Settings ----------
    if data == "signup_settings":
        await signup_settings_command(callback.message, is_callback=True)
        await callback.answer()
        return True

    if data == "toggle_auto_signup":
        cfg = await get_signup_config(user_id) or {}
        cfg['auto_signup'] = not cfg.get('auto_signup', False)
        await set_signup_config(user_id, cfg)
        await callback.answer(f"Auto Signup turned {'ON' if cfg['auto_signup'] else 'OFF'}")
        await signup_settings_command(callback.message, is_callback=True)
        return True

    if data == "setup_signup_config":
        state["stage"] = "config_email"
        user_signup_states[user_id] = state
        await callback.message.edit_text(
            "<b>Setup Email</b>\n\nEnter your base Gmail address (e.g., yourname@gmail.com). This will be used to generate dot variations for multiple accounts.",
            reply_markup=BACK_TO_CONFIG, parse_mode="HTML"
        )
        await callback.answer()
        return True

    # ---------- Start signup flow ----------
    if data == "signup_go":
        cfg = await get_signup_config(user_id) or {}
        auto_signup = cfg.get("auto_signup", False)

        if not auto_signup:
            # Manual mode: just ask for email, rest comes from config
            if not all(k in cfg for k in ['password', 'gender', 'birth_year', 'nationality']):
                await callback.message.edit_text(
                    "<b>Configuration Incomplete</b>\n\nPlease set up password, gender, birth year and nationality in <b>Signup Config</b> first.",
                    reply_markup=SIGNUP_MENU, parse_mode="HTML"
                )
                await callback.answer()
                return True
            state["stage"] = "ask_num_accounts"
            state["manual_mode"] = True
            user_signup_states[user_id] = state
            await callback.message.edit_text(
                "<b>Manual Sign Up</b>\n\nEnter the number of accounts to create (1-100):",
                reply_markup=BACK_TO_SIGNUP, parse_mode="HTML"
            )
            await callback.answer()
            return True

        # Auto signup mode: require full config
        if not all(k in cfg for k in ['email', 'password', 'gender', 'birth_year', 'nationality']):
            await callback.message.edit_text(
                "<b>Configuration Incomplete</b>\n\nPlease set up all details in <b>Signup Config</b> first.",
                reply_markup=SIGNUP_MENU, parse_mode="HTML"
            )
            await callback.answer()
            return True

        state["stage"] = "ask_num_accounts"
        user_signup_states[user_id] = state
        await callback.message.edit_text(
            "<b>Account Creation</b>\n\nEnter the number of accounts to create (1-100):",
            reply_markup=BACK_TO_SIGNUP, parse_mode="HTML"
        )
        await callback.answer()
        return True

    if data == "signup_photos_done":
        state["stage"] = "ask_filter_nationality"
        user_signup_states[user_id] = state
        await callback.message.edit_text(
            "<b>Select Filter Nationality</b>\n\nChoose the nationality filter for requests:",
            reply_markup=FILTER_NATIONALITY_KB, parse_mode="HTML"
        )
        await callback.answer()
        return True

    if data.startswith("signup_filter_nationality_"):
        code = data.split("_")[-1] if len(data.split("_")) > 3 else ""
        state["filter_nationality"] = code if code != "all" else ""
        await show_signup_preview(callback.message, user_id, state)
        await callback.answer()
        return True

    # ---------- Create accounts (preview confirmed) ----------
    if data == "create_accounts_confirm":
        await callback.message.edit_text("<b>Creating Accounts Concurrently...</b>", parse_mode="HTML")
        cfg = await get_signup_config(user_id) or {}
        num_accounts = state.get("num_accounts", 1)
        manual_mode = state.get("manual_mode", False)
        used_emails = set(cfg.get("used_emails", []))

        if manual_mode:
            selected_emails = [state.get("manual_email")]
        else:
            selected_emails = state.get("selected_emails", []) or []

        if not selected_emails or not selected_emails[0]:
            await callback.message.edit_text(
                "<b>No Available Emails</b>\n\nNo valid email variations found. Please try a different base email in Signup Config.",
                reply_markup=SIGNUP_MENU, parse_mode="HTML"
            )
            await callback.answer()
            return True

        signup_tasks = []
        accounts_to_create = []
        for email in selected_emails[:num_accounts]:
            acc = {
                "email": email,
                "password": cfg.get("password"),
                "name": state.get('name', 'User'),
                "gender": cfg.get("gender"),
                "desc": get_random_bio(),
                "photos": state.get("photos", []),
                "birth_year": cfg.get("birth_year", 2000),
                "nationality": cfg.get("nationality", "US")
            }
            if email not in used_emails:
                signup_tasks.append(try_signup(acc, user_id))
                accounts_to_create.append(acc)

        results = await asyncio.gather(*signup_tasks)
        created_accounts = []
        for i, res in enumerate(results):
            acc = accounts_to_create[i]
            if isinstance(res, dict) and res.get("user", {}).get("_id"):
                created_accounts.append({"email": acc["email"], "name": acc["name"], "password": acc["password"]})
            elif isinstance(res, dict) and "already in use" in (res.get("errorMessage") or "").lower():
                used_emails.add(acc["email"])
            else:
                logger.error(f"Signup failed for {acc['email']}: {res}")

        if used_emails:
            cfg['used_emails'] = list(used_emails)
            await set_signup_config(user_id, cfg)

        state["created_accounts"] = created_accounts
        state["verified_accounts"] = []
        state["pending_accounts"] = created_accounts.copy()

        result_text = (
            f"<b>Account Creation Results</b>\n\n<b>Created:</b> {len(created_accounts)} account{'s' if len(created_accounts) != 1 else ''}\n\n"
        )
        if created_accounts:
            result_text += "<b>Created Accounts:</b>\n" + '\n'.join([f"• {a['name']} - <code>{a['email']}</code>" for a in created_accounts])

        if len(selected_emails) > len(created_accounts):
            result_text += "\n\n⚠️ Some emails were already in use and have been skipped for future runs."

        result_text += "\n\nPlease verify all emails in your mailbox, then either click Verify All Emails or Skip For Now to save them."

        await callback.message.edit_text(result_text, reply_markup=VERIFY_AND_SKIP_KB, parse_mode="HTML")
        user_signup_states[user_id] = state
        await callback.answer()
        return True

    # ---------- Verify pending accounts (MODIFIED to use do_multi_signin) ----------
    if data == "verify_accounts" or data == "retry_pending":
        pending_in_memory = state.get("pending_accounts", []) or []
        db_pending = await get_pending_accounts(user_id) or []
        
        # Combine unique accounts from in-memory state and database pending accounts
        all_accounts_to_process = []
        emails_in_list = set()
        
        for acc in pending_in_memory + db_pending:
            if acc["email"] not in emails_in_list:
                # Ensure the account dict has the minimum required info for signin
                if acc.get("email") and acc.get("password"):
                    all_accounts_to_process.append(acc)
                    emails_in_list.add(acc["email"])
        
        if not all_accounts_to_process:
            await callback.message.edit_text(
                "<b>No Pending Accounts</b>\n\nAll accounts are either verified or none were created.",
                reply_markup=SIGNUP_MENU, parse_mode="HTML"
            )
            await callback.answer()
            return True

        # Convert account dictionaries to the (email, password) format required by do_multi_signin
        accounts_to_login = [(acc["email"], acc["password"]) for acc in all_accounts_to_process]
        
        # --- Use the batch-processing function (do_multi_signin) ---
        await do_multi_signin(callback.message, user_id, accounts_to_login)

        # Clear the in-memory pending state as processing is now complete and results are displayed
        state["pending_accounts"] = [] 
        user_signup_states[user_id] = state

        await callback.answer("Verification started in batches. Check for progress updates.")
        return True

    # ---------- Skip pending (save pending accounts into DB, do not sign in) ----------
    if data == "skip_pending":
        pending = state.get("pending_accounts", []) or []
        if pending:
            await add_pending_accounts(user_id, pending)
            state["pending_accounts"] = []
            user_signup_states[user_id] = state
            await callback.message.edit_text(
                f"<b>Pending Accounts Saved!</b>\n\nSaved {len(pending)} accounts. You can login them later from the Signup menu.",
                reply_markup=SIGNUP_MENU, parse_mode="HTML"
            )
        else:
            await callback.message.edit_text("<b>No pending accounts to save.</b>", reply_markup=SIGNUP_MENU, parse_mode="HTML")
        await callback.answer()
        return True

    # ---------- Login pending accounts from DB (existing flow, correctly uses do_multi_signin) ----------
    if data == "login_pending":
        db_pending = await get_pending_accounts(user_id) or []
        if not db_pending:
            await callback.message.edit_text("<b>No Pending Accounts</b>", reply_markup=SIGNUP_MENU, parse_mode="HTML")
            await callback.answer()
            return True

        accounts_to_login = [(acc["email"], acc["password"]) for acc in db_pending]
        await do_multi_signin(callback.message, user_id, accounts_to_login)
        # do_multi_signin updates the pending DB and sends the final message.
        await callback.answer("Login started in batches. Check above for progress.")
        return True

    # ---------- UNIFIED Sign In button logic (replaces multi_signin_go) ----------
    if data == "signin_go":
        # Redirect to the stage that accepts single or multiple emails
        state["stage"] = "multi_signin_emails" 
        user_signup_states[user_id] = state
        await callback.message.edit_text(
            "<b>Sign In (Single or Multi)</b>\n\nEnter one email for single sign-in, or multiple emails (one per line) for batch sign-in:",
            reply_markup=BACK_TO_SIGNUP, parse_mode="HTML"
        )
        await callback.answer()
        return True
        
    # ---------- Simple menu / signin flow ----------
    if data == "signup_menu":
        state["stage"] = "menu"
        user_signup_states[user_id] = state
        
        pending = await get_pending_accounts(user_id)
        pending_count = len(pending) if pending else 0
        menu = [row[:] for row in SIGNUP_MENU.inline_keyboard]
        if pending_count > 0:
            menu.insert(0, [InlineKeyboardButton(text=f"Login Pending Accounts ({pending_count})", callback_data="login_pending")])
        
        await callback.message.edit_text("<b>Account Creation</b>\n\nChoose an option:", reply_markup=InlineKeyboardMarkup(inline_keyboard=menu), parse_mode="HTML")
        await callback.answer()
        return True

    # default
    await callback.answer()
    return False

# -------------------------
# Message handler for flow (text/photo)
# -------------------------
async def signup_message_handler(message: Message) -> bool:
    user_id = message.from_user.id
    if user_id not in user_signup_states:
        return False
    state = user_signup_states.get(user_id, {})
    stage = state.get("stage", "")
    text = message.text.strip() if message.text else ""

    # configuration flow
    if stage.startswith("config_"):
        cfg = await get_signup_config(user_id) or {}
        if stage == "config_email":
            if '@' not in text:
                await message.answer("Invalid Email. Please try again:", reply_markup=BACK_TO_CONFIG, parse_mode="HTML")
                return True
            cfg["email"] = text
            cfg["used_emails"] = []
            state["stage"] = "config_password"
            await message.answer("<b>Setup Password</b>\nEnter the password:", reply_markup=BACK_TO_CONFIG, parse_mode="HTML")
        elif stage == "config_password":
            cfg["password"] = text
            state["stage"] = "config_gender"
            await message.answer("<b>Setup Gender</b>\nEnter gender (M/F):", reply_markup=BACK_TO_CONFIG, parse_mode="HTML")
        elif stage == "config_gender":
            if text.upper() not in ("M", "F"):
                await message.answer("Invalid. Please enter M or F:", parse_mode="HTML")
                return True
            cfg["gender"] = text.upper()
            state["stage"] = "config_birth_year"
            await message.answer("<b>Setup Birth Year</b>\nEnter birth year (e.g., 2000):", reply_markup=BACK_TO_CONFIG, parse_mode="HTML")
        elif stage == "config_birth_year":
            try:
                year = int(text)
                if not 1950 <= year <= 2010:
                    raise ValueError()
                cfg["birth_year"] = year
                state["stage"] = "config_nationality"
                await message.answer("<b>Setup Nationality</b>\nEnter a 2-letter code (e.g., US, UK):", reply_markup=BACK_TO_CONFIG, parse_mode="HTML")
            except ValueError:
                await message.answer("Invalid Year (1950-2010). Please try again:", parse_mode="HTML")
                return True
        elif stage == "config_nationality":
            if len(text) != 2:
                await message.answer("Invalid. Please enter a 2-letter code:", parse_mode="HTML")
                return True
            cfg["nationality"] = text.upper()
            state["stage"] = "menu"
            await message.answer("<b>Configuration Saved!</b>", parse_mode="HTML")
            await signup_settings_command(message)
        await set_signup_config(user_id, cfg)
        user_signup_states[user_id] = state
        return True
    
    # ---------- Manual Sign Up — enter email after name, before photos ----------
    if stage == "manual_signup_email":
        email = text.strip()
        if "@" not in email or "." not in email:
            await message.answer("Invalid email. Please enter a valid email address:", reply_markup=BACK_TO_SIGNUP, parse_mode="HTML")
            return True
        state["manual_email"] = email
        state["stage"] = "ask_photos"
        state["photos"] = []
        state["last_photo_message_id"] = None
        user_signup_states[user_id] = state
        await message.answer("<b>Profile Photos</b>\n\nSend up to 6 photos. Click 'Done' when finished.", reply_markup=DONE_PHOTOS, parse_mode="HTML")
        return True

    # ---------- End Manual Sign Up stages ----------

    # ---------- UNIFIED Sign In email input (formerly multi_signin_emails) ----------
    if stage == "multi_signin_emails":
        emails = [e.strip() for e in text.split('\n') if e.strip() and '@' in e.strip()]
        if not emails:
            await message.answer("No valid emails found. Please enter emails, one per line:", reply_markup=BACK_TO_SIGNUP, parse_mode="HTML")
            return True
        
        if len(emails) == 1:
            # Single Sign In Flow
            state["signin_email"] = emails[0]
            state["stage"] = "signin_password" # Use the specific single-signin stage
            await message.answer("<b>Password</b>\nEnter your password:", reply_markup=BACK_TO_SIGNUP, parse_mode="HTML")
        else:
            # Multi Sign In Flow (uses shared password)
            state["multi_signin_emails"] = emails
            state["stage"] = "multi_signin_password"
            await message.answer(f"<b>{len(emails)} Emails received.</b>\n\nEnter the **single password** to use for all accounts:", reply_markup=BACK_TO_SIGNUP, parse_mode="HTML")
            
        user_signup_states[user_id] = state
        return True

    # ---------- Multi Sign In Password (Batch Sign In) ----------
    if stage == "multi_signin_password":
        password = text
        emails = state.get("multi_signin_emails", [])
        
        accounts_to_login = [(email, password) for email in emails]
        
        # Triggers the batch sign-in logic
        await do_multi_signin(message, user_id, accounts_to_login)

        state["stage"] = "menu"
        user_signup_states[user_id] = state
        return True
    
    # ---------- Single Sign In Password (Direct Sign In) ----------
    if stage == "signin_password":
        msg = await message.answer("<b>Signing In</b>...", parse_mode="HTML")
        email_to_sign_in = state.get("signin_email")
        
        res = await try_signin(email_to_sign_in, text, user_id)
        if res.get("accessToken") and res.get("user"):
            creds = {"email": email_to_sign_in, "password": text}
            await store_token_and_show_card(msg, res, creds)
        else:
            err = res.get("errorMessage", "Unknown error.")
            await msg.edit_text(f"<b>Sign In Failed</b>\n\nError: {err}", reply_markup=SIGNUP_MENU, parse_mode="HTML")
            
        state["stage"] = "menu"
        user_signup_states[user_id] = state
        return True
    # ---------- END UNIFIED Sign In message flow ----------

    # ask number of accounts
    if stage == "ask_num_accounts":
        try:
            num = int(text)
            if not 1 <= num <= 100:
                raise ValueError()
            state["num_accounts"] = num
            state["stage"] = "ask_name"
            user_signup_states[user_id] = state
            await message.answer("<b>Display Name</b>\nEnter the display name for the account(s):", reply_markup=BACK_TO_SIGNUP, parse_mode="HTML")
        except ValueError:
            await message.answer("Invalid number (1-100). Please try again:", parse_mode="HTML")
        return True

    # ask name
    if stage == "ask_name":
        state["name"] = text or "User"
        # if manual signup, ask for email next; otherwise go straight to photos
        if state.get("manual_mode"):
            state["stage"] = "manual_signup_email"
            user_signup_states[user_id] = state
            await message.answer("<b>Email</b>\nEnter the email address for this account:", reply_markup=BACK_TO_SIGNUP, parse_mode="HTML")
        else:
            state["stage"] = "ask_photos"
            state["photos"] = []
            state["last_photo_message_id"] = None
            user_signup_states[user_id] = state
            await message.answer("<b>Profile Photos</b>\n\nSend up to 6 photos. Click 'Done' when finished.", reply_markup=DONE_PHOTOS, parse_mode="HTML")
        return True

    # photo upload stage
    if stage == "ask_photos":
        if message.content_type != "photo":
            await message.answer("Please send a photo or click 'Done'.", reply_markup=DONE_PHOTOS, parse_mode="HTML")
            return True
        if len(state.get("photos", [])) >= 6:
            await message.answer("Photo limit reached (6). Click Done.", reply_markup=DONE_PHOTOS, parse_mode="HTML")
            return True
        photo_url = await upload_tg_photo(message)
        if photo_url:
            state.setdefault("photos", []).append(photo_url)
            # cleanup previous
            if state.get("last_photo_message_id"):
                try:
                    await message.bot.delete_message(chat_id=user_id, message_id=state["last_photo_message_id"])
                except Exception:
                    pass
            new_message = await message.answer(f"<b>Profile Photos</b>\n\nPhoto uploaded ({len(state['photos'])}/6). Send another or click 'Done'.", reply_markup=DONE_PHOTOS, parse_mode="HTML")
            state["last_photo_message_id"] = new_message.message_id
        else:
            await message.answer("Upload Failed. Please try again.", reply_markup=DONE_PHOTOS, parse_mode="HTML")
        user_signup_states[user_id] = state
        return True

    return False

# -------------------------
# helpers: upload + store
# -------------------------
# Note: upload_tg_photo and meeff_upload_image are dependent on bot token and meeff endpoints
# and are assumed to be implemented correctly elsewhere in the bot environment.

async def upload_tg_photo(message: Message) -> Optional[str]:
    # Placeholder for actual implementation: this needs message.bot.get_file, which is part of aiogram
    try:
        file = await message.bot.get_file(message.photo[-1].file_id)
        file_url = f"https://api.telegram.org/file/bot{message.bot.token}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    return None
                # Assuming meeff_upload_image is defined and working
                return await meeff_upload_image(await resp.read())
    except Exception as e:
        logger.error(f"Error uploading Telegram photo: {e}")
        return None

async def meeff_upload_image(img_bytes: bytes) -> Optional[str]:
    url = "https://api.meeff.com/api/upload/v1"
    payload = {"category": "profile", "count": 1, "locale": "en"}
    headers = {
        'User-Agent': "okhttp/5.0.0-alpha.14",
        'Accept-Encoding': "gzip",
        'Content-Type': "application/json; charset=utf-8"
    }
    try:
        async with create_meeff_session() as session:
            async with session.post(url, data=json.dumps(payload), headers=headers) as resp:
                resp_json = await resp.json()
                data = resp_json.get("data", {})
                upload_info = data.get("uploadImageInfoList", [{}])[0]
                upload_url = data.get("Host")
                if not (upload_info and upload_url):
                    return None
                fields = {
                    k: upload_info.get(k) or data.get(k)
                    for k in ["X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "Policy", "X-Amz-Signature"]
                }
                fields.update({
                    k: data.get(k)
                    for k in ["acl", "Content-Type", "x-amz-meta-uuid"]
                })
                fields["key"] = upload_info.get("key")
                if any(v is None for v in fields.values()):
                    return None
                form = aiohttp.FormData()
                for k, v in fields.items():
                    form.add_field(k, v)
                form.add_field('file', img_bytes, filename='photo.jpg', content_type='image/jpeg')
                async with session.post(upload_url, data=form) as s3resp:
                    return upload_info.get("uploadImagePath") if s3resp.status in (200, 204) else None
    except Exception as e:
        logger.error(f"Error uploading image to Meeff: {e}")
        return None

async def store_token_and_show_card(msg_obj: Message, login_result: Dict, creds: Dict) -> None:
    access_token = login_result.get("accessToken")
    user_data = login_result.get("user")
    if access_token and user_data:
        user_id = msg_obj.chat.id
        token_index = await set_token(user_id, access_token, user_data.get("name", creds.get("email")), creds.get("email"), creds.get("password"))
        if token_index != -1:
            await add_token_to_auto_batch(user_id, token_index)
        user_data.update({
            "email": creds.get("email"),
            "password": creds.get("password"),
            "token": access_token
        })
        text = format_user_with_nationality(user_data)
        await set_info_card(user_id, access_token, text, creds.get("email"))
        await msg_obj.edit_text("<b>Account Signed In & Saved!</b>\n\n" + text, parse_mode="HTML", disable_web_page_preview=True)
    else:
        error_msg = login_result.get("errorMessage", "Token or user data not received.")
        await msg_obj.edit_text(f"<b>Error</b>\n\nFailed to save account: {error_msg}", parse_mode="HTML")
