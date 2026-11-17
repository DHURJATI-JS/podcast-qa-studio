#!/bin/bash
# Podcast Q&A Studio - stop all services. Your data (database, models) is kept.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
echo "Stopping Podcast Q&A Studio (your data is kept)..."
docker compose stop
echo
read -r -n 1 -s -p "Stopped. Press any key to close this window..."
echo
