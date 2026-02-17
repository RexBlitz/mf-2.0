"""Automation Module - Fully integrated logic with Filters & Spam Checks."""

import asyncio
import aiohttp
import logging
from typing import List, Dict

# Import your existing logic to ensure consistency
from db import (
    get_automation_settings, get_active_tokens, get_tokens, 
    get_user_filters, bulk_add_sent_ids, is_already_sent,
    get_blocked_users, add_automation_log, get_individual_spam_filter
)
from filters import apply_filter_for_account  # CRITICAL: Applies Age/Gender settings

logger = logging.getLogger(__name__)
automation_enabled = {}

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

# --- DISCOVERY & API HELPERS ---

async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[Dict]:
    """Discover users (Identical to friend_requests.py)."""
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    
    # Exact params from friend_requests.py
    params = {"lng": "71.9140141", "unreachableUserIds": "", "lat": "29.6264544", "locale": "en"}
    
    # Apply Nationality Filter if set
    if filters and filters.get("filterNationalityCode"):
        params["filterNationalityCode"] = filters["filterNationalityCode"]
        
    try:
        async with session.get(url, headers=headers, params=params, timeout=10) as resp:
            if resp.status != 200: return []
            data = await resp.json()
            return data.get("users", [])
    except Exception: return []

async def _send_friend_request(session, token, person_id):
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            data = await resp.json()
            if data.get("errorCode") == "LikeExceeded": return "LIMIT"
            if data.get("errorCode"): return "FAIL"
            return "OK"
    except: return "FAIL"

async def _send_lounge_msg(session, token, person_id, message):
    url = "https://api.meeff.com/lounge/create/v1/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token, 'Content-Type': "application/json"}
    try:
        async with session.post(url, headers=headers, json={"targetUserId": person_id, "content": message, "locale": "en"}, timeout=10) as resp:
            data = await resp.json()
            return not data.get("errorCode")
    except: return False

async def _send_chatroom_msg(session, token, person_id, message):
    headers = {**BASE_HEADERS, 'meeff-access-token': token, 'Content-Type': "application/json"}
    try:
        # Open Room
        async with session.post("https://api.meeff.com/chatroom/open/v2", headers=headers, json={"waitingRoomId": person_id, "locale": "en"}, timeout=10) as resp:
            if resp.status == 412: return "DISABLED" # Chat disabled by user
            if resp.status != 200: return False
            data = await resp.json()
            cid = data.get("chatRoom", {}).get("_id")
            if not cid: return False
        
        # Send Message
        async with session.post("https://api.meeff.com/chat/send/v2", headers=headers, json={"chatRoomId": cid, "message": message, "locale": "en"}, timeout=10) as resp:
            return resp.status == 200
    except: return False

# --- CORE AUTOMATION FUNCTIONS ---

