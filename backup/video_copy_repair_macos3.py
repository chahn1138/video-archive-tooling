#!/usr/bin/env python3
"""
video_copy_repair_macos.py
--------------------------
Copy video files from one or more sources to a destination, inspecting each
file for common integrity issues and attempting repairs before writing.

PLATFORM: macOS (Intel x86_64 and Apple Silicon M-series)
          Tested on macOS Ventura 13+, Sonoma 14+, Sequoia 15+

Required tools — install once via Homebrew:
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    brew install ffmpeg
    brew install mkvtoolnix           # for MKV repair

    Apple Silicon (M1/M2/M3/M4) note:
      Homebrew installs to /opt/homebrew/bin  (not /usr/local/bin)
      This script checks both locations automatically.

Optional Python package:
    pip3 install pymkv2

Supported formats and checks
-----------------------------
  MP4 / MOV / M4V / 3GP   — ffprobe metadata, moov atom detection,
                             stream validity, duration sanity
  MKV / WEBM               — ffprobe + mkvmerge --identify, EBML header
  AVI / WMV / FLV / TS     — ffprobe metadata and stream checks
  All video                 — container signature, zero-byte, SHA-256

Repair strategies (in order of preference — least destructive first)
----------------------------------------------------------------------
  1. Remux (stream copy)    — rebuilds container index, fixes moov/cues,
                              no quality loss, fastest
  2. Moov faststart         — relocates moov atom to front of MP4/MOV
  3. MKV remux via mkvmerge — rebuilds Matroska structure cleanly
  4. Re-encode (last resort)— full transcode; quality loss possible,
                              used only when remux fails entirely

macOS-specific notes
---------------------
  - VideoToolbox hardware acceleration is used for H.264/HEVC re-encode
    when available (M-series chips and recent Intel Macs with T2).
    Falls back to libx264 software encode automatically.
  - macOS file system is case-insensitive by default. Files that differ
    only in case will be deduplicated before processing.
  - Gatekeeper may block downloaded ffmpeg binaries. If so, run:
      sudo xattr -rd com.apple.quarantine /opt/homebrew/bin/ffmpeg

Input modes
-----------
  Directory    video_copy_repair_macos.py /Videos/ ~/backup/
  Single file  video_copy_repair_macos.py clip.mp4 ~/backup/
  Glob         video_copy_repair_macos.py "/Volumes/NAS/*.mkv" ~/backup/
  Multiple     video_copy_repair_macos.py a.mp4 b.mkv "*.avi" ~/backup/
  List file    video_copy_repair_macos.py --list files.txt ~/backup/

Options
-------
  --dry-run        Probe and report without writing any files
  --verbose        Show details for every file, not just problems
  --ext            Comma-separated extensions, e.g. .mp4,.mkv
  --list FILE      Text file of source paths/globs (one per line, # = comment)
  --no-reencode    Never attempt re-encode, even as last resort
  --no-hwaccel     Disable VideoToolbox hardware acceleration
  --recursive      Recurse into sub-directories (default: on)
  --no-recursive   Do not recurse
"""

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── ANSI colour helpers ────────────────────────────────────────────────────────
RESET   = "\033[0m"; BOLD    = "\033[1m"; DIM    = "\033[2m"
GREEN   = "\033[32m"; YELLOW = "\033[33m"; RED   = "\033[31m"
CYAN    = "\033[36m"; BLUE   = "\033[34m"; MAGENTA = "\033[35m"

def c(text, colour): return f"{colour}{text}{RESET}"

# ── macOS platform detection ───────────────────────────────────────────────────

def is_apple_silicon() -> bool:
    """True on M-series Macs (arm64 native or Rosetta 2)."""
    return platform.machine() in ("arm64", "arm64e")

def detect_macos_version() -> Tuple[int, int]:
    """Return (major, minor) macOS version tuple."""
    ver = platform.mac_ver()[0]
    try:
        parts = ver.split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 0, 0

APPLE_SILICON = is_apple_silicon()
MACOS_VER     = detect_macos_version()

# ── Video format sets ──────────────────────────────────────────────────────────
MP4_EXTS   = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".m4a", ".f4v"}
MKV_EXTS   = {".mkv", ".webm", ".mka"}
AVI_EXTS   = {".avi"}
TS_EXTS    = {".ts", ".mts", ".m2ts", ".mxf"}
OTHER_EXTS = {".wmv", ".flv", ".asf", ".rm", ".rmvb", ".vob", ".mpg", ".mpeg",
              ".divx", ".ogv", ".ogg"}
ALL_VIDEO_EXTS = MP4_EXTS | MKV_EXTS | AVI_EXTS | TS_EXTS | OTHER_EXTS

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
    issues: List[Issue] = field(default_factory=list)
    sha256: str = ""
    status: str = "pending"
    repair_strategy: str = ""
    elapsed: float = 0.0

# ── Tool discovery — macOS aware ───────────────────────────────────────────────

# Homebrew installs to different prefixes on Intel vs Apple Silicon
HOMEBREW_PREFIXES = [
    "/opt/homebrew/bin",    # Apple Silicon (M1/M2/M3/M4)
    "/usr/local/bin",       # Intel Macs
    "/opt/local/bin",       # MacPorts fallback
]

def find_tool(name: str) -> Optional[str]:
    """
    Find a tool on macOS, checking Homebrew prefixes explicitly before PATH.
    This avoids issues where system PATH doesn't include Homebrew on fresh installs.
    """
    # Check Homebrew prefixes first
    for prefix in HOMEBREW_PREFIXES:
        p = Path(prefix) / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    # Fall back to PATH
    return shutil.which(name)


class ToolSet:
    """Discovers external video tools and reports their status."""

    REQUIRED = ["ffmpeg", "ffprobe"]
    OPTIONAL = ["mkvmerge", "mkvinfo", "mkvpropedit"]

    def __init__(self):
        self.paths: Dict[str, Optional[str]] = {}
        self._discover()

    def _discover(self):
        for tool in self.REQUIRED + self.OPTIONAL:
            self.paths[tool] = find_tool(tool)

    @property
    def ffmpeg(self) -> Optional[str]:
        return self.paths["ffmpeg"]

    @property
    def ffprobe(self) -> Optional[str]:
        return self.paths["ffprobe"]

    @property
    def mkvmerge(self) -> Optional[str]:
        return self.paths["mkvmerge"]

    @property
    def has_mkvtools(self) -> bool:
        return bool(self.paths.get("mkvmerge"))

    def check_required(self) -> bool:
        return all(self.paths[t] for t in self.REQUIRED)

    def report(self) -> List[str]:
        import platform as _plat
        arch = "Apple Silicon" if _plat.machine() in ("arm64","arm64e") else "Intel"
        lines = [f"    {c(arch, MAGENTA)}  macOS {_plat.mac_ver()[0]}"]
        for tool in self.REQUIRED:
            p = self.paths[tool]
            tag = c("✓", GREEN) if p else c("✕ MISSING", RED)
            hint = "" if p else c("  →  brew install ffmpeg", YELLOW)
            lines.append(f"    {tag}  {tool:<14} {p or ''}{hint}")
        for tool in self.OPTIONAL:
            p = self.paths[tool]
            tag = c("✓", GREEN) if p else c("○ optional", DIM)
            hint = "" if p else c("  →  brew install mkvtoolnix", DIM)
            lines.append(f"    {tag}  {tool:<14} {p or ''}{hint}")
        return lines


TOOLS = ToolSet()

