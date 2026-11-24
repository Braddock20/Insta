#!/usr/bin/env bash
# InstaBaileys — install.
#
# Creates a virtual environment and installs the two dependencies. There is
# no browser to download: that is the whole point.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 is not installed. Install Python 3.10 or newer and try again." >&2
    exit 1
fi

VERSION=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python $VERSION is too old. InstaBaileys needs Python 3.10 or newer." >&2
    exit 1
fi

echo "→ creating the virtual environment"
"$PYTHON" -m venv .venv

echo "→ installing the dependencies"
./.venv/bin/pip install --upgrade pip --quiet
./.venv/bin/pip install -e . --quiet

mkdir -p secrets state logs

if [ ! -f secrets/cookies.txt ]; then
    cat > secrets/cookies.txt <<'EOF'
Paste your Instagram cookie here, on one line.

The value of the "sessionid" cookie from your browser:
  chrome://settings/cookies  ->  or  F12 -> Application -> Cookies
You can also get the whole thing from the console:  copy(document.cookie)
EOF
    chmod 600 secrets/cookies.txt
    echo "→ wrote a template to secrets/cookies.txt (put your cookie in it)"
fi

cat <<'EOF'

Done. Next:

  .venv/bin/python -m igbot login     # check the cookie
  .venv/bin/python -m igbot doctor    # check everything
  .venv/bin/python -m igbot watch     # run the bot

The cookie file is yours alone — it is a full login to your account.
EOF
