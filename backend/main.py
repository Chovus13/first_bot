from fastapi import FastAPI, Request
from pydantic import BaseModel
import os, requests, json, sqlite3, time
from pathlib import Path
from dotenv import load_dotenv
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from threading import Thread
import ccxt
from datetime import datetime

load_dotenv()
key = os.getenv("API_KEY", "")[:4] + "..." + os.getenv("API_KEY", "")[-4:]
print(f"🔑 Using API_KEY: {key}")

app = FastAPI()

# === Static frontend ===
app.mount("/ui", StaticFiles(directory=Path(__file__).resolve().parent.parent / "web" / "dist", html=True), name="static")

# === Baza ===
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parents[1] / "user_data" / "chovusbot.db"))
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, price REAL, timestamp TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS score_log (timestamp TEXT, score INTEGER)''')
conn.commit()

def get_config(key: str, default=None):
    cursor.execute("SELECT value FROM config WHERE key=?", (key,))
    result = cursor.fetchone()
    return result[0] if result else default

def set_config(key: str, value: str):
    cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

def get_all_config():
    cursor.execute("SELECT key, value FROM config")
    return {k: v for k, v in cursor.fetchall()}

# === SCAN USDT FUTURES ===
def scan_top_pairs(limit=3):
    try:
        exchange = ccxt.binance({"options": {"defaultType": "future"}})
        print(f"[{datetime.now()}] Scanning top {limit} USDT Futures pairs...")
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        futures = [s for s in exchange.markets if s.endswith("/USDT") and exchange.markets[s].get('contract', False)]
        sorted_by_volume = sorted(
            [(s, tickers[s]['quoteVolume']) for s in futures if 'quoteVolume' in tickers[s]],
            key=lambda x: x[1],
            reverse=True
        )
        top = [s[0] for s in sorted_by_volume[:limit]]
        return top
    except Exception as e:
        print(f"❌ Error while scanning pairs: {e}")
        return []

def update_db_pairs(pairs):
    set_config("available_pairs", ",".join(pairs))

# === API modeli ===
class TelegramMessage(BaseModel):
    message: str

@app.get("/api/status")
def get_status():
    return {
        "status": "running" if bot_state["running"] else "paused",
        "active_strategy": get_config("strategy", "default")
    }

@app.get("/api/config")
def get_config_api():
    return get_all_config()

@app.post("/strategy")
async def update_strategy(request: Request):
    body = await request.json()
    new_strategy = body.get("strategy", "default")
    set_config("strategy", new_strategy)
    return {"message": "Strategy updated!", "strategy": new_strategy}

@app.get("/balance")
def get_balance():
    return {
        "wallet_balance": get_config("balance", "0"),
        "score": get_config("score", "0")
    }

@app.get("/trades")
def get_trades():
    cursor.execute("SELECT symbol, price, timestamp FROM trades ORDER BY id DESC LIMIT 20")
    return [{"symbol": s, "price": p, "time": t} for s, p, t in cursor.fetchall()]

@app.get("/pairs")
def get_pairs():
    return get_config("available_pairs", "").split(",")

@app.post("/send_telegram")
def send_telegram(msg: TelegramMessage):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"status": "❌ Missing token or chat_id in .env"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": msg.message}
    r = requests.post(url, data=data)
    return {"status": "✅ Sent!" if r.status_code == 200 else f"❌ Error: {r.text}"}

# === BOT kontrola ===
bot_state = {"running": False}

@app.post("/start")
def start_bot():
    if not bot_state["running"]:
        top_pairs = scan_top_pairs()
        if not top_pairs:
            top_pairs = ["BTC/USDT", "ETH/USDT", "OP/USDT"]
            print(f"⚠️ Fallback used: {top_pairs}")
        update_db_pairs(top_pairs)
        print(f"✅ Updated available_pairs: {top_pairs}")
        bot_state["running"] = True
        Thread(target=bot_loop, daemon=True).start()
    return {"message": "🚀 Bot started!"}

@app.post("/pause")
def pause_bot():
    bot_state["running"] = False
    return {"message": "⏸️ Bot paused."}

# === Logika trejda ===
def log_trade(symbol, price):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO trades (symbol, price, timestamp) VALUES (?, ?, ?)", (symbol, price, now))
    conn.commit()

def log_score(score):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO score_log (timestamp, score) VALUES (?, ?)", (now, score))
    conn.commit()

def bot_loop():
    while bot_state["running"]:
        pairs = get_config("available_pairs", "").split(",")
        for symbol in pairs:
            if not symbol.strip():
                continue
            try:
                print(f"🤖 Checking {symbol} ...")
                price_data = get_price(symbol)
                print(f"📈 Price data: {price_data}")
                price = price_data["price"]
                if price != "N/A":
                    log_trade(symbol, price)
                    log_score(int(get_config("score", "0")) + 1)
                    set_config("balance", str(float(get_config("balance", "0")) + 5))
                    set_config("score", str(int(get_config("score", "0")) + 1))
            except Exception as e:
                print(f"🔥 Crash while processing {symbol}: {e}")
        time.sleep(15)

@app.get("/api/price")
def get_price(symbol: str):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.replace('/', '')}"
        response = requests.get(url)
        data = response.json()
        if "price" in data:
            return {"symbol": symbol, "price": float(data["price"])}
        else:
            return {"symbol": symbol, "price": "N/A"}
    except Exception as e:
        print(f"❌ get_price error for {symbol}: {e}")
        return {"symbol": symbol, "price": "N/A"}


# === Telegram izveštaj ===
def send_report():
    while True:
        now = time.strftime("%H:%M")
        if now == get_config("report_time", "09:00"):
            msg = f"📊 ChovusBot Report:\nWallet = {get_config('balance', '0')}, Score = {get_config('score', '0')}"
            send_telegram(TelegramMessage(message=msg))
            time.sleep(60)
        time.sleep(30)

Thread(target=send_report, daemon=True).start()