# ChovusSmartBot_v9.py
import ccxt.async_support as ccxt
import time
import math
import os
import json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import threading
import schedule
import requests
from pandas import DataFrame
from typing import Optional, Union
from dotenv import load_dotenv
import asyncio
import sqlite3
from pathlib import Path

load_dotenv()

# DB setup
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent / "user_data" / "chovusbot.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def init_db():
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, price REAL, timestamp TEXT, outcome TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS score_log (timestamp TEXT, score INTEGER)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS bot_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, message TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, symbol TEXT, price REAL, score REAL)''')
        conn.commit()

init_db()  # Inicijalizuj bazu pri pokretanju

def get_config(key: str, default=None):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key=?", (key,))
        result = cursor.fetchone()
        return result[0] if result else default

def set_config(key: str, value: str):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

def get_all_config():
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM config")
        return {k: v for k, v in cursor.fetchall()}

def log_trade(symbol, price, outcome):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO trades (symbol, price, timestamp, outcome) VALUES (?, ?, ?, ?)", (symbol, price, now, outcome))
        conn.commit()

def log_score(score):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO score_log (timestamp, score) VALUES (?, ?)", (now, score))
        conn.commit()

# U ChovusSmartBot_v9.py, ažuriraj log_candidate i dodaj export_candidates_to_json
def log_candidate(symbol, price, score):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO candidates (timestamp, symbol, price, score) VALUES (?, ?, ?, ?)", (now, symbol, price, score))
        conn.commit()
    export_candidates_to_json()

def export_candidates_to_json():
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, symbol, price, score FROM candidates ORDER BY id DESC LIMIT 10")
            candidates = [{"time": t, "symbol": s, "price": p, "score": sc} for t, s, p, sc in cursor.fetchall()]
            with open(DB_PATH.parent / "candidates.json", "w") as f:
                json.dump(candidates, f, indent=2)
    except Exception as e:
        log_action(f"Error exporting candidates to JSON: {e}")

def log_action(message):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        cursor = conn.cursor()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO bot_logs (timestamp, message) VALUES (?, ?)", (now, message))
        conn.commit()

# Constants
SYMBOLS = []
ROUND_LEVELS = [0.01, 0.1, 0.5, 1, 5, 10, 50, 100, 500, 1000]
VOLUME_SPIKE_THRESHOLD = 1.5
TRADE_DURATION_LIMIT = 60 * 10
STOP_LOSS_PERCENT = 0.01
TRAILING_TP_STEP = 0.005
TRAILING_TP_OFFSET = 0.02

class ChovusSmartBot:
    def __init__(self):
        self.running = False
        self.current_strategy = "Default"
        self.leverage = int(get_config("leverage", "10"))
        self.manual_amount = float(get_config("manual_amount", "0"))
        self._bot_task = None
        self._telegram_report_thread = None
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('API_KEY'),
            'secret': os.getenv('API_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        if get_config("balance") is None:
            set_config("balance", "1000.0")
        if get_config("score") is None:
            set_config("score", "0")
        if get_config("report_time") is None:
            set_config("report_time", "09:00")

    async def start_bot(self):
        if self.running:
            log_action("Bot is already running.")
            return
        log_action("Bot starting...")
        self.running = True
        self._bot_task = asyncio.create_task(self._main_bot_loop())
        if self._telegram_report_thread is None or not self._telegram_report_thread.is_alive():
            self._telegram_report_thread = threading.Thread(target=self._send_report_loop, daemon=True)
            self._telegram_report_thread.start()
        log_action("Bot started.")

    def stop_bot(self):
        if not self.running:
            log_action("Bot is not running.")
            return
        log_action("Bot stopping...")
        self.running = False

    def get_bot_status(self):
        return "Running" if self.running else "Stopped"

    def set_bot_strategy(self, strategy_name: str):
        self.current_strategy = strategy_name
        log_action(f"Strategy set to: {strategy_name}")
        return strategy_name

    def set_leverage(self, leverage: int):
        self.leverage = leverage
        log_action(f"Leverage set to: {leverage}x")
        try:
            self.exchange.set_leverage(leverage, symbol=None)
        except Exception as e:
            log_action(f"Error setting leverage: {e}")

    def set_manual_amount(self, amount: float):
        self.manual_amount = amount
        log_action(f"Manual amount set to: {amount} USDT")

    def smart_allocation(self, score):
        if self.manual_amount > 0:
            return self.manual_amount / float(get_config("balance", "1000"))
        if score > 0.9:
            return 0.5
        elif score > 0.8:
            return 0.3
        elif score > 0.7:
            return 0.2
        else:
            return 0.1

    async def learn_from_history(self):
        try:
            with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT symbol, price, outcome FROM trades")
                data = cursor.fetchall()
                df = pd.DataFrame(data, columns=['symbol', 'price', 'outcome'])
                if df.empty:
                    log_action("No trade data to learn from.")
                    return
                summary = df.groupby("symbol")["outcome"].value_counts().unstack().fillna(0)
                log_action(f"Performance summary: {summary.to_dict()}")
        except Exception as e:
            log_action(f"Error analyzing history: {e}")

    async def get_candles(self, symbol, timeframe='15m', limit=100):
        ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df

    def calc_smma(self, series, length):
        smma = [series.iloc[0]]
        for i in range(1, len(series)):
            smma.append((smma[-1] * (length - 1) + series.iloc[i]) / length)
        return pd.Series(smma, index=series.index)

    def calc_wma(self, series, length):
        weights = range(1, length + 1)
        return series.rolling(length).apply(
            lambda prices: sum(weights[i] * prices[i] for i in range(length)) / sum(weights), raw=True)

    def confirm_smma_wma_crossover(self, df):
        if len(df) < 144: return False
        smma = self.calc_smma(df['close'], 5)
        wma = self.calc_wma(df['close'], 144)
        return smma.iloc[-2] < wma.iloc[-2] and smma.iloc[-1] > wma.iloc[-1]

    def fib_zone_check(self, df):
        if len(df) < 50: return False
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

    # U ChovusSmartBot_v9.py, ažuriraj _scan_pairs metodu
    async def _scan_pairs(self, limit=5):
        log_action("Starting pair scanning...")
        try:
            markets = await self.exchange.load_markets()
            pairs = []
            all_futures = [s for s in markets if s.endswith("/USDT") and markets[s].get('future', False)]
            log_action(f"Found {len(all_futures)} futures pairs to scan.")

            if not all_futures:
                log_action("No futures pairs found. Check API permissions or market data.")
                return pairs

            try:
                tickers = await self.exchange.fetch_tickers(all_futures)
            except Exception as e:
                log_action(f"Error fetching tickers: {e}")
                return pairs

            for symbol in all_futures:
                ticker = tickers.get(symbol)
                if not ticker:
                    log_action(f"No ticker data for {symbol}, skipping.")
                    continue
                try:
                    volume = ticker.get('quoteVolume', 0)
                    price = ticker.get('last', 0)
                    if volume and price and price > 0:
                        df = await self.get_candles(symbol)
                        if len(df) < 150:
                            log_action(f"Not enough data for {symbol} (candles: {len(df)}), skipping.")
                            continue
                        crossover = self.confirm_smma_wma_crossover(df)
                        in_fib_zone = self.fib_zone_check(df)
                        avg_volume = df['volume'].iloc[-50:].mean() if len(df) >= 50 else volume
                        score = self.ai_score(price, volume, avg_volume, crossover, in_fib_zone)
                        log_candidate(symbol, price, score)
                        log_action(
                            f"Scanned {symbol} | Price: {price:.4f} | Volume: {volume:.2f} | Score: {score:.2f} | Crossover: {crossover} | Fib Zone: {in_fib_zone}")
                        if score > 0.6:
                            pairs.append((symbol, price, volume, score))
                            log_action(f"Candidate selected: {symbol} | Price: {price:.4f} | Score: {score:.2f}")
                    else:
                        log_action(f"Invalid ticker data for {symbol} | Price: {price} | Volume: {volume}")
                except Exception as e:
                    log_action(f"Error scanning {symbol}: {e}")
            pairs.sort(key=lambda x: x[3], reverse=True)
            log_action(f"Scanning complete. Selected {len(pairs)} candidates.")
            return pairs[:limit]

    def export_candidates_to_json(self):
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp, symbol, price, score FROM candidates ORDER BY id DESC LIMIT 10")
            candidates = [{"time": t, "symbol": s, "price": p, "score": sc} for t, s, p, sc in cursor.fetchall()]
            with open(DB_PATH.parent / "candidates.json", "w") as f:
                json.dump(candidates, f, indent=2)

    async def _monitor_trade(self, symbol, entry_price):
        log_action(f"Monitoring trade for {symbol} at entry {entry_price:.4f}")
        tp = entry_price * (1 + TRAILING_TP_OFFSET)
        sl = entry_price * (1 - STOP_LOSS_PERCENT)
        highest_price = entry_price
        end_time = datetime.now() + timedelta(seconds=TRADE_DURATION_LIMIT)
        while self.running and datetime.now() < end_time:
            try:
                ticker = await self.exchange.fetch_ticker(symbol)
                price = ticker['last']
                if price > highest_price:
                    highest_price = price
                    tp = highest_price * (1 - TRAILING_TP_STEP)
                if price >= tp:
                    log_action(f"TP hit for {symbol} at {price:.4f}")
                    current_balance = float(get_config("balance", "0"))
                    current_score = int(get_config("score", "0"))
                    profit = (price - entry_price) * self.leverage
                    set_config("balance", str(current_balance + profit))
                    set_config("score", str(current_score + 1))
                    log_trade(symbol, price, "TP")
                    await self._execute_sell_order(symbol, 'ALL')
                    return "TP"
                if price <= sl:
                    log_action(f"SL hit for {symbol} at {price:.4f}")
                    current_balance = float(get_config("balance", "0"))
                    current_score = int(get_config("score", "0"))
                    loss = (entry_price - price) * self.leverage
                    set_config("balance", str(current_balance - loss))
                    set_config("score", str(current_score - 1))
                    log_trade(symbol, price, "SL")
                    await self._execute_sell_order(symbol, 'ALL')
                    return "SL"
                await asyncio.sleep(2)
            except Exception as e:
                log_action(f"Error monitoring trade for {symbol}: {e}")
                await asyncio.sleep(5)
        if self.running:
            log_action(f"Trade for {symbol} timed out.")
            current_balance = float(get_config("balance", "0"))
            current_score = int(get_config("score", "0"))
            try:
                ticker = await self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                if current_price > entry_price:
                    profit = (current_price - entry_price) * self.leverage
                    set_config("balance", str(current_balance + profit))
                    set_config("score", str(current_score + 0.5))
                    log_trade(symbol, current_price, "TIMEOUT_PROFIT")
                else:
                    loss = (entry_price - current_price) * self.leverage
                    set_config("balance", str(current_balance - loss))
                    set_config("score", str(current_score - 0.5))
                    log_trade(symbol, current_price, "TIMEOUT_LOSS")
                await self._execute_sell_order(symbol, 'ALL')
                return "TIMEOUT"
            except Exception as e:
                log_action(f"Error closing timed out trade for {symbol}: {e}")
                return "ERROR_TIMEOUT"

    async def _execute_buy_order(self, symbol, quantity):
        try:
            order = await self.exchange.create_market_buy_order(symbol, quantity)
            log_action(f"Executed BUY order for {symbol}: {order}")
            return order
        except Exception as e:
            log_action(f"Error executing buy order for {symbol}: {e}")
            return None

    async def _execute_sell_order(self, symbol, quantity):
        try:
            order = await self.exchange.create_market_sell_order(symbol, quantity)
            log_action(f"Executed SELL order for {symbol}: {order}")
            return order
        except Exception as e:
            log_action(f"Error executing sell order for {symbol}: {e}")
            return None

    # U ChovusSmartBot_v9.py, dodaj _main_bot_loop posle _open_long
    async def _open_long(self, symbol, score):
        try:
            market = self.exchange.market(symbol)
            ticker = await self.exchange.fetch_ticker(symbol)
            price = ticker['ask']
            balance = await self.exchange.fetch_balance({"type": "future"})
            usdt_balance = balance['total']['USDT'] * 0.99
            alloc = self.smart_allocation(score)
            min_qty = market['limits']['amount']['min']
            max_qty = market['limits']['amount']['max']
            quantity = (usdt_balance * alloc * self.leverage) / price
            quantity = self.exchange.amount_to_precision(symbol, quantity)
            if quantity < min_qty:
                log_action(f"Calculated quantity {quantity} is less than min_qty {min_qty}. Setting to min_qty.")
                quantity = min_qty
            if quantity > max_qty:
                log_action(f"Calculated quantity {quantity} is more than max_qty {max_qty}. Setting to max_qty.")
                quantity = max_qty
            order = await self._execute_buy_order(symbol, float(quantity))
            return order, price
        except Exception as e:
            log_action(f"Error opening long position for {symbol}: {e}")
            return None, None

    async def _main_bot_loop(self):
        log_action("[BOT] Starting main bot loop...")
        while self.running:
            try:
                log_action("Initiating pair scan...")
                targets = await self._scan_pairs()
                log_action(f"Found {len(targets)} high-score targets")
                if targets:
                    symbol, price, volume, score = targets[0]
                    log_action(f"[BOT] Opening position on {symbol} with score {score:.2f}")
                    order, entry_price = await self._open_long(symbol, score)
                    if order:
                        log_action(f"Position opened for {symbol} at {entry_price}")
                        trade_outcome = await self._monitor_trade(symbol, entry_price)
                        log_action(f"Trade for {symbol} finished with outcome: {trade_outcome}")
                    else:
                        log_action(f"Could not open position for {symbol}.")
                else:
                    log_action("No high-score targets found in this scan.")
                await self.learn_from_history()
            except Exception as ex:
                log_action(f"Main bot loop error: {str(ex)}")
            await asyncio.sleep(15)

    def _send_telegram_message(self, message):
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if not token or not chat_id:
            log_action("Missing Telegram token or chat_id in .env")
            return {"status": "❌ Missing token or chat_id in .env"}
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        try:
            r = requests.post(url, data=data)
            log_action(f"Telegram status: {r.status_code}, Response: {r.text}")
            return {"status": "✅ Sent!" if r.status_code == 200 else f"❌ Error: {r.text}"}
        except Exception as e:
            log_action(f"Telegram send error: {e}")
            return {"status": f"❌ Exception: {e}"}

    def _send_report_loop(self):
        schedule.every().day.at(get_config("report_time", "09:00")).do(self._send_daily_report)
        while self.running:
            schedule.run_pending()
            time.sleep(1)

    def _send_daily_report(self):
        msg = f"📊 ChovusBot Report:\nWallet = {float(get_config('balance', '0')):.2f} USDT, Score = {int(get_config('score', '0'))}"
        self._send_telegram_message(msg)
        log_action(f"Daily report sent at {datetime.now().strftime('%H:%M')}")