async def run_auto_requests(user_id: int, status_msg, token_list: List[Dict]):
    """1. Friend Request Automation (Matches friend_requests.py logic)"""
    total_sent = 0
    blocked_users = await get_blocked_users(user_id)
    
    # 1. SPAM FILTER CHECK
    is_spam_on = await get_individual_spam_filter(user_id, "request")
    
    status_text = f"📨 <b>Request Auto Started</b>\nAccounts: {len(token_list)}\n\n"
    if status_msg: await status_msg.edit_text(status_text, parse_mode="HTML")

    async with aiohttp.ClientSession() as session:
        for idx, token_obj in enumerate(token_list, 1):
            token, name = token_obj["token"], token_obj.get("name", f"Acc {idx}")
            
            # 2. APPLY FILTERS (Critical Step!)
            await apply_filter_for_account(token, user_id)
            filters = await get_user_filters(user_id, token) or {}
            
            if status_msg: await status_msg.edit_text(status_text + f"<b>{idx}/{len(token_list)} {name}</b>\n🔎 Discovering...", parse_mode="HTML")
            
            users = await _discover_users(session, token, filters)
            
            # Load Sent IDs if Spam Filter is ON
            sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_on else set()
            acc_sent = 0
            ids_to_save = []

            for user in users:
                pid = user.get("_id")
                if not pid or pid in blocked_users or pid in sent_ids: continue
                
                res = await _send_friend_request(session, token, pid)
                if res == "LIMIT": 
                    status_text += f"⚠️ <b>{name}:</b> Limit Reached\n"
                    break
                if res == "OK":
                    acc_sent += 1
                    total_sent += 1
                    sent_ids.add(pid) # In-memory update
                    ids_to_save.append(pid)
                    await asyncio.sleep(2)

            # 3. SAVE SPAM DATA
            if is_spam_on and ids_to_save: await bulk_add_sent_ids(user_id, "request", ids_to_save)
            
            if acc_sent > 0:
                await add_automation_log(user_id, f"[{name}] Requests: {acc_sent}")
                status_text += f"✅ <b>{name}:</b> {acc_sent} Sent\n"
            else:
                status_text += f"🔸 <b>{name}:</b> No new users\n"

    final = f"📨 <b>Request Summary</b>\nTotal Sent: {total_sent}\n\n{status_text}"
    if status_msg: await status_msg.edit_text(final, parse_mode="HTML")


async def run_auto_lounge(user_id: int, status_msg, token_list: List[Dict], message: str):
    """2. Lounge Automation (Matches lounge.py logic)"""
    total_sent = 0
    blocked_users = await get_blocked_users(user_id)
    
    # 1. SPAM FILTER CHECK
    is_spam_on = await get_individual_spam_filter(user_id, "lounge")
    
    status_text = f"📢 <b>Lounge Auto Started</b>\nAccounts: {len(token_list)}\n\n"
    if status_msg: await status_msg.edit_text(status_text, parse_mode="HTML")

    async with aiohttp.ClientSession() as session:
        for idx, token_obj in enumerate(token_list, 1):
            token, name = token_obj["token"], token_obj.get("name", f"Acc {idx}")
            
            # 2. APPLY FILTERS
            await apply_filter_for_account(token, user_id)
            filters = await get_user_filters(user_id, token) or {}
            
            if status_msg: await status_msg.edit_text(status_text + f"<b>{idx}/{len(token_list)} {name}</b>\n🔎 Discovering...", parse_mode="HTML")
            
            users = await _discover_users(session, token, filters)
            
            sent_ids = await is_already_sent(user_id, "lounge", None, bulk=True) if is_spam_on else set()
            acc_sent = 0
            ids_to_save = []
            
            for user in users:
                pid = user.get("_id")
                if not pid or pid in blocked_users or pid in sent_ids: continue
                
                if await _send_lounge_msg(session, token, pid, message):
                    acc_sent += 1
                    total_sent += 1
                    sent_ids.add(pid)
                    ids_to_save.append(pid)
                    await asyncio.sleep(3)

            # 3. SAVE SPAM DATA
            if is_spam_on and ids_to_save: await bulk_add_sent_ids(user_id, "lounge", ids_to_save)

            if acc_sent > 0:
                await add_automation_log(user_id, f"[{name}] Lounge: {acc_sent}")
                status_text += f"✅ <b>{name}:</b> {acc_sent} Sent\n"
            else:
                status_text += f"🔸 <b>{name}:</b> 0 Sent\n"

    final = f"📢 <b>Lounge Summary</b>\nTotal Sent: {total_sent}\n\n{status_text}"
    if status_msg: await status_msg.edit_text(final, parse_mode="HTML")


