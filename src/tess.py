#!/usr/bin/env python3
"""
tess — federated AWS credentials for developer laptops.

A CLI tool that gives a laptop the same AWS credential behavior that pods
get from IRSA: short-lived federated credentials sourced from a corporate
OIDC identity provider, with no static keys and no per-application config.

The script implements the subcommands start, stop, status, refresh, logs,
config, and version, plus two hidden modes (_refresh-daemon, revelio), and a
background refresher process that keeps an OIDC id_token fresh in a known file
for the AWS SDK to consume.

See DESIGN.md for the full architecture.
"""

import argparse
import base64
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "1.0.0"

# ---------- Branding ----------
# Pure 7-bit ASCII wordmark (figlet "standard") so it renders identically on
# Windows (cmd/PowerShell/conhost), macOS, and Linux. No Unicode, no color.
WORDMARK = r"""
 _
| |_ ___  ___ ___  ___ _ __ __ _
| __/ _ \/ __/ __|/ _ \ '__/ _` |
| ||  __/\__ \__ \  __/ | | (_| |
 \__\___||___/___/\___|_|  \__,_|
"""
TAGLINE = "federated AWS credentials for laptops"
# Why the name. Pre-wrapped to ~72 cols; shown as the --help epilog and in
# `tess version`. Plain text (no markdown) since it renders in a terminal.
NAME_STORY = (
    "Named for the Roman tessera — the watchword token passed around camp,\n"
    "rotated so a stale one was worthless. tess keeps your AWS identity on\n"
    "the same short clock: fresh each day, useless once it goes stale."
)


def banner_text() -> str:
    """The ASCII wordmark plus tagline, as a single string."""
    return WORDMARK.strip("\n") + "\n " + TAGLINE


def print_banner(force: bool = False, stream=None, with_story: bool = False):
    """Print the wordmark banner. Suppressed when output is not a terminal
    (piped/redirected), unless force=True (explicit `_banner` / setup).

    with_story=True appends the name-story directly under the tagline — used by
    the setup splash so the story shows up top. (help/version print the story at
    the bottom instead.)"""
    stream = stream or sys.stdout
    if not force and not stream.isatty():
        return
    out = banner_text()
    if with_story:
        out += "\n\n" + NAME_STORY
    stream.write(out + "\n\n")


# Refresher behavior (configurable via tess-config.json)
DEFAULT_REFRESH_INTERVAL_MINUTES = 50
DEFAULT_SESSION_MAX_HOURS = 8
FAILURE_BACKOFF_SECONDS = 300            # retry every 5 min on failure
FAILURE_GIVE_UP_AFTER_SECONDS = 1800     # give up after 30 min of failures
REFRESH_SIGNAL_CHECK_SECONDS = 5         # how often daemon checks for refresh signal


# ---------- Platform-specific paths ----------

def data_dir() -> Path:
    """Return the per-platform data directory for tess."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(base) / "tess"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tess"
    else:
        return Path.home() / ".local" / "share" / "tess"


def token_path() -> Path:
    return data_dir() / "token"


def pid_path() -> Path:
    return data_dir() / "daemon.pid"


def session_started_path() -> Path:
    return data_dir() / "session.started"


def session_config_path() -> Path:
    """Path of the file recording which config the active session resolved to.

    Written once at `tess start`. All later commands in the session read this
    rather than re-resolving the ladder from their own working directory.
    """
    return data_dir() / "session.config"


def log_path() -> Path:
    return data_dir() / "daemon.log"


def refresh_signal_path() -> Path:
    return data_dir() / "refresh.signal"


def msal_cache_path() -> Path:
    return data_dir() / "msal_cache.bin"


def config_dir() -> Path:
    """Return the per-platform config directory for tess.

    Deliberately SEPARATE from data_dir(): a tool upgrade re-copies the program
    dir, so config kept there would be clobbered. This is also the OS-conventional
    config location (%APPDATA% / XDG_CONFIG_HOME / ~/.config).
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / "tess"
    elif sys.platform == "darwin":
        return Path.home() / ".config" / "tess"
    else:
        base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(base) / "tess"


CONFIG_FILENAME = "tess-config.json"


def default_config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def env_sh_path() -> Path:
    """Linux only: the file `tess start` writes with the changeable env vars,
    sourced from ~/.profile and ~/.bashrc by setup."""
    return config_dir() / "env.sh"


# ---------- Configuration ----------

# Keys the config MUST contain. tenant_id/client_id drive MSAL; role_arn is the
# AWS role to assume. region is optional (its absence is warned about, not fatal);
# refresh_interval_minutes / session_max_hours fall back to defaults.
REQUIRED_CONFIG_KEYS = ("tenant_id", "client_id", "role_arn")


class ConfigError(Exception):
    """Raised when config cannot be located, parsed, or validated.

    Carries a user-facing message; command entry points catch it, print the
    message to stderr, and exit non-zero.
    """


def resolve_config_path(explicit: str | None) -> tuple[Path, str]:
    """Resolve which config file to use, returning (path, rung_label).

    The ladder (first match wins):
      1. --config <path>  — explicit flag, any filename. Missing → error.
      2. $TESS_CONFIG     — explicit env var, any path.   Missing → error.
      3. ./tess-config.json (current dir)                 Absent  → fall through.
      4. <config-dir>/tess-config.json (global default)   Absent  → fall through.
      5. none found → ConfigError listing every location searched.

    Explicit rungs (1–2) error on miss so a typo isn't silently masked; implicit
    rungs (3–4) fall through. Existence only — contents are validated by
    load_config().
    """
    if explicit is not None:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"--config path does not exist: {p}")
        return p.resolve(), "--config flag"

    env_val = os.environ.get("TESS_CONFIG")
    if env_val:
        p = Path(env_val).expanduser()
        if not p.is_file():
            raise ConfigError(
                f"$TESS_CONFIG points to a file that does not exist: {p}"
            )
        return p.resolve(), "$TESS_CONFIG"

    cwd_candidate = Path.cwd() / CONFIG_FILENAME
    if cwd_candidate.is_file():
        return cwd_candidate.resolve(), "current dir"

    default_candidate = default_config_path()
    if default_candidate.is_file():
        return default_candidate.resolve(), "config-dir default"

    raise ConfigError(
        "no tess-config.json found. Searched:\n"
        f"  - $TESS_CONFIG (unset)\n"
        f"  - {cwd_candidate}\n"
        f"  - {default_candidate}\n"
        "Copy the example template and fill it in:\n"
        f"  cp {config_dir() / 'tess-config.example.json'} {default_candidate}\n"
        "then edit tenant_id, client_id, role_arn, region."
    )


