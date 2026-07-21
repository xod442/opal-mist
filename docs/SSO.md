# SSO auto-login (HPE Okta / Shibboleth)

Opal-Mist runs behind Apache + Shibboleth on TheEdge, so users already
authenticate with Okta before reaching the app. This document describes the
**trusted-header SSO** that removes the app's second login: matching users are
logged in automatically and first-time users are auto-provisioned.

> Tracking issue: xod442/opal-mist#1

## How it works

```
Browser ──HTTPS──> Apache (Shibboleth / Okta) ──http──> 127.0.0.1:9091 (opal-mist container)
                    │ requires a valid Okta session
                    │ forwards identity as X-Remote-* headers
```

1. Apache requires an Okta/Shibboleth session for `/opal-mist/` and forwards the
   authenticated identity to the app as request headers.
2. On the first request of a browser session, the app's `sso_auto_login`
   middleware reads `X-Remote-Email`, then:
   - **matches an existing user by email** (case-insensitive) → logs them in; or
   - **auto-provisions** a new account (`username` = email, `role` = `user`,
     random/unusable password) → logs them in.
3. It mints the normal signed session cookie, so the rest of the app is
   unchanged. Later requests use the cookie (no per-request header lookup).

Disabled accounts (`is_active = 0`) are denied even with a valid SSO header.

## App configuration (env)

| Variable            | Default              | Purpose                                         |
|---------------------|----------------------|-------------------------------------------------|
| `SSO_ENABLED`       | `false`              | Master switch. `docker-compose.yaml` sets `true`. |
| `SSO_EMAIL_HEADER`  | `x-remote-email`     | Header carrying the Okta email (REMOTE_USER).   |
| `SSO_NAME_HEADER`   | `x-remote-displayname` | Header carrying the display name (reserved).   |

With `SSO_ENABLED=false` the app behaves exactly as before (local login form
only) — this is the rollback switch.

## Identity & accounts

- **Match key:** email, case-insensitive (`lower(users.email) == lower(header email)`).
- **New users:** `username` = the full email (guaranteed unique), `role` = `user`
  (least privilege), `password_hash` = random & unusable, `is_active = 1`,
  `must_change_password = 0`.
- **Existing local users** are unaffected; the built-in `admin` account has an
  empty email, so it never matches SSO and stays local-login only (break-glass).
- **Local login** (`/login`) still works for any account with a real password.

## Admin elevation

Auto-provisioned users are non-admin. An admin promotes/demotes accounts from
**Admin → Users** with the **Make admin / Make user** button
(`POST /admin/users/{id}/role`). You cannot change your own role.

## Security — no direct access

The trusted headers are only safe if the app can be reached **exclusively**
through the proxy. Therefore the container binds to loopback:

```yaml
ports:
  - "127.0.0.1:9091:8000"   # not 0.0.0.0
```

Apache proxies to `localhost:9091`, so nothing else changes. Do **not** publish
the port publicly, or the SSO headers become spoofable.

## Apache change on TheEdge (`/etc/httpd/conf.d/ssl.conf`)

Inside the existing `<Location /opal-mist/>` block (which already has
`require shib-session`), forward the identity and strip any client-supplied
`X-Remote-*` so it can't be injected:

```apache
<Location /opal-mist/>
    AuthType shibboleth
    ShibRequestSetting requireSession 1
    require shib-session

    # Strip inbound spoofed identity headers, then set from the shib session.
    RequestHeader unset X-Remote-Email
    RequestHeader unset X-Remote-User
    RequestHeader unset X-Remote-Displayname
    RequestHeader set X-Remote-Email       "expr=%{REMOTE_USER}"
    RequestHeader set X-Remote-User        "expr=%{REMOTE_USER}"
    RequestHeader set X-Remote-Displayname "expr=%{reqenv:displayName}"
</Location>
```

`REMOTE_USER` is the Okta email (per `shibboleth2.xml`). Reload with
`sudo apachectl configtest && sudo systemctl reload httpd`.

## Rollout

1. Merge this change; on TheEdge `git pull` in `/home/rick.kauffman/opt/opal-mist`.
2. Apply the Apache block above and reload httpd.
3. `docker compose up -d --build` (loopback bind + `SSO_ENABLED=true`).

## Rollback

- Quick: set `SSO_ENABLED=false` and `docker compose up -d` → login form returns.
- Full: revert this PR and redeploy; optionally restore the previous `<Location>`
  block (removing the `RequestHeader` lines).

## Verification

- [ ] Existing user (email on file) → auto-logged-in, same account, no duplicate row.
- [ ] New Okta user → one new `users` row; a second visit reuses it.
- [ ] Disabled user with valid header → access denied.
- [ ] `SSO_ENABLED=false` → local login only (unchanged behaviour).
- [ ] Off-box request to `:9091` refused; spoofed `X-Remote-Email` ignored.
- [ ] Admin can promote/demote via **Admin → Users**.
