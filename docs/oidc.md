# OIDC sign-in

This is an integration component, not an enabled deployment. The application must
register revision 0006 after revision 0005, install the OIDC router and make only
`GET /auth/oidc/start` and `GET /auth/oidc/callback` public in the default-deny
guard. The local administrator and its recovery procedure remain necessary.
Configure exactly one trusted issuer, client ID, client secret and externally
reachable HTTPS callback URI. The issuer is an explicit allowlist of one exact
issuer; discovery must return that same issuer. TLS verification is required.
The issuer and callback URI must use HTTPS, even when cookies are configured
without Secure for local testing. No client secret belongs in source control.

Configure the host with `OIDCConfig(issuer=..., client_id=..., client_secret=...,
redirect_uri=..., secure_cookie=...)` and mount
`create_oidc_router(get_db, config)`. Its default successful destination is
`/ui/`; configure a local absolute path if needed. Add direct runtime dependencies
`httpx` and `PyJWT[crypto]>=2.10,<3`. Set the same cookie security policy as local
sign-in. Restart the application after changing configuration. The component has
no settings wired into the application yet.

## Microsoft Entra example

1. Register a single-tenant application in Microsoft Entra ID. Record the tenant
   ID and application (client) ID. Configure a Web redirect URI of
   `https://YOUR_PLUMBLINE_HOST_HERE/auth/oidc/callback`.
2. Create a client secret, store its value in the service secret store, and plan
   rotation before expiry. Use the secret value, not its identifier. Assign users
   to the application according to your tenant policy.
3. Set the issuer to
   `https://login.microsoftonline.com/YOUR_TENANT_ID_HERE/v2.0`, client ID to
   `YOUR_CLIENT_ID_HERE`, client secret to `YOUR_CLIENT_SECRET_HERE`, and the
   callback to the exact registered URI. Supply these through the host's eventual
   protected settings, not URL parameters or a committed environment file.
4. Apply migrations, bootstrap a local administrator, and test with a permitted
   account. The first sign-in creates a viewer. A local administrator can promote
   or disable that user through existing account management. Tenant application
   roles, groups, email and other token claims never grant local permissions.

Live Entra registration and sign-in have not been verified. HTTPS and the public
callback need a properly configured reverse proxy. Suppress query strings for
callback access logs at the proxy and application server: authorization codes and
state arrive in the callback URL. Never log provider tokens, client secrets, cookie
values, request bodies or raw identity claims. Provider errors are returned as
generic messages.

## Identity, sessions and revocation

Identity is the exact issuer and subject pair. Email is neither a key nor a
linking mechanism, even when it matches a local account. Generated local usernames
are opaque. An administrator assigns the local Viewer or Admin role using the
existing user endpoint. A role or active-state change revokes all local sessions
and API tokens for that user; the guard also checks active status on every request.
A disabled account cannot sign in again until locally re-enabled. Existing local
last-administrator protections apply. Local logout revokes the current local
session only and does not sign out of the identity provider.

Provider-side disablement or token revocation is checked only at the next sign-in;
there is no back-channel logout, continuous access evaluation, userinfo polling,
refresh token storage or automatic local session revocation. A successful local
session lasts up to 12 hours unless locally revoked. To stop access immediately,
disable the local user (which also revokes its sessions and API tokens). Removing
an issuer configuration prevents new OIDC sign-ins but does not revoke existing
local sessions. Keep database backups and access controls consistent with the
sensitivity of identity bindings and short-lived PKCE transactions. Expired login
transactions are removed on the next sign-in start. They are consumed before code
exchange and cannot be retried; users must start again after an error.

Metadata and JWKS fetches use HTTPS, no redirects, a 256 KiB response cap and
HTTP timeouts; ID tokens require an RSA RS256 signature, issuer, audience, expiry,
issued-at, subject and the transaction nonce. Multiple audiences require the
client as authorised party. The provider must support authorization code with
S256 PKCE and client-secret basic authentication. No live provider is contacted by
the focused tests.

The public start endpoint creates a database transaction and contacts discovery on
each request. Apply an appropriate reverse-proxy request rate limit and monitor
database capacity before exposing it publicly; this component has no built-in
per-client rate limiter.
