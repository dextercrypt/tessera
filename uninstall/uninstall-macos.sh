#!/usr/bin/env bash
# ========================================================================
# tess - macOS uninstall script
#
# Removes everything setup-macos.sh installed.
# ========================================================================

set -uo pipefail

echo
echo "=== tess uninstall (macOS) ==="
echo

DATA_DIR="$HOME/Library/Application Support/tess"
CONFIG_DIR="$HOME/.config/tess"
WRAPPER="$HOME/.local/bin/tess"
AGENT_DIR="$HOME/Library/LaunchAgents"
ENV_AGENT_PLIST="$AGENT_DIR/com.tess.env.plist"
OLD_AGENT_PLIST="$AGENT_DIR/com.tess.stop-on-logout.plist"

# ---- 1. Stop any active session ----
if [ -x "$WRAPPER" ]; then
    echo "Stopping any active session..."
    "$WRAPPER" stop >/dev/null 2>&1 || true
fi

# ---- 2. Unload and remove LaunchAgents (env-var agent + any legacy agent) ----
for plist in "$ENV_AGENT_PLIST" "$OLD_AGENT_PLIST"; do
    if [ -f "$plist" ]; then
        echo "Removing LaunchAgent: $plist"
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist"
    fi
done

# Clear the GUI-session env vars set via `launchctl setenv` (best effort;
# they also clear automatically on next logout).
for v in AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_SESSION_NAME \
         AWS_STS_REGIONAL_ENDPOINTS AWS_REGION AWS_DEFAULT_REGION; do
    launchctl unsetenv "$v" 2>/dev/null || true
done

# ---- 3. Remove env-var blocks from shell rc files ----
remove_env_block() {
    local rc="$1"
    if [ -f "$rc" ] && grep -q "# BEGIN tess env" "$rc"; then
        # macOS sed needs '' after -i
        sed -i '' '/# BEGIN tess env/,/# END tess env/d' "$rc"
        echo "Removed env block from $rc"
    fi
}
remove_env_block "$HOME/.zshrc"
remove_env_block "$HOME/.zprofile"
remove_env_block "$HOME/.bash_profile"

# Also remove the PATH-extension lines we added (if they look like ours)
remove_path_line() {
    local rc="$1"
    if [ -f "$rc" ]; then
        # Remove only if preceded by our marker
        if grep -B1 '/.local/bin' "$rc" 2>/dev/null | grep -q "# Added by tess setup"; then
            # Portable across BSD (macOS) and GNU sed: on the marker line,
            # pull in the next line (N) and delete both (d). BSD sed does not
            # support GNU's `,+1d` address form.
            sed -i '' -e '/# Added by tess setup/{' -e 'N' -e 'd' -e '}' "$rc"
        fi
    fi
}
remove_path_line "$HOME/.zshrc"
remove_path_line "$HOME/.zprofile"
remove_path_line "$HOME/.bash_profile"

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
