# Meeff 7.1.7 Native Android Architecture, Protocols & Complete API Specification

> **Package Name:** `com.noyesrun.meeff`  
> **Version:** `7.1.7` (`versionCode: 929`)  
> **Network Stack:** `okhttp3.OkHttpClient` (`User-Agent: okhttp/5.1.0`) + `AWSIotMqttManager` (AWS IoT Core MQTT) + `FirebaseMessaging` (FCM) + `org.webrtc` (AppRTC / WebRTC) + `com.amplifyframework` (AWS Rekognition Face Liveness)  
> **Purpose of Document:** Complete, self-contained reverse-engineered reference of how every native feature, background service, real-time push pipeline, security check, state machine, and REST endpoint in the Meeff Android app works step by step—so that no human engineer or AI agent ever needs to decompile the APK again.

---

## Table of Contents

1. [Global Application Boot Lifecycle & Core Singletons (`GlobalApplication`)](#1-global-application-boot-lifecycle--core-singletons-globalapplication)
2. [HTTP Transport Layer (`RestClient`) & Authentication Header Protocol](#2-http-transport-layer-restclient--authentication-header-protocol)
3. [Real-Time Push & Live Event Engine (AWS IoT MQTT + FCM + `chatPing`)](#3-real-time-push--live-event-engine-aws-iot-mqtt--fcm--chatping)
4. [Device Fingerprinting, Emulator Detection & Multi-Device Lock (`DeviceInfo`, `EmulatorDetector`, `PendingDevice`)](#4-device-fingerprinting-emulator-detection--multi-device-lock-deviceinfo-emulatordetector-pendingdevice)
5. [Native Onboarding, Registration, Login & Phone OTP Flow (`AuthHandler`)](#5-native-onboarding-registration-login--phone-otp-flow-authhandler)
6. [AWS S3 Presigned Image & Voice Upload Pipeline (`uploadInfo` & `uploadFileToS3`)](#6-aws-s3-presigned-image--voice-upload-pipeline-uploadinfo--uploadfiletos3)
7. [3D Face Verification & AWS Rekognition Liveness Architecture (`feature.faceverification`)](#7-3d-face-verification--aws-rekognition-liveness-architecture-featurefaceverification)
8. [GPS Location Engine & Mock-Location Detection (`LocationHandler`)](#8-gps-location-engine--mock-location-detection-locationhandler)
9. [Explore Discovery Engine, Swipe Queue & Filter Rules (`ExploreHandler`, `AnswerQueueHandler`, `UserFilter`)](#9-explore-discovery-engine-swipe-queue--filter-rules-explorehandler-answerqueuehandler-userfilter)
10. [Free Tier vs. Paid Tier (Meeff Plus / VIP / Ruby Items) Server Enforcement Matrix](#10-free-tier-vs-paid-tier-meeff-plus--vip--ruby-items-server-enforcement-matrix)
11. [Lounge (Waiting Room) & Chatroom Messaging Engine (`LoungeHandler` & `ChatHandler`)](#11-lounge-waiting-room--chatroom-messaging-engine-loungehandler--chathandler)
12. [WebRTC 1:1 Video & Audio Calling (`FaceTalk`, `VibeMeet`, `VoiceBloom` & `PeerConnectionManager`)](#12-webrtc-11-video--audio-calling-facetalk-vibemeet-voicebloom--peerconnectionmanager)
13. [BlindMatch (Group Speed-Dating) State Machine](#13-blindmatch-group-speed-dating-state-machine)
14. [Shop, Ruby Economy, Google Play Billing (`IabVerifyReceiptHandler`) & Today Spotlight Bidding](#14-shop-ruby-economy-google-play-billing-iabverifyreceipthandler--today-spotlight-bidding)
15. [A-to-Z Master Reference of All 125 REST API Endpoints](#15-a-to-z-master-reference-of-all-125-rest-api-endpoints)
16. [Server Error Codes & Exception Handling Reference](#16-server-error-codes--exception-handling-reference)

---

## 1. Global Application Boot Lifecycle & Core Singletons (`GlobalApplication`)

When the Meeff Android app starts, `com.noyesrun.meeff.GlobalApplication` initializes all global managers and lifecycle observers:

### Step-by-Step Native Boot Sequence
1. **`GlobalApplication.onCreate()`**:
   - Registers `ProcessLifecycleOwner` observer (`ON_START` / `ON_STOP`) and `ActivityLifecycleCallbacks` to track whether the app is in the foreground (`isAppActive_`).
   - Initializes encrypted local storage via `com.noyesrun.meeff.util.ObscuredSharedPreferences` (AES/base64-obfuscated `SharedPreferences` where `me`, `token`, `push`, and `filter` states are cached).
   - Instantiates the 12 core singleton handlers:
     1. `AuthHandler` — manages `loginToken_` (`Meeff-Access-Token`), `pushToken_` (FCM token), `restoreMe()` user backup, and `blockUsers_` list.
     2. `AWSIotMqttClient` — manages persistent real-time MQTT connection to AWS IoT Core while the app is in foreground.
     3. `NotificationHandler` — unified router for both foreground AWS IoT MQTT payloads and background Firebase Cloud Messaging (FCM) notifications.
     4. `ExploreHandler` — manages the card stack in the Explore tab (`usersExplored_`) and triggers `GET /user/explore/v4`.
     5. `AnswerQueueHandler` — background FIFO queue that dispatches Like/Nope swipes to `GET /user/undoableAnswer/v5`.
     6. `LoungeHandler` — manages `one` (incoming likes) and `both` (mutual matches) lists in the Lounge tab.
     7. `ChatHandler` — manages `chatRooms_` (`SortedList`), per-room message lists (`chatsMap_`), and the outgoing message queue (`sendingQueue_`).
     8. `LocationHandler` — manages GPS / network location updates and mock-location detection.
     9. `VisitorHandler` — manages profile visitor state and unread badges.
     10. `SignalingHandler` — dispatches incoming WebRTC signaling events (`offer`, `answer`, `ice`, `close`) for FaceTalk and VoiceBloom.
     11. `IabVerifyReceiptHandler` — manages Google Play `BillingClient` connections and server-side receipt verification.
     12. `LocaleHandler` — resolves app/device locale (`en-US`, `ko-KR`, etc.).
2. **Version & Config Handshake (`POST /api/init/v2`)**:
   - Immediately upon launch, the app calls `RestClient.apiInit`:
     ```http
     POST https://api.meeff.com/api/init/v2
     Content-Type: application/json; charset=utf-8
     User-Agent: okhttp/5.1.0

     {
       "platform": "android",
       "version": "7.1.7",
       "locale": "en-US"
     }
     ```
   - The backend responds with minimum required app version (`AppVersionValidationError` if outdated), server maintenance flags, and popup notices.
3. **Session Hydration (`GET /user/info/v1`)**:
   - If `AuthHandler.isLoggedIn()` is true (a saved `Meeff-Access-Token` exists in `ObscuredSharedPreferences`), the app calls `GET /user/info/v1` (with no `userId` query param) to refresh the logged-in `User` model (`backupMe(user)`), active `UserFilter`, Ruby balance, and subscription validity timestamps.

---

## 2. HTTP Transport Layer (`RestClient`) & Authentication Header Protocol

All REST API calls are executed by `com.noyesrun.meeff.net.RestClient` using a static singleton `okhttp3.OkHttpClient`.

### Base URL & Media Types
- **Base Host:** `https://api.meeff.com`
- **JSON Media Type:** `application/json; charset=utf-8`
- **Binary Upload Media Type:** `application/octet-stream` / `multipart/form-data`

### Automatic Header & Locale Injection (`RestClient.get` / `post` / `put`)
Inside `RestClient`:
1. **Authentication Header:** Every request reads `AuthHandler.getLoginToken()`. If non-empty, it attaches:
   ```http
   Meeff-Access-Token: <loginToken_>
   ```
2. **User-Agent:** OkHttp automatically attaches:
   ```http
   User-Agent: okhttp/5.1.0
   Accept-Encoding: gzip
   Connection: Keep-Alive
   ```
3. **Automatic `locale` Parameter:**
   - On **`GET`** requests: `RestClient.get(url, params, handler)` appends `locale=<lang>-<COUNTRY>` (e.g., `locale=en-US`) to the URL query string.
   - On **`POST` / `PUT`** requests: `RestClient.post(url, jsonObject, handler)` and `RestClient.put(...)` automatically insert `"locale": "en-US"` into the JSON request body before serializing.

---

## 3. Real-Time Push & Live Event Engine (AWS IoT MQTT + FCM + `chatPing`)

Meeff uses a **dual-channel real-time architecture**:
- **Channel A (Foreground Live Socket):** **AWS IoT Core MQTT** (`com.noyesrun.meeff.net.AWSIotMqttClient`) when the app is open (`ON_START`).
- **Channel B (Background Wake Push):** **Firebase Cloud Messaging (FCM)** (`com.noyesrun.meeff.push.PushFirebaseMessagingService`) when the app is backgrounded or killed.

### 3.1 Step-by-Step Foreground AWS IoT MQTT Lifecycle (`AWSIotMqttClient`)
1. **Trigger:** When the user logs in (`onAuthStateChanged()`) or brings the app to the foreground (`GlobalApplication.onStart()` -> `setConnectMQTT()`).
2. **Broker Endpoint:**
   ```text
   a2zsbuvqe895if-ats.iot.us-east-1.amazonaws.com
   ```
3. **Connection & Credentials (`AWSIotMqttClient$2`):**
   - Instantiates `AWSIotMqttManager(sessionId_, "a2zsbuvqe895if-ats.iot.us-east-1.amazonaws.com")`.
   - Connects using `AWSMobileClient.getInstance()` (AWS Cognito credentials provider).
4. **Topic Subscription (`subscribeMQTT()`):**
   - Once `AWSIotMqttClientStatus` transitions to `Connected`, the client subscribes with QoS 0 (`AWSIotMqttQos.QOS0`) to the user's private topic:
     ```text
     meeff/user/<userId_or_sessionId>
     ```
5. **Active Chat Heartbeat (`RestClient.chatPing`):**
   - While connected, a scheduled `Handler` runnable fires every `pingSec_` seconds and sends:
     ```http
     POST /chat/ping/v1
     {
       "uid": "<current_user_id>",
       "did": "<deviceUniqueId>",
       "locale": "en-US"
     }
     ```
   - This tells the Meeff backend that the client's MQTT listener is alive in the foreground so the server routes real-time events via AWS IoT MQTT instead of waking the device via FCM.
6. **Foreground Teardown (`ON_STOP`):**
   - When the app goes to background (`GlobalApplication.onStop()`), a `disConnectTimer_` (`CountDownTimer`) runs; once expired, `unsubscribeMQTT()` unsubscribes from `meeff/user/<id>` and calls `disconnectMQTT()`.

### 3.2 All 16 Real-Time MQTT / Push Commands (`cmd`)
When a JSON message arrives on `meeff/user/<id>` (or via FCM `RemoteMessage.getData()`), `AWSIotMqttClient` parses the UTF-8 JSON payload and checks the `"cmd"` key:

| `cmd` Value | Routed To | Exact Native Behavior in App |
| :--- | :--- | :--- |
| `chatNew` | `NotificationHandler` -> `ChatHandler.appendMostRecentChat()` | A new chat message arrived in `chatRoomId`. App appends the `Chat` object to `chatsMap_`, increments unread badge, shows an in-app top toast (or system notification if not inside that `ChatActivity`), and if inside the room, triggers `POST /chat/read/v1`. |
| `chatRead` | `NotificationHandler` -> `ChatHandler.applyChatRoomRead()` | Partner read your messages in `chatRoomId`. Updates the `"1"` unread indicator next to sent messages in `ChatActivity`. |
| `chatRoomAdded` | `NotificationHandler` -> `ChatHandler.appendChatRoom()` | Someone opened a new chatroom with you (`POST /chatroom/open/v2`). Inserts the `ChatRoom` into `chatRooms_` sorted list. |
| `chatRoomUpgrade` | `NotificationHandler` -> `ChatHandler.applyChatRoomUpgraded()` | Partner upgraded the chatroom (e.g., Secret/Permanent Chatroom item used). Updates room status in UI. |
| `oneWaitingRoomAdded` | `NotificationHandler` -> `LoungeHandler` | A user liked you (`GET /user/undoableAnswer/v5?isOkay=1`). Refreshes Lounge `one` (incoming likes) badge and shows notification. |
| `bothWaitingRoomAdded` | `NotificationHandler` -> `LoungeHandler` | Mutual match occurred! Both users liked each other. Adds user to Lounge `both` list. |
| `matched` | `NotificationHandler` | Displays match alert / top toast notification. |
| `rubyAdded` | `NotificationHandler` | Rubies were credited to the user's wallet (from purchase, event, or attendance). Refreshes `/user/info/v1`. |
| `resetPasswordSave` | `NotificationHandler` | Password change completed; refreshes auth state. |
| `facetalk_notify` | `NotificationHandler.showFaceTalkNotification()` | Incoming 1:1 FaceTalk / VibeMeet video call invitation (`roomId`, `partner`). Launches incoming call UI / notification. |
| `offer` | `SignalingHandler.onReceiveSignalData()` | WebRTC SDP Offer received from call partner for `roomId`. |
| `answer` | `SignalingHandler.onReceiveSignalData()` | WebRTC SDP Answer received from call partner for `roomId`. |
| `ice` | `SignalingHandler.onReceiveSignalData()` | WebRTC ICE Candidate received from call partner for `roomId`. |
| `iceremove` | `SignalingHandler.onReceiveSignalData()` | WebRTC ICE Candidate removal event. |
| `close` | `SignalingHandler.onReceiveSignalData()` / `NotificationHandler` | Call partner hung up or closed the FaceTalk / VoiceBloom room (`roomId`). |
| `voicebloom_extend` | `SignalingHandler.onReceiveSignalData()` | Call partner spent Rubies to extend the VoiceBloom audio call timer. |

### 3.3 Background FCM Push Deduplication (`PushFirebaseMessagingService` & `NotificationHandler`)
1. When Firebase issues or rotates an FCM token, `PushFirebaseMessagingService.onNewToken(token)` stores it in `AuthHandler.setPushToken(token)` and calls:
   ```http
   POST /user/updatePushInfo/v1
   {
     "platform": "android",
     "pushToken": "<fcm_registration_token>",
     "locale": "en-US"
   }
   ```
2. When a push arrives in `onMessageReceived(RemoteMessage)`, `NotificationHandler.onReceiveFirebaseNotification()` extracts the `"uts"` (unique timestamp ID), `"cmd"`, `"pushMeta"` (`title`, `message`, `photoUrl`, `collapseKey`), and `"data"`.
3. **Deduplication Check:** Both `onReceiveAWSIotNotification` and `onReceiveFirebaseNotification` record the event's `"uts"` in `utsNotificationSet` and `utsProcessSet` (`HashSet<String>`). If an event was already processed via AWS IoT MQTT milliseconds earlier, the duplicate FCM message is silently ignored.

---

## 4. Device Fingerprinting, Emulator Detection & Multi-Device Lock (`DeviceInfo`, `EmulatorDetector`, `PendingDevice`)

During registration (`/user/register/*/v4`) and login (`/user/login/v4`), Meeff collects a detailed hardware and environment telemetry payload to detect emulators, rooted phones, and multi-device sharing.

### 4.1 Exact Telemetry Fields Built by `com.noyesrun.meeff.util.DeviceInfo`
| JSON Key | Native Android Source in `DeviceInfo.java` | Example Value |
| :--- | :--- | :--- |
| `platform` | Hardcoded string | `"android"` |
| `os` | `Build.VERSION.RELEASE` (`"Android v" + Build.VERSION.RELEASE`) | `"Android v14"` or `"14"` |
| `device` | `String.format("BRAND: %s, MODEL: %s, DEVICE: %s, PRODUCT: %s, DISPLAY: %s", Build.BRAND, Build.MODEL, Build.DEVICE, Build.PRODUCT, Build.DISPLAY)` | `"BRAND: samsung, MODEL: SM-S918B, ..."` |
| `deviceUniqueId` | `Settings.Secure.getString(contentResolver, "android_id")` | `"9774d56d682e549c"` |
| `deviceLanguage` | `Locale.getDefault().getLanguage()` | `"en"` |
| `deviceRegion` | `Locale.getDefault().getCountry()` | `"US"` |
| `simRegion` | `TelephonyManager.getSimCountryIso()` (falls back to `deviceRegion`) | `"US"` |
| `deviceGmtOffset` | `TimeZone.getDefault().getRawOffset()` formatted as `%02d%02d` with `+`/`-` | `"+0500"` or `"0500"` |
| `appVersion` | `PackageManager.getPackageInfo().versionName` | `"7.1.7"` |
| `pushToken` | `AuthHandler.getPushToken()` (FCM token) | `"d8a9f..."` |
| `deviceRooted` | `DeviceInfo.isRooted()` (`1` if `su` binaries / root packages found, else `0`) | `0` |
| `deviceEmulator` | `DeviceInfo.isEmulator()` (`1` if any emulator check triggers, else `0`) | `0` |
| `deviceEmulatorInfo` | `DeviceInfo.getEmulatorInfo()` (diagnostic string / bitmask of triggered emulator checks) | `"000000000000"` or `"Normal"` |

### 4.2 How `com.noyesrun.meeff.util.EmulatorDetector` Works Step-by-Step
`DeviceInfo.emulatorInfo()` runs `EmulatorDetector.detect()` plus device-specific checks:
1. **`checkBuild()`**: Inspects `Build.FINGERPRINT`, `Build.MODEL`, `Build.MANUFACTURER`, `Build.HARDWARE`, and `Build.PRODUCT` for `"generic"`, `"google_sdk"`, `"droid4x"`, `"Emulator"`, `"Android SDK built for x86"`, `"Genymotion"`, `"goldfish"`, `"vbox86"`, `"sdk"`.
2. **`checkFiles()`**: Checks the filesystem for emulator-specific files:
   - Genymotion: `/dev/socket/genyd`, `/dev/socket/baseband_genyd`
   - Andy: `fstab.andy`, `ueventd.andy.rc`
   - NoxPlayer: `init.nox.rc`, `ueventd.nox.rc`, `fstab.nox`, or launcher package `YSLauncher`
   - BlueStacks: shared folder `BstSharedFolder` or `windows` properties
   - QEMU / VirtualBox / x86: `/dev/socket/qemud`, `/dev/qemu_pipe`, `init.vbox86.rc`, `ueventd.vbox86.rc`, `ueventd.android_x86.rc`, `x86.prop`, `ueventd.ttVM_x86.rc`, `init.ttVM_x86.rc`
3. **`checkQEmuProps()` & `checkQEmuDrivers()`**: Reads system properties (`init.svc.qemud`, `init.svc.qemu-props`, `qemu.hw.mainkeys`, `qemu.sf.fake_camera`, `qemu.sf.lcd_density`, `ro.kernel.android.qemud`, `ro.kernel.qemu`) and `/proc/tty/drivers` for `"goldfish"`.
4. **`hasNoBluetooth()`**: Checks if `BluetoothAdapter.getDefaultAdapter() == null` (flagged as `"NoBluetooth"`).

### 4.3 Multi-Device Pending Approval Flow (`PendingDeviceManagerActivity`)
If an account logs in from a new `deviceUniqueId` while security restrictions apply:
1. The server can return `BlockedPendingDevice`.
2. The new device calls `POST /user/pendingdevice/{pendingDeviceId}/verify/v1` to request approval.
3. The primary logged-in device fetches awaiting devices via `GET /user/pendingdevice/list/v1?limit=3&lastId=` and approves the login via `PUT /user/pendingdevice/{pendingDeviceId}/`.

---

## 5. Native Onboarding, Registration, Login & Phone OTP Flow (`AuthHandler`)

### 5.1 Step-by-Step Native Registration Flow (`Register*Fragment`)
When a user creates a new account in the app, `AuthHandler` buffers each step's input into `ObscuredSharedPreferences` under the `"register"` category before making the final registration API call:
1. **`RegisterTermsFragment`**:
   - User accepts Terms (`TOS`, `PRIVACY`, `LOCATION`, `AGE`, optional `MARKETING`, `NIGHT`).
   - App calls `POST /tos/info/v1/` after registration or stores flags locally.
2. **`RegisterEmailFragment`**:
   - User enters email and password.
   - App validates password format (`PasswordUtil`) and calls `POST /user/checkEmail/v1` with `{"email": "<email>", "locale": "en-US"}` to verify the email is not already taken.
3. **`RegisterBasicInfoFragment`**:
   - User selects `name`, `gender` (`5` = Female, `6` = Male), and date of birth (`birthYear`, `birthMonth`, `birthDay`).
4. **`RegisterNationalityFragment`**:
   - User selects ISO-2 `nationalityCode` (e.g., `"KR"`, `"US"`, `"PK"`).
5. **`RegisterLanguageFirstFragment` & `RegisterLanguageInterestFragment`**:
   - User selects native language and target exchange languages (`languages` as comma-separated codes e.g. `"en,ko"`, and `levels` as comma-separated proficiency integers `1..5` e.g. `"5,3"`).
6. **`RegisterPurposeFragment`, `RegisterInterestFragment`, `RegisterPersonalColorFragment`**:
   - User optionally picks `purpose`, `purposeEtcDetail`, `interest`, and `color` (integer ID).
7. **`RegisterPhotoFragment`**:
   - User selects 1–6 profile photos.
   - Each photo is uploaded to AWS S3 via the 3-step S3 upload pipeline (see [Section 6](#6-aws-s3-presigned-image--voice-upload-pipeline-uploadinfo--uploadfiletos3)), producing an array of S3 photo URLs/keys (`photos`).
8. **`RegisterDescriptionFragment` (Final Submission)**:
   - User enters bio (`description`) and taps Complete.
   - `AuthHandler.register()` invokes `RestClient.userRegister` (`POST /user/register/email/v4`):
     ```json
     {
       "providerId": "user@example.com",
       "providerToken": "PlaintextPassword123!",
       "os": "Android v14",
       "platform": "android",
       "device": "BRAND: samsung, MODEL: SM-S918B, DEVICE: dm3q, PRODUCT: dm3qxxx, DISPLAY: UP1A.231005.007",
       "pushToken": "<fcm_token>",
       "deviceUniqueId": "9774d56d682e549c",
       "deviceLanguage": "en",
       "deviceRegion": "US",
       "simRegion": "US",
       "deviceGmtOffset": "+0500",
       "deviceRooted": 0,
       "deviceEmulator": 0,
       "deviceEmulatorInfo": "Normal",
       "appVersion": "7.1.7",
       "name": "Alex",
       "gender": 6,
       "color": 0,
       "birthYear": 2000,
       "birthMonth": 5,
       "birthDay": 15,
       "nationalityCode": "US",
       "languages": "en,ko",
       "levels": "5,2",
       "description": "Hello!",
       "photos": ["https://..."],
       "purpose": "",
       "purposeEtcDetail": "",
       "interest": "",
       "locale": "en-US"
     }
     ```
   - Server returns `{"token": "<Meeff-Access-Token>", "user": { ... }}`.
   - `AuthHandler` saves `token` (`setLoginToken`), caches `user` (`backupMe`), and triggers `AWSIotMqttClient.onAuthStateChanged()` to connect to live MQTT.

### 5.2 Login Flow (`POST /user/login/v4`)
- Email login calls `RestClient.userLoginEmail` (`POST /user/login/v4`) with:
  - `"provider": "email"`, `"email": "<email>"`, `"providerId": "<email>"`, `"providerToken": "<password>"`, plus the exact same 12 device fingerprint fields (`os`, `platform`, `device`, `pushToken`, `deviceUniqueId`, `deviceLanguage`, `deviceRegion`, `simRegion`, `deviceGmtOffset`, `deviceRooted`, `deviceEmulator`, `deviceEmulatorInfo`, `appVersion`, `locale`).
- OAuth logins (`google`, `facebook`, `apple`) call `/user/login/v4` with `"provider": "google" | "facebook" | "apple"` and the OAuth ID/access token in `providerId` / `providerToken`.

### 5.3 Phone SMS Verification Flow (`PhoneAuthActivity`)
- When triggered (for safety verification or profile phone linking):
  1. `POST /authorize/phone/send/v2` with `{"nationalityCode": "KR", "phoneNumber": "01012345678", "locale": "en-US"}`.
  2. User receives 6-digit SMS OTP and submits it via `POST /authorize/phone/verify/v1` with `{"code": "123456", "locale": "en-US"}`.

---

## 6. AWS S3 Presigned Image & Voice Upload Pipeline (`uploadInfo` & `uploadFileToS3`)

Meeff never uploads binary image or audio files directly to `api.meeff.com`. Instead, it uses a **3-Stage Direct-to-S3 Presigned POST Pipeline**:

### Step-by-Step Native Upload Procedure
1. **Step 1 — Request Presigned S3 Policy (`RestClient.uploadInfo`):**
   ```http
   POST https://api.meeff.com/api/upload/v1
   Meeff-Access-Token: <token>
   Content-Type: application/json; charset=utf-8

   {
     "category": "profile",
     "count": 1,
     "locale": "en-US"
   }
   ```
   *(Allowed `"category"` values in `RestClient`: `"profile"`, `"chat"`, `"report"`, `"voice"`).*
   - The server responds with an array of `UploadImageInfo` objects containing the target S3 bucket URL and signed policy fields (`x-amz-meta-uuid`, `acl`, `X-Amz-Algorithm`, `Content-Type`, `X-Amz-Date`, `Policy`, `X-Amz-Signature`, `X-Amz-Credential`, `key`).
2. **Step 2 — Direct Multipart Upload to AWS S3 (`RestClient.uploadFileToS3`):**
   - The app sends a `multipart/form-data` `POST` request directly to the S3 bucket URL returned in Step 1.
   - **Critical Field Ordering in `RestClient.uploadFileToS3`:** In AWS S3 presigned POSTs, all metadata fields **must** precede the `"file"` binary part:
     1. `x-amz-meta-uuid`
     2. `acl`
     3. `X-Amz-Algorithm`
     4. `Content-Type`
     5. `X-Amz-Date`
     6. `Policy`
     7. `X-Amz-Signature`
     8. `X-Amz-Credential`
     9. `key`
     10. `file` (raw JPEG/PNG/audio bytes with `application/octet-stream`)
3. **Step 3 — Commit S3 Key/URL to Meeff Backend:**
   - Once S3 returns `HTTP 204 No Content` / `200 OK`, the app sends the resulting S3 URL/key to:
     - `POST /user/edit/profile/v1` (in the `"photos"` array) for profile photos, OR
     - `POST /chat/send/photo/v2` (`{"chatRoomId": "<id>", "photo": "<s3_key>"}`) for chat photos.

---

## 7. 3D Face Verification & AWS Rekognition Liveness Architecture (`feature.faceverification`)

Meeff `7.1.7` implements biometric 3D Face Liveness and Profile Photo Matching inside `com.noyesrun.meeff.feature.faceverification.*` using **AWS Amplify FaceLivenessDetector (`com.amplifyframework.ui.liveness`)** backed by **AWS Rekognition**.

### Complete 4-Step Native Face Verification Procedure

```text
[App: FaceVerificationIntroFragment]
   │
   ├─► Step 1: GET /user/faceliveness/check/v1
   │     Checks if account is eligible & hasn't exceeded daily verification quota.
   │
[App: FaceVerificationPhotoFragment]
   │     User selects which profile photo (index 0..5 in UI -> 1..6 in API) matches their real face.
   │
   ├─► Step 2: GET /user/faceliveness/session/v1
   │     Meeff backend calls AWS Rekognition CreateFaceLivenessSession()
   │     and returns: { "sessionId": "<aws-liveness-uuid>" }
   │
[App: FaceVerificationDetectorFragment]
   │
   ├─► Step 3: AWS Amplify FaceLivenessDetector(sessionId, region)
   │     1. Opens front camera via CameraX.
   │     2. Streams real-time video over encrypted WebSocket directly to AWS Rekognition.
   │     3. Guides user to fit face in oval & flashes colored light sequence on screen.
   │     4. AWS Rekognition evaluates 3D depth + light reflection on skin and saves
   │        Liveness Status = SUCCEEDED + Reference Audit Image on AWS servers.
   │
   ├─► Step 4: POST /user/faceliveness/verify/v1
   │     Body: { "sessionId": "<aws-liveness-uuid>", "profileIndex": 1, "locale": "en-US" }
   │     1. Meeff backend calls AWS GetFaceLivenessSessionResults(sessionId).
   │     2. Confirms Liveness confidence score passed threshold.
   │     3. Calls AWS Rekognition CompareFaces() between the live AWS Reference Image
   │        and the user's profile photo at `profileIndex` (1-based: 1..6).
   │     4. If similarity passes, awards the Face Verified badge to the account!
```

### Detailed Breakdown of Each Step in Bytecode
1. **Step 1 — Eligibility Check (`RestClient.userFacelivenessCheck`):**
   - `GET https://api.meeff.com/user/faceliveness/check/v1?locale=en-US`
   - If the user has failed too many times today or is banned from verification, the backend returns `FaceVerificationDailyQuotaExceeded`, `FaceVerifyQuotaExceeded`, or `FaceVerifyPermanentBan`.
2. **Step 2 — Session Creation (`RestClient.userFacelivenessSeesion`):**
   - `GET https://api.meeff.com/user/faceliveness/session/v1?locale=en-US`
   - Returns `{"sessionId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}`.
3. **Step 3 — Live Camera Biometric Challenge (`FaceVerificationDetectorFragment`):**
   - Renders AWS Amplify's Jetpack Compose `FaceLivenessDetector` composable.
   - Note: This step talks directly to **AWS Rekognition endpoints**, not `api.meeff.com`. If this WebSocket video stream is skipped, the AWS session remains in `CREATED` state with no reference frame.
4. **Step 4 — Verification & Face Comparison (`RestClient.userFacelivenessVerify`):**
   - `POST https://api.meeff.com/user/faceliveness/verify/v1`
   - In bytecode (`RestClient.java` line `1409335`):
     ```java
     jsonObject.put("sessionId", sessionId);
     jsonObject.put("profileIndex", profileIndex + 1); // Converts 0-based UI index to 1-based index (1..6)
     ```
   - If AWS liveness succeeded AND the live face matches the profile photo at `profileIndex`, the user receives the Face Verified badge. If the user later deletes or replaces their verified primary photo, the badge is revoked.

---

## 8. GPS Location Engine & Mock-Location Detection (`LocationHandler`)

`com.noyesrun.meeff.util.LocationHandler` manages device coordinates for both profile distance calculation and the Explore feed.

### Step-by-Step Native Location Flow
1. **Acquiring Coordinates (`requestLocation` / `requestRealTimeLocation`):**
   - Requests `ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION`.
   - Uses Google Play Services `FusedLocationProviderClient` with fallback to Android's native `LocationManager` (`gps` and `network` providers) with a timeout (`LOCATION_TIMEOUT_IN_MILLISECONDS`).
2. **Mock / Fake GPS Detection (`LocationHandler.isFakeLocation(Location)`):**
   - Whenever the Android OS returns a `Location` object from `FusedLocationProviderClient` or `LocationManager`, `LocationHandler` calls `isFakeLocation(location)` (which checks `location.isFromMockProvider()` / ` location.isMock()`).
   - If a mock location app is active on the phone, `isFakeDetected_` is set to `true` and `emitFakeLocationDetected()` blocks the UI with a warning dialog.
   - **Important Architectural Note:** That check happens **client-side inside the Android app** on the `android.location.Location` object before calling the REST API. The REST API itself only receives raw numbers (`lat`, `lng`, `isCustomLoc`).
3. **Updating Server Location (`RestClient.userUpdateLocation`):**
   ```http
   POST https://api.meeff.com/user/updateLocation/v1
   Meeff-Access-Token: <token>
   Content-Type: application/json; charset=utf-8

   {
     "lat": 37.5665,
     "lng": 126.9780,
     "isCustomLoc": 0,
     "locale": "en-US"
   }
   ```
   - `isCustomLoc: 0` indicates standard GPS coordinates (100% free).

---

## 9. Explore Discovery Engine, Swipe Queue & Filter Rules (`ExploreHandler`, `AnswerQueueHandler`, `UserFilter`)

The Explore tab (`ExploreFragment`) is powered by two coordinated singletons: `ExploreHandler` (which fetches batches of users) and `AnswerQueueHandler` (which processes Like/Nope swipes asynchronously in the background).

### 9.1 Step-by-Step Native Explore & Swipe Lifecycle
1. **Building `unreachableUserIds`:**
   - Before fetching new cards, `ExploreHandler.queryExplore()` collects:
     - All user IDs currently sitting in the card stack (`usersExplored_`), AND
     - All user IDs currently queued in `AnswerQueueHandler.getUnreachableUserIds()` (users you just swiped on whose requests are still in flight).
   - These IDs are joined as a comma-separated string (`unreachableUserIds`) so the server does not return duplicate profiles you already have on screen.
2. **Fetching the Explore Batch (`RestClient.userExplore`):**
   ```http
   GET https://api.meeff.com/user/explore/v4?lat=37.5665&lng=126.9780&unreachableUserIds=id1,id2,id3&isCustomLoc=0&label=&labelLimit=0&locale=en-US
   Meeff-Access-Token: <token>
   ```
   - Returns `{ "users": [ ... ] }` (typically **7 user profiles** per batch).
3. **Asynchronous Swipe Queue (`AnswerQueueHandler.addAnswer`):**
   - In the native app, when a user taps **Like (Heart)** or **Nope (X)** on a card, the app does **not** block the UI waiting for an HTTP response.
   - Instead:
     1. `ExploreHandler.removeUserFromExplore(user)` immediately animates the card off screen.
     2. `AnswerQueueHandler.addAnswer(user, isOkay)` pushes the swipe into an in-memory `ArrayList` (`queue_`) and calls `iterateProcess()`.
     3. `iterateProcess()` pops one item at a time and calls `RestClient.userUndoableAnswer`:
        ```http
        GET https://api.meeff.com/user/undoableAnswer/v5?userId=<target_user_id>&isOkay=1&locale=en-US
        Meeff-Access-Token: <token>
        ```
        - `isOkay=1` -> Send Friend Request (Like)
        - `isOkay=0` -> Pass / Dislike (Nope)
     4. If the server returns `LikeExceeded`, `AnswerQueueHandler` clears the queue, calls `notifyOnLikeExceeded(user, responseJson)`, and displays the daily like limit / subscription dialog (`SuggestionSubsLikeDialog`).
     5. When the remaining card stack (`usersExplored_`) drops low, `ExploreHandler.queryExplore()` automatically fires the next `GET /user/explore/v4` batch!

---

## 10. Free Tier vs. Paid Tier (Meeff Plus / VIP / Ruby Items) Server Enforcement Matrix

Meeff validates account entitlements (`hasActiveSubscription`, `isVipUser`, `itemExploreBlockValidUntil`, `itemDeniedNationalityValidUntil`, `itemAdBlockValidUntil`) on the backend.

### 10.1 `/user/updateFilter/v1` — Parameter-by-Parameter Free vs. Paid Matrix

When `ExploreFilterActivity` saves filters, it calls `RestClient.userUpdateFilter` (`POST /user/updateFilter/v1`):

| JSON Parameter | Data Type | Free Tier Allowed Value(s) | Paid Tier (Meeff Plus / Ruby Item) Value(s) | Server Response if Violated on Free Account |
| :--- | :--- | :--- | :--- | :--- |
| `filterGenderType` | `int` | `5` (Female), `6` (Male), `7` (All) | `5`, `6`, `7` | **Free** — always allowed |
| `filterBirthYearFrom` | `int` | Any valid birth year (e.g., `1946` – `2007`) | Same | **Free** — always allowed |
| `filterBirthYearTo` | `int` | Any valid birth year (`currentYear - 18`, e.g., `2007`) | Same | **Free** — always allowed |
| `filterNationalityCodes` | `string` | `""` (All countries) **OR** **1 single ISO-2 country code** (e.g., `"KR"`, `"US"`, `"JP"`) | Multiple comma-separated ISO-2 country codes (e.g., `"KR,JP,TW"`) | `HTTP 403` `PremiumFilterPermissionDenied` if `>1` country code is sent |
| `filterDistance` | `int` | **`510`** (`510` = Unlimited / Worldwide default in APK) | `5` – `500` (km radius filter) | `HTTP 403` `PremiumFilterPermissionDenied` if `< 510` |
| `filterLanguageCodes` | `string` | **`""`** (Empty string = All languages) | Comma-separated language codes (e.g., `"en,ko"`) | `HTTP 403` `PremiumFilterPermissionDenied` if non-empty |
| `filterNationalityBlock` | `int` (`0`\|`1`) | **`0`** (`false`) | `1` (`true` — hide users from your own nationality; requires `itemExploreBlockValidUntil` or Plus) | `HTTP 403` `PremiumFilterPermissionDenied` if `1` |
| `filterDeniedNationality` | `string` | **Omitted or `""`** | Comma-separated excluded ISO-2 country codes (requires `itemDeniedNationalityValidUntil` or Plus) | `HTTP 403` `PremiumFilterPermissionDenied` if non-empty |
| `filterPurposes` | `string` | **Omitted or `""`** | Comma-separated relationship/language purpose IDs | `HTTP 403` `PremiumFilterPermissionDenied` if non-empty |
| `filterMinProfileImageCount` | `int` | **`2`** (Native default hardcoded in `ExploreFilterActivity`) | `3`, `4`, `5`, `6` | `HTTP 403` `PremiumFilterPermissionDenied` if `!= 2` |
| `filterFaceVerifiedOnly` | `boolean` | **`false`** (`0`) | `true` (`1` — only show Face Verified profiles) | `HTTP 403` `PremiumFilterPermissionDenied` if `true` |
| `filterRecentlyActiveOnly` | `boolean` | **`false`** (`0`) | `true` (`1` — only show recently active profiles) | `HTTP 403` `PremiumFilterPermissionDenied` if `true` |

#### Resetting Filters to Default (`RestClient.userUpdateFilterReset`)
```http
POST https://api.meeff.com/user/updateFilter/v1
Meeff-Access-Token: <token>
Content-Type: application/json; charset=utf-8

{
  "reset": 1,
  "locale": "en-US"
}
```

### 10.2 App-Wide Feature Entitlement Matrix (Free vs. Paid)
| Feature | Endpoint | Free Tier Access | Paid / Subscription / Ruby Requirement |
| :--- | :--- | :--- | :--- |
| **Explore Card Feed** | `GET /user/explore/v4` | Full access (~7 profiles per request) | Same |
| **Send Friend Request** | `GET /user/undoableAnswer/v5?isOkay=1` | Free up to daily/rolling limit (`LikeExceeded`) | Unlimited likes with Meeff Plus or Ruby recharge |
| **Skip / Pass User** | `GET /user/undoableAnswer/v5?isOkay=0` | Unlimited & free | Same |
| **Update GPS Coordinates** | `POST /user/updateLocation/v1` | Free when `isCustomLoc: 0` | Same |
| **Lounge View & Match Accept** | `GET /lounge/dashboard/v1`, `POST /lounge/more/v1`, `POST /chatroom/open/v2` | 100% Free to view incoming requests (`one`), mutual matches (`both`), and open chatrooms | Bulk cleanup (`POST /lounge/cleanup/v1`) requires Rubies/Plus |
| **Direct Chat & Photo Send** | `POST /chat/send/v2`, `POST /chat/send/photo/v2` | 100% Free inside any open chatroom | Auto-translation (`POST /chat/send/translate/v1`) requires Translator subscription or Rubies |
| **Profile Visitors List** | `GET /user/profile/visit/list/v1` | Returns blurred/locked list (`isVisiable=false`) | Unlocks full visitor identities (`isVisiable=true`) with Plus or Visitor item |
| **Face Recommendation** | `GET /user/face/recommendation/v1` | Blocked with `RecommendedUserAccessDenied` unless Face Verified / eligible | Accessible after Face Verification + daily quota (`RecommendationQuotaExceed`) |
| **Today Spotlight Bidding** | `POST /today/bid/v2` | Requires Rubies (`NotEnoughRuby` if balance is 0) | Spends Rubies to feature profile on `/today/list/v1` |
| **FaceTalk Random Call** | `POST /facetalk/partner/random/v1` | Free when `gender: "ALL"`, `purchaseType: "FREE"`, `purchaseValue: 0` | Filtering partner gender (`"5"` or `"6"`) requires `purchaseType: "RUBY"` |

---

## 11. Lounge (Waiting Room) & Chatroom Messaging Engine (`LoungeHandler` & `ChatHandler`)

### 11.1 How the Lounge (Waiting Room) Works Step-by-Step (`LoungeHandler`)
The Lounge tab displays two categories of users (`WaitingRoom` model):
- **`one`**: Users who sent you a friend request (`isOkay=1`), waiting for you to open a chatroom or remove them.
- **`both`**: Mutual matches (both you and the other user liked each other), waiting for either party to open the chatroom.

1. **Initial Load (`RestClient.loungeDashboard`):**
   - `GET https://api.meeff.com/lounge/dashboard/v1?locale=en-US`
   - Returns the first page of `both` and `one` waiting room users plus total counts.
2. **Pagination (`RestClient.loungeBothMore` & `RestClient.loungeOneMore`):**
   - As the user scrolls down, `LoungeHandler.queryBothMore()` or `queryOneMore()` takes the `createdAt` timestamp (`fromDate`) of the last item and calls:
     ```http
     POST https://api.meeff.com/lounge/more/v1
     {
       "loungeType": "one",
       "fromDate": "2026-11-19T10:00:00.000Z",
       "locale": "en-US"
     }
     ```
     *(Or `"loungeType": "both"` for mutual matches).*
3. **Accepting a Request / Opening a Chatroom (`RestClient.openChatRoom`):**
   - When the user taps **"Start Chat"** on a profile in the Lounge, the app calls:
     ```http
     POST https://api.meeff.com/chatroom/open/v2
     {
       "waitingRoomId": "<target_user_id>",
       "locale": "en-US"
     }
     ```
   - **Important Detail:** `waitingRoomId` is the target user's `_id` (from the Lounge item).
   - The server removes the user from the Lounge, creates (or re-opens) a `ChatRoom`, returns `{ "chatRoom": { "_id": "<chatRoomId>", ... } }`, and pushes a `chatRoomAdded` MQTT/FCM event to the partner!
4. **Declining / Removing a User from Lounge (`RestClient.loungeRemove` / `loungeDeleteUser`):**
   - Single or comma-separated user IDs can be removed via:
     ```http
     POST https://api.meeff.com/lounge/remove/v1
     {
       "userId": "<id1,id2>",
       "locale": "en-US"
     }
     ```

### 11.2 How Chatrooms & Messaging Work Step-by-Step (`ChatHandler`)
1. **Loading the Chatroom List (`ChatHandler.queryDashboard` & `queryMoreChatRooms`):**
   - Calls `GET /chatroom/dashboard/v1?locale=en-US` to fetch the latest active `rooms`.
   - To load older chatrooms, calls `POST /chatroom/more/v1` with `{"fromDate": "<last_room_timestamp>", "locale": "en-US"}`.
2. **Opening a Specific Chatroom (`ChatActivity`):**
   - Fetches room metadata via `GET /chatroom/room/{chatRoomId}?locale=en-US`.
   - Fetches anti-scam warning keywords via `GET /user/scammer/words/v1?userId=<partnerId>&locale=en-US`.
   - Loads messages via `GET /chat/list/v1?chatRoomId=<chatRoomId>&lastChatId=&locale=en-US` (passing `lastChatId` paginates older messages).
3. **Marking Messages as Read (`ChatHandler.sendRead`):**
   - Calls `POST /chat/read/v1` with `{"chatRoomId": "<chatRoomId>", "chatId": "<latest_chat_id>", "locale": "en-US"}`.
   - The server immediately sends a `chatRead` MQTT message to the partner's device.
4. **Sending Messages Queue (`ChatHandler.sendingQueue_`):**
   - Outgoing messages are wrapped in `ChatHandler$ChatToSend` and processed sequentially by `iterateSendQueue()`:
     - **Text Message (`RestClient.chatSend`):**
       ```http
       POST https://api.meeff.com/chat/send/v2
       {
         "chatRoomId": "<chatRoomId>",
         "message": "Hello!",
         "locale": "en-US"
       }
       ```
     - **Photo Message (`RestClient.chatSendPhoto`):**
       First uploads image to S3 via `/api/upload/v1` (`category: "chat"`), then calls:
       ```http
       POST https://api.meeff.com/chat/send/photo/v2
       {
         "chatRoomId": "<chatRoomId>",
         "photo": "<s3_uploaded_photo_key>",
         "locale": "en-US"
       }
       ```
     - **Translated Message (`RestClient.chatSendTranslate`):**
       ```http
       POST https://api.meeff.com/chat/send/translate/v1
       {
         "chatRoomId": "<chatRoomId>",
         "message": "Hello!",
         "froms": "en",
         "to": "ko",
         "locale": "en-US"
       }
       ```
5. **Leaving / Deleting a Chatroom (`RestClient.chatRoomUnsubscribe`):**
   - When a user exits a chatroom (`DialogChatroomExitBinding`), the app calls:
     ```http
     POST https://api.meeff.com/chatroom/unsubscribe/v1
     {
       "chatRoomId": "<chatRoomId>",
       "locale": "en-US"
     }
     ```
   - Rooms where the partner left can be viewed in `RestoreChatRoomActivity` via `GET /chatRoom/left/v2?page=0&locale=en-US`.

---

## 12. WebRTC 1:1 Video & Audio Calling (`FaceTalk`, `VibeMeet`, `VoiceBloom` & `PeerConnectionManager`)

Meeff supports three real-time WebRTC calling modes built on `com.noyesrun.meeff.util.apprtc.PeerConnectionManager`:
1. **FaceTalk Random (`ActivityFaceTalkBinding`)**: 1:1 random video chat.
2. **VibeMeet (`VibeMeetActivityViewModel`)**: 1:1 direct video call with online users listed on the VibeMeet board (`/facetalk/vibemeet/list/v1`) or inside an existing chatroom (`/facetalk/partner/chatroom/v2`).
3. **VoiceBloom (`VoiceBloomMainActivity`)**: 1:1 random anonymous voice-only call with a countdown timer and question cards.

### Step-by-Step Native WebRTC Signaling & Call Flow
1. **Step 1 — Matchmaking / Room Creation:**
   - **Random FaceTalk:** `POST /facetalk/partner/random/v1` with `{"gender": "ALL", "purchaseType": "FREE", "purchaseValue": 0}`.
   - **Direct VibeMeet:** `POST /facetalk/partner/vibemeet/v2` with `{"targetId": "<userId>"}` (sends a `facetalk_notify` MQTT/FCM ring to `targetId`).
   - **Chatroom Video Call:** `POST /facetalk/partner/chatroom/v2` with `{"chatroomId": "<chatRoomId>"}`.
   - **Random VoiceBloom:** `POST /voicebloom/partner/random/v1` with `{"genderFilter": "ALL"}`.
   - All matchmaking endpoints return a `roomId` and STUN/TURN ice server configuration.
2. **Step 2 — Fetching Partner & Room Metadata:**
   - Calls `POST /facetalk/partner/info/v2` (or `POST /voicebloom/partner/info/v1`) with `{"roomId": "<roomId>"}`.
3. **Step 3 — WebRTC SDP & ICE Candidate Exchange (`FaceTalkSignalingManager` / `VoiceBloomSignalingManager`):**
   - `PeerConnectionManager` creates a local WebRTC `PeerConnection`, captures camera/microphone tracks (`PeerAudioManager`, `PeerBluetoothManager`), and generates an SDP `offer` (or `answer`).
   - **Outgoing Signaling:** Sent via HTTP REST:
     - Video (`FaceTalk` / `VibeMeet`):
       ```http
       POST https://api.meeff.com/facetalk/signaling/v2
       {
         "roomId": "<roomId>",
         "cmd": "offer" | "answer" | "ice" | "iceremove",
         "data": "<sdp_or_ice_json_string>",
         "locale": "en-US"
       }
       ```
     - Audio (`VoiceBloom`):
       ```http
       POST https://api.meeff.com/voicebloom/signaling/v1
       {
         "roomId": "<roomId>",
         "cmd": "offer" | "answer" | "ice",
         "data": "<sdp_or_ice_json_string>",
         "locale": "en-US"
       }
       ```
   - **Incoming Signaling:** Delivered in real time to the partner's device over **AWS IoT MQTT** (`meeff/user/<id>`) and routed through `SignalingHandler.onReceiveSignalData(json)` directly to `PeerConnectionManager`!
4. **Step 4 — Call Teardown & Post-Call Rating:**
   - Hanging up calls `POST /facetalk/partner/close/v2` (or `POST /voicebloom/partner/close/v1`) with `{"roomId": "<roomId>"}`, which also emits a `"cmd": "close"` MQTT event to the peer.
   - After the call ends, the user can rate the partner via `POST /facetalk/review/v1` (`{"targetId": "<id>", "rating": 5, "tags": "..."}`).

---

## 13. BlindMatch (Group Speed-Dating) State Machine

BlindMatch (`ActivityBlindMatchBinding`) is a timed speed-dating mini-app inside Meeff. Its native lifecycle operates in 7 steps:

1. **Session Login:** `POST /blindmatch/login/v2` — initializes the user's BlindMatch profile and checks availability.
2. **Enter Queue:** `POST /blindmatch/enter/v3` — places the user into an active BlindMatch channel and returns `channelId`.
3. **Poll / Sync Channel State:** `GET /blindmatch/info/v3?channelId=<channelId>&locale=en-US` — fetches participants, anonymous chat state, and round timer.
4. **In-Round Translation (Optional):** `POST /blindmatch/translate/v1` with `{"message": "<text>", "langTo": "en"}`.
5. **Select Partner:** `POST /blindmatch/select/v3` with `{"channelId": "<channelId>", "targetId": "<chosen_user_id>"}` — submits the user's secret choice before the round timer ends.
6. **Check Match Result:** `GET /blindmatch/result/v3?channelId=<channelId>&locale=en-US` — reveals whether a mutual match occurred.
7. **Leave & Review:** `POST /blindmatch/leave/v3` (`{"channelId": "<channelId>"}`) and `POST /blindmatch/review/v1` (`{"targetId": "<id>", "rating": 5}`).

---

## 14. Shop, Ruby Economy, Google Play Billing (`IabVerifyReceiptHandler`) & Today Spotlight Bidding

### 14.1 Shop & Ruby Inventory Flow (`ShopActivity`)
1. **Load Wallet & Catalog (`RestClient.userShopInfo`):**
   - `POST /shop/info/v1` returns the user's current Ruby balance, active item expiration dates, and purchasable items (`ShopData`, `ShopItem`, `RechargeItem`).
   - Expiring free Rubies are queried via `GET /user/ruby/list/expiring/v1?lastExpirationDate=<iso_date>&locale=en-US`, and transaction history via `POST /user/ruby/history/v1` (`{"lastId": "", "limit": 20}`).
2. **Google Play In-App Purchase Verification (`IabVerifyReceiptHandler`):**
   - When a user buys Rubies or a Meeff Plus subscription via Google Play Billing (`BillingClient`), the signed purchase JSON receipt is sent to:
     - One-time Ruby pack (`RestClient.inappVerifyReceipt`):
       ```http
       POST https://api.meeff.com/shop/purchase/iab/v1
       {
         "receipt": "<google_play_original_json>",
         "platform": "android",
         "locale": "en-US"
       }
       ```
     - Subscription (`RestClient.subsVerifyReceipt`):
       ```http
       POST https://api.meeff.com/shop/purchase/iab/v1
       {
         "receipt": "<google_play_original_json>",
         "platform": "android",
         "isSubscription": true,
         "locale": "en-US"
       }
       ```
3. **Buying & Using Shop Items with Rubies:**
   - Buy item with Rubies: `POST /shop/buyItem/v2` with `{"itemId": "<id>", "platform": "android", "meta1": "", "meta2": "", "meta3": ""}`.
   - Activate/consume an item from inventory: `POST /shop/useItem/v1` with `{"itemId": "<id>", "platform": "android", "targetId": "<optional_target_id>"}`.

### 14.2 Today Spotlight Bidding (`TodayFragment`)
- The **Today** tab displays spotlighted users who bid Rubies to promote their profile:
  1. `GET /today/list/v1?page=0&limit=20&locale=en-US` fetches the spotlighted profiles (`TodayUser`).
  2. When a viewer taps a profile on the Today tab, the app logs the impression via `POST /user/profile/visit/v1` with `{"targetId": "<userId>", "exposureType": "today"}`.
  3. A user can bid Rubies to place their own profile on the Today tab via `POST /today/bid/v2` (`{"platform": "android", "ruby": <bid_amount>}`) and check their impression analytics via `POST /today/report/v1`.

---

## 15. A-to-Z Master Reference of All 125 REST API Endpoints

Every endpoint below is extracted directly from `com.noyesrun.meeff.net.RestClient` in Meeff `7.1.7`.

### 15.1 App Initialization, S3 Upload & Terms of Service
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 1 | `apiInit` | `POST` | `/api/init/v2` | `{"platform": "android", "version": "7.1.7", "locale": "en-US"}` | **Free.** Version handshake, maintenance status, and startup notices. |
| 2 | `uploadInfo` | `POST` | `/api/upload/v1` | `{"category": "profile" \| "chat" \| "report" \| "voice", "count": 1, "locale": "en-US"}` | **Free.** Issues AWS S3 presigned POST policy & credentials. |
| 3 | `uploadFileToS3` | `POST` | `<s3_presigned_url>` | `multipart/form-data`: `x-amz-meta-uuid`, `acl`, `X-Amz-Algorithm`, `Content-Type`, `X-Amz-Date`, `Policy`, `X-Amz-Signature`, `X-Amz-Credential`, `key`, `file` | **Free.** Uploads binary media file directly to AWS S3. |
| 4 | `getAppTermsInfo` | `GET` | `/tos/info/v1/` | `?locale=en-US` | **Free.** Fetches user's Terms of Service consent status. |
| 5 | `setAppTermsInfo` | `POST` | `/tos/info/v1/` | `{"TOS": true, "PRIVACY": true, "LOCATION": true, "MARKETING": false, "NIGHT": false, "AGE": true}` | **Free.** Saves Terms of Service consent flags. |
| 6 | `footageReport` | `POST` | `/tos/footage/report` | `{"category": "...", "username": "...", "phone": "...", "email": "...", "msg": "...", "targetId": "..."}` | **Free.** Submits legal/footage abuse report. |
| 7 | `findMatchingCount` | `GET` | `/misc/findmatching/count/v1` | `?locale=en-US` | **Free.** Returns global matching count for UI display. |

### 15.2 Authentication, Registration, Login & Device Security
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 8 | `userCheckEmail` | `POST` | `/user/checkEmail/v1` | `{"email": "<email>", "locale": "en-US"}` | **Free.** Checks if an email is available for registration. |
| 9 | `userCheckGoogleIdEmail` | `POST` | `/user/checkGoogleIdEmail/v1` | `{"googleId": "...", "googleEmail": "..."}` | **Free.** Checks if Google account is already registered. |
| 10 | `userCheckFacebookIdEmail` | `POST` | `/user/checkFacebookIdEmail/v1` | `{"facebookId": "...", "facebookEmail": "..."}` | **Free.** Checks if Facebook account is already registered. |
| 11 | `userCheckAppleIdEmail` | `POST` | `/user/checkAppleIdEmail/v1` | `{"appleToken": "..."}` | **Free.** Checks if Apple ID is already registered. |
| 12 | `userRegister` | `POST` | `/user/register/email/v4` | Full profile + 12 device fingerprint fields (see [Section 5.1](#51-step-by-step-native-registration-flow-registerfragment)). | **Free.** Registers a new email account and returns `token` + `user`. |
| 13 | `userRegisterGoogle` | `POST` | `/user/register/google/v4` | Same as above + `"providerEmail": "<email>"` | **Free.** Registers a new account via Google OAuth. |
| 14 | `userRegisterFacebook` | `POST` | `/user/register/facebook/v4` | Same as above + `"providerEmail": "<email>"` | **Free.** Registers a new account via Facebook OAuth. |
| 15 | `userRegisterApple` | `POST` | `/user/register/apple/v4` | Same as above (`"providerToken"` only) | **Free.** Registers a new account via Apple Sign-In. |
| 16 | `userLogin` / `userLoginEmail` | `POST` | `/user/login/v4` | `{"provider": "email", "email": "...", "providerId": "...", "providerToken": "<password>", ...deviceFields}` | **Free.** Logs into existing account and returns `token`. |
| 17 | `userLogout` | `POST` | `/user/logout/v2` | `{"deviceInfo": "...", "locale": "en-US"}` | **Free.** Logs out and unbinds push session. |
| 18 | `userResendEmailVerification` | `POST` | `/user/resendEmailVerification/v1` | `{"locale": "en-US"}` | **Free.** Resends verification email to account address. |
| 19 | `userResetPassword` | `POST` | `/user/resetPassword/v2` | `{"email": "<email>", "locale": "en-US"}` | **Free.** Sends password reset email. |
| 20 | `userCheckPasswordUpdate` | `GET` | `/user/checkPasswordUpdate/v1` | `?locale=en-US` | **Free.** Checks if account is due for a password update prompt. |
| 21 | `userDelayPasswordUpdate` | `POST` | `/user/delayPasswordUpdate/v1` | `{"locale": "en-US"}` | **Free.** Postpones password update reminder. |
| 22 | `userEditPasswordUpdate` | `POST` | `/user/edit/password/update/v1` | `{"password": "<new>", "oldPassword": "<old>"}` | **Free.** Changes account password. |
| 23 | `userSwitchToEmailAccount` | `POST` | `/user/switchToEmailAccount/v1` | `{"email": "<email>"}` | **Free.** Converts social login account to email login. |
| 24 | `userSwitchToFacebookAccount` | `POST` | `/user/switchToFacebookAccount/v1` | `{"facebookId": "...", "facebookEmail": "...", "facebookToken": "..."}` | **Free.** Links account to Facebook login. |
| 25 | `authorizePhoneSend` | `POST` | `/authorize/phone/send/v2` | `{"nationalityCode": "KR", "phoneNumber": "..."}` | **Free (Quota Limited).** Sends SMS OTP code. |
| 26 | `authorizePhoneVerify` | `POST` | `/authorize/phone/verify/v1` | `{"code": "123456"}` | **Free.** Verifies SMS OTP code. |
| 27 | `userEditPhoneNumberUpdate` | `POST` | `/edit/phoneNumber/update/v1` | `{"phoneNumber": "..."}` | **Free.** Updates user's phone number. |
| 28 | `userPendingDeviceList` | `GET` | `/user/pendingdevice/list/v1` | `?limit=3&lastId=<id>&locale=en-US` | **Free.** Lists secondary devices awaiting login approval. |
| 29 | `userPendingDeviceVerify` | `POST` | `/user/pendingdevice/{id}/verify/v1` | `{"locale": "en-US"}` | **Free.** Requests approval for a pending device login. |
| 30 | `userPendingDeviceApproved` | `PUT` | `/user/pendingdevice/{id}/` | `{"locale": "en-US"}` | **Free.** Approves a pending device login. |
| 31 | `updatePushInfo` | `POST` | `/user/updatePushInfo/v1` | `{"platform": "android", "pushToken": "<fcm_token>"}` | **Free.** Registers FCM push token with Meeff backend. |

### 15.3 Discovery, Explore, Swiping/Requests & Search Filters
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 32 | `userExplore` | `GET` | `/user/explore/v4` | `?lat=<lat>&lng=<lng>&unreachableUserIds=<csv>&isCustomLoc=0&label=&labelLimit=0&locale=en-US` | **Free.** Fetches ~7 discovery profiles per call, excluding IDs in `unreachableUserIds`. |
| 33 | `exploreHotPick` | `POST` | `/user/explore/hotPick` | `{"unexplorableUserIds": ["..."], "locale": "en-US"}` | **Free / Quota.** Fetches curated HotPick profiles. |
| 34 | `userUndoableAnswer` | `GET` | `/user/undoableAnswer/v5` | `?userId=<id>&isOkay=1\|0&locale=en-US` | **Free (Daily Like Quota).** `isOkay=1` sends friend request; `isOkay=0` passes user. |
| 35 | `userAnswers` | `POST` | `/user/answers/v1` | `{"lastCreated": "<iso_date>", "locale": "en-US"}` | **Free.** Lists recently liked/answered users. |
| 36 | `userUpdateFilter` | `POST` | `/user/updateFilter/v1` | See [Section 10.1](#101-userupdatefilterv1--parameter-by-parameter-free-vs-paid-matrix). | **Hybrid.** Updates explore search filters. |
| 37 | `userUpdateFilterReset` | `POST` | `/user/updateFilter/v1` | `{"reset": 1, "locale": "en-US"}` | **Free.** Resets explore filters to default values. |
| 38 | `userUpdateLocation` | `POST` | `/user/updateLocation/v1` | `{"lat": 37.5665, "lng": 126.9780, "isCustomLoc": 0, "locale": "en-US"}` | **Free.** Updates stored GPS coordinates on backend. |
| 39 | `userPopular` | `GET` | `/user/popular/nation/v2` | `?locale=en-US` | **Free.** Lists currently popular nationality codes. |
| 40 | `userFaceRecommendation` | `GET` | `/user/face/recommendation/v1` | `?locale=en-US` | **Face-Verified / Paid Gate.** Fetches AI face-similarity recommendations. |

### 15.4 User Profile, Visitors, Blocking, Reporting & Face Liveness
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 41 | `updateUserInfo` | `GET` | `/user/info/v1` | `?userId=<optional_id>&locale=en-US` | **Free.** Fetches own account state (if `userId` omitted) or another user's public profile. |
| 42 | `userEditProfile` | `POST` | `/user/edit/profile/v1` | `{"name": "...", "birthYear": 2000, "birthMonth": 1, "birthDay": 1, "gender": 5\|6, "nationalityCode": "US", "description": "...", "languages": "en", "levels": "5", "photos": [...], "purpose": "...", "interest": "..."}` | **Free.** Updates user profile details and S3 photo URLs. |
| 43 | `userEditSetAlarm` | `POST` | `/edit/setAlarm/v1` | `{"unpushAll": false, "unpushBoth": false, "unpushOne": false, "unpushChat": false}` | **Free.** Updates global push notification toggles. |
| 44 | `userProfileVisit` | `POST` | `/user/profile/visit/v1` | `{"targetId": "<user_id>"}` | **Free.** Logs a profile view on `targetId`. |
| 45 | `userTodayProfileVisit` | `POST` | `/user/profile/visit/v1` | `{"targetId": "<user_id>", "exposureType": "today"}` | **Free.** Logs a profile view originating from the Today tab. |
| 46 | `userProfileVisitList` | `GET` | `/user/profile/visit/list/v1` | `?pageSize=20&lastId=<id>&isVisiable=true\|false&locale=en-US` | **Hybrid.** Lists users who visited your profile (`isVisiable=true` requires Plus/Visitor item). |
| 47 | `userBlock` | `POST` | `/user/blockedbyuser/v1` | `{"targetUserId": "<user_id>"}` | **Free.** Blocks a user. |
| 48 | `userBlockedInfo` | `GET` | `/user/blockedbyuser/v1` | `?locale=en-US` | **Free.** Lists all blocked users. |
| 49 | `userReport` | `POST` | `/user/report/v2` | `{"category": "...", "userId": "<target_id>", "data": "...", "roomId": "..."}` | **Free.** Reports a user for policy violations. |
| 50 | `userScammerWords` | `GET` | `/user/scammer/words/v1` | `?userId=<id>&locale=en-US` | **Free.** Fetches anti-scam keyword triggers for chat warning banners. |
| 51 | `userScammerWordsSuggestion` | `POST` | `/user/scammer/words/suggestion/v1` | `{"words": "word1,word2"}` | **Free.** Submits suggested scammer keywords. |
| 52 | `userFacelivenessCheck` | `GET` | `/user/faceliveness/check/v1` | `?locale=en-US` | **Free.** Checks Face Verification eligibility and daily quota. |
| 53 | `userFacelivenessSeesion` | `GET` | `/user/faceliveness/session/v1` | `?locale=en-US` | **Free.** Creates AWS Rekognition Face Liveness session (`sessionId`). |
| 54 | `userFacelivenessVerify` | `POST` | `/user/faceliveness/verify/v1` | `{"sessionId": "<aws_session_id>", "profileIndex": 1}` | **Free (Requires AWS 3D Stream).** Verifies completed AWS liveness session against profile photo `1..6`. |
| 55 | `userRequest` | `POST` | `/user/post/v1` | `{"message": "..."}` | **Free.** Sends feedback/support message. |
| 56 | `userLeave` | `POST` | `/user/leave/v1` | `{"message": "<reason>"}` | **Free.** Permanently deletes the user's account. |

### 15.5 Lounge (Waiting Room)
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 57 | `loungeDashboard` | `GET` | `/lounge/dashboard/v1` | `?locale=en-US` | **Free.** Fetches initial Lounge lists: `both` (mutual matches) and `one` (incoming requests). |
| 58 | `loungeBothMore` | `POST` | `/lounge/more/v1` | `{"loungeType": "both", "fromDate": "<iso_date>", "locale": "en-US"}` | **Free.** Paginates mutual matches in Lounge. |
| 59 | `loungeOneMore` | `POST` | `/lounge/more/v1` | `{"loungeType": "one", "fromDate": "<iso_date>", "locale": "en-US"}` | **Free.** Paginates incoming likes in Lounge. |
| 60 | `loungeRemove` / `loungeDeleteUser` | `POST` | `/lounge/remove/v1` | `{"userId": "<id1,id2>", "locale": "en-US"}` | **Free.** Declines/removes users from Lounge. |
| 61 | `loungeCleanUp` | `POST` | `/lounge/cleanup/v1` | `{"target": "one" \| "both"}` | **Paid / Ruby Item.** Bulk cleans up Lounge waiting room. |

### 15.6 Chatrooms & Direct Messaging
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 62 | `chatRoomDashboard` | `GET` | `/chatroom/dashboard/v1` | `?locale=en-US` | **Free.** Fetches first page of active chatrooms. |
| 63 | `chatRoomMore` | `POST` | `/chatroom/more/v1` | `{"fromDate": "<iso_date>", "locale": "en-US"}` | **Free.** Paginates older active chatrooms. |
| 64 | `openChatRoom` | `POST` | `/chatroom/open/v2` | `{"waitingRoomId": "<target_user_id>", "locale": "en-US"}` | **Free.** Opens a 1:1 chatroom with a user from the Lounge. |
| 65 | `getChatroom` | `GET` | `/chatroom/room/{chatRoomId}` | `?locale=en-US` | **Free.** Fetches details for a specific chatroom. |
| 66 | `chatRoomUnsubscribe` | `POST` | `/chatroom/unsubscribe/v1` | `{"chatRoomId": "<room_id>", "locale": "en-US"}` | **Free.** Leaves/deletes a chatroom. |
| 67 | `chatRoomLeft` | `GET` | `/chatRoom/left/v2` | `?page=0&locale=en-US` | **Free.** Lists chatrooms where the partner left (for restore). |
| 68 | `chatRoomSetAlarm` | `POST` | `/chatroom/setAlarm/v1` | `{"chatRoomId": "<room_id>", "isOn": 1\|0}` | **Free.** Toggles push notifications for a specific chatroom. |
| 69 | `chatList` | `GET` | `/chat/list/v1` | `?chatRoomId=<room_id>&lastChatId=<msg_id>&locale=en-US` | **Free.** Fetches paginated chat messages in a room. |
| 70 | `getChat` | `POST` | `/chat/get/v1` | `{"chatRoomId": "<room_id>", "chatId": "<msg_id>"}` | **Free.** Fetches a single chat message by ID. |
| 71 | `chatSend` | `POST` | `/chat/send/v2` | `{"chatRoomId": "<room_id>", "message": "<text>", "locale": "en-US"}` | **Free.** Sends a text message to a chatroom. |
| 72 | `chatSendPhoto` | `POST` | `/chat/send/photo/v2` | `{"chatRoomId": "<room_id>", "photo": "<s3_key>"}` | **Free.** Sends an S3-uploaded photo to a chatroom. |
| 73 | `chatSendTranslate` | `POST` | `/chat/send/translate/v1` | `{"chatRoomId": "<room_id>", "message": "<text>", "froms": "en", "to": "ko"}` | **Paid (Translator / Rubies).** Translates and sends a chat message. |
| 74 | `chatRead` | `POST` | `/chat/read/v1` | `{"chatRoomId": "<room_id>", "chatId": "<last_msg_id>"}` | **Free.** Marks chatroom messages up to `chatId` as read. |
| 75 | `chatPing` | `POST` | `/chat/ping/v1` | `{"uid": "<user_id>", "did": "<device_id>"}` | **Free.** Foreground keep-alive heartbeat for active chat/MQTT state. |

### 15.7 BlindMatch (Speed Dating)
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 76 | `blindmatchLogin` | `POST` | `/blindmatch/login/v2` | `{"locale": "en-US"}` | **Free.** Initializes BlindMatch state. |
| 77 | `blindmatchEnter` | `POST` | `/blindmatch/enter/v3` | `{"locale": "en-US"}` | **Free / Quota.** Joins BlindMatch matchmaking queue. |
| 78 | `blindmatchInfo` | `GET` | `/blindmatch/info/v3` | `?channelId=<id>&locale=en-US` | **Free.** Fetches current BlindMatch channel state. |
| 79 | `blindmatchSelect` | `POST` | `/blindmatch/select/v3` | `{"channelId": "<id>", "targetId": "<user_id>"}` | **Free.** Selects partner during BlindMatch round. |
| 80 | `blindmatchResult` | `GET` | `/blindmatch/result/v3` | `?channelId=<id>&locale=en-US` | **Free.** Fetches round match result. |
| 81 | `blindmatchLeave` | `POST` | `/blindmatch/leave/v3` | `{"channelId": "<id>"}` | **Free.** Leaves BlindMatch channel. |
| 82 | `blindmatchReview` | `POST` | `/blindmatch/review/v1` | `{"targetId": "<user_id>", "rating": 5}` | **Free.** Submits post-round partner rating. |
| 83 | `blindmatchTranslate` | `POST` | `/blindmatch/translate/v1` | `{"message": "<text>", "langTo": "en"}` | **Paid / Quota.** Translates text inside BlindMatch. |

### 15.8 FaceTalk & VibeMeet (1:1 Video Calling)
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 84 | `faceTalkPartnerRandom` | `POST` | `/facetalk/partner/random/v1` | `{"gender": "ALL"\|"5"\|"6", "purchaseType": "FREE"\|"RUBY", "purchaseValue": 0}` | **Hybrid.** Matches with a random video partner (`ALL` + `FREE` is free). |
| 85 | `faceTalkPartnerVibeMeet` | `POST` | `/facetalk/partner/vibemeet/v2` | `{"targetId": "<user_id>"}` | **Paid / Ruby.** Calls a specific user from the VibeMeet board. |
| 86 | `faceTalkPartnerChatroom` | `POST` | `/facetalk/partner/chatroom/v2` | `{"chatroomId": "<room_id>"}` | **Free / Hybrid.** Starts a video call inside an existing chatroom. |
| 87 | `faceTalkPartnerInfo` | `POST` | `/facetalk/partner/info/v2` | `{"roomId": "<call_room_id>"}` | **Free.** Fetches WebRTC room & partner info. |
| 88 | `faceTalkPartnerClose` | `POST` | `/facetalk/partner/close/v2` | `{"roomId": "<call_room_id>"}` | **Free.** Ends an active FaceTalk call. |
| 89 | `faceTalkSignaling` | `POST` | `/facetalk/signaling/v2` | `{"roomId": "<id>", "cmd": "offer"\|"answer"\|"ice", "data": "..."}` | **Free.** Sends WebRTC SDP/ICE signaling packet. |
| 90 | `faceTalkHistory` | `POST` | `/facetalk/history/v1` | `{"lastCreated": "<iso_date>", "limit": 20}` | **Free.** Lists past FaceTalk video calls. |
| 91 | `faceTalkReview` | `POST` | `/facetalk/review/v1` | `{"targetId": "<id>", "rating": 5, "tags": "..."}` | **Free.** Submits post-call rating. |
| 92 | `vibeMeetList` | `POST` | `/facetalk/vibemeet/list/v1` | `{"isActive": true, "lastUpdated": "<iso_date>", "limit": 20}` | **Free.** Lists users currently online for VibeMeet video calls. |
| 93 | `vibeMeetHistoryList` | `POST` | `/facetalk/vibemeet/history/v1` | `{"lastCreated": "<iso_date>", "limit": 20}` | **Free.** Lists VibeMeet call history. |
| 94 | `vibeMeetHistoryCount` | `GET` | `/facetalk/vibemeet/history/count/v1` | `?locale=en-US` | **Free.** Fetches unread VibeMeet call history badge count. |
| 95 | `vibeMeetHistoryRead` | `POST` | `/facetalk/vibemeet/history/read/v1` | `{"locale": "en-US"}` | **Free.** Marks VibeMeet history as read. |
| 96 | `vibeMeetSetting` (GET) | `GET` | `/facetalk/vibemeet/setting/v1` | `?locale=en-US` | **Free.** Gets user's VibeMeet call reception settings. |
| 97 | `vibeMeetSetting` (POST) | `POST` | `/facetalk/vibemeet/setting/v1` | `{"isEnable": true, "isSubscribe": true}` | **Free.** Updates user's VibeMeet call reception settings. |

### 15.9 VoiceBloom (1:1 Voice Calling)
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 98 | `voiceBloomInfo` | `GET` | `/voicebloom/info/v1` | `?locale=en-US` | **Free.** Fetches VoiceBloom state and free call quota. |
| 99 | `voiceBloomPartnerRandom` | `POST` | `/voicebloom/partner/random/v1` | `{"genderFilter": "ALL"\|"5"\|"6"}` | **Hybrid.** Finds a random audio call partner. |
| 100 | `voiceBloomPartnerInfo` | `POST` | `/voicebloom/partner/info/v1` | `{"roomId": "<room_id>"}` | **Free.** Fetches VoiceBloom call room and partner info. |
| 101 | `voiceBloomPartnerClose` | `POST` | `/voicebloom/partner/close/v1` | `{"roomId": "<room_id>"}` | **Free.** Ends an active VoiceBloom audio call. |
| 102 | `voiceBloomSignaling` | `POST` | `/voicebloom/signaling/v1` | `{"roomId": "<id>", "cmd": "...", "data": "..."}` | **Free.** Sends WebRTC SDP/ICE signaling for VoiceBloom. |
| 103 | `voiceBloomTimeout` | `GET` | `/voicebloom/timeout/v1` | `?locale=en-US` | **Free.** Reports matchmaking timeout. |
| 104 | `voiceBloomCheckReward` | `GET` | `/voicebloom/check/reward/v1` | `?locale=en-US` | **Free.** Checks reward status for VoiceBloom calls. |

### 15.10 Shop, Rubies, Subscriptions, Events & Today Spotlight
| # | `RestClient` Method | HTTP | Endpoint Path | Payload / Query Parameters | Tier & Description |
| :- | :--- | :--- | :--- | :--- | :--- |
| 105 | `userShopInfo` | `POST` | `/shop/info/v1` | `{"locale": "en-US"}` | **Free.** Fetches Ruby balance, owned items, and shop catalog. |
| 106 | `userShopBuyItem` | `POST` | `/shop/buyItem/v2` | `{"itemId": "<id>", "platform": "android", "meta1": "", "meta2": "", "meta3": ""}` | **Paid (Rubies).** Buys a shop item using account Rubies. |
| 107 | `userShopUseItem` | `POST` | `/shop/useItem/v1` | `{"itemId": "<id>", "platform": "android", "targetId": "<optional_id>"}` | **Paid (Inventory).** Consumes/activates an owned shop item. |
| 108 | `inappVerifyReceipt` / `subsVerifyReceipt` | `POST` | `/shop/purchase/iab/v1` | `{"receipt": "<google_play_json>", "platform": "android", "isSubscription": true\|false}` | **Paid (Google Play IAB).** Verifies a Google Play Billing purchase receipt. |
| 109 | `shopAttendanceSet` | `POST` | `/shop/attendance/set/v1` | `{"itemId": "<id>"}` | **Free / Event.** Claims shop attendance reward. |
| 110 | `shopPromotionCheck` | `POST` | `/shop/promotion/check/v1` | `{"itemId": "<id>"}` | **Free.** Checks eligibility for a discount promotion. |
| 111 | `shopPromotionSet` | `POST` | `/shop/promotion/set/v1` | `{"itemId": "<id>"}` | **Free.** Activates a promotional offer timer. |
| 112 | `userRubyHistory` | `POST` | `/user/ruby/history/v1` | `{"lastId": "<id>", "limit": 20}` | **Free.** Fetches Ruby ledger history. |
| 113 | `userRubyListExpiring` | `GET` | `/user/ruby/list/expiring/v1` | `?lastExpirationDate=<iso_date>&locale=en-US` | **Free.** Lists free bonus Rubies scheduled to expire. |
| 114 | `getEventAttendance` | `GET` | `/event/attendance/v2` | `?locale=en-US` | **Free.** Checks daily attendance status. |
| 115 | `setEventAttendance` | `POST` | `/event/attendance/v2` | `{"locale": "en-US"}` | **Free.** Claims daily attendance check-in reward. |
| 116 | `userEventCheck` | `GET` | `/event/check/v1` | `?locale=en-US` | **Free.** Checks active events for the user. |
| 117 | `userEventConfirm` | `POST` | `/event/confirm/v1` | `{"eventCode": "<code>"}` | **Free.** Confirms event participation. |
| 118 | `eventCoupon` | `POST` | `/event/coupon/v1` | `{"couponCode": "<code>"}` | **Free.** Redeems a promo/coupon code. |
| 119 | `eventInvite` | `GET` | `/event/invite/v2` | `?locale=en-US` | **Free.** Fetches user's referral invite code and reward stats. |
| 120 | `eventLink` | `POST` | `/event/link/v2` | `{"locale": "en-US"}` | **Free.** Creates a shareable referral deep link. |
| 121 | `eventReward` | `POST` | `/event/reward/v2` | `{"eventName": "<name>"}` | **Free.** Claims reward for a completed event. |
| 122 | `eventViralLoopEntry` | `POST` | `/event/viralloop/entry/v1` | `{"url": "<deep_link>"}` | **Free.** Registers referral deep-link entry attribution. |
| 123 | `todayListInfo` | `GET` | `/today/list/v1` | `?page=0&limit=20&locale=en-US` | **Free.** Fetches spotlighted profiles on the Today tab. |
| 124 | `todayBidding` | `POST` | `/today/bid/v2` | `{"platform": "android", "ruby": 50}` | **Paid (Rubies).** Bids Rubies to spotlight own profile on the Today tab. |
| 125 | `todayReport` | `POST` | `/today/report/v1` | `{"locale": "en-US"}` | **Free.** Fetches impression analytics for Today spotlight bids. |

---

## 16. Server Error Codes & Exception Handling Reference

When an API request fails (HTTP `400`, `401`, `403`, `429`, `500`), `RestClient.organizeErrorJson` parses the JSON response (`errorCode`, `errorMessage`, `errorSubType`):

| `errorCode` / Server String | HTTP Status | Exact Cause on Meeff Backend |
| :--- | :--- | :--- |
| `PremiumFilterPermissionDenied` | `403` | Sent a Plus/Paid filter field in `POST /user/updateFilter/v1` on a Free account (e.g., `>1` country in `filterNationalityCodes`, `filterDeniedNationality`, `filterNationalityBlock: 1`, `filterDistance < 510`, `filterMinProfileImageCount != 2`, `filterPurposes`, `filterLanguageCodes`, `filterFaceVerifiedOnly: true`, `filterRecentlyActiveOnly: true`). |
| `LikeExceeded` / `RequestExceeded` | `400` / `403` | Account exhausted its free daily/rolling friend-request quota on `GET /user/undoableAnswer/v5?isOkay=1`. |
| `NotEnoughRuby` / `BalanceTooLowError` | `400` / `403` | Attempted to call `/shop/buyItem/v2`, `/today/bid/v2`, or a Ruby-gated feature without enough Rubies. |
| `RecommendedUserAccessDenied` | `403` | Called `GET /user/face/recommendation/v1` on an account without Face Verification / permission. |
| `RecommendationQuotaExceed` | `403` | Exhausted daily quota on `GET /user/face/recommendation/v1`. |
| `NotEnoughFaceVerifiedUsers` | `400` / `404` | No matching Face Verified users available for the current filter/recommendation query. |
| `FaceVerificationDailyQuotaExceeded` / `FaceVerifyQuotaExceeded` | `403` / `429` | Too many AWS Rekognition Face Liveness attempts in 24 hours (`/user/faceliveness/check/v1`). |
| `FaceVerifyPermanentBan` | `403` | Account permanently barred from Face Verification due to repeated spoofing/mismatch failures. |
| `PhoneAuthorizeQuotaExceeded` | `429` | Too many SMS OTP requests to `POST /authorize/phone/send/v2`. |
| `BlockedPendingDevice` | `403` | Login blocked until the new `deviceUniqueId` is verified/approved via `/user/pendingdevice/*`. |
| `DeviceValidationError` | `400` / `403` | Malformed or missing device fingerprint fields during `/user/login/v4` or `/user/register/*/v4`. |
| `LocationValidationError` | `400` | Invalid latitude/longitude values sent to `/user/updateLocation/v1` or `/user/explore/v4`. |
| `AppVersionValidationError` | `400` / `426` | `version` sent to `/api/init/v2` or `appVersion` in `/user/login/v4` is below minimum supported version. |
