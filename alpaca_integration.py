import os
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Position
from dotenv import load_dotenv
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, OptionLatestQuoteRequest
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderClass, TimeInForce, OrderSide, PositionIntent
from datetime import datetime, timedelta
import threading, time
from alpaca.trading.requests import GetOrderByIdRequest
from alpaca.trading.enums import OrderStatus
import math
from alpaca.common.exceptions import APIError

load_dotenv()

API_KEY = os.environ.get("APCA_API_KEY_ID")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "false").lower() == "false"


def init_alpaca_client():
    try:
        client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
        print(f"Alpaca client initialized. Paper mode: {PAPER}")
        return client
    except Exception as e:
        print(f"Error initializing Alpaca client: {e}")
        return None


def place_calendar_spread_order(short_symbol, long_symbol, original_intended_quantity, limit_price=None, on_filled=None, max_total_cost_allowed=None, target_debit_price=None):
    """
    Place a call calendar spread using provided OCC symbols for each leg.
    Optionally specify a limit_price for the spread order (initial target).
    max_total_cost_allowed caps the total debit for the original_intended_quantity.
    target_debit_price is the ideal maximum debit per share; chase won't go significantly beyond this.
    Quantity may be dynamically reduced if price chase makes original quantity too expensive.
    """
    client = init_alpaca_client()
    if not client:
        return None
    
    if original_intended_quantity < 1:
        print(f"Original intended quantity for {short_symbol}/{long_symbol} is < 1. Skipping order.")
        return None

    try:
        # This limit_price parameter is the initial target, but the chase starts from mid_spread
        # We will use it as a reference if provided, but primarily rely on dynamic chasing from mid.
        if limit_price is not None:
            limit_price = round(limit_price, 2) # Store for reference, not directly used to start chase

        # Fetch initial quotes for creeping logic
        short_bid, short_ask, long_bid, long_ask = get_spread_quotes(short_symbol, long_symbol)
        print(f"Initial quotes for {short_symbol}: Bid={short_bid}, Ask={short_ask}")
        print(f"Initial quotes for {long_symbol}: Bid={long_bid}, Ask={long_ask}")
        
        current_market_mid_spread = (long_bid + long_ask) / 2 - (short_bid + short_ask) / 2
        # max_price is the absolute worst price (long ask - short bid)
        market_max_debit = long_ask - short_bid 
        
        price_to_chase = current_market_mid_spread # Start chasing from current mid
        # Dynamic crawling step: half the sum of bid-ask spreads on both legs (min $0.01)
        spread_short = short_ask - short_bid
        spread_long = long_ask - long_bid
        chase_step = max((spread_short + spread_long) / 2.0, 0.01)
        
        # Determine the actual maximum price to chase up to.
        # target_debit_price (ideal entry from initial mid) is for logging/reference here.
        # previously the market_max_debit was used as the effective max chase price
        effective_max_chase_price = long_ask
        if target_debit_price is not None:
            # Log both the ideal target and the actual chase limit (market ask)
            print(f"Ideal target debit price for {short_symbol}/{long_symbol} is ${target_debit_price:.2f}. Will chase up to market ask (effective max chase price): ${effective_max_chase_price:.2f}")
        else:
            # This case might not be hit if trade_workflow always provides target_debit_price
            print(f"No ideal target_debit_price provided. Chasing up to market ask (effective max chase price): ${effective_max_chase_price:.2f}")

        remaining_overall_quantity = original_intended_quantity
        total_cost_so_far = 0.0
        total_filled_qty = 0
        total_filled_value = 0.0
        total_commission = 0.0

        print(f"Starting trade for {short_symbol}/{long_symbol}: original_qty={original_intended_quantity}, max_total_cost_allowed=${max_total_cost_allowed if max_total_cost_allowed is not None else 'N/A'}")

        # Creeping DAY loop from mid toward effective_max_chase_price
        while remaining_overall_quantity > 0 and price_to_chase <= effective_max_chase_price:
            current_limit_price_attempt = round(price_to_chase, 2)
            
            affordable_qty_at_this_lp = remaining_overall_quantity # Default to trying to fill all remaining
            if max_total_cost_allowed is not None:
                remaining_budget = max_total_cost_allowed - total_cost_so_far
                if current_limit_price_attempt <= 0: # Avoid division by zero if spread somehow goes free/credit when chasing debit
                    if remaining_budget < 0: # No budget left even if it's free
                        affordable_qty_at_this_lp = 0
                    # else, if it's free/credit and budget is non-negative, can afford all remaining
                elif remaining_budget > 0:
                    affordable_qty_at_this_lp = math.floor(remaining_budget / (current_limit_price_attempt * 100))
                else: # No budget left
                    affordable_qty_at_this_lp = 0

            qty_for_this_order_attempt = min(remaining_overall_quantity, affordable_qty_at_this_lp)

            if qty_for_this_order_attempt < 1:
                print(f"Stopping chase for {short_symbol}/{long_symbol}: Cannot afford even 1 share/contract at ${current_limit_price_attempt}. Budget left: ${remaining_budget:.2f}, Qty needed: {remaining_overall_quantity}")
                break
            
            # Simplify leg ratios for this specific order attempt's quantity
            # In a 1:1 spread, gcd will be qty_for_this_order_attempt, so ratios are 1
            gcd_val = math.gcd(qty_for_this_order_attempt, qty_for_this_order_attempt)
            ratio_qty_short = qty_for_this_order_attempt // gcd_val
            ratio_qty_long = qty_for_this_order_attempt // gcd_val

            legs_for_this_order = [
                OptionLegRequest(
                    symbol=short_symbol,
                    ratio_qty=1, # ratio_qty_short Use ratio for the order quantity
                    side=OrderSide.SELL,
                    position_intent=PositionIntent.SELL_TO_OPEN
                ),
                OptionLegRequest(
                    symbol=long_symbol,
                    ratio_qty=1, # ratio_qty_long Use ratio for the order quantity
                    side=OrderSide.BUY,
                    position_intent=PositionIntent.BUY_TO_OPEN
                )
            ]

            req = LimitOrderRequest(
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                qty=qty_for_this_order_attempt, # This is the actual Alpaca quantity for this order
                legs=legs_for_this_order,
                limit_price=current_limit_price_attempt
            )
            
            submitted_order_this_attempt = None
            try:
                submitted_order_this_attempt = client.submit_order(req)
                print(f"Placed DAY opening for {short_symbol}/{long_symbol}: {qty_for_this_order_attempt} qty @ limit ${current_limit_price_attempt}. Order ID: {submitted_order_this_attempt.id}")
                
                filled_order_details = wait_for_fill(client, submitted_order_this_attempt.id, timeout=30)
                filled_qty_this_order = int(float(getattr(filled_order_details, 'filled_qty', 0) or 0))
                
                if filled_qty_this_order > 0:
                    avg_price = float(getattr(filled_order_details, 'filled_avg_price', 0) or 0)
                    cost_this_fill = avg_price * filled_qty_this_order * 100
                    total_cost_so_far += cost_this_fill
                    remaining_overall_quantity -= filled_qty_this_order
                    total_filled_qty += filled_qty_this_order
                    total_filled_value += avg_price * filled_qty_this_order
                    total_commission += getattr(filled_order_details, 'commission', 0) or 0
                    print(f"Order {submitted_order_this_attempt.id} for {short_symbol}/{long_symbol}: Filled {filled_qty_this_order} at ${avg_price}. Remaining overall: {remaining_overall_quantity}. Total cost so far: ${total_cost_so_far:.2f}")

                if remaining_overall_quantity <= 0:
                    print(f"Entire trade for {short_symbol}/{long_symbol} ({original_intended_quantity} contracts) filled.")
                    break # Exit chase loop
                
                # If the order submitted was partially filled (filled_qty_this_order < qty_for_this_order_attempt)
                # and not fully cancelled by wait_for_fill, try to cancel the remainder before chasing at a new price.
                if filled_qty_this_order < qty_for_this_order_attempt and filled_order_details.status != OrderStatus.CANCELED:
                    try:
                        client.cancel_order_by_id(submitted_order_this_attempt.id)
                        print(f"Cancelled remaining part of order {submitted_order_this_attempt.id} for {short_symbol}/{long_symbol} after partial fill.")
                    except APIError as e:
                        if e.status_code != 422: raise
                        else: print(f"Order {submitted_order_this_attempt.id} not cancelable (likely fully filled/expired); ignoring 422.")
            
            except TimeoutError:
                print(f"Order {submitted_order_this_attempt.id if submitted_order_this_attempt else 'N/A'} ({qty_for_this_order_attempt} qty @ ${current_limit_price_attempt}) for {short_symbol}/{long_symbol} timed out (30s). Cancelling if exists.")
                if submitted_order_this_attempt:
                    try:
                        client.cancel_order_by_id(submitted_order_this_attempt.id)
                    except APIError as e:
                        if e.status_code != 422: raise
                        else: print(f"Order {submitted_order_this_attempt.id} not cancelable on timeout (likely filled/expired); ignoring 422.")
            except Exception as e_order_submission_loop:
                print(f"Error during order submission/fill loop for {short_symbol}/{long_symbol}: {e_order_submission_loop}")
                # Decide if we should break or continue; for now, let's increment price and retry
                # unless it's a critical API error, but most are caught by the outer try/except.
            
            price_to_chase += chase_step
        
        if total_filled_qty > 0:
            cumulative_avg = total_filled_value / total_filled_qty
            summary = type('OrderSummary',(object,),{})
            summary.filled_avg_price = str(cumulative_avg)
            summary.filled_qty = str(total_filled_qty)
            summary.commission = total_commission
            summary.id = getattr(filled_order_details, 'id', 'cumulative_open_fill')
            summary.symbol = short_symbol
            if on_filled:
                on_filled(summary)
            return summary
        else:
            print(f"Could not fill any quantity for {short_symbol}/{long_symbol} within price limits (last attempt price: ${round(price_to_chase-chase_step,2)}, effective max chase: ${effective_max_chase_price:.2f}).")
            return None

    except Exception as e:
        print(f"Error placing calendar spread order for {short_symbol}/{long_symbol}: {e}")
        return None


