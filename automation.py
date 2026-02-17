import asyncio
import logging
from datetime import datetime

# --- IMPORT BOTH SINGLE AND PARALLEL FUNCTIONS ---
# Single Functions
from friend_requests import run_requests
from lounge import send_lounge
from chatroom import send_message_to_everyone

# Parallel Functions
from friend_requests import process_all_tokens, user_states
from lounge import send_lounge_all_tokens
from chatroom import send_message_to_everyone_all_tokens

# DB & Utils
from db import (
    get_automation_settings, get_active_tokens, get_current_account,
    get_individual_spam_filter, get_automation_pending_followups, 
    set_automation_last_request_time, add_automation_log
)

logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
WAVES = [
    ("wave_1", True,  False, 15,  59),   # 15-59 min: Lounge
    ("wave_2", False, True,  16,  60),   # 16-60 min: Chatroom
    ("wave_3", True,  False, 60,  299),  # 1-5 hr: Lounge
    ("wave_4", True,  True,  300, 1440), # 5 hr+: Dono
]

# --- DUMMY MESSAGE CLASS ---
class SilentMessage:
    def __init__(self, chat_id, bot): 
        self.message_id = 0
        self.chat = type('obj', (object,), {'id': chat_id})
        self.bot = bot
    async def edit_text(self, text, parse_mode=None, reply_markup=None): pass

# =============================================================================
# MAIN MONITOR LOOP
# =============================================================================
async def monitor_loop(user_id, bot):
    logger.info(f"Automation Monitor Started for {user_id}")
    waves_triggered = set() 

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"): break
            
            selected_mode = settings.get("selected_accounts", "all")
            db_data = await get_automation_pending_followups(user_id)
            
            # =================================================================
            # LOGIC 1: SINGLE ACCOUNT (Current)
            # =================================================================
            if selected_mode == "current":
                token = await get_current_account(user_id)
                if not token:
                    await asyncio.sleep(60)
                    continue

                # --- 1A. Check Request Gate (Single) ---
                last_req = db_data.get("request_times", {}).get(token)
                should_run = False
                if not last_req: should_run = True
                elif isinstance(last_req, str): last_req = datetime.fromisoformat(last_req)
                if last_req and (datetime.utcnow() - last_req).total_seconds() > 86400: should_run = True
                
                if should_run:
                    # Trigger Single Function
                    asyncio.create_task(run_requests(user_id, bot, -100))
                    await set_automation_last_request_time(user_id, token)
                    await add_automation_log(user_id, "Single Requests Triggered")
                    waves_triggered.clear()
                    continue

                # --- 1B. Check Waves (Single) ---
                if not last_req: continue
                elapsed = (datetime.utcnow() - last_req).total_seconds() / 60
                
                for wave, do_lng, do_chat, min_m, max_m in WAVES:
                    if min_m <= elapsed < max_m and wave not in waves_triggered:
                        dummy = SilentMessage(user_id, bot)
                        
                        if do_lng and settings.get("lounge_message"):
                            spam = await get_individual_spam_filter(user_id, "lounge")
                            asyncio.create_task(send_lounge(token, settings["lounge_message"], dummy, bot, user_id, spam, user_id))
                            await add_automation_log(user_id, f"Single Lounge ({wave})")
                            
                        if do_chat and settings.get("chatroom_message"):
                            spam = await get_individual_spam_filter(user_id, "chatroom")
                            # Setup for single chatroom
                            sent_ids = set() # Or fetch from DB if needed
                            lock = asyncio.Lock()
                            asyncio.create_task(send_message_to_everyone(token, settings["chatroom_message"], user_id, spam, user_id, sent_ids, lock))
                            await add_automation_log(user_id, f"Single Chatroom ({wave})")
                            
                        waves_triggered.add(wave)

            # =================================================================
            # LOGIC 2: PARALLEL ACCOUNTS (Active/All)
            # =================================================================
            else: 
                # (Active Only, All, or Manual list - treating as Parallel)
                active_tokens = await get_active_tokens(user_id)
                if not active_tokens:
                    await asyncio.sleep(60)
                    continue
                    
                # Setup lists for Parallel Functions
                token_strs = [t["token"] for t in active_tokens]
                token_names = {t["token"]: t["name"] for t in active_tokens}
                
                # --- 2A. Check Request Gate (Parallel) ---
                # Check time of FIRST token to decide for ALL (Sync approach)
                first_token = active_tokens[0]["token"]
                last_req = db_data.get("request_times", {}).get(first_token)
                
                should_run = False
                if not last_req: should_run = True
                elif isinstance(last_req, str): last_req = datetime.fromisoformat(last_req)
                if last_req and (datetime.utcnow() - last_req).total_seconds() > 86400: should_run = True

                if should_run:
                    dummy = SilentMessage(user_id, bot)
                    # Trigger Parallel Function
                    asyncio.create_task(process_all_tokens(user_id, active_tokens, bot, -100, dummy))
                    
                    for t in active_tokens:
                        await set_automation_last_request_time(user_id, t["token"])
                    
                    await add_automation_log(user_id, "Parallel Requests Triggered")
                    waves_triggered.clear()
                    continue

                # --- 2B. Check Waves (Parallel) ---
                if not last_req: continue
                elapsed = (datetime.utcnow() - last_req).total_seconds() / 60
                
                for wave, do_lng, do_chat, min_m, max_m in WAVES:
                    if min_m <= elapsed < max_m and wave not in waves_triggered:
                        dummy = SilentMessage(user_id, bot)
                        
                        if do_lng and settings.get("lounge_message"):
                            spam = await get_individual_spam_filter(user_id, "lounge")
                            asyncio.create_task(send_lounge_all_tokens(active_tokens, settings["lounge_message"], dummy, bot, user_id, spam, user_id))
                            await add_automation_log(user_id, f"Parallel Lounge ({wave})")
                            
                        if do_chat and settings.get("chatroom_message"):
                            spam = await get_individual_spam_filter(user_id, "chatroom")
                            asyncio.create_task(send_message_to_everyone_all_tokens(token_strs, settings["chatroom_message"], dummy, bot, user_id, spam, token_names, True, user_id))
                            await add_automation_log(user_id, f"Parallel Chatroom ({wave})")
                            
                        waves_triggered.add(wave)

            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Automation Error: {e}")
            await asyncio.sleep(60)

# --- START/STOP ---
monitor_task = None

def start_automation(user_id, bot):
    global monitor_task
    if monitor_task and not monitor_task.done(): return
    monitor_task = asyncio.create_task(monitor_loop(user_id, bot))

def stop_automation(user_id):
    global monitor_task
    # Stop flags for both single and parallel functions
    if user_id in user_states:
        user_states[user_id]["running"] = False
    if monitor_task: monitor_task.cancel()
