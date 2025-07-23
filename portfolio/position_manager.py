"""
Manages the portfolio's state, executes trades, and handles all interactions
with the Alpaca Trading API.

This class is the execution layer of the trading bot. It is responsible for:
-   Initializing the position at startup, either by clearing all existing
    positions ('init' mode) or by synchronizing with them ('resume' mode).
-   Listening for trade commands from the TradingStrategy via a queue.
-   Translating these commands into actual market orders submitted to Alpaca.
-   Subscribing to a real-time stream of trade fill confirmations from Alpaca.
-   Maintaining the authoritative state of the portfolio, including the number
    of shares owned, pending trades, and the symbols of the option contracts.
-   Calculating realized P&L from scalping trades using a FIFO queue.
-   Logging every trade to a file for later analysis.
-   Using a lock to ensure that only one hedge trade is processed at a time,
    preventing race conditions and duplicate orders.
"""

from typing import Optional
import asyncio
import time
import logging
import os
import json
from datetime import datetime
from clients.user_agent_mixin import TradingClientSigned
from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.stream import TradingStream
from collections import deque
from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING, HEDGING_ASSET, 
    TRADE_COMMAND_TTL_SECONDS, INITIALIZATION_MODE, STRATEGY_MULTIPLIER, TRADE_LOG_DIR
)
from utils.parsing import parse_option_symbol

# Configure logging for this module
logger = logging.getLogger(__name__)


