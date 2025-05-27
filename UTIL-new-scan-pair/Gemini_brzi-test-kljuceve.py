import asyncio
import json
import tkinter as tk
from tkinter import ttk, scrolledtext
import websockets
import threading
import time
import aiohttp
import threading
from dotenv import load_dotenv

# --- Konfiguracija ---
SYMBOL = "ETH/BTC"  # Koristimo ETHUSDT kao primer, prilagodi ako želiš ETH/BTC futures par
LEVERAGE = 3
API_KEY = bcgSOi0aUFQdHOOOoPjPAfdAcDYqYYzNwkScFq67Uy7oljdcWnheThizKriHrKUy
API_SECRET = 7x7tIdlptIcoGAhuKk6lnq35UugstIlmKxh03JqvigL63VBl3WJBOcfYyONMFK4P
BASE_URL = "https://fapi.binance.com"
WS_URL = "wss://fstream.binance.com/ws"

# --- Globalne promenljive ---
order_book_data = {"asks": {}, "bids": {}}
log_text = None

def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    full_message = f"[{timestamp}] {message}\n"
    if log_text:
        log_text.config(state=tk.NORMAL)
        log_text.insert(tk.END, full_message)
        log_text.see(tk.END)
        log_text.config(state=tk.DISABLED)
    else:
        print(full_message, end="")

async def fetch_order_book():
    url = f"{BASE_URL}/fapi/v1/depth?symbol={SYMBOL}&limit=100"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    for ask in data['asks']:
                        price = float(ask[0])
                        volume = float(ask[1])
                        order_book_data["asks"][price] = volume
                    for bid in data['bids']:
                        price = float(bid[0])
                        volume = float(bid[1])
                        order_book_data["bids"][price] = volume
                    log(f"Order book fetched successfully.")
                else:
                    log(f"Error fetching order book: {response.status}")
    except Exception as e:
        log(f"Exception while fetching order book: {e}")

async def websocket_handler():
    backoff = 1
    max_backoff = 60
    while True:
        try:
            async with websockets.connect(f"{WS_URL}/{SYMBOL.lower()}@depth@100ms") as ws:
                backoff = 1  # Reset backoff on successful connection
                log(f"WebSocket connected to {SYMBOL} depth stream.")
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=10)
                        data = json.loads(message)
                        update_order_book(data)
                        analyze_order_book()
                    except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                        log(f"WebSocket error: {e}")
                        break
        except Exception as e:
            log(f"WebSocket connection failed: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)  # Exponential backoff

import threading

order_book_lock = threading.Lock()

def update_order_book(data):
    global order_book_data
    with order_book_lock:
        if 'asks' in data:
            for ask in data['asks']:
            price = float(ask[0])
            volume = float(ask[1])
            if volume == 0:
                if price in order_book_data["asks"]:
                    del order_book_data["asks"][price]
            else:
                order_book_data["asks"][price] = volume
    if 'bids' in data:
        for bid in data['bids']:
            price = float(bid[0])
            volume = float(bid[1])
            if volume == 0:
                if price in order_book_data["bids"]:
                    del order_book_data["bids"][price]
            else:
                order_book_data["bids"][price] = volume

def analyze_order_book():
    asks = sorted(order_book_data["asks"].items())
    bids = sorted(order_book_data["bids"].items(), reverse=True)

    if not asks or not bids:
        return

    best_ask = asks[0][0]
    best_bid = bids[0][0]

    fifth_decimal_ask = int(round(best_ask * 100000) % 10)
    fifth_decimal_bid = int(round(best_bid * 100000) % 10)

    # Strategija "pomeranja zida" (pojednostavljena)
    # Ovde implementiraj složeniju logiku analize zidova
    # Za minimalni primer, fokusiracemo se na prisustvo značajnog obima
    wall_threshold = 10  # Primer praga za "zid"

    strong_ask_wall_at_9 = any(vol > wall_threshold for price, vol in asks if int(round(price * 100000) % 10) == 9)
    strong_bid_wall_at_1 = any(vol > wall_threshold for price, vol in bids if int(round(price * 100000) % 10) == 1)

    # Logika trgovanja bazirana na petoj decimali i "rokadi"
    if fifth_decimal_ask == 7 or fifth_decimal_ask == 8:
        log(f"Potencijalni SHORT signal na {best_ask:.5f}")
        # Implementiraj slanje SHORT ordera ovde
    elif fifth_decimal_bid == 2 or fifth_decimal_bid == 1:
        log(f"Potencijalni LONG signal na {best_bid:.5f}")
        # Implementiraj slanje LONG ordera ovde

    # "Rokada" logika
    if strong_ask_wall_at_9:
        log(f"Detektovan jaki ASK zid na ...9. Potencijalna ROKADA (LONG umesto SHORT).")
        # Implementiraj logiku za prelazak na LONG strategiju
    elif strong_bid_wall_at_1:
        log(f"Detektovan jaki BID zid na ...1. Potencijalna ROKADA (SHORT umesto LONG).")
        # Implementiraj logiku za prelazak na SHORT strategiju

    # Ograničavanje ispisa logova radi preglednosti
    # log(f"Best Ask: {best_ask:.5f}, Best Bid: {best_bid:.5f}, Ask 5th: {fifth_decimal_ask}, Bid 5th: {fifth_decimal_bid}")
    # log(f"Asks (top 5): {asks[:5]}, Bids (top 5): {bids[:5]}")

# --- GUI ---
def main_gui():
    global log_text
    window = tk.Tk()
    window.title("Minimalni Binance Futures Bot (ETH/BTC)")

    log_label = ttk.Label(window, text="Log:")
    log_label.pack(pady=5)

    log_text = scrolledtext.ScrolledText(window, height=15, width=80, state=tk.DISABLED)
    log_text.pack(padx=10, pady=5)

    start_button = ttk.Button(window, text="Start Bot", command=start_bot)
    start_button.pack(pady=10)

    window.mainloop()

def start_bot():
    log("Starting bot...")
    threading.Thread(target=run_asyncio_loop, daemon=True).start()

def run_asyncio_loop():
    asyncio.run(main())

async def main():
    import aiohttp
    await fetch_order_book()
    await websocket_handler()

if __name__ == "__main__":
    main_gui()