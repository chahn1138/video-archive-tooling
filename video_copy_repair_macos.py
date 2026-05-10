#!/usr/bin/env python3
r"""
video_copy_repair_macos.py
----------------------------
Copy video files from one or more sources to a destination, inspecting each
file for common integrity issues and attempting repairs before writing.

PLATFORM: macOS 12 Monterey and later (Apple Silicon and Intel)
          Run from Terminal or iTerm2.

=== Required tools – install ONE of these ways ===
  Option A – Homebrew (https://brew.sh):
    brew install ffmpeg mkvtoolnix
  Option B – MacPorts (https://www.macports.org):
    sudo port install ffmpeg mkvtoolnix
  Option C – Manual:
    Download ffmpeg from https://evermeet.cx/ffmpeg/
    Download MKVToolNix from https://mkvtoolnix.download/

  Verify your install:
    ffmpeg -version
    ffprobe -version
    mkvmerge --version

=== Optional Python package ===
    pip install pymkv2

=== Supported formats and checks ===
  MP4 / MOV / M4V / 3GP   – ffprobe metadata, moov atom detection,
                             stream validity, duration sanity
  MKV / WEBM               – ffprobe + mkvmerge --identify, EBML header
  AVI / WMV / FLV / TS     – ffprobe metadata and stream checks
  All video                 – container signature, zero-byte, SHA-256

=== Repair strategies (least destructive first) ===
  1. Remux (stream copy)    – rebuilds container, no quality loss
  2. Moov faststart         – relocates moov atom to front of MP4/MOV
  3. MKV remux via mkvmerge – rebuilds Matroska structure cleanly
  4. Re-encode (last resort) – hardware accelerated where available:
       NVENC   (Nvidia GPU)
       AMF     (AMD GPU)
       QSV     (Intel QuickSync)
       libx264 (software fallback)

=== RUNDIR ===
  All supporting files live in a _run/ subdirectory of the destination:
    <dest>/_run/video_repair.log
    <dest>/_run/video_repair_journal__<slug>.jsonl
    <dest>/_run/video_repair_report__<slug>.txt
    <dest>/_run/video_repair_report__<slug>.json
  To request a graceful shutdown (finishes current file then exits cleanly):
    touch <dest>/_run/SHUTDOWN
  The script checks for this file between every processed file.

=== macOS-specific notes ===
  - Shells expand globs before Python sees them; quoting ("*.mp4") passes
    them to Python for expansion instead – either way works.
  - AFP / SMB network mounts work transparently via pathlib.
  - HFS+ / APFS are case-insensitive by default; duplicate detection
    normalises paths to lower-case keys to match.
  - ANSI colour is enabled automatically when stdout is a TTY.
    Use --no-colour for plain text (e.g. when piping to a log file).
  - VideoToolbox hardware encoding is available on all Apple Silicon
    and Intel Macs with a supported ffmpeg build.

=== Input modes ===
  Directory   video_copy_repair_macos.py ~/Videos/ ~/Backup/
  Single file video_copy_repair_macos.py clip.mp4 ~/Backup/
  Glob        video_copy_repair_macos.py "~/Videos/*.mkv" ~/Backup/
  Multiple    video_copy_repair_macos.py a.mp4 b.mkv "*.avi" ~/Backup/
  SMB mount   video_copy_repair_macos.py /Volumes/NAS/videos/ ~/Backup/
  List file   video_copy_repair_macos.py --list files.txt ~/Backup/

=== Options ===
  --dry-run        Probe and report without writing files
  --verbose        Show details for all files, not just problems
  --ext            Comma-separated extensions, e.g. .mp4,.mkv
  --list FILE      Text file of source paths/globs (one per line, # = comment)
  --no-reencode    Never attempt re-encode, even as last resort
  --no-hwaccel     Force libx264 software encode (ignore GPU encoders)
  --recursive      Recurse into sub-directories when given a directory (default: on)
  --no-recursive   Do not recurse
  --no-colour      Disable ANSI colour output (plain text)
"""
import argparse
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── ANSI colour – enable VT processing on Windows ──────────────────────────────
_COLOUR_ENABLED = False

if sys.platform == "win32":
    _COLOUR_ENABLED = False
else:
    _COLOUR_ENABLED = True

RESET   = "\033[0m";  BOLD    = "\033[1m";  DIM     = "\033[2m"
GREEN   = "\033[32m"; YELLOW  = "\033[33m"; RED     = "\033[31m"
CYAN    = "\033[36m"; BLUE    = "\033[34m"; MAGENTA = "\033[35m"

def c(text: str, colour: str, use_colour: bool = True) -> str:
    if use_colour and _COLOUR_ENABLED:
        return f"{colour}{text}{RESET}"
    return text

# ── Video format sets ──────────────────────────────────────────────────────────
MP4_EXTS   = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".m4a", ".f4v"}
MKV_EXTS   = {".mkv", ".webm", ".mka"}
AVI_EXTS   = {".avi"}
TS_EXTS    = {".ts",  ".mts", ".m2ts", ".mxf"}
OTHER_EXTS = {".wmv", ".flv", ".asf", ".rm", ".rmvb", ".vob",
              ".mpg", ".mpeg", ".divx", ".ogv", ".ogg"}
ALL_VIDEO_EXTS = MP4_EXTS | MKV_EXTS | AVI_EXTS | TS_EXTS | OTHER_EXTS

# ── Graceful shutdown flag ─────────────────────────────────────────────────────
# Set by SIGINT/SIGTERM handler or detected via SHUTDOWN sentinel file.
# The main loop checks this between every file.
_shutdown_requested = False
_shutdown_reason    = ""

def _handle_signal(signum, frame):
    global _shutdown_requested, _shutdown_reason
    _shutdown_requested = True
    _shutdown_reason    = f"signal {signum} received"
    print(f"\n  [graceful shutdown requested via signal {signum} — "
          f"finishing current file…]", flush=True)

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Data structures ────────────────────────────────────────────────────────────
@dataclass
class Issue:
    severity: str
    code: str
    description: str
    repaired: bool = False
    repair_note: str = ""

@dataclass
class FileResult:
    path: Path
    rel: str
    size_bytes: int
    issues: List[Issue]      = field(default_factory=list)
    sha256: str              = ""
    status: str              = "pending"
    repair_strategy: str     = ""
    elapsed: float           = 0.0
    skip_reason: str         = ""
    # ── new timing / throughput fields ────────────────────────────────────
    inspect_elapsed: float   = 0.0   # seconds spent in _inspect()
    repair_elapsed: float    = 0.0   # seconds spent in repair strategies
    copy_elapsed: float      = 0.0   # seconds spent in shutil.copy2 (clean copy)
    sha_elapsed: float       = 0.0   # seconds spent computing SHA-256
    throughput_mbps: float   = 0.0   # MB/s for the copy/repair write

