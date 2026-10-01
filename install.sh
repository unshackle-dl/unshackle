#!/bin/sh
# Install uv and unshackle, then run `unshackle setup`.
# In a clone it runs `uv sync`. Alone (or piped from curl) it runs `uv tool install`.
set -eu

REPO="git+https://github.com/unshackle-dl/unshackle.git"

if ! command -v uv >/dev/null 2>&1; then
    echo "[..] uv not found. Installing..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        wget -qO- https://astral.sh/uv/install.sh | sh
    fi
    for dir in "$HOME/.local/bin" "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin}" "${XDG_BIN_HOME:-}"; do
        if [ -n "$dir" ]; then PATH="$dir:$PATH"; fi
    done
    export PATH
fi
echo "[OK] $(uv --version)"

run_setup() {
    if [ -f "$0" ]; then
        "$@" setup
    else
        "$@" setup </dev/tty
    fi
}

DIR=$(cd "$(dirname "$0")" && pwd)
if [ -f "$DIR/pyproject.toml" ] && grep -q '^name = "unshackle"' "$DIR/pyproject.toml"; then
    cd "$DIR"
    uv sync --compile-bytecode
    run_setup uv run unshackle
    NOTE=""
else
    uv tool install --compile-bytecode "$REPO"
    uv tool update-shell || true
    run_setup "$(uv tool dir --bin)/unshackle"
    NOTE="Open a new terminal so that the unshackle command is on your PATH."
fi

echo
echo "Installation completed successfully."
if [ -n "$NOTE" ]; then
    echo "$NOTE"
fi
