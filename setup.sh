#!/usr/bin/env bash
# setup.sh — Automotive DDS OTA Demo Setup Script
# ================================================
# Sets up the Python environment, dependencies, and sample data for the demo.
#
# Usage:
#   chmod +x setup.sh && ./setup.sh
#
# What this script does:
#   1. Checks Python 3.8+
#   2. Creates a virtual environment (.venv/)
#   3. Installs Python dependencies
#   4. Creates data/ and plots/ directories
#   5. Generates pre-recorded sample data

set -e  # exit on error

# ── Colors ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── Banner ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔════════════════════════════════════════════════════════╗"
echo -e "║  Automotive DDS OTA Demo Setup                         ║"
echo -e "╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Step 1: Check Python version ─────────────────────────────────────────
echo -e "${BOLD}[1/6] Checking Python version...${NC}"

if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo -e "${RED}ERROR: Python not found. Please install Python 3.8+${NC}"
    exit 1
fi

PYTHON_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.minor)")

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]); then
    echo -e "${RED}ERROR: Python 3.8+ required (found $PYTHON_VERSION)${NC}"
    exit 1
fi

echo -e "  ${GREEN}✓${NC} Python $PYTHON_VERSION found at $(which $PYTHON_CMD)"

# ── Step 2: Create virtual environment ───────────────────────────────────
echo ""
echo -e "${BOLD}[2/6] Setting up virtual environment...${NC}"

if [ ! -d ".venv" ]; then
    $PYTHON_CMD -m venv .venv
    echo -e "  ${GREEN}✓${NC} Created .venv/"
else
    echo -e "  ${YELLOW}→${NC} .venv/ already exists, reusing."
fi

# Activate the virtual environment
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    echo -e "  ${GREEN}✓${NC} Virtual environment activated"
else
    echo -e "${RED}ERROR: Failed to find .venv/bin/activate${NC}"
    exit 1
fi

# ── Step 3: Install Python dependencies ──────────────────────────────────
echo ""
echo -e "${BOLD}[3/6] Installing Python dependencies...${NC}"

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo -e "  ${GREEN}✓${NC} matplotlib, numpy, pandas, colorama, tabulate installed"

# ── Step 4: Create directories ───────────────────────────────────────────
echo ""
echo -e "${BOLD}[4/5] Creating project directories...${NC}"

mkdir -p data plots
echo -e "  ${GREEN}✓${NC} data/  — metrics CSV output"
echo -e "  ${GREEN}✓${NC} plots/ — visualization PNG output"

# ── Step 5: Generate sample data ─────────────────────────────────────────
echo ""
echo -e "${BOLD}[5/5] Generating pre-recorded sample data...${NC}"

$PYTHON_CMD generate_sample_data.py

# ── Done: Print instructions ──────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
echo -e "║  Setup complete! Ready to run the demo.              ║"
echo -e "╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}Quick Start Commands:${NC}"
echo ""
echo -e "  ${CYAN}# Activate the virtual environment${NC}"
echo -e "  source .venv/bin/activate"
echo ""
echo -e "  ${CYAN}# Run the full demo (5 ECUs, with CAN vs DDS comparison)${NC}"
echo -e "  python run_demo.py --num-ecus 5 --show-comparison"
echo ""
echo -e "  ${CYAN}# Scale to 10 ECUs${NC}"
echo -e "  python run_demo.py --num-ecus 10 --firmware 3.0.0"
echo ""
echo -e "  ${CYAN}# Demonstrate QoS impact${NC}"
echo -e "  python run_demo.py --num-ecus 5 --qos best_effort"
echo ""
echo -e "  ${CYAN}# Generate all visualization charts${NC}"
echo -e "  python visualize_results.py --generate-sample"
echo ""
echo -e "  ${CYAN}# Run individual ECU standalone${NC}"
echo -e "  python ecu.py --ecu-id ECU_001 --firmware 1.0.0"
echo ""
echo -e "${BOLD}15-Minute Demo Script:${NC}"
echo ""
echo -e "  0:00  Introduction — architecture diagram"
echo -e "  1:00  python run_demo.py --num-ecus 5 --show-comparison"
echo -e "  5:00  Metrics walkthrough (latency, jitter, throughput)"
echo -e "  8:00  python run_demo.py --num-ecus 5 --qos best_effort"
echo -e " 10:00  python visualize_results.py --generate-sample"
echo -e " 12:00  Discuss AUTOSAR Adaptive / ISO 26262 relevance"
echo -e " 14:00  Q&A"
echo ""
echo -e "${BOLD}DDS Mode:${NC}"
echo -e "  ${CYAN}→  Python DDS simulation — faithful DDS behavior emulation${NC}"
echo ""