# ── Hardware encoder detection ─────────────────────────────────────────────────
#
# Windows supports three GPU-accelerated H.264 encoders via ffmpeg:
#
#   h264_nvenc  — Nvidia (GeForce GTX 600+ / RTX series)
#   h264_amf    — AMD   (Radeon RX 400+ / recent APUs)
#   h264_qsv    — Intel (QuickSync, 6th-gen Core / "Skylake" and newer)
#
# We query ffmpeg's encoder list once and cache results.  Preference order:
#   NVENC > AMF > QSV > libx264 (software)

@dataclass
class HWAccel:
    nvenc: bool = False
    amf:   bool = False
    qsv:   bool = False

    @property
    def best(self) -> Optional[str]:
        """Return the best available encoder name, or None for software."""
        if self.nvenc: return "h264_nvenc"
        if self.amf:   return "h264_amf"
        if self.qsv:   return "h264_qsv"
        return None

    @property
    def label(self) -> str:
        if self.nvenc: return "Nvidia NVENC"
        if self.amf:   return "AMD AMF"
        if self.qsv:   return "Intel QuickSync"
        return "libx264 (software)"


def detect_hw_encoders() -> HWAccel:
    """Query ffmpeg for available hardware encoders."""
    hw = HWAccel()
    if not TOOLS.ffmpeg:
        return hw
    rc, out, err = run([TOOLS.ffmpeg, "-hide_banner", "-encoders"], timeout=10)
    hw.nvenc = "h264_nvenc" in out
    hw.amf   = "h264_amf"   in out
    hw.qsv   = "h264_qsv"   in out
    return hw


HW: Optional[HWAccel] = None   # resolved lazily after tool check

# ── Subprocess helpers ─────────────────────────────────────────────────────────

