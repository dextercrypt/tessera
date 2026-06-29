# Setup — creating the Entra (Azure AD) app for tessera

A precise runbook for creating the **one** app registration tessera needs **per
environment**. For the reasoning behind it, see
[ACCESS-CONTROL.md](ACCESS-CONTROL.md).

One app registration per environment is the **login identity**, the **access
gate**, and the **audience** AWS trusts — all at once. `<env>` = `sandbox` / `qa` /
`prod`; repeat this runbook **once per environment** (examples use `sandbox`).

## Prerequisites

- Entra role: **Application Administrator** (to create the app + manage
  assignments).
- For the `az` commands: `az login --allow-no-subscriptions`.

Each step gives the **portal** path and the equivalent **`az`** command. Two portal
blades are involved:
- **App registrations** — where you *create/configure* the app.
- **Enterprise applications** — the app's *instance*; used only for **Assignment
  required** + **Users and groups**. (Registering the app auto-creates this entry.)

---

## Part 1 — create the app `tessera-oauth-sandbox-01`

**Register as a public client** — blade **App registrations** → **New
registration**, name `tessera-oauth-sandbox-01`, **Accounts in this organizational
directory only** (single tenant), Redirect URI platform **Public client/native
(mobile & desktop)** → `http://localhost` → **Register**. Copy the **Application
(client) ID** → this is your `client_id`.

The **Mobile and desktop** redirect URI is what marks the app as a public client —
no other flag is needed for tess's interactive loopback flow.

```bash
CLIENT_APP_ID=$(az ad app create \
  --display-name "tessera-oauth-sandbox-01" \
  --sign-in-audience AzureADMyOrg \
  --public-client-redirect-uris "http://localhost" \
  --query appId -o tsv)
az ad sp create --id "$CLIENT_APP_ID" >/dev/null
echo "client_id = $CLIENT_APP_ID"
```

---

## Part 2 — turn on the gate + assign users

**2.1 Require assignment** — blade **Enterprise applications** → `tessera-oauth-sandbox-01`
(set filter to *Application type = All Applications* if hidden) → **Properties** →
**Assignment required? = Yes** → **Save**.

```bash
SP_ID=$(az ad sp show --id "$CLIENT_APP_ID" --query id -o tsv)
az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID" \
  --headers "Content-Type=application/json" \
  --body '{"appRoleAssignmentRequired": true}'
```

**2.2 Assign who's allowed** — same Enterprise app → **Users and groups** → **Add
user/group** (a user on any tier; a **group** requires Entra ID **P1**).

```bash
USER_ID=$(az ad user show --id "you@your-org.com" --query id -o tsv)
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$USER_ID\",\"resourceId\":\"$SP_ID\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}"
```

> The `appRoleId` `00000000-…-0` is Entra's built-in **"default access"** role,
> used when the app defines no custom app roles.

---

## Part 3 — tess config (`tess-config.sandbox.json`)

```json
{
  "tenant_id": "<tenant-guid>",
  "client_id": "<tessera-oauth-sandbox-01 app id>",
  "role_arn":  "<sandbox AWS role arn>",
  "region":    "us-east-1"
}
```

tess requests `<client_id>/.default` automatically — no scope or extra fields
needed.

---

## Part 4 — AWS (OIDC provider + role)

AWS must (a) trust your Entra tenant as an identity provider, and (b) have a role
whose trust policy accepts this app's `client_id` as the audience.

### 4.1 Create the OIDC identity provider

The provider represents your Entra **tenant** in this AWS account.

**Console:**
1. **IAM → Identity providers → Add provider.**
2. Provider type: **OpenID Connect.**
3. **Provider URL:** `https://login.microsoftonline.com/<tenant_id>/v2.0` → click
   **Get thumbprint**.
4. **Audience:** the app's `client_id`.
5. **Add provider.**

**AWS CLI:**
```bash
aws iam create-open-id-connect-provider \
  --url "https://login.microsoftonline.com/<tenant_id>/v2.0" \
  --client-id-list "<client_id>" \
  --thumbprint-list "626d44e704d1ceabe3bf0d53397464ac8080142c"
```

