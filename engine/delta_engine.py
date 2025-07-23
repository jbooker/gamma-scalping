# delta_hedger/engine/delta_engine.py

"""
The computational core of the gamma scalping application.

This module is responsible for all the complex financial mathematics involved in
options pricing and risk management. It leverages the industry-standard QuantLib
library to perform these calculations.

The central concept is a two-step process:
1.  **Calculate Implied Volatility (IV):** Given the current market price of an
    option, we "back-solve" for the volatility that the market is pricing in.
    This is a crucial step because volatility is the most uncertain input in
    an options pricing model.
2.  **Calculate Greeks:** Using the freshly calculated IV, we then compute the
    option's "Greeks" (Delta, Gamma, Theta). These metrics are essential for
    managing the risk of the position.

For American-style options, which can be exercised at any time before expiration,
a simple Black-Scholes formula is insufficient. This engine therefore uses a
**Binomial Pricing Model** (Cox-Ross-Rubinstein), which is a numerical method
that correctly handles the early-exercise feature of American options.
"""

import asyncio
import random
import time
import logging
import QuantLib as ql
from typing import Any
from datetime import datetime
from typing import Tuple
from market.state import MarketDataManager

logger = logging.getLogger(__name__) 

def calculate_implied_volatility(
    option_market_price: float,
    stock_price: float,
    strike: float,
    expiry_days: int,
    option_type: str,  # 'call' or 'put'
    risk_free_rate: float,
    dividend_yield: float,
    n_steps: int = 100
) -> float:
    """
    Calculates the implied volatility for a single American option using a binomial tree.

    This function takes all the known market parameters and the option's current price,
    and then uses QuantLib's solver to find the volatility value that makes the
    theoretical price match the market price.

    Args:
        option_market_price: The current mid-price of the option.
        stock_price: The current mid-price of the underlying stock.
        strike: The strike price of the option.
        expiry_days: The number of calendar days until the option expires.
        option_type: 'call' or 'put'.
        risk_free_rate: The interpolated risk-free interest rate.
        dividend_yield: The annualized dividend yield of the stock.
        n_steps: The number of steps in the binomial tree model. More steps lead
                 to higher accuracy but longer computation time.

    Returns:
        The calculated implied volatility as a float, or NaN if the calculation fails.
    """
    ql.Settings.instance().evaluationDate = ql.Date.todaysDate()

    # print(option_market_price, stock_price, strike, expiry_days, option_type, risk_free_rate, dividend_yield, n_steps)
    
    if option_type.lower() == 'call':
        payoff = ql.PlainVanillaPayoff(ql.Option.Call, strike)
    else:
        payoff = ql.PlainVanillaPayoff(ql.Option.Put, strike)
        
    exercise = ql.AmericanExercise(ql.Date.todaysDate(), ql.Date.todaysDate() + expiry_days)
    option = ql.VanillaOption(payoff, exercise)
    
    spot_handle = ql.QuoteHandle(ql.SimpleQuote(stock_price))
    riskfree_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), risk_free_rate, ql.Actual365Fixed()))
    dividend_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), dividend_yield, ql.Actual365Fixed()))
    
    # We need a dummy volatility to create the process, but it will be ignored by the solver.
    volatility_handle = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(0, ql.TARGET(), 0.20, ql.Actual365Fixed()))
    bsm_process = ql.BlackScholesMertonProcess(spot_handle, dividend_handle, riskfree_handle, volatility_handle)
    
    engine = ql.BinomialVanillaEngine(bsm_process, "crr", n_steps)
    option.setPricingEngine(engine)
    
    # Accuracy of the solver
    accuracy = 0.0001
    max_iterations = 100
    min_vol, max_vol = 0.01, 4.0 # Search range for volatility
    
    try:
        iv = option.impliedVolatility(option_market_price, bsm_process, accuracy, max_iterations, min_vol, max_vol)
        logger.debug(f"Implied volatility calculation successful: {iv:.4f} using {n_steps} steps.")
        return iv
    except RuntimeError as e:
        logger.error(f"Failed to calculate implied volatility for {option_type} K={strike}: {e}")
        # Return a sensible default or NaN if calculation fails
        return float('nan')

