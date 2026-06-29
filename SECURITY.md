# Security Policy

tessera (`tess`) issues short-lived, federated AWS credentials on developer
laptops. It handles identity tokens, so we take its security seriously and
welcome reports.

## Reporting a vulnerability

**Please report privately — do not open a public issue for security bugs.**

- Preferred: open a private advisory via the repository's **Security → Report a
  vulnerability** (GitHub Security Advisories).
- Fallback: email the maintainer at **mayankpurohit01@hotmail.com**
  *(replace with a dedicated security alias for a team/org deployment).*

Please include: affected version/commit, platform, a description, and clear
reproduction steps or a proof-of-concept. We aim to acknowledge within a few
business days and to coordinate a fix and disclosure timeline with you.

## Threat model

### What tess is designed to protect
- **No long-lived AWS credentials** on disk or in the environment — everything
  is short-lived (≤1h AWS creds, ≤1h OIDC token) and refreshed automatically.
- **Fresh MFA each session**; an **8-hour absolute cap**; access is revocable
  by disabling the account in the identity provider (effective within ~1h).
- Per-developer **attribution** in CloudTrail via the role session name (the
  signed-in UPN).

### Assets
- The OIDC `id_token` file (read by the AWS SDK).
- The MSAL token cache (contains the refresh token).
- The managed AWS environment variables / `env.sh`.
- The configuration (`tess-config.json`).

### Protections in place
- **Token file `0600`**, data and config directories **`0700`** — not readable
  or enumerable by other local users. The token write uses `O_NOFOLLOW` where
  supported (symlink hardening) on top of the private directory.
- **Token is never placed in the environment** — only its *file path* is. So
  `ps`, environment dumps, and `tess config` cannot leak the token itself.
- **MSAL refresh token encrypted at rest** via the OS keychain (DPAPI / macOS
  Keychain / libsecret). If no keychain is available, tess falls back to a
  plaintext cache **locked to `0600`** and **warns** the user.
- **Atomic token swap** (write-temp + rename) — the SDK never reads a partial
  token.
- **Auth-code flow uses PKCE and `state`** (via MSAL), so an intercepted
  loopback redirect cannot be replayed and the callback is CSRF-protected.
- **Config is validated** (GUID / ARN / region formats), which rejects shell
  metacharacters; values written into `env.sh` are additionally single-quote
  escaped — a config cannot inject code into your shells.
- **No `shell=True`, `eval`, or `os.system`**; all subprocess calls are
  list-form. The raw token is **never printed or logged**.
- **Runs unprivileged** — no `sudo`, no setuid.
- **Pinned dependencies** (`src/requirements.txt`).

### Trust assumptions
- **The host is not already compromised.** An attacker with your OS account (or
  root/admin) can read your keychain, your token, and your environment — tess
  cannot defend against that, and neither can any local credential tool.
- **You trust the source of your `tess-config.json`.** Whoever can write it
  controls which Entra tenant/app you authenticate against. The config
  directory is `0700`, so only you can write it; keep it that way and obtain
  config from a trusted channel.
- **AWS-side authorization is the deployer's responsibility.** Whether a token
  may assume a role is enforced by the IAM OIDC provider + role trust policy in
  *your* AWS account, not by tess.

### Out of scope
- Compromise of the host OS, the user account, or root/admin.
- AWS SDK-internal credential handling. tess writes the OIDC token to a file;
  the SDK independently exchanges it for AWS credentials and caches/refreshes
  those **in the application process** — tess has no visibility into or control
  over that step.
- The IAM role trust policy / OIDC provider configuration in your AWS account.
- Hardware-bound token binding (e.g. Entra PRT/WAM). Out of scope for a
  portable, cross-platform tool; the refresh token at rest is protected by the
  OS keychain, the 8-hour cap, and cache-clear on start/stop.

## Audit logging

tess keeps a local audit trail at `<data-dir>/daemon.log`, one line per event,
rotated daily and retained ~30 days. It records the events tess can observe:

- interactive sign-ins (MFA) that begin a session,
- each background **OIDC (Entra) token refresh**, with old → new expiry,
- session stop, the 8-hour cap, re-auth-needed, give-up, and logout cleanup.

It does **not** (and cannot) record AWS access-key refreshes — those happen
inside the application's AWS SDK, in a different process. The raw token is
never written to the log; the signed-in UPN may appear, so the log is `0600`.

## Supply chain

Dependencies are installed from `src/requirements.txt`, which pins **exact**
versions — you get the exact builds this release was tested against, with no
surprise minor/patch upgrades. PyPI does not allow re-uploading a version, so an
exact pin effectively locks the artifact against ordinary tampering.

Hashed lockfiles (`pip install --require-hashes`) additionally defend against a
compromised package index serving a tampered build of a pinned version. They are
**optional** here and not used by default: because `desktop-notifier` and
`msal-extensions` resolve to different backends per OS, a correct hashed lock
must be generated **per platform** (and regenerated on every dependency bump) —
disproportionate for most deployments. The header of `requirements.txt` documents
how to generate one if your environment requires it.
