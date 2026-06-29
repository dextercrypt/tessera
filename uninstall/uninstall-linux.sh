#!/usr/bin/env bash
# ========================================================================
# tess - Linux uninstall script
#
# Removes everything setup-linux.sh installed.
# ========================================================================

set -uo pipefail

echo
echo "=== tess uninstall (Linux) ==="
echo

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/tess"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tess"
WRAPPER="$HOME/.local/bin/tess"
UNIT_FILE="$HOME/.config/systemd/user/tess-stop.service"

# ---- 1. Stop any active session ----
if [ -x "$WRAPPER" ]; then
    echo "Stopping any active session..."
    "$WRAPPER" stop >/dev/null 2>&1 || true
fi

# ---- 2. Disable and remove systemd user unit ----
if [ -f "$UNIT_FILE" ]; then
    echo "Removing systemd user unit..."
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user stop tess-stop.service 2>/dev/null || true
        systemctl --user disable tess-stop.service 2>/dev/null || true
    fi
    rm -f "$UNIT_FILE"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload 2>/dev/null || true
    fi
fi

# ---- 3. Remove env-var blocks from shell rc files ----
remove_env_block() {
    local rc="$1"
    if [ -f "$rc" ] && grep -q "# BEGIN tess env" "$rc"; then
        sed -i '/# BEGIN tess env/,/# END tess env/d' "$rc"
        echo "Removed env block from $rc"
    fi
}
remove_env_block "$HOME/.profile"
remove_env_block "$HOME/.bashrc"

# Also remove PATH-extension lines if we added them
remove_path_line() {
    local rc="$1"
    if [ -f "$rc" ]; then
        if grep -B1 '/.local/bin' "$rc" 2>/dev/null | grep -q "# Added by tess setup"; then
            sed -i '/# Added by tess setup/,+1d' "$rc"
        fi
    fi
}
remove_path_line "$HOME/.profile"
remove_path_line "$HOME/.bashrc"

# ---- 4. Remove the wrapper ----
if [ -f "$WRAPPER" ]; then
    rm -f "$WRAPPER"
    echo "Removed wrapper: $WRAPPER"
fi

# ---- 5. Delete the data directory (includes venv) ----
if [ -d "$DATA_DIR" ]; then
    echo "Removing data directory: $DATA_DIR"
    rm -rf "$DATA_DIR"
fi

# ---- 6. Clean the config dir: remove our template + env.sh. Preserve a
# user-authored tess-config.json (it's their data, not ours). ----
if [ -d "$CONFIG_DIR" ]; then
    rm -f "$CONFIG_DIR/tess-config.example.json" "$CONFIG_DIR/env.sh"
    if rmdir "$CONFIG_DIR" 2>/dev/null; then
        echo "Removed config directory: $CONFIG_DIR"
    else
        echo "Kept config directory (still contains your tess-config.json): $CONFIG_DIR"
    fi
fi

echo
echo "=== Uninstall complete ==="
echo
echo "Notes:"
echo "  - Restart your terminal or open a new one to clear cached env vars."
echo "  - Log out and back in to fully clear env vars from GUI apps."
echo "  - Python itself was not uninstalled (it was already on your system)."
echo
