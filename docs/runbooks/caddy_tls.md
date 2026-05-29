# Runbook — Caddy edge TLS issuer

**Phase 8 Step 3 (R-P7-3).** This runbook covers swapping the
`georag-caddy` HTTPS listener at `:8443` between issuers.

## Default — `internal` (Caddy's embedded CA)

Out of the box, `CADDY_TLS_ISSUER=internal` makes Caddy generate a
self-signed root + intermediate at first boot, then issue a leaf cert
for `localhost`. The root is persisted in the `caddy_data` named
volume so it survives container restarts.

**Trust the dev CA in your client:**

```bash
# Extract the root cert from the running container
docker cp georag-caddy:/data/caddy/pki/authorities/local/root.crt \
    ~/caddy-georag-local-root.crt

# Linux (Debian/Ubuntu)
sudo cp ~/caddy-georag-local-root.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates

# macOS
sudo security add-trusted-cert -d -r trustRoot \
    -k /Library/Keychains/System.keychain ~/caddy-georag-local-root.crt
```

Until you import the root, browsers will warn "not trusted" and
`curl` requires `-k`.

## Production — `acme` (Let's Encrypt or similar)

For a real cert, set two env vars. Both the issuer and the registration
email are env-driven as of Phase 9 Step 3 — no Caddyfile edit needed.
(Note: Caddy v2 calls this directive `email`; older docs sometimes
refer to it as `acme_email` — same thing.)

```bash
# In .env (or the deploy environment):
CADDY_TLS_ISSUER=acme
CADDY_ACME_EMAIL=ops@example.com

# Public hostname: the :8443 site key must be a real hostname that
# resolves to the Caddy container's external IP. ACME HTTP-01 challenge
# also requires port 80 reachable for validation — open it on the host
# and add a `:80 { ... }` site in the Caddyfile if you don't already
# terminate HTTP elsewhere.

# Recreate the container:
docker compose up -d --force-recreate caddy
```

The `caddy_data` volume now also holds the ACME account key + issued
certs. Back it up if you're on a single-instance deploy.

## Switching back to internal

```bash
unset CADDY_TLS_ISSUER  # or set it back to 'internal' in .env
docker compose up -d --force-recreate caddy
```

The cached ACME cert in `caddy_data` is ignored once the issuer
flips back; Caddy issues a new internal-CA leaf at the next request.

## Troubleshooting

- **Boot fails with "automation policy conflict":** make sure
  `auto_https disable_redirects` is in the global block (not
  `auto_https off`, which blocks even the internal issuer's
  automation).
- **`curl: (60) SSL certificate problem`:** dev root not trusted —
  import the cert per the section above or use `curl -k`.
- **ACME challenge times out:** the public hostname doesn't route to
  this Caddy instance, or port 80/443 is blocked. ACME HTTP-01 needs
  port 80 reachable; DNS-01 needs an API token for your DNS provider
  (Caddy supports DNS plugins but they aren't bundled in the stock
  image — would need a custom build).