def close_calendar_spread_order(short_symbol, long_symbol, quantity):
    """
    Close both legs of the call calendar spread using provided OCC symbols.
    """
    client = init_alpaca_client()
    if not client:
        return None
    try:
        # Fetch quotes for closing creeping logic
        short_bid, short_ask, long_bid, long_ask = get_spread_quotes(short_symbol, long_symbol)
        print(f"Initial quotes for {short_symbol}: Bid={short_bid}, Ask={short_ask}")
        print(f"Initial quotes for {long_symbol}: Bid={long_bid}, Ask={long_ask}")
        
        # Calculate the most aggressive credit target: selling long at its ask, buying short at its bid.
        # This is what the user means by "try at the ask".
        initial_target_credit_value = long_ask - short_bid
        
        # min_price will be the upper bound for the Alpaca limit_price (max debit we are willing to pay)
        # User wants to use short_ask as the max debit.
        min_price = short_ask
        
        # price will now represent the actual limit_price to be sent to Alpaca.
        # Negative for credit, positive for debit. Start by targeting the most aggressive credit.
        price = -initial_target_credit_value # e.g., if target credit is $0.60, price is -0.60
        
        # dynamic crawling step: half the sum of bid-ask spreads on both legs (min $0.01)
        spread_short = short_ask - short_bid
        spread_long = long_ask - long_bid
        step = max((spread_short + spread_long) / 2.0, 0.01)
        remaining = quantity
        last_order = None
        total_filled_qty = 0
        total_filled_value = 0.0
        
        # Creeping DAY loop.
        # price (Alpaca limit_price) starts negative (credit) and creeps up towards min_price (max debit).
        while remaining > 0 and price <= min_price:
            lp = round(price, 2) # lp is the correctly signed Alpaca limit_price
            req = LimitOrderRequest(
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                qty=remaining,
                legs=[
                    OptionLegRequest(
                        symbol=short_symbol,
                        ratio_qty=1,
                        side=OrderSide.BUY,
                        position_intent=PositionIntent.BUY_TO_CLOSE
                    ),
                    OptionLegRequest(
                        symbol=long_symbol,
                        ratio_qty=1,
                        side=OrderSide.SELL,
                        position_intent=PositionIntent.SELL_TO_CLOSE
                    )
                ],
                limit_price=lp
            )
            last_order = client.submit_order(req)
            print(f"Placed DAY closing at limit ${lp}: {last_order}")
            try:
                filled = wait_for_fill(client, last_order.id, timeout=60)
                filled_qty = int(float(getattr(filled, 'filled_qty', 0)))
                if filled_qty > 0:
                    avg_price_this_fill = float(getattr(filled, 'filled_avg_price', 0.0))
                    total_filled_qty += filled_qty
                    total_filled_value += avg_price_this_fill * filled_qty
                remaining -= filled_qty
                if remaining <= 0:
                    if total_filled_qty > 0:
                        cumulative_avg_price = total_filled_value / total_filled_qty
                        setattr(filled, 'filled_avg_price', str(cumulative_avg_price))
                        setattr(filled, 'filled_qty', str(total_filled_qty))
                        last_order = filled
                    break
            except TimeoutError:
                # cancel unfilled order after timeout for DAY TIF
                try:
                    client.cancel_order_by_id(last_order.id)
                except APIError as e:
                    if e.status_code != 422:
                        raise
                    else:
                        print(f"Order {last_order.id} not cancelable (likely filled); ignoring 422.")
            else:
                # cancel any remaining unfilled portion after a fill
                if remaining > 0:
                    try:
                        client.cancel_order_by_id(last_order.id)
                    except APIError as e:
                        if e.status_code != 422:
                            raise
                        else:
                            print(f"Order {last_order.id} not cancelable (likely filled); ignoring 422.")
            price += step # Creep price upwards (less credit or more debit)
        
        if total_filled_qty > 0 and total_filled_qty < quantity : # Partially filled overall
            if last_order:
                cumulative_avg_price = total_filled_value / total_filled_qty
                final_summary_order = type('OrderSummary', (object,), {
                    'filled_avg_price': str(cumulative_avg_price),
                    'filled_qty': str(total_filled_qty),
                    'commission': getattr(last_order, 'commission', 0),
                    'id': getattr(last_order, 'id', 'cumulative_partial_fill'),
                    'symbol': short_symbol
                })
                return final_summary_order

        elif total_filled_qty == quantity: # Fully filled (possibly in multiple steps)
             return last_order

        # If no fills at all, last_order will be the last submitted (and presumably cancelled) order.
        # The `monitor_fill_async` will timeout or error, and `on_filled` won't be called.
        # This is existing behavior.
        return last_order
    except Exception as e:
        print(f"Error closing calendar spread order: {e}")
        return None


