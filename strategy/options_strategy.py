"""
Handles the initial selection and opening of the options straddle.

This module contains the logic for the "init" mode of the application. Its primary
purpose is to programmatically scan the options market for the underlying asset,
evaluate all potential straddles against a set of criteria, and select the most
cost-effective one to purchase. A "cost-effective" straddle, in the context of
gamma scalping, is one that offers the highest gamma (potential for profit from
volatility) for the lowest cost (theta decay and transaction costs).
"""

import logging
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.enums import AssetStatus, ContractType, AssetClass, OrderSide, TimeInForce
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data import StockHistoricalDataClient
from config import (
    API_KEY, API_SECRET, HEDGING_ASSET,
    MIN_EXPIRATION_DAYS, MAX_EXPIRATION_DAYS, MIN_OPEN_INTEREST,
    STRATEGY_MULTIPLIER, THETA_WEIGHT, OPTIONS_CONTRACT_MULTIPLIER
)
from portfolio.position_manager import PositionManager
from datetime import datetime, timedelta
from engine.delta_engine import calculate_single_option_greeks, calculate_implied_volatility
from market.us_treasury_yield_curve import get_risk_free_rate
from market.dividends import get_dividend_yield

# Configure logging for this module
logger = logging.getLogger(__name__)

async def _fetch_all_contracts(trading_client: TradingClient, request_params: GetOptionContractsRequest):
    """
    A helper function to fetch all available option contracts by handling API pagination.
    The Alpaca API limits the number of contracts returned in a single call, so we
    must loop using the `next_page_token` until all contracts are retrieved.
    """
    all_contracts = []
    page_token = None
    
    while True:
        request_params.page_token = page_token
        response = trading_client.get_option_contracts(request_params)
        
        if response.option_contracts:
            all_contracts.extend(response.option_contracts)
        
        page_token = response.next_page_token
        if not page_token:
            break
            
    return all_contracts