def calculate_single_option_greeks(
    option_market_price: float,
    stock_price: float,
    strike: float,
    expiry_days: int,
    option_type: str,  # 'call' or 'put'
    risk_free_rate: float,
    dividend_yield: float,
    requested_greeks: list[str],
    iv_steps: int = 100,
    greeks_steps: int = 100
) -> dict[str, float]:
    """
    Calculates the requested Greeks for a single American option.

    This function first calculates the implied volatility from the market price,
    then uses that volatility to price the option and derive its Greeks. This ensures
    the risk metrics are consistent with the latest market data.

    Args:
        option_market_price: The current mid-price of the option.
        stock_price: The current mid-price of the underlying stock.
        strike: The strike price of the option.
        expiry_days: The number of calendar days until the option expires.
        option_type: 'call' or 'put'.
        risk_free_rate: The interpolated risk-free interest rate.
        dividend_yield: The annualized dividend yield of the stock.
        requested_greeks: A list of strings of the greeks to calculate (e.g., ['delta', 'gamma']).
        iv_steps: Number of binomial steps for the IV calculation (can be lower for speed).
        greeks_steps: Number of binomial steps for the final Greek calculation (can be higher for accuracy).

    Returns:
        A dictionary containing the calculated Greek values.
    """
    
    # Step 1: Calculate Implied Volatility using a potentially faster, lower-step model.
    implied_vol = calculate_implied_volatility(
        option_market_price, stock_price, strike, expiry_days,
        option_type, risk_free_rate, dividend_yield, n_steps=iv_steps
    )
    
    if implied_vol is None or implied_vol != implied_vol: # Check for NaN
        logger.warning(f"Skipping Greeks calculation for {option_type} K={strike} due to failed IV calculation.")
        return {greek: float('nan') for greek in requested_greeks}

    # Step 2: Recalculate Greeks using a more precise, higher-step model with the found IV.
    # This setup is largely identical to the IV function, but now we input our calculated IV.
    ql.Settings.instance().evaluationDate = ql.Date.todaysDate()

    if option_type.lower() == 'call':
        payoff = ql.PlainVanillaPayoff(ql.Option.Call, strike)
    else:
        payoff = ql.PlainVanillaPayoff(ql.Option.Put, strike)
        
    exercise = ql.AmericanExercise(ql.Date.todaysDate(), ql.Date.todaysDate() + expiry_days)
    option = ql.VanillaOption(payoff, exercise)
    
    spot_handle = ql.QuoteHandle(ql.SimpleQuote(stock_price))
    riskfree_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), risk_free_rate, ql.Actual365Fixed()))
    dividend_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), dividend_yield, ql.Actual365Fixed()))
    
    # The key difference: use the *calculated* implied volatility for this pricing process.
    volatility_handle = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(0, ql.TARGET(), implied_vol, ql.Actual365Fixed()))
    bsm_process = ql.BlackScholesMertonProcess(spot_handle, dividend_handle, riskfree_handle, volatility_handle)
    
    engine = ql.BinomialVanillaEngine(bsm_process, "crr", greeks_steps)
    option.setPricingEngine(engine)
    
    greeks = {}
    if 'delta' in requested_greeks:
        greeks['delta'] = option.delta()
    if 'theta' in requested_greeks:
        greeks['theta'] = option.theta() / 365.0
    if 'gamma' in requested_greeks:
        greeks['gamma'] = option.gamma()
    
    log_msg_parts = [f"{k.capitalize()}={v:.6f}" for k, v in greeks.items()]
    logger.debug(f"Greeks calculated with {greeks_steps} steps: {', '.join(log_msg_parts)}")
    
    return greeks



