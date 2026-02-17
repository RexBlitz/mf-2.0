import asyncio
import logging
from datetime import datetime

# --- IMPORT ORIGINAL PARALLEL FUNCTIONS ---
from friend_requests import process_all_tokens, user_states
from lounge import send_lounge_all_tokens
from chatroom import send_message_to_everyone_all_tokens
from db import (
    get_automation_settings, get_active_tokens, 
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


class SilentMessage:
    def __init__(self, chat_id, bot): 
        self.message_id = 0
        self.chat = type('obj', (object,), {'id': chat_id})
        self.bot = bot
    async def edit_text(self, text, parse_mode=None, reply_markup=None): 
        # Log to console instead of crashing
        pass

# =============================================================================
# MAIN MONITOR LOOP
# =============================================================================
async def monitor_loop(user_id, bot):
    logger.info(f"AIO Automation Monitor Started for {user_id}")
    
    waves_triggered = set() 

    while True:
        try:
            settings = await get_automation_settings(user_id)
            if not settings.get("enabled"): break

            # Active tokens nikalo
            active_tokens = await get_active_tokens(user_id)
            if not active_tokens:
                await asyncio.sleep(60)
                continue


            token_strs = [t["token"] for t in active_tokens]
            token_names = {t["token"]: t["name"] for t in active_tokens}
          
            db_data = await get_automation_pending_followups(user_id)
            first_token = active_tokens[0]["token"]
            last_req = db_data.get("request_times", {}).get(first_token)
            
            should_run_req = False
            if not last_req:
                should_run_req = True
            elif isinstance(last_req, str):
                last_req = datetime.fromisoformat(last_req)
            
           
            if last_req and (datetime.utcnow() - last_req).total_seconds() > 86400:
                should_run_req = True

            if should_run_req:
                # --- TRIGGER ORIGINAL PARALLEL REQUESTS ---
                dummy_msg = SilentMessage(user_id, bot)

                asyncio.create_task(process_all_tokens(
                    user_id, active_tokens, bot, -100, dummy_msg
                ))
                

                for t in active_tokens:
                    await set_automation_last_request_time(user_id, t["token"])
                
                await add_automation_log(user_id, "AIO Requests Triggered")
                waves_triggered.clear() 
                continue

            # 2. CHECK WAVES (Lounge/Chatroom for ALL accounts)
            if not last_req: continue
            elapsed_mins = (datetime.utcnow() - last_req).total_seconds() / 60

            for wave_name, do_lng, do_chat, min_m, max_m in WAVES:
                if min_m <= elapsed_mins < max_m and wave_name not in waves_triggered:
                    
                    dummy_msg = SilentMessage(user_id, bot)

                    if do_lng and settings.get("lounge_message"):
                        # --- TRIGGER ORIGINAL PARALLEL LOUNGE ---
                        spam_enabled = await get_individual_spam_filter(user_id, "lounge")
                        asyncio.create_task(send_lounge_all_tokens(
                            active_tokens, settings["lounge_message"], dummy_msg, 
                            bot, user_id, spam_enabled, user_id
                        ))
                        await add_automation_log(user_id, f"AIO Lounge Triggered ({wave_name})")

                    if do_chat and settings.get("chatroom_message"):
                        # --- TRIGGER ORIGINAL PARALLEL CHATROOM ---
                        spam_enabled = await get_individual_spam_filter(user_id, "chatroom")
                        asyncio.create_task(send_message_to_everyone_all_tokens(
                            token_strs, settings["chatroom_message"], dummy_msg, 
                            bot, user_id, spam_enabled, token_names, True, user_id
                        ))
                        await add_automation_log(user_id, f"AIO Chatroom Triggered ({wave_name})")
                    
                    # Mark wave as done so it doesn't repeat every minute
                    waves_triggered.add(wave_name)

            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"AIO Monitor Error: {e}")
            await asyncio.sleep(60)

# --- START/STOP ---
monitor_task = None

def start_automation(user_id, bot):
    global monitor_task
    if monitor_task and not monitor_task.done(): return
    monitor_task = asyncio.create_task(monitor_loop(user_id, bot))

def stop_automation(user_id):
    global monitor_task
  
    if user_id in user_states:
        user_states[user_id]["running"] = False
    
    if monitor_task: monitor_task.cancel()