> AWS no longer relies on the thumbprint to validate Entra tokens (it trusts the
> public CA), but the API still accepts one; the console's **Get thumbprint**
> button fills it for you.

> **More than one environment in the same AWS account?** The provider is
> per-**tenant**, not per-app — create it once per account, then for each further
> environment just add that env's `client_id` as an audience to the existing one:
> ```bash
> aws iam add-client-id-to-open-id-connect-provider \
>   --open-id-connect-provider-arn "arn:aws:iam::<account-id>:oidc-provider/login.microsoftonline.com/<tenant_id>/v2.0" \
>   --client-id "<other env client_id>"
> ```

### 4.2 Create the role + trust policy

**The trust policy** (replace `<account-id>`, `<tenant_id>`, `<client_id>`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<account-id>:oidc-provider/login.microsoftonline.com/<tenant_id>/v2.0"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "login.microsoftonline.com/<tenant_id>/v2.0:aud": "<client_id>"
      }
    }
  }]
}
```

**Console:**
1. **IAM → Roles → Create role.**
2. Trusted entity type: **Web identity.**
3. **Identity provider** = the one from 4.1; **Audience** = your `client_id`. (The
   console builds the trust policy above from these.)
4. **Add permissions** — attach what the role may do (e.g.
   `AmazonS3ReadOnlyAccess`, or your own policy).
5. Name it (e.g. `tessera-sandbox`) → **Create role**.
6. Open the role → **Trust relationships** → confirm the `…/v2.0:aud` condition
   shows your `client_id`. Copy the **Role ARN** → your config's `role_arn`.

**AWS CLI:** save the JSON above as `trust.json`, then:
```bash
aws iam create-role --role-name tessera-sandbox --assume-role-policy-document file://trust.json
aws iam attach-role-policy --role-name tessera-sandbox \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
```

> The condition key is the provider URL **without** the `https://` scheme, suffixed
> with `:aud`, and the audience is the app's `client_id` (the token's `aud`). **No
> per-user `sub` conditions** — Entra assignment decides the people.

---

## Verify

1. `tess start --config tess-config.sandbox.json` (while assigned) → signs in.
2. `tess refresh` → **"Token refreshed"**.
3. `aws sts get-caller-identity` → returns `…assumed-role/<role>/<you>`.
4. Have a **non-admin** colleague who is **not assigned** run `tess start` → they
   get **`AADSTS50105`** ("not assigned"). Assign them → they get in. ← proves the
   gate.

> ⚠️ **Test the gate with a NON-admin account.** Privileged directory admins
> (Global Administrator, etc.) **bypass the assignment requirement** — an
> unassigned admin will still sign in, making it look like the gate is broken. It
> isn't: a normal unassigned user gets `AADSTS50105`. Always verify with an
> ordinary (non-admin) user — using *your* admin account, even in incognito, will
> still bypass the gate.

Common issues: [ACCESS-CONTROL.md › Troubleshooting](ACCESS-CONTROL.md#troubleshooting).

---

## Appendix — one-shot `az` script

Run after `az login --allow-no-subscriptions`. Edit the top two values.

```bash
#!/usr/bin/env bash
set -euo pipefail

ENV="sandbox"                       # sandbox | qa | prod
ASSIGN_USER_UPN="you@your-org.com"  # individual user (free tier; groups need P1)
APP_NAME="tessera-oauth-${ENV}-01"

CLIENT_APP_ID=$(az ad app create --display-name "$APP_NAME" \
  --sign-in-audience AzureADMyOrg \
  --public-client-redirect-uris "http://localhost" --query appId -o tsv)
SP_ID=$(az ad sp create --id "$CLIENT_APP_ID" --query id -o tsv)

# require assignment
az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID" \
  --headers "Content-Type=application/json" \
  --body '{"appRoleAssignmentRequired": true}'

# assign the user
USER_ID=$(az ad user show --id "$ASSIGN_USER_UPN" --query id -o tsv)
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
  --headers "Content-Type=application/json" \
  --body "{\"principalId\":\"$USER_ID\",\"resourceId\":\"$SP_ID\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}"

echo "client_id = $CLIENT_APP_ID   → put in tess-config.${ENV}.json + AWS OIDC audience + role trust"
```
