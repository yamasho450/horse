import os
import sqlite3
import httpx

X_BEARER_TOKEN = os.environ["X_BEARER_TOKEN"]
X_USER_ID = os.environ["X_USER_ID"]   # 2020450979972616192
NOTIFY_URL = os.environ["RENDER_NOTIFY_URL"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]

DB_PATH = "cron.db"

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS last (id TEXT)")
    return con

def get_last_id(con):
    row = con.execute("SELECT id FROM last").fetchone()
    return row[0] if row else None

def set_last_id(con, tid):
    con.execute("DELETE FROM last")
    con.execute("INSERT INTO last(id) VALUES (?)", (tid,))
    con.commit()

def fetch_latest():
    url = f"https://api.x.com/2/users/{X_USER_ID}/tweets"
    headers = {
        "Authorization": f"Bearer {X_BEARER_TOKEN}"
    }
    params = {
        "max_results": 5,
        "exclude": "replies,retweets"
    }
    r = httpx.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])

def main():
    con = get_db()
    last_id = get_last_id(con)

    tweets = fetch_latest()
    if not tweets:
        return

    # 古い → 新しい順
    tweets = sorted(tweets, key=lambda x: int(x["id"]))

    for t in tweets:
        if last_id and int(t["id"]) <= int(last_id):
            continue

        payload = {
            "post_id": t["id"],
            "post_url": f"https://x.com/spl_1155/status/{t['id']}",
            "text": t["text"]
        }
        headers = {"x-admin-token": ADMIN_TOKEN}

        r = httpx.post(NOTIFY_URL, json=payload, headers=headers, timeout=20)
        r.raise_for_status()

        set_last_id(con, t["id"])

    con.close()

if __name__ == "__main__":
    main()