# ── Run journal ────────────────────────────────────────────────────────────────
class RunJournal:
    """
    Per-run JSONL journal written into <dst>/_run/.
    Trusted terminal states: ok, repaired, done
    Not trusted (re-attempted on resume): partial, failed, skipped
    """
    TRUSTED = {"ok", "repaired", "done"}

    def __init__(self, run_dir: Path, src_label: str, run_ts: str):
        self.run_dir  = run_dir
        self.run_ts   = run_ts
        self._src_slug = self._slugify(src_label)
        self._dst_slug = self._slugify(str(run_dir.parent))
        self._stem     = f"video_repair_journal__{self._src_slug}__{self._dst_slug}"
        self._fname    = f"{self._stem}__{run_ts}.jsonl"
        self._path     = run_dir / self._fname
        self._entries: Dict[str, dict] = {}
        self._loaded_from: Optional[Path] = None

    @staticmethod
    def _slugify(text: str) -> str:
        import re
        s = re.sub(r'[^\w.\-]', '_', text)
        s = re.sub(r'_+', '_', s).strip('_')
        return s[:48]

    def find_previous(self) -> Optional[Path]:
        if not self.run_dir.exists():
            return None
        pattern = f"{self._stem}__*.jsonl"
        candidates = sorted(self.run_dir.glob(pattern),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True)
        candidates = [p for p in candidates if p.name != self._fname]
        return candidates[0] if candidates else None

    def load(self, path: Path) -> int:
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("status") in self.TRUSTED:
                        key = self._key(entry["rel"])
                        self._entries[key] = entry
                        count += 1
                except (json.JSONDecodeError, KeyError):
                    pass
        except OSError:
            pass
        self._loaded_from = path
        return count

    def is_done(self, rel: str) -> Tuple[bool, dict]:
        entry = self._entries.get(self._key(rel), {})
        return (bool(entry), entry)

    def record(self, result: "FileResult"):
        entry = {
            "ts":               datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "run":              self.run_ts,
            "rel":              result.rel,
            "src":              str(result.path),
            "status":           result.status,
            "size":             result.size_bytes,
            "sha256":           result.sha256,
            "elapsed":          round(result.elapsed, 2),
            "inspect_elapsed":  round(result.inspect_elapsed, 2),
            "repair_elapsed":   round(result.repair_elapsed, 2),
            "copy_elapsed":     round(result.copy_elapsed, 2),
            "sha_elapsed":      round(result.sha_elapsed, 2),
            "throughput_mbps":  round(result.throughput_mbps, 2),
            "strategy":         result.repair_strategy,
            "issues": [
                {"severity": i.severity, "code": i.code,
                 "repaired": i.repaired, "note": i.repair_note}
                for i in result.issues
            ],
        }
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass
        if result.status in self.TRUSTED:
            self._entries[self._key(result.rel)] = entry

    def consolidate(self):
        if not self.run_dir.exists():
            return
        all_entries = list(self._entries.values())
        tmp_path = self._path.with_suffix(".jsonl.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in all_entries:
                    f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())
            shutil.move(str(tmp_path), str(self._path))
            if self._loaded_from and self._loaded_from != self._path:
                try:
                    self._loaded_from.unlink()
                except OSError:
                    pass
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def _key(self, rel: str) -> str:
        return rel.lower()   # HFS+ / APFS are case-insensitive

# ── Tool discovery ─────────────────────────────────────────────────────────────
MACOS_SEARCH_PATHS = [
    "/opt/homebrew/bin",          # Apple Silicon Homebrew
    "/usr/local/bin",             # Intel Homebrew / manual installs
    "/opt/local/bin",             # MacPorts
    "/usr/bin",
    os.path.expanduser("~/bin"),
]

def find_tool(name: str) -> Optional[str]:
    for directory in MACOS_SEARCH_PATHS:
        candidate = Path(directory) / name
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)

class ToolSet:
    REQUIRED = ["ffmpeg", "ffprobe"]
    OPTIONAL = ["mkvmerge", "mkvinfo", "mkvpropedit"]

    def __init__(self):
        self.paths: Dict[str, Optional[str]] = {}
        self._discover()

    def _discover(self):
        for tool in self.REQUIRED + self.OPTIONAL:
            self.paths[tool] = find_tool(tool)

    @property
    def ffmpeg(self) -> Optional[str]:  return self.paths["ffmpeg"]
    @property
    def ffprobe(self) -> Optional[str]: return self.paths["ffprobe"]
    @property
    def mkvmerge(self) -> Optional[str]: return self.paths["mkvmerge"]
    @property
    def has_mkvtools(self) -> bool: return bool(self.paths.get("mkvmerge"))

    def check_required(self) -> bool:
        return all(self.paths[t] for t in self.REQUIRED)

    def report(self, use_colour: bool = True) -> List[str]:
        lines = []
        lines.append(f"    {c('macOS', CYAN, use_colour)}  {platform.mac_ver()[0]}  "
                     f"({platform.machine()})")
        for tool in self.REQUIRED:
            p    = self.paths[tool]
            tag  = c("✓", GREEN, use_colour) if p else c("✗ MISSING", RED, use_colour)
            hint = "" if p else c('  →  brew install ffmpeg', YELLOW, use_colour)
            lines.append(f"    {tag}  {tool:<16} {p or ''}{hint}")
        for tool in self.OPTIONAL:
            p    = self.paths[tool]
            tag  = c("✓", GREEN, use_colour) if p else c("○ optional", DIM, use_colour)
            hint = "" if p else c("  →  brew install mkvtoolnix", DIM, use_colour)
            lines.append(f"    {tag}  {tool:<16} {p or ''}{hint}")
        return lines

TOOLS = ToolSet()

# ── Hardware encoder detection ─────────────────────────────────────────────────
@dataclass
class HWAccel:
    videotoolbox: bool = False

    @property
    def best(self) -> Optional[str]:
        if self.videotoolbox: return "h264_videotoolbox"
        return None

    @property
    def label(self) -> str:
        if self.videotoolbox: return "Apple VideoToolbox"
        return "libx264 (software)"

def detect_hw_encoders() -> HWAccel:
    hw = HWAccel()
    if not TOOLS.ffmpeg:
        return hw
    rc, out, err = run([TOOLS.ffmpeg, "-hide_banner", "-encoders"], timeout=10)
    hw.videotoolbox = "h264_videotoolbox" in out
    return hw

HW: Optional[HWAccel] = None

# ── Subprocess helpers ─────────────────────────────────────────────────────────
def _subprocess_kwargs() -> dict:
    return {}

def run(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, **_subprocess_kwargs())
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", str(e)
    except Exception as e:
        return -1, "", str(e)

