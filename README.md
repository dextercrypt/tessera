# tessera (`tess`)

> Federated AWS credentials for developer laptops — short-lived, no static keys, gated by Microsoft Entra.

### Why "tessera"?

In a Roman camp the *tessera* was the **watchword token** — handed round and
rotated each watch, so a stolen or stale one was worthless. `tess` puts your AWS
identity on the same short clock: **minted at sign-in, auto-rotated by a
background daemon, and useless the moment it expires.**

`tess` is a cross-platform CLI that gives a laptop **IRSA-like** AWS credentials:
sign in to **Microsoft Entra** (with MFA) → OIDC `id_token` →
`AssumeRoleWithWebIdentity`. No static keys, no `~/.aws/`, an 8-hour session cap,
and a background daemon that auto-refreshes. Access is gated by a single Entra
app per environment ("Assignment required"). It runs on any developer machine —
laptop, desktop, or dev VM — i.e. any interactive human session, as opposed to a
pod or CI role.

## Install

Requires **Python 3.10+** on your `PATH`.

```bash
# macOS
curl -fsSL https://raw.githubusercontent.com/dextercrypt/tessera/v1.0.0/setup/setup-macos.sh | bash

# Linux
curl -fsSL https://raw.githubusercontent.com/dextercrypt/tessera/v1.0.0/setup/setup-linux.sh | bash
```
```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/dextercrypt/tessera/v1.0.0/setup/setup-windows.bat -OutFile setup.bat; .\setup.bat
```

The one-liners fetch the pinned, checksum-verified `v1.0.0` release. Then open a
new terminal. For GUI apps (IntelliJ, etc.) to pick up the AWS env vars: on
**macOS**, relaunch the app after `tess start` (log out/in if it still doesn't see
them); on **Windows/Linux**, sign out and back in once.

> **From source:** `git clone … && setup/setup-<os>.sh` (`setup\setup-windows.bat`
> on Windows) — the same scripts run from a checkout.

## Configure

Create `tess-config.json` with four values:

```json
{
  "tenant_id": "<entra-tenant-guid-or-domain>",
  "client_id": "<entra-app-client-id>",
  "role_arn":  "arn:aws:iam::<account-id>:role/<role-name>",
  "region":    "us-east-1"
}
```

Two optional keys tune behavior: `refresh_interval_minutes` (default 50, the
daemon's refresh cadence) and `session_max_hours` (default 8, the hard session
cap before re-auth). Omit them to use the defaults.

It lives at `~/.config/tess/tess-config.json` (macOS/Linux) or
`%APPDATA%\tess\tess-config.json` (Windows); `tess config` shows the resolved
path. To create the Entra app + AWS OIDC provider + IAM role, see
[SETUP-AZURE-APPS.md](SETUP-AZURE-APPS.md).

## Usage

| Command | What it does |
|---|---|
| `tess start` | Browser sign-in (MFA); write the token + AWS env vars; spawn the refresh daemon. |
| `tess stop` | End the session: kill the daemon, delete session files, clear the MSAL cache. |
| `tess status` | Show identity, token expiry, 8-hour cap, and health. |
| `tess refresh` | Force an immediate token rotation now. |
| `tess logs` | Show recent refresher/audit activity. |
| `tess config` | Show which config resolved, drift, and managed AWS env vars. |
| `tess version` | Print version, config summary, and paths. |

Day to day it's just `tess start` each morning; the session ends on its own at
logout, shutdown, or the 8-hour cap.

## Docs

- [README.full.md](README.full.md) — the complete user guide.
- [ARCHITECTURE.md](ARCHITECTURE.md) — component and flow diagrams.
- [ACCESS-CONTROL.md](ACCESS-CONTROL.md) — how Entra gates who gets credentials.
- [SETUP-AZURE-APPS.md](SETUP-AZURE-APPS.md) — Entra app + AWS role runbook.
- [DESIGN.md](DESIGN.md) — full design and hardening rationale.
- [SECURITY.md](SECURITY.md) — threat model and how to report issues.

## License

[MIT](LICENSE) © 2026 dextercrypt. Contributions welcome — open an issue or PR.
