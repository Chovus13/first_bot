import ccxt
import time
import math
import os
import json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import threading
import schedule
import requests # Dodati import za requests
from pandas import DataFrame
from typing import Optional, Union
from dotenv import load_dotenv
import asyncio
import sqlite3
from pathlib import Path

load_dotenv()

# === CONFIG ===
api_key = os.getenv('API_KEY')
api_secret = os.getenv('API_SECRET')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# DB setup (prebaciti ovo ovde, ili proslediti conn u bota)
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

def log_trade(symbol, price):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO trades (symbol, price, timestamp) VALUES (?, ?, ?)", (symbol, price, now))
    conn.commit()

def log_score(score):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO score_log (timestamp, score) VALUES (?, ?)", (now, score))
    conn.commit()

# Constants (mogu biti deo klase, ili globalne ako su fiksne)
SYMBOLS = []
ROUND_LEVELS = [0.01, 0.1, 0.5, 1, 5, 10, 50, 100, 500, 1000]
VOLUME_SPIKE_THRESHOLD = 1.5
TRADE_DURATION_LIMIT = 20
STOP_LOSS_PERCENT = 0.01
TRAILING_TP_STEP = 0.005
TRAILING_TP_OFFSET = 0.02

# Uklonio sam DAILY_LOG i LOG_FILE jer se baza koristi za logovanje

