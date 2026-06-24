#!/bin/bash
# Wedding Photo Booth — auto-restart wrapper
#
# Use this INSTEAD OF running "python3 server.py" directly.
# If the server crashes for any reason during the event, this script
# automatically restarts it after a short pause, so the booth recovers
# on its own rather than staying down until someone notices.
#
# To stop the booth entirely, press Ctrl+C in this Terminal window.

cd ~/wedding-booth

echo ""
echo "  Wedding Booth — auto-restart supervisor"
echo "  Press Ctrl+C to stop the booth completely."
echo ""

while true; do
    python3 server.py
    EXIT_CODE=$?

    echo ""
    echo "  [supervisor] Server stopped (exit code $EXIT_CODE)."
    echo "  [supervisor] Restarting in 2 seconds... (Ctrl+C to stop for good)"
    echo ""
    sleep 2
done