def run_watched(cmd: List[str], label: str, timeout: int,
                use_colour: bool = True) -> Tuple[int, str, str]:
    import threading
    SPINNER  = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    spin_idx = 0
    last_line: List[str] = [""]
    stdout_buf: List[str] = []
    stderr_buf: List[str] = []

    def _drain(stream, buf, is_stderr):
        for raw in stream:
            line = raw.rstrip()
            buf.append(line)
            if is_stderr and line and not line.startswith("  "):
                last_line[0] = line[:80]
        stream.close()

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, **_subprocess_kwargs())
    except FileNotFoundError as e:
        return -1, "", str(e)

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_buf, False), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf, True),  daemon=True)
    t_out.start(); t_err.start()

    t_start   = time.perf_counter()
    timed_out = False
    col_spin  = "\033[36m" if use_colour else ""
    col_dim   = "\033[2m"  if use_colour else ""
    rst       = "\033[0m"  if use_colour else ""

    while proc.poll() is None:
        elapsed = time.perf_counter() - t_start
        if elapsed > timeout:
            proc.kill(); timed_out = True; break
        spin = SPINNER[spin_idx % len(SPINNER)] if use_colour else "-"
        spin_idx += 1
        last = last_line[0]
        if len(last) > 52: last = last[:49] + "..."
        line = (f"  {col_spin}{spin}{rst}  {col_dim}{label:<22}{rst}  "
                f"{col_dim}{elapsed:>5.1f}s{rst}  {col_dim}{last}{rst}")
        print(f"\r{line:<78}", end="", flush=True)
        time.sleep(0.12)

    t_out.join(timeout=2); t_err.join(timeout=2)
    print(f"\r{' ' * 80}\r", end="", flush=True)
    if timed_out:
        return -1, "", f"Timed out after {timeout}s"
    return proc.returncode, "\n".join(stdout_buf), "\n".join(stderr_buf)