def get_open_option_positions():
    client = init_alpaca_client()
    if not client:
        return []
    try:
        positions = client.get_all_positions()
        option_positions = [p for p in positions if isinstance(p, Position) and p.asset_class == 'option']
        print(f"Open option positions: {option_positions}")
        return option_positions
    except Exception as e:
        print(f"Error fetching open option positions: {e}")
        return []


def get_portfolio_value():
    """Fetch the current portfolio/account equity value from Alpaca (in USD)."""
    client = init_alpaca_client()
    if not client:
        return None
    try:
        account = client.get_account()
        equity = float(account.equity)
        print(f"Current portfolio value (equity): ${equity}")
        return equity
    except Exception as e:
        print(f"Error fetching portfolio value: {e}")
        return None


def get_alpaca_option_chain(symbol):
    """
    Fetch the option chain for a given symbol using Alpaca's REST API.
    Returns a dict: {expiry: {strike: {call: {...}, put: {...}}}}
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOptionContractsRequest
        from datetime import datetime
        trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
        today = datetime.now().date()
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol.upper()],
            expiration_date_gte=today,
            limit=10000
        )
        response = trading_client.get_option_contracts(req)
        contracts = response.option_contracts or []
        # Organize by expiry and strike
        option_chain = {}
        for contract in contracts:
            expiry = contract.expiration_date.strftime('%Y-%m-%d')
            strike = float(contract.strike_price)
            cp = contract.type  # 'call' or 'put'
            if expiry not in option_chain:
                option_chain[expiry] = {}
            if strike not in option_chain[expiry]:
                option_chain[expiry][strike] = {}
            option_chain[expiry][strike][cp] = contract
        return option_chain
    except Exception as e:
        print(f"Error fetching Alpaca option chain for {symbol}: {e}")
        return None


def select_expiries_and_strike_alpaca(symbol, earnings_date):
    """
    Use Alpaca's option chain to select front and back month expiries and ATM strike for the calendar spread.
    Returns (expiry_short, expiry_long, strike) or (None, None, None) if not found.
    """
    option_chain = get_alpaca_option_chain(symbol)
    if not option_chain:
        return None, None, None
    try:
        exp_dates = sorted([datetime.strptime(d, "%Y-%m-%d").date() for d in option_chain.keys()])
        # Find front month expiry (first after earnings)
        expiry_short = next((d for d in exp_dates if d > earnings_date), None)
        if not expiry_short:
            return None, None, None
        # Find back month expiry (closest to 30 days after front)
        target_back = expiry_short + timedelta(days=30)
        expiry_long = min((d for d in exp_dates if d > expiry_short), key=lambda d: abs((d - target_back).days), default=None)
        if not expiry_long:
            return None, None, None
        # Get ATM strike (closest to underlying price)
        # Fetch underlying price from Alpaca (latest bar)
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestBarRequest
        stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)
        bar_resp = stock_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=symbol))
        if not bar_resp or symbol.upper() not in bar_resp:
            print(f"No price data for {symbol}")
            return None, None, None
        underlying_price = bar_resp[symbol.upper()].close
        strikes = list(option_chain[expiry_short.strftime('%Y-%m-%d')].keys())
        strike = min(strikes, key=lambda x: abs(x - underlying_price))
        return expiry_short.strftime('%Y-%m-%d'), expiry_long.strftime('%Y-%m-%d'), strike
    except Exception as e:
        print(f"Error selecting expiries/strike from Alpaca: {e}")
        return None, None, None


def get_option_spread_mid_price(symbol, expiry_short, expiry_long, strike, callput='C'):
    """
    Fetch the latest quotes for both legs and return the mid price for the calendar spread (long_mid - short_mid).
    Returns float or None if unavailable.
    """
    def make_option_symbol(symbol, expiry, strike, callput):
        expiry_fmt = expiry.replace('-', '')[2:]
        strike_fmt = f"{int(float(strike) * 1000):08d}"
        return f"{symbol.upper()}{expiry_fmt}{callput.upper()}{strike_fmt}"
    try:
        options_client = OptionHistoricalDataClient(
            api_key=os.environ.get("APCA_API_KEY_ID"),
            secret_key=os.environ.get("APCA_API_SECRET_KEY")
        )
        call_symbol_short = make_option_symbol(symbol, expiry_short, strike, 'C')
        call_symbol_long = make_option_symbol(symbol, expiry_long, strike, 'C')
        req = OptionLatestQuoteRequest(symbol_or_symbols=[call_symbol_short, call_symbol_long])
        quote_resp = options_client.get_option_latest_quote(req)
        quote_short = quote_resp.get(call_symbol_short)
        quote_long = quote_resp.get(call_symbol_long)
        if not quote_short or not quote_long:
            return None
        short_bid = quote_short.bid_price
        short_ask = quote_short.ask_price
        long_bid = quote_long.bid_price
        long_ask = quote_long.ask_price
        if None in (short_bid, short_ask, long_bid, long_ask):
            return None
        short_mid = (short_bid + short_ask) / 2
        long_mid = (long_bid + long_ask) / 2
        return float(long_mid - short_mid)
    except Exception as e:
        print(f"Error fetching Alpaca spread mid price: {e}")
        return None


def wait_for_fill(client, order_id, timeout=30, interval=1):
    """
    Poll an order until it is fully filled or timeout expires. Returns the filled order.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        ord = client.get_order_by_id(order_id)
        # Accept partial fills immediately
        if float(getattr(ord, 'filled_qty', 0) or 0) > 0:
            filled_qty_val = float(getattr(ord, 'filled_qty', 0) or 0)
            print(f"wait_for_fill: Order {order_id} filled with qty {filled_qty_val}")
            return ord
        time.sleep(interval)
    raise TimeoutError(f"Order {order_id} not filled in {timeout}s")


