import os
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta, time
from automation import compute_recommendation, get_tomorrows_earnings, get_todays_earnings
from alpaca_integration import (
    init_alpaca_client, place_calendar_spread_order, 
    close_calendar_spread_order, get_portfolio_value, 
    select_expiries_and_strike_alpaca, get_alpaca_option_chain, 
    get_option_spread_mid_price, monitor_fill_async,
    get_single_option_quotes, close_single_option_leg_order
)
import yfinance as yf
from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.enums import OrderSide, PositionIntent
from zoneinfo import ZoneInfo
import sys
import sqlite3
import queue

# Constants
PROFIT_ADJUSTMENT_FACTOR = 0.5  # Only 50% of the profits are considered for adjustment

# queue for filled trades and tracking threads
trade_fill_queue = queue.Queue()
trade_monitor_threads = []

load_dotenv()
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
DB_PATH = "trades.db"

# Google Apps Script integration functions

def init_db():
    """Initialize SQLite DB and trades table if it doesn't exist."""
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
    # Migrate: add 'When' column if missing
    cursor.execute("PRAGMA table_info(trades)")
    cols = [row[1] for row in cursor.fetchall()]
    if "When" not in cols:
        cursor.execute('ALTER TABLE trades ADD COLUMN "When" TEXT')
    conn.commit()
    conn.close()

init_db()

def get_total_profit():
    """Calculate the total profit from all closed trades.
    
    Returns:
        float: Total profit from all closed trades, adjusted by PROFIT_ADJUSTMENT_FACTOR.
        Returns 0 if no closed trades or negative profit.
        
    Note:
        - Size represents the number of option contracts in the trade
        - Each contract represents 100 shares, hence the *100 multiplier
        - Open Comm. and Close Comm. are the commission costs from Alpaca for opening/closing trades
        - The final profit is adjusted by PROFIT_ADJUSTMENT_FACTOR (e.g., 0.5 means 50% of profit)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Calculate profit for each closed trade: (ClosePrice - OpenPrice) * Size * 100 - OpenComm - CloseComm
        cursor.execute("""
            SELECT SUM(
                ("Close Price" - "Open Price") * "Size" * 100 - "Open Comm." - "Close Comm."
            ) as TotalProfit
            FROM trades
            WHERE "Close Date" IS NOT NULL AND "Close Date" != ''
        """)
        result = cursor.fetchone()[0]
        conn.close()
        
        # Return adjusted profit if it exists and is positive, otherwise return 0
        if result is not None and result > 0:
            adjusted_profit = result * PROFIT_ADJUSTMENT_FACTOR
            print(f"Total profit from closed trades: ${result:.2f}, Adjusted profit (× {PROFIT_ADJUSTMENT_FACTOR}): ${adjusted_profit:.2f}")
            return adjusted_profit
        else:
            print("No positive profit found, defaulting to 0")
            return 0
    except Exception as e:
        print(f"Error calculating total profit: {e}")
        return 0

def post_trade(trade_data):
    """POST a new trade to the Google Apps Script endpoint."""
    try:
        # include action flag for create
        trade_data['action'] = 'create'
        trade_data.setdefault('Open Comm.', 0)
        trade_data.setdefault('Close Comm.', 0)
        r = requests.post(GOOGLE_SCRIPT_URL, json=trade_data)
        r.raise_for_status()
        print(f"POST trade: {trade_data} -> {r.text}")
        # insert into SQLite
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO trades ("Ticker","Implied Move","Structure","Side","When","Size","Short Symbol","Long Symbol","Open Date","Open Price","Open Comm.","Close Date","Close Price","Close Comm.")
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade_data.get('Ticker'),
                    trade_data.get('Implied Move'),
                    trade_data.get('Structure'),
                    trade_data.get('Side'),
                    trade_data.get('When'),
                    trade_data.get('Size'),
                    trade_data.get('Short Symbol'),
                    trade_data.get('Long Symbol'),
                    trade_data.get('Open Date'),
                    trade_data.get('Open Price'),
                    trade_data.get('Open Comm.', 0),
                    trade_data.get('Close Date'),
                    trade_data.get('Close Price'),
                    trade_data.get('Close Comm.', 0)
                )
            )
            conn.commit()
            conn.close()
        except Exception as db_e:
            print(f"Error inserting trade into SQLite: {db_e}")
        return r.text
    except Exception as e:
        print(f"Error posting trade: {e}")
        return None

def get_open_trades():
    """Retrieve open trades from local SQLite DB instead of Google Apps Script."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE [Close Date] IS NULL OR [Close Date] = ''")
        rows = cursor.fetchall()
        col_names = [description[0] for description in cursor.description]
        trades = [dict(zip(col_names, row)) for row in rows]
        conn.close()
        print("Fetched open trades from SQLite DB.")
        return trades
    except Exception as e:
        print(f"Error fetching open trades from SQLite DB: {e}")
        return []

