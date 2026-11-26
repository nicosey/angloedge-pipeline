#!/bin/bash
# Wrapper for launchd: generate briefing then publish.
# Usage: ./run.sh <topic> [briefing flags...]
# Example: ./run.sh uk_capital_markets --full-day

set -euo pipefail

TOPIC="${1:-robotics}"
shift || true   # remaining args passed to briefing.py

cd "$(dirname "$0")"

python3 briefing.py "$TOPIC" "$@"
python3 site_repo.py --repair   # clear a stuck rebase on generated data first; alerts otherwise
python3 publish.py
