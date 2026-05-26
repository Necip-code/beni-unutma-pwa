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
APP_PASSWORD = os.environ.get("APP_PASSWORD", "1234")
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
                    alarm TIMESTAMPTZ NOT NULL,
                    status TEXT DEFAULT 'pending',
                    completed_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
            """)
        conn.commit()

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json()
    if body.get("password") == APP_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Hatalı şifre"}), 401

# ── VAPID public key ──────────────────────────────────────────────────────────
@app.route("/api/vapid-public-key")
def vapid_public_key():
    return jsonify({"key": VAPID_PUBLIC_KEY})

# ── Push subscription ─────────────────────────────────────────────────────────
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

# ── Tasks ─────────────────────────────────────────────────────────────────────
@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY alarm")
            pending = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM tasks WHERE status='done' ORDER BY completed_at DESC LIMIT 50")
            done = [dict(r) for r in cur.fetchall()]
    
    def fmt(t):
        return {
            "id": t["id"],
            "title": t["title"],
            "alarm": t["alarm"].isoformat() if hasattr(t["alarm"], "isoformat") else t["alarm"],
            "status": t["status"],
            "completedAt": t["completed_at"].isoformat() if t.get("completed_at") else None,
        }
    
    return jsonify({"pending": [fmt(t) for t in pending], "done": [fmt(t) for t in done]})

@app.route("/api/tasks", methods=["POST"])
def add_task():
    body = request.get_json()
    now = datetime.now(TZ)
    task_id = int(now.timestamp() * 1000)
    alarm_str = body["alarm"]
    alarm_dt = datetime.fromisoformat(alarm_str).replace(tzinfo=None).replace(tzinfo=TZ)
    
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (id, title, alarm, status) VALUES (%s, %s, %s, 'pending')",
                (task_id, body["title"], alarm_dt)
            )
        conn.commit()
    return jsonify({"ok": True, "task": {"id": task_id, "title": body["title"], "alarm": alarm_dt.isoformat()}})

@app.route("/api/tasks/<int:task_id>/done", methods=["POST"])
def mark_done(task_id):
    now = datetime.now(TZ)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status='done', completed_at=%s WHERE id=%s",
                (now, task_id)
            )
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        conn.commit()
    return jsonify({"ok": True})

# ── Push sender ───────────────────────────────────────────────────────────────
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

# ── Background alarm checker ──────────────────────────────────────────────────
def alarm_loop():
    notified = {}
    while True:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM tasks WHERE status='pending'")
                    tasks = [dict(r) for r in cur.fetchall()]
            
            now = datetime.now(TZ)
            for task in tasks:
                alarm_dt = task["alarm"]
                if hasattr(alarm_dt, "tzinfo") and alarm_dt.tzinfo is None:
                    alarm_dt = alarm_dt.replace(tzinfo=TZ)
                diff = (now - alarm_dt).total_seconds()
                task_id = task["id"]

                if 0 <= diff < 60 and task_id not in notified:
                    send_push({"type": "alarm", "id": task_id, "title": task["title"]})
                    notified[task_id] = now.timestamp()
                elif task_id in notified:
                    elapsed = now.timestamp() - notified[task_id]
                    if elapsed >= 15 * 60:
                        send_push({"type": "check", "id": task_id, "title": task["title"]})
                        notified[task_id] = now.timestamp()
        except Exception as e:
            print(f"Alarm loop error: {e}")
        time.sleep(30)

# ── Serve PWA ─────────────────────────────────────────────────────────────────
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
