#!/usr/bin/env bash
# Start Caliber — double-click this file to launch.
#
# Mac will run this in Terminal. Closing the Terminal window or pressing
# Ctrl-C in it will stop Caliber.

# cd to the script's directory, regardless of where it was launched from
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# Show a clean banner in the Terminal window
clear
cat <<'BANNER'
   ╔══════════════════════════════════════════════════════════╗
   ║                                                          ║
   ║         Caliber — privacy LLM gateway                    ║
   ║                                                          ║
   ║         Starting up…                                     ║
   ║                                                          ║
   ║         Press Ctrl-C in this window to stop.             ║
   ║         Closing this window also stops Caliber.          ║
   ║                                                          ║
   ╚══════════════════════════════════════════════════════════╝

BANNER

# Hand off to the quickstart script (which starts the gateway, seeds demo
# data if needed, and opens the browser at http://localhost:8800/app).
exec ./scripts/quickstart.sh
