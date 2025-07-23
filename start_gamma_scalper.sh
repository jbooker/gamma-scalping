#!/bin/bash

# Gamma Scalping Auto-Start Script
# This script automates the process of:
# 1. Running the app in 'init' mode to establish positions
# 2. Stopping the app after initialization is complete
# 3. Switching to 'resume' mode
# 4. Restarting the app for normal operation
# 5. Optional: Automatically liquidate positions before market close

set -e  # Exit on any error

echo "🚀 Starting Gamma Scalping Auto-Setup..."

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default parameters
TICKER=""
LIQUIDATE_BEFORE_CLOSE=false
MODE="auto"  # auto, init, or resume

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --ticker)
            TICKER="$2"
            shift 2
            ;;
        --liquidate-before-close)
            LIQUIDATE_BEFORE_CLOSE=true
            shift
            ;;
        --mode)
            MODE="$2"
            if [[ "$MODE" != "auto" && "$MODE" != "init" && "$MODE" != "resume" ]]; then
                echo -e "${RED}❌ Invalid mode: $MODE. Must be 'auto', 'init', or 'resume'${NC}"
                exit 1
            fi
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --mode MODE                  Set mode: auto (default), init, or resume"
            echo "                               auto: Run init then resume automatically"
            echo "                               init: Only initialize positions and exit"
            echo "                               resume: Only resume with existing positions"
            echo "  --ticker SYMBOL              Override ticker symbol (e.g., SPY, QQQ)"
            echo "  --liquidate-before-close     Enable automatic liquidation before market close"
            echo "  --help, -h                   Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                                    # Auto mode with default settings"
            echo "  $0 --mode init --ticker QQQ           # Only initialize QQQ positions"
            echo "  $0 --mode resume --liquidate-before-close  # Resume trading with auto-liquidation"
            echo "  $0 --ticker SPY --liquidate-before-close   # Auto mode: SPY with liquidation"
            exit 0
            ;;
        *)
            echo -e "${RED}❌ Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Function to check if app has finished initialization
wait_for_initialization() {
    echo -e "${YELLOW}⏳ Waiting for initialization to complete...${NC}"
    
    # Wait for the app to start and complete initialization
    local timeout=60  # 60 seconds timeout
    local elapsed=0
    
    while [ $elapsed -lt $timeout ]; do
        sleep 2
        elapsed=$((elapsed + 2))
        echo -n "."
    done
    
    echo -e "\n${GREEN}✅ Initialization phase completed${NC}"
    return 0
}

# Function to start app and wait for it to be ready
start_app() {
    local mode=$1
    
    echo -e "${YELLOW}🔄 Starting app in $mode mode...${NC}"
    
    # Build command with optional parameters
    local cmd=".venv/bin/python main.py --mode \"$mode\""
    
    # Add ticker if specified
    if [ -n "$TICKER" ]; then
        cmd="$cmd --ticker \"$TICKER\""
        echo -e "${BLUE}📊 Using ticker: $TICKER${NC}"
    fi
    
    # Add liquidate-before-close flag if specified (only for resume mode)
    if [ "$mode" = "resume" ] && [ "$LIQUIDATE_BEFORE_CLOSE" = true ]; then
        cmd="$cmd --liquidate-before-close"
        echo -e "${BLUE}💸 Auto-liquidation before market close: ENABLED${NC}"
    fi
    
    echo -e "${YELLOW}🚀 Command: $cmd${NC}"
    
    # Start the app in background
    eval "$cmd" &
    local pid=$!
    
    echo "App started with PID: $pid"
    
    # Return the PID so caller can manage the process
    echo $pid
}

# Function to stop the app gracefully
stop_app() {
    local pid=$1
    echo -e "${YELLOW}🛑 Stopping app (PID: $pid)...${NC}"
    
    # Try graceful shutdown first
    kill -TERM "$pid" 2>/dev/null || true
    sleep 3
    
    # Force kill if still running
    if kill -0 "$pid" 2>/dev/null; then
        echo "Force killing process..."
        kill -9 "$pid" 2>/dev/null || true
    fi
    
    # Also kill any remaining python main.py processes
    pkill -f "python main.py" 2>/dev/null || true
    
    echo -e "${GREEN}✅ App stopped${NC}"
}

# Main execution
main() {
    echo ""
    echo -e "${GREEN}⚙️  Configuration:${NC}"
    echo -e "  🎯 Mode: $MODE"
    if [ -n "$TICKER" ]; then
        echo -e "  📊 Ticker: $TICKER"
    else
        echo -e "  📊 Ticker: Using default from config"
    fi
    echo -e "  💸 Auto-liquidation: $LIQUIDATE_BEFORE_CLOSE"
    echo ""
    
    if [ "$MODE" = "auto" ]; then
        echo "🎯 Step 1: Initialize positions"
        
        # Start app in init mode
        init_pid=$(start_app "init")
        
        # Wait for initialization to complete
        if wait_for_initialization; then
            echo -e "${GREEN}✅ Positions initialized successfully${NC}"
        else
            echo -e "${RED}❌ Initialization failed${NC}"
            stop_app "$init_pid"
            exit 1
        fi
        
        # Stop the app
        stop_app "$init_pid"
        
        echo ""
        echo "🔄 Step 2: Switch to resume mode"
        
        # Wait a moment for cleanup
        sleep 2
        
        # Start app in resume mode
        echo -e "${YELLOW}🚀 Starting app in resume mode for normal operation...${NC}"
        resume_pid=$(start_app "resume")
        
        echo ""
        echo -e "${GREEN}🎉 Gamma Scalping is now running in resume mode!${NC}"
        echo -e "🛑 Stop the app: kill $resume_pid or use the stop script"
        echo ""
        echo "App PID: $resume_pid"
        
    elif [ "$MODE" = "init" ]; then
        echo "🎯 Initialize positions only"
        
        # Start app in init mode
        init_pid=$(start_app "init")
        
        # Wait for initialization to complete
        if wait_for_initialization; then
            echo -e "${GREEN}✅ Positions initialized successfully${NC}"
            echo -e "${BLUE}💡 Use '$0 --mode resume' to start trading${NC}"
        else
            echo -e "${RED}❌ Initialization failed${NC}"
            stop_app "$init_pid"
            exit 1
        fi
        
        # Stop the app
        stop_app "$init_pid"
        echo -e "${GREEN}🏁 Initialization complete. App stopped.${NC}"
        
    elif [ "$MODE" = "resume" ]; then
        echo "🔄 Resume trading with existing positions"
        
        # Start app directly in resume mode
        echo -e "${YELLOW}🚀 Starting app in resume mode...${NC}"
        resume_pid=$(start_app "resume")
        
        echo ""
        echo -e "${GREEN}🎉 Gamma Scalping is running in resume mode!${NC}"
        echo -e "🛑 Stop the app: kill $resume_pid or use the stop script"
        echo ""
        echo "App PID: $resume_pid"
    fi
}

# Check if we're in the right directory
if [ ! -f "main.py" ]; then
    echo -e "${RED}❌ Error: main.py not found. Please run this script from the gamma-scalping directory.${NC}"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo -e "${RED}❌ Error: .venv directory not found. Please ensure Python virtual environment is set up.${NC}"
    exit 1
fi

# Run the main function
main

echo "✨ Script completed!"
