# U ChovusSmartBot_v9.py, ažuriraj importe
import os
import logging
import time
import sqlite3
import json
import asyncio
import threading
import pandas as pd
import requests
import ccxt.async_support as ccxt
from pathlib import Path
from dotenv import load_dotenv

# Definicija DB_PATH i ostalih globalnih promenljivih
DB_PATH = Path(os.getenv("DB_PATH", "/app/user_data/chovusbot.db"))
VOLUME_SPIKE_THRESHOLD = 1.5  # Pretpostavka za ai_score

def get_config(key, default=None):
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
            result = cursor.fetchone()
            return result[0] if result else default
    except Exception as e:
        print(f"Error fetching config {key}: {e}")
        return default

def set_config(key, value):
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
    except Exception as e:
        print(f"Error setting config {key}: {e}")

def log_action(message):
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO bot_logs (timestamp, message) VALUES (?, ?)", (now, message))
            conn.commit()
            print(f"LOG: {now} - {message}")
    except Exception as e:
        print(f"Error logging action: {str(e)}")

class ChovusSmartBot:
    def __init__(self):
        self.running = False
        self.leverage = int(get_config("leverage", 10))
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('API_KEY'),
            'secret': os.getenv('API_SECRET'),
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        self._bot_task = None
        self._telegram_report_thread = None
        self.current_strategy = "Default"
        init_db()  # Inicijalizuj bazu pri kreiranju instance

    def ai_score(self, price, volume, avg_volume, crossover, in_fib_zone):
        score = 0
        if self.is_near_round(price):
            score += 1
            log_action(f"ai_score: {price} is near round number, adding 1 to score")
        if volume > avg_volume * VOLUME_SPIKE_THRESHOLD:
            score += 1
            log_action(f"ai_score: Volume spike detected ({volume} > {avg_volume * VOLUME_SPIKE_THRESHOLD}), adding 1 to score")
        if crossover:
            score += 1.2
            log_action("ai_score: Crossover detected, adding 1.2 to score")
        if in_fib_zone:
            score += 0.8
            log_action("ai_score: In Fib zone, adding 0.8 to score")
        final_score = min(score / 4.0, 1.0)
        log_action(f"ai_score: Final score = {final_score} (raw score = {score})")
        return final_score

    def is_near_round(self, price):
        round_numbers = [100, 500, 1000, 5000, 10000]
        for r in round_numbers:
            if abs(price - round(price / r) * r) < r * 0.01:
                return True
        return False

    async def get_candles(self, symbol, timeframe='1h', limit=150):
        # Pretpostavljam da ova metoda dohvata sveće
        try:
            candles = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            log_action(f"Error fetching candles for {symbol}: {str(e)}")
            return pd.DataFrame()

    def confirm_smma_wma_crossover(self, df):
        # Pretpostavljam da ova metoda proverava crossover SMMA i WMA
        try:
            smma = df['close'].ewm(span=5, adjust=False).mean()
            wma = df['close'].rolling(window=144).mean()
            return smma.iloc[-1] > wma.iloc[-1] and smma.iloc[-2] <= wma.iloc[-2]
        except Exception as e:
            log_action(f"Error in confirm_smma_wma_crossover: {str(e)}")
            return False

    def fib_zone_check(self, df):
        # Pretpostavljam da ova metoda proverava Fib zone
        try:
            high = df['high'].rolling(50).max().iloc[-1]
            low = df['low'].rolling(50).min().iloc[-1]
            fib_range = high - low
            support = high - fib_range * 0.618
            resistance = high - fib_range * 0.382
            current_price = df['close'].iloc[-1]
            return support <= current_price <= resistance
        except Exception as e:
            log_action(f"Error in fib_zone_check: {str(e)}")
            return False

    def log_candidate(self, symbol, price, score):
        try:
            with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
                cursor = conn.cursor()
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("INSERT INTO candidates (timestamp, symbol, price, score) VALUES (?, ?, ?, ?)",
                              (now, symbol, price, score))
                conn.commit()
                log_action(f"Logged candidate: {symbol} | Price: {price:.4f} | Score: {score:.2f}")
            self.export_candidates_to_json()
        except Exception as e:
            log_action(f"Error in log_candidate: {str(e)}")

    def export_candidates_to_json(self):
        try:
            log_action("Exporting candidates to JSON...")
            with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT timestamp, symbol, price, score FROM candidates ORDER BY id DESC LIMIT 10")
                candidates = [{"time": t, "symbol": s, "price": p, "score": sc} for t, s, p, sc in cursor.fetchall()]
                json_path = Path(DB_PATH).parent / "candidates.json"
                log_action(f"Writing candidates to {json_path}")
                with open(json_path, "w") as f:
                    json.dump(candidates, f, indent=2)
                log_action("Candidates exported to JSON successfully.")
        except Exception as e:
            log_action(f"Error exporting candidates to JSON: {e}")

    async def _scan_pairs(self, limit=5):
        log_action("Starting pair scanning...")
        try:
            log_action("Loading markets...")
            markets = await self.exchange.load_markets()
            available_pairs = get_config("available_pairs", "BTC/USDT,ETH/USDT,SOL/USDT")
            all_futures = available_pairs.split(",") if available_pairs else ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
            log_action(f"Scanning {len(all_futures)} predefined pairs: {all_futures}...")

            if not all_futures:
                log_action("No pairs defined in config. Add pairs to scan.")
                return []

            try:
                log_action("Fetching tickers...")
                tickers = await self.exchange.fetch_tickers(all_futures)
                log_action(f"Fetched tickers for {len(tickers)} pairs: {list(tickers.keys())[:5]}...")
            except Exception as e:
                log_action(f"Error fetching tickers: {str(e)}")
                return []

            pairs = []
            for symbol in all_futures:
                ticker = tickers.get(symbol)
                if not ticker:
                    log_action(f"No ticker data for {symbol}, skipping.")
                    continue
                try:
                    volume = ticker.get('quoteVolume', 0)
                    price = ticker.get('last', 0)
                    if volume and price and price > 0:
                        log_action(f"Fetching candles for {symbol}...")
                        df = await self.get_candles(symbol, timeframe='1h', limit=150)
                        if len(df) < 150:
                            log_action(f"Not enough data for {symbol} (candles: {len(df)}), skipping.")
                            continue
                        log_action(f"Calculating indicators for {symbol}...")
                        crossover = self.confirm_smma_wma_crossover(df)
                        in_fib_zone = self.fib_zone_check(df)
                        avg_volume = df['volume'].iloc[-50:].mean() if len(df) >= 50 else volume
                        score = self.ai_score(price, volume, avg_volume, crossover, in_fib_zone)
                        log_action(f"Scanned {symbol} | Price: {price:.4f} | Volume: {volume:.2f} | Score: {score:.2f} | Crossover: {crossover} | Fib Zone: {in_fib_zone}")
                        self.log_candidate(symbol, price, score)
                        if score > 0.1:
                            pairs.append((symbol, price, volume, score))
                            log_action(f"Candidate selected: {symbol} | Price: {price:.4f} | Score: {score:.2f}")
                    else:
                        log_action(f"Invalid ticker data for {symbol} | Price: {price} | Volume: {volume}")
                except Exception as e:
                    log_action(f"Error scanning {symbol}: {str(e)}")
            pairs.sort(key=lambda x: x[3], reverse=True)
            log_action(f"Scanning complete. Selected {len(pairs)} candidates.")
            return pairs[:limit]
        except Exception as e:
            log_action(f"Error in pair scanning: {str(e)}")
            return []

    async def _main_bot_loop(self):
        log_action("[BOT] Starting main bot loop...")
        while self.running:
            log_action("Bot loop iteration running...")
            try:
                log_action("Initiating pair scan...")
                targets = await self._scan_pairs()
                log_action(f"Found {len(targets)} high-score targets: {[t[0] for t in targets]}")
                if targets:
                    symbol, price, volume, score = targets[0]
                    log_action(f"[BOT] Opening position on {symbol} with score {score:.2f}")
                    order, entry_price = await self._open_long(symbol, score)
                    if order:
                        log_action(f"Position opened for {symbol} at {entry_price}")
                        trade_outcome = await self._monitor_trade(symbol, entry_price)
                        log_action(f"Trade for {symbol} finished with outcome: {trade_outcome}")
                        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
                            cursor = conn.cursor()
                            now = time.strftime("%Y-%m-%d %H:%M:%S")
                            cursor.execute("INSERT INTO trades (symbol, price, timestamp, outcome) VALUES (?, ?, ?, ?)",
                                          (symbol, entry_price, now, trade_outcome))
                            conn.commit()
                    else:
                        log_action(f"Could not open position for {symbol}.")
                else:
                    log_action("No high-score targets found in this scan.")
                await self.learn_from_history()
            except Exception as ex:
                log_action(f"Main bot loop error: {str(ex)}")
            await asyncio.sleep(15)

    async def start_bot(self):
        if self.running:
            log_action("Bot is already running.")
            return
        log_action("Bot starting...")
        self.running = True
        try:
            await self.set_leverage(self.leverage)
        except Exception as e:
            log_action(f"Error setting leverage in start_bot: {str(e)}")
            self.running = False
            raise
        self._bot_task = asyncio.create_task(self._main_bot_loop())
        if self._telegram_report_thread is None or not self._telegram_report_thread.is_alive():
            self._telegram_report_thread = threading.Thread(target=self._send_report_loop, daemon=True)
            self._telegram_report_thread.start()
        log_action("Bot started.")

    def stop_bot(self):
        self.running = False
        if self._bot_task:
            self._bot_task.cancel()
        log_action("Bot stopped.")

    async def set_leverage(self, leverage):
        self.leverage = leverage
        log_action(f"Leverage set to: {leverage}x")
        try:
            await self.exchange.set_leverage(leverage, symbol=None)
        except Exception as e:
            log_action(f"Error setting leverage: {e}")

    def set_manual_amount(self, amount):
        self.manual_amount = amount
        log_action(f"Manual amount set to: {amount} USDT")

    def set_bot_strategy(self, strategy_name):
        self.current_strategy = strategy_name
        log_action(f"Strategy set to: {strategy_name}")
        return strategy_name

    def get_bot_status(self):
        return "Running" if self.running else "Stopped"

    async def _open_long(self, symbol, score):
        # Pretpostavljam da ova metoda otvara long poziciju
        try:
            # Ovo je samo primer – prilagodi prema tvom kodu
            amount = self.manual_amount / price if hasattr(self, 'manual_amount') else 0.001
            order = await self.exchange.create_market_buy_order(symbol, amount)
            entry_price = order['price'] if 'price' in order else price
            return order, entry_price
        except Exception as e:
            log_action(f"Error opening long position for {symbol}: {str(e)}")
            return None, None

    async def _monitor_trade(self, symbol, entry_price):
        # Pretpostavljam da ova metoda prati trejd
        try:
            # Ovo je samo primer – prilagodi prema tvom kodu
            await asyncio.sleep(60)  # Simulacija praćenja trejda 60 sekundi
            ticker = await self.exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            if current_price > entry_price * 1.02:  # 2% profit
                return "TP"
            elif current_price < entry_price * 0.98:  # 2% loss
                return "SL"
            return "Timeout"
        except Exception as e:
            log_action(f"Error monitoring trade for {symbol}: {str(e)}")
            return "Error"

    async def learn_from_history(self):
        # Pretpostavljam da ova metoda uči iz istorije trejdova
        try:
            with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT outcome FROM trades ORDER BY id DESC LIMIT 10")
                outcomes = [row[0] for row in cursor.fetchall()]
                win_rate = outcomes.count("TP") / len(outcomes) if outcomes else 0
                log_action(f"Learning from history: Win rate = {win_rate:.2f}")
        except Exception as e:
            log_action(f"Error in learn_from_history: {str(e)}")

    def _send_report_loop(self):
        # Pretpostavljam da ova metoda šalje izveštaje na Telegram
        while self.running:
            try:
                self._send_telegram_message("Bot is running...")
                time.sleep(300)  # Šalje svakih 5 minuta
            except Exception as e:
                print(f"Error in send_report_loop: {str(e)}")

    def _send_telegram_message(self, message):
        try:
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                return {"status": "Missing Telegram token or chat_id in .env"}
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": message}
            response = requests.post(url, json=payload)
            return {"status": "✅ Sent!"} if response.status_code == 200 else {"status": "Failed",
                                                                              "error": response.text}
        except Exception as e:
            return {"status": "Failed", "error": str(e)}