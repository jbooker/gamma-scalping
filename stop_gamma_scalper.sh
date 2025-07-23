#!/bin/bash

# Gamma Scalping Stop Script
# This script stops all running gamma scalping processes

echo "🛑 Stopping Gamma Scalping..."

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Direct approach - kill all python processes running main.py
echo -e "${YELLOW}� Stopping all gamma scalping processes...${NC}"

# Method 1: Kill by pattern
pkill -9 -f "main.py" 2>/dev/null && echo "Killed main.py processes" || echo "No main.py processes found"

# Method 2: Kill by specific pattern
pkill -9 -f "python.*main.py" 2>/dev/null && echo "Killed python main.py processes" || true

# Method 3: Kill by venv pattern  
pkill -9 -f ".venv.*python.*main.py" 2>/dev/null && echo "Killed venv python processes" || true

# Method 4: Kill by directory pattern
pkill -9 -f "gamma-scalping.*python.*main.py" 2>/dev/null && echo "Killed gamma-scalping processes" || true

sleep 1

echo -e "${GREEN}✅ Gamma Scalping shutdown complete${NC}"
echo "✨ You can now restart with: ./start_gamma_scalper.sh"
