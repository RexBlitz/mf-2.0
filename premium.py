#!/usr/bin/env python3
"""
Meeff Utility & Diagnostics Module (v7.1.7 Protocol-Aligned):
- Account Status & Ruby Audit (/vip_audit)
- Daily Attendance Check & Claim (/claim_attendance)
- GPS Location Updater (/spoof_loc)
- Promo Code / Coupon Redeemer (/redeem_promo)
- Owned Item / Pass Activator (/use_item)
"""

import html
import logging
import re
from typing import Any, Dict, Optional

from aiogram import Router, types
from aiogram.filters import Command

from auth import has_valid_access
from db import get_info_card, get_tokens
from meeff_http import MEEFF_DEFAULT_LOCALE, build_meeff_headers, create_meeff_session

logger = logging.getLogger(__name__)
premium_router = Router()

# Real itemId strings from Meeff v7.1.7 MyItemActivity.getUnusedTitleId
KNOWN_ITEM_IDS = (
    "custom_location_1h_quota",
    "custom_location_1_quota",
    "history_1_quota",
    "facetalk_history_1_quota",
    "chat_upgrade_1_quota",
    "vibemeet",
    "visitor",
    "facetalkGenderFilter",
)


def _extract_meeff_user_id(token_obj: Dict[str, Any], info_card: Optional[str]) -> Optional[str]:
    if token_obj.get("meeff_user_id"):
        return str(token_obj["meeff_user_id"])
    if info_card:
        match = re.search(r"\b([a-f0-9]{24})\b", info_card)
        if match:
            return match.group(1)
    return None


