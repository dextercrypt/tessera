# tessera (`tess`) — Design

> Federated AWS credentials for developer laptops.

`tess` gives a laptop the same credential behavior a Kubernetes pod gets from
**IRSA**: short-lived AWS credentials minted from a corporate OIDC identity
provider, with **no static access keys** and **no per-application AWS config**.
A developer runs `tess start`, signs into Microsoft Entra in the browser (with
MFA), and from then on the AWS SDK in their shell and GUI apps transparently
assumes an IAM role. A background daemon keeps the credential fresh until an
8-hour cap or logout.

The entire implementation is a single, dependency-light Python file:
`src/tess.py`. This document describes how it works and why.

---

## 1. Purpose and goals

- **No long-lived AWS secrets on laptops.** Nothing in `~/.aws/`, no
  `aws_access_key_id`. The only artifact at rest is a short-lived OIDC
  `id_token` in an owner-only file, plus an OS-keychain-encrypted MSAL refresh
  token.
- **IRSA-shaped.** AWS already knows how to trade a web-identity token for role
  credentials via `AssumeRoleWithWebIdentity`. `tess` simply provides the token
  file and the environment variables the SDK's web-identity provider reads —
  exactly the mechanism pods use. AWS does the signature verification and the
  credential minting; `tess` never touches AWS APIs itself.
- **Config-independent program.** The shipped `tess.py` is identical for every
  environment and every user. All per-environment specifics (`tenant_id`,
  `client_id`, `role_arn`, `region`) live in a JSON config that is delivered
  separately. The program is installed read-only.