def update_trade(trade_data):
    """PUT/POST to update a trade as closed in the Google Apps Script endpoint."""
    try:
        # update SQLite
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                """UPDATE trades
                   SET "Close Date" = ?,
                       "Close Price" = ?,
                       "Close Comm." = ?
                   WHERE "Ticker" = ? AND "Open Date" = ?""",
                (
                    trade_data.get('Close Date'),
                    trade_data.get('Close Price'),
                    trade_data.get('Close Comm.', 0),
                    trade_data.get('Ticker'),
                    trade_data.get('Open Date')
                )
            )
            conn.commit()
            conn.close()
        except Exception as db_e:
            print(f"Error updating trade in SQLite: {db_e}")
        
        # include action flag for update
        trade_data['action'] = 'update'
        r = requests.post(GOOGLE_SCRIPT_URL, json=trade_data)
        r.raise_for_status()
        print(f"Updated trade: {trade_data} -> {r.text}")
        return r.text
    except Exception as e:
        print(f"Error updating trade: {e}")
        return None

def is_time_to_open(earnings_date, when):
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(tz=eastern)
    market_close = time(16, 0)
    if when == "BMO":
        # Open window starts at 3:35 PM ET (25 minutes before close) to match GitHub Actions schedule
        open_dt = datetime.combine(earnings_date - timedelta(days=1), market_close, tzinfo=eastern) - timedelta(minutes=25)
    else:  # AMC
        # Open window starts at 3:35 PM ET (25 minutes before close) to match GitHub Actions schedule
        open_dt = datetime.combine(earnings_date, market_close, tzinfo=eastern) - timedelta(minutes=25)
    # Extended window: 3:35 PM to 4:15 PM ET (40 minutes total)
    return open_dt <= now < open_dt + timedelta(minutes=40)

def is_time_to_close(earnings_date, when):
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(tz=eastern)
    open_time = time(9, 30)
    if when == "BMO":
        close_dt = datetime.combine(earnings_date, open_time, tzinfo=eastern) + timedelta(minutes=15)
    else:  # AMC
        close_dt = datetime.combine(earnings_date + timedelta(days=1), open_time, tzinfo=eastern) + timedelta(minutes=15)
    # Close any due or overdue trades after close_dt (all trades close in the morning)
    return now >= close_dt

def select_expiries_and_strike_yahoo(stock, earnings_date):
    """
    (Renamed) Select front and back month expiries and ATM strike for the calendar spread using Yahoo Finance.
    """
    try:
        exp_dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in stock.options]
        exp_dates = sorted(exp_dates)
        expiry_short = next((d for d in exp_dates if d > earnings_date), None)
        if not expiry_short:
            return None, None, None
        target_back = expiry_short + timedelta(days=30)
        expiry_long = min((d for d in exp_dates if d > expiry_short), key=lambda d: abs((d - target_back).days), default=None)
        if not expiry_long:
            return None, None, None
        underlying_price = stock.history(period='1d')['Close'].iloc[0]
        chain = stock.option_chain(expiry_short.strftime('%Y-%m-%d'))
        strikes = chain.calls['strike'].tolist()
        strike = min(strikes, key=lambda x: abs(x - underlying_price))
        return expiry_short.strftime('%Y-%m-%d'), expiry_long.strftime('%Y-%m-%d'), strike
    except Exception as e:
        print(f"Error selecting expiries/strike: {e}")
        return None, None, None

