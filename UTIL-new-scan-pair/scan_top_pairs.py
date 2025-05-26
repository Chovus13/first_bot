import ccxt
import os
import sqlite3
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "../backend/user_data/chovusbot.db")
exchange = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

def scan_top_pairs(limit=3):
    print(f"[{datetime.now()}] Scanning top {limit} USDT Futures pairs...")
    exchange.load_markets()
    tickers = exchange.fetch_tickers()
    markets = exchange.load_markets()

    futures = [s for s in markets if s.endswith("/USDT") and markets[s].get('contract', False)]
    print(f"Found {len(futures)} futures markets ending with /USDT")

    valid_pairs = []
    for s in futures:
        ticker = tickers.get(s)
        if ticker and 'quoteVolume' in ticker:
            valid_pairs.append((s, ticker['quoteVolume']))

    sorted_by_volume = sorted(valid_pairs, key=lambda x: x[1], reverse=True)
    top = [s[0] for s in sorted_by_volume[:limit]]
    print(f"Top pairs: {top}")
    return top

def update_db_pairs(pairs):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ("available_pairs", ",".join(pairs)))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    top_pairs = scan_top_pairs()
    if not top_pairs:
        top_pairs = ["BTC/USDT", "ETH/USDT", "OP/USDT"]
        print("⚠️ No valid pairs found. Using fallback:", top_pairs)
    update_db_pairs(top_pairs)
