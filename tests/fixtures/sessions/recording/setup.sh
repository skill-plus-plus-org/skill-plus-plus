#!/bin/sh
# Prepare the folder for one recorded session (see ../RECORDING.md).
#
#     tests/fixtures/sessions/recording/setup.sh C-F1
#     tests/fixtures/sessions/recording/setup.sh C-F1 /some/other/root
#
# Code sessions get a fresh copy of the seed repo, committed once by a neutral
# author, so the developer's name never reaches a transcript. Procedure
# sessions get exactly the files their prompts read, and the deck sessions
# copy skillpp's own docs, so every run reads the same text.
set -eu

if [ $# -lt 1 ]; then
    echo "usage: $0 <session-id> [root]" >&2
    exit 2
fi
id="$1"
root="${2:-$HOME/skillpp-recordings}"
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../../.." && pwd)"
dest="$root/$id"

case "$id" in
    C-F1|C-F2|C-F3|C-V1|C-V2|C-V3|C-N1|C-2said|C-2unsaid|C-3|C-same|L1) kind=code ;;
    P-F1|P-F2|P-F3|P-V1|P-V2|P-V3|P-N1|P-2said|P-2unsaid|P-3) kind=procedure ;;
    *) echo "unknown session id: $id" >&2; exit 2 ;;
esac

if [ -e "$dest" ]; then
    echo "$dest already exists; remove it to record this session again" >&2
    exit 1
fi
mkdir -p "$dest"

if [ "$kind" = code ]; then
    cp -R "$here/seed-repo/." "$dest/"
    (
        cd "$dest"
        git init -q
        git config user.name "Recorder"
        git config user.email "recorder@example.com"
        git config commit.gpgsign false
        git add -A
        git -c core.hooksPath=/dev/null commit -q -m "Initial textkit"
    )
else
    case "$id" in
        P-F1|P-F2|P-F3) cp "$here/notes.md" "$dest/" ;;
        P-V1) cp "$repo/README.md" "$dest/" ;;
        P-V2) mkdir -p "$dest/docs"; cp "$repo/docs/usage.md" "$dest/docs/" ;;
        P-V3) mkdir -p "$dest/docs"; cp "$repo/docs/architecture.md" "$dest/docs/" ;;
        P-N1) cp "$here/CHANGELOG.md" "$dest/" ;;
        P-2said) cp "$here/meeting-1.md" "$here/CHANGELOG.md" "$dest/" ;;
        P-2unsaid) mkdir -p "$dest/docs"; cp "$repo/docs/privacy.md" "$dest/docs/"
                   cp "$here/meeting-2.md" "$dest/" ;;
        P-3) cp "$here/notes.md" "$here/meeting-3.md" "$here/CHANGELOG.md" "$dest/" ;;
    esac
fi

echo "ready: $dest"