async def open_initial_straddle(position_manager: PositionManager):
    """
    Analyzes the options market to find and purchase the most favorable straddle.

    This is the main function for the 'init' mode. It follows a systematic process
    to ensure the strategy starts with the best possible position.
    """
    logger.info("--- Beginning search for an initial straddle position ---")
    
    # Initialize API clients.
    trading_client = position_manager.trading_client
    stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)
    option_client = OptionHistoricalDataClient(API_KEY, API_SECRET)

    # --- Step 1: Get the current price of the underlying asset. ---
    # The mid-price is used to get a fair estimate of the current market value.
    hedging_asset = position_manager.hedging_asset
    try:
        request_params = StockLatestQuoteRequest(symbol_or_symbols=hedging_asset)
        latest_quote = stock_client.get_stock_latest_quote(request_params)
        underlying_price = (latest_quote[hedging_asset].ask_price + latest_quote[hedging_asset].bid_price) / 2
        logger.info(f"Current price of {hedging_asset} is ${underlying_price:.2f}")
    except Exception as e:
        logger.error(f"Failed to get current price for {hedging_asset}: {e}")
        return
    
    # --- Step 2: Fetch all active option contracts within our criteria. ---
    logger.info(f"Fetching options expiring between {MIN_EXPIRATION_DAYS} and {MAX_EXPIRATION_DAYS} days.")
    # Set up the parameters for the API request for option contracts.
    request_params = GetOptionContractsRequest(
        underlying_symbols=[hedging_asset],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=(datetime.now() + timedelta(days=MIN_EXPIRATION_DAYS)).date(),
        expiration_date_lte=(datetime.now() + timedelta(days=MAX_EXPIRATION_DAYS)).date(),
        root_symbol=hedging_asset,
        type=ContractType.CALL
    )
    # Fetch all eligible call contracts.
    call_contracts = await _fetch_all_contracts(trading_client, request_params)
    # Modify the request to fetch puts.
    request_params.type = ContractType.PUT
    # Fetch all eligible put contracts.
    put_contracts = await _fetch_all_contracts(trading_client, request_params)

    # Filter by open interest to ensure we only consider liquid contracts.
    call_contracts = [contract for contract in call_contracts if contract.open_interest is not None and int(contract.open_interest) > MIN_OPEN_INTEREST]
    put_contracts = [contract for contract in put_contracts if contract.open_interest is not None and int(contract.open_interest) > MIN_OPEN_INTEREST]
    logger.info(f"Found {len(call_contracts)} eligible calls and {len(put_contracts)} eligible puts after filtering.")

    # --- Step 3: Group contracts by expiry and strike to find straddle pairs. ---
    # This data structure makes it easy to find a call and a put with the same expiry and strike.
    contracts_by_expiry = {}
    for contract in call_contracts + put_contracts:
        expiry = contract.expiration_date
        strike = contract.strike_price
        # Create nested dictionaries if they don't exist.
        if expiry not in contracts_by_expiry:
            contracts_by_expiry[expiry] = {}
        if strike not in contracts_by_expiry[expiry]:
            contracts_by_expiry[expiry][strike] = {}

        # Add the contract to the appropriate 'call' or 'put' key.
        if contract.type == ContractType.CALL:
            contracts_by_expiry[expiry][strike]['call'] = contract
        else:
            contracts_by_expiry[expiry][strike]['put'] = contract

    # --- Step 4: For each expiration, define a dynamic strike range and find candidates. ---
    # This is a more intelligent way to find "near-the-money" options than using a fixed percentage.
    # Get a single risk-free rate and dividend yield to use for all calculations in this function.
    approximate_risk_free_rate = get_risk_free_rate((MIN_EXPIRATION_DAYS + MAX_EXPIRATION_DAYS)/2)
    dividend_yield = get_dividend_yield()
    all_candidate_straddles = []
    for expiry, strikes in contracts_by_expiry.items():
        # Find strikes that have both a call and a put, making them a valid straddle.
        valid_straddle_strikes = [k for k, v in strikes.items() if 'call' in v and 'put' in v]
        
        # If no valid straddles are found for this expiry, skip to the next one.
        if not valid_straddle_strikes:
            logger.warning(f"No valid straddles found for {expiry}. Skipping this expiration.")
            continue
            
        # Find the at-the-money (ATM) strike to use for a baseline volatility calculation.
        atm_strike = min(valid_straddle_strikes, key=lambda k: abs(k - underlying_price))
        
        try:
            # --- Calculate Dynamic Strike Range ---
            # Get the market data for the ATM call option to calculate its implied volatility.
            atm_call_symbol = strikes[atm_strike]['call'].symbol
            snapshot_request = OptionSnapshotRequest(symbol_or_symbols=[atm_call_symbol])
            snapshot = option_client.get_option_snapshot(snapshot_request)[atm_call_symbol]
            atm_call_price = (snapshot.latest_quote.ask_price + snapshot.latest_quote.bid_price) / 2
            
            expiry_days = (expiry - datetime.now().date()).days
            
            # Calculate the implied volatility of the ATM option. This IV represents the
            # market's current expectation of future volatility for this expiration.
            baseline_iv = calculate_implied_volatility(
                atm_call_price, underlying_price, atm_strike, expiry_days, 'call', approximate_risk_free_rate, dividend_yield
            )
            
            # If IV calculation fails, we can't proceed with this expiration.
            if not (baseline_iv > 0): # Check for NaN or non-positive IV
                logger.warning(f"Could not calculate a valid baseline IV for ATM strike {atm_strike} on {expiry}. Skipping.")
                continue

            # Use the IV to calculate the 1-standard-deviation expected move of the stock.
            # This gives us a market-based range of relevant strikes to consider.
            time_to_expiry_years = expiry_days / 365.25
            expected_move = underlying_price * baseline_iv * math.sqrt(time_to_expiry_years)
            min_strike = underlying_price - expected_move
            max_strike = underlying_price + expected_move
            logger.info(f"For expiry {expiry}, baseline IV is {baseline_iv:.2%}. Expected move: ${expected_move:.2f}. Strike Range: {min_strike:.2f}-{max_strike:.2f}")

            # Collect all valid straddles within this dynamic, 1-std-dev range.
            for strike, contract_pair in strikes.items():
                if 'call' in contract_pair and 'put' in contract_pair and min_strike <= strike <= max_strike:
                    all_candidate_straddles.append({
                        'expiration': expiry,
                        'strike': strike,
                        'call': contract_pair['call'],
                        'put': contract_pair['put']
                    })

        except Exception as e:
            logger.error(f"Error processing expiration {expiry}: {e}")
            continue

    if not all_candidate_straddles:
        logger.warning("Could not find any suitable straddle pairs within the dynamic criteria.")
        return
    
    logger.info(f"Identified {len(all_candidate_straddles)} potential straddle candidates across all expiries. Now scoring them...")
    
    # --- Step 5: Score all candidate straddles to find the most favorable one. ---
    for straddle in all_candidate_straddles:
        try:
            call_symbol = straddle['call'].symbol
            put_symbol = straddle['put'].symbol
            
            # Get the latest mid-price for both the call and the put option using a real-time snapshot.
            snapshot_request = OptionSnapshotRequest(symbol_or_symbols=[call_symbol, put_symbol])
            snapshots = option_client.get_option_snapshot(snapshot_request)

            call_price = (snapshots[call_symbol].latest_quote.ask_price + snapshots[call_symbol].latest_quote.bid_price) / 2
            put_price = (snapshots[put_symbol].latest_quote.ask_price + snapshots[put_symbol].latest_quote.bid_price) / 2
            # The bid-ask spread is a proxy for the transaction cost of opening the position.
            call_spread = snapshots[call_symbol].latest_quote.ask_price - snapshots[call_symbol].latest_quote.bid_price
            put_spread = snapshots[put_symbol].latest_quote.ask_price - snapshots[put_symbol].latest_quote.bid_price
            
            expiry_days = (straddle['expiration'] - datetime.now().date()).days
            
            # Calculate the greeks (theta and gamma) for both options.
            call_greeks = calculate_single_option_greeks(
                call_price, underlying_price, straddle['strike'], expiry_days, 'call', approximate_risk_free_rate, dividend_yield, ['theta', 'gamma']
            )
            put_greeks = calculate_single_option_greeks(
                put_price, underlying_price, straddle['strike'], expiry_days, 'put', approximate_risk_free_rate, dividend_yield, ['theta', 'gamma']
            )
            
            call_theta = call_greeks['theta']
            call_gamma = call_greeks['gamma']
            put_theta = put_greeks['theta']
            put_gamma = put_greeks['gamma']
            
            # If the greeks calculation fails (returns NaN), we disqualify the straddle.
            if any(v != v for v in [call_theta, call_gamma, put_theta, put_gamma]):
                logger.warning(f"Greeks calculation resulted in NaN for {straddle['call'].symbol} or {straddle['put'].symbol}. Skipping.")
                straddle['score'] = float('inf')
                continue

            # The score represents the "cost per unit of gamma." A lower score is better.
            total_theta = call_theta + put_theta
            total_gamma = call_gamma + put_gamma
            # The total spread represents the cost to enter the position.
            total_spread = call_spread + put_spread
            
            # The scoring formula balances the daily cost of holding the position (theta)
            # and the cost of entering it (spread) against the potential profit engine (gamma).
            if total_gamma > 0:
                # We multiply the spread cost by the total number of shares to get a comparable
                # dollar value against the daily theta decay.
                straddle['score'] = (abs(total_theta) * THETA_WEIGHT + total_spread * STRATEGY_MULTIPLIER * OPTIONS_CONTRACT_MULTIPLIER ) / total_gamma
            else:
                # Avoid division by zero if gamma is not positive.
                straddle['score'] = float('inf')

        except Exception as e:
            logger.error(f"Failed to score straddle for expiry {straddle['expiration']}: {e}")
            straddle['score'] = float('inf')

    # --- Step 6: Select the straddle with the best (lowest) score. ---
    all_candidate_straddles.sort(key=lambda x: x.get('score', float('inf')))

    # If no straddles could be scored successfully, we cannot proceed.
    if not all_candidate_straddles or all_candidate_straddles[0].get('score') == float('inf'):
        logger.warning("No suitable straddles could be scored successfully.")
        return
    
    # The best straddle is the one with the lowest score at the top of the sorted list.
    chosen_straddle = all_candidate_straddles[0]
    call_symbol = chosen_straddle['call'].symbol
    put_symbol = chosen_straddle['put'].symbol
    
    logger.info(f"Best straddle found: Expiry {chosen_straddle['expiration']}, Strike {chosen_straddle['strike']}, Score: {chosen_straddle['score']:.4f}")
    logger.info(f"Call: {call_symbol}, Put: {put_symbol}")

    # --- Step 7: Submit market orders to open the position and save state. ---
    try:
        # Buy the call option.
        call_order_request = MarketOrderRequest(
            symbol=call_symbol,
            qty=STRATEGY_MULTIPLIER,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=call_order_request)
        logger.info(f"Submitted market order to BUY {STRATEGY_MULTIPLIER} {call_symbol}")

        # Buy the put option.
        put_order_request = MarketOrderRequest(
            symbol=put_symbol,
            qty=STRATEGY_MULTIPLIER,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=put_order_request)
        logger.info(f"Submitted market order to BUY {STRATEGY_MULTIPLIER} {put_symbol}")

        # Save the chosen option symbols to the PositionManager.
        # This informs the rest of the application which contracts to monitor and hedge.
        position_manager.call_option_symbol = call_symbol
        position_manager.put_option_symbol = put_symbol

    except Exception as e:
        logger.error(f"Failed to submit initial straddle orders: {e}") 