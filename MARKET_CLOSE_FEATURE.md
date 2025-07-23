# Market Close Monitoring - Feature Documentation

## Overview

The Gamma Scalping application now includes automatic market close monitoring that:
- **Stops the application 15 minutes before market close (3:45 PM ET)**
- **Optionally liquidates all positions before shutdown**
- **Handles market schedules including weekends and holidays**

## New Features

### 1. Market Schedule Monitor (`market/schedule_monitor.py`)

A new component that tracks US market hours and automatically triggers shutdown:

- **Market Hours**: 9:30 AM - 4:00 PM Eastern Time
- **Shutdown Time**: 3:45 PM ET (15 minutes before close)
- **Market Days**: Monday-Friday, excluding major holidays
- **Holiday Support**: Basic holiday detection (can be extended)

### 2. Command Line Parameter

New `--liquidate-before-close` flag:

```bash
# Enable automatic liquidation before market close
python main.py --mode resume --ticker SPY --liquidate-before-close

# Keep positions (default behavior)
python main.py --mode resume --ticker SPY
```

### 3. Enhanced Start Script

The `start_gamma_scalper.sh` script now supports:

```bash
# Auto mode (default): Init then resume automatically
./start_gamma_scalper.sh

# Init mode only: Just initialize positions and exit
./start_gamma_scalper.sh --mode init

# Resume mode only: Start trading with existing positions
./start_gamma_scalper.sh --mode resume

# With custom ticker and auto mode
./start_gamma_scalper.sh --ticker QQQ

# Init mode with custom ticker
./start_gamma_scalper.sh --mode init --ticker SPY

# Resume mode with auto-liquidation before market close
./start_gamma_scalper.sh --mode resume --liquidate-before-close

# Combined options
./start_gamma_scalper.sh --mode auto --ticker SPY --liquidate-before-close

# Show help
./start_gamma_scalper.sh --help
```

#### Mode Options

- **`auto` (default)**: Runs initialization first, then automatically switches to resume mode
- **`init`**: Only initializes positions and exits - useful for testing setup
- **`resume`**: Starts trading immediately with existing positions - good for restarting

## How It Works

### Market Monitoring Process

1. **Monitor Start**: The market monitor starts when the application launches
2. **Schedule Checking**: Continuously checks time until market close
3. **Status Updates**: Logs periodic updates about time remaining
4. **Shutdown Trigger**: When 15 minutes before close (3:45 PM ET):
   - Optionally liquidates all positions
   - Triggers graceful shutdown of entire application

### Liquidation Process

When `--liquidate-before-close` is enabled:

1. **Position Check**: Monitor checks if position manager is available
2. **Liquidation**: Calls `position_manager._close_all_positions()`
3. **Wait Period**: Brief pause to allow orders to process
4. **Shutdown**: Triggers application shutdown

### Logging Output

The monitor provides clear status messages:

```
🕐 Market schedule monitor started
📈 Market is currently open
⏰ Next shutdown scheduled for: 2025-07-23 03:45 PM EDT
💸 Automatic liquidation ENABLED before market close
⏱️  45 minutes until market close shutdown
🛑 Market close approaching - initiating shutdown sequence
🔄 Liquidating all positions before market close...
✅ All positions liquidated successfully
📴 Triggering application shutdown
```

## Configuration Examples

### Example 1: Quick Testing (Init Only)
```bash
./start_gamma_scalper.sh --mode init --ticker SPY
```
- Only initializes SPY positions
- Exits after setup complete
- Good for testing position creation

### Example 2: Resume Trading (Existing Positions)
```bash
./start_gamma_scalper.sh --mode resume --liquidate-before-close
```
- Resumes with existing positions
- Automatically closes positions before market close
- Good for restarting after interruption

### Example 3: Full Automation (Default)
```bash
./start_gamma_scalper.sh --ticker QQQ --liquidate-before-close
```
- Auto mode: initializes then resumes automatically
- Trades QQQ options with auto-liquidation
- Complete hands-off operation

### Example 4: Conservative Setup
```bash
./start_gamma_scalper.sh --mode auto --ticker SPY --liquidate-before-close
```
- Full automation with position safety
- SPY trading (typically more stable)
- Automatic liquidation before market close

## Implementation Details

### Market Schedule Detection

```python
# Market hours (Eastern Time)
market_open = time(9, 30)    # 9:30 AM ET
market_close = time(16, 0)   # 4:00 PM ET
shutdown_time = time(15, 45) # 3:45 PM ET
```

### Holiday Detection

Basic holiday support included:
- New Year's Day (1/1)
- Independence Day (7/4)  
- Christmas Day (12/25)

*Note: Can be extended with comprehensive holiday calendar*

### Time Zone Handling

Uses `pytz` library for accurate Eastern Time calculations:
- Handles daylight saving time transitions
- Ensures accurate market schedule detection

## Testing

### Test Script Available

Run `test_market_monitor.py` to verify functionality:

```bash
python test_market_monitor.py
```

This tests:
- Current market status
- Next shutdown calculation
- Time until shutdown
- Monitor execution

### Manual Testing

Check help output:
```bash
python main.py --help
./start_gamma_scalper.sh --help
```

## Benefits

1. **Automated Risk Management**: No more forgetting to close positions
2. **Flexible Configuration**: Choose when to liquidate vs keep positions
3. **Clear Monitoring**: Detailed logs show exactly what's happening
4. **Reliable Timing**: Accurate market schedule detection
5. **Safe Defaults**: Conservative behavior unless explicitly enabled

## Backward Compatibility

- **Existing scripts work unchanged**: All previous functionality preserved
- **Default behavior unchanged**: No liquidation unless explicitly requested
- **Config files untouched**: No modifications to existing configuration

## Next Steps

Potential enhancements:
1. **Extended Holiday Calendar**: Full NYSE holiday support
2. **Custom Shutdown Times**: User-configurable shutdown timing
3. **Partial Liquidation**: Close only specific position types
4. **Pre-Market Detection**: Handle early market closures
5. **Multiple Time Zones**: Support for different market schedules