- **Access is gated in Entra, not AWS.** One Entra app registration per
  environment with *Assignment required* turned on is the gate (see
  `ACCESS-CONTROL.md` / `SETUP-AZURE-APPS.md`). AWS only trusts the token's
  `aud` (= the app's `client_id`) and `iss`; it carries no per-person list.
- **Cross-platform, zero-ceremony.** macOS, Linux, and Windows. After
  bootstrap, the daily workflow is just `tess start`.

---

## 2. Trust and threat model

**Identities and trust boundaries**

- **Microsoft Entra** is the identity provider and the access gate. It performs
  interactive auth (including MFA), enforces *Assignment required*, and signs
  the `id_token`. `tess` trusts Entra to authenticate the user.
- **AWS STS** is the relying party. The IAM role's trust policy trusts the
  Entra OIDC issuer and the app's `client_id` as `aud`. STS verifies the JWT
  signature, issuer, audience, and expiry on every `AssumeRoleWithWebIdentity`
  call. **`tess` never validates the token itself** — it only decodes it for
  display.
- **The local user account** is trusted with its own data. `tess` defends one
  user's session against *other* local users (file modes), and defends the
  user's terminal against a malicious-content token (sanitization), but a
  process running as the user can read that user's token — the same as IRSA on
  a node.

**What `tess` protects against**

- *Credential theft at rest:* no static keys; the token expires in ~1 hour and
  the whole session is capped at 8 hours; the token file is `0600`.
- *Other local users reading the token / cache:* token and data dirs are
  owner-only (`0600` / `0700`); writes use `O_NOFOLLOW` + atomic rename so a
  symlink planted at the path can't redirect or expose the write.
- *Injection through config values:* config fields are format-validated and
  reject shell metacharacters before they reach `env.sh` or the MSAL authority
  URL.
- *Injection through token claims:* the JWT is untrusted display data (its
  signature is verified by STS, not `tess`), so any claim printed or logged is
  stripped of non-printable / CR-LF characters to defuse ANSI-escape and
  log-forging attacks.

**Out of scope**

- A user-level compromise (malware running as the developer) — it can read the
  token, exactly as it could on any IRSA node.
- Authorization *inside* AWS — that's the IAM role's policies.
- A note on the gate: an Entra **admin** can bypass *Assignment required* on an
  app (admins implicitly have access). This is a known Entra behavior, called
  out in `ACCESS-CONTROL.md`.

---

## 3. Credential lifecycle (end to end)

```
tess start
   │  1. resolve + validate config (ladder)
   │  2. clear MSAL cache  → force fresh MFA
   ▼
acquire_token_interactive  ──browser──►  Entra (sign-in + MFA + assignment check)
   │  returns id_token (aud = client_id)
   ▼
   3. write session.started (timestamp)         data dir, 0600
   4. write session.config  (resolved path)     data dir, 0644
   5. write token           (the id_token)      data dir, 0600
   6. inject env vars (role/region/session)     per-platform
   7. spawn _refresh-daemon, write daemon.pid
   ▼
AWS SDK (mvn / aws cli / IntelliJ / Postman)
   reads AWS_WEB_IDENTITY_TOKEN_FILE → AssumeRoleWithWebIdentity → role creds
   ▼
daemon loop (every ~50 min, or on `tess refresh`)
   acquire_token_silent(force_refresh=True) → new id_token → atomic rewrite token
   ▼
ends when:  8-hour cap reached │ `tess stop` │ logout (SIGTERM / logoff task)
            → token + session files deleted, MSAL cache cleared
```

The AWS SDK is never invoked by `tess`. `tess` only maintains (a) the token
file and (b) the environment variables that point the SDK's web-identity
provider at that file and role. Credential minting and rotation on the AWS side
happen lazily inside whatever SDK reads them.

---

## 4. Components

All in `src/tess.py`.

### 4.1 Commands

| Command | Purpose |
|---|---|
| `start` | Resolve config, interactive MFA sign-in, write token + env + spawn daemon. `--config`, `--force`, `--quiet`. |
| `stop` | Kill the daemon, delete session files, clear MSAL cache. |
| `status` | Show identity, token expiry, cap expiry, health. `--verbose`, `--json`. Auto-cleans a stale session. |
| `refresh` | Touch a sentinel and wait (≤10s) for the daemon to rotate the token now. |
| `logs` | Tail `daemon.log`. `-n/--lines`, `-f/--follow` (survives rotation). |
| `config` | Show which config resolves, how, any drift, and every managed env var's current value. |
| `version` | Banner, version, resolved config summary, paths. |

**Hidden modes** (handled before argparse, absent from `--help`):

- `_refresh-daemon [config-path]` — the background loop (§6).
- `_banner` — prints the ASCII wordmark; used by the setup splash.
- `revelio` — decodes the active token and prints its **claims only** plus file
  metadata and sanity checks (`aud == client_id`, issuer carries `tenant_id`,
  not expired). It **never prints the raw token** — a deliberate security
  choice so a runbook step can't leak a usable credential into a terminal,
  screen-share, or shell history.

### 4.2 MSAL integration

`build_msal_app()` constructs an MSAL `PublicClientApplication` against
authority `https://login.microsoftonline.com/<tenant_id>` with `client_id`. The
token cache is **OS-keychain-encrypted** via `msal-extensions`
(`build_encrypted_persistence` + `PersistedTokenCache`). If the keychain is
unreachable (e.g. minimal Linux without libsecret), it falls back to a plain
`SerializableTokenCache` persisted manually at `0600` via `atomic_write`, and
warns on stderr. `start` passes `clear_cache=True` so each morning forces a
fresh interactive MFA rather than silently reusing yesterday's refresh token.

### 4.3 `resolve_scopes()` — why a non-empty scope matters

MSAL is access-token-centric: the OIDC `id_token` `tess` wants is a by-product
of a *token request for a scope*. With an empty scope list, the interactive
sign-in still yields an `id_token`, but the **silent refresh path has nothing
to request and returns no `id_token`** — so the token can't be renewed.
`resolve_scopes()` therefore defaults to `"<client_id>/.default"`, which makes
the refresh return a renewable `id_token`. The audience stays `client_id` either
way, so AWS trust is unaffected. The scope is overridable via the config
`scope` key (string or list); set it to `""` to force the legacy empty-scope
behavior.

---

## 5. Configuration

### 5.1 Schema

```json
{
  "tenant_id": "<entra-tenant-guid-or-domain>",   // required
  "client_id": "<entra-app-client-id>",           // required
  "role_arn":  "arn:aws:iam::<acct>:role/<name>", // required
  "region":    "us-east-1",                        // optional (warned if absent)
  "refresh_interval_minutes": 50,                  // optional, default 50
  "session_max_hours": 8,                          // optional, default 8
  "scope": "<client_id>/.default"                  // optional, see §4.3
}
```

Validation (`validate_config`) requires `tenant_id`, `client_id`, `role_arn`,
and enforces a strict format on each:

- `tenant_id`: `^[A-Za-z0-9.-]+$`
- `client_id`: `^[A-Za-z0-9-]+$`
- `role_arn`: `^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$`
- `region` (if present): `^[a-z0-9-]+$`
- `refresh_interval_minutes` / `session_max_hours` (if present): positive number

Besides catching typos, these regexes are a **security control**: they reject
quotes, `$`, `;`, spaces, and slashes in values that get written into `env.sh`
(sourced by every shell) and the MSAL authority URL, closing those injection
vectors at the source.

> There is exactly one app per environment. The `client_id` is both the MSAL
> login identity and the token `aud` AWS trusts — there is **no** separate
> "resource"/"API" app and **no** `resource_app_id` key.

### 5.2 Resolution ladder

`resolve_config_path()` picks the config, first match wins:

1. `--config <path>` — explicit flag. **Missing file → error.**
2. `$TESS_CONFIG` — explicit env var. **Missing file → error.**
3. `./tess-config.json` (current directory) — absent → fall through.
4. `<config-dir>/tess-config.json` (global default) — absent → fall through.
5. None found → `ConfigError` listing every location searched.

Explicit rungs (1–2) error on a miss so a typo isn't silently masked; implicit
rungs (3–4) fall through.

**Session pinning.** `tess start` records the resolved path in
`session.config`. Every *later* command (and the daemon) reads that recorded
file via `session_or_resolved_config()` rather than re-resolving the ladder from
its own working directory — so `tess status` run from a different folder can't
accidentally report a different config than the live session uses. An explicit
`--config` always re-resolves. `tess config` surfaces **drift** when the ladder
would now resolve to a different file than the recorded one.

### 5.3 Directories (per platform)

| | Data dir (token, pid, log, cache, venv) | Config dir (`tess-config.json`, `env.sh`) |
|---|---|---|
| Windows | `%LOCALAPPDATA%\tess` | `%APPDATA%\tess` |
| macOS | `~/Library/Application Support/tess` | `~/.config/tess` |
| Linux | `~/.local/share/tess` (XDG) | `~/.config/tess` (XDG) |

Data and config dirs are deliberately separate: an upgrade re-copies the
program/data dir, so config kept there would be clobbered; the config dir is
also the OS-conventional location.

---

## 6. The refresh daemon

`cmd_refresh_daemon()` is the background loop, spawned detached by `start`
(`start_new_session` on Unix; `DETACHED_PROCESS | CREATE_NO_WINDOW` + `pythonw`
on Windows) and handed the resolved config path on its command line.

**Tunables** (constants, config-overridable where noted):

- `DEFAULT_REFRESH_INTERVAL_MINUTES = 50` (config `refresh_interval_minutes`)
- `DEFAULT_SESSION_MAX_HOURS = 8` (config `session_max_hours`)
- `FAILURE_BACKOFF_SECONDS = 300` — retry every 5 min on failure
- `FAILURE_GIVE_UP_AFTER_SECONDS = 1800` — tear down after 30 min of failures
- `TRANSIENT_FAILURE_GRACE = 3` — failures logged WARNING before escalating to ERROR
- `REFRESH_SIGNAL_CHECK_SECONDS = 5` — wake cadence

**Loop, per iteration:**

1. **Hard cap check, always first.** Read `session.started`; if missing, the
   session was torn down — clean up and exit. If `age >= cap_seconds`, notify,
   delete session files, clear cache, and exit.
2. **Refresh trigger:** either the `refresh.signal` sentinel exists (from
   `tess refresh`) or the scheduled time has arrived. The sentinel is consumed
   up front so a single signal can't make the daemon spin past the backoff.
   - *Phase 1 — obtain token:* `acquire_token_silent(..., force_refresh=True)`.
     `force_refresh` forces an actual refresh-token exchange at the token
     endpoint so a *fresh* `id_token` is minted (not a cached one). A failure
     here is a genuine auth problem (Entra session lapsed, RT revoked, network
     down): increment the failure counter, log WARNING (ERROR after 3 in a
     row), fire a one-time "needs re-auth" desktop notification, and after 30
     minutes of continuous failure, tear the session down. Back off to
     5-minute retries.
   - *Phase 2 — persist token:* `atomic_write` the new token. A write failure is
     an **I/O** problem, explicitly *not* an auth problem — it does not fire the
     re-auth notification or touch the failure clock; it just logs and retries
     soon. On success the failure streak resets and the next refresh is
     rescheduled at the normal interval.
   - *Staleness self-check:* the daemon compares the previous token's `exp`
     against the new one. If `exp` did **not** advance, MSAL likely returned a
     cached `id_token` and the file would silently go stale — so the daemon logs
     a loud WARNING rather than letting it bite an hour later.
3. **Short sleep** (`REFRESH_SIGNAL_CHECK_SECONDS`) and loop, so the cap and the
   refresh signal are both checked promptly.

---

## 7. Environment-variable propagation

`tess` uses **environment variables only** — never `~/.aws/`. Variables are
split by who owns them:

- **Constants** (set once by the setup script): `AWS_WEB_IDENTITY_TOKEN_FILE`
  (the token path), `AWS_STS_REGIONAL_ENDPOINTS=regional`.
- **Changeable** (rewritten by each `tess start` from config + token):
  `AWS_ROLE_ARN`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_ROLE_SESSION_NAME`.

`AWS_ROLE_SESSION_NAME` is derived from the token's UPN
(`derive_session_name` → `sanitize_session_name`, reduced to the STS charset,
≤64 chars), so CloudTrail labels activity with the real signed-in identity by
construction. If config has no `region`, `tess` warns and leaves `AWS_REGION`
untouched — it never invents one (regional STS needs a region).

**How the values reach processes** differs by platform, because there is no
single mechanism that reaches both GUI apps and already-open terminals:

- **Windows:** `setx` writes the user registry; all later-launched processes
  (GUI and terminal) inherit it. Already-open apps keep their old values until
  relaunch.
- **macOS:** `launchctl setenv` feeds the launchd session (GUI apps launched
  afterward, e.g. IntelliJ/Postman). Terminals instead source
  `<config-dir>/env.sh`, which setup wires into `~/.zshrc` (with a `precmd`
  reload hook so an open terminal picks up a new `tess start` on its next
  prompt) and `~/.zprofile`. Because no login agent persists the constants
  across reboot, `tess start` re-applies the constants via `launchctl setenv`
  too.
- **Linux:** terminals and the GUI session both source `env.sh`, wired into
  `~/.profile` (login + display-manager → GUI apps) and `~/.bashrc` (with a
  `PROMPT_COMMAND` reload hook). GUI propagation is display-manager-dependent
  (modern Wayland/GDM may need `~/.config/environment.d/`).

`env.sh` is written with each value single-quoted and embedded quotes escaped
the POSIX way (`'\''`), so a value can never break out and inject shell code.

**No login autostart agents** on any platform. The constants are useless before
`tess start` (no token, no role), and `tess start` re-applies everything anyway,
so an autostart agent would add a persistence footprint (and trip macOS
background-item / EDR notices) for no gain. This matches the passive-data model
across all three OSes (registry / rc files / launchctl session).

---

## 8. Security hardening — decisions and rationale

| Decision | Why |
|---|---|
| Token & log `0600`; data/config dirs `0700` | Keep other local users from reading the token, plaintext-cache fallback, or the UPN-bearing audit log. |
| `atomic_write`: temp file opened with target mode via `os.open`, then `os.replace` | Token is never briefly world-readable between create and chmod; rename is atomic so the SDK never reads a half-written token. Windows retries `os.replace` on transient `PermissionError` from a concurrent reader. |
| `O_NOFOLLOW` on the temp write | Refuses to write through a symlink planted at the path — defense in depth over the `0700` parent. No-op where unsupported (Windows). |
| `tess.py` installed read-only (`0500` / `+R`) | The live program can't be edited in place by accident or by a non-root attacker. |
| Config format validation rejects shell metacharacters | Values flow into `env.sh` and the authority URL; this closes injection at the source. |
| `safe_display()` strips non-printable chars from claims; `log_event()` collapses CR/LF | The JWT is decoded for display **without** signature verification (STS verifies), so claims are untrusted — defuses ANSI/terminal-escape injection and log-line forgery. |
| `revelio` prints claims only, never the raw token | A diagnostic can't leak a usable credential to a terminal/screen-share/history. |
| `decode_jwt_payload` never validates signatures | Validation is AWS STS's job; doing it locally would imply a trust `tess` doesn't have and isn't needed. |
| MSAL cache keychain-encrypted, `0600` plaintext fallback | The refresh token at rest is protected by the OS keychain; on minimal systems it stays owner-only and the user is warned. |
| Pinned dependencies (`requirements.txt`) + optional `--require-hashes` lockfile + `SHA256SUMS`-verified curl bootstrap | Reproducible installs; the standalone installer verifies checksums *before* anything is installed. |
| Audit log (`daemon.log`), one line per event, `TimedRotatingFileHandler`, ~30 days | A chronological trail of sign-ins, refreshes, caps, and stops. Foreground commands append via a short `O_APPEND` write so they never contend with the daemon's rotating handler. |
| Fresh MFA each morning (`clear_cache=True` on `start`) | A start can't silently reuse a stale day-old refresh token. |

---

## 9. Session teardown

A session ends three ways, all converging on `cleanup_session_files()` (deletes
token, pid, `session.started`, `session.config`, `refresh.signal`) plus
`clear_msal_cache()`:

- **`tess stop`** — kills the daemon (`SIGTERM`→`SIGKILL` on Unix, `taskkill /F`
  on Windows) and cleans up.
- **8-hour cap / 30-min auth failure** — the daemon itself cleans up and exits.
- **Logout** —
  - *macOS/Linux:* the OS sends `SIGTERM` to session processes; the daemon's
    signal handler (`install_daemon_signal_handlers`) traps `SIGTERM`/`SIGHUP`
    and cleans up before exit. On Linux a systemd user unit also runs
    `tess stop` on logout.
  - *Windows:* a Task Scheduler task (`tess-stop-on-logoff`, triggered by
    logoff event 4647) runs `tess stop`; the daemon has no catchable-signal
    handler there because `taskkill /F` is an uncatchable terminate.

`tess status` self-heals: if the recorded PID is dead, it runs the stop cleanup
and reports no active session.

---

## 10. Cross-platform notes

- **One source file, one requirements file.** pip resolves per-OS wheels;
  `msal-extensions` and `desktop-notifier` pull different backends per platform
  automatically. Pinned: `msal==1.37.0`, `msal-extensions==1.3.1`,
  `desktop-notifier==6.2.0`.
- **Process liveness:** on Windows, `os.kill(pid, 0)` would *terminate* the
  process (CPython maps it to `OpenProcess`+`TerminateProcess`), so liveness is
  checked via `OpenProcess` + `GetExitCodeProcess` instead.
- **ASCII-only banner** (figlet "standard") so the wordmark renders identically
  on cmd/PowerShell/conhost, Terminal.app, and Linux terminals. The banner is
  suppressed when stdout isn't a TTY.
- **Browser success/error pages** are MSAL templates rendered after the IdP
  redirect; the error page interpolates the IdP's `$error`/`$error_description`
  (HTML-escaped by MSAL).
- **Install layout:** a per-user venv under the data dir, `tess.py` copied in
  read-only, and a tiny wrapper on `PATH` (`~/.local/bin/tess` on Unix,
  `tess.bat` in `WindowsApps` on Windows). Setup is bootstrap-only — it never
  prompts for or writes the real config.

---

## 11. Non-goals and limitations

- **No `~/.aws/` integration.** Environment variables only, by design. Tools
  that read only profiles (not env vars) won't see the credentials.
- **No local token/signature validation.** STS is the sole verifier.
- **No automatic refresh of already-running GUI apps.** Env changes reach only
  newly launched processes; an open IntelliJ/Postman must be relaunched (macOS
  needs a full Cmd-Q). New terminals (or the next prompt, via the reload hook)
  pick up changes automatically.
- **Linux GUI env propagation is display-manager-dependent** and may require
  `~/.config/environment.d/`.
- **Not a secrets manager and not an AWS authorization layer.** It brokers a
  federated identity into role credentials; what the role can do is pure IAM.
- **Single active session per user/laptop.** `tess start` on an active session
  is a no-op unless `--force` restarts it.
- **Admin bypass of the Entra gate** is inherent to Entra (see §2 /
  `ACCESS-CONTROL.md`), not something `tess` can override.
