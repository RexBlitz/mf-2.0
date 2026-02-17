"""Automation Module - Continuous batches per account with live updates."""

import asyncio
import aiohttp
import logging
from typing import List, Dict

from db import (
    get_automation_settings, get_active_tokens, get_tokens, 
    get_user_filters, bulk_add_sent_ids, is_already_sent,
    get_blocked_users, add_automation_log, get_individual_spam_filter
)
from filters import apply_filter_for_account

logger = logging.getLogger(__name__)
automation_enabled = {}

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

# --- DISCOVERY & API HELPERS ---

async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[Dict]:
    """Discover users (Continuous Fetching Mode)."""
    url = "https://api.meeff.com/user/explore/v2/"
    headers = {**BASE_HEADERS, 'meeff-access-token': token}
    
    # Exact params from friend_requests.py
    params = {
        "lng": "71.9140141", 
        "unreachableUserIds": "", 
        "lat": "29.6264544", 
        "locale": "en"
    }
    
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
            if resp.status == 412: return "DISABLED"
            if resp.status != 200: return False
            data = await resp.json()
            cid = data.get("chatRoom", {}).get("_id")
            if not cid: return False
        
        # Send Message
        async with session.post("https://api.meeff.com/chat/send/v2", headers=headers, json={"chatRoomId": cid, "message": message, "locale": "en"}, timeout=10) as resp:
            return resp.status == 200
    except: return False

# --- LIVE STATUS UPDATER ---
async def update_live_status(status_msg, header, current_acc_info, total_sent, recent_log):
    """Updates Telegram message with live stats."""
    try:
        text = (
            f"{header}\n"
            f"📊 <b>Total Sent:</b> {total_sent}\n\n"
            f"{current_acc_info}\n"
            f"📝 {recent_log}"
        )
        await status_msg.edit_text(text, parse_mode="HTML")
    except Exception:
        pass

# --- CORE AUTOMATION FUNCTIONS ---

async def run_auto_requests(user_id: int, status_msg, token_list: List[Dict]):
    """1. Friend Request Automation (Continuous Batching)"""
    total_sent = 0
    blocked_users = await get_blocked_users(user_id)
    is_spam_on = await get_individual_spam_filter(user_id, "request")
    
    header = f"📨 <b>Request Auto Started</b>\nAccounts: {len(token_list)}"
    await update_live_status(status_msg, header, "🚀 Starting...", 0, "")

    async with aiohttp.ClientSession() as session:
        for idx, token_obj in enumerate(token_list, 1):
            if not is_automation_running(user_id): break
            
            token, name = token_obj["token"], token_obj.get("name", f"Acc {idx}")
            acc_sent = 0
            empty_batches = 0
            
            # Apply Filter
            await apply_filter_for_account(token, user_id)
            filters = await get_user_filters(user_id, token) or {}
            
            # === CONTINUOUS BATCH LOOP ===
            while True:
                if not is_automation_running(user_id): break
                
                # Update Status: Fetching
                acc_info = f"<b>{idx}/{len(token_list)} {name}</b>\n⚡ Requests Sent: {acc_sent}"
                await update_live_status(status_msg, header, acc_info, total_sent, "🔎 Fetching batch...")
                
                users = await _discover_users(session, token, filters)
                
                if not users:
                    empty_batches += 1
                    await update_live_status(status_msg, header, acc_info, total_sent, f"🔸 Empty batch {empty_batches}/5")
                    if empty_batches >= 5: break # Move to next account after 5 empty tries
                    await asyncio.sleep(2)
                    continue
                
                empty_batches = 0 # Reset if users found
                sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_on else set()
                ids_to_save = []
                limit_reached = False

                for user in users:
                    if not is_automation_running(user_id): break
                    
                    pid = user.get("_id")
                    if not pid or pid in blocked_users or pid in sent_ids: continue
                    
                    res = await _send_friend_request(session, token, pid)
                    
                    if res == "LIMIT": 
                        limit_reached = True
                        break
                    
                    if res == "OK":
                        acc_sent += 1
                        total_sent += 1
                        sent_ids.add(pid)
                        ids_to_save.append(pid)
                        
                        # LIVE UPDATE PER USER
                        acc_info = f"<b>{idx}/{len(token_list)} {name}</b>\n⚡ Requests Sent: {acc_sent}"
                        if acc_sent % 2 == 0: # Update every 2nd user to prevent flood limits
                            await update_live_status(status_msg, header, acc_info, total_sent, f"✅ Sent to ...{pid[-4:]}")
                        
                        await asyncio.sleep(1.5) # Speed control

                if is_spam_on and ids_to_save: await bulk_add_sent_ids(user_id, "request", ids_to_save)
                
                if limit_reached:
                    await update_live_status(status_msg, header, acc_info, total_sent, "⚠️ Limit Reached - Next Account")
                    await asyncio.sleep(2)
                    break 
                
                await asyncio.sleep(1) # Delay between batches

            # Log Account Completion
            if acc_sent > 0:
                await add_automation_log(user_id, f"[{name}] Requests: {acc_sent}")

    final = f"📨 <b>Request Summary</b>\nTotal Sent: {total_sent}\n✅ Done"
    await status_msg.edit_text(final, parse_mode="HTML")


