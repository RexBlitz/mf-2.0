"""Automation Module - Run action immediately when triggered via /automation command."""

import asyncio
import aiohttp
import logging
from typing import List, Dict, Set
from db import (
    get_automation_settings, get_active_tokens, get_tokens, 
    get_user_filters, bulk_add_sent_ids, is_already_sent,
    get_blocked_users, add_automation_log, get_individual_spam_filter
)

logger = logging.getLogger(__name__)
automation_enabled = {}

# Standard headers to look like a real app user
BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}

async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[Dict]:
    """Discover users respecting nationality filter and location."""
    url = "https://api.meeff.com/user/explore/v2/"
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    
    # Added location params to match friend_requests.py behavior
    params = {
        "locale": "en",
        "lat": "29.6264544",
        "lng": "71.9140141"
    }
    
    if filters and filters.get("filterNationalityCode"):
        params["filterNationalityCode"] = filters["filterNationalityCode"]
    
    try:
        async with session.get(url, headers=headers, params=params, timeout=10) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("result", {}).get("users", [])
    except Exception as e:
        logger.error(f"Discover error: {e}")
        return []

async def _send_friend_request(session: aiohttp.ClientSession, token: str, person_id: str) -> str:
    """
    Send friend request.
    Returns: "OK", "LIMIT", or "FAIL"
    """
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            data = await resp.json()
            
            # Case 1: Daily Limit Reached -> STOP processing this account
            if data.get("errorCode") == "LikeExceeded":
                return "LIMIT"
            
            # Case 2: Other Errors (Already liked, User not found, etc) -> SKIP user
            if data.get("errorCode"):
                return "FAIL"
            
            # Case 3: Success
            return "OK"
            
    except Exception:
        return "FAIL"

async def _send_lounge_msg(session: aiohttp.ClientSession, token: str, person_id: str, message: str) -> bool:
    """Send lounge message."""
    url = "https://api.meeff.com/lounge/create/v1/"
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    headers['Content-Type'] = "application/json"
    payload = {"targetUserId": person_id, "content": message, "locale": "en"}
    
    try:
        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
            data = await resp.json()
            return not data.get("errorCode")
    except Exception:
        return False

