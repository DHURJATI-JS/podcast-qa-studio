#!/bin/bash
# Podcast Q&A Studio - double-click to install (first time) and run.
# If macOS says it can't verify the file: right-click it, choose Open, then Open again.
cd "$(dirname "$0")" || exit 1
/bin/bash ./scripts/install-and-run.sh
echo
read -r -n 1 -s -p "Press any key to close this window..."
echo