async def run_auto_lounge(user_id: int, status_msg, token_list: List[Dict], message: str):
    """2. Lounge Automation (Continuous Batching)"""
    total_sent = 0
    blocked_users = await get_blocked_users(user_id)
    is_spam_on = await get_individual_spam_filter(user_id, "lounge")
    
    header = f"📢 <b>Lounge Auto Started</b>\nAccounts: {len(token_list)}"
    await update_live_status(status_msg, header, "🚀 Starting...", 0, "")

    async with aiohttp.ClientSession() as session:
        for idx, token_obj in enumerate(token_list, 1):
            if not is_automation_running(user_id): break
            
            token, name = token_obj["token"], token_obj.get("name", f"Acc {idx}")
            acc_sent = 0
            empty_batches = 0
            
            await apply_filter_for_account(token, user_id)
            filters = await get_user_filters(user_id, token) or {}

            while True:
                if not is_automation_running(user_id): break
                
                acc_info = f"<b>{idx}/{len(token_list)} {name}</b>\n⚡ Lounge Sent: {acc_sent}"
                await update_live_status(status_msg, header, acc_info, total_sent, "🔎 Fetching batch...")
                
                users = await _discover_users(session, token, filters)
                
                if not users:
                    empty_batches += 1
                    if empty_batches >= 3: break 
                    await asyncio.sleep(2)
                    continue

                sent_ids = await is_already_sent(user_id, "lounge", None, bulk=True) if is_spam_on else set()
                ids_to_save = []
                
                for user in users:
                    if not is_automation_running(user_id): break
                    
                    pid = user.get("_id")
                    if not pid or pid in blocked_users or pid in sent_ids: continue
                    
                    if await _send_lounge_msg(session, token, pid, message):
                        acc_sent += 1
                        total_sent += 1
                        sent_ids.add(pid)
                        ids_to_save.append(pid)
                        
                        acc_info = f"<b>{idx}/{len(token_list)} {name}</b>\n⚡ Lounge Sent: {acc_sent}"
                        if acc_sent % 2 == 0:
                            await update_live_status(status_msg, header, acc_info, total_sent, f"✅ Msg to ...{pid[-4:]}")
                        await asyncio.sleep(2.5)

                if is_spam_on and ids_to_save: await bulk_add_sent_ids(user_id, "lounge", ids_to_save)
                
                await asyncio.sleep(1)

            if acc_sent > 0: await add_automation_log(user_id, f"[{name}] Lounge: {acc_sent}")

    final = f"📢 <b>Lounge Summary</b>\nTotal Sent: {total_sent}\n✅ Done"
    await status_msg.edit_text(final, parse_mode="HTML")


