from fastapi import FastAPI, WebSocket, HTTPException, Request, Form
from pydantic import BaseModel
import os, json, sqlite3, time
from pathlib import Path
from dotenv import load_dotenv
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, HTMLResponse
from threading import Thread
import ccxt
from datetime import datetime
import logging
from typing import Dict, Any, Optional, Set, List
import asyncio

## Telegram importi
from telegram import Update
from telegram.ext import Application, CommandHandler, \
    ContextTypes  # Izbacen MessageHandler i filters ako nisu direktno korisceni za komande

# Učitavanje .env fajla na početku
load_dotenv()

# Importi iz vaših drugih modula (prilagodite putanje ako je potrebno)
# Pretpostavljam da su ovi fajlovi u istom (root) direktorijumu kao main.py
try:
    from levels import generate_signals
    from orderbook import filter_walls, detect_trend
    from config import set_Strategija_status, \
        get_Strategija_status  # , TARGET_DIGITS, SPECIAL_DIGITS, PROFIT_TARGET # Ostali importi iz config.py ako su potrebni globalno
except ImportError as e:
    logging.error(
        f"Greška pri importovanju lokalnih modula (levels, orderbook, config): {e}. Proverite da li su fajlovi u root direktorijumu.")



key = os.getenv("API_KEY", "")[:4] + "..." + os.getenv("API_KEY", "")[-4:]
print(f"🔑 Using API_KEY: {key}")

app = FastAPI()
#templates = Jinja2Templates(directory="templates")


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


# Osnovna konfiguracija logging-a za ispis u konzoli
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Ispis na konzolu (stderr)
    ]
)
logger = logging.getLogger(__name__)  # Glavni logger za main.py

# Čuvanje aktivnih WebSocket konekcija
active_connections: Set[WebSocket] = set()

# Globalna instanca za Telegram Bot Application
telegram_bot_app: Optional[Application] = None
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


class WebSocketLoggingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs_buffer: List[Dict[str, Any]] = []  # Eksplicitno tipiziranje

    def emit(self, record):
        log_entry = self.format(record)
        # Formatiranje za slanje preko WebSocket-a
        self.logs_buffer.append({"message": log_entry, "level": record.levelname, "logger_name": record.name})
        if len(self.logs_buffer) > 100:  # Ograničenje bafera
            self.logs_buffer.pop(0)

    async def broadcast_logs(self):
        if self.logs_buffer and active_connections:
            logs_to_send = list(self.logs_buffer)  # Kopija za slanje
            self.logs_buffer.clear()

            disconnected_clients: Set[WebSocket] = set()
            for websocket in active_connections:
                try:
                    await websocket.send_json({"type": "logs", "data": logs_to_send})
                except Exception:
                    logger.warning(
                        f"Greška pri slanju logova na WebSocket klijenta {websocket.client.host if websocket.client else 'unknown client'}. Klijent će biti uklonjen.")
                    disconnected_clients.add(websocket)

            for client in disconnected_clients:
                if client in active_connections:
                    active_connections.remove(client)


ws_logging_handler = WebSocketLoggingHandler()
logging.getLogger().addHandler(ws_logging_handler)
logging.getLogger().setLevel(logging.DEBUG)  # Hvata sve od DEBUG nivoa pa naviše

app = FastAPI(title="Haos Bot API", version="1.0.0")


