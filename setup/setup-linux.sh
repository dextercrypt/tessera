#!/usr/bin/env bash
# ========================================================================
# tess - Linux setup script  (bootstrap only)
#
# Run once per laptop. Creates a dedicated venv, installs dependencies,
# copies the program in, creates the config dir, drops the config template,
# sets ONLY the constant environment variables, wires the env.sh source lines
# (for the changeable vars `tess start` writes), and registers the systemd
# user unit for logout cleanup. It never prompts for any values.
# ========================================================================

set -euo pipefail

echo
echo "=== tess setup (Linux) ==="
echo

# ---- 1. Verify Python 3.10+ is installed ----
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is not on PATH."
    echo "Install Python 3.10 or newer via your distribution's package manager."
    echo "  Debian/Ubuntu: sudo apt install python3 python3-venv"
    echo "  Fedora:        sudo dnf install python3"
    exit 1
fi

PY_VER=$(python3 --version 2>&1 | awk '{print $2}')
echo "Found Python $PY_VER"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "ERROR: Python 3.10 or newer is required. Found $PY_VER."
    exit 1
}

# Some distros split out venv into a separate package
if ! python3 -c "import venv" 2>/dev/null; then
    echo "ERROR: Python venv module is not available."
    echo "  Debian/Ubuntu: sudo apt install python3-venv"
    exit 1
fi

# ---- 2. Locate the program source: local ../src, else fetch the pinned release ----
# Bump TESS_REF when cutting a new release; override (e.g. TESS_REF=main) to test.
TESS_REF="${TESS_REF:-v1.0.0}"
RAW_BASE="https://raw.githubusercontent.com/dextercrypt/tessera/${TESS_REF}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
LOCAL_SRC="$(cd "${SCRIPT_DIR:-.}/../src" 2>/dev/null && pwd || true)"

if [ -n "$LOCAL_SRC" ] && [ -f "$LOCAL_SRC/tess.py" ] \
   && [ -f "$LOCAL_SRC/requirements.txt" ] && [ -f "$LOCAL_SRC/tess-config.example.json" ]; then
    SRC_DIR="$LOCAL_SRC"
    echo "Using local source: $SRC_DIR"
else
    # Standalone run (e.g. piped from curl) — fetch the pinned, checksum-verified
    # release into a throwaway temp dir, verify BEFORE anything is installed.
    echo "No local ../src — fetching tess $TESS_REF from GitHub..."
    command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required to download tess."; exit 1; }
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    mkdir -p "$STAGE/src"
    for f in src/tess.py src/requirements.txt src/tess-config.example.json SHA256SUMS; do
        curl -fsSL "$RAW_BASE/$f" -o "$STAGE/$f" \
            || { echo "ERROR: failed to download $f from $RAW_BASE"; exit 1; }
    done
    echo "Verifying checksums..."
    if command -v sha256sum >/dev/null 2>&1; then
        ( cd "$STAGE" && sha256sum -c SHA256SUMS ) \
            || { echo "ERROR: checksum verification failed — aborting install."; exit 1; }
    else
        ( cd "$STAGE" && shasum -a 256 -c SHA256SUMS ) \
            || { echo "ERROR: checksum verification failed — aborting install."; exit 1; }
    fi
    SRC_DIR="$STAGE/src"
    echo "Verified tess $TESS_REF."
fi

# Welcome banner (rendered by Python so it is byte-identical on every OS).
python3 "$SRC_DIR/tess.py" _banner 2>/dev/null || true
echo

# ---- 3. Create data directory and venv ----
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/tess"
VENV_DIR="$DATA_DIR/venv"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tess"

# Detect whether this is a fresh install or an update of an existing one.
if [ -f "$DATA_DIR/tess.py" ]; then
    MODE="update"
else
    MODE="install"
fi

echo "Data directory:   $DATA_DIR"
echo "Config directory: $CONFIG_DIR"
if [ "$MODE" = "update" ]; then
    echo "Existing installation found → updating tess in place."
else
    echo "No existing installation → fresh install."
fi

if [ -d "$VENV_DIR" ]; then
    echo "Removing existing venv..."
    rm -rf "$VENV_DIR"
fi

mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"   # owner-only: holds the token + (fallback) plaintext cache

echo "Creating venv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"

# ---- 4. Install dependencies into the venv ----
echo "Installing dependencies into venv..."
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$SRC_DIR/requirements.txt"

# ---- 5. Copy tess.py into the data directory (config lives elsewhere) ----
# Remove any prior (possibly read-only) copy first — the 0700 dir is writable
# by us, so this succeeds regardless of the old file's mode — then copy fresh
# and lock to read+execute (0500) so the live script can't be edited by accident.
rm -f "$DATA_DIR/tess.py"
cp "$SRC_DIR/tess.py" "$DATA_DIR/tess.py"
chmod 500 "$DATA_DIR/tess.py"
echo "Copied tess.py to $DATA_DIR (read-only)"

# ---- 6. Create config dir and drop the template (never the real config) ----
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
cp "$SRC_DIR/tess-config.example.json" "$CONFIG_DIR/tess-config.example.json"
echo "Dropped config template: $CONFIG_DIR/tess-config.example.json"

