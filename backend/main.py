from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import asyncio
from pydantic import BaseModel
import os, sqlite3, time
from pathlib import Path
from dotenv import load_dotenv
# from fastapi.staticfiles import StaticFiles
# from fastapi.responses import FileResponse
from threading import Thread
import ccxt
from datetime import datetime
import requests # Dodao sam requests, jer je falio iako se koristi

from ChovusSmartBot_v9 import ChovusSmartBot  # Pretpostavljam da je klasa tako nazvana

load_dotenv()

key = os.getenv("API_KEY", "")[:4] + "..." + os.getenv("API_KEY", "")[-4:]
print(f"🔑 Using API_KEY: {key}")

app = FastAPI()
templates = Jinja2Templates(directory="/app/html")  # Postavi direktorijum gde su HTML fajlovi

# === Baza ===
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parents[1] / "user_data" / "chovusbot.db"))

# Globalna konekcija, ali ćemo kursor kreirati za svaku operaciju
# Bitno je da konekcija bude dostupna, ali kursor ne sme biti deljen
conn = sqlite3.connect(DB_PATH, check_same_thread=False)

# Inicijalizacija tabele (ovo može i dalje koristiti privremeni kursor)
with conn:
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, price REAL, timestamp TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS score_log (timestamp TEXT, score INTEGER)''')
    conn.commit()


def get_config(key: str, default=None):
    with conn: # Koristi 'with' za automatski commit/rollback i za kursor
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key=?", (key,))
        result = cursor.fetchone()
        return result[0] if result else default

def set_config(key: str, value: str):
    with conn:
        cursor = conn.cursor()
        cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

def get_all_config():
    with conn:
        cursor = conn.cursor()
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
async def get_bot_status_endpoint():
    """Vraća status bota."""
    return {"status": bot.get_bot_status()}  # Pretpostavljam da je get_bot_status sync


@app.post("/api/stop")
async def stop_bot_endpoint():
    """Zaustavlja bota."""
    global bot_task
    if bot_task and not bot_task.done():
        bot.stop_bot()  # Pretpostavljam da stop_bot ima logiku za zaustavljanje
        await bot_task  # Sačekaj da se task završi
        bot_task = None
        return {"status": "Bot stopped"}
    return {"status": "Bot is not running"}

@app.get("/api/config")
def get_config_api():
    return get_all_config()


@app.post("/api/restart")
async def restart_bot_endpoint():
    """Restartuje bota."""
    global bot_task
    if bot_task and not bot_task.done():
        bot.stop_bot()
        await bot_task
        bot_task = None
    bot_task = asyncio.create_task(bot.start_bot())
    return {"status": "Bot restarted"}

@app.get("/api/current_strategy")
async def get_current_strategy_endpoint():
    return {"strategy": bot.current_strategy}

@app.post("/api/set_strategy")
async def set_strategy_endpoint(strategy_name: str):  # Primer, prilagodi ako treba više parametara
    """Postavlja strategiju za bota."""
    bot.set_bot_strategy(strategy_name)  # Pretpostavljam da set_bot_strategy prima strategiju
    return {"status": f"Strategy set to: {strategy_name}"}

@app.get("/api/balance") # Promenjeno iz /balance u /api/balance
def get_balance():
    # Ovde se poziva get_config, koji sada koristi svoj kursor, rešavajući problem
    return {
        "wallet_balance": get_config("balance", "0"),
        "score": get_config("score", "0")
    }

@app.get("/api/trades") # Promenjeno iz /trades u /api/trades
def get_trades():
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, price, timestamp FROM trades ORDER BY id DESC LIMIT 20")
        return [{"symbol": s, "price": p, "time": t} for s, p, t in cursor.fetchall()]

@app.get("/api/pairs") # Promenjeno iz /pairs u /api/pairs
def get_pairs():
    return get_config("available_pairs", "").split(",")

@app.post("/api/send_telegram") # Promenjeno iz /send_telegram u /api/send_telegram
def send_telegram(msg: TelegramMessage):
    token = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return {"status": "❌ Missing token or chat_id in .env"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": msg.message}
    r = requests.post(url, data=data)
    return {"status": "✅ Sent!" if r.status_code == 200 else f"❌ Error: {r.text}"}


# Inicijalizuj bota
bot = ChovusSmartBot()
bot_task = None  # Za čuvanje asyncio taska bota


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Prikazuje index.html stranicu."""
    return templates.TemplateResponse("index.html", {"request": request})


# --- API Endpoints za upravljanje botom ---

@app.post("/api/start")
async def start_bot_endpoint():
    """Startuje bota."""
    global bot_task
    if bot_task is None or bot_task.done():
        bot_task = asyncio.create_task(bot.start_bot())  # Pretpostavljam da je start_bot async
        return {"status": "Bot started"}
    return {"status": "Bot is already running"}

@app.post("/api/pause") # Promenjeno iz /pause u /api/pause
def pause_bot():
    # bot_state["running"] = False # Ova varijabla nije definisana, koristi bot.is_running
    bot.stop_bot() # Koristi metodu bota za pauziranje/zaustavljanje
    return {"message": "⏸️ Bot paused."}

# === Logika trejda ===
def log_trade(symbol, price):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO trades (symbol, price, timestamp) VALUES (?, ?, ?)", (symbol, price, now))
        conn.commit()

def log_score(score):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO score_log (timestamp, score) VALUES (?, ?)", (now, score))
        conn.commit()

def bot_loop():
    while bot.is_running(): # Koristi metodu bota za proveru statusa
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

@app.get("/api/price") # Promenjeno iz /api/price
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