"""Supabase(PostgREST + Storage)に requests だけでアクセスする最小クライアント

【事前準備】Supabase の「SQL Editor」で次を実行して、保存用のテーブルを作ります。

    create table if not exists public.bot_data (
        id text primary key,
        data jsonb not null,
        updated_at timestamptz not null default now()
    );
    alter table public.bot_data enable row level security;

【接続に使うキー】 service_role キー(秘密のキー)を使います。anon キーでは読み書きできません。
このキーは外部に公開しないでください。(Botの環境変数 SUPABASE_KEY にだけ設定します)

画像などのファイル在庫は Storage のバケット(既定: stock-files、非公開)に保存します。
バケットは起動時に自動で作成されます。
"""
from datetime import datetime, timezone
from urllib.parse import quote

import requests

DOC_TIMEOUT = (5, 20)      # (接続, 応答) 秒。保存・読み込みはこの時間で諦める
FILE_TIMEOUT = (10, 120)   # ファイルのアップロード/ダウンロード用


class SupabaseError(Exception):
    """Supabase との通信・操作に失敗した(メッセージはそのまま画面やログに表示できる)"""


def _error_detail(resp) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("message") or body.get("error") or body.get("msg") or "")[:200]
    except Exception:
        pass
    try:
        return (getattr(resp, "text", "") or "")[:200]
    except Exception:
        return ""


class SupabaseDB:
    def __init__(self, url: str, key: str, table: str = "bot_data", bucket: str = "stock-files", session=None):
        self.url = url.strip().rstrip("/")
        self.key = key.strip()
        self.table = table
        self.bucket = bucket
        self._s = session or requests.Session()

    # ---------- 共通 ----------
    def _headers(self, extra: dict = None) -> dict:
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, what: str, *, timeout=DOC_TIMEOUT, ok=(), headers=None, **kwargs):
        """リクエストを送り、失敗(2xx以外。ok に指定したステータスは除く)なら SupabaseError にする"""
        try:
            resp = self._s.request(method, f"{self.url}{path}", headers=self._headers(headers), timeout=timeout, **kwargs)
        except requests.RequestException as e:
            raise SupabaseError(f"{what}: 接続できません ({e.__class__.__name__}: {e})") from e
        if 200 <= resp.status_code < 300 or resp.status_code in ok:
            return resp
        hint = ""
        if resp.status_code in (401, 403):
            hint = " (キーが正しくありません。service_role キーを SUPABASE_KEY に設定してください)"
        elif resp.status_code == 404 and path.startswith("/rest/"):
            hint = f" (SUPABASE_URL が正しいか、テーブル {self.table} を作成済みか確認してください)"
        detail = _error_detail(resp)   # 接続を閉じる前に本文を読む(stream=True の場合に備える)
        if hasattr(resp, "close"):
            resp.close()
        raise SupabaseError(f"{what}: HTTP {resp.status_code} {detail}{hint}".rstrip())

    # ---------- テーブル(データ) ----------
    def ping(self) -> None:
        """URL・キー・テーブルが正しいか実際に通信して確認する"""
        self._request("GET", f"/rest/v1/{self.table}", "接続確認", params={"select": "id", "limit": "1"})

    def get_doc(self, doc_id: str):
        """保存済みのデータ(dict)を返す。まだ無ければ None"""
        resp = self._request("GET", f"/rest/v1/{self.table}", "データの読み込み",
                             params={"id": f"eq.{doc_id}", "select": "data"})
        rows = resp.json()
        if not rows:
            return None
        return rows[0].get("data")

    def put_doc(self, doc_id: str, data: dict) -> None:
        """データを保存(同じidがあれば上書き)"""
        self._request(
            "POST", f"/rest/v1/{self.table}", "データの保存",
            params={"on_conflict": "id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"id": doc_id, "data": data, "updated_at": datetime.now(timezone.utc).isoformat()},
        )

    def delete_doc(self, doc_id: str) -> None:
        self._request("DELETE", f"/rest/v1/{self.table}", "データの削除", params={"id": f"eq.{doc_id}"})

    # ---------- Storage(ファイル) ----------
    def ensure_bucket(self) -> None:
        """ファイル用の非公開バケットを作る(すでにあれば何もしない)"""
        resp = self._request("POST", "/storage/v1/bucket", "バケット作成", ok=(400, 409),
                             json={"id": self.bucket, "name": self.bucket, "public": False})
        if resp.status_code in (400, 409):
            text = _error_detail(resp).lower()
            if "already exists" not in text and "duplicate" not in text:
                raise SupabaseError(f"バケット作成: HTTP {resp.status_code} {_error_detail(resp)}")

    def _object_path(self, name: str, authenticated: bool = False) -> str:
        middle = "authenticated/" if authenticated else ""
        return f"/storage/v1/object/{middle}{self.bucket}/{quote(name, safe='')}"

    def upload_file(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._request("POST", self._object_path(name), "ファイルのアップロード", timeout=FILE_TIMEOUT,
                      headers={"Content-Type": content_type, "x-upsert": "true"}, data=data)

    def download_file(self, name: str) -> bytes:
        resp = self._request("GET", self._object_path(name, authenticated=True), "ファイルのダウンロード", timeout=FILE_TIMEOUT)
        return resp.content

    def file_exists(self, name: str) -> bool:
        resp = self._request("GET", self._object_path(name, authenticated=True), "ファイルの確認",
                             timeout=DOC_TIMEOUT, ok=(400, 404), stream=True)
        exists = 200 <= resp.status_code < 300
        if hasattr(resp, "close"):
            resp.close()
        return exists

    def delete_file(self, name: str) -> None:
        self._request("DELETE", self._object_path(name), "ファイルの削除", ok=(400, 404))