async def audit_account_vip(
    token: str,
    user_id: Optional[int] = None,
    token_obj: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Inspect account shop state, expiring rubies, attendance status, and /user/info/v1 (if userId known)
    without rotating tokens.
    """
    get_headers = build_meeff_headers(token=token, json_body=False)
    post_headers = build_meeff_headers(token=token, json_body=True)

    info_card = await get_info_card(user_id, token) if user_id else None
    meeff_uid = _extract_meeff_user_id(token_obj or {}, info_card)

    async with create_meeff_session() as session:
        try:
            async with session.post(
                "https://api.meeff.com/shop/info/v1",
                json={"locale": MEEFF_DEFAULT_LOCALE},
                headers=post_headers,
                timeout=15,
            ) as resp:
                shop_data = await resp.json(content_type=None) if resp.status == 200 else {}
                if isinstance(shop_data, dict) and shop_data.get("errorCode") == "AuthRequired":
                    return {"success": False, "error": "AuthRequired (token expired)"}

            async with session.get(
                "https://api.meeff.com/user/ruby/list/expiring/v1",
                params={"locale": MEEFF_DEFAULT_LOCALE},
                headers=get_headers,
                timeout=15,
            ) as resp:
                ruby_data = await resp.json(content_type=None) if resp.status == 200 else {}

            async with session.get(
                "https://api.meeff.com/event/attendance/v2",
                params={"locale": MEEFF_DEFAULT_LOCALE},
                headers=get_headers,
                timeout=15,
            ) as resp:
                att_data = await resp.json(content_type=None) if resp.status == 200 else {}

            user_obj: Dict[str, Any] = {}
            if meeff_uid:
                async with session.get(
                    "https://api.meeff.com/user/info/v1",
                    params={"userId": meeff_uid, "locale": MEEFF_DEFAULT_LOCALE},
                    headers=get_headers,
                    timeout=15,
                ) as resp:
                    if resp.status == 200:
                        info_data = await resp.json(content_type=None) or {}
                        user_obj = info_data.get("user") or {}

            shop_user = shop_data.get("user") or shop_data.get("me") or {}
            merged = {**shop_user, **user_obj}

            ruby_total = (
                merged.get("ruby")
                if merged.get("ruby") is not None
                else shop_data.get("ruby", ruby_data.get("ruby", "N/A"))
            )
            has_sub = bool(merged.get("hasActiveSubscription") or shop_data.get("hasActiveSubscription"))
            is_vip = bool(merged.get("isVipUser") or shop_data.get("isVipUser"))
            sub_item = merged.get("subscriptionItemId") or shop_data.get("subscriptionItemId") or "None"
            valid_until = merged.get("subscriptionValidUntil") or shop_data.get("subscriptionValidUntil") or "N/A"
            att_count = att_data.get("attendanceCount") or att_data.get("streak") or att_data.get("day")

            return {
                "success": True,
                "name": merged.get("name") or (token_obj or {}).get("name", "Unknown"),
                "nationality": merged.get("nationalityCode", "??"),
                "has_sub": has_sub,
                "sub_item": sub_item,
                "valid_until": valid_until,
                "is_vip": is_vip,
                "ruby": ruby_total,
                "expiring_ruby": ruby_data.get("expiringRubyTotal", 0),
                "face_verified": merged.get("faceVerified"),
                "profile_score": merged.get("profileScore"),
                "attendance_streak": att_count,
                "shop_items_count": len(shop_data.get("shopItems", shop_data.get("items", []))),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


async def claim_attendance_for_token(token: str) -> Dict[str, Any]:
    """Check GET /event/attendance/v2 and submit POST /event/attendance/v2 per RestClient."""
    get_headers = build_meeff_headers(token=token, json_body=False)
    post_headers = build_meeff_headers(token=token, json_body=True)

    async with create_meeff_session() as session:
        try:
            async with session.get(
                "https://api.meeff.com/event/attendance/v2",
                params={"locale": MEEFF_DEFAULT_LOCALE},
                headers=get_headers,
                timeout=15,
            ) as resp:
                att_status = resp.status
                att_data = await resp.json(content_type=None) if resp.content_length != 0 else {}

            async with session.post(
                "https://api.meeff.com/event/attendance/v2",
                json={"locale": MEEFF_DEFAULT_LOCALE},
                headers=post_headers,
                timeout=15,
            ) as resp:
                claim_status = resp.status
                try:
                    claim_data = await resp.json(content_type=None) or {}
                except Exception:
                    claim_data = {}

            err_code = claim_data.get("errorCode") or (att_data.get("errorCode") if att_status != 200 else None)
            err_msg = claim_data.get("errorMessage") or (att_data.get("errorMessage") if att_status != 200 else None)
            ok = claim_status == 200 and not claim_data.get("errorCode")
            return {
                "success": ok,
                "status": claim_status,
                "error": err_msg or err_code or (f"HTTP {claim_status}" if not ok else None),
                "attendance": att_data,
                "claim": claim_data,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


async def update_location_for_token(
    token: str,
    lat: float,
    lng: float,
    is_custom_loc: bool = False,
) -> bool:
    """POST /user/updateLocation/v1 with numeric float lat/lng per RestClient.userUpdateLocation."""
    headers = build_meeff_headers(token=token, json_body=True)
    payload: Dict[str, Any] = {
        "lat": float(lat),
        "lng": float(lng),
        "locale": MEEFF_DEFAULT_LOCALE,
    }
    if is_custom_loc:
        payload["isCustomLoc"] = 1

    async with create_meeff_session() as session:
        try:
            async with session.post(
                "https://api.meeff.com/user/updateLocation/v1",
                json=payload,
                headers=headers,
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    return False
                try:
                    data = await resp.json(content_type=None) or {}
                except Exception:
                    return True
                return not data.get("errorCode")
        except Exception:
            return False


async def redeem_coupon_for_token(token: str, coupon_code: str) -> Dict[str, Any]:
    headers = build_meeff_headers(token=token, json_body=True)
    payload = {
        "couponCode": coupon_code.strip(),
        "locale": MEEFF_DEFAULT_LOCALE,
    }
    async with create_meeff_session() as session:
        try:
            async with session.post(
                "https://api.meeff.com/event/coupon/v1",
                json=payload,
                headers=headers,
                timeout=15,
            ) as resp:
                try:
                    data = await resp.json(content_type=None) or {}
                except Exception:
                    data = {}
                return {"status": resp.status, "body": data}
        except Exception as e:
            return {"status": 500, "body": {"errorMessage": str(e)}}


async def use_item_for_token(
    token: str,
    item_id: str,
    target_id: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /shop/useItem/v1 with required platform='android' per RestClient.userShopUseItem."""
    headers = build_meeff_headers(token=token, json_body=True)
    payload: Dict[str, Any] = {
        "itemId": item_id.strip(),
        "platform": "android",
        "locale": MEEFF_DEFAULT_LOCALE,
    }
    if target_id:
        payload["targetId"] = target_id.strip()

    async with create_meeff_session() as session:
        try:
            async with session.post(
                "https://api.meeff.com/shop/useItem/v1",
                json=payload,
                headers=headers,
                timeout=15,
            ) as resp:
                try:
                    data = await resp.json(content_type=None) or {}
                except Exception:
                    data = {}
                return {"status": resp.status, "body": data}
        except Exception as e:
            return {"status": 500, "body": {"errorMessage": str(e)}}


@premium_router.message(Command("vip_audit"))
async def vip_audit_command(message: types.Message):
    user_id = message.chat.id
    if not has_valid_access(user_id):
        return await message.reply("You are not authorized. Use /password to get access.")

    tokens = await get_tokens(user_id)
    if not tokens:
        return await message.reply("❌ No accounts found in your database.")

    status_msg = await message.reply(
        f"<b>👑 Running VIP &amp; Account Audit</b> for {len(tokens)} accounts...",
        parse_mode="HTML",
    )

    report_lines = ["<b>👑 Meeff Account &amp; Entitlement Audit</b>\n"]
    for i, t in enumerate(tokens):
        name = html.escape(t.get("name", f"Account #{i + 1}"))
        email = html.escape(t.get("email", "No email"))
        res = await audit_account_vip(t["token"], user_id=user_id, token_obj=t)
        if not res.get("success"):
            err = html.escape(str(res.get("error", "Unknown error")))
            report_lines.append(f"<b>{i + 1}. {name}</b> ({email})\n  ❌ Error: {err}\n")
            continue

        tier = "👑 VIP / Plus" if res["has_sub"] or res["is_vip"] else "⚪ Free"
        sub_info = (
            f" | Sub: {html.escape(str(res['sub_item']))} (Exp: {html.escape(str(res['valid_until']))})"
            if res["has_sub"]
            else ""
        )
        face_str = (
            "✅ Verified"
            if res["face_verified"] is True
            else "❌ Unverified"
            if res["face_verified"] is False
            else "N/A"
        )
        score_str = res["profile_score"] if res["profile_score"] is not None else "N/A"
        att_str = f" | Att Streak: {res['attendance_streak']}" if res.get("attendance_streak") is not None else ""

        report_lines.append(
            f"<b>{i + 1}. {name}</b> ({email})\n"
            f"  • Tier: <b>{tier}</b>{sub_info}\n"
            f"  • Rubies: 💎 {html.escape(str(res['ruby']))} (Expiring: {html.escape(str(res['expiring_ruby']))}){att_str}\n"
            f"  • Status: Face: {face_str} | Score: {html.escape(str(score_str))}\n"
        )

    full_text = "\n".join(report_lines)
    if len(full_text) > 4096:
        full_text = full_text[:4000] + "\n\n<i>[Report truncated due to length]</i>"

    await status_msg.edit_text(full_text, parse_mode="HTML")


@premium_router.message(Command("claim_attendance"))
async def claim_attendance_command(message: types.Message):
    user_id = message.chat.id
    if not has_valid_access(user_id):
        return await message.reply("You are not authorized. Use /password to get access.")

    tokens = await get_tokens(user_id)
    if not tokens:
        return await message.reply("❌ No accounts found.")

    status_msg = await message.reply(
        f"<b>🎁 Claiming Daily Attendance</b> for {len(tokens)} accounts...",
        parse_mode="HTML",
    )

    results = []
    success_count = 0
    for i, t in enumerate(tokens):
        name = html.escape(t.get("name", f"#{i + 1}"))
        res = await claim_attendance_for_token(t["token"])
        if res.get("success"):
            success_count += 1
            results.append(f"<b>{name}</b>: ✅ Claimed")
        else:
            err = html.escape(str(res.get("error") or "Failed"))
            results.append(f"<b>{name}</b>: ❌ {err}")

    report = "\n".join(results[:25])
    if len(results) > 25:
        report += f"\n<i>...and {len(results) - 25} more</i>"

    await status_msg.edit_text(
        f"<b>🎁 Attendance Claim Complete ({success_count}/{len(tokens)})</b>\n\n{report}",
        parse_mode="HTML",
    )


@premium_router.message(Command("spoof_loc"))
async def spoof_loc_command(message: types.Message):
    user_id = message.chat.id
    if not has_valid_access(user_id):
        return await message.reply("You are not authorized. Use /password to get access.")

    parts = message.text.split()
    if len(parts) not in (3, 4):
        return await message.reply(
            "<b>🌍 GPS Location Updater</b>\n\n"
            "Usage: <code>/spoof_loc &lt;lat&gt; &lt;lng&gt; [custom]</code>\n\n"
            "Presets:\n"
            "• Seoul (Korea): <code>/spoof_loc 37.5665 126.9780</code>\n"
            "• Tokyo (Japan): <code>/spoof_loc 35.6762 139.6503</code>\n"
            "• New York (USA): <code>/spoof_loc 40.7128 -74.0060</code>\n"
            "• Paris (France): <code>/spoof_loc 48.8566 2.3522</code>",
            parse_mode="HTML",
        )
    try:
        lat = float(parts[1])
        lng = float(parts[2])
    except ValueError:
        return await message.reply("❌ Invalid latitude or longitude.")

    is_custom = len(parts) == 4 and parts[3].lower() in ("1", "true", "custom")
    tokens = await get_tokens(user_id)
    if not tokens:
        return await message.reply("❌ No accounts found.")

    status_msg = await message.reply(
        f"<b>🌍 Updating location on {len(tokens)} accounts</b> to ({lat}, {lng})...",
        parse_mode="HTML",
    )

    success = 0
    for t in tokens:
        if await update_location_for_token(t["token"], lat, lng, is_custom_loc=is_custom):
            success += 1

    await status_msg.edit_text(
        f"<b>🌍 Location Update Complete!</b>\n\n"
        f"Updated {success}/{len(tokens)} accounts to <code>({lat}, {lng})</code>.",
        parse_mode="HTML",
    )


@premium_router.message(Command("redeem_promo"))
async def redeem_promo_command(message: types.Message):
    user_id = message.chat.id
    if not has_valid_access(user_id):
        return await message.reply("You are not authorized. Use /password to get access.")

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        return await message.reply(
            "<b>🎟️ Promo Code / Coupon Redeemer</b>\n\n"
            "Usage: <code>/redeem_promo &lt;coupon_code&gt;</code>\n"
            "Applies the promo/coupon code across all your accounts.",
            parse_mode="HTML",
        )
    code = parts[1].strip()
    escaped_code = html.escape(code)
    tokens = await get_tokens(user_id)
    if not tokens:
        return await message.reply("❌ No accounts found.")

    status_msg = await message.reply(
        f"<b>️ Redeeming promo code <code>{escaped_code}</code></b> across {len(tokens)} accounts...",
        parse_mode="HTML",
    )

    results = []
    for i, t in enumerate(tokens):
        name = html.escape(t.get("name", f"#{i + 1}"))
        res = await redeem_coupon_for_token(t["token"], code)
        status = res["status"]
        body = res["body"] if isinstance(res["body"], dict) else {}
        if status == 200 and not body.get("errorCode"):
            results.append(f"<b>{name}</b>: ✅ Success")
        else:
            err = html.escape(str(body.get("errorMessage") or body.get("errorCode") or f"HTTP {status}"))
            results.append(f"<b>{name}</b>: ❌ {err}")

    await status_msg.edit_text(
        f"<b>🎟️ Promo Redemption Report for <code>{escaped_code}</code>:</b>\n\n" + "\n".join(results),
        parse_mode="HTML",
    )


@premium_router.message(Command("use_item"))
async def use_item_command(message: types.Message):
    user_id = message.chat.id
    if not has_valid_access(user_id):
        return await message.reply("You are not authorized. Use /password to get access.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        sample_ids = ", ".join(f"<code>{x}</code>" for x in KNOWN_ITEM_IDS[:4])
        return await message.reply(
            "<b>🎒 Owned Item / Pass Activator</b>\n\n"
            "Usage: <code>/use_item &lt;itemId&gt; [targetId]</code>\n"
            f"Real v7.1.7 IDs: {sample_ids}\n"
            "Activates an owned inventory item across all your accounts.",
            parse_mode="HTML",
        )
    item_id = parts[1].strip()
    target_id = parts[2].strip() if len(parts) == 3 else None
    escaped_item = html.escape(item_id)

    tokens = await get_tokens(user_id)
    if not tokens:
        return await message.reply("❌ No accounts found.")

    status_msg = await message.reply(
        f"<b>🎒 Activating item <code>{escaped_item}</code></b> across {len(tokens)} accounts...",
        parse_mode="HTML",
    )

    results = []
    for i, t in enumerate(tokens):
        name = html.escape(t.get("name", f"#{i + 1}"))
        res = await use_item_for_token(t["token"], item_id, target_id=target_id)
        status = res["status"]
        body = res["body"] if isinstance(res["body"], dict) else {}
        if status == 200 and not body.get("errorCode"):
            results.append(f"<b>{name}</b>: ✅ Activated")
        else:
            err = html.escape(str(body.get("errorMessage") or body.get("errorCode") or f"HTTP {status}"))
            results.append(f"<b>{name}</b>: ❌ {err}")

    await status_msg.edit_text(
        f"<b>🎒 Item Activation Report for <code>{escaped_item}</code>:</b>\n\n" + "\n".join(results),
        parse_mode="HTML",
    )
