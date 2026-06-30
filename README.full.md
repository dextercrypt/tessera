# tessera (`tess`)

> Federated AWS credentials for developer laptops — short-lived, no static keys, gated by Microsoft Entra.

### Why "tessera"?

In a Roman camp the *tessera* was the **watchword token** — handed round and
rotated each watch, so a stolen or stale one was worthless. `tess` puts your AWS
identity on the same short clock: **minted at sign-in, auto-rotated by a
background daemon, and useless the moment it expires.**

`tess` gives a laptop the same credential behavior a Kubernetes pod gets from
**IRSA**: short-lived AWS credentials minted from your corporate OIDC identity
provider, with **no long-lived access keys** and **no `~/.aws/`**. It runs on any
developer machine — laptop, desktop, or dev VM — i.e. any interactive human
session (browser sign-in, MFA, a bounded session), as opposed to a pod or CI
role. You run
`tess start` in the morning, sign in to Microsoft Entra in the browser (with
MFA), and from then on every AWS SDK and CLI on your machine transparently
assumes an IAM role. A background daemon keeps the credential fresh until an
8-hour cap or logout.

## How it works

```
tess start ──browser──► Microsoft Entra (sign-in + MFA + assignment gate)
   │  receives an OIDC id_token (aud = app client_id)
   ▼
writes token file (0600) + sets AWS_* env vars + spawns a refresh daemon
   ▼
any AWS SDK/CLI ──► STS AssumeRoleWithWebIdentity ──► temporary AWS creds
```

The AWS SDK reads the token file (`AWS_WEB_IDENTITY_TOKEN_FILE`) and assumes the
role on its own; `tess` never calls AWS APIs. A background daemon silently
re-mints the id_token (~hourly) so the file never goes stale, until the 8-hour
cap or logout. See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagrams.

## Install

Requires **Python 3.10+** on your `PATH`. The one-liners below fetch the pinned,
checksum-verified `v1.0.0` release:

**macOS**
```bash
curl -fsSL https://raw.githubusercontent.com/dextercrypt/tessera/v1.0.0/setup/setup-macos.sh | bash
```

**Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/dextercrypt/tessera/v1.0.0/setup/setup-linux.sh | bash
```

**Windows (PowerShell)**
```powershell
irm https://raw.githubusercontent.com/dextercrypt/tessera/v1.0.0/setup/setup-windows.bat -OutFile setup.bat; .\setup.bat
```

The setup scripts are **bootstrap-only**: they create a dedicated Python venv,
install pinned dependencies, copy `tess.py` into your data dir, drop a
`tess-config.example.json` template, put a `tess` command on your `PATH`, set the
two constant env vars, and register a logout-cleanup hook. They never ask for or
write your real config.

> Prefer to inspect first? Clone the repo and run `setup/setup-<os>.sh` (or
> `setup\setup-windows.bat`) directly — the same scripts run from a checkout.
> After install, open a new terminal. For GUI apps to pick up the AWS env vars: on
> **macOS**, relaunch the app after `tess start` (log out/in if needed); on
> **Windows/Linux**, sign out and back in once.

## Configure

`tess` ships with **no org config** — only the template. Create your real config
once by filling in the four required values (two optional keys tune behavior):

```json
{
  "tenant_id": "<entra-tenant-guid-or-domain>",
  "client_id": "<entra-app-client-id>",
  "role_arn":  "arn:aws:iam::<account-id>:role/<role-name>",
  "region":    "us-east-1",

  "refresh_interval_minutes": 50,
  "session_max_hours": 8
}
```

**Required**

| Key | What it is |
|---|---|
| `tenant_id` | Your Entra **tenant** GUID or domain. |
| `client_id` | The Entra app registration's **Application (client) ID**. This value is the OIDC token's audience (`aud`) that the AWS role's trust policy must trust. |
| `role_arn` | ARN of the IAM role to assume, e.g. `arn:aws:iam::<account-id>:role/<role-name>`. |
| `region` | AWS region, e.g. `us-east-1`. |

**Optional** — the security / tuning knobs; omit either to use its default:

| Key | Default | What it controls |
|---|---|---|
| `refresh_interval_minutes` | `50` | How often the background daemon re-mints the OIDC `id_token` so the credential never goes stale. OIDC tokens last ~1h, so refreshing at 50 min keeps a safe margin. Must be a positive number. |
| `session_max_hours` | `8` | Hard cap on total session length. When reached, `tess` stops refreshing and the session ends, forcing a fresh MFA sign-in — bounding how long a single sign-in stays valid. Must be a positive number. |

`tess` resolves the config file in this order (first match wins):
`--config <path>` → `$TESS_CONFIG` → `./tess-config.json` (current dir) →
`<config-dir>/tess-config.json` (the default global location:
`~/.config/tess` on macOS/Linux, `%APPDATA%\tess` on Windows). The winner is
printed at every `tess start` and shown by `tess config`.

```bash
# macOS / Linux
cp ~/.config/tess/tess-config.example.json ~/.config/tess/tess-config.json
# then edit tenant_id, client_id, role_arn, region
```

To create the Entra app registration (one per environment) and the matching AWS
OIDC provider + IAM role, follow [SETUP-AZURE-APPS.md](SETUP-AZURE-APPS.md).

## Usage

| Command | What it does |
|---|---|
| `tess start` | Browser sign-in (MFA), write the token + AWS env vars, spawn the refresh daemon. `--config`, `--force`, `--quiet`. |
| `tess stop` | End the session: kill the daemon, delete session files, clear the MSAL cache. |
| `tess status` | Show identity, token expiry, 8-hour cap, and health. `--verbose`, `--json`. |
| `tess refresh` | Force an immediate token rotation now (waits up to 10s for the daemon). |
| `tess logs` | Show recent refresher/audit activity. `-n/--lines`, `-f/--follow`. |
| `tess config` | Show which config resolved, how, any drift, and every managed AWS env var. |
| `tess version` | Print the version, resolved config summary, and paths. |

Day to day it's just `tess start` in the morning; the session ends on its own at
logout, shutdown, or the 8-hour cap.

## Docs

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component, `tess start`, and refresh-loop diagrams. |
| [ACCESS-CONTROL.md](ACCESS-CONTROL.md) | How Entra gates *who* can get credentials, and why. |
| [SETUP-AZURE-APPS.md](SETUP-AZURE-APPS.md) | Step-by-step runbook: Entra app + AWS OIDC provider + IAM role. |
| [DESIGN.md](DESIGN.md) | Full design, the refresh daemon, env-var propagation, and hardening rationale. |
| [SECURITY.md](SECURITY.md) | Threat model, protections, and how to report a vulnerability. |

## Security

Credentials are short-lived (≤1h AWS creds, ≤1h OIDC id_token) with an 8-hour
session cap and fresh MFA each morning. No static AWS keys touch disk — only the
short-lived id_token (file mode `0600`) and an OS-keychain-encrypted MSAL refresh
token. The whole program is a single, auditable Python file (`src/tess.py`), and
the installer is checksum-verified. See [SECURITY.md](SECURITY.md) for the full
threat model and reporting process.

## Uninstall

```
uninstall/uninstall-macos.sh      # macOS
uninstall/uninstall-linux.sh      # Linux
uninstall\uninstall-windows.bat   # Windows
```

Removes the session, logout hook, env vars, `tess` command, and data dir. Your
own `tess-config.json` is preserved.

## License

[MIT](LICENSE) © 2026 dextercrypt. Contributions welcome — open an issue or PR.
