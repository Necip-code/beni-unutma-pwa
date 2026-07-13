import os
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_from_directory
from pywebpush import webpush, WebPushException
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__, static_folder="static")

TZ = ZoneInfo("Europe/Istanbul")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL = os.environ.get("VAPID_EMAIL", "mailto:admin@beniunutma.app")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id BIGINT PRIMARY KEY,
                    title TEXT NOT NULL,
                    alarm_str TEXT NOT NULL,
                    alarm_ts BIGINT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    completed_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
            """)
        conn.commit()

@app.route("/api/vapid-public-key")
def vapid_public_key():
    return jsonify({"key": VAPID_PUBLIC_KEY})

@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    sub = request.get_json()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscriptions (endpoint, data)
                VALUES (%s, %s)
                ON CONFLICT (endpoint) DO UPDATE SET data = EXCLUDED.data
            """, (sub["endpoint"], json.dumps(sub)))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY alarm_ts")
            pending = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM tasks WHERE status='done' ORDER BY id DESC LIMIT 50")
            done = [dict(r) for r in cur.fetchall()]

    def fmt(t):
        return {
            "id": t["id"],
            "title": t["title"],
            "alarm": t["alarm_str"],
            "status": t["status"],
            "completedAt": t.get("completed_at"),
        }

    return jsonify({"pending": [fmt(t) for t in pending], "done": [fmt(t) for t in done]})

@app.route("/api/tasks", methods=["POST"])
def add_task():
    body = request.get_json()
    now = datetime.now(TZ)
    task_id = int(now.timestamp() * 1000)
    alarm_str = body["alarm"]  # e.g. "2026-07-13T19:15:00"
    # Parse as Istanbul time, get unix timestamp for comparison
    dt = datetime.fromisoformat(alarm_str.replace("Z", "")).replace(tzinfo=None)
    dt_istanbul = dt.replace(tzinfo=TZ)
    alarm_ts = int(dt_istanbul.timestamp())

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (id, title, alarm_str, alarm_ts, status) VALUES (%s, %s, %s, %s, 'pending')",
                (task_id, body["title"], alarm_str, alarm_ts)
            )
        conn.commit()
    return jsonify({"ok": True, "task": {"id": task_id, "title": body["title"], "alarm": alarm_str}})

@app.route("/api/tasks/<int:task_id>/done", methods=["POST"])
def mark_done(task_id):
    now = datetime.now(TZ).isoformat()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET status='done', completed_at=%s WHERE id=%s", (now, task_id))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        conn.commit()
    return jsonify({"ok": True})

def send_push(payload: dict):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM subscriptions")
            rows = cur.fetchall()
    dead = []
    for row in rows:
        sub = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_EMAIL},
            )
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                dead.append(sub["endpoint"])
    if dead:
        with get_db() as conn:
            with conn.cursor() as cur:
                for ep in dead:
                    cur.execute("DELETE FROM subscriptions WHERE endpoint=%s", (ep,))
            conn.commit()

def alarm_loop():
    notified = {}
    while True:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM tasks WHERE status='pending'")
                    tasks = [dict(r) for r in cur.fetchall()]
            now_ts = int(datetime.now(TZ).timestamp())
            for task in tasks:
                alarm_ts = task["alarm_ts"]
                task_id = task["id"]
                diff = now_ts - alarm_ts
                if 0 <= diff < 60 and task_id not in notified:
                    send_push({"type": "alarm", "id": task_id, "title": task["title"]})
                    notified[task_id] = now_ts
                elif task_id in notified:
                    elapsed = now_ts - notified[task_id]
                    if elapsed >= 15 * 60:
                        send_push({"type": "check", "id": task_id, "title": task["title"]})
                        notified[task_id] = now_ts
        except Exception as e:
            print(f"Alarm loop error: {e}")
        time.sleep(30)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/sw.js")
def sw():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")

if __name__ == "__main__":
    init_db()
    threading.Thread(target=alarm_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
