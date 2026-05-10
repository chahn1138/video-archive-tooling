# video-archive-tooling

A personal toolkit for inspecting, repairing, and cataloguing old video archives.

## Goals

- **Inspect** video files for corruption, missing keyframes, or broken containers
- **Repair** recoverable video files (remux, rebuild index, recover partial files)
- **Transcode** videos to modern, space-efficient formats while preserving quality
- **Catalogue** and deduplicate an archive directory (checksums, metadata extraction)

## Prerequisites

| Tool | Purpose |
|------|---------|
| [FFmpeg](https://ffmpeg.org/) | Decode, inspect, remux, and transcode video |
| [MediaInfo](https://mediaarea.net/en/MediaInfo) | Rich container/stream metadata |
| [Python 3.9+](https://www.python.org/) | Scripting glue |

Install on macOS with Homebrew:

```bash
brew install ffmpeg mediainfo python
```

Install on Ubuntu/Debian:

```bash
sudo apt install ffmpeg mediainfo python3
```

## Planned Scripts

| Script | Status | Description |
|--------|--------|-------------|
| `scan.py` | 🔲 planned | Walk a directory tree and report files that fail `ffprobe` validation |
| `repair.py` | 🔲 planned | Attempt remux (container rebuild) for files that have a repairable container error |
| `transcode.py` | 🔲 planned | Batch-transcode to H.265/HEVC inside an MKV container |
| `catalogue.py` | 🔲 planned | Generate a CSV/JSON catalogue with SHA-256 checksums and MediaInfo metadata |
| `dedup.py` | 🔲 planned | Find duplicate files by checksum and/or perceptual video hash |

## Usage

> Scripts will be documented here as they are implemented.

## Notes

- All lossy transcoding keeps the original file untouched; output goes to a separate directory.
- Repairs are always a remux (container-only operation) — no re-encoding unless absolutely necessary.
- SHA-256 checksums are written both into the CSV/JSON catalogue (alongside full MediaInfo metadata) **and** into a per-directory `checksums.sha256` sidecar file so that integrity can be re-verified quickly with standard tools (e.g. `sha256sum -c checksums.sha256`) without loading the full catalogue.
