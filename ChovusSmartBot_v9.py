# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file

import ccxt
import time
import math
import json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import threading
import schedule
import requests
from pandas import DataFrame
from typing import Optional, Union

# === CONFIG ===
API_KEY = 'os.getenv('API_KEY')'
API_SECRET = 'os.getenv('API_SECRET')'
TELEGRAM_BOT_TOKEN = 'os.getenv('TELEGRAM_BOT_TOKEN')'
TELEGRAM_CHAT_ID = 'os.getenv('TELEGRAM_CHAT_ID')'

SYMBOLS = []
ROUND_LEVELS = [0.01, 0.1, 0.5, 1, 5, 10, 50, 100, 500, 1000]
VOLUME_SPIKE_THRESHOLD = 1.5
TRADE_DURATION_LIMIT = 20
STOP_LOSS_PERCENT = 0.01
TRAILING_TP_STEP = 0.005
TRAILING_TP_OFFSET = 0.02

DAILY_LOG = []
LOG_FILE = 'user_data/daily_trade_log.json'

exchange = ccxt.binance({
    'apiKey': 'os.getenv('API_KEY')',
    'secret': 'os.getenv('API_SECRET')',
    'options': {
        'defaultType': 'future'
    },
    'enableRateLimit': True
})


def smart_allocation(score):
    if score > 0.9:
        return 0.5
    elif score > 0.8:
        return 0.3
    elif score > 0.7:
        return 0.2
    else:
        return 0.1

def save_to_log(entry):
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        print(f"Log save error: {e}")

def learn_from_history():
    try:
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()
        data = [json.loads(line) for line in lines]
        df = pd.DataFrame(data)
        if df.empty:
            print("[LEARN] No data to learn from.")
            return
        summary = df.groupby("symbol")["type"].value_counts().unstack().fillna(0)
        print("[LEARN] Performance summary:")
        print(summary)
        print("[LEARN] Win rate per symbol:")
        winrate = summary.get("TP", 0) / (summary.get("TP", 0) + summary.get("SL", 0))
        print(winrate.sort_values(ascending=False))
    except Exception as e:
        print(f"[LEARN] Error analyzing history: {e}")

def scan_pairs():
    markets = exchange.load_markets()
    pairs = []
    for symbol, info in markets.items():
        if '/USDT' in symbol and info.get('future', False):
            try:
                ticker = exchange.fetch_ticker(symbol)
                volume = ticker.get('quoteVolume', 0)
                price = ticker.get('last', 0)
                if volume and price and price > 0:
                    df = get_candles(symbol)
                    crossover = confirm_smma_wma_crossover(df)
                    in_fib_zone = fib_zone_check(df)
                    score = ai_score(price, volume, volume, crossover, in_fib_zone)
                    if score > 0.6:
                        pairs.append((symbol, price, volume, score))
            except Exception as e:
                continue
    pairs.sort(key=lambda x: x[3], reverse=True)
    return pairs[:3]

def get_candles(symbol, timeframe='15m', limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df

def calc_smma(series, length):
    smma = [series.iloc[0]]
    for i in range(1, len(series)):
        smma.append((smma[-1] * (length - 1) + series.iloc[i]) / length)
    return pd.Series(smma, index=series.index)

def calc_wma(series, length):
    weights = range(1, length + 1)
    return series.rolling(length).apply(lambda prices: sum(weights[i] * prices[i] for i in range(length)) / sum(weights), raw=True)

def confirm_smma_wma_crossover(df):
    smma = calc_smma(df['close'], 5)
    wma = calc_wma(df['close'], 144)
    return smma.iloc[-2] < wma.iloc[-2] and smma.iloc[-1] > wma.iloc[-1]

def fib_zone_check(df):
    high = df['high'].rolling(50).max()
    low = df['low'].rolling(50).min()
    fib_range = high - low
    fib_382 = high - fib_range * 0.382
    fib_618 = high - fib_range * 0.618
    latest_price = df['close'].iloc[-1]
    return fib_618.iloc[-1] <= latest_price <= fib_382.iloc[-1]

def is_near_round(price):
    for level in ROUND_LEVELS:
        if abs(price % level - level) < 0.01 * level or price % level < 0.01 * level:
            return True
    return False

def ai_score(price, volume, avg_volume, crossover, in_fib_zone):
    score = 0
    if is_near_round(price):
        score += 1
    if volume > avg_volume * VOLUME_SPIKE_THRESHOLD:
        score += 1
    if crossover:
        score += 1.5
    if in_fib_zone:
        score += 0.5
    return min(score / 4.0, 1.0)

def monitor_trade(symbol, entry_price):
    print(f"[TRAILING TP/SL] Monitoring {symbol}")
    tp = entry_price * (1 + TRAILING_TP_OFFSET)
    sl = entry_price * (1 - STOP_LOSS_PERCENT)
    highest_price = entry_price

    while True:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']

        if price > highest_price:
            highest_price = price
            tp = highest_price * (1 - TRAILING_TP_STEP)

        if price >= tp:
            print(f"[TP] Exiting {symbol} at {price:.4f}")
            trade = {"symbol": symbol, "type": "TP", "price": price, "profit": (price / entry_price) - 1, "timestamp": str(datetime.utcnow())}
            DAILY_LOG.append(trade)
            save_to_log(trade)
            exchange.create_market_sell_order(symbol, 'ALL')
            break

        if price <= sl:
            print(f"[SL] Exiting {symbol} at {price:.4f}")
            trade = {"symbol": symbol, "type": "SL", "price": price, "loss": 1 - (price / entry_price), "timestamp": str(datetime.utcnow())}
            DAILY_LOG.append(trade)
            save_to_log(trade)
            exchange.create_market_sell_order(symbol, 'ALL')
            break

        time.sleep(2)

def open_long(symbol, score):
    print(f"[LONG] Opening position on {symbol} with score {score:.2f}")
    market = exchange.market(symbol)
    ticker = exchange.fetch_ticker(symbol)
    price = ticker['ask']
    balance = exchange.fetch_balance({"type": "future"})
    usdt_balance = balance['total']['USDT'] * 0.99
    alloc = smart_allocation(score)
    quantity = round((usdt_balance * alloc * 10) / price, int(market['precision']['amount']))
    order = exchange.create_market_buy_order(symbol, quantity)
    return order, price

print("[RUNNING] Scanning...")
while True:
    try:
        learn_from_history()
        targets = scan_pairs()
        print(f"Found {len(targets)} high-score targets")
        for (symbol, price, volume, score) in targets:
            order, entry = open_long(symbol, score)
            start_time = time.time()
            while time.time() - start_time < TRADE_DURATION_LIMIT:
                monitor_trade(symbol, entry)
                break
    except Exception as ex:
        print(f"Main loop error: {str(ex)}")
    time.sleep(10)
