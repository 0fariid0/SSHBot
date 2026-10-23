#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo bash.' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'Python 3 is required (Ubuntu 22.04/24.04).' >&2; exit 1; }
TEMP_SOURCE=''
trap '[[ -z "$TEMP_SOURCE" ]] || rm -rf -- "$TEMP_SOURCE"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
SOURCE=''
# BASH_SOURCE is empty when invoked as curl ... | bash.
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then
    SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fi
if [[ -z "$SOURCE" || ! -f "$SOURCE/deploy/manager.py" || ! -f "$SOURCE/web/app.py" || ! -f "$SOURCE/requirements.txt" ]]; then
    command -v curl >/dev/null || { echo 'curl is required.' >&2; exit 1; }
    TEMP_SOURCE=$(mktemp -d /tmp/sshbot-install.XXXXXXXX)
    SOURCE="$TEMP_SOURCE/source"
    echo 'Downloading SSHBot Web from 0fariid0/SSHBot (main)...'
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --retry 3 --connect-timeout 15 --max-time 180 \
        https://codeload.github.com/0fariid0/SSHBot/tar.gz/refs/heads/main \
        --output "$TEMP_SOURCE/source.tar.gz"
    python3 - "$TEMP_SOURCE/source.tar.gz" "$SOURCE" <<'PY'
import pathlib, sys, tarfile
archive, destination = sys.argv[1:]
root = pathlib.Path(destination)
root.mkdir(mode=0o700)
with tarfile.open(archive, 'r:gz') as tar:
    members = tar.getmembers()
    total = 0
    for member in members:
        p = pathlib.PurePosixPath(member.name)
        if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] != 'SSHBot-main':
            raise SystemExit('Unsafe archive path; installation stopped.')
        if not (member.isfile() or member.isdir()):
            raise SystemExit('Archive contains a link or special file; installation stopped.')
        total += member.size
        if total > 100 * 1024 * 1024:
            raise SystemExit('Archive exceeds installation size limit.')
    for member in members:
        relative = pathlib.PurePosixPath(member.name).parts[1:]
        if not relative:
            continue
        target = root.joinpath(*relative)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, target.open('wb') as dst:
                import shutil
                shutil.copyfileobj(src, dst)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
PY
fi
for required in deploy/manager.py web/app.py requirements.txt; do
    [[ -f "$SOURCE/$required" ]] || {
        echo "Missing $required. Upload the complete new package to the root of GitHub main first." >&2
        exit 1
    }
done
if [[ " ${*} " == *' --status '* ]]; then
    python3 "$SOURCE/deploy/manager.py" --source "$SOURCE" "$@"
else
    # Never let menu input consume the downloaded shell script on stdin.
    if ! { true </dev/tty; } 2>/dev/null; then
        echo 'An interactive terminal is required. Connect with ssh -t, then run the installer.' >&2
        exit 1
    fi
    python3 "$SOURCE/deploy/manager.py" --source "$SOURCE" "$@" </dev/tty
fi
