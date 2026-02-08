import os
import re
import time
import base64
import hashlib
import hmac
import sqlite3
import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =======================
# Env
# =======================
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

X_BEARER_TOKEN = os.environ["X_BEARER_TOKEN"]
X_USER_ID = os.environ.get("X_USER_ID", "2020450979972616192")  # はるほーす
X_USERNAME = os.environ.get("X_USERNAME", "spl_1155")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# =======================
# Place dictionary
# =======================
PLACES = [
    # JRA
    "札幌","函館","福島","新潟","東京","中山","中京","京都","阪神","小倉",
    # NAR major (必要に応じて増やす)
    "大井","川崎","船橋","浦和","門別","盛岡","水沢","金沢","笠松","名古屋","園田","姫路","高知","佐賀",
]
place_re = re.compile("|".join(map(re.escape, PLACES)))

# 「予想っぽい」タグ付け（任意）
strong_re = re.compile(r"(◎|○|▲|△|☆|\b([1-9]|1[0-2])R\b|3連単|3連複|馬連|馬単|ワイド|単勝|複勝)")

# =======================
# DB helpers (SQLite)
# ※Render Freeだと再起動/再デプロイで消える可能性あり
# =======================
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS seen (post_id TEXT PRIMARY KEY, created_at INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
    return con

def get_users() -> list[str]:
    con = db()
    try:
        rows = con.execute("SELECT user_id FROM users").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()

def upsert_user(user_id: str) -> None:
    con = db()
    try:
        con.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
        con.commit()
    finally:
        con.close()

def seen_post(post_id: str) -> bool:
    con = db()
    try:
        row = con.execute("SELECT 1 FROM seen WHERE post_id = ?", (post_id,)).fetchone()
        return row is not None
    finally:
        con.close()

def mark_seen(post_id: str) -> None:
    con = db()
    try:
        con.execute("INSERT OR IGNORE INTO seen(post_id, created_at) VALUES (?, ?)", (post_id, int(time.time())))
        con.commit()
    finally:
        con.close()

def get_last_id() -> str | None:
    con = db()
    try:
        row = con.execute("SELECT v FROM state WHERE k='last_id'").fetchone()
        return row[0] if row else None
    finally:
        con.close()

def set_last_id(post_id: str) -> None:
    con = db()
    try:
        con.execute("INSERT INTO state(k, v) VALUES('last_id', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (post_id,))
        con.commit()
    finally:
        con.close()

# =======================
# LINE helpers
# =======================
def verify_line_signature(raw_body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")

async def line_push(user_id: str, text: str) -> None:
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()

# =======================
# X helpers
# =======================
async def fetch_latest_tweets(max_results: int = 5) -> list[dict]:
    url = f"https://api.x.com/2/users/{X_USER_ID}/tweets"
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    params = {"max_results": max_results, "exclude": "replies,retweets"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json().get("data", []) or []

def build_post_url(post_id: str) -> str:
    return f"https://x.com/{X_USERNAME}/status/{post_id}"

# =======================
# Background poll loop
# =======================
async def poll_loop(stop_event: asyncio.Event):
    # 起動直後に暴発しないように「現時点の最新」をlast_idにセットしたい場合は True
    # ただし「起動直後に直近ツイートも通知したい」なら False
    SKIP_EXISTING_ON_BOOT = False

    if SKIP_EXISTING_ON_BOOT:
        try:
            tweets = await fetch_latest_tweets(max_results=5)
            if tweets:
                newest = max(tweets, key=lambda t: int(t["id"]))
                set_last_id(newest["id"])
        except Exception:
            pass

    while not stop_event.is_set():
        try:
            tweets = await fetch_latest_tweets(max_results=5)
            if tweets:
                last_id = get_last_id()
                # 古い→新しいで処理
                tweets_sorted = sorted(tweets, key=lambda t: int(t["id"]))

                for t in tweets_sorted:
                    tid = t["id"]
                    text = t["text"]

                    if last_id and int(tid) <= int(last_id):
                        continue
                    # last_idが無い初回は、全部通知が飛ぶ可能性があるので
                    # 「seen」による二重防止も併用
                    if seen_post(tid):
                        set_last_id(tid)
                        continue

                    # 場所検知
                    m = place_re.search(text)
                    if not m:
                        set_last_id(tid)
                        mark_seen(tid)
                        continue

                    place = m.group(0)
                    tag = "予想濃厚" if strong_re.search(text) else "場所一致"

                    msg = (
                        f"【はるほーす】{tag} / 場所: {place}\n\n"
                        f"{text}\n\n"
                        f"{build_post_url(tid)}"
                    )

                    users = get_users()
                    for uid in users:
                        await line_push(uid, msg)

                    mark_seen(tid)
                    set_last_id(tid)

        except Exception as e:
            # ログで追えるようにstdoutへ
            print("poll_loop error:", repr(e))

        # スリープ（停止要求が来てもすぐ抜けられるように）
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass

# =======================
# FastAPI lifespan
# =======================
stop_event = asyncio.Event()
poll_task: asyncio.Task | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global poll_task
    # バックグラウンド開始
    poll_task = asyncio.create_task(poll_loop(stop_event))
    yield
    # 終了処理
    stop_event.set()
    if poll_task:
        poll_task.cancel()
        with contextlib.suppress(Exception):
            await poll_task

app = FastAPI(lifespan=lifespan)

# =======================
# Routes
# =======================
@app.get("/")
def health():
    return {"ok": True}

@app.post("/line/webhook")
async def line_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-line-signature", "")
    if not verify_line_signature(raw, sig):
        raise HTTPException(status_code=401, detail="Bad signature")

    body = await request.json()
    for ev in body.get("events", []):
        user_id = ev.get("source", {}).get("userId")
        if user_id:
            upsert_user(user_id)
    return {"ok": True}

class TestPushIn(BaseModel):
    text: str

# 動作確認用：自分にpushしてみたいときだけ使う（ADMIN_TOKEN不要）
@app.post("/test_push")
async def test_push(payload: TestPushIn):
    users = get_users()
    for uid in users:
        await line_push(uid, f"【テスト】\n{payload.text}")
    return {"ok": True, "sent": len(users)}