def size_scaled_timeout(path: Path, base: int = 60, bps: int = 20 * 1024 * 1024) -> int:
    try:
        size = path.stat().st_size
    except OSError:
        return base
    return min(max(base, size // bps), 3600)

def ffprobe_json(path: Path, use_colour: bool = False) -> Optional[dict]:
    if not TOOLS.ffprobe:
        return None
    cmd = [TOOLS.ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", "-show_error", str(path)]
    timeout = size_scaled_timeout(path, base=30)
    rc, out, err = run_watched(cmd, label="ffprobe", timeout=timeout, use_colour=use_colour)
    try:
        return json.loads(out)
    except Exception:
        return None

# ── Container signature checks ─────────────────────────────────────────────────
MP4_SIGNATURES = [b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"]

def check_container_signature(path: Path) -> List[Issue]:
    issues = []
    ext = path.suffix.lower()
    try:
        raw = path.read_bytes()[:32]
    except OSError as e:
        return [Issue("error", "read_error", f"Cannot read file: {e}")]
    if ext in MP4_EXTS:
        if len(raw) >= 8 and raw[4:8] not in MP4_SIGNATURES:
            issues.append(Issue("warning", "mp4_unknown_box",
                f"First atom '{raw[4:8]}' is not a recognised MP4/MOV atom"))
    elif ext in MKV_EXTS:
        if raw[:4] != b"\x1a\x45\xdf\xa3":
            issues.append(Issue("error", "mkv_bad_ebml",
                f"Missing EBML header – not a valid Matroska file (got {raw[:4].hex()})"))
    elif ext in AVI_EXTS:
        if raw[:4] != b"RIFF" or raw[8:12] != b"AVI ":
            issues.append(Issue("error", "avi_bad_signature", "Missing RIFF/AVI signature"))
    elif ext in {".wmv", ".asf"}:
        if raw[:4] != b"\x30\x26\xb2\x75":
            issues.append(Issue("warning", "wmv_bad_signature",
                "Missing ASF/WMV GUID header"))
    return issues

def check_mp4_moov_atom(path: Path) -> List[Issue]:
    issues = []
    size = path.stat().st_size
    if size < 8:
        return [Issue("error", "too_small", f"File is only {size} bytes")]
    with open(path, "rb") as f:
        head = f.read(min(65536, size))
        tail = b""
        if size > 65536:
            f.seek(max(0, size - 65536))
            tail = f.read()
    has_moov     = b"moov" in head or b"moov" in tail
    moov_in_head = b"moov" in head
    moov_in_tail = b"moov" in tail
    has_mdat     = b"mdat" in head or b"mdat" in tail
    if not has_moov:
        issues.append(Issue("error", "mp4_no_moov",
            "moov atom not found – recording likely interrupted before finalisation. "
            "Repair will attempt remux with -movflags faststart"))
    elif moov_in_tail and not moov_in_head:
        issues.append(Issue("warning", "mp4_moov_at_end",
            "moov atom is at end of file (not streamable). "
            "Repair will relocate it to the front"))
    if has_mdat and not has_moov:
        issues.append(Issue("warning", "mp4_mdat_only",
            "mdat block found but no moov index – partial recovery may be possible"))
    return issues

def check_via_ffprobe(path: Path, use_colour: bool = False) -> List[Issue]:
    issues = []
    data = ffprobe_json(path, use_colour=use_colour)
    if data is None:
        issues.append(Issue("error", "ffprobe_failed",
            "ffprobe could not read file – likely corrupt or unrecognised format"))
        return issues
    if "error" in data:
        msg = data["error"].get("string", "unknown error")
        issues.append(Issue("error", "ffprobe_error", f"ffprobe: {msg}"))
        return issues
    fmt     = data.get("format", {})
    streams = data.get("streams", [])
    if not streams:
        issues.append(Issue("error", "no_streams", "File contains no audio or video streams"))
        return issues
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        issues.append(Issue("warning", "no_video_stream",
            "No video stream found – audio-only or stripped container"))
    duration = float(fmt.get("duration", 0) or 0)
    if duration == 0:
        issues.append(Issue("error", "zero_duration",
            "Duration is 0 – container metadata may be missing or corrupt"))
    elif duration < 0:
        issues.append(Issue("error", "negative_duration",
            f"Negative duration ({duration:.2f}s) – corrupt metadata"))
    bit_rate = int(fmt.get("bit_rate", 0) or 0)
    if bit_rate == 0 and duration > 0:
        issues.append(Issue("warning", "zero_bitrate",
            "Bitrate reported as 0 – index may be missing"))
    for vs in video_streams:
        codec  = vs.get("codec_name", "unknown")
        width  = vs.get("width", 0)
        height = vs.get("height", 0)
        if codec in ("none", "unknown"):
            issues.append(Issue("error", "unknown_video_codec",
                f"Video codec not identified: '{codec}'"))
        if width == 0 or height == 0:
            issues.append(Issue("error", "zero_dimensions",
                f"Video dimensions {width}x{height} – stream header corrupt"))
        r_frame_rate = vs.get("r_frame_rate", "0/1")
        try:
            num, den = map(int, r_frame_rate.split("/"))
            fps = num / den if den else 0
            if fps > 240:
                issues.append(Issue("warning", "unusual_fps",
                    f"Unusually high frame rate: {fps:.1f} fps"))
        except Exception:
            pass
    if path.suffix.lower() in MP4_EXTS:
        issues.extend(check_mp4_moov_atom(path))
    return issues

def check_via_mkvmerge(path: Path, use_colour: bool = False) -> List[Issue]:
    issues = []
    if not TOOLS.has_mkvtools:
        return issues
    cmd = [TOOLS.mkvmerge, "--identify", "--identification-format", "json", str(path)]
    timeout = size_scaled_timeout(path, base=30)
    rc, out, err = run_watched(cmd, label="mkvmerge identify", timeout=timeout, use_colour=use_colour)
    try:
        data = json.loads(out)
    except Exception:
        if rc != 0:
            issues.append(Issue("warning", "mkvmerge_identify_failed",
                f"mkvmerge could not identify: {err.strip()[:120]}"))
        return issues
    if rc == 2:
        issues.append(Issue("error", "mkvmerge_error",
            f"mkvmerge structural error: {err.strip()[:200]}"))
    elif rc == 1:
        issues.append(Issue("warning", "mkvmerge_warning",
            f"mkvmerge warnings: {err.strip()[:200]}"))
    if not data.get("tracks"):
        issues.append(Issue("error", "mkv_no_tracks", "No tracks found in MKV"))
    return issues

def check_ffmpeg_decode(path: Path, use_colour: bool = False) -> List[Issue]:
    issues = []
    if not TOOLS.ffmpeg:
        return issues
    cmd = [TOOLS.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"]
    timeout = size_scaled_timeout(path, base=60)
    rc, out, err = run_watched(cmd, label="ffmpeg decode-probe", timeout=timeout, use_colour=use_colour)
    if rc == -1 and "Timed out" in err:
        issues.append(Issue("warning", "decode_probe_timeout",
            f"Decode probe timed out after {timeout}s"))
        return issues
    if err.strip():
        skip = ["deprecated", "last message", "non monotonous",
                "display rect", "reserved bits", "buffer underflow"]
        lines = [ln.strip() for ln in err.strip().splitlines()
                 if ln.strip() and not any(s in ln.lower() for s in skip)]
        if lines:
            sample = lines[0][:180]
            total  = len(lines)
            issues.append(Issue(
                "warning" if rc == 0 else "error",
                "decode_errors",
                f"{total} decode error(s). First: {sample}"
                + (f" (+{total-1} more)" if total > 1 else "")
            ))
    return issues

# ── Repair strategies ──────────────────────────────────────────────────────────
def repair_remux(src: Path, dst: Path, use_colour: bool = True) -> Tuple[bool, str]:
    cmd = [TOOLS.ffmpeg, "-y", "-err_detect", "ignore_err",
           "-i", str(src), "-c", "copy", "-map", "0", str(dst)]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="remux (stream copy)", timeout=timeout, use_colour=use_colour)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "Stream-copy remux succeeded (lossless)"
    return False, f"Remux failed (rc={rc}): {err.strip()[-200:]}"

def repair_mp4_faststart(src: Path, dst: Path, use_colour: bool = True) -> Tuple[bool, str]:
    cmd = [TOOLS.ffmpeg, "-y", "-err_detect", "ignore_err",
           "-i", str(src), "-c", "copy", "-map", "0", "-movflags", "+faststart", str(dst)]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="MP4 moov faststart", timeout=timeout, use_colour=use_colour)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "MP4 moov faststart relocation succeeded"
    return False, f"Faststart failed (rc={rc}): {err.strip()[-200:]}"

def repair_mkv_mkvmerge(src: Path, dst: Path, use_colour: bool = True) -> Tuple[bool, str]:
    if not TOOLS.has_mkvtools:
        return False, "mkvmerge not available"
    cmd = [TOOLS.mkvmerge, "--no-global-tags", "-o", str(dst), str(src)]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="mkvmerge remux", timeout=timeout, use_colour=use_colour)
    if rc in (0, 1) and dst.exists() and dst.stat().st_size > 100:
        return True, "MKV remux via mkvmerge succeeded" + (" (with warnings)" if rc == 1 else "")
    return False, f"mkvmerge failed (rc={rc}): {err.strip()[-200:]}"

def repair_reencode(src: Path, dst: Path, use_hwaccel: bool = True,
                    use_colour: bool = True) -> Tuple[bool, str]:
    global HW
    if HW is None:
        HW = detect_hw_encoders()
    encoder = HW.best if (use_hwaccel and HW) else None
    if encoder == "h264_videotoolbox":
        codec_args = ["-c:v", "h264_videotoolbox", "-q:v", "60"]
        method = "Apple VideoToolbox HW H.264"
    else:
        codec_args = ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
        method = "libx264 SW H.264"
    timeout = size_scaled_timeout(src)
    cmd = [TOOLS.ffmpeg, "-y", "-err_detect", "ignore_err", "-i", str(src),
           *codec_args, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
    rc, _, err = run_watched(cmd, label=f"re-encode ({method[:14]})", timeout=timeout, use_colour=use_colour)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, f"Re-encode succeeded using {method}"
    if encoder:
        cmd_sw = [TOOLS.ffmpeg, "-y", "-err_detect", "ignore_err", "-i", str(src),
                  "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                  "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
        rc2, _, err2 = run_watched(cmd_sw, label="re-encode (libx264 SW)", timeout=timeout, use_colour=use_colour)
        if rc2 == 0 and dst.exists() and dst.stat().st_size > 100:
            return True, f"Re-encode succeeded using libx264 SW (GPU fallback after {method} failed)"
    return False, f"Re-encode failed (rc={rc}): {err.strip()[-200:]}"

def attempt_video_repair(
        src: Path, dst_dir: Path, rel: str,
        issues: List[Issue],
        allow_reencode: bool, use_hwaccel: bool,
        use_colour: bool = True) -> Tuple[Optional[Path], List[Issue], float, float]:
    """
    Returns (output_path_or_None, updated_issues, copy_elapsed, repair_elapsed).
    """
    ext        = src.suffix.lower()
    has_errors = any(i.severity == "error" for i in issues)

    if not has_errors:
        dst = dst_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        shutil.copy2(src, dst)
        return dst, issues, time.perf_counter() - t0, 0.0

    strategies: List[Tuple[str, object]] = []
    moov_issue = any(i.code in ("mp4_no_moov", "mp4_moov_at_end") for i in issues)
    if ext in MP4_EXTS and moov_issue:
        strategies.append(("faststart", lambda s, d: repair_mp4_faststart(s, d, use_colour)))
    strategies.append(("remux", lambda s, d: repair_remux(s, d, use_colour)))
    if ext in MKV_EXTS and TOOLS.has_mkvtools:
        strategies.append(("mkvmerge", lambda s, d: repair_mkv_mkvmerge(s, d, use_colour)))
    if allow_reencode:
        strategies.append(("reencode", lambda s, d: repair_reencode(s, d, use_hwaccel, use_colour)))

    dst_dir.mkdir(parents=True, exist_ok=True)
    repair_t0 = time.perf_counter()
    for strategy_name, strategy_fn in strategies:
        suffix = ".mp4" if (strategy_name == "reencode" and ext not in MP4_EXTS) else ext
        with tempfile.NamedTemporaryFile(dir=dst_dir, suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        success, note = strategy_fn(src, tmp_path)
        if success:
            final = dst_dir / rel
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_path), str(final))
            for issue in issues:
                if issue.severity == "error" and not issue.repaired:
                    issue.repaired    = True
                    issue.repair_note = note
            return final, issues, 0.0, time.perf_counter() - repair_t0
        else:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            issues.append(Issue("warning", f"{strategy_name}_failed",
                f"Strategy '{strategy_name}' did not succeed: {note[:120]}"))

    return None, issues, 0.0, time.perf_counter() - repair_t0

# ── Helpers ────────────────────────────────────────────────────────────────────
def sha256_of(path: Path, chunk: int = 1 << 20) -> Tuple[str, float]:
    """Returns (hex_digest, elapsed_seconds)."""
    h  = hashlib.sha256()
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest(), time.perf_counter() - t0

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def bar(done: int, total: int, width: int = 28) -> str:
    if total == 0:
        return "[" + "-" * width + "]"
    filled   = int(width * done / total)
    fill_ch  = "█" if _COLOUR_ENABLED else "#"
    rest_ch  = "─" if _COLOUR_ENABLED else "-"
    return "[" + fill_ch * filled + rest_ch * (width - filled) + "]"

# ── Input resolution ───────────────────────────────────────────────────────────
def resolve_inputs(sources: List[str], recursive: bool,
                   ext_filter: Optional[Set[str]]) -> Tuple[List[Path], List[str]]:
    seen:   Dict[str, Path] = {}
    errors: List[str]       = []

    def _add(p: Path):
        p   = p.resolve()
        key = str(p).lower()
        if ext_filter and p.suffix.lower() not in ext_filter:
            return
        if key not in seen:
            seen[key] = p

    def _expand_dir(d: Path):
        pattern = "**/*" if recursive else "*"
        for p in sorted(d.glob(pattern)):
            if p.is_file() and p.suffix.lower() in ALL_VIDEO_EXTS:
                _add(p)

    for src in sources:
        src_norm = src.replace("\\", "/")
        has_glob = any(ch in src_norm for ch in ("*", "?", "["))
        if has_glob:
            parts   = Path(src)
            parent  = parts.parent
            pattern = parts.name
            if any(ch in str(parent) for ch in ("*", "?", "[")):
                parent  = Path(".")
                pattern = src
            matches = sorted(Path(parent).glob(pattern))
            if not matches:
                errors.append(f"No files matched glob: {src}")
            for m in matches:
                if m.is_file(): _add(m)
                elif m.is_dir(): _expand_dir(m)
        else:
            p = Path(src).expanduser().resolve()
            if p.is_dir():    _expand_dir(p)
            elif p.is_file(): _add(p)
            else:             errors.append(f"Not found: {src}")

    return list(seen.values()), errors

def load_list_file(path: Path) -> List[str]:
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8-sig").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

# ── Report writer ──────────────────────────────────────────────────────────────
def write_report(run_dir: Path, run_ts: str, results: List[FileResult],
                 sources_label: str, dst: Path,
                 shutdown_reason: str = "", wall_elapsed: float = 0.0):
    """Write both .txt (human) and .json (machine) reports into run_dir."""
    ok      = [r for r in results if r.status == "ok"]
    rep     = [r for r in results if r.status == "repaired"]
    partial = [r for r in results if r.status == "partial"]
    failed  = [r for r in results if r.status in ("failed", "skipped")]
    done    = [r for r in results if r.status == "done"]

    total_bytes = sum(r.size_bytes for r in results
                      if r.status not in ("done",))
    total_mbps  = (total_bytes / 1024 / 1024 / wall_elapsed
                   if wall_elapsed > 0 else 0)

    lines = [
        "=" * 72,
        f"  VIDEO COPY + REPAIR  —  Run Report",
        f"  Generated : {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}",
        f"  Run ID    : {run_ts}",
        "=" * 72,
        f"  Source    : {sources_label}",
        f"  Dest      : {dst}",
        f"  RunDir    : {run_dir}",
    ]
    if shutdown_reason:
        lines.append(f"  Exit      : GRACEFUL SHUTDOWN ({shutdown_reason})")
    else:
        lines.append(f"  Exit      : clean")
    lines += [
        "",
        "-" * 72,
        "  SUMMARY",
        "-" * 72,
        f"  {'Total files':<30} {len(results)}",
        f"  {'Already done (journal/resume)':<30} {len(done)}",
        f"  {'Copied OK':<30} {len(ok)}",
        f"  {'Repaired + copied':<30} {len(rep)}",
        f"  {'Partial recovery':<30} {len(partial)}",
        f"  {'Failed / skipped':<30} {len(failed)}",
        f"  {'Wall time':<30} {wall_elapsed:.1f}s",
        f"  {'Avg throughput (new files)':<30} {total_mbps:.1f} MB/s",
        "",
    ]

    if rep:
        lines += ["-" * 72, "  REPAIRED FILES", "-" * 72]
        for r in rep:
            lines.append(f"  {r.rel}  [{r.repair_strategy}]  {r.elapsed:.1f}s  "
                         f"{r.throughput_mbps:.1f} MB/s")
        lines.append("")

    if partial:
        lines += ["-" * 72, "  PARTIAL RECOVERY (manual inspection needed)", "-" * 72]
        for r in partial:
            lines.append(f"  {r.rel}  {r.elapsed:.1f}s")
            for iss in r.issues:
                if iss.severity == "error" and not iss.repaired:
                    lines.append(f"    ✗ {iss.code}: {iss.description}")
        lines.append("")

    if failed:
        lines += ["-" * 72, "  FAILED FILES", "-" * 72]
        for r in failed:
            lines.append(f"  {r.rel}  ({r.status})")
            for iss in r.issues:
                lines.append(f"    ✗ {iss.code}: {iss.description}")
        lines.append("")

    lines.append("=" * 72)

    txt_path = run_dir / f"video_repair_report__{run_ts}.txt"
    try:
        txt_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass

    # ── JSON report ────────────────────────────────────────────────────────
    json_data = {
        "run_ts":          run_ts,
        "generated":       datetime.now().isoformat(),
        "source":          sources_label,
        "destination":     str(dst),
        "run_dir":         str(run_dir),
        "shutdown_reason": shutdown_reason,
        "wall_elapsed":    round(wall_elapsed, 2),
        "avg_throughput_mbps": round(total_mbps, 2),
        "counts": {
            "total": len(results), "done": len(done), "ok": len(ok),
            "repaired": len(rep), "partial": len(partial), "failed": len(failed),
        },
        "files": [
            {
                "rel":             r.rel,
                "status":          r.status,
                "size":            r.size_bytes,
                "sha256":          r.sha256,
                "elapsed":         round(r.elapsed, 2),
                "inspect_elapsed": round(r.inspect_elapsed, 2),
                "repair_elapsed":  round(r.repair_elapsed, 2),
                "copy_elapsed":    round(r.copy_elapsed, 2),
                "sha_elapsed":     round(r.sha_elapsed, 2),
                "throughput_mbps": round(r.throughput_mbps, 2),
                "strategy":        r.repair_strategy,
                "issues":          [
                    {"severity": i.severity, "code": i.code,
                     "repaired": i.repaired, "note": i.repair_note}
                    for i in r.issues
                ],
            }
            for r in results
        ],
    }
    json_path = run_dir / f"video_repair_report__{run_ts}.json"
    try:
        json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    except OSError:
        pass

    return txt_path, json_path

# ── Engine ─────────────────────────────────────────────────────────────────────
class VideoRepairEngine:
    def __init__(self, files: List[Path], dst: Path,
                 dry_run: bool, verbose: bool,
                 ext_filter: Optional[Set[str]],
                 allow_reencode: bool, use_hwaccel: bool,
                 use_colour: bool, resume: bool = False,
                 sources_label: str = ""):
        self.files          = files
        self.dst            = dst
        self.dry_run        = dry_run
        self.verbose        = verbose
        self.ext_filter     = ext_filter
        self.allow_reencode = allow_reencode
        self.use_hwaccel    = use_hwaccel
        self.use_colour     = use_colour and _COLOUR_ENABLED
        self.resume         = resume
        self.sources_label  = sources_label
        self.results: List[FileResult] = []
        self._run_dir: Optional[Path]  = None   # <dst>/_run/
        self._log_path: Optional[Path] = None
        self._common_root: Optional[Path] = None
        self._run_ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._journal: Optional[RunJournal] = None
        self._wall_t0: float = 0.0
        self._setup_logging()

    # ── logging ───────────────────────────────────────────────────────────
    def _setup_logging(self):
        self._logger = logging.getLogger("video_repair_win")
        self._logger.setLevel(logging.DEBUG)
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.WARNING)
        self._logger.addHandler(sh)

    def _attach_file_log(self):
        if self.dry_run or self._run_dir is None:
            return
        self._log_path = self._run_dir / "video_repair.log"
        fh = logging.FileHandler(self._log_path, encoding="utf-8", mode="a")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        self._logger.addHandler(fh)

    def _init_run_dir(self):
        """Create <dst>/_run/ and return it."""
        run_dir = self.dst / "_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir = run_dir
        # Write a README so the folder's purpose is obvious
        readme = run_dir / "README.txt"
        if not readme.exists():
            readme.write_text(
                "This folder is created and managed by video_copy_repair.\n"
                "Contents:\n"
                "  video_repair.log          — full run log (appended across restarts)\n"
                "  video_repair_journal_*.jsonl — resume index (certified files)\n"
                "  video_repair_report_*.txt  — human-readable run report\n"
                "  video_repair_report_*.json — machine-readable run report\n\n"
                "To request a GRACEFUL SHUTDOWN:\n"
                "  Create a file named SHUTDOWN in this directory.\n"
                "  The script will finish its current file then exit cleanly.\n"
                "  macOS:    touch _run/SHUTDOWN\n"
                "  macOS/Linux: touch _run/SHUTDOWN\n",
                encoding="utf-8"
            )

    def _init_journal(self) -> int:
        if self.dry_run or self._run_dir is None:
            return 0
        self._journal = RunJournal(
            run_dir   = self._run_dir,
            src_label = self.sources_label.splitlines()[0],
            run_ts    = self._run_ts,
        )
        prev   = self._journal.find_previous()
        loaded = 0
        if prev:
            loaded = self._journal.load(prev)
            self._logger.info(
                f"JOURNAL    Loaded {loaded} certified entries from {prev.name}")
        return loaded

    # ── shutdown signal file ───────────────────────────────────────────────
    def _check_shutdown_file(self) -> bool:
        """Return True if a SHUTDOWN sentinel file exists in _run/."""
        if self._run_dir is None:
            return False
        sentinel = self._run_dir / "SHUTDOWN"
        if sentinel.exists():
            global _shutdown_requested, _shutdown_reason
            _shutdown_requested = True
            _shutdown_reason    = "SHUTDOWN file detected in _run/"
            try:
                sentinel.unlink()   # remove it so a re-run starts fresh
            except OSError:
                pass
            return True
        return False

    # ── resume / destination health check ─────────────────────────────────
    _DST_MISSING   = "missing"
    _DST_TRUNCATED = "truncated"
    _DST_CORRUPT   = "corrupt"
    _DST_WARN      = "warn"
    _DST_CLEAN     = "clean"

    def _check_destination(self, src: Path, dst_path: Path) -> Tuple[str, str]:
        if not dst_path.exists():
            return self._DST_MISSING, ""
        try:
            src_size = src.stat().st_size
            dst_size = dst_path.stat().st_size
        except OSError as e:
            return self._DST_MISSING, f"stat error: {e}"
        if dst_size == 0:
            return self._DST_TRUNCATED, "destination is zero bytes"
        ratio = dst_size / src_size if src_size else 0
        if ratio < 0.95:
            return self._DST_TRUNCATED, (
                f"destination is only {ratio*100:.0f}% of source size "
                f"({human_size(dst_size)} vs {human_size(src_size)}) – "
                f"likely an interrupted write")
        issues   = self._inspect(dst_path)
        errors   = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        if errors:
            codes = ", ".join(i.code for i in errors[:3])
            return self._DST_CORRUPT, (
                f"destination fails health check ({len(errors)} error(s): {codes}) "
                f"– will re-copy from source")
        size_note = (f"identical size ({human_size(dst_size)})"
                     if dst_size == src_size
                     else f"size ok ({human_size(dst_size)} / {human_size(src_size)})")
        if warnings:
            codes = ", ".join(i.code for i in warnings[:2])
            return self._DST_WARN, f"{size_note} – {len(warnings)} warning(s): {codes}"
        return self._DST_CLEAN, f"healthy – {size_note}"

    # ── printing ───────────────────────────────────────────────────────────
    def _p(self, msg: str, end: str = "\n"):
        print(msg, end=end, flush=True)

    def _c(self, text: str, colour: str) -> str:
        return c(text, colour, self.use_colour)

    def _header(self):
        global HW
        w = 72
        self._p(self._c("─" * w, DIM))
        self._p(self._c("  VIDEO COPY + REPAIR UTILITY  ·  macOS", BOLD))
        self._p(self._c(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}", DIM))
        self._p(self._c("─" * w, DIM))
        label_lines = self.sources_label.splitlines() if self.sources_label else ["(none)"]
        self._p(f"  Source  : {self._c(label_lines[0], CYAN)}")
        for ln in label_lines[1:]:
            self._p(f"            {self._c(ln, CYAN)}")
        self._p(f"  Dest    : {self._c(str(self.dst), CYAN)}")
        self._p(f"  RunDir  : {self._c(str(self._run_dir), CYAN)}")
        if self.dry_run:
            self._p(f"  Mode    : {self._c('DRY RUN – no files will be written', YELLOW)}")
        if self.resume:
            self._p(f"  Mode    : {self._c('RESUME', CYAN)}")
            if self._journal and self._journal.entry_count:
                self._p(f"  Journal : {self._c(str(self._journal.entry_count) + ' certified entries loaded', GREEN)}")
        if not self.allow_reencode:
            self._p(f"  Mode    : {self._c('Re-encode disabled (--no-reencode)', YELLOW)}")
        if self.use_hwaccel and HW:
            if HW.best:
                self._p(f"  HW Accel: {self._c(f'{HW.label} available', GREEN)}")
            else:
                self._p(f"  HW Accel: {self._c('No GPU encoder found – using libx264 SW', YELLOW)}")
        self._p(f"  Shutdown: {self._c('create _run/SHUTDOWN file for graceful stop', DIM)}")
        self._p(self._c("─" * w, DIM))
        self._p(self._c("  Tool availability:", DIM))
        for line in TOOLS.report(self.use_colour):
            self._p(line)
        self._p(self._c("─" * w, DIM))

    def _footer(self, shutdown_reason: str = ""):
        ok      = sum(1 for r in self.results if r.status == "ok")
        rep     = sum(1 for r in self.results if r.status == "repaired")
        partial = sum(1 for r in self.results if r.status == "partial")
        failed  = sum(1 for r in self.results if r.status in ("failed", "skipped"))
        done    = sum(1 for r in self.results if r.status == "done")
        wall    = time.perf_counter() - self._wall_t0
        w = 72
        self._p(self._c("─" * w, DIM))
        self._p(self._c("  SUMMARY", BOLD))
        self._p(self._c("─" * w, DIM))
        if shutdown_reason:
            self._p(f"  {self._c('GRACEFUL SHUTDOWN', YELLOW)}  {shutdown_reason}")
        self._p(f"  {'Total files':<28} {len(self.results)}")
        if done:
            self._p(f"  {'Already copied (skipped)':<28} {self._c(str(done), DIM)}")
        self._p(f"  {'Copied OK (no issues)':<28} {self._c(str(ok),  GREEN)}")
        self._p(f"  {'Repaired + copied':<28} {self._c(str(rep),     BLUE)}")
        self._p(f"  {'Partial recovery':<28} {self._c(str(partial),  YELLOW)}")
        self._p(f"  {'Failed / skipped':<28} {self._c(str(failed),   RED)}")
        self._p(f"  {'Wall time':<28} {wall:.1f}s")
        if self._log_path:
            self._p(f"  {'Log':<28} {self._log_path}")
        if self._journal and not self.dry_run:
            self._p(f"  {'Journal':<28} {self._journal.path}")
        self._p(self._c("─" * w, DIM))
        if failed or partial:
            self._p(self._c("\n  ⚠  Files marked FAILED or PARTIAL need manual inspection.", YELLOW))
        elif not shutdown_reason:
            self._p(self._c("\n  ✓  All files processed successfully.", GREEN))
        self._p("")

    def _file_line(self, idx: int, total: int, result: FileResult):
        prog = bar(idx, total)
        pct  = f"{idx}/{total}"
        rel  = result.rel
        if len(rel) > 38:
            rel = "..." + rel[-35:]
        status_map = {
            "ok":       self._c("  OK      ", GREEN),
            "repaired": self._c("  REPAIRED", BLUE),
            "partial":  self._c("  PARTIAL ", YELLOW),
            "failed":   self._c("  FAILED  ", RED),
            "skipped":  self._c("  SKIPPED ", RED),
            "done":     self._c("  done    ", DIM),
        }
        st    = status_map.get(result.status, f"  {result.status:<8}")
        size  = human_size(result.size_bytes)
        tp    = (self._c(f" {result.throughput_mbps:.0f}MB/s", DIM)
                 if result.throughput_mbps > 0 else "")
        strat = (self._c(f" [{result.repair_strategy}]", MAGENTA)
                 if result.repair_strategy else "")
        self._p(f"  {self._c(prog, DIM)} {self._c(pct, DIM):>8}  "
                f"{st}  {rel:<38}  {self._c(size, DIM)}{tp}{strat}")

    def _issue_lines(self, result: FileResult):
        for iss in result.issues:
            icon   = "!" if iss.severity == "warning" else "X"
            colour = YELLOW if iss.severity == "warning" else RED
            self._p(f"               {self._c(icon, colour)} "
                    f"{iss.code}: {iss.description}")
            if iss.repair_note:
                tag = "repaired" if iss.repaired else "->"
                col = GREEN if iss.repaired else YELLOW
                self._p(f"                 {self._c(tag, col)} {iss.repair_note}")

    # ── relative path ──────────────────────────────────────────────────────
    def _rel(self, path: Path) -> str:
        if self._common_root is None:
            parents = [p.parent for p in self.files]
            try:
                self._common_root = (
                    Path(os.path.commonpath([str(p) for p in parents]))
                    if parents else Path(".")
                )
            except ValueError:
                self._common_root = Path(".")
        try:
            return str(path.relative_to(self._common_root))
        except ValueError:
            return path.name

    # ── inspect ────────────────────────────────────────────────────────────
    def _inspect(self, path: Path) -> List[Issue]:
        issues = []
        try:
            size = path.stat().st_size
        except OSError as e:
            return [Issue("error", "stat_error", f"Cannot stat file: {e}")]
        if size == 0:
            return [Issue("error", "zero_byte", "File is zero bytes")]
        issues += check_container_signature(path)
        issues += check_via_ffprobe(path, use_colour=self.use_colour)
        if path.suffix.lower() in MKV_EXTS:
            issues += check_via_mkvmerge(path, use_colour=self.use_colour)
        if any(i.severity == "error" for i in issues):
            sig_err = any(i.code in ("mkv_bad_ebml", "avi_bad_signature",
                                     "wmv_bad_signature") for i in issues)
            if not sig_err:
                issues += check_ffmpeg_decode(path, use_colour=self.use_colour)
        return issues

    # ── run ────────────────────────────────────────────────────────────────
    def run(self):
        global HW, _shutdown_requested, _shutdown_reason
        self._wall_t0 = time.perf_counter()

        if self.use_hwaccel and TOOLS.ffmpeg:
            HW = detect_hw_encoders()

        if not self.dry_run:
            self.dst.mkdir(parents=True, exist_ok=True)
            self._init_run_dir()
            self._attach_file_log()
            loaded = self._init_journal()
        else:
            loaded = 0

        self._header()

        if not TOOLS.check_required():
            self._p(self._c("\n  Error: ffmpeg and ffprobe are required.", RED))
            self._p(self._c('  Install: brew install ffmpeg', CYAN))
            sys.exit(1)

        total = len(self.files)
        if total == 0:
            self._p(self._c("  No video files found matching criteria.", YELLOW))
            return

        self._p(f"  Found {self._c(str(total), BOLD)} video file(s) to process.\n")

        seen_rels: Dict[str, int] = {}

        for idx, path in enumerate(self.files, 1):

            # ── Check for graceful shutdown (signal or sentinel file) ──────
            if _shutdown_requested or self._check_shutdown_file():
                self._p(self._c(
                    f"\n  ⚡ Graceful shutdown after {idx-1}/{total} files "
                    f"({_shutdown_reason})", YELLOW))
                break

            t0  = time.perf_counter()
            rel = self._rel(path)

            if rel in seen_rels:
                seen_rels[rel] += 1
                stem = Path(rel).stem
                suf  = Path(rel).suffix
                rel  = f"{stem}_{seen_rels[rel]}{suf}"
            else:
                seen_rels[rel] = 0

            try:
                size = path.stat().st_size
            except OSError:
                size = 0

            result = FileResult(path=path, rel=rel, size_bytes=size)

            # ── Resume logic ───────────────────────────────────────────────
            if self.resume and not self.dry_run:
                dst_path = self.dst / rel

                # Step 1: journal fast-path
                journaled, entry = (self._journal.is_done(rel)
                                    if self._journal else (False, {}))
                if journaled:
                    result.status      = "done"
                    result.sha256      = entry.get("sha256", "")
                    result.skip_reason = (f"journal certified "
                                         f"({entry.get('status','?')} on {entry.get('run','?')})")
                    result.elapsed     = time.perf_counter() - t0
                    self.results.append(result)
                    self._file_line(idx, total, result)
                    if self.verbose:
                        self._p(f"               {self._c('-', DIM)} {result.skip_reason}")
                    self._logger.info(f"JOURNAL    {path}  (certified – skipping health-check)")
                    continue

                # Step 2: health-check un-journaled destination files
                verdict, detail = self._check_destination(path, dst_path)
                if verdict in (self._DST_CLEAN, self._DST_WARN):
                    result.status      = "done"
                    result.skip_reason = detail
                    result.elapsed     = time.perf_counter() - t0
                    self.results.append(result)
                    self._file_line(idx, total, result)
                    if verdict == self._DST_WARN or self.verbose:
                        col = YELLOW if verdict == self._DST_WARN else DIM
                        self._p(f"               {self._c('!', col)} {detail}")
                    self._logger.info(f"DONE       {path}  ({detail})")
                    if self._journal:
                        self._journal.record(result)
                    continue
                elif verdict == self._DST_CORRUPT:
                    self._p(f"               {self._c('X', RED)} {detail}")
                    self._logger.warning(f"RECOPY     {path}  ({detail})")
                    try:
                        dst_path.unlink()
                    except OSError:
                        pass
                elif verdict == self._DST_TRUNCATED:
                    self._p(f"               {self._c('!', YELLOW)} {detail}")
                    self._logger.warning(f"TRUNCATED  {path}  ({detail})")
                    try:
                        dst_path.unlink()
                    except OSError:
                        pass

            # ── Inspect ────────────────────────────────────────────────────
            inspect_t0    = time.perf_counter()
            issues        = self._inspect(path)
            result.inspect_elapsed = time.perf_counter() - inspect_t0
            result.issues = issues
            has_errors    = any(i.severity == "error"   for i in issues)
            has_warnings  = any(i.severity == "warning" for i in issues)

            # ── Copy / repair ──────────────────────────────────────────────
            if not self.dry_run:
                zero_byte = any(i.code == "zero_byte" for i in issues)
                if zero_byte:
                    result.status = "skipped"
                    self._logger.warning(f"SKIPPED {rel} – zero byte")
                else:
                    out_path, result.issues, copy_el, repair_el = attempt_video_repair(
                        path, self.dst, rel, result.issues,
                        self.allow_reencode, self.use_hwaccel,
                        use_colour=self.use_colour)
                    result.copy_elapsed   = copy_el
                    result.repair_elapsed = repair_el

                    if out_path and out_path.exists():
                        sha_t0          = time.perf_counter()
                        result.sha256, result.sha_elapsed = sha256_of(out_path)
                        write_bytes     = out_path.stat().st_size
                        write_elapsed   = copy_el + repair_el
                        if write_elapsed > 0:
                            result.throughput_mbps = (
                                write_bytes / 1024 / 1024 / write_elapsed)

                        repaired_any   = any(i.repaired for i in result.issues)
                        unrepaired_err = any(i.severity == "error" and not i.repaired
                                             for i in result.issues)
                        if unrepaired_err:
                            result.status = "partial"
                        elif repaired_any:
                            result.status = "repaired"
                            for i in result.issues:
                                if i.repaired and i.repair_note:
                                    result.repair_strategy = i.repair_note.split()[0].lower()
                                    break
                        else:
                            result.status = "ok"
                    else:
                        result.status = "failed"

                self._logger.info(
                    f"{result.status.upper():10} {path}  ->  {rel}  "
                    f"sha256={result.sha256[:16] or '-'}..."
                    + (f"  issues={len(issues)}" if issues else "")
                    + (f"  {result.throughput_mbps:.1f}MB/s" if result.throughput_mbps else ""))
            else:
                if any(i.code == "zero_byte" for i in issues):
                    result.status = "skipped"
                elif has_errors:
                    result.status = "partial"
                elif has_warnings:
                    result.status = "repaired"
                else:
                    result.status = "ok"

            result.elapsed = time.perf_counter() - t0
            self.results.append(result)

            if self._journal and not self.dry_run:
                self._journal.record(result)

            show = has_errors or has_warnings or self.verbose
            self._file_line(idx, total, result)
            if show:
                self._issue_lines(result)

        # ── End of loop ────────────────────────────────────────────────────
        self._p("")
        wall = time.perf_counter() - self._wall_t0
        self._footer(shutdown_reason=_shutdown_reason)

        if self._journal and not self.dry_run:
            self._journal.consolidate()

        # ── Write final report ─────────────────────────────────────────────
        if not self.dry_run and self._run_dir:
            txt_path, json_path = write_report(
                run_dir        = self._run_dir,
                run_ts         = self._run_ts,
                results        = self.results,
                sources_label  = self.sources_label,
                dst            = self.dst,
                shutdown_reason= _shutdown_reason,
                wall_elapsed   = wall,
            )
            self._p(f"  Report (text) : {txt_path}")
            self._p(f"  Report (JSON) : {json_path}")
            self._p("")

# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Copy and repair video files – macOS version.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("sources", nargs="+", metavar="SOURCE",
        help="Files, directories, or glob patterns. Last argument = destination.")
    ap.add_argument("--dry-run",      action="store_true")
    ap.add_argument("--verbose",      action="store_true")
    ap.add_argument("--ext",          type=str, default=None)
    ap.add_argument("--list",         type=Path, default=None, metavar="FILE")
    ap.add_argument("--no-reencode",  action="store_true")
    ap.add_argument("--resume",       action="store_true")
    ap.add_argument("--no-hwaccel",   action="store_true")
    ap.add_argument("--no-colour",    action="store_true")
    ap.add_argument("--recursive",    action="store_true", default=True)
    ap.add_argument("--no-recursive", dest="recursive", action="store_false")
    args = ap.parse_args()

    if args.no_colour:
        global _COLOUR_ENABLED
        _COLOUR_ENABLED = False

    *raw_sources, destination = args.sources
    dst = Path(destination).expanduser().resolve()

    source_strings: List[str] = list(raw_sources)
    if args.list:
        if not args.list.is_file():
            print(f"  Error: list file '{args.list}' not found.")
            sys.exit(1)
        source_strings += load_list_file(args.list)

    if not source_strings:
        print("  Error: no source files specified.")
        sys.exit(1)

    ext_filter: Optional[Set[str]] = None
    if args.ext:
        ext_filter = {e if e.startswith(".") else "." + e
                      for e in args.ext.split(",")}
    else:
        ext_filter = ALL_VIDEO_EXTS

    files, errors = resolve_inputs(source_strings, args.recursive, ext_filter)
    for e in errors:
        print(f"  Warning: {e}")

    if len(source_strings) == 1:
        label = source_strings[0]
    elif len(source_strings) <= 4:
        label = "\n".join(source_strings)
    else:
        label = "\n".join(source_strings[:3]) + f"\n  ... and {len(source_strings)-3} more"

    VideoRepairEngine(
        files          = files,
        dst            = dst,
        dry_run        = args.dry_run,
        verbose        = args.verbose,
        ext_filter     = ext_filter,
        allow_reencode = not args.no_reencode,
        use_hwaccel    = not args.no_hwaccel,
        use_colour     = not args.no_colour,
        resume         = args.resume,
        sources_label  = label,
    ).run()

if __name__ == "__main__":
    main()
