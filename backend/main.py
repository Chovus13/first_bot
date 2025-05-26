from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
import requests # Uvezi requests za get_price ako je potrebno (mada get_price treba u bota)
from ChovusSmartBot_v9 import ChovusSmartBot, get_config, set_config, get_all_config, cursor # Uvezi potrebne funkcije i klase, dodao cursor za trades

load_dotenv()

key = os.getenv("API_KEY", "")[:4] + "..." + os.getenv("API_KEY", "")[-4:]
print(f"🔑 Using API_KEY: {key}")

app = FastAPI()

# Postavi direktorijum gde su HTML fajlovi.
# Prema GitHub strukturi, ako se main.py izvršava iz korena "first_bot"
# a index.html je u "html" folderu, onda je ovo ispravno.
templates = Jinja2Templates(directory="html")

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
    try:
        await bot.start_bot()
        return {"status": "Bot started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start bot: {e}")

@app.post("/api/stop")
async def stop_bot_endpoint():
    """Zaustavlja bota."""
    try:
        bot.stop_bot()
        # Optional: Ako želiš da sačekaš da se glavna petlja bota zaista završi,
        # treba ti mehanizam unutar bota da signalizira kraj taska.
        # Npr. await bot._bot_main_task if bot._bot_main_task and not bot._bot_main_task.done() else None
        return {"status": "Bot stopping (may take a moment to fully halt)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop bot: {e}")

@app.get("/api/status")
async def get_bot_status_endpoint():
    """Vraća status bota."""
    return {"status": bot.get_bot_status()}

@app.post("/api/restart")
async def restart_bot_endpoint():
    """Restartuje bota."""
    try:
        if bot.running:
            bot.stop_bot()
            # Mali delay da se trenutni ciklus završi pre novog starta
            await asyncio.sleep(2)
        await bot.start_bot()
        return {"status": "Bot restarted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restart bot: {e}")

@app.post("/api/set_strategy")
async def set_strategy_endpoint(request: StrategyRequest):
    """Postavlja strategiju za bota."""
    bot.set_bot_strategy(request.strategy_name)
    return {"status": f"Strategy set to: {request.strategy_name}"}

@app.get("/api/config")
async def get_config_api():
    return get_all_config()

@app.get("/api/balance")
async def get_balance_api():
    # Uzmi najnovije vrednosti iz baze
    return {
        "wallet_balance": float(get_config("balance", "0")),
        "score": int(get_config("score", "0"))
    }

@app.get("/api/trades")
async def get_trades_api():
    # Preuzmi trejdove direktno iz baze
    cursor.execute("SELECT symbol, price, timestamp, outcome FROM trades ORDER BY id DESC LIMIT 20")
    return [{"symbol": s, "price": p, "time": t, "outcome": o} for s, p, t, o in cursor.fetchall()]

@app.get("/api/pairs")
async def get_pairs_api():
    pairs_str = get_config("available_pairs", "")
    return pairs_str.split(",") if pairs_str else []

@app.post("/api/send_telegram")
async def send_telegram_endpoint(msg: TelegramMessage):
    # Pozovi metodu bota za slanje Telegram poruka
    return bot._send_telegram_message(msg.message)


# Uklonjene sve blokirajuće while True petlje i funkcije iz main.py
# Uklonjena referenca na bot_state jer se stanje održava u instanci bota