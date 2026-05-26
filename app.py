import os
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_from_directory, render_template
from pywebpush import webpush, WebPushException

app = Flask(__name__, static_folder="static", template_folder="templates")

TZ = ZoneInfo("Europe/Istanbul")
DATA_FILE = "tasks.json"
SUBS_FILE = "subscriptions.json"

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL = os.environ.get("VAPID_EMAIL", "mailto:admin@beniunutma.app")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "1234")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
    subs = load_json(SUBS_FILE, [])
    # deduplicate by endpoint
    subs = [s for s in subs if s.get("endpoint") != sub.get("endpoint")]
    subs.append(sub)
    save_json(SUBS_FILE, subs)
    return jsonify({"ok": True})


# ── Tasks ─────────────────────────────────────────────────────────────────────
@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    return jsonify(load_json(DATA_FILE, {"pending": [], "done": []}))


@app.route("/api/tasks", methods=["POST"])
def add_task():
    body = request.get_json()
    data = load_json(DATA_FILE, {"pending": [], "done": []})
    now = datetime.now(TZ)
    task = {
        "id": int(now.timestamp() * 1000),
        "title": body["title"],
        "alarm": body["alarm"],  # ISO string
        "status": "pending",
    }
    data["pending"].append(task)
    save_json(DATA_FILE, data)
    return jsonify({"ok": True, "task": task})


@app.route("/api/tasks/<int:task_id>/done", methods=["POST"])
def mark_done(task_id):
    data = load_json(DATA_FILE, {"pending": [], "done": []})
    task = next((t for t in data["pending"] if t["id"] == task_id), None)
    if task:
        task["status"] = "done"
        task["completedAt"] = datetime.now(TZ).isoformat()
        data["pending"] = [t for t in data["pending"] if t["id"] != task_id]
        data["done"].append(task)
        save_json(DATA_FILE, data)
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    data = load_json(DATA_FILE, {"pending": [], "done": []})
    data["pending"] = [t for t in data["pending"] if t["id"] != task_id]
    save_json(DATA_FILE, data)
    return jsonify({"ok": True})


# ── Push sender ───────────────────────────────────────────────────────────────
def send_push(payload: dict):
    subs = load_json(SUBS_FILE, [])
    dead = []
    for sub in subs:
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
        subs = [s for s in subs if s["endpoint"] not in dead]
        save_json(SUBS_FILE, subs)


# ── Background alarm checker ──────────────────────────────────────────────────
def alarm_loop():
    notified = set()  # task_id -> last notified time (for 15-min repeat)
    while True:
        try:
            data = load_json(DATA_FILE, {"pending": [], "done": []})
            now = datetime.now(TZ)
            for task in data["pending"]:
                alarm_dt = datetime.fromisoformat(task["alarm"])
                if alarm_dt.tzinfo is None:
                    alarm_dt = alarm_dt.replace(tzinfo=TZ)
                diff = (now - alarm_dt).total_seconds()
                task_id = task["id"]

                # Initial alarm: within 60s window
                if 0 <= diff < 60 and task_id not in notified:
                    send_push({
                        "type": "alarm",
                        "id": task_id,
                        "title": task["title"],
                    })
                    notified[task_id] = now.timestamp()

                # 15-min repeat check
                elif task_id in notified:
                    elapsed = now.timestamp() - notified[task_id]
                    if elapsed >= 15 * 60:
                        send_push({
                            "type": "check",
                            "id": task_id,
                            "title": task["title"],
                        })
                        notified[task_id] = now.timestamp()
        except Exception as e:
            print(f"Alarm loop error: {e}")
        time.sleep(30)


threading.Thread(target=alarm_loop, daemon=True).start()


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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
