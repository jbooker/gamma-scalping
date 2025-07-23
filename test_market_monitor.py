#!/usr/bin/env python3
"""
Test script for Market Schedule Monitor

This script tests the market schedule monitoring functionality
without running the full trading application.
"""

import asyncio
import logging
import sys
from market.schedule_monitor import MarketScheduleMonitor

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def test_market_monitor():
    """Test the market schedule monitor functionality."""
    print("🧪 Testing Market Schedule Monitor")
    print("=" * 50)
    
    # Create shutdown event
    shutdown_event = asyncio.Event()
    
    # Test with liquidation disabled
    print("\n📊 Creating monitor with liquidation DISABLED...")
    monitor = MarketScheduleMonitor(shutdown_event, liquidate_before_close=False)
    
    # Test current market status
    print(f"📈 Market open now: {monitor.is_market_open_now()}")
    
    # Test next shutdown time
    next_shutdown = monitor.get_next_shutdown_time()
    if next_shutdown:
        print(f"⏰ Next shutdown: {next_shutdown.strftime('%Y-%m-%d %I:%M %p %Z')}")
    else:
        print("⚠️  No shutdown scheduled")
    
    # Test time until shutdown
    time_until = monitor.time_until_shutdown()
    if time_until:
        total_seconds = int(time_until.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        print(f"⏱️  Time until shutdown: {hours}h {minutes}m")
    else:
        print("⏱️  No shutdown time available")
    
    print("\n🔄 Testing monitor with liquidation ENABLED...")
    monitor_with_liquidation = MarketScheduleMonitor(shutdown_event, liquidate_before_close=True)
    
    # Test running the monitor for a few seconds
    print("\n🏃 Running monitor for 5 seconds...")
    try:
        await asyncio.wait_for(monitor.run(), timeout=5.0)
    except asyncio.TimeoutError:
        print("✅ Monitor ran successfully for 5 seconds")
    
    print("\n✅ Market monitor test completed!")

if __name__ == "__main__":
    try:
        asyncio.run(test_market_monitor())
    except KeyboardInterrupt:
        print("\n🛑 Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
