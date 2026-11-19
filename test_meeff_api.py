#!/usr/bin/env python3
"""
All-In-One Meeff v7.1.7 API Diagnostic & Inspection Suite.

Zero external HTTP dependencies required (uses requests if installed, otherwise
falls back to Python stdlib urllib).

Clarification on Meeff Protocol (verified from v7.1.7 APK):
- Client requests do NOT send `isVipUser` or `ruby` balance in headers/payload.
- Client ONLY sends `Meeff-Access-Token` + `User-Agent: okhttp/5.3.2` + `locale`.
- The Meeff server looks up your account tier, Ruby balance, and quotas on the
  backend and returns them in responses (e.g. /user/login/v4, /user/info/v1, /shop/info/v1).
"""

import argparse
import asyncio
import gzip
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_URL = "https://api.meeff.com"
USER_AGENT = "okhttp/5.3.2"
APP_VERSION = "7.1.7"
DEFAULT_LOCALE = "en-US"
DEFAULT_LAT = 37.5665
DEFAULT_LNG = 126.9780


def mask_token(token: str) -> str:
    if not token:
        return "<none>"
    if len(token) <= 14:
        return "***"
    return f"{token[:6]}...{token[-6:]}"


def build_headers(token: Optional[str] = None, has_body: bool = False) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if token:
        headers["Meeff-Access-Token"] = token.strip()
    if has_body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    return headers


def extract_interesting_headers(headers: Any) -> Dict[str, str]:
    prefixes = ("date", "retry-after", "x-ratelimit", "ratelimit", "cf-", "server", "content-type")
    out: Dict[str, str] = {}
    for k, v in headers.items():
        if k.lower().startswith(prefixes):
            out[k] = v
    return out


def _http_call_sync(
    method: str,
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]],
    json_body: Optional[Dict[str, Any]],
    proxy: Optional[str],
) -> tuple[int, Dict[str, str], str]:
    if _requests is not None:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = _requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params or None,
            json=json_body,
            proxies=proxies,
            timeout=20,
        )
        return resp.status_code, extract_interesting_headers(resp.headers), resp.text

    full_url = url
    if params:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
    data_bytes = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    req = urllib.request.Request(full_url, data=data_bytes, headers=headers, method=method)

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, extract_interesting_headers(resp.headers), raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers and e.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return e.code, extract_interesting_headers(e.headers or {}), raw.decode("utf-8", errors="replace")