def monitor_fill_async(client, order, on_filled, timeout=30, interval=1):
    """
    Start a daemon thread to wait for fill and call on_filled callback when done.
    """
    def _poll():
        try:
            filled = wait_for_fill(client, order.id, timeout=timeout, interval=interval)
            on_filled(filled)
        except Exception as e:
            print(f"Fill monitor error for {order.id}: {e}")
    # spawn and return the thread object for external join
    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return t


def get_spread_quotes(short_symbol, long_symbol):
    """
    Return bid and ask prices for both option legs.
    """
    options_client = OptionHistoricalDataClient(
        api_key=API_KEY,
        secret_key=API_SECRET
    )
    req = OptionLatestQuoteRequest(symbol_or_symbols=[short_symbol, long_symbol])
    quote_resp = options_client.get_option_latest_quote(req)
    qs = quote_resp.get(short_symbol)
    ql = quote_resp.get(long_symbol)
    if not qs or not ql or None in (qs.bid_price, qs.ask_price, ql.bid_price, ql.ask_price):
        raise RuntimeError(f"Could not fetch bid/ask for {short_symbol} or {long_symbol}")
    return qs.bid_price, qs.ask_price, ql.bid_price, ql.ask_price 


def get_single_option_quotes(symbol: str):
    """
    Return bid and ask prices for a single option leg.
    Raises RuntimeError if quotes are not available.
    """
    client = init_alpaca_client() # Assuming init_alpaca_client is defined and returns a valid client or handles errors
    if not client:
        raise RuntimeError("Failed to initialize Alpaca client for get_single_option_quotes")

    options_client = OptionHistoricalDataClient(
        api_key=API_KEY, # Assuming API_KEY is globally defined
        secret_key=API_SECRET # Assuming API_SECRET is globally defined
    )
    req = OptionLatestQuoteRequest(symbol_or_symbols=[symbol])
    quote_resp = options_client.get_option_latest_quote(req)
    
    q = quote_resp.get(symbol)
    
    if not q or q.bid_price is None or q.ask_price is None:
        raise RuntimeError(f"Could not fetch valid bid/ask for {symbol}")
    return q.bid_price, q.ask_price