def calculate_calendar_spread_cost_yahoo(stock, expiry_short, expiry_long, strike):
    """
    (Renamed) Calculate the cost of the calendar spread (mid prices) using Yahoo Finance.
    """
    try:
        chain_short = stock.option_chain(expiry_short)
        chain_long = stock.option_chain(expiry_long)
        call_short = chain_short.calls.loc[chain_short.calls['strike'] == strike]
        call_long = chain_long.calls.loc[chain_long.calls['strike'] == strike]
        if call_short.empty or call_long.empty:
            return None
        print(f"Yahoo quotes for short leg ({expiry_short} {strike}C): Bid={call_short['bid'].iloc[0]}, Ask={call_short['ask'].iloc[0]}")
        print(f"Yahoo quotes for long leg ({expiry_long} {strike}C): Bid={call_long['bid'].iloc[0]}, Ask={call_long['ask'].iloc[0]}")
        short_mid = (call_short['bid'].iloc[0] + call_short['ask'].iloc[0]) / 2
        long_mid = (call_long['bid'].iloc[0] + call_long['ask'].iloc[0]) / 2
        cost = long_mid - short_mid
        return float(cost)
    except Exception as e:
        print(f"Error calculating spread cost: {e}")
        return None

def run_trade_workflow():
    print("Running trade workflow...")
    # reset any previous monitor threads and queued trades
    trade_monitor_threads.clear()
    while not trade_fill_queue.empty():
        trade_fill_queue.get()
    # 0. Market open check via Alpaca clock
    client = init_alpaca_client()
    if not client:
        print("Could not initialize Alpaca client. Exiting.")
        return 1
    clock = client.get_clock()
    if not getattr(clock, 'is_open', False):
        print(f"Market is closed (next open at {clock.next_open}). Exiting.")
        return 1
    print(f"Market is open (current time: {clock.timestamp}). Continuing...")
    # 1. Close due trades
    open_trades = get_open_trades()
    for trade in open_trades:
        try:
            open_date = datetime.strptime(trade['Open Date'], "%Y-%m-%d").date()
            when = trade.get('When', 'AMC')
            # Determine actual earnings date: BMO trades have open_date = day before earnings
            if when == 'BMO':
                earnings_date = open_date + timedelta(days=1)
            else:
                earnings_date = open_date
            if is_time_to_close(earnings_date, when):
                print(f"Closing trade for {trade['Ticker']}...")
                # enqueue update when close-leg fills
                def _on_close_filled(filled, t=trade):
                    cp = float(getattr(filled, 'filled_avg_price', 0) or 0)
                    cc = getattr(filled, 'commission', 0) or 0
                    data = {
                        'Ticker': t['Ticker'],
                        'Open Date': t['Open Date'],
                        'Close Date': datetime.now().strftime('%Y-%m-%d'),
                        'Close Price': cp,
                        'Close Comm.': cc
                    }
                    trade_fill_queue.put((update_trade, data))
                # use creeping DAY close with callback
                order = close_calendar_spread_order(
                    trade.get('Short Symbol'),
                    trade.get('Long Symbol'),
                    trade.get('Size')
                )
                if order:
                    th = monitor_fill_async(client, order, _on_close_filled)
                    trade_monitor_threads.append(th)
                else:
                    print(f"Spread close order for {trade['Ticker']} ({trade.get('Short Symbol')}/{trade.get('Long Symbol')}) failed or was not placed. Checking individual legs.")
                    short_symbol = trade.get('Short Symbol')
                    long_symbol = trade.get('Long Symbol')
                    size = trade.get('Size')
                    
                    short_quotable = False
                    long_quotable = False

                    if short_symbol and size > 0:
                        try:
                            get_single_option_quotes(short_symbol)
                            short_quotable = True
                            print(f"Short leg {short_symbol} for {trade['Ticker']} is quotable.")
                        except RuntimeError:
                            print(f"Short leg {short_symbol} for {trade['Ticker']} is unquotable (likely expired or no market).")
                        except Exception as e_quote_short:
                            print(f"Error checking short leg {short_symbol} quotability: {e_quote_short}")
                    else:
                        print(f"Skipping quotability check for short leg for {trade['Ticker']} due to missing symbol or zero size.")
                        short_leg_closed_or_expired = True

                    if long_symbol and size > 0:
                        try:
                            get_single_option_quotes(long_symbol)
                            long_quotable = True
                            print(f"Long leg {long_symbol} for {trade['Ticker']} is quotable.")
                        except RuntimeError:
                            print(f"Long leg {long_symbol} for {trade['Ticker']} is unquotable (likely expired or no market).")
                        except Exception as e_quote_long:
                            print(f"Error checking long leg {long_symbol} quotability: {e_quote_long}")
                    else:
                        print(f"Skipping quotability check for long leg for {trade['Ticker']} due to missing symbol or zero size.")
                        long_leg_closed_or_expired = True

                    if not short_quotable and long_quotable:
                        print(f"Attempting to close remaining long leg {long_symbol} for {trade['Ticker']} as short leg is unquotable.")
                        single_leg_order = close_single_option_leg_order(long_symbol, size, PositionIntent.SELL_TO_CLOSE)
                        if single_leg_order:
                            def _on_single_long_leg_closed(filled, t=trade):
                                cp = float(getattr(filled, 'filled_avg_price', 0) or 0)
                                cc = getattr(filled, 'commission', 0) or 0
                                data = {
                                    'Ticker': t['Ticker'], 'Open Date': t['Open Date'],
                                    'Close Date': datetime.now().strftime('%Y-%m-%d'),
                                    'Close Price': cp,
                                    'Close Comm.': cc,
                                }
                                print(f"Callback: Successfully processed close for remaining long leg {t.get('Long Symbol')} for {t['Ticker']}. Filled: {getattr(filled, 'id', 'N/A')}")
                                trade_fill_queue.put((update_trade, data))
                            
                            th = monitor_fill_async(client, single_leg_order, _on_single_long_leg_closed)
                            trade_monitor_threads.append(th)
                        else:
                            print(f"Failed to place order to close single long leg {long_symbol} for {trade['Ticker']}. Position may require manual review.")
                    
                    elif not long_quotable and short_quotable:
                        print(f"Attempting to close remaining short leg {short_symbol} for {trade['Ticker']} as long leg is unquotable.")
                        single_leg_order = close_single_option_leg_order(short_symbol, size, PositionIntent.BUY_TO_CLOSE)
                        if single_leg_order:
                            def _on_single_short_leg_closed(filled, t=trade):
                                cp = float(getattr(filled, 'filled_avg_price', 0) or 0)
                                cc = getattr(filled, 'commission', 0) or 0
                                data = {
                                    'Ticker': t['Ticker'], 'Open Date': t['Open Date'],
                                    'Close Date': datetime.now().strftime('%Y-%m-%d'),
                                    'Close Price': -cp,
                                    'Close Comm.': cc,
                                }
                                print(f"Callback: Successfully processed close for remaining short leg {t.get('Short Symbol')} for {t['Ticker']}. Filled: {getattr(filled, 'id', 'N/A')}")
                                trade_fill_queue.put((update_trade, data))

                            th = monitor_fill_async(client, single_leg_order, _on_single_short_leg_closed)
                            trade_monitor_threads.append(th)
                        else:
                            print(f"Failed to place order to close single short leg {short_symbol} for {trade['Ticker']}. Position may require manual review.")

                    elif not short_quotable and not long_quotable:
                        print(f"Both legs for {trade['Ticker']} ({short_symbol}, {long_symbol}) are unquotable. Marking trade as closed with $0 value.")
                        data_both_unquotable = {
                            'Ticker': trade['Ticker'], 'Open Date': trade['Open Date'],
                            'Close Date': datetime.now().strftime('%Y-%m-%d'),
                            'Close Price': 0, 'Close Comm.': 0,
                        }
                        trade_fill_queue.put((update_trade, data_both_unquotable))
                    
                    elif short_quotable and long_quotable:
                        print(f"Spread order failed for {trade['Ticker']}, but both legs ({short_symbol}, {long_symbol}) appear individually quotable. Original attempt to close as spread did not succeed. Skipping automated single leg closure for now.")
                    else:
                        print(f"Could not determine specific closing action for {trade['Ticker']} ({short_symbol}, {long_symbol}). Original close order failed. Skipping monitor.")
        except Exception as e:
            print(f"Error closing trade: {e}")
    # wait for all close-trade monitor threads before proceeding
    for th in trade_monitor_threads:
        th.join()
    while not trade_fill_queue.empty():
        func, pdata = trade_fill_queue.get()
        func(pdata)
    trade_monitor_threads.clear()
    # Skip opening new trades during morning run to only close open orders
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(tz=eastern)
    if now.time() < time(12, 0):
        print("Morning run: skipping opening new trades and API pulls.")
        return
    # 2. Screen and open new trades
    # Fetch both today's and tomorrow's earnings
    todays_earnings = get_todays_earnings()
    tomorrows_earnings = get_tomorrows_earnings()
    portfolio_value = get_portfolio_value()
    if not portfolio_value:
        print("Could not fetch portfolio value. Skipping trade opening.")
        return
    
    # Calculate total profit and subtract it from portfolio value to determine available capital
    total_profit = get_total_profit()
    adjusted_portfolio_value = portfolio_value - total_profit
    print(f"Portfolio value: ${portfolio_value:.2f}, Adjusted for profit: ${adjusted_portfolio_value:.2f}")
    
    # Open BMO trades for tomorrow's earnings (open the day before)
    for ticker_info in tomorrows_earnings:
        ticker = ticker_info['act_symbol']
        when = ticker_info.get('when')
        if not when:
            print(f"Skipping {ticker}: no 'when' info available.")
            continue
        when_norm = 'BMO' if 'before' in (when or '').lower() else 'AMC'
        if when_norm != 'BMO':
            continue  # Only process BMO here
        try:
            rec = compute_recommendation(ticker)
            if isinstance(rec, dict) and rec.get('avg_volume') and rec.get('iv30_rv30') and rec.get('ts_slope_0_45'):
                earnings_date = datetime.now().date() + timedelta(days=1)
                if is_time_to_open(earnings_date, when_norm):
                    print(f"Preparing BMO trade for {ticker} ({when_norm})...")
                    stock = yf.Ticker(ticker)
                    # allow same-day expiry for BMO by filtering from one day earlier
                    filter_date = earnings_date - timedelta(days=1) if when_norm == 'BMO' else earnings_date
                    expiry_short, expiry_long, strike = select_expiries_and_strike_alpaca(ticker, filter_date)
                    if not expiry_short or not expiry_long or not strike:
                        print(f"Could not determine expiries/strike for {ticker} using Alpaca. Trying Yahoo...")
                        stock = yf.Ticker(ticker)
                        expiry_short, expiry_long, strike = select_expiries_and_strike_yahoo(stock, filter_date)
                    if not expiry_short or not expiry_long or not strike:
                        print(f"Could not determine expiries/strike for {ticker}. Skipping.")
                        continue
                    spread_cost = get_option_spread_mid_price(ticker, expiry_short, expiry_long, strike)
                    print(f"Alpaca spread_cost for {ticker}: {spread_cost}")
                    if spread_cost is None:
                        print(f"Invalid spread cost for {ticker} using Alpaca (value={spread_cost}). Trying Yahoo...")
                        stock = yf.Ticker(ticker)
                        spread_cost = calculate_calendar_spread_cost_yahoo(stock, expiry_short, expiry_long, strike)
                        print(f"Yahoo spread_cost for {ticker}: {spread_cost}")
                    if spread_cost is None:
                        print(f"Invalid spread cost for {ticker} (value={spread_cost}). Skipping.")
                        continue
                    
                    # Add check for non-positive spread_cost before calculating quantity
                    if spread_cost <= 0:
                        print(f"Spread cost for {ticker} is ${spread_cost:.2f} (non-positive). Skipping.")
                        continue

                    # Fetch OCC symbols from Alpaca chain
                    chain = get_alpaca_option_chain(ticker)
                    short_contract = chain.get(expiry_short, {}).get(strike, {}).get('call')
                    long_contract = chain.get(expiry_long, {}).get(strike, {}).get('call')
                    short_symbol = getattr(short_contract, 'symbol', None)
                    long_symbol = getattr(long_contract, 'symbol', None)
                    # Fetch live mid price for limit order
                    limit_price = get_option_spread_mid_price(ticker, expiry_short, expiry_long, strike)
                    kelly_fraction = 0.06
                    max_allocation = adjusted_portfolio_value * kelly_fraction
                    quantity = int(max_allocation // (spread_cost * 100))  # 1 contract = 100 shares
                    if quantity < 1:
                        print(f"Kelly sizing yields 0 contracts for {ticker}. Skipping.")
                        continue
                    implied_move = rec.get('expected_move', '')
                    print(f"Opening BMO trade for {ticker}: {quantity}x {expiry_short}/{expiry_long} @ {strike}, cost/spread: ${spread_cost:.2f}, Kelly allocation: ${max_allocation:.2f}, Implied Move: {implied_move}")
                    
                    base_open_data_bmo = { # Renamed to indicate it's a base template
                        'Short Symbol': short_symbol,
                        'Long Symbol': long_symbol,
                        'Ticker': ticker,
                        'Implied Move': implied_move,
                        'Structure': 'Calendar Spread',
                        'Side': 'debit',
                        'When': when_norm,
                        # Size, Open Date, Open Price, Open Comm. will be set per fill
                        'Close Date': '',
                        'Close Price': '',
                        'Close Comm.': ''
                    }

                    def _on_open_filled(filled, base_data=base_open_data_bmo): # Pass base_data
                        # Make a copy for this specific fill to avoid modifying shared state
                        data_for_this_fill = base_data.copy()
                        
                        data_for_this_fill['Open Date'] = datetime.now().strftime('%Y-%m-%d')
                        # Price and Qty are from the specific filled slice
                        data_for_this_fill['Open Price'] = float(getattr(filled, 'filled_avg_price', 0) or 0)
                        data_for_this_fill['Size'] = int(float(getattr(filled, 'filled_qty', 0) or 0))
                        data_for_this_fill['Open Comm.'] = getattr(filled, 'commission', 0) or 0
                        
                        if data_for_this_fill['Size'] > 0: # Only post if something actually filled for this slice
                            trade_fill_queue.put((post_trade, data_for_this_fill))
                        else:
                            print(f"Warning: _on_open_filled called for {base_data.get('Ticker')} but filled_qty is 0. Order ID: {getattr(filled, 'id', 'N/A')}")

                    # use creeping DAY open with callback
                    # No longer need external monitor_fill_async for opening trades
                    order_status = place_calendar_spread_order(
                        short_symbol,
                        long_symbol,
                        quantity, # This is the original_intended_quantity
                        limit_price=limit_price, # Initial target, will be refined by target_debit_price logic
                        on_filled=_on_open_filled,
                        max_total_cost_allowed=max_allocation,
                        target_debit_price=spread_cost # New parameter: do not exceed this initial cost much
                    )
                    if order_status is None: # place_calendar_spread_order now returns cumulative_filled_order_obj or None
                        print(f"Order placement process did not result in a confirmed fill for {ticker}. Skipping further processing for this attempt.")
                        # Continue to next ticker, no thread to append
                else:
                    print(f"Skipping {ticker}: not in correct time window to open BMO trade.")
        except Exception as e:
            print(f"Error screening/opening BMO trade for {ticker}: {e}")
    # Open AMC trades for today's earnings (open the day of)
    for ticker_info in todays_earnings:
        ticker = ticker_info['act_symbol']
        when = ticker_info.get('when')
        if not when:
            print(f"Skipping {ticker}: no 'when' info available.")
            continue
        when_norm = 'BMO' if 'before' in (when or '').lower() else 'AMC'
        if when_norm != 'AMC':
            continue  # Only process AMC here
        try:
            rec = compute_recommendation(ticker)
            if isinstance(rec, dict) and rec.get('avg_volume') and rec.get('iv30_rv30') and rec.get('ts_slope_0_45'):
                earnings_date = datetime.now().date()
                if is_time_to_open(earnings_date, when_norm):
                    print(f"Preparing AMC trade for {ticker} ({when_norm})...")
                    stock = yf.Ticker(ticker)
                    expiry_short, expiry_long, strike = select_expiries_and_strike_alpaca(ticker, earnings_date)
                    if not expiry_short or not expiry_long or not strike:
                        print(f"Could not determine expiries/strike for {ticker} using Alpaca. Trying Yahoo...")
                        stock = yf.Ticker(ticker)
                        expiry_short, expiry_long, strike = select_expiries_and_strike_yahoo(stock, earnings_date)
                    if not expiry_short or not expiry_long or not strike:
                        print(f"Could not determine expiries/strike for {ticker}. Skipping.")
                        continue
                    spread_cost = get_option_spread_mid_price(ticker, expiry_short, expiry_long, strike)
                    if spread_cost is None:
                        print(f"Invalid spread cost for {ticker} using Alpaca. Trying Yahoo...")
                        stock = yf.Ticker(ticker)
                        spread_cost = calculate_calendar_spread_cost_yahoo(stock, expiry_short, expiry_long, strike)
                    if spread_cost is None:
                        print(f"Invalid spread cost for {ticker}. Skipping.")
                        continue
                    
                    # Add check for non-positive spread_cost before calculating quantity
                    if spread_cost <= 0:
                        print(f"Spread cost for {ticker} is ${spread_cost:.2f} (non-positive). Skipping.")
                        continue

                    # Fetch OCC symbols from Alpaca chain for AMC
                    chain = get_alpaca_option_chain(ticker)
                    short_contract = chain.get(expiry_short, {}).get(strike, {}).get('call')
                    long_contract = chain.get(expiry_long, {}).get(strike, {}).get('call')
                    short_symbol = getattr(short_contract, 'symbol', None)
                    long_symbol = getattr(long_contract, 'symbol', None)
                    limit_price = get_option_spread_mid_price(ticker, expiry_short, expiry_long, strike)
                    kelly_fraction = 0.06
                    max_allocation = adjusted_portfolio_value * kelly_fraction
                    quantity = int(max_allocation // (spread_cost * 100))  # 1 contract = 100 shares
                    if quantity < 1:
                        print(f"Kelly sizing yields 0 contracts for {ticker}. Skipping.")
                        continue
                    implied_move = rec.get('expected_move', '')
                    print(f"Opening AMC trade for {ticker}: {quantity}x {expiry_short}/{expiry_long} @ {strike}, cost/spread: ${spread_cost:.2f}, Kelly allocation: ${max_allocation:.2f}, Implied Move: {implied_move}")
                    
                    base_open_data_amc = { # Renamed for AMC
                        'Short Symbol': short_symbol,
                        'Long Symbol': long_symbol,
                        'Ticker': ticker,
                        'Implied Move': implied_move,
                        'Structure': 'Calendar Spread',
                        'Side': 'debit',
                        'When': when_norm,
                        # Size, Open Date, Open Price, Open Comm. will be set per fill
                        'Close Date': '',
                        'Close Price': '',
                        'Close Comm.': ''
                    }

                    def _on_open_amc_filled(filled, base_data=base_open_data_amc): # Pass base_data
                        # Make a copy for this specific fill
                        data_for_this_fill = base_data.copy()
                        
                        data_for_this_fill['Open Date'] = datetime.now().strftime('%Y-%m-%d')
                        data_for_this_fill['Open Price'] = float(getattr(filled, 'filled_avg_price', 0) or 0)
                        data_for_this_fill['Size'] = int(float(getattr(filled, 'filled_qty', 0) or 0))
                        data_for_this_fill['Open Comm.'] = getattr(filled, 'commission', 0) or 0

                        if data_for_this_fill['Size'] > 0: # Only post if something actually filled
                            trade_fill_queue.put((post_trade, data_for_this_fill))
                        else:
                            print(f"Warning: _on_open_amc_filled called for {base_data.get('Ticker')} but filled_qty is 0. Order ID: {getattr(filled, 'id', 'N/A')}")
                    
                    # No longer need external monitor_fill_async for opening trades
                    order_status = place_calendar_spread_order(
                        short_symbol,
                        long_symbol,
                        quantity, # original_intended_quantity
                        limit_price=limit_price,
                        on_filled=_on_open_amc_filled,
                        max_total_cost_allowed=max_allocation,
                        target_debit_price=spread_cost # New parameter
                    )
                    if order_status is None:
                        print(f"Order placement process did not result in a confirmed fill for {ticker}. Skipping further processing for this attempt.")
                        # Continue to next ticker
                else:
                    print(f"Skipping {ticker}: not in correct time window to open AMC trade.")
        except Exception as e:
            print(f"Error screening/opening AMC trade for {ticker}: {e}")
    # after all open-trade monitor threads, wait and flush queue
    for th in trade_monitor_threads:
        th.join()
    while not trade_fill_queue.empty():
        func, pdata = trade_fill_queue.get()
        func(pdata)
if __name__ == "__main__":
    sys.exit(run_trade_workflow())
