import os
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta, time
from automation import compute_recommendation, get_tomorrows_earnings, get_todays_earnings
from alpaca_integration import (
    init_alpaca_client, place_iron_fly_order, close_iron_fly_order,
    get_portfolio_value, get_alpaca_option_chain, get_single_option_quotes
)
import yfinance as yf
from zoneinfo import ZoneInfo
import sys
import sqlite3
import queue

# Constants
PROFIT_ADJUSTMENT_FACTOR = 0.5
DB_PATH = "trades.db"
load_dotenv()
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")

# Execution tracking
trade_fill_queue = queue.Queue()
trade_monitor_threads = []


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS trades (
           "Ticker" TEXT,
           "Implied Move" TEXT,
           "Structure" TEXT,
           "Side" TEXT,
           "When" TEXT,
           "Size" INTEGER,
           "Short Symbol" TEXT,
           "Long Symbol" TEXT,
           "Open Date" TEXT,
           "Open Price" REAL,
           "Open Comm." REAL,
           "Close Date" TEXT,
           "Close Price" REAL,
           "Close Comm." REAL
        )'''
    )
    cursor.execute("PRAGMA table_info(trades)")
    cols = [row[1] for row in cursor.fetchall()]
    if "When" not in cols:
        cursor.execute('ALTER TABLE trades ADD COLUMN "When" TEXT')
    conn.commit()
    conn.close()


def select_iron_fly_strikes(ticker, expiration_date):
    chain = get_alpaca_option_chain(ticker, expiration_date)
    if not chain:
        return None
    spot = yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1]
    strikes = sorted({float(o["strike_price"]) for o in chain})
    atm = min(strikes, key=lambda x: abs(x - spot))
    wings = (atm + 5, atm - 5)

    def find_symbol(strike, type_):
        for o in chain:
            if float(o["strike_price"]) == strike and o["option_type"] == type_:
                return o["symbol"]
        return None

    return (
        find_symbol(atm, "call"), find_symbol(atm, "put"),
        find_symbol(wings[0], "call"), find_symbol(wings[1], "put")
    )


def run_trade_workflow():
    print("Running Iron Fly trade workflow...")
    client = init_alpaca_client()
    if not client:
        print("Alpaca init failed.")
        return 1
    clock = client.get_clock()
    if not getattr(clock, 'is_open', False):
        print("Market closed.")
        return 1

    # Fetch earnings & portfolio
    earnings = get_todays_earnings() + get_tomorrows_earnings()
    portfolio_value = get_portfolio_value()
    if not portfolio_value:
        print("No portfolio value.")
        return
    adjusted_value = portfolio_value - PROFIT_ADJUSTMENT_FACTOR * get_total_profit()

    for e in earnings:
        ticker = e['act_symbol']
        when = e.get('when', 'AMC').upper()
        is_bmo = 'BEFORE' in when
        earnings_date = datetime.now().date() + (timedelta(days=1) if is_bmo else timedelta(days=0))
        if not is_time_to_open(earnings_date, 'BMO' if is_bmo else 'AMC'):
            continue

        rec = compute_recommendation(ticker)
        if not rec or rec.get('label') != 'recommend':
            continue

        chain_date = earnings_date + timedelta(days=1)
        strikes = select_iron_fly_strikes(ticker, chain_date)
        if not strikes or None in strikes:
            continue
        sc, sp, lc, lp = strikes

        # Get quote prices for sizing
        try:
            sc_bid, _ = get_single_option_quotes(sc)
            sp_bid, _ = get_single_option_quotes(sp)
            _, lc_ask = get_single_option_quotes(lc)
            _, lp_ask = get_single_option_quotes(lp)
        except:
            print(f"Quote failure for {ticker}.")
            continue

        credit = ((sc_bid + sp_bid) - (lc_ask + lp_ask)) / 2
        if credit <= 0:
            continue

        max_alloc = adjusted_value * 0.06
        quantity = int(max_alloc // (100 * credit))
        if quantity < 1:
            continue

        print(f"Opening Iron Fly for {ticker} @ {credit:.2f}, Q={quantity}")
        trade_data = {
            'Ticker': ticker,
            'Structure': 'Iron Fly',
            'Side': 'credit',
            'When': 'BMO' if is_bmo else 'AMC',
            'Short Symbol': f"{sc},{sp}",
            'Long Symbol': f"{lc},{lp}",
            'Implied Move': rec.get('expected_move', ''),
            'Open Date': datetime.now().strftime('%Y-%m-%d'),
            'Close Date': '',
            'Close Price': '',
            'Close Comm.': ''
        }

        def _on_filled(fill, base=trade_data):
            filled_price = float(getattr(fill, 'filled_avg_price', 0))
            filled_qty = int(float(getattr(fill, 'filled_qty', 0)))
            commission = getattr(fill, 'commission', 0) or 0
            if filled_qty > 0:
                base['Size'] = filled_qty
                base['Open Price'] = filled_price
                base['Open Comm.'] = commission
                trade_fill_queue.put((post_trade, base))

        place_iron_fly_order(sc, sp, lc, lp, quantity, limit_credit=credit, on_filled=_on_filled)

    for th in trade_monitor_threads:
        th.join()
    while not trade_fill_queue.empty():
        func, pdata = trade_fill_queue.get()
        func(pdata)


# --- Helper Functions ---

def get_total_profit():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT SUM((`Close Price` - `Open Price`) * Size * 100 - `Open Comm.` - `Close Comm.`) FROM trades WHERE `Close Date` IS NOT NULL")
        result = cur.fetchone()[0]
        return result * PROFIT_ADJUSTMENT_FACTOR if result and result > 0 else 0
    except:
        return 0

def post_trade(data):
    try:
        data['action'] = 'create'
        requests.post(GOOGLE_SCRIPT_URL, json=data)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""INSERT INTO trades ("Ticker","Implied Move","Structure","Side","When","Size","Short Symbol","Long Symbol","Open Date","Open Price","Open Comm.","Close Date","Close Price","Close Comm.") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            data['Ticker'], data['Implied Move'], data['Structure'], data['Side'], data['When'],
            data['Size'], data['Short Symbol'], data['Long Symbol'], data['Open Date'],
            data['Open Price'], data.get('Open Comm.', 0), '', '', 0))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Post trade error: {e}")

def is_time_to_open(earnings_date, when):
    now = datetime.now(tz=ZoneInfo("America/New_York"))
    base = earnings_date - timedelta(days=1) if when == "BMO" else earnings_date
    open_dt = datetime.combine(base, time(15, 35), tzinfo=ZoneInfo("America/New_York"))
    return open_dt <= now < open_dt + timedelta(minutes=40)

if __name__ == "__main__":
    sys.exit(run_trade_workflow())
