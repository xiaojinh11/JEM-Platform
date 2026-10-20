#!/bin/bash
# JEM Platform Backend Quick Start (Linux/Mac)
cd /d/zidongh/backend

echo "========================================"
echo "JEM Backend Starting..."
echo "========================================"

# Create venv if not exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate venv
source .venv/bin/activate

echo "Installing/Updating dependencies..."
pip install -q -r requirements.txt

echo ""
echo "Starting Uvicorn server on http://localhost:8000"
echo "API Docs: http://localhost:8000/docs"
echo ""

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