class ChovusSmartBot:
    def __init__(self):
        self.running = False
        self.current_strategy = "Default"
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future'
            }
        })
        self._bot_task = None # Čuvamo referencu na asyncio task za kontrolu

    async def start_bot(self):
        """Asinhrona funkcija za startovanje bota."""
        if self.running:
            print("Bot is already running.")
            return

        print("Bot starting...")
        self.running = True
        self._bot_task = asyncio.create_task(self._main_bot_loop())
        # Pokreni Telegram report u zasebnom threadu ako ne blokira main loop
        threading.Thread(target=self._send_report_loop, daemon=True).start()
        print("Bot started.")

    def stop_bot(self):
        """Zaustavlja bota."""
        if not self.running:
            print("Bot is not running.")
            return

        print("Bot stopping...")
        self.running = False
        # Ne treba nam await self._bot_task ovde, samo ga zaustavi
        # Logika unutar _main_bot_loop će reagovati na self.running = False

    def get_bot_status(self):
        """Vraća status bota."""
        return "Running" if self.running else "Stopped"

    def set_bot_strategy(self, strategy_name: str):
        """Postavlja strategiju za bota."""
        self.current_strategy = strategy_name
        print(f"Strategy set to: {strategy_name}")
        return self.current_strategy

    def smart_allocation(self, score):
        if score > 0.9: return 0.5
        elif score > 0.8: return 0.3
        elif score > 0.7: return 0.2
        else: return 0.1

    def learn_from_history(self):
        try:
            cursor.execute("SELECT symbol, price FROM trades") # Ovo treba da se prilagodi logovanju TP/SL
            data = cursor.fetchall() # Prilagodi ovo da uzme status trejda (TP/SL)
            df = pd.DataFrame(data, columns=['symbol', 'price']) # Nije dovoljno, treba ti status (TP/SL)
            if df.empty:
                print("[LEARN] No data to learn from.")
                return
            # Implementiraj logiku za analizu istorije trejdova iz baze (koja mora da sadrzi ishod TP/SL)
            print("[LEARN] Performance summary (placeholder, implement analysis based on trade outcomes from DB)")
        except Exception as e:
            print(f"[LEARN] Error analyzing history: {e}")

    def get_candles(self, symbol, timeframe='15m', limit=100):
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df

    def calc_smma(self, series, length):
        smma = [series.iloc[0]]
        for i in range(1, len(series)):
            smma.append((smma[-1] * (length - 1) + series.iloc[i]) / length)
        return pd.Series(smma, index=series.index)

    def calc_wma(self, series, length):
        weights = range(1, length + 1)
        return series.rolling(length).apply(lambda prices: sum(weights[i] * prices[i] for i in range(length)) / sum(weights), raw=True)

    def confirm_smma_wma_crossover(self, df):
        smma = self.calc_smma(df['close'], 5)
        wma = self.calc_wma(df['close'], 144)
        return smma.iloc[-2] < wma.iloc[-2] and smma.iloc[-1] > wma.iloc[-1]

    def fib_zone_check(self, df):
        high = df['high'].rolling(50).max()
        low = df['low'].rolling(50).min()
        fib_range = high - low
        fib_382 = high - fib_range * 0.382
        fib_618 = high - fib_range * 0.618
        latest_price = df['close'].iloc[-1]
        return fib_618.iloc[-1] <= latest_price <= fib_382.iloc[-1]

    def is_near_round(self, price):
        for level in ROUND_LEVELS:
            if abs(price % level - level) < 0.01 * level or price % level < 0.01 * level:
                return True
        return False

    def ai_score(self, price, volume, avg_volume, crossover, in_fib_zone):
        score = 0
        if self.is_near_round(price): score += 1
        if volume > avg_volume * VOLUME_SPIKE_THRESHOLD: score += 1
        if crossover: score += 1.5
        if in_fib_zone: score += 0.5
        return min(score / 4.0, 1.0)

    async def _scan_pairs(self):
        markets = self.exchange.load_markets()
        pairs = []
        for symbol, info in markets.items():
            if '/USDT' in symbol and info.get('future', False):
                try:
                    ticker = self.exchange.fetch_ticker(symbol)
                    volume = ticker.get('quoteVolume', 0)
                    price = ticker.get('last', 0)
                    if volume and price and price > 0:
                        df = self.get_candles(symbol)
                        crossover = self.confirm_smma_wma_crossover(df)
                        in_fib_zone = self.fib_zone_check(df)
                        # Placeholder for avg_volume (implement real calculation)
                        avg_volume = volume # Ovde treba da bude pravi prosečan volumen
                        score = self.ai_score(price, volume, avg_volume, crossover, in_fib_zone)
                        if score > 0.6:
                            pairs.append((symbol, price, volume, score))
                except Exception as e:
                    print(f"Error scanning {symbol}: {e}")
                    continue
        pairs.sort(key=lambda x: x[3], reverse=True)
        return pairs[:3]

    async def _monitor_trade(self, symbol, entry_price):
        print(f"[TRAILING TP/SL] Monitoring {symbol}")
        tp = entry_price * (1 + TRAILING_TP_OFFSET)
        sl = entry_price * (1 - STOP_LOSS_PERCENT)
        highest_price = entry_price

        while self.running: # Dodao self.running uslov
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price = ticker['last']

                if price > highest_price:
                    highest_price = price
                    tp = highest_price * (1 - TRAILING_TP_STEP)

                if price >= tp:
                    print(f"[TP] Exiting {symbol} at {price:.4f}")
                    # Ažuriraj config baze
                    current_balance = float(get_config("balance", "0"))
                    current_score = int(get_config("score", "0"))
                    set_config("balance", str(current_balance + (price - entry_price) * 10)) # Primer profita
                    set_config("score", str(current_score + 1))
                    log_trade(symbol, price) # Log trade ishod
                    await self._execute_sell_order(symbol, 'ALL') # Asinhrona funkcija
                    break

                if price <= sl:
                    print(f"[SL] Exiting {symbol} at {price:.4f}")
                    current_balance = float(get_config("balance", "0"))
                    current_score = int(get_config("score", "0"))
                    set_config("balance", str(current_balance - (entry_price - price) * 10)) # Primer gubitka
                    set_config("score", str(current_score - 1)) # Smanji score za gubitak
                    log_trade(symbol, price) # Log trade ishod
                    await self._execute_sell_order(symbol, 'ALL')
                    break

                await asyncio.sleep(2) # Koristi asyncio.sleep za neblokirajuće čekanje
            except Exception as e:
                print(f"Error monitoring trade for {symbol}: {e}")
                await asyncio.sleep(5) # Pričekaj duže ako ima greške

    async def _execute_buy_order(self, symbol, quantity):
        try:
            # Implementiraj stvarno kupovanje
            order = self.exchange.create_market_buy_order(symbol, quantity)
            print(f"Executed BUY order for {symbol}: {order}")
            return order
        except Exception as e:
            print(f"Error executing buy order for {symbol}: {e}")
            return None

    async def _execute_sell_order(self, symbol, quantity):
        try:
            # Implementiraj stvarno prodavanje
            order = self.exchange.create_market_sell_order(symbol, quantity)
            print(f"Executed SELL order for {symbol}: {order}")
            return order
        except Exception as e:
            print(f"Error executing sell order for {symbol}: {e}")
            return None

    async def _main_bot_loop(self):
        """Glavna asinhrona petlja bota."""
        print("[BOT] Starting main bot loop...")
        while self.running:
            try:
                self.learn_from_history() # Ovo može biti asinhrono ako radi puno DB operacija
                targets = await self._scan_pairs() # Pozovi asinhronu funkciju
                print(f"Found {len(targets)} high-score targets")
                for (symbol, price, volume, score) in targets:
                    order, entry_price = await self._open_long(symbol, score)
                    if order:
                        # Pokreni monitor_trade kao zaseban task da ne bi blokirao main loop
                        asyncio.create_task(self._monitor_trade(symbol, entry_price))
                    # Ako želiš da limitiraš trajanje trejda, možeš koristiti asyncio.wait_for
                    # try:
                    #     await asyncio.wait_for(self._monitor_trade(symbol, entry_price), timeout=TRADE_DURATION_LIMIT)
                    # except asyncio.TimeoutError:
                    #     print(f"Trade for {symbol} timed out after {TRADE_DURATION_LIMIT} seconds.")
                    #     # Ovdje implementiraj logiku za zatvaranje pozicije ako istekne vrijeme
            except Exception as ex:
                print(f"Main bot loop error: {str(ex)}")
            await asyncio.sleep(10) # Čekaj 10 sekundi pre sledećeg skeniranja

    async def _open_long(self, symbol, score):
        print(f"[LONG] Opening position on {symbol} with score {score:.2f}")
        try:
            market = self.exchange.market(symbol)
            ticker = self.exchange.fetch_ticker(symbol)
            price = ticker['ask']
            balance = self.exchange.fetch_balance({"type": "future"})
            usdt_balance = balance['total']['USDT'] * 0.99
            alloc = self.smart_allocation(score)
            quantity = round((usdt_balance * alloc * 10) / price, int(market['precision']['amount']))
            order = await self._execute_buy_order(symbol, quantity)
            return order, price
        except Exception as e:
            print(f"Error opening long position for {symbol}: {e}")
            return None, None

    def _send_telegram_message(self, message):
        token = TELEGRAM_BOT_TOKEN
        chat_id = TELEGRAM_CHAT_ID
        if not token or not chat_id:
            print("❌ Missing Telegram token or chat_id in .env")
            return {"status": "❌ Missing token or chat_id in .env"}
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        try:
            r = requests.post(url, data=data)
            print(f"Telegram status: {r.status_code}, Response: {r.text}")
            return {"status": "✅ Sent!" if r.status_code == 200 else f"❌ Error: {r.text}"}
        except Exception as e:
            print(f"Telegram send error: {e}")
            return {"status": f"❌ Exception: {e}"}

    def _send_report_loop(self):
        # Ovo se pokreće u zasebnom threadu, pa je ok da koristi time.sleep
        while self.running:
            now = datetime.now().strftime("%H:%M")
            report_time = get_config("report_time", "09:00")
            if now == report_time:
                msg = f"📊 ChovusBot Report:\nWallet = {get_config('balance', '0')}, Score = {get_config('score', '0')}"
                self._send_telegram_message(msg)
                time.sleep(60) # Sprečava slanje više poruka u istom minutu
            time.sleep(30) # Proveri svakih 30 sekundi