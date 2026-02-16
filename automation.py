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

BASE_HEADERS = {
    'User-Agent': "okhttp/5.1.0",
    'Accept-Encoding': "gzip",
}


async def _discover_users(session: aiohttp.ClientSession, token: str, filters: dict = None) -> List[Dict]:
    """Discover users respecting nationality filter."""
    url = "https://api.meeff.com/user/explore/v2/"
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    params = {"locale": "en"}
    
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


async def _send_friend_request(session: aiohttp.ClientSession, token: str, person_id: str) -> bool:
    """Send friend request."""
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    headers = BASE_HEADERS.copy()
    headers['meeff-access-token'] = token
    
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            data = await resp.json()
            if data.get("errorCode") == "LikeExceeded":
                return False
            if data.get("errorCode"):
                return False
            return True
    except Exception:
        return False


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
        payload_open = {"waitingRoomId": person_id, "locale": "en"}
        async with session.post(url_open, headers=headers, json=payload_open, timeout=10) as resp:
            if resp.status == 412:
                return False
            if resp.status != 200:
                return False
            data = await resp.json()
            chatroom_id = data.get("chatRoom", {}).get("_id")
            if not chatroom_id:
                return False
        
        payload_send = {"chatRoomId": chatroom_id, "message": message, "locale": "en"}
        async with session.post(url_send, headers=headers, json=payload_send, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


async def run_automation_action(user_id: int, status_msg) -> None:
    """Run automation action immediately: requests + lounge + chatroom."""
    try:
        settings = await get_automation_settings(user_id)
        lounge_msg = settings.get("lounge_message", "")
        chatroom_msg = settings.get("chatroom_message", "")
        
        if not lounge_msg or not chatroom_msg:
            return
        
        selected = settings.get("selected_accounts", "all")
        if selected == "all":
            token_list = await get_tokens(user_id)
        elif selected == "active_only":
            token_list = await get_active_tokens(user_id)
        else:
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
            return
        
        blocked_users = await get_blocked_users(user_id)
        status_text = f"<b>Automation Cycle Started</b>\nProcessing {len(token_list)} account(s)...\n\n"
        
        if status_msg:
            try:
                await status_msg.edit_text(status_text, parse_mode="HTML")
            except:
                pass
        
        total_sent = 0
        async with aiohttp.ClientSession() as session:
            for idx, token_obj in enumerate(token_list, 1):
                token = token_obj["token"]
                token_name = token_obj.get("name", "Unknown")
                filters = await get_user_filters(user_id, token) or {}
                filter_nat = filters.get("filterNationalityCode", "")
                filter_txt = f" (Filter: {filter_nat})" if filter_nat else " (All)"
                
                status_text += f"<b>[{idx}/{len(token_list)}] {token_name}</b>\n"
                status_text += f"Discovering users{filter_txt}...\n"
                
                if status_msg:
                    try:
                        await status_msg.edit_text(status_text, parse_mode="HTML")
                    except:
                        pass
                
                is_spam_enabled = await get_individual_spam_filter(user_id, "request")
                sent_ids = await is_already_sent(user_id, "request", None, bulk=True) if is_spam_enabled else set()
                
                users = await _discover_users(session, token, filters)
                if not users:
                    status_text += f"❌ No users found\n\n"
                    if status_msg:
                        try:
                            await status_msg.edit_text(status_text, parse_mode="HTML")
                        except:
                            pass
                    continue
                
                status_text += f"✓ Found {len(users)} user(s)\n"
                if status_msg:
                    try:
                        await status_msg.edit_text(status_text, parse_mode="HTML")
                    except:
                        pass
                
                sent_count = 0
                ids_to_save = []
                
                for user in users:
                    person_id = user.get("_id")
                    if not person_id or person_id in blocked_users or person_id in sent_ids:
                        continue
                    
                    success = await _send_friend_request(session, token, person_id)
                    if not success:
                        break
                    
                    sent_ids.add(person_id)
                    sent_count += 1
                    total_sent += 1
                    ids_to_save.append(person_id)
                    
                    await asyncio.sleep(5)
                    ok_lounge = await _send_lounge_msg(session, token, person_id, lounge_msg)
                    if ok_lounge:
                        await add_automation_log(user_id, f"[{token_name}] Lounge → {person_id[:8]}...")
                        await asyncio.sleep(5)
                        ok_chat = await _send_chatroom_msg(session, token, person_id, chatroom_msg)
                        if ok_chat:
                            await add_automation_log(user_id, f"[{token_name}] Chat → {person_id[:8]}...")
                    
                    await asyncio.sleep(1)
                
                if is_spam_enabled and ids_to_save:
                    await bulk_add_sent_ids(user_id, "request", ids_to_save)
                
                status_text += f"Sent: {sent_count} requests\n\n"
                if status_msg:
                    try:
                        await status_msg.edit_text(status_text, parse_mode="HTML")
                    except:
                        pass
                
                if sent_count > 0:
                    await add_automation_log(user_id, f"[{token_name}] Cycle: {sent_count} requests")
                
                await asyncio.sleep(2)
        
        status_text += f"\n<b>✓ Complete</b>\nTotal Requests: <b>{total_sent}</b>"
        if status_msg:
            try:
                await status_msg.edit_text(status_text, parse_mode="HTML")
            except:
                pass
    
    except Exception as e:
        logger.error(f"Automation error: {e}")
        if status_msg:
            try:
                await status_msg.edit_text(f"<b>❌ Error:</b> {str(e)[:60]}", parse_mode="HTML")
            except:
                pass


def start_automation(user_id: int, bot):
    """Mark automation as enabled."""
    automation_enabled[user_id] = True


def stop_automation(user_id: int):
    """Mark automation as disabled."""
    automation_enabled[user_id] = False


def is_automation_running(user_id: int) -> bool:
    """Check if automation is enabled."""
    return automation_enabled.get(user_id, False)
