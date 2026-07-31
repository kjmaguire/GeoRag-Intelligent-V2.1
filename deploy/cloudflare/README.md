# Cloudflare Pages setup (plan C7)

Pages only — no Workers (see plan C7 for why: the RAG latency lives in
backend compute, which Workers can't run and wouldn't help).

This step needs your Cloudflare account login/API token, which this
session doesn't have — the available Cloudflare MCP tools cover
D1/R2/KV/Hyperdrive/Workers management, not Pages project creation. Do
this via the dashboard or `wrangler` CLI with your own credentials.

## 1. Create the Pages project

Dashboard: **Workers & Pages → Create → Pages → Connect to Git**, pick the
GeoRAG repo, branch `main` (or whichever branch you deploy from).

Build settings:
- **Build command**: `npm run build`
- **Build output directory**: `public/build` (Vite's default manifest
  output for a Laravel + Inertia app — confirm against
  `vite.config.ts`'s `build.outDir` if it's been overridden)
- **Root directory**: `/` (repo root — `package.json` lives there)

Or via `wrangler` CLI (`npm install -g wrangler`, then `wrangler login`):

```bash
wrangler pages project create georag-frontend
wrangler pages deploy public/build --project-name georag-frontend
```

## 2. Environment variables

The Vite build needs the backend API origin baked in at build time (check
`resources/js` for `import.meta.env.VITE_*` usage — these are inlined at
build, not runtime). Set in the Pages project's environment variables:

- `VITE_API_BASE_URL` → the Container Apps ingress FQDN for `laravel-octane`
  once C4 fully deploys (e.g. `https://laravel-octane.<env-domain>.eastus.azurecontainerapps.io`)
- Any other `VITE_*` vars currently read from `.env` at build time on the
  existing deploy — audit `resources/js/**` for `import.meta.env` before
  the first real deploy so nothing silently falls back to a dev default.

## 3. DNS

Point your custom domain's CNAME at the Pages project's `*.pages.dev`
hostname (dashboard shows the exact target under **Custom domains**). No
DNS changes needed for the backend — the frontend calls the Container
Apps ingress FQDN directly over HTTPS; Cloudflare's free CDN/TLS applies
only to the static Pages content, not the API calls.

## 4. Reverb / WebSockets

`laravel-reverb` is NOT proxied through Cloudflare Pages — Pages serves
static assets only. The frontend's `window.Echo` client
(`Foundry/Chat.tsx:275` per the plan's Phase A load-bearing note) connects
directly to the Reverb Container App's ingress FQDN over WSS. Confirm
`REVERB_HOST`/`VITE_REVERB_HOST` point at the deployed Container App, not
`laravel-reverb:8080` (the compose-internal hostname) — see
[[project_reverb_dual_purpose_env_2026_05_21]] in memory for the exact
env-var trap this bit before.

## Status

Not started — needs your Cloudflare login. Everything above is ready to
execute once C4's `laravel-octane` and `laravel-reverb` Container Apps
have real ingress FQDNs to point `VITE_API_BASE_URL`/`VITE_REVERB_HOST` at.
