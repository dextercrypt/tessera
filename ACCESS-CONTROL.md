# Access Control — gating *who* can use tessera

> **What this doc is.** The *why* behind how tessera controls *which people* can
> obtain AWS credentials — enforced in Microsoft Entra (Azure AD) so that
> **removing someone actually denies them**, without maintaining per-user lists
> in AWS.
>
> **Just want to create the app?** → [SETUP-AZURE-APPS.md](SETUP-AZURE-APPS.md) is
> the precise step-by-step runbook. This doc explains the reasoning.

---

## TL;DR

Use **one Entra app registration per environment**. On that app:

- turn on **Assignment required**, and assign the users (or, with Entra ID **P1**,
  the groups) who should have access → this is the **gate**;
- its **`client_id`** is the token **`aud`** that AWS trusts.

tess signs into that app and gets a token; Entra checks the assignment list on
every sign-in and blocks anyone not on it. **AWS is untouched** by people changes —
it only trusts the per-environment `aud`.

---

## How the gate works

When a user runs `tess start`, tess signs into the environment's app and requests
`<client_id>/.default`. At that sign-in, Entra checks: **is this user assigned to
the app?**

```
        tess start
            │
   sign in to  ────────►  tessera-oauth-<env>   (client_id → token aud)
            │                   │
            │            "Assignment required = Yes" → check the list
            │                   ├─ assigned (directly or via group) → issue token
            │                   └─ not assigned                     → AADSTS50105, DENIED
            ▼
   id_token (aud = client_id) ──► AWS AssumeRoleWithWebIdentity
                                   AWS checks: iss + aud (no sub condition)
```

Add someone under **Users and groups** → they can sign in. Remove them → their next
`tess start` fails with `AADSTS50105`.

---

## Why the gate lives in Entra, not AWS

AWS *can* restrict by the token's `sub`, but per-person filtering in IAM trust
policies is a mess:

- `sub` is an opaque GUID, not an email — trust policies become unreadable.
- Adding/removing a person means editing an **IAM trust policy** (infrastructure)
  for a **people event** — wrong place, often behind change control.
- Trust policies match *literal* claim values; they can't resolve group
  membership. You'd have to enumerate every person's `sub` in every role.

So responsibilities split cleanly:

| Question | Belongs in | Changes |
|----------|-----------|---------|
| *Who is allowed?* (membership) | **Entra** assignment | constantly (joiners/leavers) |
| *What does this environment's role trust?* | **AWS** trust policy (`aud`) | almost never |

AWS trusts the **environment** (the per-env `aud`); Entra decides the **humans**.
Add/remove people = an assignment change in Entra, **zero AWS edits**.

---

## ⚠️ Admins bypass the gate — test with a normal user

Privileged directory admins (Global Administrator, etc.) can reach an app **even
when they aren't assigned** — they bypass the assignment requirement. So if *you*
(an admin) remove your own assignment and still sign in, **that does not mean the
gate is broken.** Always verify with an **ordinary, non-admin** account: an
unassigned normal user gets `AADSTS50105`. This is the single most common source of
"it's not gating!" confusion.

---

## Users vs groups (licensing)

To tess, **users and groups are identical** — both are just *assignments* on the
app. The token is the same either way.

- **Individual users** — works on **any** Entra tier (free included).
- **Groups** — assigning a *group* to an app requires **Entra ID P1**. Without P1
  you assign individual users; with P1 you assign a group and manage membership
  there.

No tess change is needed for either.

---

## Per-environment segregation

Create **one app per environment / AWS account** (a trust boundary): `sandbox`,
`qa`, `prod`, … Do **not** share one app across environments.

Why: with a shared app, every token carries the **same `aud`**, so it's a skeleton
key valid against *any* role that trusts it — and your only separation lever becomes
messy `sub` lists. A per-env app gives each environment its **own `aud`** (AWS keys
on it cleanly) and its **own assignment list** (a dev-only person can't even mint a
prod token).

Don't segment finer than the trust boundary:

- per AWS account / environment → **separate app** ✅
- per IAM role inside one account → **no** (that's IAM's job)
- per team → only if the team has its own account

### Constant vs per-env

| Thing | Scope | Same across envs? |
|-------|-------|-------------------|
| Issuer / IdP URL (`iss`) | tenant | ✅ `https://login.microsoftonline.com/<tenant_id>/v2.0` |
| Audience (`aud`) | the app | ❌ one per env |

The IdP URL never changes (it's your tenant). Only the **`client_id` (`aud`)** is
per-environment.

---

## Naming convention

```
tessera-oauth-<env>-NN     → client_id  (login + gate + audience)
```

- `tessera` — the product (groups all tessera apps together).
- `oauth` — it's the OAuth/OIDC identity app (clear purpose to anyone scanning
  the tenant).
- `<env>` — `sandbox` / `qa` / `prod` — the trust boundary.
- `-NN` — **optional** instance id (`-01`, `-02`) for rotating/recreating the
  registration. Display-name only; Entra assigns the real GUID.

---

## Creating the app

Step-by-step (portal + `az`, per environment) is in
**[SETUP-AZURE-APPS.md](SETUP-AZURE-APPS.md)**, including the AWS OIDC provider +
role trust policy and a verification checklist.

In the tess config the app is just:

```json
{ "client_id": "<tessera-oauth-<env> app id>" }
```

---

## Managing access day-to-day

- **Grant:** add the user (or, with P1, add them to the assigned group) under the
  app's *Users and groups*.
- **Revoke:** remove them from *Users and groups* (or the group).
- **No AWS changes ever** for people events — AWS only trusts the per-env `aud`.

### How fast revocation takes effect

`tess start` clears its token cache and forces a fresh interactive sign-in, so a
removed user is blocked at their **next `tess start`** (typically the next
morning). A session already running keeps working until its next token mint, then
the daemon's silent refresh is re-evaluated against the assignment. There is no
instant kill for an in-flight session on the free tier; the 8-hour session cap
bounds it.

---

## Troubleshooting

- **"I removed myself but I still get in"** — you're almost certainly a
  **privileged directory admin**. Admins **bypass** the assignment requirement.
  The gate is fine — verify with an ordinary **non-admin** user, who will correctly
  get `AADSTS50105`.
- **`AADSTS50105` for someone who *should* have access** — they're not assigned to
  the app (or not in the assigned group). Add them under *Users and groups*.
- **Removal "did nothing" (non-admin)** — confirm **Assignment required = Yes** is
  actually set on the app's **Enterprise application → Properties** (re-open after
  saving; the toggle sometimes doesn't stick), and that the person isn't still
  assigned via a group.
- **AWS `Not authorized to perform sts:AssumeRoleWithWebIdentity`** — AWS-side: the
  role trust policy `aud`/issuer doesn't match. Check the provider URL is the
  **v2.0** form and `aud` equals the app's `client_id`. Use `tess revelio` to read
  the token's `aud`/`iss`/`sub`.
