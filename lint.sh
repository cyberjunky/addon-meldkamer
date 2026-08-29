#!/bin/bash
# Meldkamer - Lint & Format Script
# Usage: ./lint.sh [check|fix]
#   check - Report issues only (default, CI-safe)
#   fix   - Auto-fix and format in place

set -e

MODE="${1:-check}"
SRC_DIR="src"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}Meldkamer - Lint & Format${NC}"
echo "Mode: $MODE"
echo "---"

# Check if ruff is installed
if ! command -v ruff &> /dev/null; then
    echo -e "${RED}ruff not found. Install with: pip install ruff${NC}"
    exit 1
fi

if [ "$MODE" = "fix" ]; then
    echo -e "\n${YELLOW}Formatting...${NC}"
    ruff format "$SRC_DIR"
    
    echo -e "\n${YELLOW}Auto-fixing lint issues...${NC}"
    ruff check --fix "$SRC_DIR"
    
    echo -e "\n${GREEN}Done!${NC}"
elif [ "$MODE" = "check" ]; then
    ERRORS=0
    
    echo -e "\n${YELLOW}Checking format...${NC}"
    if ! ruff format --check "$SRC_DIR"; then
        ERRORS=1
    fi
    
    echo -e "\n${YELLOW}Linting...${NC}"
    if ! ruff check "$SRC_DIR"; then
        ERRORS=1
    fi
    
    if [ $ERRORS -eq 0 ]; then
        echo -e "\n${GREEN}All checks passed!${NC}"
    else
        echo -e "\n${RED}Issues found. Run './lint.sh fix' to auto-fix.${NC}"
        exit 1
    fi
else
    echo "Usage: ./lint.sh [check|fix]"
    exit 1
fi
