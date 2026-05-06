# audimo_indexers

Comet-style sources + debrid addon for Audimo. Designed to be
installed as an "extension" inside the Audimo AIO addon
(`audimo_aio`), but works standalone like any other Audimo addon.

## Public-host-safe

This addon never peers torrents itself. It queries indexers and
resolves torrents through debrid backends (Real-Debrid, AllDebrid,
TorBox, Premiumize, Debrid-Link, EasyDebrid). When a torrent has no
debrid coverage, `resolve.stream` emits an `unsupported` SSE event
with code `torrent_no_debrid` carrying the magnet/info_hash. The
user's local Audimo desktop app routes that to its bundled streaming
sidecar (port 11471, libtorrent lives there) which peers the torrent
locally — the addon never sees torrent bytes.

The practical consequence: this addon is safe to host on a public URL
(DigitalOcean, Fly, …) and let multiple users point their own debrid
keys at it. Each request is stateless on the server side; secrets
travel in the URL config segment per Audimo's standard addon contract.

## What's in here (v0.16.0)

- **6 indexers**: apibay, bitsearch, torrentdownload, prowlarr,
  rutracker, audiobookbay. Per-indexer enable toggles, RTN-style
  ranking by format/bitrate/year/seeders, version-tag penalties so
  studio originals beat instrumental/karaoke/live variants.
- **6 debrid clients** with priority RD → AllDebrid → TorBox →
  Premiumize → Debrid-Link → EasyDebrid. Each one implements
  `find_cached`, `add_and_wait`, `unrestrict_audio`,
  `fetch_downloaded`. The first key configured wins.
- **SQLite cache** at `~/.audimo-indexers/cache.db`. Snapshots each
  debrid's downloaded library (60s TTL) and rutracker query results
  (10min TTL); api keys are sha256-hashed before storage so a leaked
  cache file doesn't leak credentials. Survives uvicorn `--reload`.
- **/admin** dashboard (HTML or JSON via Accept header) — addon
  info, indexer enable/blocked status, debrid configured/active
  flags, SQLite row counts + db size.

## Run locally

```sh
./run_native.sh
# → http://localhost:9005
```

Listens on port 9005 (audimo_aio is on 9004). Cache db lives at
`~/.audimo-indexers/cache.db`.

## Auto-start at login (optional)

Use the repo-root installer, which renders the plist template into
`~/Library/LaunchAgents/` with the right paths for your machine:

```sh
scripts/install_launchd.sh indexers
```

## Endpoints

| Path | What |
|---|---|
| `GET /` | Landing page |
| `GET /health` | `{"status":"ok"}` |
| `GET /version` | id/version/hosted flag |
| `GET /admin` | Dashboard (HTML or JSON by Accept header) |
| `GET /manifest.json` | Audimo addon manifest |
| `GET /configure` | Settings UI; postMessages an install URL back |
| `POST /resolve/sources` | JSON list of ranked torrent + book sources |
| `POST /resolve/sources/stream` | SSE: section per indexer, RD-cache marks emitted as a second pass |
| `POST /resolve/stream` | SSE: cached → fast-path unrestrict; otherwise emits `unsupported: torrent_no_debrid` so the desktop sidecar takes over |
| `POST /cache/resolve` | Re-resolve a stored entry to a fresh stream URL |
| `POST /test/{provider}` | Auth verify for any debrid (`rd`, `alldebrid`, `torbox`, `premiumize`, `debridlink`, `easydebrid`) |

Each source returned is stamped `addon_id = "audimo-indexers"` so the
`audimo_aio` aggregator can route follow-up `resolve.stream` /
`cache.resolve` calls back here.
