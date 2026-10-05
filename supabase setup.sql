-- Supabase の「SQL Editor」に貼り付けて、Run を押してください。(最初に1回だけ)
-- Botのデータ(自販機・クーポン・購入履歴・PayPayのログイン情報など)を保存するテーブルです。

create table if not exists public.bot_data (
    id text primary key,
    data jsonb not null,
    updated_at timestamptz not null default now()
);

-- 外部から読み書きされないようにする(Botは service_role キーで接続するので、この設定でも動きます)
alter table public.bot_data enable row level security;
