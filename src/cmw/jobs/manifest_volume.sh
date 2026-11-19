#!/bin/bash
# Observes the UUID of the volume holding a path. This script owns tool discovery
# (df/diskutil/plutil on macOS, findmnt on Linux); Python (cmw.jobs.manifest_legacy)
# validates and compares the value and never invents one. Prints one JSON line;
# an unavailable UUID prints null.
set -u
path=$1
uuid=
case "$(uname -s)" in
    Darwin)
        device=$(/bin/df -P "$path" 2>/dev/null | /usr/bin/awk 'NR == 2 {print $1}')
        if [ -n "$device" ]; then
            uuid=$(/usr/sbin/diskutil info -plist "$device" 2>/dev/null \
                | /usr/bin/plutil -extract VolumeUUID raw -o - - 2>/dev/null)
        fi ;;
    Linux)
        if command -v findmnt >/dev/null 2>&1; then
            uuid=$(findmnt -n -o UUID -T "$path" 2>/dev/null)
        fi ;;
esac
if [[ $uuid =~ ^[0-9A-Fa-f-]{36}$ ]]; then
    printf '{"volume_uuid":"%s"}\n' "$uuid"
else
    printf '{"volume_uuid":null}\n'
fi
