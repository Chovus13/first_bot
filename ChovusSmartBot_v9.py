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
import requests
from pandas import DataFrame
from typing import Optional, Union
from dotenv import load_dotenv
import asyncio
import sqlite3
from pathlib import Path

load_dotenv()

# === CONFIG & DB SETUP ===
api_key = os.getenv('API_KEY')
api_secret = os.getenv('API_SECRET')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# DB setup (prebaciti ovo ovde, ili proslediti conn u bota)
# DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parents[1] / "user_data" / "chovusbot.db"))
# Prilagođeno za strukturu gde je ChovusSmartBot_v9.py u korenu, a user_data u korenu
# Ako je ChovusSmartBot_v9.py u 'first_bot/', onda user_data/chovusbot.db
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent / "user_data" / "chovusbot.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)  # Kreiraj user_data folder ako ne postoji
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
cursor.execute(
    '''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, price REAL, timestamp TEXT, outcome TEXT)''')  # Dodato 'outcome'
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


def log_trade(symbol, price, outcome):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO trades (symbol, price, timestamp, outcome) VALUES (?, ?, ?, ?)",
                   (symbol, price, now, outcome))
    conn.commit()


def log_score(score):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO score_log (timestamp, score) VALUES (?, ?)", (now, score))
    conn.commit()


# Constants
SYMBOLS = []  # Ovo bi trebalo da se popunjava sa top parovima iz skeniranja
ROUND_LEVELS = [0.01, 0.1, 0.5, 1, 5, 10, 50, 100, 500, 1000]
VOLUME_SPIKE_THRESHOLD = 1.5
TRADE_DURATION_LIMIT = 60 * 10  # 10 minuta, za sada. Originalno 20 sekundi je prekratko
STOP_LOSS_PERCENT = 0.01
TRAILING_TP_STEP = 0.005
TRAILING_TP_OFFSET = 0.02


