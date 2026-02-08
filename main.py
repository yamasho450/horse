import os
import base64
import hashlib
import hmac
import sqlite3
import re
import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# ===== 環境変数 =====
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]

DB_PATH = "bot.db"

# ===== 競馬場（場所）辞書 =====
PLACES = [
    # JRA
    "札幌","函館","福島","新潟","東京","中山","中京","京都","阪神","小倉",
    # 地方（主要）
    "大井","川崎","船橋","浦和","門別","盛岡","水沢","金沢","笠松","名古屋","園田","姫路","高知","佐賀"
]
place_re = re.compile("|".join(map(re.escape, PLACES)))

# ===== FastAPI =====
app = FastAPI()

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS seen (post_id TEXT PRIMARY KEY)")
    return con

# ===== LINE署名検証 =====
def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")

# ===== LINE送信 =====
async def line_push(user_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}]
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()

# ===== LINE Webhook =====
@app.post("/line/webhook")
async def line_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-line-signature", "")
    if not verify_signature(raw, sig):
        raise HTTPException(status_code=401, detail="Bad signature")

    data = await request.json()
    con = get_db()

    try:
        for ev in data.get("events", []):
            user_id = ev.get("source", {}).get("userId")
            if user_id:
                con.execute(
                    "INSERT OR IGNORE INTO users(user_id) VALUES (?)",
                    (user_id,)
                )
                con.commit()
    finally:
        con.close()

    return {"ok": True}

# ===== Cron Job からの通知受信 =====
class NotifyIn(BaseModel):
    post_id: str
    post_url: str
    text: str

@app.post("/notify")
async def notify(payload: NotifyIn, request: Request):
    token = request.headers.get("x-admin-token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 場所が含まれていなければスキップ
    if not place_re.search(payload.text):
        return {"ok": True, "skipped": "no_place"}

    con = get_db()
    try:
        # 二重送信防止
        if con.execute(
            "SELECT 1 FROM seen WHERE post_id=?",
            (payload.post_id,)
        ).fetchone():
            return {"ok": True, "skipped": "already_sent"}

        con.execute(
            "INSERT INTO seen(post_id) VALUES (?)",
            (payload.post_id,)
        )
        con.commit()

        users = [
            r[0] for r in con.execute("SELECT user_id FROM users").fetchall()
        ]
    finally:
        con.close()

    message = (
        "【はるほーす 新着】\n\n"
        f"{payload.text}\n\n"
        f"{payload.post_url}"
    )

    for uid in users:
        await line_push(uid, message)

    return {"ok": True, "sent": len(users)}