# Format validators. Besides catching typos, these REJECT shell metacharacters
# (quotes, $, ;, spaces, slashes) in the values that get written to env.sh and
# the MSAL authority URL — closing those injection vectors at the source.
_CONFIG_FORMATS = {
    "tenant_id": (r"^[A-Za-z0-9.-]+$", "a GUID or domain"),
    "client_id": (r"^[A-Za-z0-9-]+$", "a GUID"),
    "role_arn": (r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$",
                 "an IAM role ARN (arn:aws:iam::<account>:role/<name>)"),
}


def validate_config(data: dict, path: Path) -> dict:
    """Validate parsed config contents: required keys present, and each value
    matches a strict format. Raises ConfigError on any problem."""
    if not isinstance(data, dict):
        raise ConfigError(f"config is not a JSON object: {path}")
    missing = [k for k in REQUIRED_CONFIG_KEYS if not data.get(k)]
    if missing:
        raise ConfigError(
            f"config is missing required key(s) {', '.join(missing)}: {path}"
        )
    for key, (pattern, desc) in _CONFIG_FORMATS.items():
        if not re.match(pattern, str(data[key])):
            raise ConfigError(f"config '{key}' is not valid — expected {desc}: {path}")
    region = data.get("region")
    if region is not None and region != "" and not re.match(r"^[a-z0-9-]+$", str(region)):
        raise ConfigError(f"config 'region' is not a valid AWS region name: {path}")
    for key in ("refresh_interval_minutes", "session_max_hours"):
        if key in data:
            try:
                if float(data[key]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ConfigError(f"config '{key}' must be a positive number: {path}")
    return data


def load_config(path: Path) -> dict:
    """Read, parse, and validate the config file at the given path."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"config is not valid JSON ({path}): {e}") from e
    return validate_config(data, path)


def session_or_resolved_config(explicit: str | None) -> tuple[Path, str]:
    """Return (path, rung) for a non-start command.

    During an active session, the recorded session.config path wins (commands
    must not re-resolve from their own CWD). Otherwise fall back to the ladder.
    An explicit --config always re-resolves.
    """
    if explicit is None:
        sc = session_config_path()
        if sc.is_file():
            recorded = sc.read_text().strip()
            if recorded:
                return Path(recorded), "session (recorded at start)"
    return resolve_config_path(explicit)


# ---------- MSAL setup ----------

def build_msal_app(config: dict, clear_cache: bool = False):
    """
    Build an MSAL PublicClientApplication with a keychain-encrypted token cache.

    If clear_cache=True, the cache is wiped before initialization. This is
    used by `start` to enforce fresh MFA every morning.
    """
    from msal import PublicClientApplication

    cache_file = msal_cache_path()
    if clear_cache and cache_file.exists():
        cache_file.unlink()

    # Try to use msal-extensions for OS-keychain-encrypted persistence.
    # Fall back to a plain SerializableTokenCache if msal-extensions isn't
    # available or the platform's keychain isn't reachable (rare on real
    # corporate laptops, but possible on minimal Linux setups).
    try:
        from msal_extensions import (
            PersistedTokenCache,
            build_encrypted_persistence,
        )
        persistence = build_encrypted_persistence(str(cache_file))
        cache = PersistedTokenCache(persistence)
    except Exception:
        from msal import SerializableTokenCache
        cache = SerializableTokenCache()
        sys.stderr.write(
            "WARNING: OS keychain unavailable — the MSAL token cache will be\n"
            f"  stored UNENCRYPTED at {cache_file} (locked to owner-only, 0600).\n"
            "  Expected on minimal Linux without libsecret/Keyring installed.\n"
        )
        if cache_file.exists():
            try:
                cache.deserialize(cache_file.read_text())
            except Exception:
                pass  # corrupt cache; start fresh

    authority = f"https://login.microsoftonline.com/{config['tenant_id']}"
    app = PublicClientApplication(
        config["client_id"],
        authority=authority,
        token_cache=cache,
    )
    return app, cache


def resolve_scopes(config: dict) -> list:
    """Decide which OAuth scopes to request, both interactively and on refresh.

    Why this matters: MSAL is access-token-centric — the OIDC id_token tess
    needs is a by-product of a *token request for a scope*. With an empty scope
    list, interactive sign-in still returns a (minimal) id_token, but the silent
    REFRESH path has nothing to request and returns no id_token — so the token
    can't be renewed. Requesting the app's own resource fixes that. The id_token
    audience stays the client_id either way, so AWS trust is unaffected.

    The exact scope depends on the Entra app registration, so it's configurable:
      - config "scope" present  → use it (space-separated string, or a list).
        Set it to "" to force the legacy empty-scope behaviour.
      - config "scope" absent   → default to "<client_id>/.default".
    """
    if "scope" in config:
        s = config["scope"]
        return list(s) if isinstance(s, list) else str(s).split()
    return [f"{config['client_id']}/.default"]


def save_plain_cache_if_needed(cache):
    """For the fallback SerializableTokenCache, persist to disk manually.

    Written via atomic_write at 0600 so the refresh token in the plaintext
    cache is never readable by other local users (default write_text() would
    have left it world-readable at 0644)."""
    from msal import SerializableTokenCache
    if isinstance(cache, SerializableTokenCache) and cache.has_state_changed:
        try:
            atomic_write(msal_cache_path(), cache.serialize(), mode=0o600)
        except TokenWriteError:
            pass  # best-effort cache persistence; not fatal to the session


def clear_msal_cache():
    """Delete the MSAL cache file. Best-effort."""
    try:
        msal_cache_path().unlink(missing_ok=True)
    except Exception:
        pass


# ---------- JWT decoding (for status/info, never for validation) ----------

def decode_jwt_payload(jwt: str) -> dict:
    """
    Decode the payload of a JWT for display purposes only.
    Does NOT validate the signature — AWS STS does that.
    """
    try:
        parts = jwt.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # pad base64
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _fmt_exp(exp) -> str:
    """Format a JWT exp (Unix timestamp) as a local HH:MM:SS string for logs."""
    if not exp:
        return "?"
    try:
        return datetime.fromtimestamp(exp).strftime("%H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(exp)


# ---------- Atomic file writes ----------

class TokenWriteError(Exception):
    """Raised when the token/state file cannot be written or replaced.

    Distinct from authentication failures so callers (the daemon) can react
    appropriately — a failed file write is not a reason to tell the user to
    re-authenticate.
    """


def atomic_write(path: Path, content: str, mode: int = 0o600):
    """Write content to path atomically (temp file + rename).

    The temp file is created with the target mode from the start (via os.open)
    so the token is never briefly world-readable between write and chmod.

    On Windows, os.replace can transiently fail with PermissionError if another
    process (e.g. the AWS SDK reading the token) holds the destination open
    without share-delete. We retry a few times before giving up.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")

    data = content.encode("utf-8")
    # O_NOFOLLOW (where supported) refuses to write through a symlink planted at
    # the temp path — defense-in-depth on top of the 0700 parent dir. It's a
    # no-op flag (0) on platforms without it, e.g. Windows.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(tmp), flags, mode)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError as e:
        raise TokenWriteError(f"could not write temp file {tmp}: {e}") from e

    last_err: Exception | None = None
    attempts = 10 if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            # Windows: destination momentarily locked by a reader. Back off.
            last_err = e
            time.sleep(0.1)
        except OSError as e:
            last_err = e
            break

    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise TokenWriteError(f"could not replace {path}: {last_err}") from last_err


def ensure_private_dir(path: Path) -> Path:
    """Create a directory if needed and lock it to owner-only (0700).

    Keeps other local users from enumerating or reading its contents (the token
    and, on the no-keychain fallback, the plaintext MSAL cache). chmod is
    best-effort — on Windows POSIX modes are largely cosmetic, but the per-user
    AppData/LocalAppData location is already ACL-scoped to the user.
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def safe_display(value, limit: int = 256) -> str:
    """Sanitize a value parsed from the token before printing it to a terminal
    or writing it to the log.

    tess decodes the JWT for display WITHOUT verifying its signature (STS does
    that), so claims like `name`/`upn` are treated as untrusted: we strip
    non-printable characters — defusing ANSI/terminal-escape injection when
    printed and CR/LF log-line forgery when logged — and cap the length.
    """
    return "".join(ch for ch in str(value) if ch.isprintable())[:limit]


def log_event(message: str, level: str = "INFO"):
    """Append one timestamped line to the audit log (daemon.log).

    Used by foreground commands (start/stop) so session-lifecycle events land in
    the SAME file the daemon writes its refresh events to — giving one
    chronological audit trail. A short-lived O_APPEND write (atomic for small
    lines) rather than a logging handler, so it never contends with the daemon's
    rotating handler. Format matches the daemon's so the file reads uniformly.
    """
    try:
        ensure_private_dir(log_path().parent)
        # Collapse any CR/LF so a single event can never become multiple log
        # lines (final guard on top of per-value sanitization at call sites).
        message = message.replace("\r", " ").replace("\n", " ")
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {message}\n"
        fd = os.open(str(log_path()), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass  # logging must never break the command


# ---------- Process management ----------

def is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID exists.

    NOTE: on Windows we must NOT use os.kill(pid, 0). Unlike Unix, Windows
    has no signal-0 probe — CPython implements os.kill for any signal other
    than CTRL_C_EVENT/CTRL_BREAK_EVENT as OpenProcess + TerminateProcess,
    so os.kill(pid, 0) would *terminate* the process instead of checking it.
    We query the process state via the Win32 API instead.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_process_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_process_alive_windows(pid: int) -> bool:
    """Windows-only liveness check via OpenProcess + GetExitCodeProcess."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32
    # Set signatures explicitly so 64-bit HANDLEs aren't truncated to int.
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No handle => process does not exist (or is fully gone). Either way,
        # not alive for our purposes.
        return False
    try:
        exit_code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        if not ok:
            return False
        # STILL_ACTIVE means running. Caveat: a process that genuinely exited
        # with code 259 reads as alive — negligible here (pythonw exits 0).
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill_process(pid: int):
    """Kill a process by PID. Uses taskkill on Windows, SIGTERM/SIGKILL on Unix."""
    if not is_process_alive(pid):
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for clean shutdown
            for _ in range(20):
                if not is_process_alive(pid):
                    return
                time.sleep(0.1)
            # Force kill if still alive
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def read_pid() -> int | None:
    """Read the PID file. Returns None if missing or invalid."""
    try:
        return int(pid_path().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_session_active() -> bool:
    """Check whether a session is currently active and the daemon is alive."""
    pid = read_pid()
    return pid is not None and is_process_alive(pid)


def session_age_seconds() -> float | None:
    """How long the current session has been alive, or None if no session."""
    try:
        started = float(session_started_path().read_text().strip())
        return time.time() - started
    except (FileNotFoundError, ValueError):
        return None


def cleanup_session_files():
    """Remove all session-related files. Idempotent."""
    for p in (token_path(), pid_path(), session_started_path(),
              session_config_path(), refresh_signal_path()):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


# ---------- Browser sign-in pages (MSAL success/error templates) ----------
# Shown in the browser after the IdP redirect. MSAL renders these with
# string.Template.safe_substitute, so $error / $error_description in the error
# page are filled with the actual failure (HTML-escaped by MSAL). The wordmark
# is injected from the single WORDMARK source via __WORDMARK__ to avoid drift.
# (Note: these only render for IdP-side outcomes that come back through the
# browser redirect; pre-redirect failures surface in the terminal instead.)

_AUTH_PAGE_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>tessera</title>
<style>
  body{ margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
        background:#f5f5f4; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .card{ background:#fff; padding:48px 64px; border-radius:18px; text-align:center;
         box-shadow:0 10px 40px rgba(0,0,0,.08); max-width:440px; }
  .badge{ width:48px; height:48px; border-radius:50%; color:#fff; display:flex;
          align-items:center; justify-content:center; font-size:25px; margin:0 auto 24px; }
  .ok{ background:#16a34a; } .err{ background:#dc2626; }
  .wordmark{ font-family:"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace; font-size:15px;
             line-height:1.15; white-space:pre; display:inline-block; text-align:left; color:#1c1917; margin:0; }
  .title{ margin-top:24px; font-size:16px; font-weight:600; color:#1c1917; }
  .msg{ margin-top:8px; font-size:14px; color:#78716c; }
  .detail{ margin-top:10px; font-size:13px; color:#b91c1c; background:#fef2f2; border:1px solid #fecaca;
           border-radius:8px; padding:10px 12px; word-break:break-word;
           font-family:"SF Mono",Menlo,Consolas,monospace; }
  code{ font-family:"SF Mono",Menlo,Consolas,monospace; background:#f5f5f4; padding:1px 6px; border-radius:5px; color:#1c1917; }
</style></head><body><div class="card">"""

_AUTH_PAGE_TAIL = "</div></body></html>"

AUTH_SUCCESS_TEMPLATE = (
    _AUTH_PAGE_HEAD
    + '<div class="badge ok">&#10003;</div>'
    + '<pre class="wordmark">__WORDMARK__</pre>'
    + '<div class="title">Authentication complete</div>'
    + '<div class="msg">You can close this window and return to your terminal.</div>'
    + _AUTH_PAGE_TAIL
).replace("__WORDMARK__", WORDMARK.strip("\n"))

AUTH_ERROR_TEMPLATE = (
    _AUTH_PAGE_HEAD
    + '<div class="badge err">&#10005;</div>'
    + '<pre class="wordmark">__WORDMARK__</pre>'
    + '<div class="title">Sign-in failed</div>'
    + '<div class="detail">$error: $error_description</div>'
    + '<div class="msg">Close this window and run <code>tess start</code> to try again.</div>'
    + _AUTH_PAGE_TAIL
).replace("__WORDMARK__", WORDMARK.strip("\n"))


# ---------- Subcommand: start ----------

def cmd_start(args):
    config_file, rung = resolve_config_path(args.config)
    config = load_config(config_file)
    # Always announce which config won, before doing any work.
    print(f"tess: config → {config_file}  ({rung})")
    ensure_private_dir(data_dir())

    # If a session is already active and we're not forcing, just print status
    if is_session_active() and not args.force:
        if not args.quiet:
            print("Session already active. Run `tess status` for details, or use --force to restart.")
        return 0

    # Force or stale state — clean up before proceeding
    if is_session_active():
        if not args.quiet:
            print("Stopping existing session...")
        _stop_internal(quiet=True)
    else:
        cleanup_session_files()

    # Approach A: clear MSAL cache to force fresh MFA every morning
    clear_msal_cache()

    if not args.quiet:
        print("Opening browser for authentication...")

    app, cache = build_msal_app(config, clear_cache=True)

    try:
        result = app.acquire_token_interactive(
            scopes=resolve_scopes(config),
            prompt="select_account",
            success_template=AUTH_SUCCESS_TEMPLATE,
            error_template=AUTH_ERROR_TEMPLATE,
        )
    except Exception as e:
        sys.stderr.write(f"ERROR: Authentication failed: {e}\n")
        return 1

    if not result or "id_token" not in result:
        err = result.get("error_description", str(result)) if result else "unknown error"
        sys.stderr.write(f"ERROR: Authentication failed: {err}\n")
        return 1

    save_plain_cache_if_needed(cache)

    id_token = result["id_token"]

    try:
        # Write the session-start timestamp BEFORE writing the token, so the
        # cap is enforceable from the moment the token becomes usable.
        atomic_write(session_started_path(), str(time.time()))
        # Record which config this session resolved to, so every later command
        # (and the daemon) reads the same file rather than re-resolving.
        atomic_write(session_config_path(), str(config_file), mode=0o644)
        # Write the id_token to the file the AWS SDK will read.
        atomic_write(token_path(), id_token)
    except TokenWriteError as e:
        sys.stderr.write(f"ERROR: could not write session files: {e}\n")
        cleanup_session_files()
        return 1

    # Inject the changeable env vars (AWS_ROLE_ARN, AWS_REGION, session name)
    # machine-wide so later-launched processes (IntelliJ, mvn) inherit them.
    inject_changeable_env(config, id_token, quiet=args.quiet)

    # Spawn the background refresher daemon, handing it the resolved config path.
    daemon_pid = spawn_daemon(config_file)
    try:
        atomic_write(pid_path(), str(daemon_pid), mode=0o644)
    except TokenWriteError as e:
        # We have a running daemon but nowhere to record its PID, which would
        # orphan it (no way to `tess stop` it). Kill it and abort cleanly.
        sys.stderr.write(f"ERROR: could not write PID file: {e}\n")
        kill_process(daemon_pid)
        cleanup_session_files()
        return 1

    if not args.quiet:
        print_session_banner(id_token, config)

    # Audit: record the interactive (MFA) sign-in that began this session.
    _p = decode_jwt_payload(id_token)
    _upn = safe_display(_p.get("preferred_username") or _p.get("upn") or "unknown")
    log_event(f"session started via interactive sign-in (MFA); user={_upn}; "
              f"token exp={_fmt_exp(_p.get('exp'))}; daemon pid={daemon_pid}")

    return 0


def spawn_daemon(config_file: Path) -> int:
    """Spawn the _refresh-daemon subprocess detached from this terminal."""
    # Use pythonw.exe on Windows to suppress the console window
    python_cmd = sys.executable
    if sys.platform == "win32":
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if os.path.exists(pythonw):
            python_cmd = pythonw

    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }

    if sys.platform == "win32":
        # DETACHED_PROCESS = 0x00000008, CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        # start_new_session detaches from the controlling terminal,
        # equivalent to setsid()
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [python_cmd, str(Path(__file__).resolve()),
         "_refresh-daemon", str(config_file)],
        **kwargs,
    )
    return proc.pid


def print_session_banner(id_token: str, config: dict):
    payload = decode_jwt_payload(id_token)
    upn = safe_display(payload.get("preferred_username") or payload.get("upn") or "unknown user")
    cap_hours = config.get("session_max_hours", DEFAULT_SESSION_MAX_HOURS)
    cap_time = datetime.fromtimestamp(time.time() + cap_hours * 3600)
    # %-I doesn't work on Windows; format then strip leading zero
    cap_str = cap_time.strftime("%I:%M %p").lstrip("0")

    print()
    print(f"Session active for: {upn}")
    print(f"Token refreshes automatically every "
          f"{config.get('refresh_interval_minutes', DEFAULT_REFRESH_INTERVAL_MINUTES)} minutes.")
    print(f"Session ends at {cap_str} "
          f"({cap_hours}-hour cap) or on logout, whichever first.")
    print()


# ---------- Environment-variable sync (Model B) ----------

# The env vars tess manages, split by who sets them. CONSTANTS are set once by
# the setup script; CHANGEABLE are (re)written by `tess start` from config+token.
# Listed here so `tess config` can report every value in one place.
CONSTANT_ENV_VARS = ("AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_STS_REGIONAL_ENDPOINTS")
CHANGEABLE_ENV_VARS = (
    "AWS_ROLE_ARN", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ROLE_SESSION_NAME",
)
MANAGED_ENV_VARS = CONSTANT_ENV_VARS + CHANGEABLE_ENV_VARS

# STS RoleSessionName allows alphanumerics plus these, max 64 chars.
_SESSION_NAME_ALLOWED = set("+=,.@-_")


def sanitize_session_name(raw: str) -> str:
    """Reduce an arbitrary string to STS's RoleSessionName charset (≤64)."""
    cleaned = "".join(
        c for c in raw if c.isalnum() or c in _SESSION_NAME_ALLOWED
    )[:64]
    return cleaned or "tess-user"


def derive_session_name(id_token: str) -> str:
    """Compute AWS_ROLE_SESSION_NAME from the token's UPN.

    The session name labels the developer's activity in CloudTrail. It comes
    straight from the signed-in identity, so it is always correct by
    construction — there is nothing to prompt for or reconcile.
    """
    payload = decode_jwt_payload(id_token)
    upn = payload.get("preferred_username") or payload.get("upn") or ""
    return sanitize_session_name(upn)


def set_persistent_env(name: str, value: str):
    """Set a user-scoped env var machine-wide (Windows/macOS).

    Windows: `setx` writes the user registry — all later-launched processes
    (GUI and terminal) inherit it. macOS: `launchctl setenv` injects into the
    launchd session, which feeds GUI apps (Postman/IntelliJ launched fresh).
    Terminals on macOS/Linux instead read env.sh (see write_env_sh).
    """
    if sys.platform == "win32":
        subprocess.run(["setx", name, value], capture_output=True, check=False)
    elif sys.platform == "darwin":
        subprocess.run(["launchctl", "setenv", name, value],
                       capture_output=True, check=False)


def write_env_sh(vars_to_set: dict):
    """Write the changeable env vars to <config-dir>/env.sh (macOS + Linux).

    setup wires `[ -f …/env.sh ] && . …/env.sh` into the shell rc files
    (~/.zshrc/~/.zprofile on macOS; ~/.profile/~/.bashrc on Linux), so every NEW
    shell sources the latest values. This is what makes terminals pick up
    role/region without a relaunch — `launchctl setenv` alone does not reach a
    new tab of an already-running Terminal.app.
    """
    path = env_sh_path()
    ensure_private_dir(path.parent)
    lines = ["# Written by `tess start` — changeable AWS env vars. Do not edit.\n"]
    for k, v in vars_to_set.items():
        # Wrap in single quotes and escape any embedded single quote the
        # POSIX way ( ' -> '\'' ), so a value can never break out of the
        # quoting and inject shell code into env.sh (which every shell sources).
        safe = str(v).replace("'", "'\\''")
        lines.append(f"export {k}='{safe}'\n")
    atomic_write(path, "".join(lines), mode=0o644)


def inject_changeable_env(config: dict, id_token: str, quiet: bool = False) -> dict:
    """Set the changeable env vars (AWS_ROLE_ARN, region pair, session name)
    machine-wide per platform, and into this process for immediate consistency.

    Region: regional STS requires a region. If config.region is absent we warn
    and leave any existing AWS_REGION untouched — we never invent one. Returns
    the dict of values actually set (for logging/inspection)."""
    values = {
        "AWS_ROLE_ARN": config["role_arn"],
        "AWS_ROLE_SESSION_NAME": derive_session_name(id_token),
    }
    region = config.get("region")
    if region:
        values["AWS_REGION"] = region
        values["AWS_DEFAULT_REGION"] = region
    elif not quiet:
        sys.stderr.write(
            "WARNING: config has no 'region'. AWS_REGION left unchanged.\n"
            "  Regional STS (AWS_STS_REGIONAL_ENDPOINTS=regional) needs a region;\n"
            "  set 'region' in your tess-config.json to avoid SDK errors.\n"
        )

    # Terminals on macOS AND Linux read env.sh (sourced by the shell rc files),
    # so every new shell picks up the latest role/region. This is the reliable
    # path for terminal AWS SDK use (mvn, aws cli).
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        write_env_sh(values)

    # GUI apps don't read shell rc / env.sh. Push into the session env:
    #   Windows — setx (registry); inherited by all later processes.
    #   macOS   — launchctl setenv; inherited by GUI apps launched afterward.
    if sys.platform == "win32":
        for k, v in values.items():
            set_persistent_env(k, v)
    elif sys.platform == "darwin":
        for k, v in values.items():
            set_persistent_env(k, v)
        # No login agent is installed, so the launchd session has no persistent
        # copy of the CONSTANTS across reboots. Re-apply them so a freshly
        # launched GUI app is fully populated after `tess start`. (On Windows
        # the constants persist in the registry from setup.)
        set_persistent_env("AWS_WEB_IDENTITY_TOKEN_FILE", str(token_path()))
        set_persistent_env("AWS_STS_REGIONAL_ENDPOINTS", "regional")

    # Reflect into the current process too, so anything spawned from this very
    # shell after `tess start` sees consistent values without a relaunch.
    os.environ.update(values)

    if not quiet:
        print("Synced env: " + ", ".join(f"{k}={v}" for k, v in values.items()))
        # GUI apps can't be updated in place — only newly launched ones inherit.
        if sys.platform == "darwin":
            print("If a GUI app (Postman/IntelliJ) was already open, quit it "
                  "fully (Cmd+Q) and relaunch to pick up the new credentials.")
        elif sys.platform == "win32":
            print("Already-open GUI apps keep their old environment; relaunch "
                  "them to pick up the new credentials.")
    return values


# ---------- Subcommand: stop ----------

def cmd_stop(args):
    return _stop_internal(quiet=False)


def _stop_internal(quiet: bool = False) -> int:
    pid = read_pid()
    had_session = pid is not None
    if pid is not None:
        kill_process(pid)
    cleanup_session_files()
    clear_msal_cache()
    if had_session:
        log_event("session stopped")
    if not quiet:
        print("Session ended.")
    return 0


# ---------- Subcommand: status ----------

def cmd_status(args):
    pid = read_pid()

    if pid is None:
        if args.json:
            print(json.dumps({"active": False}))
        else:
            print("No active session.")
        return 0

    if not is_process_alive(pid):
        if not args.json:
            print("Session is stale, cleaning up...")
        _stop_internal(quiet=True)
        if args.json:
            print(json.dumps({"active": False, "reason": "stale"}))
        else:
            print("No active session.")
        return 0

    # Read the token to extract identity + expiry
    try:
        id_token = token_path().read_text()
    except FileNotFoundError:
        if args.json:
            print(json.dumps({"active": False, "reason": "no-token"}))
        else:
            print("Session is broken (PID alive but no token). Run `tess stop` then `tess start`.")
        return 1

    payload = decode_jwt_payload(id_token)
    upn = safe_display(payload.get("preferred_username") or payload.get("upn") or "unknown")
    exp = payload.get("exp", 0)
    exp_dt = datetime.fromtimestamp(exp) if exp else None

    try:
        cfg_path, _ = session_or_resolved_config(None)
        config = load_config(cfg_path)
    except ConfigError:
        config = {}  # status still works off the token even if config is gone
    cap_hours = config.get("session_max_hours", DEFAULT_SESSION_MAX_HOURS)
    age = session_age_seconds() or 0
    cap_remaining = cap_hours * 3600 - age
    cap_end_dt = datetime.fromtimestamp(time.time() + cap_remaining) if cap_remaining > 0 else None

    healthy = exp_dt and exp_dt > datetime.now() and cap_remaining > 0

    if args.json:
        print(json.dumps({
            "active": True,
            "healthy": bool(healthy),
            "upn": upn,
            "token_expires": exp_dt.isoformat() if exp_dt else None,
            "cap_expires": cap_end_dt.isoformat() if cap_end_dt else None,
            "pid": pid,
            "log_file": str(log_path()),
        }))
        return 0

    health_str = "healthy" if healthy else "expired/stale"
    token_str = exp_dt.strftime("%I:%M %p").lstrip("0") if exp_dt else "?"
    cap_str = cap_end_dt.strftime("%I:%M %p").lstrip("0") if cap_end_dt else "?"
    print(f"{upn} — token expires {token_str}, cap {cap_str}, {health_str}")

    if args.verbose:
        print()
        print(f"  PID:        {pid}")
        print(f"  Log file:   {log_path()}")
        print(f"  Started:    {datetime.fromtimestamp(time.time() - age).isoformat()}")
        print(f"  Token file: {token_path()}")

    return 0


# ---------- Subcommand: refresh ----------

def cmd_refresh(args):
    if not is_session_active():
        sys.stderr.write("ERROR: No active session. Run `tess start` first.\n")
        return 1

    # Record the current token contents so we can detect a new one. Comparing
    # contents (not mtime) avoids false negatives from coarse filesystem mtime
    # resolution when the refresh lands within the same second.
    try:
        before_token = token_path().read_text()
    except FileNotFoundError:
        before_token = ""

    # Create the sentinel file
    refresh_signal_path().touch()

    # Wait for the daemon to act (up to 10 seconds)
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            now_token = token_path().read_text()
        except FileNotFoundError:
            continue
        if now_token and now_token != before_token:
            # New token written; show new expiration
            payload = decode_jwt_payload(now_token)
            exp = payload.get("exp")
            if exp:
                exp_str = datetime.fromtimestamp(exp).strftime("%I:%M %p").lstrip("0")
                print(f"Token refreshed. New expiration: {exp_str}")
            else:
                print("Token refreshed.")
            return 0

    sys.stderr.write(
        "WARNING: Refresh signal sent but no response from daemon within 10s.\n"
        "Check `tess logs` and `tess status` for details.\n"
    )
    return 1


# ---------- Subcommand: logs ----------

def cmd_logs(args):
    log = log_path()
    if not log.exists():
        print("No log file yet. Has the daemon started?")
        return 0

    if args.follow:
        # tail -f that survives log rotation. The daemon uses a
        # RotatingFileHandler, so daemon.log is periodically renamed and a new
        # file created in its place; we detect that (inode change or
        # truncation) and reopen so following doesn't silently go dead.
        f = open(log)
        try:
            # First print the last N lines
            lines = f.readlines()[-args.lines:]
            sys.stdout.write("".join(lines))
            sys.stdout.flush()
            cur_ino = os.fstat(f.fileno()).st_ino
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    continue
                time.sleep(0.5)
                try:
                    st = os.stat(log)
                except FileNotFoundError:
                    continue
                if st.st_ino != cur_ino or st.st_size < f.tell():
                    # Rotated or truncated — reopen the path from the start.
                    f.close()
                    f = open(log)
                    cur_ino = os.fstat(f.fileno()).st_ino
        except KeyboardInterrupt:
            return 0
        finally:
            f.close()
    else:
        with open(log) as f:
            lines = f.readlines()[-args.lines:]
            sys.stdout.write("".join(lines))
        return 0


# ---------- Subcommand: version ----------

def cmd_version(args):
    print_banner()
    print(f"tess {VERSION}")
    try:
        cfg_path, rung = session_or_resolved_config(None)
        config = load_config(cfg_path)
        print(f"  Config:    {cfg_path} ({rung})")
        print(f"  Tenant:    {config.get('tenant_id', 'unset')}")
        print(f"  Client:    {config.get('client_id', 'unset')}")
        print(f"  Role:      {config.get('role_arn', 'unset')}")
        print(f"  Region:    {config.get('region', 'unset')}")
    except ConfigError as e:
        print(f"  Config:    (not loaded — {e})")
    print(f"  Python:    {sys.version.split()[0]} ({sys.executable})")
    print(f"  Data dir:  {data_dir()}")
    print(f"  Config dir:{config_dir()}")
    print()
    print(NAME_STORY)
    return 0


# ---------- Subcommand: config ----------

def cmd_config(args):
    """Show which config is loaded, how it was resolved, any drift, and the
    current values of every managed env var."""
    # Whether a session is actually running — so the config line is never
    # mistaken for "a session is live using this".
    if is_session_active():
        print(f"Session: running (pid {read_pid()})")
        config_label = "Config in use"
    else:
        print("Session: not running")
        config_label = "Config that `tess start` would use"

    try:
        active_path, rung = session_or_resolved_config(None)
    except ConfigError as e:
        print(f"{config_label}: (none resolvable)\n  {e}")
        active_path = None

    if active_path is not None:
        print(f"{config_label}: {active_path}")
        print(f"  Supplied by: {rung}")
        try:
            load_config(active_path)
            print("  Valid:       yes")
        except ConfigError as e:
            print(f"  Valid:       NO — {e}")

    # Drift: what the ladder would resolve right now vs the recorded session.
    sc = session_config_path()
    if sc.is_file():
        recorded = sc.read_text().strip()
        try:
            now_path, now_rung = resolve_config_path(None)
            now_str, now_rung_str = str(now_path), now_rung
        except ConfigError:
            now_str, now_rung_str = "(none resolvable)", "—"
        if now_str != recorded:
            print()
            print("  DRIFT: the recorded session config differs from what the")
            print(f"         ladder resolves now ({now_rung_str}: {now_str}).")
            print("         The session keeps using the recorded file until the")
            print("         next `tess start`.")

    print()
    print("Managed environment variables (this process's view):")
    for name in MANAGED_ENV_VARS:
        val = os.environ.get(name)
        print(f"  {name} = {val if val is not None else '(unset)'}")
    print("  (GUI apps / new shells see the persisted values; already-running")
    print("   processes keep what they had at launch.)")
    return 0


# ---------- Hidden command: revelio ----------

def _revelio_check(label: str, ok: bool, detail: str = ""):
    mark = "✓" if ok else "✗"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  ({detail})"
    print(line)


def cmd_revelio():
    """Hidden: decode the active token's claims and run sanity checks against
    config. NEVER prints the raw token — only its decoded, non-secret claims and
    file metadata. Intended for the internal runbook, not day-to-day use."""
    print_banner()
    tp = token_path()
    if not tp.is_file():
        print("revelio: no token file present. Run `tess start` first.")
        return 1
    try:
        raw = tp.read_text()
    except OSError as e:
        print(f"revelio: cannot read token file: {e}")
        return 1

    payload = decode_jwt_payload(raw)
    if not payload:
        print("revelio: token file is not a decodable JWT.")
        return 1

    st = tp.stat()
    print("Token file:")
    print(f"  Path:  {tp}")
    print(f"  Size:  {st.st_size} bytes")
    print(f"  Mode:  {oct(st.st_mode & 0o777)}")
    print(f"  Mtime: {datetime.fromtimestamp(st.st_mtime).isoformat()}")

    print()
    print("Decoded claims (display only — signature NOT verified here; STS does that):")
    for k in ("preferred_username", "upn", "name", "oid", "sub",
              "aud", "iss", "tid", "iat", "nbf", "exp"):
        if k in payload:
            v = payload[k]
            if k in ("iat", "nbf", "exp"):
                v = f"{v} ({_fmt_exp(v)})"
            print(f"  {k}: {safe_display(v)}")

    print()
    print("Sanity checks:")
    try:
        cfg_path, _ = session_or_resolved_config(None)
        config = load_config(cfg_path)
    except ConfigError as e:
        print(f"  (config unavailable — {e})")
        config = None

    if config:
        aud = payload.get("aud")
        client_id = config.get("client_id")
        _revelio_check("aud == client_id", aud == client_id,
                       f"aud={aud}")
        iss = payload.get("iss", "")
        tenant = str(config.get("tenant_id", ""))
        _revelio_check("issuer carries tenant_id", bool(tenant) and tenant in iss,
                       f"iss={iss}")

    exp = payload.get("exp")
    now = time.time()
    if exp:
        mins = int((exp - now) / 60)
        _revelio_check("token not expired", exp > now, f"{mins} min left")
    else:
        _revelio_check("token not expired", False, "no exp claim")
    return 0


# ---------- Background refresher (the daemon) ----------

def setup_daemon_logging():
    log = log_path()
    ensure_private_dir(log.parent)
    # Time-based rotation: one file per day, keep ~30 days of history (the audit
    # trail of sign-ins and token refreshes). Volume is tiny (~a few KB/day), so
    # 30 days costs well under a megabyte. Rotation advances whenever the daemon
    # is running and emits after midnight; the log file persists across sessions
    # and reboots (only `tess stop`/uninstall touch the session files, not this).
    handler = logging.handlers.TimedRotatingFileHandler(
        log, when="midnight", interval=1, backupCount=30, utc=False
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger = logging.getLogger("tess")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # Audit log may carry the signed-in UPN; keep it owner-only.
    try:
        log.chmod(0o600)
    except OSError:
        pass
    return logger


def notify(title: str, message: str):
    """Best-effort desktop notification."""
    try:
        import asyncio
        from desktop_notifier import DesktopNotifier
        notifier = DesktopNotifier(app_name="tess")
        asyncio.run(notifier.send(title=title, message=message))
    except Exception:
        pass  # Notifications are nice-to-have, not critical


def install_daemon_signal_handlers(logger):
    """Clean up the session when the daemon is terminated.

    This is the cross-platform logout-cleanup mechanism on macOS and Linux:
    when the user logs out, the OS tears down the session's processes by
    sending SIGTERM (then SIGKILL after a grace period). We trap SIGTERM
    (and SIGHUP) and use it to delete the token file and clear the MSAL cache
    before exiting, so a logout leaves no live token behind. The same handler
    makes `tess stop` (which sends SIGTERM) self-clean.

    Not installed on Windows: there, taskkill /F (used by `tess stop` and by
    the Task Scheduler logoff task) delivers an uncatchable terminate, and the
    logoff task runs `tess stop` to perform cleanup explicitly.
    """
    if sys.platform == "win32":
        return

    def _on_terminate(signum, frame):
        logger.info("received signal %s; cleaning up and exiting", signum)
        cleanup_session_files()
        clear_msal_cache()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_terminate)
    try:
        signal.signal(signal.SIGHUP, _on_terminate)
    except (AttributeError, ValueError):
        pass  # SIGHUP may be unavailable on some platforms


def cmd_refresh_daemon(config_file: str | None):
    """The background refresher loop. Not intended for direct user invocation.

    Receives the resolved config path from `tess start` on the command line so
    it uses the exact same file the session resolved to. Falls back to the
    recorded session.config (then the ladder) if invoked without one.
    """
    logger = setup_daemon_logging()
    logger.info("daemon started (pid=%d)", os.getpid())

    install_daemon_signal_handlers(logger)

    try:
        if config_file:
            path = Path(config_file)
        else:
            path, _ = session_or_resolved_config(None)
        config = load_config(path)
    except ConfigError as e:
        logger.error("could not load config: %s", e)
        cleanup_session_files()
        clear_msal_cache()
        return 1
    refresh_interval = config.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES) * 60
    cap_seconds = config.get("session_max_hours", DEFAULT_SESSION_MAX_HOURS) * 3600

    app, cache = build_msal_app(config, clear_cache=False)
    failures_start: float | None = None

    next_refresh = time.time() + refresh_interval

    while True:
        # 1. Hard cap check — always first
        age = session_age_seconds()
        if age is None:
            # No start timestamp means the session was torn down (or never
            # fully established). Clean up any leftovers so we don't leave a
            # stale token the AWS SDK would keep reading, then exit.
            logger.warning("session.started missing; cleaning up and exiting")
            cleanup_session_files()
            clear_msal_cache()
            break
        if age >= cap_seconds:
            logger.info("8-hour cap reached (age=%.0fs); ending session", age)
            notify(
                "Session ended",
                f"AWS session reached its {cap_seconds // 3600}-hour cap. "
                "Run `tess start` to begin a new session.",
            )
            cleanup_session_files()
            clear_msal_cache()
            break

        # 2. Check for refresh signal (early wake)
        signal_present = refresh_signal_path().exists()
        time_for_scheduled_refresh = time.time() >= next_refresh

        if signal_present or time_for_scheduled_refresh:
            # Consume the signal up front. A refresh is now being serviced
            # regardless of outcome; leaving the sentinel in place would make
            # the daemon re-enter this block every REFRESH_SIGNAL_CHECK_SECONDS
            # and defeat the failure backoff below.
            if signal_present:
                refresh_signal_path().unlink(missing_ok=True)

            # Phase 1 — obtain a fresh id_token. Failures here are genuine
            # authentication problems (Entra session lapsed, RT invalidated,
            # network unreachable) and feed the re-auth / give-up logic.
            id_token = None
            try:
                accounts = app.get_accounts()
                if not accounts:
                    raise RuntimeError("no MSAL account in cache")
                # force_refresh=True makes MSAL exchange the refresh token at the
                # token endpoint every cycle (instead of possibly returning a
                # cached entry with no id_token), so we always get a freshly
                # minted id_token to write.
                result = app.acquire_token_silent(
                    scopes=resolve_scopes(config), account=accounts[0],
                    force_refresh=True,
                )
                if not result or "id_token" not in result:
                    raise RuntimeError(
                        f"silent refresh returned no id_token: "
                        f"{result.get('error_description', 'unknown') if result else 'None'}"
                    )
                id_token = result["id_token"]
            except Exception as e:
                logger.error("silent refresh failed: %s", e)
                if failures_start is None:
                    failures_start = time.time()
                    notify(
                        "AWS session needs re-authentication",
                        "Run `tess start` to sign in again.",
                    )
                elif time.time() - failures_start >= FAILURE_GIVE_UP_AFTER_SECONDS:
                    logger.warning("auth failures persisted >30min; ending session")
                    notify(
                        "AWS session ended",
                        "Refresh failed for 30+ minutes. Run `tess start` to re-authenticate.",
                    )
                    cleanup_session_files()
                    clear_msal_cache()
                    break
                # Auth failure — back off and retry.
                next_refresh = time.time() + FAILURE_BACKOFF_SECONDS

            # Phase 2 — persist the new token. A write failure is an I/O
            # problem, NOT an authentication problem: do not fire the re-auth
            # notification, do not touch failures_start, do not tear the
            # session down. Just log and retry soon.
            if id_token is not None:
                # Self-check (DESIGN #5): capture the OUTGOING token's expiry
                # so we can confirm the refresh actually advanced it. If exp
                # does not move forward, MSAL handed back a cached id_token
                # instead of minting a fresh one — the file would silently go
                # stale even though the refresh "succeeded". We surface that
                # loudly in the log rather than letting it bite an hour later.
                prev_exp = None
                try:
                    prev_exp = decode_jwt_payload(token_path().read_text()).get("exp")
                except (FileNotFoundError, OSError):
                    pass
                new_exp = decode_jwt_payload(id_token).get("exp")

                try:
                    save_plain_cache_if_needed(cache)
                    atomic_write(token_path(), id_token)
                except TokenWriteError as e:
                    logger.error("token write failed (will retry): %s", e)
                    next_refresh = time.time() + FAILURE_BACKOFF_SECONDS
                else:
                    failures_start = None  # clear failure streak
                    next_refresh = time.time() + refresh_interval
                    if prev_exp and new_exp and new_exp <= prev_exp:
                        logger.warning(
                            "token exp did NOT advance (prev=%s new=%s) — MSAL "
                            "likely returned a cached id_token; token may go "
                            "stale. Check acquire_token_silent scopes/force_refresh.",
                            _fmt_exp(prev_exp), _fmt_exp(new_exp),
                        )
                    else:
                        logger.info(
                            "token refreshed (signal=%s, exp %s -> %s)",
                            signal_present, _fmt_exp(prev_exp), _fmt_exp(new_exp),
                        )

        # 3. Short sleep — wake periodically to check signal + cap
        time.sleep(REFRESH_SIGNAL_CHECK_SECONDS)

    logger.info("daemon exiting")


# ---------- Argument parsing ----------

class BannerParser(argparse.ArgumentParser):
    """ArgumentParser that prepends the ASCII banner to --help output.

    The banner is only shown when stdout is a terminal, so piping/redirecting
    `tess --help` yields clean text. The name-story epilog is part of the help
    text itself (shown regardless), via RawDescriptionHelpFormatter so its line
    breaks are preserved.
    """

    def format_help(self) -> str:
        text = super().format_help()
        if sys.stdout.isatty():
            return banner_text() + "\n\n" + text
        return text


def build_parser() -> argparse.ArgumentParser:
    parser = BannerParser(
        prog="tess",
        description="Federated AWS credentials for developer laptops.",
        epilog=NAME_STORY,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"tess {VERSION}")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    p_start = subparsers.add_parser("start", help="begin an AWS session")
    p_start.add_argument("--config", metavar="PATH", default=None,
                         help="explicit path to a config file (any filename)")
    p_start.add_argument("--force", action="store_true",
                         help="restart even if a session is already active")
    p_start.add_argument("--quiet", action="store_true",
                         help="suppress banner output")
    p_start.set_defaults(func=cmd_start)

    p_stop = subparsers.add_parser("stop", help="end the AWS session")
    p_stop.set_defaults(func=cmd_stop)

    p_status = subparsers.add_parser("status", help="show current session state")
    p_status.add_argument("--verbose", "-v", action="store_true",
                          help="show extended details")
    p_status.add_argument("--json", action="store_true",
                          help="output as JSON")
    p_status.set_defaults(func=cmd_status)

    p_refresh = subparsers.add_parser("refresh", help="force immediate token rotation")
    p_refresh.set_defaults(func=cmd_refresh)

    p_logs = subparsers.add_parser("logs", help="show recent refresher activity")
    p_logs.add_argument("-n", "--lines", type=int, default=50,
                        help="number of lines to show (default 50)")
    p_logs.add_argument("-f", "--follow", action="store_true",
                        help="follow the log file as it grows")
    p_logs.set_defaults(func=cmd_logs)

    p_config = subparsers.add_parser(
        "config", help="show which config is loaded and how it was resolved")
    p_config.set_defaults(func=cmd_config)

    p_version = subparsers.add_parser("version", help="show version and config info")
    p_version.set_defaults(func=cmd_version)

    return parser


def main():
    # Hidden internal modes, handled before argparse so they don't appear in
    # --help output.
    #   _refresh-daemon [config-path]  — the background refresher loop.
    #   _banner                        — print the wordmark (used by setup).
    #   revelio                        — decoded-token inspection (runbook only).
    if len(sys.argv) >= 2 and sys.argv[1] == "_refresh-daemon":
        cfg = sys.argv[2] if len(sys.argv) > 2 else None
        sys.exit(cmd_refresh_daemon(cfg))
    if len(sys.argv) == 2 and sys.argv[1] == "_banner":
        print_banner(force=True, with_story=True)
        sys.exit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "revelio":
        sys.exit(cmd_revelio())

    parser = build_parser()
    if len(sys.argv) == 1:
        # Bare `tess` with no verb: show the banner + options (a friendly
        # landing screen) instead of an argparse "command required" error.
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except ConfigError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
