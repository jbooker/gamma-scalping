#!/bin/bash

# Gamma Scalping Liquidation Script
# This script liquidates all positions without establishing new ones

echo "💸 Liquidating All Gamma Scalping Positions..."

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to create a liquidation-only version of main.py
create_liquidation_script() {
    echo -e "${YELLOW}📝 Creating temporary liquidation script...${NC}"
    
    cat > liquidate_only.py << 'EOF'
#!/usr/bin/env python3
"""
Liquidation-only script for gamma scalping strategy.
This closes all positions without establishing new ones.
"""

import asyncio
import logging
import argparse
from portfolio.position_manager import PositionManager

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

def parse_arguments():
    """Parse command line arguments for initialization mode."""
    parser = argparse.ArgumentParser(description='Liquidation Script')
    parser.add_argument(
        '--mode', 
        choices=['init', 'resume'], 
        default='init',
        help='Initialization mode for liquidation'
    )
    return parser.parse_args()

async def liquidate_positions():
    """Liquidate all positions and exit."""
    logger.info("🚀 Starting liquidation process...")
    
    # Parse arguments
    args = parse_arguments()
    
    try:
        # Create required queue and event for PositionManager
        trade_action_queue = asyncio.Queue(maxsize=1)
        shutdown_event = asyncio.Event()
        
        # Initialize position manager with required arguments and mode
        position_manager = PositionManager(trade_action_queue, shutdown_event, args.mode)
        
        # Initialize the position manager (this will trigger liquidation in 'init' mode)
        await position_manager.initialize_position()
        
        # Wait a moment for liquidations to complete
        await asyncio.sleep(3)
        
        logger.info("✅ Liquidation completed successfully!")
        
    except Exception as e:
        logger.error(f"❌ Error during liquidation: {e}")
        raise
    finally:
        # Cleanup - set shutdown event and close streams
        try:
            shutdown_event.set()
            await position_manager._close_streams()
        except:
            pass

if __name__ == "__main__":
    asyncio.run(liquidate_positions())
EOF
    
    echo -e "${GREEN}✅ Liquidation script created${NC}"
}

# Function to run liquidation
run_liquidation() {
    echo -e "${YELLOW}💸 Executing liquidation...${NC}"
    
    # Run the liquidation script with init mode
    .venv/bin/python liquidate_only.py --mode init
    
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        echo -e "${GREEN}✅ Liquidation completed successfully${NC}"
    else
        echo -e "${RED}❌ Liquidation failed with exit code: $exit_code${NC}"
        return $exit_code
    fi
}

# Function to cleanup
cleanup() {
    echo -e "${YELLOW}🧹 Cleaning up temporary files...${NC}"
    rm -f liquidate_only.py
    echo -e "${GREEN}✅ Cleanup completed${NC}"
}

# Main execution
main() {
    echo "🎯 Step 1: Create liquidation script"
    create_liquidation_script
    
    echo ""
    echo "🎯 Step 2: Execute liquidation"
    if run_liquidation; then
        echo ""
        echo -e "${GREEN}🎉 All positions have been liquidated successfully!${NC}"
        echo -e "💰 Your account is now flat (no positions)"
    else
        echo ""
        echo -e "${RED}❌ Liquidation failed. Please check the logs above.${NC}"
        cleanup
        exit 1
    fi
    
    echo ""
    echo "🎯 Step 3: Cleanup"
    cleanup
    
    echo ""
    echo -e "${GREEN}🎯 Liquidation process complete!${NC}"
    echo -e "📊 You can check your positions in your broker account"
    echo -e "🚀 To start fresh: ./start_gamma_scalper.sh"
    echo -e "📈 To resume existing positions: .venv/bin/python main.py --mode resume"
}

# Validation checks
if [ ! -f "main.py" ]; then
    echo -e "${RED}❌ Error: main.py not found. Please run this script from the gamma-scalping directory.${NC}"
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo -e "${RED}❌ Error: .venv directory not found. Please ensure Python virtual environment is set up.${NC}"
    exit 1
fi

if [ ! -f "config.py" ]; then
    echo -e "${RED}❌ Error: config.py not found. Please ensure you're in the correct directory.${NC}"
    exit 1
fi

# Run the main function
main

echo "✨ Script completed!"