def _subprocess_kwargs() -> dict:
    """Suppress console flash on Windows."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    return kwargs


def run(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a command, wait for completion. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
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
    """
    Run a command and show a live spinner with elapsed time and the last
    meaningful stderr line from the process.  Prevents silent hanging on
    large or badly corrupt files.

    The spinner overwrites a single terminal line using \\r so log output
    above it is preserved.  On completion the line is cleared.

    Returns (returncode, stdout, stderr) — same interface as run().
    """
    import threading

    SPINNER    = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    spin_idx   = 0
    last_line  = [""]
    stdout_buf: List[str] = []
    stderr_buf: List[str] = []

    def _drain(stream, buf: List[str], is_stderr: bool):
        for raw in stream:
            line = raw.rstrip()
            buf.append(line)
            if is_stderr and line and not line.startswith("  "):
                last_line[0] = line[:80]
        stream.close()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            **_subprocess_kwargs())
    except FileNotFoundError as e:
        return -1, "", str(e)

    t_out = threading.Thread(target=_drain,
                             args=(proc.stdout, stdout_buf, False), daemon=True)
    t_err = threading.Thread(target=_drain,
                             args=(proc.stderr, stderr_buf, True),  daemon=True)
    t_out.start()
    t_err.start()

    t_start   = time.perf_counter()
    timed_out = False

    col_spin = "\033[36m" if use_colour else ""
    col_dim  = "\033[2m"  if use_colour else ""
    rst      = "\033[0m"  if use_colour else ""

    while proc.poll() is None:
        elapsed = time.perf_counter() - t_start
        if elapsed > timeout:
            proc.kill()
            timed_out = True
            break

        spin  = SPINNER[spin_idx % len(SPINNER)] if use_colour else "-"
        spin_idx += 1

        last  = last_line[0]
        if len(last) > 52:
            last = last[:49] + "..."

        line = (f"  {col_spin}{spin}{rst}  "
                f"{col_dim}{label:<22}{rst}  "
                f"{col_dim}{elapsed:>5.1f}s{rst}  "
                f"{col_dim}{last}{rst}")
        print(f"\r{line:<78}", end="", flush=True)
        time.sleep(0.12)

    t_out.join(timeout=2)
    t_err.join(timeout=2)
    print(f"\r{' ' * 80}\r", end="", flush=True)   # clear spinner line

    if timed_out:
        return -1, "", f"Timed out after {timeout}s"

    return proc.returncode, "\n".join(stdout_buf), "\n".join(stderr_buf)


def size_scaled_timeout(path: Path, base: int = 60,
                        bps: int = 20 * 1024 * 1024) -> int:
    """
    Timeout scaled to file size assuming ~20 MB/s decode throughput.
    Floor = base seconds.  Ceiling = 3600s.

      500 MB →  60s (floor)
        2 GB → 160s
       20 GB → 1000s
    """
    try:
        size = path.stat().st_size
    except OSError:
        return base
    return min(max(base, size // bps), 3600)


def ffprobe_json(path: Path, use_colour: bool = False) -> Optional[dict]:
    if not TOOLS.ffprobe:
        return None
    cmd = [
        TOOLS.ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_error",
        str(path)
    ]
    timeout = size_scaled_timeout(path, base=30)
    rc, out, err = run_watched(cmd, label="ffprobe", timeout=timeout,
                               use_colour=use_colour)
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
                f"Missing EBML header — not a valid Matroska file "
                f"(got {raw[:4].hex()})"))
    elif ext in AVI_EXTS:
        if raw[:4] != b"RIFF" or raw[8:12] != b"AVI ":
            issues.append(Issue("error", "avi_bad_signature",
                "Missing RIFF/AVI signature"))
    elif ext in {".wmv", ".asf"}:
        # ASF/WMV: GUID header 30 26 B2 75 8E 66 CF 11 A6 D9 00 AA 00 62 CE 6C
        if raw[:4] != b"\x30\x26\xb2\x75":
            issues.append(Issue("warning", "wmv_bad_signature",
                "Missing ASF/WMV GUID header — file may not be a valid WMV"))
    return issues


def check_mp4_moov_atom(path: Path) -> List[Issue]:
    """Scan for moov atom position — reads only head and tail, safe on huge files."""
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
            "moov atom not found — recording likely interrupted before finalisation. "
            "Repair will attempt remux with -movflags faststart"))
    elif moov_in_tail and not moov_in_head:
        issues.append(Issue("warning", "mp4_moov_at_end",
            "moov atom is at end of file (not streamable). "
            "Repair will relocate it to the front"))
    if has_mdat and not has_moov:
        issues.append(Issue("warning", "mp4_mdat_only",
            "mdat block found but no moov index — partial recovery may be possible"))
    return issues


def check_via_ffprobe(path: Path, use_colour: bool = False) -> List[Issue]:
    issues = []
    data = ffprobe_json(path, use_colour=use_colour)

    if data is None:
        issues.append(Issue("error", "ffprobe_failed",
            "ffprobe could not read file — likely corrupt or unrecognised format"))
        return issues

    if "error" in data:
        msg = data["error"].get("string", "unknown error")
        issues.append(Issue("error", "ffprobe_error", f"ffprobe: {msg}"))
        return issues

    fmt     = data.get("format", {})
    streams = data.get("streams", [])

    if not streams:
        issues.append(Issue("error", "no_streams",
            "File contains no audio or video streams"))
        return issues

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        issues.append(Issue("warning", "no_video_stream",
            "No video stream found — audio-only or stripped container"))

    duration = float(fmt.get("duration", 0) or 0)
    if duration == 0:
        issues.append(Issue("error", "zero_duration",
            "Duration is 0 — container metadata may be missing or corrupt"))
    elif duration < 0:
        issues.append(Issue("error", "negative_duration",
            f"Negative duration ({duration:.2f}s) — corrupt metadata"))

    bit_rate = int(fmt.get("bit_rate", 0) or 0)
    if bit_rate == 0 and duration > 0:
        issues.append(Issue("warning", "zero_bitrate",
            "Bitrate reported as 0 — index may be missing"))

    for vs in video_streams:
        codec  = vs.get("codec_name", "unknown")
        width  = vs.get("width", 0)
        height = vs.get("height", 0)
        if codec in ("none", "unknown"):
            issues.append(Issue("error", "unknown_video_codec",
                f"Video codec not identified: '{codec}'"))
        if width == 0 or height == 0:
            issues.append(Issue("error", "zero_dimensions",
                f"Video dimensions {width}x{height} — stream header corrupt"))
        r_frame_rate = vs.get("r_frame_rate", "0/1")
        try:
            num, den = map(int, r_frame_rate.split("/"))
            fps = num / den if den else 0
            if fps > 240:
                issues.append(Issue("warning", "unusual_fps",
                    f"Unusually high frame rate: {fps:.1f} fps — metadata may be corrupt"))
        except Exception:
            pass

    if path.suffix.lower() in MP4_EXTS:
        issues.extend(check_mp4_moov_atom(path))

    return issues


def check_via_mkvmerge(path: Path, use_colour: bool = False) -> List[Issue]:
    issues = []
    if not TOOLS.has_mkvtools:
        return issues
    cmd = [TOOLS.mkvmerge, "--identify",
           "--identification-format", "json", str(path)]
    timeout = size_scaled_timeout(path, base=30)
    rc, out, err = run_watched(cmd, label="mkvmerge identify", timeout=timeout,
                               use_colour=use_colour)
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
    """
    Decode-probe the full file, streaming stderr live so the user can see
    progress.  Timeout is scaled to file size — a 20 GB file gets up to
    1000s, a 500 MB file gets the 60s floor.
    """
    issues = []
    if not TOOLS.ffmpeg:
        return issues
    cmd = [TOOLS.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"]
    timeout = size_scaled_timeout(path, base=60)
    rc, out, err = run_watched(cmd, label="ffmpeg decode-probe", timeout=timeout,
                               use_colour=use_colour)
    if rc == -1 and "Timed out" in err:
        issues.append(Issue("warning", "decode_probe_timeout",
            f"Decode probe timed out after {timeout}s — file may be very large "
            f"or severely corrupt. Copy will still be attempted."))
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
    """Stream-copy remux — lossless, fixes most index/container issues."""
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy", "-map", "0",
        str(dst)
    ]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="remux (stream copy)", timeout=timeout,
                             use_colour=use_colour)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "Stream-copy remux succeeded (lossless)"
    return False, f"Remux failed (rc={rc}): {err.strip()[-200:]}"


def repair_mp4_faststart(src: Path, dst: Path, use_colour: bool = True) -> Tuple[bool, str]:
    """Remux MP4 with moov relocated to the front of the file."""
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy", "-map", "0",
        "-movflags", "+faststart",
        str(dst)
    ]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="MP4 moov faststart", timeout=timeout,
                             use_colour=use_colour)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "MP4 moov faststart relocation succeeded"
    return False, f"Faststart failed (rc={rc}): {err.strip()[-200:]}"


def repair_mkv_mkvmerge(src: Path, dst: Path, use_colour: bool = True) -> Tuple[bool, str]:
    """Rebuild MKV structure using mkvmerge."""
    if not TOOLS.has_mkvtools:
        return False, ("mkvmerge not available — install via: "
                       "winget install MKVToolNix")
    cmd = [TOOLS.mkvmerge, "--no-global-tags", "-o", str(dst), str(src)]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="mkvmerge remux", timeout=timeout,
                             use_colour=use_colour)
    if rc in (0, 1) and dst.exists() and dst.stat().st_size > 100:
        note = "MKV remux via mkvmerge succeeded"
        if rc == 1:
            note += " (with warnings)"
        return True, note
    return False, f"mkvmerge failed (rc={rc}): {err.strip()[-200:]}"


def repair_reencode(src: Path, dst: Path,
                    use_hwaccel: bool = True,
                    use_colour: bool = True) -> Tuple[bool, str]:
    """
    Re-encode using the best available Windows GPU encoder, falling back
    through NVENC → AMF → QSV → libx264.
    """
    global HW
    if HW is None:
        HW = detect_hw_encoders()

    encoder = HW.best if (use_hwaccel and HW) else None

    if encoder == "h264_nvenc":
        codec_args = ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "18", "-preset", "p4"]
        method = "Nvidia NVENC HW H.264"
    elif encoder == "h264_amf":
        codec_args = ["-c:v", "h264_amf", "-quality", "quality"]
        method = "AMD AMF HW H.264"
    elif encoder == "h264_qsv":
        codec_args = ["-c:v", "h264_qsv", "-global_quality", "18", "-preset", "medium"]
        method = "Intel QuickSync HW H.264"
    else:
        codec_args = ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
        method = "libx264 SW H.264"

    timeout = size_scaled_timeout(src)
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        *codec_args,
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst)
    ]
    rc, _, err = run_watched(cmd, label=f"re-encode ({method[:14]})", timeout=timeout,
                             use_colour=use_colour)

    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, f"Re-encode succeeded using {method}"

    # GPU encoder failed — fall back to software
    if encoder:
        cmd_sw = [
            TOOLS.ffmpeg, "-y",
            "-err_detect", "ignore_err",
            "-i", str(src),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(dst)
        ]
        rc2, _, err2 = run_watched(cmd_sw, label="re-encode (libx264 SW)",
                                   timeout=timeout, use_colour=use_colour)
        if rc2 == 0 and dst.exists() and dst.stat().st_size > 100:
            return True, f"Re-encode succeeded using libx264 SW (GPU fallback after {method} failed)"

    return False, f"Re-encode failed (rc={rc}): {err.strip()[-200:]}"


def attempt_video_repair(
        src: Path, dst_dir: Path, rel: str,
        issues: List[Issue],
        allow_reencode: bool,
        use_hwaccel: bool,
        use_colour: bool = True) -> Tuple[Optional[Path], List[Issue]]:
    """
    Try repair strategies in order. First success wins.
    Returns (output_path_or_None, updated_issues).
    """
    ext        = src.suffix.lower()
    has_errors = any(i.severity == "error" for i in issues)

    if not has_errors:
        # Clean file — straight copy preserving timestamps
        dst = dst_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst, issues

    # Build strategy list
    strategies: List[Tuple[str, object]] = []

    moov_issue = any(i.code in ("mp4_no_moov", "mp4_moov_at_end") for i in issues)
    if ext in MP4_EXTS and moov_issue:
        strategies.append(("faststart",
                            lambda s, d: repair_mp4_faststart(s, d, use_colour)))

    strategies.append(("remux",
                        lambda s, d: repair_remux(s, d, use_colour)))

    if ext in MKV_EXTS and TOOLS.has_mkvtools:
        strategies.append(("mkvmerge",
                            lambda s, d: repair_mkv_mkvmerge(s, d, use_colour)))

    if allow_reencode:
        strategies.append(("reencode",
                            lambda s, d: repair_reencode(s, d, use_hwaccel, use_colour)))

    dst_dir.mkdir(parents=True, exist_ok=True)

    for strategy_name, strategy_fn in strategies:
        suffix = ".mp4" if (strategy_name == "reencode"
                            and ext not in MP4_EXTS) else ext
        # Use a temp file so a failed attempt never clobbers the destination
        with tempfile.NamedTemporaryFile(
                dir=dst_dir, suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)

        success, note = strategy_fn(src, tmp_path)

        if success:
            final = dst_dir / rel
            final.parent.mkdir(parents=True, exist_ok=True)
            # On Windows, Path.replace() is atomic within the same volume
            shutil.move(str(tmp_path), str(final))
            for issue in issues:
                if issue.severity == "error" and not issue.repaired:
                    issue.repaired = True
                    issue.repair_note = note
            return final, issues
        else:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            issues.append(Issue("warning", f"{strategy_name}_failed",
                f"Strategy '{strategy_name}' did not succeed: {note[:120]}"))

    return None, issues

# ── Helpers ────────────────────────────────────────────────────────────────────

def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest()

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def bar(done: int, total: int, width: int = 28) -> str:
    if total == 0:
        return "[" + "-" * width + "]"
    filled = int(width * done / total)
    # Use ASCII fallback chars if colour is off (older Windows consoles)
    fill_ch = "█"
    rest_ch = "─"
    return "[" + fill_ch * filled + rest_ch * (width - filled) + "]"

# ── Input resolution — Windows-aware ──────────────────────────────────────────

def resolve_inputs(sources: List[str], recursive: bool,
                   ext_filter: Optional[Set[str]]) -> Tuple[List[Path], List[str]]:
    """
    Expand a mixed list of paths, globs, and directories into concrete files.

    Windows notes:
    - NTFS is case-insensitive: dedup uses lowercased path keys.
    - UNC paths (\\\\server\\share\\...) work via pathlib transparently.
    - cmd.exe and PowerShell do NOT expand globs — Python handles them here.
    - Both backslash and forward-slash separators are normalised by pathlib.
    """
    seen:   Dict[str, Path] = {}   # lower-key → resolved Path (case-insensitive dedup)
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
        # Normalise Windows backslashes for glob detection
        src_norm = src.replace("\\", "/")
        has_glob = any(ch in src_norm for ch in ("*", "?", "["))

        if has_glob:
            # Split on last non-glob directory component
            parts  = Path(src)
            parent  = parts.parent
            pattern = parts.name
            # If the parent also has glob chars, root at cwd
            if any(ch in str(parent) for ch in ("*", "?", "[")):
                parent  = Path(".")
                pattern = src
            matches = sorted(Path(parent).glob(pattern))
            if not matches:
                errors.append(f"No files matched glob: {src}")
            for m in matches:
                if m.is_file():   _add(m)
                elif m.is_dir():  _expand_dir(m)
        else:
            p = Path(src).expanduser().resolve()
            if p.is_dir():
                _expand_dir(p)
            elif p.is_file():
                _add(p)
            else:
                errors.append(f"Not found: {src}")

    return list(seen.values()), errors


def load_list_file(path: Path) -> List[str]:
    """Read a text file of source paths/globs, one per line. # = comment."""
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8-sig").splitlines()
        # utf-8-sig strips the BOM that Notepad adds to UTF-8 files on Windows
        if ln.strip() and not ln.strip().startswith("#")
    ]

