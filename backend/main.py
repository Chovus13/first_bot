# main.py
from fastapi import FastAPI, Request, HTTPException
import ccxt.async_support as ccxt
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
import sqlite3
ChovusSmartBot_v9 import ChovusSmartBot, get_config, set_config



load_dotenv()

key = os.getenv("API_KEY", "")[:4] + "..." + os.getenv("API_KEY", "")[-4:]
print(f"🔑 Using API_KEY: {key}")

app = FastAPI()
templates = Jinja2Templates(directory="html")
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent / "user_data" / "chovusbot.db"))

# Inicijalizuj bota
bot = ChovusSmartBot()
bot_task = None

# API modeli
class TelegramMessage(BaseModel):
    message: str

class StrategyRequest(BaseModel):
    strategy_name: str

class LeverageRequest(BaseModel):
    leverage: int

class AmountRequest(BaseModel):
    amount: float

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})



async def start_bot(self):
    if self.running:
        log_action("Bot is already running.")
        return
    log_action("Bot starting...")
    self.running = True
    # Postavi leverage na početku (ako je potrebno)
    await self.set_leverage(self.leverage)  # Dodat await jer je set_leverage sada async
    self._bot_task = asyncio.create_task(self._main_bot_loop())
    if self._telegram_report_thread is None or not self._telegram_report_thread.is_alive():
        self._telegram_report_thread = threading.Thread(target=self._send_report_loop, daemon=True)
        self._telegram_report_thread.start()
    log_action("Bot started.")

@app.post("/api/stop")
async def stop_bot_endpoint():
    global bot_task
    if bot.running:
        try:
            bot.stop_bot()
            if bot._bot_task and not bot._bot_task.done():
                await bot._bot_task
            return {"status": "Bot stopped"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to stop bot: {e}")
    return {"status": "Bot is not running"}

@app.get("/api/status")
async def get_bot_status_endpoint():
    return {"status": bot.get_bot_status(), "strategy": bot.current_strategy}

@app.post("/api/restart")
async def restart_bot_endpoint():
    if bot.running:
        bot.stop_bot()
        if bot._bot_task and not bot._bot_task.done():
            await bot._bot_task
    await bot.start_bot()
    return {"status": "Bot restarted"}

@app.post("/api/set_strategy")
async def set_strategy_endpoint(request: StrategyRequest):
    strategy_status = bot.set_bot_strategy(request.strategy_name)
    return {"status": f"Strategy set to: {strategy_status}"}

@app.get("/api/config")
def get_config_api():
    return get_all_config()

@app.get("/api/balance")
def get_balance():
    return {
        "wallet_balance": get_config("balance", "0"),
        "score": get_config("score", "0")
    }

@app.get("/api/trades")
def get_trades():
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, price, timestamp, outcome FROM trades ORDER BY id DESC LIMIT 20")
            return [{"symbol": s, "price": p, "time": t, "outcome": o} for s, p, t, o in cursor.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching trades: {e}")

@app.get("/api/pairs")
def get_pairs():
    return get_config("available_pairs", "").split(",")

@app.post("/api/send_telegram")
async def send_telegram_endpoint(msg: TelegramMessage):
    return bot._send_telegram_message(msg.message)

@app.get("/api/market_data")
async def get_market_data(symbol: str = "ETH/BTC"):
    try:
        ticker = await bot.exchange.fetch_ticker(symbol)
        df = await bot.get_candles(symbol)
        high = df['high'].rolling(50).max().iloc[-1]
        low = df['low'].rolling(50).min().iloc[-1]
        fib_range = high - low
        support = high - fib_range * 0.618
        resistance = high - fib_range * 0.382
        smma = bot.calc_smma(df['close'], 5)
        wma = bot.calc_wma(df['close'], 144)
        trend = "Bullish" if smma.iloc[-1] > wma.iloc[-1] else "Bearish"
        return {
            "price": ticker['last'],
            "support": support,
            "resistance": resistance,
            "trend": trend
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching market data: {e}")






# U main.py, ažuriraj /api/candidates - polako
@app.get("/api/candidates")
async def get_candidates():
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, price, score, timestamp FROM candidates ORDER BY score DESC, id DESC LIMIT 10")
            return [{"symbol": s, "price": p, "score": sc, "time": t} for s, p, sc, t in cursor.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching candidates: {e}")


# @app.get("/api/signals")
# async def get_signals():
#     try:
#         with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
#             cursor = conn.cursor()
#             cursor.execute("SELECT symbol, price, timestamp, outcome FROM trades ORDER BY timestamp DESC LIMIT 10")
#             trades = cursor.fetchall()
#             signals = [
#                 {"symbol": t[0], "price": t[1], "time": t[2], "type": f"Trade ({t[3]})"}
#                 for t in trades
#             ]
#             return signals
#     except Exception as e:
#         logger.error(f"Error in /api/signals: {str(e)}")
#         return {"status": "error", "message": str(e)}

# U main.py, ažuriraj /api/signals
# U main.py, ažuriraj /api/signals endpoint
# @app.get("/api/signals")
# async def get_signals():
# try:
#     with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
#         cursor = conn.cursor()
#         cursor.execute("SELECT symbol, price, timestamp, outcome FROM trades ORDER BY timestamp DESC LIMIT 10")
#         trades = cursor.fetchall()
#         signals = [
#             {"symbol": t[0], "price": t[1], "time": t[2], "type": f"Trade ({t[3]})"}
#             for t in trades
#         ]
#         return signals
# except Exception as e:
#     logger.error(f"Error in /api/signals: {str(e)}")  # Pretpostavljam da imaš logger
#     return {"status": "error", "message": str(e)}

# U ChovusSmartBot_v9.py, ažuriraj set_leverage

@app.get("/api/signals")
async def get_signals():
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, price, timestamp, outcome FROM trades ORDER BY timestamp DESC LIMIT 10")
            trades = cursor.fetchall()
            signals = [
                {"symbol": t[0], "price": t[1], "time": t[2], "type": f"Trade ({t[3]})"}
                for t in trades
            ]
            return signals
    except Exception as e:
        logger.error(f"Error in /api/signals: {str(e)}")  # Pretpostavljam da imaš logger
        return {"status": "error", "message": str(e)}

async def set_leverage(self, leverage: int):  # Dodat async
    self.leverage = leverage
    log_action(f"Leverage set to: {leverage}x")
    try:
        await self.exchange.set_leverage(leverage, symbol=None)  # Dodat await
    except Exception as e:
        log_action(f"Error setting leverage: {e}")

@app.post("/api/set_manual_amount")
async def set_manual_amount(request: AmountRequest):
    try:
        bot.set_manual_amount(request.amount)
        set_config("manual_amount", str(request.amount))
        return {"status": f"Manual amount set to: {request.amount} USDT"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error setting manual amount: {e}")

@app.get("/api/logs")
async def get_logs():
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, message FROM bot_logs ORDER BY id DESC LIMIT 10")
            return [{"time": t, "message": m} for t, m in cursor.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching logs: {e}")

# Dodaj u main.py privremeni endpoint za testiranje
@app.get("/api/export_candidates")
async def export_candidates():
    from ChovusSmartBot_v9 import export_candidates_to_json
    export_candidates_to_json()
    return {"status": "Export triggered"}

@app.get("/api/test_scan")
async def test_scan():
    try:
        bot = ChovusSmartBot()  # Pretpostavljam da je bot globalna instanca
        targets = await bot._scan_pairs()
        return {"status": "Scan completed", "targets": targets}
    except Exception as e:
        return {"status": "Error", "message": str(e)}