class DeltaEngine:
    """
    A long-running service that orchestrates the calculation of portfolio delta.

    This class acts as a background worker. It listens for trigger messages from
    the MarketDataManager, and upon receiving one, it fetches the latest market
    data, calls the appropriate QuantLib functions to calculate the delta of the
    call and put options, sums them, and publishes the net delta to the
    TradingStrategy via an output queue.
    """
    def __init__(
        self,
        market_manager: Any,  # Accept any market manager that has the required attributes
        trigger_queue: asyncio.Queue,
        delta_queue: asyncio.Queue,
        shutdown_event: asyncio.Event
    ):
        """
        Initializes the DeltaEngine.

        Args:
            market_manager: A reference to the MarketDataManager to access live prices.
            trigger_queue: The input queue for receiving calculation triggers.
            delta_queue: The output queue for publishing calculated deltas.
            shutdown_event: The event to signal graceful shutdown.
        """
        self.market_manager = market_manager
        self.trigger_queue = trigger_queue
        self.delta_queue = delta_queue
        self.shutdown_event = shutdown_event

    async def _publish_result(self, delta: float):
        """
        Puts the calculated delta into the output queue.

        It first clears any stale, unprocessed delta value from the queue to ensure
        the TradingStrategy only ever acts on the most recent calculation.
        """
        try:
            # Clear any old delta value that the strategy hasn't consumed yet.
            if not self.delta_queue.empty():
                self.delta_queue.get_nowait()
                self.delta_queue.task_done()
            self.delta_queue.put_nowait(delta)
        except asyncio.QueueFull:
            # This is safe to ignore. It means a new value was produced before
            # the old one was consumed, and we are just replacing it.
            pass

    async def run(self):
        """The main loop for the Delta Engine task."""
        logger.info("Delta Engine started. Waiting for triggers...")
        while not self.shutdown_event.is_set():
            try:
                # Wait for a trigger from the MarketDataManager.
                # A timeout is used to allow the shutdown event to be checked periodically.
                await asyncio.wait_for(self.trigger_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # --- Calculation Cycle ---
            # Once triggered, process all items currently in the queue to catch up
            # with any rapid market movements, then return to waiting.
            while True:
                # Get a consistent snapshot of market data.
                stock_price = self.market_manager.stock_price
                call_price = self.market_manager.call_option_price
                put_price = self.market_manager.put_option_price

                # The QuantLib calculations are CPU-bound and would block the async event
                # loop. We run them in a separate thread pool using `to_thread` to
                # keep the application responsive.
                put_greeks_task = asyncio.to_thread(
                    calculate_single_option_greeks,
                    put_price, stock_price,
                    self.market_manager.put_option_strike,
                    (self.market_manager.put_option_expiry - datetime.now().date()).days,
                    "put", self.market_manager.risk_free_rate, self.market_manager.dividend_yield,
                    ['delta']
                )

                call_greeks_task = asyncio.to_thread(
                    calculate_single_option_greeks,
                    call_price, stock_price,
                    self.market_manager.call_option_strike,
                    (self.market_manager.call_option_expiry - datetime.now().date()).days,
                    "call", self.market_manager.risk_free_rate, self.market_manager.dividend_yield,
                    ['delta']
                )

                put_greeks, call_greeks = await asyncio.gather(put_greeks_task, call_greeks_task)

                # The net delta of the straddle is the sum of the individual deltas.
                delta = put_greeks["delta"] + call_greeks["delta"]
                # Publish the final result to the TradingStrategy.
                await self._publish_result(delta)
                self.trigger_queue.task_done()

                # Check if another trigger came in while we were calculating.
                # If so, loop again immediately. If not, break and wait for a new trigger.
                try:
                    self.trigger_queue.get_nowait()
                except asyncio.QueueEmpty:
                    logger.debug("Delta trigger queue empty. Returning to idle state.")
                    break
        
        logger.info("Delta Engine shutting down.")


