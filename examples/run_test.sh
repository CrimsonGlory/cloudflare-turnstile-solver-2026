#!/usr/bin/env bash
# Run the title test inside the solver container (works over SSH, no GUI).
set -euo pipefail
exec docker compose exec -T solver python3 /app/examples/test_site.py --expect "${1:-Upwork}" "${@:2}"
