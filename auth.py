from datetime import datetime, timedelta
from typing import Dict
import os

ADMIN_USER_IDS = {7405203657, 7725409374, 7691399254, 7795345443}
TEMP_PASSWORD = os.getenv("TEMP_PASSWORD")
password_access: Dict[int, datetime] = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def has_valid_access(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    expiry = password_access.get(user_id)
    return expiry is not None and expiry > datetime.now()


def grant_password_access(user_id: int, hours: int = 1) -> None:
    password_access[user_id] = datetime.now() + timedelta(hours=hours)
