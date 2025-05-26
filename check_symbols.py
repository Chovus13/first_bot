import asyncio
import ccxt.async_support as ccxt
import os
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv('API_KEY')
api_secret = os.getenv('API_SECRET')

async def check_markets():
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future'
        }
    })

    try:
        await exchange.load_markets()
        markets = exchange.markets
        perpetual_futures = [
            symbol for symbol, market in markets.items()
            if market['active'] and market['type'] == 'future'
        ]
        print("Dostupni USD-M Perpetual Futures simboli:", perpetual_futures)
    except Exception as e:
        print(f"Greška: {str(e)}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(check_markets())