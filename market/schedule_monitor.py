"""
Market Schedule Monitor for Gamma Scalping Strategy

This module handles market schedule monitoring and automatic shutdown
15 minutes before market close, with optional position liquidation.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Optional, TYPE_CHECKING
import pytz

if TYPE_CHECKING:
    from ..portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)

class MarketScheduleMonitor:
    """
    Monitors market schedule and triggers shutdown before market close.
    
    Features:
    - Monitors US market hours (9:30 AM - 4:00 PM ET)
    - Triggers shutdown 15 minutes before market close (3:45 PM ET)
    - Optional position liquidation before shutdown
    - Handles market holidays and weekends
    """
    
    def __init__(self, shutdown_event: asyncio.Event, liquidate_before_close: bool = False):
        """
        Initialize the market schedule monitor.
        
        Args:
            shutdown_event: Event to signal when app should shut down
            liquidate_before_close: Whether to liquidate positions before shutdown
        """
        self.shutdown_event = shutdown_event
        self.liquidate_before_close = liquidate_before_close
        self.position_manager: Optional['PositionManager'] = None
        
        # US Eastern Time zone
        self.et_tz = pytz.timezone('US/Eastern')
        
        # Market hours (Eastern Time)
        self.market_open = time(9, 30)  # 9:30 AM ET
        self.market_close = time(16, 0)  # 4:00 PM ET
        self.shutdown_time = time(15, 45)  # 3:45 PM ET (15 min before close)
        
    def set_position_manager(self, position_manager):
        """Set the position manager for liquidation functionality."""
        self.position_manager = position_manager
        
    def is_market_day(self, date: datetime) -> bool:
        """
        Check if the given date is a market day (weekday, excluding holidays).
        
        Args:
            date: Date to check
            
        Returns:
            True if it's a market day, False otherwise
        """
        # Check if it's a weekday (Monday=0, Sunday=6)
        if date.weekday() >= 5:  # Saturday or Sunday
            return False
            
        # Basic holiday check (you can extend this with a proper holiday calendar)
        # For now, just check some major holidays
        major_holidays = [
            (1, 1),    # New Year's Day
            (7, 4),    # Independence Day
            (12, 25),  # Christmas Day
        ]
        
        month_day = (date.month, date.day)
        if month_day in major_holidays:
            return False
            
        return True
        
    def get_next_shutdown_time(self) -> Optional[datetime]:
        """
        Get the next shutdown time (3:45 PM ET on the next market day).
        
        Returns:
            Next shutdown datetime, or None if market is closed for an extended period
        """
        now_et = datetime.now(self.et_tz)
        today = now_et.date()
        
        # Check if today is a market day
        if self.is_market_day(now_et):
            # Create shutdown datetime for today
            shutdown_today = self.et_tz.localize(
                datetime.combine(today, self.shutdown_time)
            )
            
            # If we haven't passed shutdown time today, use today
            if now_et < shutdown_today:
                return shutdown_today
                
        # Look for the next market day (up to 7 days ahead)
        for days_ahead in range(1, 8):
            future_date = now_et + timedelta(days=days_ahead)
            if self.is_market_day(future_date):
                return self.et_tz.localize(
                    datetime.combine(future_date.date(), self.shutdown_time)
                )
                
        return None
        
    def is_market_open_now(self) -> bool:
        """
        Check if the market is currently open.
        
        Returns:
            True if market is open, False otherwise
        """
        now_et = datetime.now(self.et_tz)
        
        # Check if it's a market day
        if not self.is_market_day(now_et):
            return False
            
        # Check if current time is within market hours
        current_time = now_et.time()
        return self.market_open <= current_time <= self.market_close
        
    def time_until_shutdown(self) -> Optional[timedelta]:
        """
        Get the time remaining until the next shutdown.
        
        Returns:
            Time until shutdown, or None if no shutdown scheduled
        """
        next_shutdown = self.get_next_shutdown_time()
        if next_shutdown is None:
            return None
            
        now_et = datetime.now(self.et_tz)
        return next_shutdown - now_et
        
    async def liquidate_positions(self):
        """Liquidate all positions before market close."""
        if self.position_manager is None:
            logger.warning("Position manager not set, cannot liquidate positions")
            return
            
        logger.info("🔄 Liquidating all positions before market close...")
        try:
            await self.position_manager._close_all_positions()
            logger.info("✅ All positions liquidated successfully")
        except Exception as e:
            logger.error(f"❌ Error during position liquidation: {e}")
            
    async def run(self):
        """
        Main monitoring loop that checks for shutdown time.
        
        This runs continuously and triggers shutdown 15 minutes before market close.
        """
        logger.info("🕐 Market schedule monitor started")
        
        # Log initial status
        if self.is_market_open_now():
            logger.info("📈 Market is currently open")
        else:
            logger.info("📉 Market is currently closed")
            
        next_shutdown = self.get_next_shutdown_time()
        if next_shutdown:
            logger.info(f"⏰ Next shutdown scheduled for: {next_shutdown.strftime('%Y-%m-%d %I:%M %p %Z')}")
        else:
            logger.warning("⚠️  No shutdown time found (extended market closure?)")
            
        if self.liquidate_before_close:
            logger.info("💸 Automatic liquidation ENABLED before market close")
        else:
            logger.info("🔒 Automatic liquidation DISABLED")
            
        while not self.shutdown_event.is_set():
            try:
                time_until = self.time_until_shutdown()
                
                if time_until is None:
                    # No shutdown scheduled, check again in 1 hour
                    await asyncio.sleep(3600)
                    continue
                    
                # If shutdown time has passed or is very close (within 1 minute)
                if time_until.total_seconds() <= 60:
                    logger.info("🛑 Market close approaching - initiating shutdown sequence")
                    
                    # Liquidate positions if requested
                    if self.liquidate_before_close:
                        await self.liquidate_positions()
                        # Wait a moment for liquidation to complete
                        await asyncio.sleep(5)
                        
                    logger.info("📴 Triggering application shutdown")
                    self.shutdown_event.set()
                    break
                    
                # Log periodic status updates
                if time_until.total_seconds() < 3600:  # Less than 1 hour
                    minutes_remaining = int(time_until.total_seconds() / 60)
                    logger.info(f"⏱️  {minutes_remaining} minutes until market close shutdown")
                    
                # Check every minute when close to shutdown, otherwise every 10 minutes
                sleep_time = 60 if time_until.total_seconds() < 1800 else 600  # 30 min threshold
                await asyncio.sleep(sleep_time)
                
            except Exception as e:
                logger.error(f"Error in market schedule monitor: {e}")
                await asyncio.sleep(60)  # Wait a minute before retrying
                
        logger.info("🏁 Market schedule monitor stopped")