async def run_auto_chat(user_id: int, status_msg, token_list: List[Dict], message: str):
    """3. Chatroom Automation (Continuous Batching)"""
    total_sent = 0
    blocked_users = await get_blocked_users(user_id)
    is_spam_on = await get_individual_spam_filter(user_id, "chatroom")
    
    header = f"💬 <b>Chatroom Auto Started</b>\nAccounts: {len(token_list)}"
    await update_live_status(status_msg, header, "🚀 Starting...", 0, "")

    async with aiohttp.ClientSession() as session:
        for idx, token_obj in enumerate(token_list, 1):
            if not is_automation_running(user_id): break
            
            token, name = token_obj["token"], token_obj.get("name", f"Acc {idx}")
            acc_sent = 0
            empty_batches = 0
            
            await apply_filter_for_account(token, user_id)
            filters = await get_user_filters(user_id, token) or {}

            while True:
                if not is_automation_running(user_id): break
                
                acc_info = f"<b>{idx}/{len(token_list)} {name}</b>\n⚡ Chat Sent: {acc_sent}"
                await update_live_status(status_msg, header, acc_info, total_sent, "🔎 Fetching batch...")
                
                users = await _discover_users(session, token, filters)
                
                if not users:
                    empty_batches += 1
                    if empty_batches >= 3: break 
                    await asyncio.sleep(2)
                    continue

                sent_ids = await is_already_sent(user_id, "chatroom", None, bulk=True) if is_spam_on else set()
                ids_to_save = []
                
                for user in users:
                    if not is_automation_running(user_id): break
                    pid = user.get("_id")
                    if not pid or pid in blocked_users or pid in sent_ids: continue
                    
                    res = await _send_chatroom_msg(session, token, pid, message)
                    if res == "DISABLED": continue
                    if res:
                        acc_sent += 1
                        total_sent += 1
                        sent_ids.add(pid)
                        ids_to_save.append(pid)
                        
                        acc_info = f"<b>{idx}/{len(token_list)} {name}</b>\n⚡ Chat Sent: {acc_sent}"
                        if acc_sent % 2 == 0:
                            await update_live_status(status_msg, header, acc_info, total_sent, f"✅ Msg to ...{pid[-4:]}")
                        await asyncio.sleep(2.5)

                if is_spam_on and ids_to_save: await bulk_add_sent_ids(user_id, "chatroom", ids_to_save)
                
                await asyncio.sleep(1)

            if acc_sent > 0: await add_automation_log(user_id, f"[{name}] Chat: {acc_sent}")

    final = f"💬 <b>Chatroom Summary</b>\nTotal Sent: {total_sent}\n✅ Done"
    await status_msg.edit_text(final, parse_mode="HTML")

# --- DISPATCHER ---

async def run_automation_action(user_id: int, status_msg, task_type: str = "request") -> None:
    try:
        settings = await get_automation_settings(user_id)
        selected = settings.get("selected_accounts", "all")
        if selected == "all": token_list = await get_tokens(user_id)
        elif selected == "active_only": token_list = await get_active_tokens(user_id)
        else:
            all_tokens = await get_tokens(user_id)
            token_list = [all_tokens[i] for i in selected if 0 <= i < len(all_tokens)]

        if not token_list:
            if status_msg: await status_msg.edit_text("❌ No accounts selected.", parse_mode="HTML")
            return

        if task_type == "request": await run_auto_requests(user_id, status_msg, token_list)
        elif task_type == "lounge":
            msg = settings.get("lounge_message")
            if not msg: return await status_msg.edit_text("❌ Lounge msg not set.", parse_mode="HTML")
            await run_auto_lounge(user_id, status_msg, token_list, msg)
        elif task_type == "chat":
            msg = settings.get("chatroom_message")
            if not msg: return await status_msg.edit_text("❌ Chat msg not set.", parse_mode="HTML")
            await run_auto_chat(user_id, status_msg, token_list, msg)
        elif task_type == "all": await run_auto_requests(user_id, status_msg, token_list)

    except Exception as e:
        logger.error(f"Auto Error: {e}")
        if status_msg: await status_msg.edit_text(f"❌ Error: {str(e)[:50]}", parse_mode="HTML")

# --- CONTROL HELPERS ---
def start_automation(user_id: int, bot): automation_enabled[user_id] = True
def stop_automation(user_id: int): automation_enabled[user_id] = False
def is_automation_running(user_id: int) -> bool: return automation_enabled.get(user_id, False)