class PositionManager:
    """Manages portfolio state, executes trades, and handles fill confirmations."""
    def __init__(
        self,
        trade_action_queue: asyncio.Queue,
        shutdown_event: asyncio.Event,
        initialization_mode: Optional[str] = None,
        hedging_asset: Optional[str] = None
    ):
        """
        Initializes the PositionManager.

        Args:
            trade_action_queue: The input queue for receiving trade commands from the strategy.
            shutdown_event: The event to signal graceful shutdown.
            initialization_mode: Override for initialization mode ('init' or 'resume').
                                If None, uses the config value.
            hedging_asset: Override for the ticker symbol to hedge.
                          If None, uses the config value.
        """
        self.trade_action_queue = trade_action_queue
        self.shutdown_event = shutdown_event
        self.initialization_mode = initialization_mode or INITIALIZATION_MODE
        self.hedging_asset = hedging_asset or HEDGING_ASSET
        self.trading_client = TradingClientSigned(API_KEY, API_SECRET, paper=IS_PAPER_TRADING)
        if not isinstance(API_KEY, str) or not API_KEY or not isinstance(API_SECRET, str) or not API_SECRET:
            raise ValueError("API_KEY and API_SECRET must be non-empty strings.")
        self.trade_stream = TradingStream(API_KEY, API_SECRET, paper=IS_PAPER_TRADING)
        
        # --- Critical State ---
        # The number of shares of the underlying asset currently held. Can be negative for short positions.
        self.shares_owned: int = 0
        # The number of shares in flight (orders submitted but not yet filled).
        self.pending_shares_change: int = 0
        # The symbols for the specific call and put options that make up our straddle.
        self.call_option_symbol: str | None = None
        self.put_option_symbol: str | None = None

        # --- P&L Tracking State ---
        # The cumulative profit or loss from completed scalp trades.
        self.realized_scalp_pnl = 0.0
        # A FIFO (First-In, First-Out) queue to track individual hedge trades. This is
        # essential for correctly calculating P&L when trades partially close positions.
        self.hedge_positions = deque()
        
        # --- Trade Logging ---
        # Sets up a directory and a unique file for logging every trade execution.
        log_dir = TRADE_LOG_DIR
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.trade_log_file = os.path.join(log_dir, f"trades_{timestamp}.jsonl")
        logger.info(f"Logging trades to {self.trade_log_file}")

        # A concurrency lock to ensure that only one trade is being executed at a time.
        # It is initialized as "set" (unlocked).
        self._trade_lock = asyncio.Event()
        self._trade_lock.set()
        
        # A temporary holder for the second part of a two-leg trade, used when a single
        # hedge command requires crossing from a long to a short position or vice versa.
        self._pending_second_leg = None

    async def initialize_position(self):
        """
        Initializes the position based on the configured mode. This is one of the
        first actions taken when the application starts.
        """
        logger.info(f"Initializing in '{self.initialization_mode}' mode.")

        # Always cancel any lingering open orders to ensure a clean start.
        try:
            self.trading_client.cancel_orders()
            logger.info(f"Canceled any open orders.")
        except Exception as e:
            logger.warning(f"Could not cancel open orders: {e}")


        if self.initialization_mode == 'init':
            # In 'init' mode, we liquidate everything to start from a known flat state.
            await self._close_all_positions()
        elif self.initialization_mode == 'resume':
            # In 'resume' mode, we synchronize with existing positions.
            await self._resume_position()


    async def _close_all_positions(self):
        """
        The handler for 'init' mode. Finds and liquidates all positions related
        to the hedging asset (both the stock and any of its options).
        """
        logger.info("Closing all positions for a fresh start...")
        try:
            # Get all current positions from Alpaca.
            positions = self.trading_client.get_all_positions()
            closed_positions = []
            # Iterate through all positions to find the ones related to our strategy.
            for pos in positions:
                # If it's an option, parse the symbol to check the underlying.
                if pos.asset_class == AssetClass.US_OPTION:
                    underlying, _, _, _ = parse_option_symbol(pos.symbol)
                    if underlying == self.hedging_asset:
                        self.trading_client.close_position(pos.symbol)
                        closed_positions.append(pos.symbol)
                # If it's a stock, check if it's our hedging asset.
                elif pos.asset_class == AssetClass.US_EQUITY:
                    if pos.symbol == self.hedging_asset:
                        self.trading_client.close_position(pos.symbol)
                        closed_positions.append(pos.symbol) 

            logger.info(f"Closed positions: {closed_positions}")
        except Exception as e:
            logger.error(f"Error while closing all positions: {e}")
        
        # Reset our internal state to flat.
        self.shares_owned = 0
        logger.info("All positions closed. Starting at 0 shares.")


    async def _resume_position(self):
        """
        The handler for 'resume' mode. Queries Alpaca for existing positions and
        performs validation to ensure the state is suitable for the strategy to take over.
        """
        logger.info(f"Resuming positions for {self.hedging_asset}...")
        self.shares_owned = 0

        try:
            positions = self.trading_client.get_all_positions()

            # First, find the stock position for the hedging asset.
            for pos in positions:
                if pos.asset_class == AssetClass.US_EQUITY and pos.symbol == self.hedging_asset:
                    self.shares_owned = int(float(pos.qty))
                    logger.info(f"Resumed stock position: {self.shares_owned} shares of {self.hedging_asset}.")
                    break  # Found it, no need to continue looping.

            # Now, find all option positions for the underlying asset.
            option_positions = []
            for pos in positions:
                if pos.asset_class == AssetClass.US_OPTION:
                    underlying, option_type, _, _ = parse_option_symbol(pos.symbol)
                    if underlying == self.hedging_asset:
                        option_positions.append((pos.symbol, option_type, int(float(pos.qty))))

            if not option_positions:
                raise ValueError("No option positions found for the hedging asset. Must have one call and one put.")

            # --- Critical Validation Checks ---
            # Ensure the existing position matches the strategy's expectations.
            calls = [(p, q) for p, t, q in option_positions if t == 'C']
            puts = [(p, q) for p, t, q in option_positions if t == 'P']

            if len(calls) != 1 or len(puts) != 1:
                raise ValueError(
                    f"Expected exactly 1 call and 1 put for {self.hedging_asset}, "
                    f"but found {len(calls)} calls and {len(puts)} puts."
                )

            call_symbol, call_qty = calls[0]
            put_symbol, put_qty = puts[0]

            if call_qty != put_qty:
                raise ValueError(
                    f"Mismatched option quantities. Call qty: {call_qty}, Put qty: {put_qty}."
                )
            
            if call_qty != STRATEGY_MULTIPLIER:
                raise ValueError(
                    f"Position quantity ({call_qty}) does not match STRATEGY_MULTIPLIER ({STRATEGY_MULTIPLIER})."
                )

            self.call_option_symbol = call_symbol
            self.put_option_symbol = put_symbol

            logger.info(f"Resumed option positions: Call={self.call_option_symbol}, Put={self.put_option_symbol}, Qty={call_qty}")

        except Exception as e:
            logger.error(f"Error resuming positions: {e}")
            # In case of any error (including no positions found), we start fresh
            self.shares_owned = 0
            self.call_option_symbol = None
            self.put_option_symbol = None
            logger.info("Could not resume positions.")
            # Re-raise the exception to halt initialization if resume fails validation. 
            raise e

    async def _handle_trade_fill(self, data):
        """The callback handler for trade update events."""
        logger.info(f"Trade update received: {data.event}")
        
        if data.event in ['fill', 'partial_fill', 'canceled', 'rejected']:
            if data.event == 'partial_fill':
                logger.warning("Partial fill received. Position state may be temporarily inconsistent.")
                return

            order = data.order
            fill_qty = int(order.filled_qty)
            side = order.side
            fill_price = float(order.filled_avg_price)

            # --- Only process trades for the hedging asset ---
            if data.order.symbol != self.hedging_asset:
                # This is likely an option trade from initialization.
                # We log it but don't use it to update hedging state.
                logger.info(f"Received fill for non-hedging asset {data.order.symbol}. Logging and ignoring for state.")
                # --- Log Trade to File ---
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "symbol": order.symbol,
                    "side": side.value,
                    "quantity": fill_qty,
                    "fill_price": fill_price,
                    "pnl": 0
                }
                with open(self.trade_log_file, 'a') as f:
                    f.write(json.dumps(log_entry) + '\n')

                return
            
            # --- FIFO P&L Calculation ---
            pnl_this_trade = 0.0
            qty_to_match = fill_qty

            if side == OrderSide.BUY:
                # This BUY is closing a previous SHORT position
                while qty_to_match > 0 and self.hedge_positions and self.hedge_positions[0]['side'] == OrderSide.SELL:
                    oldest_short = self.hedge_positions.popleft()
                    match_qty = min(qty_to_match, oldest_short['qty'])
                    
                    pnl_this_trade += (oldest_short['price'] - fill_price) * match_qty
                    
                    qty_to_match -= match_qty
                    oldest_short['qty'] -= match_qty
                    
                    if oldest_short['qty'] > 0:
                        self.hedge_positions.appendleft(oldest_short)

            elif side == OrderSide.SELL:
                # This SELL is closing a previous LONG position
                while qty_to_match > 0 and self.hedge_positions and self.hedge_positions[0]['side'] == OrderSide.BUY:
                    oldest_long = self.hedge_positions.popleft()
                    match_qty = min(qty_to_match, oldest_long['qty'])

                    pnl_this_trade += (fill_price - oldest_long['price']) * match_qty

                    qty_to_match -= match_qty
                    oldest_long['qty'] -= match_qty

                    if oldest_long['qty'] > 0:
                        self.hedge_positions.appendleft(oldest_long)
            
            # Add any remaining quantity as a new open position
            if qty_to_match > 0:
                self.hedge_positions.append({'price': fill_price, 'qty': qty_to_match, 'side': side})
            
            if pnl_this_trade != 0.0:
                self.realized_scalp_pnl += pnl_this_trade
                logger.warning(
                    f"Realized P&L from this scalp: ${pnl_this_trade:+.2f}. "
                    f"Cumulative Scalp P&L: ${self.realized_scalp_pnl:+.2f}"
                )

            # --- Log Trade to File ---
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "symbol": order.symbol,
                "side": side.value,
                "quantity": fill_qty,
                "fill_price": fill_price,
                "pnl": pnl_this_trade
            }
            with open(self.trade_log_file, 'a') as f:
                f.write(json.dumps(log_entry) + '\n')
            
            # --- State Reconciliation ---
            # Update the authoritative state based on the fill.
            side_multiplier = 1 if side == OrderSide.BUY else -1
            self.shares_owned += (fill_qty * side_multiplier)
            self.pending_shares_change -= (fill_qty * side_multiplier)
            
            logger.info(f"Order {data.event}. Position is now {self.shares_owned} shares.")

            # If there's a second leg waiting, execute it now.
            if self._pending_second_leg:
                logger.info("Executing second leg of two-part trade.")
                leg2 = self._pending_second_leg
                self._pending_second_leg = None
                await self._execute_trade(leg2['quantity'], leg2['side'])
            else:
                # If no second leg, the trade is complete. Release the lock.
                logger.info("Order is terminal. Releasing trade lock.")
                self._trade_lock.set()

    async def _execute_trade(self, quantity: int, side: OrderSide):
        """Submits a single trade and handles immediate errors."""
        try:
            order_request = MarketOrderRequest(
                symbol=self.hedging_asset,
                qty=abs(quantity),
                side=side,
                time_in_force=TimeInForce.DAY
            )
            self.trading_client.submit_order(order_data=order_request)
            # Update our pending state immediately upon submission.
            self.pending_shares_change += quantity
            logger.info(f"Submitted market order to {side.value} {abs(quantity)} {self.hedging_asset}. Pending change: {self.pending_shares_change}")
        except Exception as e:
            logger.error(f"Error submitting order: {e}")
            # If the trade fails, release the lock so the bot doesn't get stuck.
            self._trade_lock.set()

    async def trade_executor_loop(self):
        """The main loop for the trade execution task."""
        logger.info("Trade executor started.")
        while not self.shutdown_event.is_set():
            # This task waits for the lock to be free before getting a command.
            await self._trade_lock.wait()
            
            # Get the next trade command from the strategy.
            command = await self.trade_action_queue.get()
            # Immediately acquire the lock after getting a command.
            self._trade_lock.clear()

            # Check if the command is stale to avoid acting on old data.
            if time.time() - command["timestamp"] > TRADE_COMMAND_TTL_SECONDS:
                logger.warning(f"Discarding stale trade command: {command}")
                self.trade_action_queue.task_done()
                self._trade_lock.set()
                continue

            quantity = command['quantity']
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL

            # --- Long/Short Transition Logic ---
            # This complex logic handles trades that cross zero (e.g., from long to short).
            # It splits one logical trade into two orders.
            if side == OrderSide.SELL and self.shares_owned > 0 and abs(quantity) > self.shares_owned:
                # Case: We are long, but need to sell more than we own, flipping short.
                # Leg 1: Sell to flatten the current long position.
                qty1 = -self.shares_owned
                # Leg 2: The remaining quantity to sell, opening a new short position.
                qty2 = quantity - qty1
                self._pending_second_leg = {'quantity': qty2, 'side': OrderSide.SELL}
                logger.warning(f"Trade crosses zero. Leg 1: Sell {abs(qty1)}. Pending Leg 2: Sell {abs(qty2)}.")
                await self._execute_trade(qty1, OrderSide.SELL)
            elif side == OrderSide.BUY and self.shares_owned < 0 and quantity > abs(self.shares_owned):
                # Case: We are short, but need to buy more than we're short, flipping long.
                # Leg 1: Buy to cover the entire short position.
                qty1 = abs(self.shares_owned)
                # Leg 2: The remaining quantity to buy, opening a new long position.
                qty2 = quantity - qty1
                self._pending_second_leg = {'quantity': qty2, 'side': OrderSide.BUY}
                logger.warning(f"Trade crosses zero. Leg 1: Buy {qty1}. Pending Leg 2: Buy {qty2}.")
                await self._execute_trade(qty1, OrderSide.BUY)
            else:
                # Standard trade that doesn't cross zero. No splitting is required.
                await self._execute_trade(quantity, side)
            
            # Acknowledge that the command has been processed.
            self.trade_action_queue.task_done()

    async def fill_listener_loop(self):
        """Listens to the trading stream for fill events."""
        logger.info("Fill Listener started.")
        # Subscribes the handler function to the trade update stream.
        self.trade_stream.subscribe_trade_updates(self._handle_trade_fill)
        logger.info("Fill Listener subscribed to trade updates.")
        # The SDK handles the reconnect logic internally. We just need to run it.
        await self.trade_stream._run_forever()