def send_request(
    method: str,
    path: str,
    token: Optional[str],
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    proxy: Optional[str] = None,
    quiet_body: bool = False,
    transcript: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if not path.startswith("/"):
        path = "/" + path
    url = f"{BASE_URL}{path}"

    req_params = dict(params or {})
    req_body = dict(json_body) if json_body is not None else None

    if method.upper() == "GET":
        req_params.setdefault("locale", DEFAULT_LOCALE)
    elif req_body is not None:
        req_body.setdefault("locale", DEFAULT_LOCALE)

    headers = build_headers(token, has_body=(req_body is not None))

    t0 = time.perf_counter()
    status, resp_headers, raw_text = _http_call_sync(
        method.upper(), url, headers, req_params, req_body, proxy
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    try:
        parsed = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError:
        parsed = {"_raw_text": raw_text}

    print("\n" + "=" * 72)
    print(f"{method.upper()} {url}")
    if req_params:
        print(f"Query   : {req_params}")
    if req_body is not None:
        redacted_in = {k: ("***" if k == "providerToken" else v) for k, v in req_body.items()}
        print(f"JSON In : {json.dumps(redacted_in, ensure_ascii=False)}")
    print(f"Token   : {mask_token(token or '')}")
    print(f"Status  : HTTP {status} ({elapsed_ms:.1f} ms)")
    print(f"Headers : {json.dumps(resp_headers, ensure_ascii=False)}")
    if not quiet_body:
        print("Body    :")
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    print("=" * 72)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": method.upper(),
        "path": path,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "headers": resp_headers,
        "body": parsed,
    }
    if transcript is not None:
        transcript.append(record)
    return record


def print_user_snapshot(user: Dict[str, Any]) -> None:
    if not isinstance(user, dict) or not user:
        return
    print("\n" + "#" * 72)
    print("ACCOUNT SNAPSHOT (Server-Reported Entitlements & State)")
    print(f"  ID / Name        : {user.get('_id')} | {user.get('name')} ({user.get('nationalityCode')})")
    print(f"  Subscription     : active={user.get('hasActiveSubscription')} | itemId={user.get('subscriptionItemId')} | validUntil={user.get('subscriptionValidUntil')}")
    print(f"  VIP Flag         : isVipUser={user.get('isVipUser')}")
    print(f"  Ruby             : {user.get('ruby', 0)} (expiring={user.get('expiringRubyTotal', 0)}, nearest={user.get('rubyNearestExpiry')})")
    print(f"  Verification     : faceVerified={user.get('faceVerified')} | faceLivenessPassed={user.get('faceLivenessPassed')} | phone={user.get('isPhoneVerified')}")
    print(f"  Profile Score    : {user.get('profileScore')} | deleted={user.get('isDeleted')} | banned={user.get('isBanned')}")
    print(f"  Quotas on User   : livenessQuota={user.get('livenessQuota')} | compareQuota={user.get('compareQuota')}")
    items = user.get("items")
    if items:
        print(f"  Items            : {items}")
    print("#" * 72)


def email_login(
    email: str,
    password: str,
    proxy: Optional[str],
    device_id: Optional[str] = None,
    transcript: Optional[List[Dict[str, Any]]] = None,
) -> tuple[Optional[str], Optional[str]]:
    uid = device_id or "".join(random.choices("0123456789abcdef", k=16))
    payload = {
        "provider": "email",
        "email": email,
        "providerId": email,
        "providerToken": password,
        "os": "Android v15",
        "platform": "android",
        "device": "BRAND: INFINIX, MODEL: Infinix X6858, DEVICE: Infinix-X6858, PRODUCT: X6858-OP, DISPLAY: X6858-15.1.0.138SP01(OP001PF001AZ)",
        "appVersion": APP_VERSION,
        "deviceUniqueId": uid,
        "pushToken": "",
        "deviceLanguage": "en",
        "deviceRegion": "US",
        "simRegion": "PK",
        "deviceGmtOffset": "+0500",
        "deviceRooted": 0,
        "deviceEmulator": 0,
        "deviceEmulatorInfo": "",
        "locale": DEFAULT_LOCALE,
    }
    res = send_request("POST", "/user/login/v4", None, json_body=payload, proxy=proxy, transcript=transcript)
    body = res["body"] if isinstance(res["body"], dict) else {}
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    token = body.get("token") or (user.get("token") if isinstance(user, dict) else None)
    if user:
        print_user_snapshot(user)
    return token, user.get("_id") if isinstance(user, dict) else None


async def load_token_from_db(tg_user_id: int, account_index: int) -> Optional[str]:
    try:
        import db
        tokens = await db.get_tokens(tg_user_id)
        if not tokens:
            print(f"No tokens found in MongoDB for user_{tg_user_id}")
            return None
        if account_index < 0 or account_index >= len(tokens):
            print(f"Invalid account index {account_index}; valid range: 0..{len(tokens) - 1}")
            for i, t in enumerate(tokens):
                print(f"  [{i}] {t.get('name', 'Unnamed')} ({t.get('email', 'no-email')}) active={t.get('active', True)}")
            return None
        chosen = tokens[account_index]
        print(f"Loaded DB token [{account_index}]: {chosen.get('name', 'Unnamed')} ({chosen.get('email', 'no-email')})")
        return chosen["token"]
    except Exception as e:
        print(f"Failed loading token from DB: {e}")
        return None


def run_all_readonly_checks(
    token: str,
    lat: float,
    lng: float,
    proxy: Optional[str],
    user_id: Optional[str] = None,
    transcript: Optional[List[Dict[str, Any]]] = None,
) -> None:
    print("\n>>> RUNNING ALL-IN-ONE DIAGNOSTIC AUDIT (SAFE READ-ONLY + EXPLORE FETCH) <<<")

    init_res = send_request(
        "GET", "/api/init/v2", token,
        params={"platform": "android", "version": APP_VERSION},
        proxy=proxy, transcript=transcript
    )
    shop_res = send_request("POST", "/shop/info/v1", token, json_body={}, proxy=proxy, transcript=transcript)
    ruby_res = send_request("GET", "/user/ruby/list/expiring/v1", token, proxy=proxy, transcript=transcript)
    att_res = send_request("GET", "/event/attendance/v2", token, proxy=proxy, transcript=transcript)
    evt_res = send_request("GET", "/event/check/v1", token, proxy=proxy, transcript=transcript)
    lounge_res = send_request("GET", "/lounge/dashboard/v1", token, proxy=proxy, transcript=transcript)
    chat_res = send_request("GET", "/chatroom/dashboard/v1", token, proxy=proxy, transcript=transcript)
    exp_res = send_request(
        "GET", "/user/explore/v4", token,
        params={"lat": str(lat), "lng": str(lng), "unreachableUserIds": "", "isCustomLoc": "0"},
        proxy=proxy, transcript=transcript
    )

    info_res = None
    if user_id:
        info_res = send_request(
            "GET", "/user/info/v1", token, params={"userId": user_id}, proxy=proxy, transcript=transcript
        )
        ubody = info_res["body"] if isinstance(info_res["body"], dict) else {}
        if isinstance(ubody.get("user"), dict):
            print_user_snapshot(ubody["user"])
        elif ubody.get("_id"):
            print_user_snapshot(ubody)

    # Compact summary
    lb = lounge_res["body"] if isinstance(lounge_res["body"], dict) else {}
    cb = chat_res["body"] if isinstance(chat_res["body"], dict) else {}
    eb = exp_res["body"] if isinstance(exp_res["body"], dict) else {}
    sb = shop_res["body"] if isinstance(shop_res["body"], dict) else {}

    print("\n" + "#" * 72)
    print("ALL-IN-ONE SUMMARY")
    print(f"  /api/init/v2               : HTTP {init_res['status']} ({init_res['elapsed_ms']:.0f}ms)")
    print(f"  /shop/info/v1              : HTTP {shop_res['status']} | keys={list(sb.keys())[:8]}")
    print(f"  /user/ruby/list/expiring   : HTTP {ruby_res['status']}")
    print(f"  /event/attendance/v2       : HTTP {att_res['status']}")
    print(f"  /event/check/v1            : HTTP {evt_res['status']}")
    print(f"  /lounge/dashboard/v1       : HTTP {lounge_res['status']} | both={len(lb.get('both') or [])} | one={len(lb.get('one') or [])}")
    print(f"  /chatroom/dashboard/v1     : HTTP {chat_res['status']} | rooms={len(cb.get('rooms') or [])}")
    print(f"  /user/explore/v4           : HTTP {exp_res['status']} | users={len(eb.get('users') or [])} | hasMore={eb.get('hasMore')} | errorCode={eb.get('errorCode')}")
    if info_res:
        print(f"  /user/info/v1              : HTTP {info_res['status']}")
    print("#" * 72)


def probe_like_quota(
    token: str,
    lat: float,
    lng: float,
    delay: float,
    max_likes: int,
    proxy: Optional[str],
    send_nope: bool = False,
    transcript: Optional[List[Dict[str, Any]]] = None,
) -> None:
    is_okay = "0" if send_nope else "1"
    action_label = "NOPE (isOkay=0)" if send_nope else "LIKE (isOkay=1)"
    sent_count = 0
    seen_ids = set()
    page = 0

    print(f"\nStarting Quota Probe -> {action_label}, delay={delay}s, max={max_likes}")

    while sent_count < max_likes:
        page += 1
        unreachable = ",".join(list(seen_ids)[-50:])
        res = send_request(
            "GET",
            "/user/explore/v4",
            token,
            params={
                "lat": str(lat),
                "lng": str(lng),
                "unreachableUserIds": unreachable,
                "isCustomLoc": "0",
            },
            proxy=proxy,
            quiet_body=True,
            transcript=transcript,
        )
        body = res["body"] if isinstance(res["body"], dict) else {}
        if res["status"] != 200 or body.get("errorCode"):
            print(f"\n[STOPPED ON EXPLORE] Page {page}, total sent={sent_count}")
            print(json.dumps(body, indent=2, ensure_ascii=False))
            return

        users = body.get("users") or []
        print(f"[Explore Page {page}] got {len(users)} users (hasMore={body.get('hasMore')})")
        if not users:
            print("[STOPPED] Explore returned 0 users.")
            return

        for u in users:
            if sent_count >= max_likes:
                break
            uid = u.get("_id")
            if not uid or uid in seen_ids:
                continue
            seen_ids.add(uid)

            ans = send_request(
                "GET",
                "/user/undoableAnswer/v5",
                token,
                params={"userId": uid, "isOkay": is_okay},
                proxy=proxy,
                quiet_body=True,
                transcript=transcript,
            )
            ab = ans["body"] if isinstance(ans["body"], dict) else {}
            err_code = ab.get("errorCode")
            if ans["status"] == 200 and not err_code:
                sent_count += 1
                print(
                    f"  [{sent_count}/{max_likes}] OK -> {u.get('name', '?')} "
                    f"({u.get('nationalityCode', '?')}, id={uid}) [{ans['elapsed_ms']:.0f}ms]"
                )
            elif err_code in ("AlreadyAnswered", "ChatRoomExist"):
                print(f"  [SKIP {err_code}] {uid} -> continuing")
            else:
                print("\n" + "!" * 72)
                print(f"[QUOTA / ERROR HIT after {sent_count} successful {action_label}]")
                print(f"HTTP Status : {ans['status']}")
                print(f"Headers     : {json.dumps(ans['headers'], ensure_ascii=False)}")
                print(f"Full JSON   :\n{json.dumps(ab, indent=2, ensure_ascii=False)}")
                print("!" * 72)
                return

            if delay > 0:
                time.sleep(delay)

    print(f"\n[DONE] Sent {sent_count} requests without hitting a quota limit.")


def interactive_menu(
    token: Optional[str],
    user_id: Optional[str],
    proxy: Optional[str],
    lat: float,
    lng: float,
    transcript: List[Dict[str, Any]],
) -> None:
    while True:
        print(f"\n--- All-In-One Meeff v{APP_VERSION} API Tester (token={mask_token(token or '')}) ---")
        print("1)  ALL-IN-ONE Safe Audit       (init + shop + ruby + events + lounge + chatrooms + explore)")
        print("2)  Email Login + Full Snapshot (POST /user/login/v4 -> shows VIP/Plus/Ruby/Verified)")
        print("3)  Explore Users               (GET /user/explore/v4)")
        print("4)  Send 1 Like / Nope          (GET /user/undoableAnswer/v5)")
        print("5)  Probe Like Quota Limit      (Explore + sequential Likes until LikeExceeded/RequestExceeded)")
        print("6)  Test Update Filter          (POST /user/updateFilter/v1)")
        print("7)  Lounge & Chatrooms          (GET /lounge/dashboard/v1 & /chatroom/dashboard/v1)")
        print("8)  Read Chatroom Messages      (POST /chat/get/v1)")
        print("9)  User Info (by userId)       (GET /user/info/v1)")
        print("10) HotPick / Face Recs         (POST /user/explore/hotPick)")
        print("11) Custom Raw Request          (GET / POST any path)")
        print("12) Switch / Paste Token")
        print("0)  Exit")

        choice = input("Select [0-12]: ").strip()
        if choice == "0":
            break
        elif choice == "2":
            em = input("Email: ").strip()
            pw = input("Password: ").strip()
            new_tok, new_uid = email_login(em, pw, proxy, transcript=transcript)
            if new_tok:
                token = new_tok
                user_id = new_uid or user_id
                print(f"Active session token updated: {mask_token(token)}")
            continue
        elif choice == "12":
            token = input("Paste new Meeff-Access-Token: ").strip() or token
            continue

        if not token:
            print("No active token! Login with option 2 or set token with option 12.")
            continue

        if choice == "1":
            run_all_readonly_checks(token, lat, lng, proxy, user_id=user_id, transcript=transcript)
        elif choice == "3":
            unreach = input("unreachableUserIds (comma-sep, optional): ").strip()
            send_request(
                "GET", "/user/explore/v4", token,
                params={"lat": str(lat), "lng": str(lng), "unreachableUserIds": unreach, "isCustomLoc": "0"},
                proxy=proxy, transcript=transcript,
            )
        elif choice == "4":
            uid = input("Target userId: ").strip()
            okay = input("isOkay (1=Like, 0=Nope) [1]: ").strip() or "1"
            send_request(
                "GET", "/user/undoableAnswer/v5", token,
                params={"userId": uid, "isOkay": okay}, proxy=proxy, transcript=transcript,
            )
        elif choice == "5":
            max_n = int(input("Max Likes to send [50]: ").strip() or "50")
            delay = float(input("Delay between requests in sec [0.3]: ").strip() or "0.3")
            probe_like_quota(token, lat, lng, delay, max_n, proxy, transcript=transcript)
        elif choice == "6":
            nats = input("Nationality codes comma-sep (blank=all, e.g. KR,JP): ").strip()
            codes_csv = ",".join(c.strip().upper() for c in nats.split(",") if c.strip()) if nats else ""
            body = {
                "filterGenderType": 5,
                "filterBirthYearFrom": 1990,
                "filterBirthYearTo": 2008,
                "filterDistance": 510,
                "filterLanguageCodes": "",
                "filterNationalityBlock": 0,
                "filterMinProfileImageCount": 1,
                "filterFaceVerifiedOnly": False,
                "filterRecentlyActiveOnly": False,
            }
            if codes_csv:
                body["filterNationalityCodes"] = codes_csv
            send_request("POST", "/user/updateFilter/v1", token, json_body=body, proxy=proxy, transcript=transcript)
        elif choice == "7":
            send_request("GET", "/lounge/dashboard/v1", token, proxy=proxy, transcript=transcript)
            send_request("GET", "/chatroom/dashboard/v1", token, proxy=proxy, transcript=transcript)
        elif choice == "8":
            rid = input("chatRoomId: ").strip()
            send_request("POST", "/chat/get/v1", token, json_body={"chatRoomId": rid}, proxy=proxy, transcript=transcript)
        elif choice == "9":
            uid = input(f"userId [{user_id or ''}]: ").strip() or (user_id or "")
            if uid:
                res = send_request("GET", "/user/info/v1", token, params={"userId": uid}, proxy=proxy, transcript=transcript)
                b = res["body"] if isinstance(res["body"], dict) else {}
                if isinstance(b.get("user"), dict):
                    print_user_snapshot(b["user"])
        elif choice == "10":
            send_request("POST", "/user/explore/hotPick", token, json_body={}, proxy=proxy, transcript=transcript)
        elif choice == "11":
            method = (input("Method (GET/POST) [GET]: ").strip() or "GET").upper()
            path = input("Path (e.g. /user/explore/v4): ").strip()
            if method == "GET":
                raw_q = input('Query params JSON (optional, e.g. {"lat":"37.5"}): ').strip()
                q = json.loads(raw_q) if raw_q else {}
                send_request("GET", path, token, params=q, proxy=proxy, transcript=transcript)
            else:
                raw_b = input("JSON body (optional, e.g. {}): ").strip()
                b = json.loads(raw_b) if raw_b else {}
                send_request("POST", path, token, json_body=b, proxy=proxy, transcript=transcript)
        else:
            print("Invalid option.")


def main() -> None:
    parser = argparse.ArgumentParser(description=f"All-In-One Meeff v{APP_VERSION} API Diagnostic Suite")
    parser.add_argument("--token", help="Meeff-Access-Token")
    parser.add_argument("--email", help="Optional Meeff email for /user/login/v4")
    parser.add_argument("--password", help="Optional Meeff password for /user/login/v4")
    parser.add_argument("--tg-user-id", type=int, help="Optional Telegram user_id to load token from MongoDB")
    parser.add_argument("--account-index", type=int, default=0, help="Account index in MongoDB user collection (default: 0)")
    parser.add_argument("--proxy", default=os.getenv("PROXY"), help="Optional HTTP/SOCKS proxy URL (defaults to PROXY env)")
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT, help=f"Latitude (default: {DEFAULT_LAT})")
    parser.add_argument("--lng", type=float, default=DEFAULT_LNG, help=f"Longitude (default: {DEFAULT_LNG})")
    parser.add_argument(
        "--action",
        choices=["all", "login", "explore", "like", "probe", "lounge", "chatroom", "shop", "attendance", "user", "raw"],
        help="Run a single action or 'all' non-interactively and exit",
    )
    parser.add_argument("--user-id", help="Target userId for 'like' or 'user' action")
    parser.add_argument("--nope", action="store_true", help="Send isOkay=0 instead of 1 for 'like' / 'probe'")
    parser.add_argument("--max-likes", type=int, default=50, help="Max requests for 'probe' action (default: 50)")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay seconds between requests for 'probe' (default: 0.3)")
    parser.add_argument("--method", default="GET", help="Method for 'raw' action (GET/POST)")
    parser.add_argument("--path", help="Endpoint path for 'raw' action (e.g. /user/explore/v4)")
    parser.add_argument("--data", default="{}", help="JSON string for query (GET) or body (POST) in 'raw' action")
    parser.add_argument("--save", help="Save all request/response transcripts to a JSON file")

    args = parser.parse_args()
    transcript: List[Dict[str, Any]] = []
    token = args.token
    self_user_id = args.user_id

    if args.email and args.password:
        login_tok, login_uid = email_login(args.email, args.password, args.proxy, transcript=transcript)
        token = token or login_tok
        self_user_id = self_user_id or login_uid
    elif not token and args.tg_user_id is not None:
        token = asyncio.run(load_token_from_db(args.tg_user_id, args.account_index))

    if not token and args.action and args.action != "login":
        token = input("Paste Meeff-Access-Token: ").strip()

    engine = "requests" if _requests is not None else "urllib"
    print(f"HTTP Engine: {engine} | UA: {USER_AGENT} | App: {APP_VERSION} | Proxy: {args.proxy or 'none (direct)'}")

    try:
        if not args.action:
            if not token:
                ans = input("Paste Meeff-Access-Token (or press Enter to login by email in menu): ").strip()
                token = ans or None
            interactive_menu(token, self_user_id, args.proxy, args.lat, args.lng, transcript)
        elif args.action == "login":
            if not (args.email and args.password):
                print("--email and --password are required for --action login")
        elif args.action == "all":
            run_all_readonly_checks(token, args.lat, args.lng, args.proxy, user_id=self_user_id, transcript=transcript)
        elif args.action == "explore":
            send_request(
                "GET", "/user/explore/v4", token,
                params={"lat": str(args.lat), "lng": str(args.lng), "unreachableUserIds": "", "isCustomLoc": "0"},
                proxy=args.proxy, transcript=transcript,
            )
        elif args.action == "like":
            if not args.user_id:
                print("--user-id is required for --action like")
            else:
                send_request(
                    "GET", "/user/undoableAnswer/v5", token,
                    params={"userId": args.user_id, "isOkay": "0" if args.nope else "1"},
                    proxy=args.proxy, transcript=transcript,
                )
        elif args.action == "probe":
            probe_like_quota(
                token, args.lat, args.lng, args.delay, args.max_likes, args.proxy,
                send_nope=args.nope, transcript=transcript,
            )
        elif args.action == "lounge":
            send_request("GET", "/lounge/dashboard/v1", token, proxy=args.proxy, transcript=transcript)
        elif args.action == "chatroom":
            send_request("GET", "/chatroom/dashboard/v1", token, proxy=args.proxy, transcript=transcript)
        elif args.action == "shop":
            send_request("POST", "/shop/info/v1", token, json_body={}, proxy=args.proxy, transcript=transcript)
        elif args.action == "attendance":
            send_request("GET", "/event/attendance/v2", token, proxy=args.proxy, transcript=transcript)
        elif args.action == "user":
            if not self_user_id:
                print("--user-id is required for --action user")
            else:
                res = send_request("GET", "/user/info/v1", token, params={"userId": self_user_id}, proxy=args.proxy, transcript=transcript)
                b = res["body"] if isinstance(res["body"], dict) else {}
                if isinstance(b.get("user"), dict):
                    print_user_snapshot(b["user"])
        elif args.action == "raw":
            if not args.path:
                print("--path is required for --action raw")
            else:
                payload = json.loads(args.data)
                if args.method.upper() == "GET":
                    send_request("GET", args.path, token, params=payload, proxy=args.proxy, transcript=transcript)
                else:
                    send_request("POST", args.path, token, json_body=payload, proxy=args.proxy, transcript=transcript)
    finally:
        if args.save and transcript:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(transcript, f, indent=2, ensure_ascii=False)
            print(f"\nSaved {len(transcript)} responses to {args.save}")


if __name__ == "__main__":
    main()
