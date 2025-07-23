# delta_hedger/market/polling_data_manager.py

import asyncio
import time
import logging
from typing import Optional
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest
from config import (
    API_KEY, API_SECRET, HEDGING_ASSET,
    PRICE_CHANGE_THRESHOLD, HEARTBEAT_TRIGGER_SECONDS
)
from utils.parsing import parse_option_symbol
from .us_treasury_yield_curve import get_risk_free_rate
from .dividends import get_dividend_yield
from datetime import datetime

logger = logging.getLogger(__name__)

class PollingMarketDataManager:
    """
    A market data manager that uses polling instead of websocket streaming.
    
    This avoids connection limit issues with Alpaca's paper trading environment
    by making periodic API calls to get the latest quotes instead of maintaining
    persistent websocket connections.
    """

    def __init__(self, trigger_queue: asyncio.Queue, call_option_symbol: str, put_option_symbol: str, hedging_asset: Optional[str] = None):
        """
        Initializes the PollingMarketDataManager.

        Args:
            trigger_queue: An asyncio.Queue used to send trigger signals to the DeltaEngine.
            call_option_symbol: The symbol for the call option of the straddle.
            put_option_symbol: The symbol for the put option of the straddle.
            hedging_asset: The ticker symbol for the underlying asset (defaults to config value).
        """
        self.trigger_queue = trigger_queue
        self.call_option_symbol = call_option_symbol
        self.put_option_symbol = put_option_symbol
        self.hedging_asset = hedging_asset or HEDGING_ASSET

        # Parse option symbols to extract expiry and strike, which are needed for pricing.
        _, _, self.call_option_expiry, self.call_option_strike = parse_option_symbol(call_option_symbol)
        _, _, self.put_option_expiry, self.put_option_strike = parse_option_symbol(put_option_symbol)

        # --- Live Market State ---
        # These attributes hold the latest mid-price for each instrument.
        self.stock_price: float = 0.0
        self.call_option_price: float = 0.0
        self.put_option_price: float = 0.0

        # --- Triggering Logic State ---
        self.last_trigger_time: float = 0.0
        # Stores the stock price at the time of the last trigger to calculate the price change.
        self._last_checked_stock_price: float = 0.0
        # An exponential moving average of the stock's bid-ask spread, used for quote filtering.
        self._spread_ema: float | None = None

        # --- Options Pricing Parameters ---
        # On initialization, fetch the key inputs required for the QuantLib pricing model.
        days_to_expiry = (self.call_option_expiry - datetime.now().date()).days
        # Fetches the risk-free rate from the US Treasury yield curve for the given expiry.
        self.risk_free_rate = get_risk_free_rate(days_to_expiry)
        # Fetches the dividend yield for the underlying asset.
        self.dividend_yield = get_dividend_yield()

        # Validate API credentials
        if not API_KEY or not API_SECRET:
            raise ValueError("API_KEY and API_SECRET must be set in environment variables")
        
        # Create historical data clients for polling
        self.stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)
        self.option_client = OptionHistoricalDataClient(API_KEY, API_SECRET)
        
        # Polling configuration
        self.poll_interval = 1.0  # Poll every 1 second
        self._running = False

    async def _check_and_trigger(self):
        """
        Checks if the conditions for a delta recalculation are met and, if so,
        sends a trigger signal to the DeltaEngine.

        A trigger is sent if either of two conditions is true:
        1. The price of the underlying asset has moved more than PRICE_CHANGE_THRESHOLD.
        2. A certain amount of time (HEARTBEAT_TRIGGER_SECONDS) has passed since the
           last trigger, ensuring periodic recalculation even in a quiet market.
        """
        now = time.time()

        # Avoid triggering on the very first price update.
        if self._last_checked_stock_price == 0.0:
            if self.stock_price != 0.0:
                self._last_checked_stock_price = self.stock_price
            return

        price_change = abs(self.stock_price - self._last_checked_stock_price)

        # Condition 1: Price movement threshold is breached.
        if price_change >= PRICE_CHANGE_THRESHOLD:
            self._send_trigger(now)
            self._last_checked_stock_price = self.stock_price

        # Condition 2: Heartbeat interval is exceeded.
        elif now - self.last_trigger_time > HEARTBEAT_TRIGGER_SECONDS:
            logger.info("Heartbeat trigger: Forcing delta recalculation.")
            self._send_trigger(now)

    def _send_trigger(self, trigger_time: float):
        """
        Places a 'CALCULATE_DELTA' message onto the trigger queue.

        This method uses `put_nowait` to avoid blocking. If the queue is full
        (because the DeltaEngine is still working on a previous request), this
        call will simply be skipped. This is intentional, as it means the system
        will only ever process the most recent trigger. It also ensures all necessary
        market data has been received before sending a trigger.
        """
        if self.stock_price == 0.0 or self.call_option_price == 0.0 or self.put_option_price == 0.0:
            logger.warning("Skipping trigger: Market data is not yet complete.")
            return

        try:
            self.trigger_queue.put_nowait('CALCULATE_DELTA')
            self.last_trigger_time = trigger_time
        except asyncio.QueueFull:
            # It's okay to pass here. It just means the engine is busy,
            # and we're dropping this trigger in favor of a future one.
            pass

    async def _poll_stock_data(self):
        """Poll for latest stock quote data."""
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=[self.hedging_asset])
            quotes = self.stock_client.get_stock_latest_quote(request)
            
            if self.hedging_asset in quotes:
                quote = quotes[self.hedging_asset]
                await self._handle_stock_quote(quote)
        except Exception as e:
            logger.warning(f"Error polling stock data: {e}")

    async def _poll_option_data(self):
        """Poll for latest option quote data."""
        try:
            request = OptionLatestQuoteRequest(
                symbol_or_symbols=[self.call_option_symbol, self.put_option_symbol]
            )
            quotes = self.option_client.get_option_latest_quote(request)
            
            for symbol, quote in quotes.items():
                await self._handle_option_quote_data(symbol, quote)
        except Exception as e:
            logger.warning(f"Error polling option data: {e}")

    async def _handle_stock_quote(self, quote):
        """
        Process stock quote data.
        """
        if not hasattr(quote, 'ask_price') or not hasattr(quote, 'bid_price'):
            return
            
        spread = quote.ask_price - quote.bid_price
        mid_price = (quote.bid_price + quote.ask_price) / 2

        # A quote is considered valid if its spread is not excessively wide
        # compared to the recent moving average of the spread.
        if mid_price > 0 and self._spread_ema is not None and spread <= 1.5 * self._spread_ema:
            self.stock_price = mid_price
            await self._check_and_trigger()

        # Update the spread EMA. We only use "good" quotes (spread < $0.50) to
        # prevent a single bad print from corrupting the EMA.
        if self._spread_ema is None and spread < 0.5:
            self._spread_ema = spread  # Initialize the EMA
        elif spread < 0.5 and self._spread_ema is not None:
            # Standard EMA formula
            self._spread_ema = 0.9 * self._spread_ema + 0.1 * spread

    async def _handle_option_quote_data(self, symbol: str, quote):
        """
        Process option quote data.
        """
        if not hasattr(quote, 'ask_price') or not hasattr(quote, 'bid_price'):
            return
            
        mid_price = (quote.bid_price + quote.ask_price) / 2
        if mid_price > 0:
            if symbol == self.call_option_symbol:
                self.call_option_price = mid_price
            elif symbol == self.put_option_symbol:
                self.put_option_price = mid_price

    async def run(self):
        """The main entry point for the PollingMarketDataManager task."""
        self._running = True
        logger.info("Polling market data manager starting...")
        
        while self._running:
            try:
                # Poll both stock and option data
                await asyncio.gather(
                    self._poll_stock_data(),
                    self._poll_option_data(),
                    return_exceptions=True
                )
                
                # Wait for the next poll interval
                await asyncio.sleep(self.poll_interval)
                
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                await asyncio.sleep(self.poll_interval)

    def stop(self):
        """Stop the polling loop."""
        self._running = False
        logger.info("Polling market data manager stopping...")