class ChovusSmartBot:
    def __init__(self):
        self.running = False
        self.current_strategy = "Default"
        self._bot_main_task = None  # Za glavnu asinhronu petlju bota
        self._telegram_report_thread = None  # Za Telegram report thread
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future'
            }
        })
        # Inicijalizuj balans i score ako ne postoje u bazi
        if get_config("balance") is None:
            set_config("balance", "1000.0")  # Početni balans
        if get_config("score") is None:
            set_config("score", "0")  # Početni score
        if get_config("report_time") is None:
            set_config("report_time", "09:00")  # Vreme za dnevni report

    async def start_bot(self):
        if self.running:
            print("Bot is already running. [ChovusSmartBot]")
            return

        print("Bot starting... [ChovusSmartBot]")
        self.running = True
        self._bot_main_task = asyncio.create_task(self._main_bot_loop())  # Pokreni glavnu petlju bota

        # Pokreni Telegram report u zasebnom threadu
        if self._telegram_report_thread is None or not self._telegram_report_thread.is_alive():
            self._telegram_report_thread = threading.Thread(target=self._send_report_loop, daemon=True)
            self._telegram_report_thread.start()

        print("Bot started. [ChovusSmartBot]")

    def stop_bot(self):
        if not self.running:
            print("Bot is not running. [ChovusSmartBot]")
            return

        print("Bot stopping... [ChovusSmartBot]")
        self.running = False
        # Ne čekaj ovde, main_bot_loop će reagovati na self.running = False
        # Ako je potrebno da se task eksplicitno prekine, to je složenije
        # Za sada, samo postavi self.running na False i čekaj da se petlja završi

    def get_bot_status(self):
        return "Running" if self.running else "Stopped"

    def set_bot_strategy(self, strategy_name: str):
        self.current_strategy = strategy_name
        print(f"Strategy set to: {strategy_name}")

    def smart_allocation(self, score):
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
            # Implementiraj logiku za analizu istorije trejdova iz baze
            # Treba ti outcome (TP/SL) da bi smisleno učio
            cursor.execute("SELECT symbol, price, outcome FROM trades")
            data = cursor.fetchall()
            df = pd.DataFrame(data, columns=['symbol', 'price', 'outcome'])
            if df.empty:
                print("[LEARN] No data to learn from.")
                return

            # Primer analize: broj TP/SL za svaki simbol
            summary = df.groupby("symbol")["outcome"].value_counts().unstack().fillna(0)
            print("[LEARN] Performance summary:")
            print(summary)

            # Prilagodi logiku učenja na osnovu ishoda trejdova
            # Npr. povećanje težine za simbole sa više TP, smanjenje za SL

        except Exception as e:
            print(f"[LEARN] Error analyzing history: {e}")

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
        if len(df) < 144: return False  # Nema dovoljno podataka
        smma = self.calc_smma(df['close'], 5)
        wma = self.calc_wma(df['close'], 144)
        return smma.iloc[-2] < wma.iloc[-2] and smma.iloc[-1] > wma.iloc[-1]

    def fib_zone_check(self, df):
        if len(df) < 50: return False  # Nema dovoljno podataka
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
        return min(score / 4.0, 1.0)  # Normalizuj score od 0 do 1

    async def _scan_pairs(self, limit=3):
        markets = await self.exchange.load_markets()  # Asinhrono
        pairs = []
        all_futures = [s for s in markets if s.endswith("/USDT") and markets[s].get('future', False)]

        # Optimizovano: fetchuj sve tickere jednom
        tickers = await self.exchange.fetch_tickers(all_futures)  # Asinhrono

        for symbol in all_futures:
            info = markets[symbol]
            ticker = tickers.get(symbol)
            if not ticker: continue

            try:
                volume = ticker.get('quoteVolume', 0)
                price = ticker.get('last', 0)
                if volume and price and price > 0:
                    df = await self.get_candles(symbol)  # Asinhrono
                    if len(df) < 150:  # Proveri da li ima dovoljno podataka za indikatore
                        print(f"Not enough data for {symbol}, skipping indicators.")
                        continue

                    crossover = self.confirm_smma_wma_crossover(df)
                    in_fib_zone = self.fib_zone_check(df)

                    # Realističniji avg_volume (poslednji 50 volumena, na primer)
                    avg_volume = df['volume'].iloc[-50:].mean() if len(df) >= 50 else volume

                    score = self.ai_score(price, volume, avg_volume, crossover, in_fib_zone)
                    if score > 0.6:
                        pairs.append((symbol, price, volume, score))
            except Exception as e:
                print(f"Error scanning {symbol}: {e}")
                continue
        pairs.sort(key=lambda x: x[3], reverse=True)
        return pairs[:limit]

    async def _monitor_trade(self, symbol, entry_price):
        print(f"[TRAILING TP/SL] Monitoring {symbol}")
        tp = entry_price * (1 + TRAILING_TP_OFFSET)
        sl = entry_price * (1 - STOP_LOSS_PERCENT)
        highest_price = entry_price

        # Kreiraj timeout za trejd ako trade_duration_limit istekne
        end_time = datetime.now() + timedelta(seconds=TRADE_DURATION_LIMIT)

        while self.running and datetime.now() < end_time:  # Dodao self.running uslov i timeout
            try:
                ticker = await self.exchange.fetch_ticker(symbol)  # Asinhrono
                price = ticker['last']

                if price > highest_price:
                    highest_price = price
                    tp = highest_price * (1 - TRAILING_TP_STEP)

                if price >= tp:
                    print(f"[TP] Exiting {symbol} at {price:.4f}")
                    current_balance = float(get_config("balance", "0"))
                    current_score = int(get_config("score", "0"))
                    profit = (price - entry_price) * 10  # Pretpostavimo 10x leverage za primer
                    set_config("balance", str(current_balance + profit))
                    set_config("score", str(current_score + 1))
                    log_trade(symbol, price, "TP")
                    await self._execute_sell_order(symbol, 'ALL')
                    return "TP"  # Vraća ishod trejda

                if price <= sl:
                    print(f"[SL] Exiting {symbol} at {price:.4f}")
                    current_balance = float(get_config("balance", "0"))
                    current_score = int(get_config("score", "0"))
                    loss = (entry_price - price) * 10
                    set_config("balance", str(current_balance - loss))
                    set_config("score", str(current_score - 1))
                    log_trade(symbol, price, "SL")
                    await self._execute_sell_order(symbol, 'ALL')
                    return "SL"  # Vraća ishod trejda

                await asyncio.sleep(2)  # Koristi asyncio.sleep
            except Exception as e:
                print(f"Error monitoring trade for {symbol}: {e}")
                await asyncio.sleep(5)  # Pričekaj duže ako ima greške

        # Ako je petlja završila zbog isteka vremena
        if self.running:
            print(f"Trade for {symbol} timed out. Closing position.")
            current_balance = float(get_config("balance", "0"))
            current_score = int(get_config("score", "0"))
            # Zatvori poziciju na trenutnoj ceni i izračunaj ishod
            try:
                ticker = await self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                if current_price > entry_price:
                    profit = (current_price - entry_price) * 10
                    set_config("balance", str(current_balance + profit))
                    set_config("score", str(current_score + 0.5))  # Manji score za timeout sa profitom
                    log_trade(symbol, current_price, "TIMEOUT_PROFIT")
                else:
                    loss = (entry_price - current_price) * 10
                    set_config("balance", str(current_balance - loss))
                    set_config("score", str(current_score - 0.5))  # Manji score za timeout sa gubitkom
                    log_trade(symbol, current_price, "TIMEOUT_LOSS")
                await self._execute_sell_order(symbol, 'ALL')
                return "TIMEOUT"
            except Exception as e:
                print(f"Error closing timed out trade for {symbol}: {e}")
                return "ERROR_TIMEOUT"

    async def _execute_buy_order(self, symbol, quantity):
        try:
            order = await self.exchange.create_market_buy_order(symbol, quantity)  # Asinhrono
            print(f"Executed BUY order for {symbol}: {order}")
            return order
        except Exception as e:
            print(f"Error executing buy order for {symbol}: {e}")
            return None

    async def _execute_sell_order(self, symbol, quantity):
        try:
            order = await self.exchange.create_market_sell_order(symbol, quantity)  # Asinhrono
            print(f"Executed SELL order for {symbol}: {order}")
            return order
        except Exception as e:
            print(f"Error executing sell order for {symbol}: {e}")
            return None

    async def _main_bot_loop(self):
        print("[BOT] Starting main bot loop...")
        while self.running:
            try:
                await self.learn_from_history()  # Asinhrono
                targets = await self._scan_pairs()  # Asinhrono
                print(f"Found {len(targets)} high-score targets")

                if targets:
                    # Logika za odabir najboljeg trejda ako ima više targeta
                    # Za sada, uzimamo prvi
                    symbol, price, volume, score = targets[0]
                    print(f"[BOT] Opening position on {symbol} with score {score:.2f}")

                    order, entry_price = await self._open_long(symbol, score)  # Asinhrono
                    if order:
                        print(f"Position opened for {symbol} at {entry_price}")
                        # Pokreni monitor_trade kao zaseban task
                        trade_outcome = await self._monitor_trade(symbol, entry_price)  # Cekaj ishod trejda
                        print(f"Trade for {symbol} finished with outcome: {trade_outcome}")
                    else:
                        print(f"Could not open position for {symbol}.")
                else:
                    print("No high-score targets found in this scan.")

            except Exception as ex:
                print(f"Main bot loop error: {str(ex)}")

            await asyncio.sleep(15)  # Čekaj 15 sekundi pre sledećeg ciklusa skeniranja/trejdovanja

    async def _open_long(self, symbol, score):
        try:
            market = self.exchange.market(symbol)
            ticker = await self.exchange.fetch_ticker(symbol)  # Asinhrono
            price = ticker['ask']
            balance = await self.exchange.fetch_balance({"type": "future"})  # Asinhrono
            usdt_balance = balance['total']['USDT'] * 0.99  # Ostavi malo prostora

            alloc = self.smart_allocation(score)
            # Uzeti u obzir min/max quantity
            min_qty = market['limits']['amount']['min']
            max_qty = market['limits']['amount']['max']
            price_precision = market['precision']['price']
            amount_precision = market['precision']['amount']

            quantity = (usdt_balance * alloc * 10) / price  # Pretpostavimo 10x leverage
            quantity = self.exchange.amount_to_precision(symbol, quantity)  # Zaokruzi na preciznost menjačnice

            # Proveri min/max quantity
            if quantity < min_qty:
                print(f"Calculated quantity {quantity} is less than min_qty {min_qty}. Setting to min_qty.")
                quantity = min_qty
            if quantity > max_qty:
                print(f"Calculated quantity {quantity} is more than max_qty {max_qty}. Setting to max_qty.")
                quantity = max_qty

            order = await self._execute_buy_order(symbol, float(quantity))
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
        # Koristi schedule biblioteku za zakazivanje
        schedule.every().day.at(get_config("report_time", "09:00")).do(self._send_daily_report)
        while self.running:
            schedule.run_pending()
            time.sleep(1)  # Proveri svakih 1 sekundu

    def _send_daily_report(self):
        msg = f"📊 ChovusBot Report:\nWallet = {float(get_config('balance', '0')):.2f} USDT, Score = {int(get_config('score', '0'))}"
        self._send_telegram_message(msg)
        print(f"Daily report sent at {datetime.now().strftime('%H:%M')}")