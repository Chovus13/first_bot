from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
import requests # Dodati import za requests
from ChovusSmartBot_v9 import ChovusSmartBot, get_config, set_config, log_trade, log_score, cursor, conn # Uvezi potrebne funkcije i klase iz bota

load_dotenv()

key = os.getenv("API_KEY", "")[:4] + "..." + os.getenv("API_KEY", "")[-4:]
print(f"🔑 Using API_KEY: {key}")

app = FastAPI()

# Postavi direktorijum gde su HTML fajlovi
# AKO JE index.html U KORENU PROJEKTA, KORISTI directory="."
# AKO JE U PODFOLDERU "html", KORISTI directory="html"
# Prema tvom GitHubu, index.html je u korenu
templates = Jinja2Templates(directory=".") # PROMENJENO OVO!

# Inicijalizuj bota
bot = ChovusSmartBot()
bot_task = None  # Za čuvanje asyncio taska bota

# API modeli
class TelegramMessage(BaseModel):
    message: str

class StrategyRequest(BaseModel): # Dodao sam model za set_strategy
    strategy_name: str

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
        try:
            await bot.start_bot() # Pozovi asinhronu metodu start_bot klase ChovusSmartBot
            # bot.start_bot() unutra stvara asyncio.Task, ne treba nam ovde da ga čuvamo
            return {"status": "Bot started"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to start bot: {e}")
    return {"status": "Bot is already running"}

@app.post("/api/stop")
async def stop_bot_endpoint():
    """Zaustavlja bota."""
    global bot_task
    if bot.running: # Proveri status bota preko instance klase
        try:
            bot.stop_bot() # Ovo samo postavlja self.running = False, bot loop će reagovati
            # Sačekaj da se bot zaista zaustavi, ako je potrebno (može biti kompleksno)
            # Ako _bot_task postoji i nije done, možeš da ga čekaš.
            if bot._bot_task and not bot._bot_task.done():
                await bot._bot_task # Sačekaj da se glavni loop bota završi
            return {"status": "Bot stopped"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to stop bot: {e}")
    return {"status": "Bot is not running"}

@app.get("/api/status")
async def get_bot_status_endpoint():
    """Vraća status bota."""
    # Ispravno pozivanje metode bota za status
    return {"status": bot.get_bot_status()}

@app.post("/api/restart")
async def restart_bot_endpoint():
    """Restartuje bota."""
    if bot.running:
        bot.stop_bot()
        if bot._bot_task and not bot._bot_task.done():
            await bot._bot_task # Sačekaj da se zaustavi pre restarta
    await bot.start_bot()
    return {"status": "Bot restarted"}

@app.post("/api/set_strategy")
async def set_strategy_endpoint(request: StrategyRequest): # Koristi Pydantic model
    """Postavlja strategiju za bota."""
    strategy_status = bot.set_bot_strategy(request.strategy_name) # Pozovi metodu na instanci bota
    return {"status": f"Strategy set to: {strategy_status}"}

@app.get("/api/config") # Dodao sam prefix /api
def get_config_api():
    # Funkcija get_all_config mora biti definisana negde (može u bot fajlu)
    # Ili se sve config funkcije moraju prebaciti u main.py ili koristiti bot instancu
    from ChovusSmartBot_v9 import get_all_config # Privremeni import ako nije već globalno
    return get_all_config()

@app.get("/api/balance") # Dodao sam prefix /api
def get_balance():
    return {
        "wallet_balance": get_config("balance", "0"),
        "score": get_config("score", "0")
    }

@app.get("/api/trades") # Dodao sam prefix /api
def get_trades():
    cursor.execute("SELECT symbol, price, timestamp FROM trades ORDER BY id DESC LIMIT 20")
    return [{"symbol": s, "price": p, "time": t} for s, p, t in cursor.fetchall()]

@app.get("/api/pairs") # Dodao sam prefix /api
def get_pairs():
    return get_config("available_pairs", "").split(",")

@app.post("/api/send_telegram") # Dodao sam prefix /api
async def send_telegram_endpoint(msg: TelegramMessage):
    # Pozovi metodu bota za slanje Telegram poruka
    return bot._send_telegram_message(msg.message)

# OBAVEZNO: Ukloni sve while True petlje i blokirajući kod iz main.py
# Npr. ukloni bot_loop, scan_top_pairs, update_db_pairs, send_report Thread, get_price itd.
# Sve to treba da bude unutar ChovusSmartBot klase ili pomoćnih funkcija koje poziva bot.

# Bot startuje samo na zahtev korisnika preko /api/start
# Nema globalnog bot_state dictionary-ja u main.py, jer se stanje održava u instanci bota