import os
import sqlite3
import time
import threading
import requests
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

# ===========================
# CONFIG
# ===========================
ADMIN_USERNAME = "kunaldhade@tragene.com"
ADMIN_PASSWORD = "Kunal_8805"
DB_PATH = "credentials.db"
SERVER_URL = "https://www.tragenefunded.com/api/mt5/sync"
INTERNAL_KEY = "TGF_INT_xK92mQ27pL38nR4"
MONITOR_KEY_NAME = "TGF_MONITOR_SESSION"
MONITOR_KEY_SECRET = "TGF_MON_pQ85nR31mK57xJ9"
MT5_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
CYCLE_MIN_INTERVAL = 15  # minutes
SECRET_KEY = os.urandom(24).hex()

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ===========================
# DATABASE SETUP
# ===========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mt5_login TEXT NOT NULL,
            mt5_password TEXT NOT NULL,
            mt5_server TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            challenge_token TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            last_fetched TEXT,
            last_sync_result TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===========================
# AUTH DECORATOR
# ===========================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ===========================
# ROUTES
# ===========================
@app.route("/")
def index():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, mt5_login, mt5_password, mt5_server, challenge_id, status, last_fetched, last_sync_result 
        FROM credentials ORDER BY id DESC
    """)
    creds = c.fetchall()
    conn.close()
    return render_template("dashboard.html", credentials=creds)

@app.route("/api/add_credential", methods=["POST"])
@login_required
def add_credential():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO credentials (mt5_login, mt5_password, mt5_server, challenge_id, challenge_token, status)
        VALUES (?, ?, ?, ?, ?, 'active')
    """, (
        data["mt5_login"],
        data["mt5_password"],
        data["mt5_server"],
        data["challenge_id"],
        data["challenge_token"]
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/toggle_status", methods=["POST"])
@login_required
def toggle_status():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE credentials SET status = ? WHERE id = ?", (data["status"], data["id"]))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/delete_credential", methods=["POST"])
@login_required
def delete_credential():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM credentials WHERE id = ?", (data["id"],))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/get_status")
@login_required
def get_status():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, mt5_login, mt5_password, mt5_server, challenge_id, status, last_fetched, last_sync_result 
        FROM credentials ORDER BY id DESC
    """)
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r[0],
            "mt5_login": r[1],
            "mt5_password": r[2],
            "mt5_server": r[3],
            "challenge_id": r[4],
            "status": r[5],
            "last_fetched": r[6],
            "last_sync_result": r[7]
        })
    return jsonify(result)

# ===========================
# MT5 FETCHER (BACKGROUND THREAD)
# ===========================
def get_active_credentials():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, mt5_login, mt5_password, mt5_server, challenge_id, challenge_token 
        FROM credentials WHERE status='active'
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def update_credential_status(cred_id, last_fetched, sync_result):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE credentials SET last_fetched=?, last_sync_result=? WHERE id=?",
        (last_fetched, sync_result, cred_id)
    )
    conn.commit()
    conn.close()

def fetch_single_account(cred):
    cred_id, mt5_login, mt5_password, mt5_server, challenge_id, challenge_token = cred
    try:
        import MetaTrader5 as mt5

        if not mt5.initialize(
            path=MT5_TERMINAL_PATH,
            login=int(mt5_login),
            password=mt5_password,
            server=mt5_server
        ):
            return False, "MT5 login failed"

        # Set monitor key so EA sends data
        mt5.global_variable_set(MONITOR_KEY_NAME, float(MONITOR_KEY_SECRET))

        # Wait for EA to process and delete the variable
        waited = 0
        while mt5.global_variable_check(MONITOR_KEY_NAME) and waited < 10:
            time.sleep(1)
            waited += 1

        # Fetch account data directly from MT5
        acc_info = mt5.account_info()
        if acc_info is None:
            mt5.shutdown()
            return False, "Account info fetch failed"

        positions = mt5.positions_get()
        history_from = datetime.now() - timedelta(days=7)
        history = mt5.history_deals_get(history_from, datetime.now())

        # Build account snapshot
        account_json = {
            "account_login": acc_info.login,
            "broker_server": mt5_server,
            "balance": acc_info.balance,
            "equity": acc_info.equity,
            "free_margin": acc_info.margin_free,
            "margin_used": acc_info.margin,
            "leverage": acc_info.leverage,
            "currency": acc_info.currency,
            "terminal_time": int(time.time())
        }

        # Build open trades
        open_trades = []
        if positions:
            for pos in positions:
                open_trades.append({
                    "ticket": pos.ticket,
                    "symbol": pos.symbol,
                    "type": pos.type,
                    "lots": pos.volume,
                    "open_price": pos.price_open,
                    "current_price": pos.price_current,
                    "floating_pnl": pos.profit,
                    "sl": pos.sl,
                    "tp": pos.tp,
                    "open_time": int(pos.time)
                })

        # Build closed trades (last 2 out deals)
        closed_trades = []
        if history:
            out_deals = [d for d in history if d.entry in (1, 2)][-2:]  # DEAL_ENTRY_OUT or DEAL_ENTRY_INOUT
            for deal in out_deals:
                closed_trades.append({
                    "ticket": deal.ticket,
                    "symbol": deal.symbol,
                    "type": 0 if deal.type == 0 else 1,
                    "lots": deal.volume,
                    "open_price": deal.price_open,
                    "close_price": deal.price,
                    "profit": deal.profit,
                    "close_time": int(deal.time)
                })

        # Build full payload
        payload = {
            "challenge_id": int(challenge_id),
            "challenge_token": challenge_token,
            "ea_version": "1.0.0",
            "terminal_build": mt5.terminal_info().build if mt5.terminal_info() else 0,
            "heartbeat": int(time.time()),
            "account": account_json,
            "open_trades": open_trades,
            "closed_trades": closed_trades
        }

        # Send to main server
        resp = requests.post(
            SERVER_URL,
            json=payload,
            headers={"X-Internal-Key": INTERNAL_KEY},
            timeout=30
        )

        mt5.shutdown()

        if resp.status_code == 200:
            return True, "SUCCESS"
        else:
            return True, f"Server: {resp.status_code}"

    except Exception as e:
        try:
            import MetaTrader5 as mt5
            mt5.shutdown()
        except:
            pass
        return False, str(e)[:100]

def fetcher_loop():
    print("[FETCHER] Started background thread")
    while True:
        cycle_start = time.time()
        ist_now = datetime.now(IST).strftime("%I:%M:%S %p")
        print(f"[FETCHER] ========== New cycle at {ist_now} IST ==========")

        creds = get_active_credentials()
        print(f"[FETCHER] {len(creds)} active account(s) to process")

        if len(creds) == 0:
            print(f"[FETCHER] No active accounts. Waiting {CYCLE_MIN_INTERVAL} minutes...")
            time.sleep(CYCLE_MIN_INTERVAL * 60)
            continue

        for i, cred in enumerate(creds):
            cred_id = cred[0]
            mt5_login = cred[1]
            mt5_server = cred[3]
            challenge_id = cred[4]

            print(f"[FETCHER] [{i+1}/{len(creds)}] Login={mt5_login} | Server={mt5_server} | Challenge={challenge_id}")

            success, msg = fetch_single_account(cred)
            now_ist = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")
            result = f"SUCCESS - {msg}" if success else f"FAILED - {msg}"
            update_credential_status(cred_id, now_ist, result)
            print(f"[FETCHER]   -> {result}")

            time.sleep(2)  # Small delay between accounts

        cycle_duration = time.time() - cycle_start
        cycle_minutes = cycle_duration / 60
        print(f"[FETCHER] Cycle completed in {cycle_minutes:.1f} minutes")

        if cycle_minutes < CYCLE_MIN_INTERVAL:
            wait_seconds = (CYCLE_MIN_INTERVAL * 60) - cycle_duration
            wait_minutes = wait_seconds / 60
            print(f"[FETCHER] Waiting {wait_minutes:.1f} minutes until next cycle...")
            time.sleep(wait_seconds)
        else:
            print(f"[FETCHER] Cycle exceeded {CYCLE_MIN_INTERVAL} min. Starting next cycle immediately.")

# Start fetcher in background thread
fetcher_thread = threading.Thread(target=fetcher_loop, daemon=True)
fetcher_thread.start()

# ===========================
# MAIN
# ===========================
if __name__ == "__main__":
    print("=" * 50)
    print("  MT5 Monitor Dashboard")
    print(f"  URL: http://0.0.0.0:5002/dashboard")
    print(f"  Cycle interval: {CYCLE_MIN_INTERVAL} minutes")
    print(f"  Server: {SERVER_URL}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5002, debug=False)