# ── Engine ─────────────────────────────────────────────────────────────────────


# ── Run journal ────────────────────────────────────────────────────────────────
#
# The journal is a JSONL file written to the destination directory.  Each line
# is one completed file result.  On restart the script reads the journal,
# rebuilds a set of "already done" relative paths, and skips the destination
# health-check for those — trusting its own prior certified work.
#
# Naming convention:
#   video_repair_journal__<src_slug>__<dst_slug>__<start_timestamp>.jsonl
#
# On restart, the script scans the destination for journals whose src+dst slug
# matches the current run and loads the newest one.  If the newest is from
# THIS run (already open) it appends.  On clean finish it merges all entries
# from the loaded journal plus this run and writes a fresh consolidated file,
# then removes the old one atomically.
#
# Crash safety: the merge+rewrite uses write-to-temp / atomic-rename so the
# old journal is never touched until the new one is fully flushed to disk.

class RunJournal:
    """
    Per-run JSONL journal that records every completed file result.

    Terminal states that are considered "certified done" by this script
    and will be trusted unconditionally on resume:
        ok, repaired, done

    States that will NOT be trusted (will be re-attempted on resume):
        partial, failed, skipped
    """

    TRUSTED = {"ok", "repaired", "done"}

    def __init__(self, dst: Path, src_label: str, run_ts: str):
        """
        dst        — destination directory (journal lives here)
        src_label  — human label for the source (sanitised into slug)
        run_ts     — ISO-like timestamp string for this run, e.g. "20260503-1037"
        """
        self.dst      = dst
        self.run_ts   = run_ts
        self._src_slug = self._slugify(src_label)
        self._dst_slug = self._slugify(str(dst))
        self._stem     = f"video_repair_journal__{self._src_slug}__{self._dst_slug}"
        self._fname    = f"{self._stem}__{run_ts}.jsonl"
        self._path     = dst / self._fname

        # In-memory index: rel_path (lower on Windows) → entry dict
        self._entries: Dict[str, dict] = {}

        # Previously certified entries loaded from an earlier journal
        self._loaded_from: Optional[Path] = None

    # ── slug helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _slugify(text: str) -> str:
        """
        Produce a safe filename component from an arbitrary path or label.
        Keeps letters, digits, dots and hyphens; replaces everything else
        with underscores; collapses runs; trims to 48 chars.
        """
        import re
        s = re.sub(r'[^\w.\-]', '_', text)
        s = re.sub(r'_+', '_', s).strip('_')
        return s[:48]

    # ── discovery ─────────────────────────────────────────────────────────────

    def find_previous(self) -> Optional[Path]:
        """
        Scan dst for journals matching this src+dst slug pair.
        Return the Path of the newest one (by filename timestamp, then mtime),
        or None if none found.  Raises RuntimeError if two share an identical
        timestamp string (shouldn't happen but we guard against it).
        """
        if not self.dst.exists():
            return None
        pattern = f"{self._stem}__*.jsonl"
        candidates = sorted(self.dst.glob(pattern),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True)
        # Exclude the current run's file (hasn't been written yet)
        candidates = [p for p in candidates if p.name != self._fname]
        return candidates[0] if candidates else None

    # ── load ──────────────────────────────────────────────────────────────────

    def load(self, path: Path) -> int:
        """
        Read a previous journal file and populate self._entries.
        Returns the count of trusted entries loaded.
        Only TRUSTED states are indexed — partial/failed are ignored so they
        get re-attempted.
        """
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
                    pass   # corrupt line — skip it
        except OSError:
            pass
        self._loaded_from = path
        return count

    # ── query ─────────────────────────────────────────────────────────────────

    def is_done(self, rel: str) -> Tuple[bool, dict]:
        """
        Return (True, entry) if rel is already certified done, else (False, {}).
        """
        entry = self._entries.get(self._key(rel), {})
        return (bool(entry), entry)

    # ── record ────────────────────────────────────────────────────────────────

    def record(self, result: "FileResult"):
        """
        Append one result to the journal file immediately (crash-safe append).
        Also updates the in-memory index if the status is trusted.
        """
        entry = {
            "ts":       datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "run":      self.run_ts,
            "rel":      result.rel,
            "src":      str(result.path),
            "status":   result.status,
            "size":     result.size_bytes,
            "sha256":   result.sha256,
            "elapsed":  round(result.elapsed, 2),
            "strategy": result.repair_strategy,
            "issues":   [
                {"severity": i.severity, "code": i.code,
                 "repaired": i.repaired, "note": i.repair_note}
                for i in result.issues
            ],
        }
        # Append to journal file immediately
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())   # ensure bytes hit disk before continuing
        except OSError as e:
            pass   # journal write failure is non-fatal — log to stderr only
        # Update in-memory index
        if result.status in self.TRUSTED:
            self._entries[self._key(result.rel)] = entry

    # ── consolidate ───────────────────────────────────────────────────────────

    def consolidate(self):
        """
        Merge the previously loaded journal (if any) with this run's entries
        and write a single fresh consolidated journal.

        Uses write-to-temp + atomic rename:
          1. Write merged content to <fname>.tmp
          2. fsync the temp file
          3. Replace the old journal with the temp file (atomic on same volume)
          4. Delete the previous journal (if different from current)

        A crash at any point before step 3 leaves the old journal intact.
        A crash between steps 3 and 4 leaves a harmless orphan .tmp file.
        """
        if self.dry_run_mode:
            return

        # Collect all entries: loaded entries + anything recorded this run
        # (self._entries already merges both via the record() calls)
        all_entries = list(self._entries.values())

        tmp_path = self._path.with_suffix(".jsonl.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in all_entries:
                    f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())

            # Atomic replace
            shutil.move(str(tmp_path), str(self._path))

            # Remove the old journal if it was a different file
            if self._loaded_from and self._loaded_from != self._path:
                try:
                    self._loaded_from.unlink()
                except OSError:
                    pass

        except OSError as e:
            # Non-fatal — the per-run journal still has the data
            try:
                tmp_path.unlink()
            except OSError:
                pass

    @property
    def dry_run_mode(self) -> bool:
        return not self._path.parent.exists()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def _key(self, rel: str) -> str:
        """Case-insensitive key for NTFS compatibility."""
        return rel.lower()


