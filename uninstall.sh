#!/bin/bash
# Removes the scheduled job. Leaves the Desktop file and saved state alone.
LABEL="com.craigslistcash.jobs"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null && echo "unloaded $LABEL" || echo "$LABEL was not loaded"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" && echo "removed plist"
echo "Desktop file and ~/.craigslistcash left in place; delete by hand if you want them gone."
