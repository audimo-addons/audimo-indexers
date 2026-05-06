# audimo-indexers
#
# Public-host-safe addon: indexer + debrid only, no libtorrent peering.
# Users' desktop apps run their own bundled streaming sidecar to peer
# torrents; this container never opens a peer connection. So no
# python3-libtorrent, no DHT state, no peer ports.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: kept for any future audio-fixup paths; not currently used by
# this addon directly, but the upstream audimo-aio image carries it
# and we'd rather have it ready than rebuild later.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY server.py bep15.py cache_db.py ./
COPY clients ./clients
COPY indexers ./indexers

# State dir holds the SQLite cache of indexer queries + debrid library
# snapshots. Survives container recreates when mounted as a volume:
#   -v indexers-state:/state
ENV AUDIMO_STATE_DIR=/state \
    AUDIMO_CACHE_DB=/state/cache.db \
    AUDIMO_INDEXERS_MUSIC_DIR=/downloads/music \
    AUDIMO_INDEXERS_AUDIOBOOK_DIR=/downloads/audiobooks
VOLUME ["/state"]

EXPOSE 9005

# --no-access-log: the {config} segment in URLs encodes user secrets
# (RD key, rutracker session). Default access logs would write the
# full URL to stdout, which would land in container log aggregation.

CMD ["uvicorn", "server:app", \
     "--host", "0.0.0.0", \
     "--port", "9005", \
     "--no-access-log", \
     "--proxy-headers"]