def close_single_option_leg_order(symbol: str, quantity: int, position_intent: PositionIntent):
    """
    Close a single option leg using a creeping limit order.
    position_intent should be PositionIntent.SELL_TO_CLOSE (for a long leg) or PositionIntent.BUY_TO_CLOSE (for a short leg).
    """
    client = init_alpaca_client()
    if not client:
        print(f"Failed to initialize Alpaca client for closing single leg {symbol}.")
        return None

    try:
        initial_bid, initial_ask = get_single_option_quotes(symbol)
        print(f"Initial quotes for single leg {symbol}: Bid={initial_bid}, Ask={initial_ask}")

        side = None
        if position_intent == PositionIntent.SELL_TO_CLOSE:
            side = OrderSide.SELL
            # Start trying to sell at the ask, creep down towards bid
            price_to_chase = initial_ask
            price_limit = initial_bid # Don't go below bid
            step_direction = -1
        elif position_intent == PositionIntent.BUY_TO_CLOSE:
            side = OrderSide.BUY
            # Start trying to buy at the bid, creep up towards ask
            price_to_chase = initial_bid
            price_limit = initial_ask # Don't go above ask
            step_direction = 1
        else:
            print(f"Invalid position_intent '{position_intent}' for close_single_option_leg_order.")
            return None

        spread = initial_ask - initial_bid
        step = max(spread / 10, 0.01) # Or a fixed $0.01, or a fraction of spread
        
        remaining_qty = quantity
        last_order_details = None
        total_filled_qty = 0
        total_filled_value = 0.0 # For calculating cumulative average price

        print(f"Starting single leg close for {symbol}: {quantity} qty, intent {position_intent}, side {side}, initial price {price_to_chase}, limit {price_limit}, step {step*step_direction:.2f}")

        while remaining_qty > 0:
            current_limit_price = round(price_to_chase, 2)
            
            # Check if we've crossed the price limit
            if (step_direction == 1 and current_limit_price > price_limit) or \
               (step_direction == -1 and current_limit_price < price_limit):
                print(f"Stopping chase for {symbol}: current price {current_limit_price} crossed limit {price_limit}.")
                break

            req = LimitOrderRequest(
                symbol=symbol,
                qty=remaining_qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                limit_price=current_limit_price,
                order_class=OrderClass.SIMPLE, 
                position_intent=position_intent
            )
            
            submitted_order = None
            try:
                submitted_order = client.submit_order(req)
                print(f"Placed DAY single leg closing order for {symbol}: {remaining_qty} qty @ limit ${current_limit_price}. Order ID: {submitted_order.id}")
                
                # Wait for fill or partial fill
                filled_details = wait_for_fill(client, submitted_order.id, timeout=30) # Using existing wait_for_fill
                
                filled_qty_this_order = int(float(getattr(filled_details, 'filled_qty', 0) or 0))
                
                if filled_qty_this_order > 0:
                    avg_price_this_fill = float(getattr(filled_details, 'filled_avg_price', 0) or 0)
                    commission_this_fill = getattr(filled_details, 'commission', 0) or 0

                    total_filled_qty += filled_qty_this_order
                    total_filled_value += avg_price_this_fill * filled_qty_this_order
                    # Note: commission handling might need to be summed up if multiple partial fills occur.
                    # For now, last_order_details will hold the commission of the last fill.
                    
                    print(f"Order {submitted_order.id} for {symbol}: Filled {filled_qty_this_order} at ${avg_price_this_fill}.")
                    remaining_qty -= filled_qty_this_order
                    last_order_details = filled_details # Store the latest filled order details

                if remaining_qty <= 0:
                    print(f"Entire single leg order for {symbol} ({quantity} contracts) filled.")
                    break 
                
                # If partially filled but not fully cancelled, cancel remainder before chasing at new price
                if filled_qty_this_order < remaining_qty and filled_details.status != OrderStatus.CANCELED:
                    try:
                        client.cancel_order_by_id(submitted_order.id)
                        print(f"Cancelled remaining part of single leg order {submitted_order.id} for {symbol} after partial fill.")
                    except APIError as e_cancel:
                        if e_cancel.status_code != 422: raise # Re-raise if not 'already uncancelable'
                        else: print(f"Order {submitted_order.id} not cancelable (likely fully filled/expired); ignoring 422 on cancel.")
            
            except TimeoutError:
                print(f"Single leg Order {submitted_order.id if submitted_order else 'N/A'} for {symbol} timed out. Cancelling if exists.")
                if submitted_order:
                    try:
                        client.cancel_order_by_id(submitted_order.id)
                    except APIError as e_timeout_cancel:
                        if e_timeout_cancel.status_code != 422: raise
                        else: print(f"Order {submitted_order.id} not cancelable on timeout; ignoring 422.")
            except Exception as e_order_loop:
                print(f"Error during single leg order submission/fill loop for {symbol}: {e_order_loop}")
                break # Exit loop on other errors

            price_to_chase += (step * step_direction)
            if remaining_qty > 0 : time.sleep(1) # Small delay before next chase attempt if not fully filled

        if total_filled_qty > 0 and last_order_details:
            # Create a summary object similar to what place_calendar_spread_order returns
            # or ensure last_order_details (which is an Order object) has cumulative info if needed.
            # For simplicity, if there were partial fills, wait_for_fill returns the Order object
            # which should have cumulative filled_qty and filled_avg_price for that specific order ID.
            # If multiple orders were placed (not in this simpler loop), we'd need to sum.
            # Here, we are modifying one order or placing new ones if the previous was fully done or cancelled.
            # The current wait_for_fill might return an order that's only partially filled.
            # The callback will use the properties of the final 'last_order_details'.
            
            # If there were multiple partial fills from *different* order submissions in a more complex loop,
            # we'd need to calculate a true cumulative average price and total commission.
            # This loop places one order at a time and waits for it.
            # So, `last_order_details` should reflect the state of the last *successful* fill.
            
            # To ensure filled_avg_price and filled_qty are cumulative for the *attempt* if it involved multiple fills on ONE order_id:
            # This is generally handled by Alpaca's Order object returned by wait_for_fill.
            # If we had to place multiple distinct orders, we would need this:
            # setattr(last_order_details, 'filled_avg_price', str(total_filled_value / total_filled_qty))
            # setattr(last_order_details, 'filled_qty', str(total_filled_qty))
            # setattr(last_order_details, 'commission', total_commission_so_far) -> would need to sum commissions
            return last_order_details # This is an Alpaca Order object
        else:
            print(f"Could not fill any quantity for single leg {symbol} within price limits.")
            return None

    except RuntimeError as e_quotes: # From get_single_option_quotes
        print(f"Error getting quotes for single leg {symbol}: {e_quotes}")
        return None
    except Exception as e:
        print(f"Error closing single option leg order for {symbol}: {e}")
        return None 