# --- Telegram Bot Funkcije ---
async def send_telegram_message(message: str):
    if telegram_bot_app and TELEGRAM_CHAT_ID:
        try:
            await telegram_bot_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
            logger.info(f"Telegram poruka poslata na chat_id {TELEGRAM_CHAT_ID[:4]}...: {message}")
        except Exception as e:
            logger.error(f"Greška pri slanju Telegram poruke: {e}")
    else:
        if not telegram_bot_app:
            logger.warning("Pokušaj slanja Telegram poruke, ali bot nije inicijalizovan.")
        if not TELEGRAM_CHAT_ID:
            logger.warning("Pokušaj slanja Telegram poruke, ali TELEGRAM_CHAT_ID nije podešen.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_info = update.effective_user.username if update.effective_user else 'nepoznat korisnik'
    logger.info(f"Telegram komanda /start primljena od korisnika {user_info}")
    reply_text = "Pozdrav! Haos Bot je aktivan. WebSocket logovi su omogućeni."
    await update.message.reply_text(reply_text)
    await send_telegram_message("Bot je uspešno startovan i komunicira preko Telegrama.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_info = update.effective_user.username if update.effective_user else 'nepoznat korisnik'
    logger.info(f"Telegram komanda /status primljena od korisnika {user_info}")
    status_message = (
        f"Haos Bot Status:\n"
        # f"- Strategija: {get_Strategija_status()}\n"
        f"- Broj aktivnih WebSocket konekcija: {len(active_connections)}\n"
        f"- API ključ učitan: {'Da' if os.getenv('API_KEY') else 'Ne'}\n"
        f"- Telegram Chat ID konfigurisan: {'Da' if TELEGRAM_CHAT_ID else 'Ne'}"
    )
    await update.message.reply_text(status_message)

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


# --- WebSocket Endpoint ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    client_host = websocket.client.host if websocket.client else "unknown_host"
    client_port = websocket.client.port if websocket.client else "unknown_port"
    logger.info(f"Nova WebSocket konekcija uspostavljena sa {client_host}:{client_port}")
    await send_telegram_message(f"Nova WebSocket konekcija uspostavljena sa klijentom: {client_host}")
    try:
        if ws_logging_handler.logs_buffer:
            await websocket.send_json({"type": "initial_logs", "data": list(ws_logging_handler.logs_buffer)})

        while True:
            # Čekanje na poruku od klijenta, ali ne zahteva se aktivno slanje od klijenta da bi veza ostala aktivna
            # Možete dodati timeout ako želite da server proverava konekciju
            data = await websocket.receive_text()
            logger.info(f"Primljena poruka preko WebSocket-a od {client_host}: {data}")
            # Možete dodati logiku za obradu klijentskih poruka ako je potrebno
            # Npr. ako klijent pošalje "PING", odgovorite sa "PONG"
            if data.upper() == "PING":
                await websocket.send_text("PONG")
            elif data == "REQUEST_BOT_DATA":
                bot_data = await get_bot_data_internal()
                await websocket.send_json(bot_data)
            else:
                await websocket.send_json({"status": "primljeno", "original_message": data,
                                           "reply": "Poruka primljena, ali nema definisane akcije."})


    except Exception as e:
        logger.warning(f"WebSocket konekcija sa {client_host} prekinuta ili greška: {e}")
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)
        logger.info(f"WebSocket konekcija sa {client_host} zatvorena.")
        await send_telegram_message(f"WebSocket konekcija zatvorena sa klijentom: {client_host}")



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

 # @app.post("/strategy")
 # async def update_strategy(request: Request):
 #     body = await requests.json()
 #     new_strategy = body.get("strategy", "default")
 #     set_config("strategy", new_strategy)
 #     return {"message": "Strategy updated!", "strategy": new_strategy}

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


@app.post("/telegram/send_test_message")
async def send_test_telegram_message_endpoint(data: Dict[str, str]):
    message = data.get("message")
    if message is None:
        raise HTTPException(status_code=400, detail="JSON mora sadržati 'message' ključ.")

    logger.info(f"Primljen zahtev za slanje testne Telegram poruke: {message}")
    await send_telegram_message(message)
    return JSONResponse({"status": "success", "message": f"Pokušano slanje poruke: '{message}'"})

async def get_bot_data_internal():
    # Ova funkcija treba da prikupi stvarne podatke od vašeg bota
    # Trenutno vraća placeholder vrednosti i koristi placeholder pozive
    current_price_example = 0.055

    # Primer kako biste mogli dobiti zidove i trend (zahteva `orderbook` objekat)
    # orderbook_example = await some_function_to_get_orderbook() # Morate ovo implementirati
    # walls_example = filter_walls(orderbook_example, current_price_example)
    # trend_example = detect_trend(orderbook_example, current_price_example)
    # signals_example = generate_signals(current_price_example, walls_example, trend_example, get_Strategija_status())

    # Logovanje "razmišljanja" bota prilikom generisanja signala:
    # if signals_example:
    #    for signal in signals_example:
    #        logger.info(f"Generisan Signal: Tip={signal.get('type')}, Ulaz={signal.get('entry_price')}, "
    #                    f"SL={signal.get('stop_loss')}, TP={signal.get('take_profit')}, "
    #                    f"Confidence={signal.get('confidence')}")
    #        await send_telegram_message(f"Novi signal: {signal.get('type')} @ {signal.get('entry_price')}")

    # Placeholder vrednosti:
    walls_example = {'support': [{'price': 0.054, 'volume': 10}], 'resistance': [{'price': 0.056, 'volume': 12}]}
    trend_example = "NEUTRAL"
    signals_example = [
        {"type": "LONG_EXAMPLE", "entry_price": 0.055, "stop_loss": 0.054, "take_profit": 0.056, "confidence": 0.75}]
    active_trades_example = []

    logger.debug("Prikupljanje podataka za /get_data ili WebSocket zahtev.")
    return {
        "price": current_price_example,
        "support_walls_count": len(walls_example.get('support', [])),
        "resistance_walls_count": len(walls_example.get('resistance', [])),
        "support_walls_details": walls_example.get('support', []),
        "resistance_walls_details": walls_example.get('resistance', []),
        "trend": trend_example,
        "signals": signals_example,
        "Strategija_status": get_Strategija_status(),
        "active_trades": active_trades_example,
        "api_key_loaded": bool(os.getenv("API_KEY")),
        "telegram_configured": bool(telegram_bot_app and TELEGRAM_CHAT_ID)
    }


# --- Pozadinski zadaci ---
async def background_tasks():
    logger.info("Pokretanje pozadinskih zadataka (slanje logova preko WebSocket-a).")
    api_key_short = (os.getenv('API_KEY')[:4] + "...") if os.getenv('API_KEY') else "NijePostavljen"
    logger.info(f"API ključ (prva 4 karaktera): {api_key_short}")

    # Ovde bi trebala da bude vaša glavna petlja bota ili poziv funkcije koja je pokreće
    # logger.info("Botova glavna petlja (simulacija) se pokreće u pozadini...")
    # primer: asyncio.create_task(vasa_glavna_bot_petlja())

    while True:
        await ws_logging_handler.broadcast_logs()
        # Ovde možete dodati i druge periodične zadatke vašeg bota
        await asyncio.sleep(1)  # Slanje logova svake sekunde


# --- FastAPI Startup i Shutdown događaji ---
@app.on_event("startup")
async def startup_event():
    global telegram_bot_app
    logger.info("FastAPI aplikacija se pokreće...")

    asyncio.create_task(background_tasks())

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if telegram_token and TELEGRAM_CHAT_ID:
        logger.info("Inicijalizacija Telegram bota...")
        try:
            telegram_bot_app = Application.builder().token(telegram_token).build()
            telegram_bot_app.add_handler(CommandHandler("start", start_command))
            telegram_bot_app.add_handler(CommandHandler("status", status_command))

            await telegram_bot_app.initialize()
            await telegram_bot_app.start()
            # Pokretanje polling-a kao zasebnog asyncio task-a
            asyncio.create_task(telegram_bot_app.updater.start_polling(poll_interval=1))
            logger.info("Telegram bot je inicijalizovan i pokrenut (polling).")

            # Ne šaljite poruku ovde ako updater nije još uvek pokrenut, može izazvati grešku.
            # Umesto toga, možete je poslati iz start_command ili nakon kratkog sleep-a.
            # Privremeno odloženo slanje inicijalne poruke:
            async def delayed_start_message():
                await asyncio.sleep(2)  # Dajte vremena updater-u da se pokrene
                await send_telegram_message("Haos Bot je online i povezan na Telegram!")

            asyncio.create_task(delayed_start_message())

        except Exception as e:
            logger.error(f"Greška pri inicijalizaciji Telegram bota: {e}")
            telegram_bot_app = None  # Resetuj da ne bi bilo pokušaja korišćenja neispravnog bota
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN ili TELEGRAM_CHAT_ID nisu podešeni u .env fajlu. Telegram bot neće biti aktivan.")

    logger.info("FastAPI aplikacija je uspešno pokrenuta.")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("FastAPI aplikacija se zaustavlja...")
    if telegram_bot_app and telegram_bot_app.updater and telegram_bot_app.updater.running:
        logger.info("Zaustavljanje Telegram bota (polling)...")
        await telegram_bot_app.updater.stop()
        await telegram_bot_app.stop()
        logger.info("Telegram bot je zaustavljen.")

    # Čekanje da se preostale Telegram poruke pošalju ako je moguće (ovo je aproksimacija)
    await asyncio.sleep(1)
    await send_telegram_message("Haos Bot se gasi.")  # Pokušaj slanja poslednje poruke
    logger.info("FastAPI aplikacija je zaustavljena.")

# Deo za direktno pokretanje (ako ne koristite Docker ili uvicorn komandu direktno)
# if __name__ == "__main__":
#     import uvicorn
#     logger.info("Pokretanje FastAPI servera direktno sa uvicorn za razvoj...")
#     uvicorn.run("main:app", host="0.0.0.0", port=8024, reload=True)