#
# Tools land in very different places depending on how they were installed:
#
#   winget   → %LOCALAPPDATA%\Microsoft\WinGet\Packages\...\ffmpeg.exe
#              (deeply nested; winget shims usually add to PATH anyway)
#   choco    → C:\ProgramData\chocolatey\bin\ffmpeg.exe
#   scoop    → C:\Users\<user>\scoop\shims\ffmpeg.exe
#              or C:\ProgramData\scoop\shims\ffmpeg.exe  (global install)
#   manual   → C:\ffmpeg\bin\ffmpeg.exe  (common convention)
#   MKVToolNix installer → C:\Program Files\MKVToolNix\mkvmerge.exe

WINDOWS_SEARCH_PATHS = [
    # Chocolatey
    r"C:\ProgramData\chocolatey\bin",
    # Scoop — per-user
    os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
    # Scoop — global
    r"C:\ProgramData\scoop\shims",
    # Common manual ffmpeg install
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
    # MKVToolNix installer default
    r"C:\Program Files\MKVToolNix",
    r"C:\Program Files (x86)\MKVToolNix",
]

def find_tool(name: str) -> Optional[str]:
    """
    Find a Windows executable, checking known install locations before PATH.
    Appends .exe automatically if not already present.
    """
    exe = name if name.endswith(".exe") else name + ".exe"

    # Check explicit search paths first
    for directory in WINDOWS_SEARCH_PATHS:
        candidate = Path(directory) / exe
        if candidate.exists():
            return str(candidate)

    # Fall back to PATH (winget shims, user-configured PATH, etc.)
    found = shutil.which(exe) or shutil.which(name)
    return found



# ── Hardware encoder detection ─────────────────────────────────────────────────
#
# Windows supports three GPU-accelerated H.264 encoders via ffmpeg:
#
#   h264_nvenc  — Nvidia (GeForce GTX 600+ / RTX series)
#   h264_amf    — AMD   (Radeon RX 400+ / recent APUs)
#   h264_qsv    — Intel (QuickSync, 6th-gen Core / "Skylake" and newer)
#
# We query ffmpeg's encoder list once and cache results.  Preference order:
#   NVENC > AMF > QSV > libx264 (software)

@dataclass
class HWAccel:
    nvenc: bool = False
    amf:   bool = False
    qsv:   bool = False

    @property
    def best(self) -> Optional[str]:
        """Return the best available encoder name, or None for software."""
        if self.nvenc: return "h264_nvenc"
        if self.amf:   return "h264_amf"
        if self.qsv:   return "h264_qsv"
        return None

    @property
    def label(self) -> str:
        if self.nvenc: return "Nvidia NVENC"
        if self.amf:   return "AMD AMF"
        if self.qsv:   return "Intel QuickSync"
        return "libx264 (software)"


def detect_hw_encoders() -> HWAccel:
    """Query ffmpeg for available hardware encoders."""
    hw = HWAccel()
    if not TOOLS.ffmpeg:
        return hw
    rc, out, err = run([TOOLS.ffmpeg, "-hide_banner", "-encoders"], timeout=10)
    hw.nvenc = "h264_nvenc" in out
    hw.amf   = "h264_amf"   in out
    hw.qsv   = "h264_qsv"   in out
    return hw


HW: Optional[HWAccel] = None   # resolved lazily after tool check

# ── Subprocess helpers ─────────────────────────────────────────────────────────

