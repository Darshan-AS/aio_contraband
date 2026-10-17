# Contraband (`dev.darshanas.contraband`)

Curated static Stremio / Nuvio / AIOStreams addon and pure functional compiler for niche courses, rare lectures, documentaries, and unlisted specials (`series` & `movie`).

## Manifest URL

```text
https://raw.githubusercontent.com/Darshan-AS/aio_contraband/main/manifest.json
```

- **Stremio Direct Install:** `stremio://raw.githubusercontent.com/Darshan-AS/aio_contraband/main/manifest.json`
- **AIOStreams / Nuvio / Omni:** Paste the HTTPS Manifest URL above into Custom Addons.

---

## Contraband CLI (`cb`)

This repository includes its own pure functional static addon compiler and TorBox cloud ingestor (`src/contraband/`), managed via `uv`:

```bash
uv sync
```

> **Note:** `cb` automatically loads `.env` from this directory, and falls back to `../stream-weaver/.env` if `stream-weaver` is cloned alongside this repo.

### Commands

```bash
# 1. Add a magnet link, 40-char infoHash, .md magnet list, or .torrent file to TorBox & weave metadata
uv run cb add "magnet:?xt=urn:btih:..."
uv run cb add --from-file magnets.md
uv run cb add --url "https://www.masterclass.com/classes/..." ~/Downloads/course.torrent
uv run cb add --collection "Stand-Up Specials" --type movie "magnet:?xt=urn:btih:..."

# 2. Scan TorBox Cloud Library, weave metadata for completed downloads, and promote queued slots
uv run cb sync

# 3. Continuously watch & drain TorBox queue (keeps 3 parallel downloads active & auto-publishes)
uv run cb watch

# 4. List all active, completed, and queued torrents in your TorBox Cloud Library
uv run cb list

# 5. Build static Stremio metadata directly for an infoHash & publish to GitHub
uv run cb build --hash <40-char-hash> --torrent-id <id> --collection "Stand-Up Specials" --publish
uv run cb publish
```

### Verification Suite

```bash
uv run ruff check src tests
uv run mypy src tests
uv run pytest
```