async def run_auto_chat(user_id: int, status_msg, token_list: List[Dict], message: str):
    """3. Chatroom Automation (Matches chatroom.py logic)"""
    total_sent = 0
    blocked_users = await get_blocked_users(user_id)
    
    # 1. SPAM FILTER CHECK
    is_spam_on = await get_individual_spam_filter(user_id, "chatroom")
    
    status_text = f"💬 <b>Chatroom Auto Started</b>\nAccounts: {len(token_list)}\n\n"
    if status_msg: await status_msg.edit_text(status_text, parse_mode="HTML")

    async with aiohttp.ClientSession() as session:
        for idx, token_obj in enumerate(token_list, 1):
            token, name = token_obj["token"], token_obj.get("name", f"Acc {idx}")
            
            # 2. APPLY FILTERS
            await apply_filter_for_account(token, user_id)
            filters = await get_user_filters(user_id, token) or {}
            
            if status_msg: await status_msg.edit_text(status_text + f"<b>{idx}/{len(token_list)} {name}</b>\n🔎 Discovering...", parse_mode="HTML")
            
            users = await _discover_users(session, token, filters)
            
            sent_ids = await is_already_sent(user_id, "chatroom", None, bulk=True) if is_spam_on else set()
            acc_sent = 0
            ids_to_save = []
            
            for user in users:
                pid = user.get("_id")
                if not pid or pid in blocked_users or pid in sent_ids: continue
                
                res = await _send_chatroom_msg(session, token, pid, message)
                
                if res == "DISABLED": continue # User blocked chat
                if res:
                    acc_sent += 1
                    total_sent += 1
                    sent_ids.add(pid)
                    ids_to_save.append(pid)
                    await asyncio.sleep(3)

            # 3. SAVE SPAM DATA
            if is_spam_on and ids_to_save: await bulk_add_sent_ids(user_id, "chatroom", ids_to_save)

            if acc_sent > 0:
                await add_automation_log(user_id, f"[{name}] Chat: {acc_sent}")
                status_text += f"✅ <b>{name}:</b> {acc_sent} Sent\n"
            else:
                status_text += f"🔸 <b>{name}:</b> 0 Sent\n"

    final = f"💬 <b>Chatroom Summary</b>\nTotal Sent: {total_sent}\n\n{status_text}"
    if status_msg: await status_msg.edit_text(final, parse_mode="HTML")


# --- DISPATCHER ---

async def run_automation_action(user_id: int, status_msg, task_type: str = "request") -> None:
    """Dispatcher to run specific automation tasks."""
    try:
        settings = await get_automation_settings(user_id)
        
        # Select Accounts
        selected = settings.get("selected_accounts", "all")
        if selected == "all":
            token_list = await get_tokens(user_id)
        elif selected == "active_only":
            token_list = await get_active_tokens(user_id)
        else:
            all_tokens = await get_tokens(user_id)
            token_list = [all_tokens[i] for i in selected if 0 <= i < len(all_tokens)]

        if not token_list:
            if status_msg: await status_msg.edit_text("❌ No accounts selected.", parse_mode="HTML")
            return

        # Execute Task
        if task_type == "request":
            await run_auto_requests(user_id, status_msg, token_list)
            
        elif task_type == "lounge":
            msg = settings.get("lounge_message")
            if not msg:
                if status_msg: await status_msg.edit_text("❌ Lounge message not set.", parse_mode="HTML")
                return
            await run_auto_lounge(user_id, status_msg, token_list, msg)
            
        elif task_type == "chat":
            msg = settings.get("chatroom_message")
            if not msg:
                if status_msg: await status_msg.edit_text("❌ Chatroom message not set.", parse_mode="HTML")
                return
            await run_auto_chat(user_id, status_msg, token_list, msg)
        
        # If "all", default to request for now (or implement sequence if needed)
        elif task_type == "all":
             await run_auto_requests(user_id, status_msg, token_list)

    except Exception as e:
        logger.error(f"Auto Error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ Error: {str(e)[:50]}", parse_mode="HTML")

# --- CONTROL HELPERS ---
def start_automation(user_id: int, bot): automation_enabled[user_id] = True
def stop_automation(user_id: int): automation_enabled[user_id] = False
def is_automation_running(user_id: int) -> bool: return automation_enabled.get(user_id, False)
