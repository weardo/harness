#!/bin/bash
# Idempotent setup for harness-dev
# Safe to run multiple times
set -e

cd "$(dirname "$0")"

echo "=== Harness Dev Setup ==="

# Install Python dependencies (idempotent)
echo "Installing dependencies..."
pip3 install -q PyYAML pytest requests 2>/dev/null || pip install -q PyYAML pytest requests 2>/dev/null || true

# Verify dependencies
echo "Verifying imports..."
python3 -c "import yaml; import pytest; import requests; print('  All dependencies OK')"

# Verify existing codebase
echo "Running test suite..."
python3 -m pytest tests/ -v --tb=short 2>&1 | tail -5

echo ""
echo "=== Setup Complete ==="
echo "Run tests:   python3 -m pytest tests/ -v"
echo "Run harness: python3 src/run.py --prompt 'Build a todo app'"
