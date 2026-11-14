#!/bin/sh
# git asks for username then password. Token comes from the file named in
# DESK_TOKEN_FILE at push time; it is never in argv, the remote URL or a log.
case "$1" in
  Username*) echo "x-access-token" ;;
  *) tr -d ' \t\r\n' < "$DESK_TOKEN_FILE" ;;
esac
