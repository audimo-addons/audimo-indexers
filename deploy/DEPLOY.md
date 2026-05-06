# Deploy `audimo-indexers` to DigitalOcean

Single-Droplet, single-container hosted deployment. TLS via Caddy
+ Let's Encrypt. sslip.io for the hostname so you don't need to own
a domain.

**This addon never peers torrents.** It's indexer + debrid only —
libtorrent isn't even installed in the container, so your VPS IP
can't end up in BitTorrent swarms on behalf of strangers. When a
torrent has no debrid hit, the addon emits an `unsupported` SSE
event with code `torrent_no_debrid`; the user's local Audimo desktop
app then routes the peering to its bundled streaming sidecar
(`audimo-streaming` on port 11471) running on their machine, not
yours.

## One-time setup

Install `doctl` if you haven't and authenticate:

```bash
brew install doctl   # or: https://docs.digitalocean.com/reference/doctl/how-to/install/
doctl auth init
```

## Provision the Droplet

From this directory:

```bash
doctl compute droplet create audimo-indexers \
  --image ubuntu-24-04-x64 \
  --size s-1vcpu-1gb \
  --region nyc3 \
  --ssh-keys $(doctl compute ssh-key list -o json | jq -r '.[0].fingerprint') \
  --user-data-file ./cloud-init.yaml \
  --wait
```

`s-1vcpu-1gb` is $6/mo and enough for hundreds of users (the addon
is mostly waiting on outbound HTTP). Bump to `s-1vcpu-2gb` if RD
fan-out latency starts spiking.

Note the public IP from the output. You'll use it for the hostname.

## First boot

The cloud-init script installs Docker, opens the firewall, and clones
this repo to `/opt/audimo-indexers`. It does **not** start the stack
(needs your secrets first).

```bash
ssh root@<droplet-ip>
cd /opt/audimo-indexers/addons/audimo_indexers/deploy
cp .env.example .env
```

Edit `.env`:

- `HOSTNAME`: replace with your Droplet's IP using **dashes**.
  `<your-public-ip>` → `<your-public-ip-with-dashes>.sslip.io`
- `AUDIMO_ADDON_KEY`: long random string. Generate locally:
  `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`

Bring it up:

```bash
docker compose up -d --build
```

Wait ~60 seconds for Caddy to negotiate the Let's Encrypt cert. Then:

```bash
curl -H "X-Audimo-Addon-Key: <your-key>" \
  https://<HOSTNAME>/manifest.json
```

Should return the addon's manifest JSON.

## Wire it into Audimo

In the Audimo desktop app:

1. Addons tab → Install URL.
2. Paste: `https://<HOSTNAME>/<config-segment>` where `<config-segment>`
   is the base64url-encoded JSON of:

   ```json
   {
     "addon_key": "<the same AUDIMO_ADDON_KEY>",
     "rd_api_key": "<user's Real-Debrid token>",
     "src_apibay_enabled": false,
     "src_bitsearch_enabled": false
   }
   ```

   Easier: open `https://<HOSTNAME>/configure` in a browser, fill the
   form, click Save, paste the URL it generates.

## Updating

```bash
ssh root@<droplet-ip>
cd /opt/audimo-indexers
git pull
cd addons/audimo_indexers/deploy
docker compose up -d --build
```

## What this deploys

- 1 container: `audimo-indexers` (FastAPI; no libtorrent at all).
- 1 container: `audimo-caddy` (TLS termination, sslip.io-aware ACME).
- 2 named volumes: `indexers-state` (cache.db — debrid library +
  indexer query snapshots), `caddy-data` (issued certs).

## What this does NOT do

- No multi-user accounts. Each user is identified by their RD api key in their addon URL. The `AUDIMO_ADDON_KEY` is a single shared secret — anyone with it can hit your endpoint.
- No metrics / monitoring. Add Grafana / a `/health` ping later.
- No auto-renew of the SSH key. Standard Droplet hygiene applies.
