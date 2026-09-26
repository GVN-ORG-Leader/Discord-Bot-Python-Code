import os
import json
import datetime
import re
from typing import NamedTuple
from uuid import uuid4
import requests

TOKEN_FILE = "paypay_tokens.json"

def save_tokens(access_token: str, refresh_token: str, client_uuid: str):
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_uuid": client_uuid
    }
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def load_tokens():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None

# PayPay側でブロックされない最新のヘッダー情報
headers = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "PayPay/4.80.0 (iPhone; iOS 16.5; Scale/3.00)",
    "Content-Type": "application/json",
    "X-Device-Uuid": str(uuid4()),
    "X-Client-Type": "IOS",
    "X-Client-Version": "4.80.0",
    "Authorization": "Basic YXBwLWlvcy1wYXlwYXk6Y3lOeldsY0pHT0xOQnEzeXFWeUEyYklzOThjRzVxdmw="
}

class PayPayError(Exception): pass
class PayPayNetWorkError(Exception): pass
class PayPayLoginError(Exception): pass

class PayPay:
    def __init__(self, phone: str = None, password: str = None, client_uuid: str = None, access_token: str = None, refresh_token: str = None, proxy: dict = None):
        self.session = requests.Session()
        self.proxy = proxy
        self.client_uuid = client_uuid if client_uuid else str(uuid4()).upper()
        self.phone = phone
        self.password = password
        self.otp_prefix = None
        self.otp_reference_id = None
        self.access_token = access_token
        self.refresh_token = refresh_token
        
        if access_token:
            self.session.cookies.set("token", access_token)
        elif phone:
            payload = {
                "scope": "SIGN_IN",
                "client_uuid": self.client_uuid,
                "grant_type": "password",
                "username": self.phone,
                "password": self.password,
                "add_otp_prefix": True,
                "language": "ja"
            }
            login = self.session.post("https://www.paypay.ne.jp/app/v1/oauth/token", json=payload, headers=headers, proxies=proxy)
            try:
                data = login.json()
                if "access_token" in data:
                    self.access_token = data.get("access_token")
                    self.refresh_token = data.get("refresh_token")
                    self.session.cookies.set("token", self.access_token)
                    save_tokens(self.access_token, self.refresh_token, self.client_uuid)
                elif "otp_prefix" in data or "otp_reference_id" in data:
                    self.otp_prefix = data.get("otp_prefix")
                    self.otp_reference_id = data.get("otp_reference_id")
                else:
                    raise PayPayLoginError(data)
            except Exception as e:
                if isinstance(e, PayPayLoginError): raise e
                raise PayPayNetWorkError(login.text)

    def refresh_access_token(self):
        if not self.refresh_token:
            raise PayPayLoginError("リフレッシュトークンが存在しません。")

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_uuid": self.client_uuid
        }
        res = self.session.post("https://www.paypay.ne.jp/app/v1/oauth/token", json=payload, headers=headers, proxies=self.proxy)
        try:
            data = res.json()
            if "access_token" in data:
                self.access_token = data.get("access_token")
                if "refresh_token" in data:
                    self.refresh_token = data.get("refresh_token")
                self.session.cookies.set("token", self.access_token)
                save_tokens(self.access_token, self.refresh_token, self.client_uuid)
                return True
            else:
                raise PayPayLoginError(data)
        except Exception as e:
            raise PayPayLoginError("トークンの自動更新に失敗しました。")

    def login(self, otp: str):
        clean_otp = "".join(filter(str.isdigit, str(otp)))
        payload = {
            "scope": "SIGN_IN",
            "client_uuid": self.client_uuid,
            "grant_type": "otp",
            "otp": clean_otp,
            "otp_reference_id": self.otp_reference_id
        }
        if self.otp_prefix:
            payload["otp_prefix"] = self.otp_prefix

        res = self.session.post("https://www.paypay.ne.jp/app/v1/oauth/token", json=payload, headers=headers, proxies=self.proxy)
        try:
            data = res.json()
            if "access_token" in data:
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.session.cookies.set("token", self.access_token)
                save_tokens(self.access_token, self.refresh_token, self.client_uuid)
                return data
            else:
                raise PayPayLoginError(data)
        except Exception as e:
            if isinstance(e, PayPayLoginError): raise e
            raise PayPayNetWorkError(res.text)

    def _clean_code(self, url: str) -> str:
        """URLから不要なクエリパラメータを除去し verificationCode のみを抽出"""
        url = url.replace("https://pay.paypay.ne.jp/", "").strip()
        url = url.split("?")[0]
        return url

    def link_check(self, url: str):
        code = self._clean_code(url)
        param = {"verificationCode": code}
        res = self.session.get("https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo", headers=headers, params=param, proxies=self.proxy)
        
        if res.status_code == 401 or (res.headers.get("content-type") == "application/json" and res.json().get("header", {}).get("resultCode") == "S0001"):
            self.refresh_access_token()
            res = self.session.get("https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo", headers=headers, params=param, proxies=self.proxy)

        link_info = res.json()
        if link_info.get("header", {}).get("resultCode") != "S0000":
            raise PayPayError(link_info)
        
        class LinkInfo(NamedTuple):
            sender_name: str
            sender_external_id: str
            sender_icon: str
            order_id: str
            chat_room_id: str
            amount: int
            status: str
            money_light: int
            money: int
            has_password: bool
            raw: dict

        return LinkInfo(
            link_info["payload"]["sender"]["displayName"],
            link_info["payload"]["sender"]["externalId"],
            link_info["payload"]["sender"].get("photoUrl", ""),
            link_info["payload"]["pendingP2PInfo"]["orderId"],
            link_info["payload"]["message"]["chatRoomId"],
            link_info["payload"]["pendingP2PInfo"]["amount"],
            link_info["payload"]["message"]["data"]["status"],
            link_info["payload"]["message"]["data"]["subWalletSplit"]["senderPrepaidAmount"],
            link_info["payload"]["message"]["data"]["subWalletSplit"]["senderEmoneyAmount"],
            link_info["payload"]["pendingP2PInfo"]["isSetPasscode"],
            link_info
        )

    def link_receive(self, url: str, password: str = None, link_info: dict = None) -> dict:
        if not self.access_token:
            raise PayPayLoginError("ログイン情報が存在しません")
            
        code = self._clean_code(url)
        
        if not link_info:
            param = {"verificationCode": code}
            res = self.session.get("https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo", headers=headers, params=param, proxies=self.proxy)
            if res.status_code == 401 or (res.headers.get("content-type") == "application/json" and res.json().get("header", {}).get("resultCode") == "S0001"):
                self.refresh_access_token()
                res = self.session.get("https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo", headers=headers, params=param, proxies=self.proxy)
            
            link_info = res.json()
            if link_info.get("header", {}).get("resultCode") != "S0000":
                raise PayPayError(link_info)
        
        if link_info["payload"]["orderStatus"] != "PENDING":
            raise PayPayError("すでに 受け取り / 辞退 / キャンセル されているリンクです")
        if link_info["payload"]["pendingP2PInfo"]["isSetPasscode"] and password is None:
            raise PayPayError("このリンクにはパスワードが設定されています")
        
        payload = {
            "verificationCode": code,
            "client_uuid": self.client_uuid,
            "requestAt": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900')),
            "requestId": link_info["payload"]["message"]["data"]["requestId"],
            "orderId": link_info["payload"]["message"]["data"]["orderId"],
            "senderMessageId": link_info["payload"]["message"]["messageId"],
            "senderChannelUrl": link_info["payload"]["message"]["chatRoomId"],
            "iosMinimumVersion": "4.80.0",
            "androidMinimumVersion": "4.80.0"
        }
        if password:
            payload["passcode"] = password

        receive_res = self.session.post("https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink", json=payload, headers=headers, proxies=self.proxy)
        receive = receive_res.json()

        if receive.get("header", {}).get("resultCode") == "S0001":
            self.refresh_access_token()
            receive_res = self.session.post("https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink", json=payload, headers=headers, proxies=self.proxy)
            receive = receive_res.json()

        if receive.get("header", {}).get("resultCode") != "S0000":
            raise PayPayError(receive)
        
        return receive