def _subprocess_kwargs() -> dict:
    """Suppress console flash on Windows."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    return kwargs


def run(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a command, wait for completion. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
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
    """
    Run a command and show a live spinner with elapsed time and the last
    meaningful stderr line from the process.  Prevents silent hanging on
    large or badly corrupt files.

    The spinner overwrites a single terminal line using \\r so log output
    above it is preserved.  On completion the line is cleared.

    Returns (returncode, stdout, stderr) — same interface as run().
    """
    import threading

    SPINNER    = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    spin_idx   = 0
    last_line  = [""]
    stdout_buf: List[str] = []
    stderr_buf: List[str] = []

    def _drain(stream, buf: List[str], is_stderr: bool):
        for raw in stream:
            line = raw.rstrip()
            buf.append(line)
            if is_stderr and line and not line.startswith("  "):
                last_line[0] = line[:80]
        stream.close()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            **_subprocess_kwargs())
    except FileNotFoundError as e:
        return -1, "", str(e)

    t_out = threading.Thread(target=_drain,
                             args=(proc.stdout, stdout_buf, False), daemon=True)
    t_err = threading.Thread(target=_drain,
                             args=(proc.stderr, stderr_buf, True),  daemon=True)
    t_out.start()
    t_err.start()

    t_start   = time.perf_counter()
    timed_out = False

    col_spin = "\033[36m" if use_colour else ""
    col_dim  = "\033[2m"  if use_colour else ""
    rst      = "\033[0m"  if use_colour else ""

    while proc.poll() is None:
        elapsed = time.perf_counter() - t_start
        if elapsed > timeout:
            proc.kill()
            timed_out = True
            break

        spin  = SPINNER[spin_idx % len(SPINNER)] if use_colour else "-"
        spin_idx += 1

        last  = last_line[0]
        if len(last) > 52:
            last = last[:49] + "..."

        line = (f"  {col_spin}{spin}{rst}  "
                f"{col_dim}{label:<22}{rst}  "
                f"{col_dim}{elapsed:>5.1f}s{rst}  "
                f"{col_dim}{last}{rst}")
        print(f"\r{line:<78}", end="", flush=True)
        time.sleep(0.12)

    t_out.join(timeout=2)
    t_err.join(timeout=2)
    print(f"\r{' ' * 80}\r", end="", flush=True)   # clear spinner line

    if timed_out:
        return -1, "", f"Timed out after {timeout}s"

    return proc.returncode, "\n".join(stdout_buf), "\n".join(stderr_buf)


def size_scaled_timeout(path: Path, base: int = 60,
                        bps: int = 20 * 1024 * 1024) -> int:
    """
    Timeout scaled to file size assuming ~20 MB/s decode throughput.
    Floor = base seconds.  Ceiling = 3600s.

      500 MB →  60s (floor)
        2 GB → 160s
       20 GB → 1000s
    """
    try:
        size = path.stat().st_size
    except OSError:
        return base
    return min(max(base, size // bps), 3600)


def ffprobe_json(path: Path, use_colour: bool = False) -> Optional[dict]:
    if not TOOLS.ffprobe:
        return None
    cmd = [
        TOOLS.ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_error",
        str(path)
    ]
    timeout = size_scaled_timeout(path, base=30)
    rc, out, err = run_watched(cmd, label="ffprobe", timeout=timeout,
                               use_colour=use_colour)
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
                f"Missing EBML header — not a valid Matroska file "
                f"(got {raw[:4].hex()})"))
    elif ext in AVI_EXTS:
        if raw[:4] != b"RIFF" or raw[8:12] != b"AVI ":
            issues.append(Issue("error", "avi_bad_signature",
                "Missing RIFF/AVI signature"))
    elif ext in {".wmv", ".asf"}:
        # ASF/WMV: GUID header 30 26 B2 75 8E 66 CF 11 A6 D9 00 AA 00 62 CE 6C
        if raw[:4] != b"\x30\x26\xb2\x75":
            issues.append(Issue("warning", "wmv_bad_signature",
                "Missing ASF/WMV GUID header — file may not be a valid WMV"))
    return issues


def check_mp4_moov_atom(path: Path) -> List[Issue]:
    """Scan for moov atom position — reads only head and tail, safe on huge files."""
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
            "moov atom not found — recording likely interrupted before finalisation. "
            "Repair will attempt remux with -movflags faststart"))
    elif moov_in_tail and not moov_in_head:
        issues.append(Issue("warning", "mp4_moov_at_end",
            "moov atom is at end of file (not streamable). "
            "Repair will relocate it to the front"))
    if has_mdat and not has_moov:
        issues.append(Issue("warning", "mp4_mdat_only",
            "mdat block found but no moov index — partial recovery may be possible"))
    return issues


def check_via_ffprobe(path: Path, use_colour: bool = False) -> List[Issue]:
    issues = []
    data = ffprobe_json(path, use_colour=use_colour)

    if data is None:
        issues.append(Issue("error", "ffprobe_failed",
            "ffprobe could not read file — likely corrupt or unrecognised format"))
        return issues

    if "error" in data:
        msg = data["error"].get("string", "unknown error")
        issues.append(Issue("error", "ffprobe_error", f"ffprobe: {msg}"))
        return issues

    fmt     = data.get("format", {})
    streams = data.get("streams", [])

    if not streams:
        issues.append(Issue("error", "no_streams",
            "File contains no audio or video streams"))
        return issues

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        issues.append(Issue("warning", "no_video_stream",
            "No video stream found — audio-only or stripped container"))

    duration = float(fmt.get("duration", 0) or 0)
    if duration == 0:
        issues.append(Issue("error", "zero_duration",
            "Duration is 0 — container metadata may be missing or corrupt"))
    elif duration < 0:
        issues.append(Issue("error", "negative_duration",
            f"Negative duration ({duration:.2f}s) — corrupt metadata"))

    bit_rate = int(fmt.get("bit_rate", 0) or 0)
    if bit_rate == 0 and duration > 0:
        issues.append(Issue("warning", "zero_bitrate",
            "Bitrate reported as 0 — index may be missing"))

    for vs in video_streams:
        codec  = vs.get("codec_name", "unknown")
        width  = vs.get("width", 0)
        height = vs.get("height", 0)
        if codec in ("none", "unknown"):
            issues.append(Issue("error", "unknown_video_codec",
                f"Video codec not identified: '{codec}'"))
        if width == 0 or height == 0:
            issues.append(Issue("error", "zero_dimensions",
                f"Video dimensions {width}x{height} — stream header corrupt"))
        r_frame_rate = vs.get("r_frame_rate", "0/1")
        try:
            num, den = map(int, r_frame_rate.split("/"))
            fps = num / den if den else 0
            if fps > 240:
                issues.append(Issue("warning", "unusual_fps",
                    f"Unusually high frame rate: {fps:.1f} fps — metadata may be corrupt"))
        except Exception:
            pass

    if path.suffix.lower() in MP4_EXTS:
        issues.extend(check_mp4_moov_atom(path))

    return issues


def check_via_mkvmerge(path: Path, use_colour: bool = False) -> List[Issue]:
    issues = []
    if not TOOLS.has_mkvtools:
        return issues
    cmd = [TOOLS.mkvmerge, "--identify",
           "--identification-format", "json", str(path)]
    timeout = size_scaled_timeout(path, base=30)
    rc, out, err = run_watched(cmd, label="mkvmerge identify", timeout=timeout,
                               use_colour=use_colour)
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
    """
    Decode-probe the full file, streaming stderr live so the user can see
    progress.  Timeout is scaled to file size — a 20 GB file gets up to
    1000s, a 500 MB file gets the 60s floor.
    """
    issues = []
    if not TOOLS.ffmpeg:
        return issues
    cmd = [TOOLS.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"]
    timeout = size_scaled_timeout(path, base=60)
    rc, out, err = run_watched(cmd, label="ffmpeg decode-probe", timeout=timeout,
                               use_colour=use_colour)
    if rc == -1 and "Timed out" in err:
        issues.append(Issue("warning", "decode_probe_timeout",
            f"Decode probe timed out after {timeout}s — file may be very large "
            f"or severely corrupt. Copy will still be attempted."))
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
    """Stream-copy remux — lossless, fixes most index/container issues."""
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy", "-map", "0",
        str(dst)
    ]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="remux (stream copy)", timeout=timeout,
                             use_colour=use_colour)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "Stream-copy remux succeeded (lossless)"
    return False, f"Remux failed (rc={rc}): {err.strip()[-200:]}"


def repair_mp4_faststart(src: Path, dst: Path, use_colour: bool = True) -> Tuple[bool, str]:
    """Remux MP4 with moov relocated to the front of the file."""
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy", "-map", "0",
        "-movflags", "+faststart",
        str(dst)
    ]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="MP4 moov faststart", timeout=timeout,
                             use_colour=use_colour)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "MP4 moov faststart relocation succeeded"
    return False, f"Faststart failed (rc={rc}): {err.strip()[-200:]}"


def repair_mkv_mkvmerge(src: Path, dst: Path, use_colour: bool = True) -> Tuple[bool, str]:
    """Rebuild MKV structure using mkvmerge."""
    if not TOOLS.has_mkvtools:
        return False, ("mkvmerge not available — install via: "
                       "winget install MKVToolNix")
    cmd = [TOOLS.mkvmerge, "--no-global-tags", "-o", str(dst), str(src)]
    timeout = size_scaled_timeout(src)
    rc, _, err = run_watched(cmd, label="mkvmerge remux", timeout=timeout,
                             use_colour=use_colour)
    if rc in (0, 1) and dst.exists() and dst.stat().st_size > 100:
        note = "MKV remux via mkvmerge succeeded"
        if rc == 1:
            note += " (with warnings)"
        return True, note
    return False, f"mkvmerge failed (rc={rc}): {err.strip()[-200:]}"


def repair_reencode(src: Path, dst: Path,
                    use_hwaccel: bool = True,
                    use_colour: bool = True) -> Tuple[bool, str]:
    """
    Re-encode using the best available Windows GPU encoder, falling back
    through NVENC → AMF → QSV → libx264.
    """
    global HW
    if HW is None:
        HW = detect_hw_encoders()

    encoder = HW.best if (use_hwaccel and HW) else None

    if encoder == "h264_nvenc":
        codec_args = ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "18", "-preset", "p4"]
        method = "Nvidia NVENC HW H.264"
    elif encoder == "h264_amf":
        codec_args = ["-c:v", "h264_amf", "-quality", "quality"]
        method = "AMD AMF HW H.264"
    elif encoder == "h264_qsv":
        codec_args = ["-c:v", "h264_qsv", "-global_quality", "18", "-preset", "medium"]
        method = "Intel QuickSync HW H.264"
    else:
        codec_args = ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
        method = "libx264 SW H.264"

    timeout = size_scaled_timeout(src)
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        *codec_args,
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst)
    ]
    rc, _, err = run_watched(cmd, label=f"re-encode ({method[:14]})", timeout=timeout,
                             use_colour=use_colour)

    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, f"Re-encode succeeded using {method}"

    # GPU encoder failed — fall back to software
    if encoder:
        cmd_sw = [
            TOOLS.ffmpeg, "-y",
            "-err_detect", "ignore_err",
            "-i", str(src),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(dst)
        ]
        rc2, _, err2 = run_watched(cmd_sw, label="re-encode (libx264 SW)",
                                   timeout=timeout, use_colour=use_colour)
        if rc2 == 0 and dst.exists() and dst.stat().st_size > 100:
            return True, f"Re-encode succeeded using libx264 SW (GPU fallback after {method} failed)"

    return False, f"Re-encode failed (rc={rc}): {err.strip()[-200:]}"


class VideoRepairEngine:

    def __init__(self, files: List[Path], dst: Path, dry_run: bool,
                 verbose: bool, ext_filter: Optional[Set[str]],
                 allow_reencode: bool, use_hwaccel: bool,
                 resume: bool = False,
                 sources_label: str = ""):
        self.files         = files
        self.dst           = dst
        self.dry_run       = dry_run
        self.verbose       = verbose
        self.ext_filter    = ext_filter
        self.allow_reencode = allow_reencode
        self.use_hwaccel   = use_hwaccel
        self.sources_label = sources_label
        self.resume         = resume
        self.results: List[FileResult] = []
        self._log_path: Optional[Path] = None
        self._common_root: Optional[Path] = None
        self._run_ts        = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._journal: Optional[RunJournal] = None
        self._setup_logging()

    def _setup_logging(self):
        self._logger = logging.getLogger("video_repair_mac")
        self._logger.setLevel(logging.DEBUG)
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.WARNING)
        self._logger.addHandler(sh)

    def _attach_file_log(self):
        if self.dry_run:
            return
        self._log_path = self.dst / "video_repair.log"
        fh = logging.FileHandler(self._log_path, encoding="utf-8", mode="a")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        self._logger.addHandler(fh)

    def _p(self, msg: str, end="\n"):
        print(msg, end=end, flush=True)

    def _header(self):
        w = 72
        arch = "Apple Silicon" if APPLE_SILICON else "Intel"
        self._p(c("─" * w, DIM))
        self._p(c(f"  VIDEO COPY + REPAIR UTILITY  ·  macOS ({arch})", BOLD))
        self._p(c(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}", DIM))
        self._p(c("─" * w, DIM))
        label_lines = self.sources_label.splitlines() if self.sources_label else ["(none)"]
        self._p(f"  Source  : {c(label_lines[0], CYAN)}")
        for ln in label_lines[1:]:
            self._p(f"            {c(ln, CYAN)}")
        self._p(f"  Dest    : {c(str(self.dst), CYAN)}")
        if self.dry_run:
            self._p(f"  Mode    : {c('DRY RUN — no files will be written', YELLOW)}")
        if not self.allow_reencode:
            self._p(f"  Mode    : {c('Re-encode disabled (--no-reencode)', YELLOW)}")
        if self.use_hwaccel and HAS_VTB:
            self._p(f"  HW Accel: {c('VideoToolbox available — will use for re-encode', GREEN)}")
        elif self.use_hwaccel and not HAS_VTB:
            self._p(f"  HW Accel: {c('VideoToolbox not available — using libx264 SW', YELLOW)}")
        else:
            self._p(f"  HW Accel: {c('Disabled (--no-hwaccel)', DIM)}")
        self._p(c("─" * w, DIM))
        self._p(c("  Tool availability:", DIM))
        for line in TOOLS.report():
            self._p(line)
        self._p(c("─" * w, DIM))

    def _footer(self):
        ok      = sum(1 for r in self.results if r.status == "ok")
        rep     = sum(1 for r in self.results if r.status == "repaired")
        partial = sum(1 for r in self.results if r.status == "partial")
        failed  = sum(1 for r in self.results if r.status in ("failed", "skipped"))
        total   = len(self.results)
        elapsed = sum(r.elapsed for r in self.results)
        w = 72
        self._p(c("─" * w, DIM))
        self._p(c("  SUMMARY", BOLD))
        self._p(c("─" * w, DIM))
        self._p(f"  {'Total files':<28} {total}")
        self._p(f"  {'Copied OK (no issues)':<28} {c(str(ok), GREEN)}")
        self._p(f"  {'Repaired + copied':<28} {c(str(rep), BLUE)}")
        self._p(f"  {'Partial recovery':<28} {c(str(partial), YELLOW)}")
        self._p(f"  {'Failed / skipped':<28} {c(str(failed), RED)}")
        self._p(f"  {'Time elapsed':<28} {elapsed:.1f}s")
        if self._log_path:
            self._p(f"  {'Log':<28} {self._log_path}")
        self._p(c("─" * w, DIM))
        if failed or partial:
            self._p(c("\n  ⚠  Files marked FAILED or PARTIAL need manual inspection.", YELLOW))
        else:
            self._p(c("\n  ✓  All files processed successfully.", GREEN))
        self._p("")

    def _file_line(self, idx: int, total: int, result: FileResult):
        prog  = bar(idx, total)
        pct   = f"{idx}/{total}"
        rel   = result.rel
        if len(rel) > 38: rel = "…" + rel[-37:]
        status_map = {
            "ok":       c("  OK      ", GREEN),
            "repaired": c("  REPAIRED", BLUE),
            "partial":  c("  PARTIAL ", YELLOW),
            "failed":   c("  FAILED  ", RED),
            "skipped":  c("  SKIPPED ", RED),
            "done":     c("  done    ", DIM),
        }
        st    = status_map.get(result.status, result.status)
        size  = human_size(result.size_bytes)
        strat = c(f" [{result.repair_strategy}]", MAGENTA) if result.repair_strategy else ""
        self._p(f"  {c(prog, DIM)} {c(pct, DIM):>8}  {st}  {rel:<38}  {c(size, DIM)}{strat}")

    def _issue_lines(self, result: FileResult):
        for iss in result.issues:
            icon   = "⚠" if iss.severity == "warning" else "✕"
            colour = YELLOW if iss.severity == "warning" else RED
            self._p(f"               {c(icon, colour)} {iss.code}: {iss.description}")
            if iss.repair_note:
                tag = "↻ repaired" if iss.repaired else "→"
                col = GREEN if iss.repaired else YELLOW
                self._p(f"                 {c(tag, col)} {iss.repair_note}")

    def _rel(self, path: Path) -> str:
        if self._common_root is None:
            parents = [p.parent for p in self.files]
            try:
                self._common_root = Path(os.path.commonpath(parents)) if parents else Path("/")
            except ValueError:
                self._common_root = Path("/")
        try:
            return str(path.relative_to(self._common_root))
        except ValueError:
            return path.name

    def _inspect(self, path: Path) -> List[Issue]:
        issues = []
        if path.stat().st_size == 0:
            issues.append(Issue("error", "zero_byte", "File is zero bytes"))
            return issues
        issues += check_container_signature(path)
        issues += check_via_ffprobe(path)
        if path.suffix.lower() in MKV_EXTS:
            issues += check_via_mkvmerge(path)
        if any(i.severity == "error" for i in issues):
            sig_err = any(i.code in ("mkv_bad_ebml", "avi_bad_signature") for i in issues)
            if not sig_err:
                issues += check_ffmpeg_decode(path)
        return issues

    def run(self):
        global HAS_VTB
        self._header()

        if not TOOLS.check_required():
            self._p(c("\n  Error: ffmpeg and ffprobe are required.", RED))
            self._p(c("  Install with:", YELLOW))
            self._p(c("    brew install ffmpeg", CYAN))
            if APPLE_SILICON:
                self._p(c("  If brew is not found: add /opt/homebrew/bin to your PATH", YELLOW))
                self._p(c("    echo 'export PATH=\"/opt/homebrew/bin:$PATH\"' >> ~/.zshrc", CYAN))
            sys.exit(1)

        # Resolve VideoToolbox after confirming ffmpeg is present
        if self.use_hwaccel:
            HAS_VTB = has_videotoolbox()

        if not self.dry_run:
            self.dst.mkdir(parents=True, exist_ok=True)
            self._attach_file_log()
            loaded = self._init_journal()
        else:
            loaded = 0

        total = len(self.files)
        if total == 0:
            self._p(c("  No video files found matching criteria.", YELLOW))
            return

        self._p(f"  Found {c(str(total), BOLD)} video file(s) to process.\n")

        seen_rels: dict[str, int] = {}

        for idx, path in enumerate(self.files, 1):
            t0  = time.perf_counter()
            rel = self._rel(path)

            if rel in seen_rels:
                seen_rels[rel] += 1
                stem = Path(rel).stem
                suf  = Path(rel).suffix
                rel  = f"{stem}_{seen_rels[rel]}{suf}"
            else:
                seen_rels[rel] = 0

            size   = path.stat().st_size
            result = FileResult(path=path, rel=rel, size_bytes=size)

            # ── Resume check — inspect destination before deciding to skip ────────
            if self.resume and not self.dry_run:
                dst_path = self.dst / rel
                verdict, detail = self._check_destination(path, dst_path)

                if verdict == self._DST_CLEAN:
                    # Destination is healthy — skip entirely
                    result.status      = "done"
                    result.skip_reason = detail
                    result.elapsed     = time.perf_counter() - t0
                    self.results.append(result)
                    self._file_line(idx, total, result)
                    if self.verbose:
                        self._p(f"               {c('-', DIM)} {detail}")
                    self._logger.info(f"DONE       {path}  ({detail})")
                    if self._journal:
                        self._journal.record(result)
                    continue

                elif verdict == self._DST_WARN:
                    # Destination has warnings — skip but tell the user
                    result.status      = "done"
                    result.skip_reason = detail
                    result.elapsed     = time.perf_counter() - t0
                    self.results.append(result)
                    self._file_line(idx, total, result)
                    self._p(f"               {c('!', YELLOW)} {detail}")
                    self._logger.warning(f"DONE/WARN  {path}  ({detail})")
                    if self._journal:
                        self._journal.record(result)
                    continue

                elif verdict == self._DST_CORRUPT:
                    # Destination is corrupt — fall through to re-copy from src
                    self._p(f"               {c('X', RED)} {detail}")
                    self._logger.warning(f"RECOPY     {path}  ({detail})")
                    # Remove the corrupt destination so repair logic writes fresh
                    try:
                        dst_path.unlink()
                    except OSError:
                        pass

                elif verdict == self._DST_TRUNCATED:
                    # Destination is truncated — fall through to re-copy
                    self._p(f"               {c('!', YELLOW)} {detail}")
                    self._logger.warning(f"TRUNCATED  {path}  ({detail})")
                    try:
                        dst_path.unlink()
                    except OSError:
                        pass

                # _DST_MISSING → fall through silently to normal processing

            issues = self._inspect(path)
            result.issues = issues
            has_errors   = any(i.severity == "error"   for i in issues)
            has_warnings = any(i.severity == "warning" for i in issues)

            if not self.dry_run:
                zero_byte = any(i.code == "zero_byte" for i in issues)
                if zero_byte:
                    result.status = "skipped"
                    self._logger.warning(f"SKIPPED {rel} — zero byte")
                else:
                    out_path, result.issues = attempt_video_repair(
                        path, self.dst, rel, result.issues,
                        allow_reencode=self.allow_reencode,
                        use_hwaccel=True, use_colour=True)

                    if out_path and out_path.exists():
                        result.sha256 = sha256_of(out_path)
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
                    f"{result.status.upper():10} {path}  →  {rel}  "
                    f"sha256={result.sha256[:16] or '—'}…"
                    + (f"  issues={len(issues)}" if issues else ""))
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

        self._p("")
        self._footer()

        if self._journal and not self.dry_run:
            self._journal.consolidate()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Copy and repair video files — macOS version.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("sources", nargs="+", metavar="SOURCE",
        help="Files, directories, or glob patterns. Last argument = destination.")
    ap.add_argument("--dry-run",      action="store_true")
    ap.add_argument("--verbose",      action="store_true")
    ap.add_argument("--ext",          type=str, default=None)
    ap.add_argument("--list",         type=Path, default=None, metavar="FILE")
    ap.add_argument("--resume",       action="store_true",
        help="Skip files already in the destination at >= 95%% of source size")
    ap.add_argument("--no-reencode",  action="store_true")
    ap.add_argument("--no-hwaccel",   action="store_true",
        help="Disable VideoToolbox hardware acceleration (use libx264 SW instead)")
    ap.add_argument("--recursive",    action="store_true", default=True)
    ap.add_argument("--no-recursive", dest="recursive", action="store_false")
    args = ap.parse_args()

    *raw_sources, destination = args.sources
    dst = Path(destination).expanduser().resolve()

    source_strings: List[str] = list(raw_sources)
    if args.list:
        if not args.list.is_file():
            print(c(f"  Error: list file '{args.list}' not found.", RED))
            sys.exit(1)
        source_strings += load_list_file(args.list)

    if not source_strings:
        print(c("  Error: no source files specified.", RED))
        sys.exit(1)

    ext_filter: Optional[Set[str]] = None
    if args.ext:
        ext_filter = {e if e.startswith(".") else "." + e for e in args.ext.split(",")}
    else:
        ext_filter = ALL_VIDEO_EXTS

    files, errors = resolve_inputs(source_strings, args.recursive, ext_filter)
    for e in errors:
        print(c(f"  Warning: {e}", YELLOW))

    if len(source_strings) == 1:
        label = source_strings[0]
    elif len(source_strings) <= 4:
        label = "\n".join(source_strings)
    else:
        label = "\n".join(source_strings[:3]) + f"\n  … and {len(source_strings)-3} more"

    VideoRepairEngine(
        files         = files,
        dst           = dst,
        dry_run       = args.dry_run,
        verbose       = args.verbose,
        ext_filter    = ext_filter,
        allow_reencode= not args.no_reencode,
        resume        = args.resume,
        use_hwaccel   = not args.no_hwaccel,
        sources_label = label,
    ).run()


if __name__ == "__main__":
    main()