# ---- 7. Create wrapper shell script on PATH ----
WRAPPER_DIR="$HOME/.local/bin"
mkdir -p "$WRAPPER_DIR"
WRAPPER="$WRAPPER_DIR/tess"

cat > "$WRAPPER" <<EOF
#!/bin/sh
exec "$VENV_DIR/bin/python" "$DATA_DIR/tess.py" "\$@"
EOF
chmod +x "$WRAPPER"
echo "Created wrapper: $WRAPPER"

# Ensure ~/.local/bin is on PATH
add_to_path_if_needed() {
    local rc="$1"
    local line='export PATH="$HOME/.local/bin:$PATH"'
    if [ -f "$rc" ] && grep -Fq '/.local/bin' "$rc"; then
        return
    fi
    [ -f "$rc" ] || touch "$rc"
    echo "" >> "$rc"
    echo "# Added by tess setup" >> "$rc"
    echo "$line" >> "$rc"
}
add_to_path_if_needed "$HOME/.profile"
add_to_path_if_needed "$HOME/.bashrc"

# ---- 8. Set the constant env vars + wire env.sh for the changeable ones ----
# Constants (token path, regional STS) never change for the life of the install
# and are written directly here. The changeable vars (role ARN, region, session
# name) are written by `tess start` into $CONFIG_DIR/env.sh; we source that file
# from both ~/.profile (the display manager sources it into the GUI session, so
# GUI apps like IntelliJ inherit the vars) and ~/.bashrc (terminals).
TOKEN_FILE="$DATA_DIR/token"
ENV_SH="$CONFIG_DIR/env.sh"

set_constants_in_file() {
    local rc="$1"; local hook="${2:-0}"
    [ -f "$rc" ] || touch "$rc"
    if grep -q "# BEGIN tess env" "$rc"; then
        sed -i '/# BEGIN tess env/,/# END tess env/d' "$rc"
    fi
    cat >> "$rc" <<EOF

# BEGIN tess env
export AWS_WEB_IDENTITY_TOKEN_FILE="$TOKEN_FILE"
export AWS_STS_REGIONAL_ENDPOINTS="regional"
# Changeable vars (role/region/identity) are written here by \`tess start\`:
[ -f "$ENV_SH" ] && . "$ENV_SH"
EOF
    if [ "$hook" = "1" ]; then
        # Re-source before each prompt so an ALREADY-OPEN terminal picks up a
        # new \`tess start\` on its next prompt — no manual \`source\` needed.
        # The case guard keeps it out of PROMPT_COMMAND if already present.
        cat >> "$rc" <<EOF
_tess_reload_env() { [ -f "$ENV_SH" ] && . "$ENV_SH"; }
case "\${PROMPT_COMMAND:-}" in *_tess_reload_env*) ;; *) PROMPT_COMMAND="_tess_reload_env;\${PROMPT_COMMAND:-}";; esac
EOF
    fi
    echo "# END tess env" >> "$rc"
}
# .profile bridges to the GUI session + login shells (one-time source is enough);
# .bashrc is the interactive terminal file (gets the per-prompt reload hook).
set_constants_in_file "$HOME/.profile" 0
set_constants_in_file "$HOME/.bashrc" 1

echo "Set constant env vars and wired $ENV_SH into ~/.profile and ~/.bashrc."
echo "NOTE: GUI env propagation is display-manager-dependent. On modern"
echo "      Wayland/GDM you may instead need ~/.config/environment.d/."

# ---- 9. Register systemd user unit for logout cleanup ----
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT_FILE="$UNIT_DIR/tess-stop.service"

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=tess - stop AWS session on logout
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
ExecStop=$WRAPPER stop

[Install]
WantedBy=default.target
EOF

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user enable tess-stop.service >/dev/null 2>&1 || {
        echo "WARNING: Could not enable systemd user service."
        echo "         The 8-hour cap will still apply."
    }
    systemctl --user start tess-stop.service >/dev/null 2>&1 || true
    echo "Registered systemd user unit: tess-stop.service"
else
    echo "WARNING: systemctl not found. Logout-stop hook is not active."
    echo "         The 8-hour cap will still apply."
fi

# ---- Done ----
echo
if [ "$MODE" = "update" ]; then
    echo "=== Update complete ==="
    echo
    echo "tess was already installed — the program was updated in place."
    echo "If a session is currently running, reload the new code with:"
    echo "  tess stop && tess start"
else
    echo "=== Setup complete ==="
fi
echo
if [ -f "$CONFIG_DIR/tess-config.json" ]; then
    echo "Config: tess-config.json already present at $CONFIG_DIR (left untouched)."
else
    echo "Config: no tess-config.json yet. Create it once:"
    echo "  cp \"$CONFIG_DIR/tess-config.example.json\" \"$CONFIG_DIR/tess-config.json\""
    echo "  then edit: tenant_id, client_id, role_arn, region"
    echo "(Or have it delivered to that path by your org, or pass --config / set \$TESS_CONFIG.)"
fi
echo
echo "Next steps:"
echo "  1. Quit and reopen your terminal (so PATH and env vars refresh)."
echo "  2. Log out and back in once (so GUI apps pick up env vars)."
echo "  3. After logging back in, run: tess start"
echo
