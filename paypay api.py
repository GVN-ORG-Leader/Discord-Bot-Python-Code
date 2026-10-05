"""PayPay 送金リンク受け取り用モジュール(main.py から PAYPAY_BACKEND=api のときに使われる)

【ログイン】 Discord の /paypay_login で電話番号とパスワードを入力します。
  ログインに成功すると、電話番号・パスワード・端末ID(UUID)を暗号化して保存し、
  約2時間で切れるログインは自動で更新されます(最初の1回だけSMS認証が必要です)。
  保存先は set_credentials_store() / set_session_store() で差し替えます(main.py ではSupabaseに保存します)。
  差し替えなかった場合、電話番号とパスワードはメモリ上にだけ保持され、再起動で消えます。

【任意】 環境変数 PAYPAY_PHONE / PAYPAY_PASSWORD / PAYPAY_UUID(または config.json)で指定した場合は、そちらが優先されます。
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SESSION_FILE = os.path.join(BASE_DIR, "paypay_session.json")

TOKEN_URL = "https://www.paypay.ne.jp/app/v1/oauth/token"
LINK_INFO_URL = "https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo"
ACCEPT_URL = "https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink"
BALANCE_URL = "https://www.paypay.ne.jp/app/v1/bff/getBalanceInfo"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
}

PROXY_URL = os.getenv("PROXY_URL")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

TIMEOUT = (10, 60)

_lock = threading.Lock()


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# --- セッション・認証情報の保存先(既定はローカルファイル/メモリ。DBなどに切り替える場合は set_*_store で差し替え) ---
_session_loader = None
_session_saver = None
_session_deleter = None
_credentials_loader = None
_credentials_saver = None
_credentials_deleter = None
_mem_credentials: dict = {}


def set_session_store(loader, saver, deleter=None) -> None:
    """セッション(access_token/client_uuid)の読み書きを差し替える。
    loader() -> dict | None / saver(access_token, client_uuid) -> None / deleter() -> None
    差し替え先で例外が起きた場合は、ローカルファイルに切り替えて動作を続ける"""
    global _session_loader, _session_saver, _session_deleter
    _session_loader, _session_saver, _session_deleter = loader, saver, deleter


def set_credentials_store(loader, saver, deleter=None) -> None:
    """認証情報(電話番号・パスワード・端末ID)の読み書きを差し替える。
    loader() -> dict | None / saver(phone, password, client_uuid) -> None / deleter() -> None"""
    global _credentials_loader, _credentials_saver, _credentials_deleter
    _credentials_loader, _credentials_saver, _credentials_deleter = loader, saver, deleter


def load_session() -> dict:
    if _session_loader is not None:
        try:
            data = _session_loader()
            if isinstance(data, dict) and data.get("access_token"):
                return data
        except Exception as e:
            print(f"PAYPAY_SESSION_LOAD_ERR: {e}")
    return _load_json(SESSION_FILE)


def save_session(access_token: str, client_uuid: str) -> None:
    if _session_saver is not None:
        try:
            _session_saver(access_token, client_uuid)
        except Exception as e:
            print(f"PAYPAY_SESSION_SAVE_ERR(store): {e}")
    try:
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"access_token": access_token, "client_uuid": client_uuid},
                      f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SESSION_FILE)
    except OSError as e:
        print(f"PAYPAY_SESSION_SAVE_ERR: {e}")


def delete_session() -> None:
    """保存済みのセッションを削除する(ログアウト)"""
    if _session_deleter is not None:
        try:
            _session_deleter()
        except Exception as e:
            print(f"PAYPAY_SESSION_DELETE_ERR(store): {e}")
    try:
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
    except OSError as e:
        print(f"PAYPAY_SESSION_DELETE_ERR: {e}")


def save_credentials(phone: str, password: str, client_uuid: str) -> bool:
    """認証情報を保存する(自動再ログイン用)。保存先(DB等)に保存できたら True。
    保存先が無い・失敗した場合でも、メモリ上には保持する(再起動までは自動再ログインできる)"""
    _mem_credentials.clear()
    _mem_credentials.update({"phone": phone, "password": password, "client_uuid": client_uuid})
    if _credentials_saver is None:
        return False
    try:
        _credentials_saver(phone, password, client_uuid)
        return True
    except Exception as e:
        print(f"PAYPAY_CREDENTIALS_SAVE_ERR: {e}")
        return False


def delete_credentials() -> None:
    """保存済みの認証情報を削除する(ログアウト)"""
    _mem_credentials.clear()
    if _credentials_deleter is not None:
        try:
            _credentials_deleter()
        except Exception as e:
            print(f"PAYPAY_CREDENTIALS_DELETE_ERR: {e}")


def load_credentials() -> dict:
    """認証情報を返す。優先順位: 環境変数 → 保存先(DB等) → メモリ → config.json"""
    phone = os.getenv("PAYPAY_PHONE")
    password = os.getenv("PAYPAY_PASSWORD")
    client_uuid = os.getenv("PAYPAY_UUID")
    if phone and password and client_uuid:
        return {"phone": phone, "password": password, "client_uuid": client_uuid}

    if _credentials_loader is not None:
        try:
            data = _credentials_loader()
            if isinstance(data, dict) and data.get("phone") and data.get("password") and data.get("client_uuid"):
                return {"phone": data["phone"], "password": data["password"], "client_uuid": data["client_uuid"]}
        except Exception as e:
            print(f"PAYPAY_CREDENTIALS_LOAD_ERR: {e}")

    if _mem_credentials.get("phone") and _mem_credentials.get("password") and _mem_credentials.get("client_uuid"):
        return dict(_mem_credentials)

    config = _load_json(CONFIG_FILE)
    phone = config.get("paypay_phone")
    password = config.get("paypay_password")
    client_uuid = config.get("paypay_uuid")
    if phone and password and client_uuid:
        return {"phone": phone, "password": password, "client_uuid": client_uuid}
    return {}


def configured_uuid() -> str | None:
    """設定済みの端末ID(認証情報 → セッションの順)。SMS認証済みの端末IDを使うと認証をスキップできる"""
    return load_credentials().get("client_uuid") or load_session().get("client_uuid")


def has_session_or_credentials() -> bool:
    """ログイン済みのセッション、または自動ログインできる認証情報があるか"""
    return bool(load_session().get("access_token") or load_credentials())


def _is_token_revoked(data: dict) -> bool:
    header = data.get("header") or {}
    text = f"{header.get('resultCode', '')} {header.get('resultMessage', '')}".lower()
    return any(word in text for word in (
        "accesstoken is revoked",
        "access token is revoked",
        "session refresh",
        "token is expired",
        "invalid token",
        "unauthorized",
    ))


def _is_temp_hold(data: dict) -> bool:
    error = data.get("error") or {}
    if error.get("backendResultCode") == "42007013":
        return True
    message = str((data.get("header") or {}).get("resultMessage", ""))
    return "half sheet" in message.lower()


def _extract_code(value: str) -> str:
    return str(value).replace("https://pay.paypay.ne.jp/", "").strip().split("/")[-1]


def _request_at() -> str:
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%dT%H:%M:%S+0900")


def login(phoneNumber: str, password: str, uuid: str) -> dict:
    payload = {
        "scope": "SIGN_IN",
        "client_uuid": str(uuid),
        "grant_type": "password",
        "username": phoneNumber,
        "password": password,
        "add_otp_prefix": True,
        "language": "ja",
    }
    try:
        response = requests.post(TOKEN_URL, headers=HEADERS, json=payload,
                                 proxies=PROXIES, timeout=TIMEOUT)
        data = response.json()
    except Exception as e:
        print(f"PAYPAY_LOGIN_EXC: {e}")
        return {}

    if data.get("access_token"):
        save_session(data["access_token"], str(uuid))
    return data


def login_otp(set_uuid, otp, otpid, otp_pre):
    payload = {
        "scope": "SIGN_IN",
        "client_uuid": str(set_uuid),
        "grant_type": "otp",
        "otp_prefix": str(otp_pre),
        "otp": otp,
        "otp_reference_id": otpid,
        "username_type": "MOBILE",
        "language": "ja",
    }
    try:
        response = requests.post(TOKEN_URL, headers=HEADERS, json=payload,
                                 proxies=PROXIES, timeout=TIMEOUT)
        data = response.json()
    except Exception as e:
        print(f"PAYPAY_OTP_EXC: {e}")
        return None

    if data.get("response_type") == "ErrorResponse":
        print(f"PAYPAY_OTP_ERR: {data}")
        return None
    token = data.get("access_token")
    if token:
        save_session(token, str(set_uuid))
    return token


def ensure_login(force: bool = False) -> str | None:
    with _lock:
        session_data = load_session()
        credentials = load_credentials()

        token = session_data.get("access_token")
        if token and credentials and session_data.get("client_uuid") != credentials["client_uuid"]:
            print("PAYPAY_SESSION_STALE: client_uuid が config.json と異なるため取り直します")
            token = None
        if token and not force:
            return token

        if not credentials:
            if token:
                return token
            print("PAYPAY_CONFIG_MISSING: config.json に "
                  "paypay_phone / paypay_password / paypay_uuid がありません")
            return None

        data = login(credentials["phone"], credentials["password"],
                     credentials["client_uuid"])
        if data.get("access_token"):
            print("PAYPAY_LOGIN_OK: access_token を取得しました")
            return data["access_token"]
        if data.get("otp_reference_id"):
            print("PAYPAY_LOGIN_NEED_OTP: SMS認証が要求されました。自動ログインはできません。")
            return None
        print(f"PAYPAY_LOGIN_FAILED: {data}")
        return None


def keepalive() -> bool:
    token = ensure_login()
    if not token:
        return False

    session = _session_with_token(token)
    try:
        response = session.get(BALANCE_URL, proxies=PROXIES, timeout=TIMEOUT)
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = {}
    except requests.exceptions.RequestException as e:
        print(f"PAYPAY_KEEPALIVE: 確認に失敗しました（次回再試行）: {e}")
        return True
    finally:
        session.close()

    if data.get("header", {}).get("resultCode") == "S0000":
        return True

    if status in (401, 403) or _is_token_revoked(data):
        print("PAYPAY_KEEPALIVE: 失効を検知したので再ログインします")
        return bool(ensure_login(force=True))

    print(f"PAYPAY_KEEPALIVE: セッションが無効です: "
          f"{data.get('header', {}).get('resultMessage', data)}")
    return False


def get_balance() -> int | None:
    token = ensure_login()
    if not token:
        return None
    session = _session_with_token(token)
    try:
        response = session.get(BALANCE_URL, proxies=PROXIES, timeout=TIMEOUT)
        data = response.json()
    except Exception as e:
        print(f"PAYPAY_BALANCE_EXC: {e}")
        return None
    finally:
        session.close()

    if data.get("header", {}).get("resultCode") != "S0000":
        return None
    wallet = (data.get("payload", {})
                  .get("walletSummary", {})
                  .get("allTotalBalanceInfo", {}))
    return wallet.get("balance")


def _session_with_token(token: str | None) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    if token:
        session.cookies.set("token", token, domain="www.paypay.ne.jp")
    return session


def check_link(cd):
    code = _extract_code(cd)
    session = _session_with_token(load_session().get("access_token"))
    try:
        response = session.get(f"{LINK_INFO_URL}?verificationCode={code}",
                               proxies=PROXIES, timeout=TIMEOUT)
        response.raise_for_status()
        link_info = response.json()
    except requests.exceptions.RequestException as e:
        print(f"API_REQ_EXC: {e}")
        return False
    except ValueError as e:
        print(f"API_JSON_EXC: {e}")
        return False
    finally:
        session.close()

    if link_info.get("header", {}).get("resultCode") != "S0000":
        if _is_temp_hold(link_info):
            print("LINK_CHECK: PayPay側で一時保留になっています（送金者の確認待ち）")
        return False

    if link_info.get("payload", {}).get("orderStatus") == "PENDING":
        return link_info
    return False


def link_rev(cd: str, phoneNumber: str = None, password: str = None, uuid: str = None,
             link_password: str = None, access_token: str = None,
             _retried: bool = False):
    code = _extract_code(cd)

    token = access_token or ensure_login()
    if not token:
        return "LOGINERR"

    client_uuid = (uuid or load_session().get("client_uuid")
                   or load_credentials().get("client_uuid"))
    if not client_uuid:
        print("LINK_REV_ERR: client_uuid がありません")
        return "LOGINERR"

    session = _session_with_token(token)
    try:
        try:
            response = session.get(f"{LINK_INFO_URL}?verificationCode={code}",
                                   proxies=PROXIES, timeout=TIMEOUT)
            status = response.status_code
            try:
                link_info = response.json()
            except ValueError:
                link_info = {}
        except requests.exceptions.RequestException as e:
            print(f"LINK_REQ_EXC: {e}")
            return False

        if not _retried and (status in (401, 403) or _is_token_revoked(link_info)):
            reason = f"HTTP {status}" if status in (401, 403) else "トークン失効の応答"
            print(f"PAYPAY_TOKEN_EXPIRED: {reason} のため再ログインします")
            if ensure_login(force=True):
                return link_rev(cd, phoneNumber, password, uuid,
                                link_password, access_token=None, _retried=True)
            return "LOGINERR"

        if link_info.get("payload", {}).get("orderStatus") != "PENDING":
            if _is_temp_hold(link_info):
                return "TEMP_HOLD"
            return False

        pending = link_info.get("payload", {}).get("pendingP2PInfo", {})
        if pending.get("isSetPasscode") and link_password is None:
            print("LINK_REV_ERR: パスワード付きリンクですがパスワードが指定されていません")
            return False

        message = link_info["payload"]["message"]
        payload = {
            "verificationCode": code,
            "client_uuid": client_uuid,
            "requestAt": _request_at(),
            "requestId": message["data"]["requestId"],
            "orderId": message["data"]["orderId"],
            "senderMessageId": message["messageId"],
            "senderChannelUrl": message["chatRoomId"],
            "iosMinimumVersion": "3.45.0",
            "androidMinimumVersion": "3.45.0",
        }
        if link_password:
            payload["passcode"] = link_password

        try:
            response = session.post(ACCEPT_URL, json=payload,
                                    proxies=PROXIES, timeout=TIMEOUT)
            try:
                data = response.json()
            except ValueError:
                data = {}
        except requests.exceptions.RequestException as e:
            print(f"REVERR: {e}")
            return False
    finally:
        session.close()

    if data.get("header", {}).get("resultCode") == "S0000":
        return True

    if _is_token_revoked(data) and not _retried:
        print("PAYPAY_TOKEN_EXPIRED: 受け取り時に失効を検知したため再ログインします")
        if ensure_login(force=True):
            return link_rev(cd, phoneNumber, password, uuid,
                            link_password, access_token=None, _retried=True)
        return "LOGINERR"

    print(f"LINK_REV_ACCEPT_ERR: {data}")
    return "TEMP_HOLD" if _is_temp_hold(data) else False