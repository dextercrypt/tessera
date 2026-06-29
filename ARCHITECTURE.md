# tess — Architecture

`tess` is a laptop CLI that gives developers short-lived, federated AWS
credentials — the same behavior pods get from IRSA, but on a laptop and with no
static keys and no `~/.aws`. You run `tess start`, sign in to Microsoft Entra in
the browser (with MFA), and tess receives an OIDC **id_token**. It writes that
token to a file (locked to `0600`) and sets a handful of `AWS_*` environment
variables. From then on, any AWS SDK or CLI you run calls STS
`AssumeRoleWithWebIdentity` with that id_token and gets temporary AWS
credentials on its own. A small background daemon silently re-mints the id_token
about once an hour so the file never goes stale, until an 8-hour cap is reached
or you log out. The diagrams below show the pieces, the `tess start` flow, and
the refresh loop.

---

## 1. Components — what trusts and talks to what

The three worlds: your **laptop** (the tess CLI, its background daemon, the
token file, and the synced env vars), **Microsoft Entra** (the identity
provider, with a single app per environment whose assignment list gates who may
sign in), and **AWS** (which trusts Entra as an OIDC provider and hands out
temporary credentials via STS). Entra issues the id_token; AWS trusts the
id_token's `aud` (the app's `client_id`); the AWS SDK reads the token file and
env vars to talk to STS.

```
  ===================  LAPTOP  ====================
 |                                                 |
 |   tess CLI                  background daemon    |
 |   (tess start)              (silent refresh)     |
 |       |                          |               |
 |       |  writes id_token         | re-writes     |
 |       v          + env vars      v id_token       |
 |   +-------------------+      +-----------------+  |
 |   | token file (0600) |      |  AWS_ROLE_ARN   |  |
 |   | the id_token      |      |  AWS_REGION ... |  |
 |   +-------------------+      +-----------------+  |
 |       ^                          ^               |
 |       | reads token + env        |               |
 |   +---------------------------------+            |
 |   |  any AWS SDK / CLI (mvn, boto)  |            |
 |   +---------------------------------+            |
 |       |                                          |
  =======|==========================================
         |  sign-in (browser, MFA)        | AssumeRoleWithWebIdentity
         v                                v  (presents the id_token)
  ===============================   ============================
 |     MICROSOFT ENTRA           | |          AWS              |
 |                               | |                           |
 |  one app per environment      | |  OIDC identity provider   |
 |  client_id = token `aud`      | |  (trusts the Entra issuer)|
 |                               | |          |                |
 |  "Assignment required = Yes"  | |          v                |
 |   gate: is the user assigned? | |  IAM role + trust policy  |
 |   yes -> issue id_token       | |  (trusts iss + aud)       |
 |   no  -> AADSTS50105 DENIED   | |          |                |
 |                               | |          v                |
 |          issues id_token -----+-+--> STS hands back         |
 |          (aud = client_id)    | |     temporary credentials |
  ===============================   ============================
```

---

## 2. `tess start` flow — from command to temporary credentials

A clean top-to-bottom sequence: you run the command, tess opens the browser, you
authenticate (MFA), Entra checks the assignment gate and issues an **id_token**,
tess writes the token file and syncs the env vars and spawns the daemon — and
then your AWS SDK takes over, using the token to assume the role via STS.

```
  you: $ tess start
        |
        v
  tess: resolve config (tenant_id, client_id, role_arn, region)
        clear MSAL cache  ->  forces fresh MFA
        |
        v
  tess: open browser to Microsoft Entra
        |
        v
  Entra: you sign in + complete MFA
         gate check: "Assignment required = Yes"
            +-- assigned     --> continue
            +-- NOT assigned --> AADSTS50105, sign-in DENIED  (stop)
        |
        v
  Entra: issue OIDC id_token  (aud = app client_id)
        |
        v
  tess: write token file (0600)        = AWS_WEB_IDENTITY_TOKEN_FILE
        sync env vars:
            AWS_ROLE_ARN              (from config role_arn)
            AWS_REGION / AWS_DEFAULT_REGION
            AWS_ROLE_SESSION_NAME     (from token UPN -> CloudTrail)
            AWS_STS_REGIONAL_ENDPOINTS = regional   (set once at setup)
        spawn background refresh daemon
        |
        v
  ---- session is now active; any AWS SDK / CLI can be used ----
        |
        v
  AWS SDK: read token file + env vars
           call STS AssumeRoleWithWebIdentity(role_arn, id_token)
        |
        v
  AWS STS: verify id_token (iss, aud) against the role trust policy
           return TEMPORARY AWS credentials
        |
        v
  your AWS calls now work, with short-lived federated creds
```

---

## 3. Background refresh loop — keeping the id_token fresh

After `tess start`, the daemon sleeps in short ticks. On each scheduled interval
(~50 min by default) or on a manual `tess refresh` signal, it silently re-mints
the id_token (no browser, no MFA), writes the new token to the same file, and
advances the expiry. On failure it backs off and retries; it stops cleanly when
the 8-hour cap is reached or you log out / `tess stop`.

```
  daemon running (spawned by `tess start`)
        |
        v
  +--> tick (wake every few seconds) ----------------------+
  |     |                                                  |
  |     v                                                  |
  |   8-hour cap reached?  --yes--> clean up, notify, STOP  |
  |     | no                                               |
  |     v                                                  |
  |   logout / `tess stop`? (SIGTERM) --> clean up, STOP    |
  |     | no                                               |
  |     v                                                  |
  |   time for refresh?  (interval elapsed OR              |
  |                       `tess refresh` signal)           |
  |     | no --> sleep, loop back ------------------------>+
  |     | yes                                              |
  |     v                                                  |
  |   silent refresh via MSAL  (force a fresh id_token,    |
  |                             no browser)                |
  |     |                                                  |
  |     +-- success --> write token file (0600)            |
  |     |               advance expiry                     |
  |     |               reset failure counter --> loop --->+
  |     |                                                  |
  |     +-- failure --> log, back off 5 min, retry         |
  |                     notify "re-authenticate"           |
  |                     persists > 30 min --> clean up, STOP|
  +--------------------------------------------------------+
```