async def _send_chatroom_msg(session: aiohttp.ClientSession, token: str, person_id: str, message: str) -> bool:
    """Send chatroom message (open + send)."""
    url_open = "https://api.meeff.com/chatroom/open/v2"
    url_send = "https://api.meeff.com/chat/send/v2"
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    headers['Content-Type'] = "application/json"
    
    try:
        # Step 1: Open the room
        payload_open = {"waitingRoomId": person_id, "locale": "en"}
        async with session.post(url_open, headers=headers, json=payload_open, timeout=10) as resp:
            if resp.status == 412: # Chat disabled by user
                return False
            if resp.status != 200:
                return False
            data = await resp.json()
            chatroom_id = data.get("chatRoom", {}).get("_id")
            if not chatroom_id:
                return False
        
        # Step 2: Send the message
        payload_send = {"chatRoomId": chatroom_id, "message": message, "locale": "en"}
        async with session.post(url_send, headers=headers, json=payload_send, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False

async def run_automation_action(user_id: int, status_msg) -> None:
    """
    Run automation action immediately: 
    1. Send Friend Request 
    2. Send Lounge Message
    3. Send Chatroom Message
    """
    try:
        settings = await get_automation_settings(user_id)
        lounge_msg = settings.get("lounge_message", "")
        chatroom_msg = settings.get("chatroom_message", "")
        
        # Check if messages are set
        if not lounge_msg or not chatroom_msg:
            if status_msg: await status_msg.edit_text("❌ <b>Automation Failed:</b> Messages not set in settings.", parse_mode="HTML")
            return
        
        # Select accounts based on settings
        selected = settings.get("selected_accounts", "all")
        if selected == "all":
            token_list = await get_tokens(user_id)
        elif selected == "active_only":
            token_list = await get_active_tokens(user_id)
        else:
            # Manual selection (list of indices)
            all_tokens = await get_tokens(user_id)
            token_list = []
            for idx in selected:
                try:
                    idx = int(idx) if not isinstance(idx, int) else idx
                    if 0 <= idx < len(all_tokens) and all_tokens[idx].get("active", True):
                        token_list.append(all_tokens[idx])
                except (ValueError, TypeError):
                    pass
        
        if not token_list:
            if status_msg: await status_msg.edit_text("❌ <b>Automation Failed:</b> No valid accounts selected.", parse_mode="HTML")
            return
        
        # Get blocked users to skip them
        blocked_users = await get_blocked_users(user_id)
        
        # Initial status
        status_text = f"🚀 <b>Automation Started</b>\nAccounts: {len(token_list)}\n\n"
        if status_msg:
            try: await status_msg.edit_text(status_text, parse_mode="HTML")
            except: pass
        
        total_requests_sent = 0
        total_lounge_sent = 0
        total_chat_sent = 0
        
        async with aiohttp.ClientSession() as session:
            for idx, token_obj in enumerate(token_list, 1):
                token = token_obj["token"]
                token_name = token_obj.get("name", f"Account {idx}")
                
                # Get filters for display
                filters = await get_user_filters(user_id, token) or {}
                filter_nat = filters.get("filterNationalityCode", "")
                filter_txt = f"[{filter_nat}]" if filter_nat else "[All]"
                
                # Update Status: Discovering
                current_status = status_text + f"<b>{idx}/{len(token_list)} {token_name} {filter_txt}</b>\n🔎 Discovering users..."
                if status_msg:
                    try: await status_msg.edit_text(current_status, parse_mode="HTML")
                    except: pass
                
                # Check spam filter settings
                is_spam_enabled = await get_individual_spam_filter(user_id, "request")
                sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_enabled else set()
                
                # Discover users
                users = await _discover_users(session, token, filters)
                
                if not users:
                    status_text += f"🔹 <b>{token_name}:</b> No users found.\n"
                    continue
                
                acc_req_count = 0
                ids_to_save = []
                
                # Update Status: Processing users
                current_status = status_text + f"<b>{idx}/{len(token_list)} {token_name}</b>\nProcessing {len(users)} users..."
                if status_msg:
                    try: await status_msg.edit_text(current_status, parse_mode="HTML")
                    except: pass
                
                for user in users:
                    person_id = user.get("_id")
                    
                    # Validation
                    if not person_id or person_id in blocked_users or person_id in sent_ids:
                        continue
                    
                    # 1. Send Friend Request
                    req_status = await _send_friend_request(session, token, person_id)
                    
                    if req_status == "LIMIT":
                        status_text += f"⚠️ <b>{token_name}:</b> Limit Reached.\n"
                        break # Stop this account, move to next
                    
                    if req_status == "FAIL":
                        continue # Skip user, try next
                    
                    # Request was successful ("OK")
                    sent_ids.add(person_id)
                    ids_to_save.append(person_id)
                    acc_req_count += 1
                    total_requests_sent += 1
                    
                    # Update Log
                    log_msg = f"Req Sent"
                    
                    # Wait a bit
                    await asyncio.sleep(2) 
                    
                    # 2. Send Lounge Message
                    ok_lounge = await _send_lounge_msg(session, token, person_id, lounge_msg)
                    if ok_lounge:
                        total_lounge_sent += 1
                        log_msg += " | Lounge OK"
                    
                    # Wait a bit
                    await asyncio.sleep(2)
                    
                    # 3. Send Chatroom Message
                    ok_chat = await _send_chatroom_msg(session, token, person_id, chatroom_msg)
                    if ok_chat:
                        total_chat_sent += 1
                        log_msg += " | Chat OK"

                    # Live Update for this specific action
                    temp_status = (
                        status_text + 
                        f"<b>{idx}/{len(token_list)} {token_name}</b>\n"
                        f"✅ Added: {acc_req_count}\n"
                        f"Action: {log_msg} ({person_id[-4:]})"
                    )
                    if status_msg:
                        try: await status_msg.edit_text(temp_status, parse_mode="HTML")
                        except: pass
                    
                    await asyncio.sleep(1) # Slight delay between users
                
                # Save to DB if spam filter is ON
                if is_spam_enabled and ids_to_save:
                    await bulk_add_sent_ids(user_id, "request", ids_to_save)
                
                # Log completion for this account
                await add_automation_log(user_id, f"[{token_name}] Sent {acc_req_count} requests")
                
                # Add to main status text history so it persists
                status_text += f"✅ <b>{token_name}:</b> Sent {acc_req_count}\n"
                
                await asyncio.sleep(2) # Delay between accounts
        
        # FINAL SUMMARY
        final_summary = (
            f"✅ <b>Automation Complete</b>\n\n"
            f"📨 Requests: {total_requests_sent}\n"
            f"📢 Lounge Msgs: {total_lounge_sent}\n"
            f"💬 Chat Msgs: {total_chat_sent}\n\n"
            f"{status_text}"
        )
        
        if status_msg:
            try: await status_msg.edit_text(final_summary, parse_mode="HTML")
            except: pass
    
    except Exception as e:
        logger.error(f"Automation error: {e}")
        if status_msg:
            try: await status_msg.edit_text(f"<b>❌ Critical Error:</b> {str(e)[:100]}", parse_mode="HTML")
            except: pass

# --- Helper Functions ---

def start_automation(user_id: int, bot):
    """Mark automation as enabled."""
    automation_enabled[user_id] = True

def stop_automation(user_id: int):
    """Mark automation as disabled."""
    automation_enabled[user_id] = False

def is_automation_running(user_id: int) -> bool:
    """Check if automation is enabled."""
    return automation_enabled.get(user_id, False)
