#!/usr/bin/env python3
r"""
video_copy_repair_windows.py
----------------------------
Copy video files from one or more sources to a destination, inspecting each
file for common integrity issues and attempting repairs before writing.

PLATFORM: Windows 10 / 11 (native — cmd.exe, PowerShell, or Windows Terminal)
          Also works under Git Bash, though Windows Terminal is recommended
          for best colour output.

=== Required tools — install ONE of these ways ===

  Option A — winget (built into Windows 10 2004+ / Windows 11):
    winget install "FFmpeg (Essentials Build)"
    winget install MKVToolNix

  Option B — Chocolatey (https://chocolatey.org):
    choco install ffmpeg
    choco install mkvtoolnix

  Option C — Scoop (https://scoop.sh):
    scoop install ffmpeg
    scoop install mkvtoolnix

  Option D — Manual:
    Download ffmpeg from https://www.gyan.dev/ffmpeg/builds/
    Extract to C:\\ffmpeg\\ and add C:\\ffmpeg\\bin to your PATH.
    Download MKVToolNix installer from https://mkvtoolnix.download/

  Verify your install:
    ffmpeg -version
    ffprobe -version
    mkvmerge --version

=== Optional Python package ===
    pip install pymkv2

=== Supported formats and checks ===
  MP4 / MOV / M4V / 3GP   — ffprobe metadata, moov atom detection,
                             stream validity, duration sanity
  MKV / WEBM               — ffprobe + mkvmerge --identify, EBML header
  AVI / WMV / FLV / TS     — ffprobe metadata and stream checks
  All video                 — container signature, zero-byte, SHA-256

=== Repair strategies (least destructive first) ===
  1. Remux (stream copy)    — rebuilds container, no quality loss
  2. Moov faststart         — relocates moov atom to front of MP4/MOV
  3. MKV remux via mkvmerge — rebuilds Matroska structure cleanly
  4. Re-encode (last resort) — hardware accelerated where available:
       NVENC   (Nvidia GPU)
       AMF     (AMD GPU)
       QSV     (Intel QuickSync)
       libx264 (software fallback)

=== Windows-specific notes ===
  - Unlike Linux/macOS shells, cmd.exe and PowerShell do NOT expand
    wildcards. Globs like *.mp4 are passed as literal strings to Python,
    which this script handles correctly.
  - UNC paths (\\server\share\...) are supported.
  - Drive-letter paths (C:\, D:\) and forward-slash variants both work.
  - NTFS is case-insensitive — duplicate detection uses lowercased keys.
  - ANSI colour requires Windows 10 version 1511+ or Windows Terminal.
    The script enables VT processing automatically via SetConsoleMode().
    If colours look wrong, run in Windows Terminal or add --no-colour.

=== Input modes ===
  Directory   video_copy_repair_windows.py C:\Videos\ D:\Backup\
  Single file video_copy_repair_windows.py clip.mp4 D:\Backup\
  Glob        video_copy_repair_windows.py "C:\Videos\*.mkv" D:\Backup\
  Multiple    video_copy_repair_windows.py a.mp4 b.mkv "*.avi" D:\Backup\
  UNC path    video_copy_repair_windows.py \\\\NAS\\share\\*.mp4 D:\Backup\
  List file   video_copy_repair_windows.py --list files.txt D:\Backup\

=== Options ===
  --dry-run        Probe and report without writing any files
  --verbose        Show details for every file, not just problems
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
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── ANSI colour — enable VT processing on Windows ─────────────────────────────
#
# Windows 10 v1511+ supports ANSI escape codes, but the console must opt in
# via SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING).  We do this once at
# import time.  If it fails (older Windows, redirected output) we fall back to
# plain text automatically.

_COLOUR_ENABLED = False

def _enable_windows_ansi() -> bool:
    """Enable VT100 processing on the Windows console. Returns True on success."""
    try:
        import ctypes, ctypes.wintypes
        kernel32 = ctypes.windll.kernel32
        # Get current stdout console mode
        handle = kernel32.GetStdHandle(-11)   # STD_OUTPUT_HANDLE
        mode   = ctypes.wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            return False
        return True
    except Exception:
        return False

if sys.platform == "win32":
    _COLOUR_ENABLED = _enable_windows_ansi()
else:
    # Running under Git Bash / WSL / CI — ANSI works natively
    _COLOUR_ENABLED = True

# Colour codes — only emitted when colour is enabled
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

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Issue:
    severity: str          # "warning" | "error"
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
    status: str = "pending"   # pending|ok|repaired|partial|failed|skipped|done
    repair_strategy: str = ""
    elapsed: float = 0.0
    skip_reason: str = ""     # populated when status == "done" (already exists)

# ── Windows tool discovery ─────────────────────────────────────────────────────
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

    def report(self, use_colour: bool = True) -> List[str]:
        lines = []
        win_ver = platform.version()
        lines.append(f"    {c('Windows', CYAN, use_colour)}  {platform.release()}  "
                     f"({platform.machine()})  build {win_ver.split('.')[-1][:5]}")
        for tool in self.REQUIRED:
            p = self.paths[tool]
            tag  = c("✓", GREEN, use_colour) if p else c("✕ MISSING", RED, use_colour)
            hint = ""
            if not p:
                hint = c('  →  winget install "FFmpeg (Essentials Build)"',
                         YELLOW, use_colour)
            lines.append(f"    {tag}  {tool:<16} {p or ''}{hint}")
        for tool in self.OPTIONAL:
            p   = self.paths[tool]
            tag = c("✓", GREEN, use_colour) if p else c("○ optional", DIM, use_colour)
            hint = ""
            if not p:
                hint = c("  →  winget install MKVToolNix", DIM, use_colour)
            lines.append(f"    {tag}  {tool:<16} {p or ''}{hint}")
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

def run(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a command. Returns (returncode, stdout, stderr)."""
    try:
        # On Windows, CREATE_NO_WINDOW prevents console flicker from subprocesses
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, **kwargs)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", str(e)


def ffprobe_json(path: Path) -> Optional[dict]:
    if not TOOLS.ffprobe:
        return None
    cmd = [
        TOOLS.ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_error",
        str(path)
    ]
    rc, out, err = run(cmd, timeout=60)
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


def check_via_ffprobe(path: Path) -> List[Issue]:
    issues = []
    data = ffprobe_json(path)

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


def check_via_mkvmerge(path: Path) -> List[Issue]:
    issues = []
    if not TOOLS.has_mkvtools:
        return issues
    cmd = [TOOLS.mkvmerge, "--identify",
           "--identification-format", "json", str(path)]
    rc, out, err = run(cmd, timeout=30)
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


def check_ffmpeg_decode(path: Path) -> List[Issue]:
    """Decode-probe the full file, catching corrupt packets silently."""
    issues = []
    if not TOOLS.ffmpeg:
        return issues
    cmd = [TOOLS.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"]
    rc, out, err = run(cmd, timeout=300)
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

def repair_remux(src: Path, dst: Path) -> Tuple[bool, str]:
    """Stream-copy remux — lossless, fixes most index/container issues."""
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy", "-map", "0",
        str(dst)
    ]
    rc, _, err = run(cmd, timeout=600)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "Stream-copy remux succeeded (lossless)"
    return False, f"Remux failed (rc={rc}): {err.strip()[-200:]}"


def repair_mp4_faststart(src: Path, dst: Path) -> Tuple[bool, str]:
    """Remux MP4 with moov relocated to the front of the file."""
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy", "-map", "0",
        "-movflags", "+faststart",
        str(dst)
    ]
    rc, _, err = run(cmd, timeout=600)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "MP4 moov faststart relocation succeeded"
    return False, f"Faststart failed (rc={rc}): {err.strip()[-200:]}"


def repair_mkv_mkvmerge(src: Path, dst: Path) -> Tuple[bool, str]:
    """Rebuild MKV structure using mkvmerge."""
    if not TOOLS.has_mkvtools:
        return False, ("mkvmerge not available — install via: "
                       "winget install MKVToolNix")
    cmd = [TOOLS.mkvmerge, "--no-global-tags", "-o", str(dst), str(src)]
    rc, _, err = run(cmd, timeout=600)
    if rc in (0, 1) and dst.exists() and dst.stat().st_size > 100:
        note = "MKV remux via mkvmerge succeeded"
        if rc == 1:
            note += " (with warnings)"
        return True, note
    return False, f"mkvmerge failed (rc={rc}): {err.strip()[-200:]}"


def repair_reencode(src: Path, dst: Path,
                    use_hwaccel: bool = True) -> Tuple[bool, str]:
    """
    Re-encode using the best available Windows GPU encoder, falling back
    through NVENC → AMF → QSV → libx264.

    Encoder-specific quality flags:
      NVENC  — -rc vbr -cq 18   (constant quality mode)
      AMF    — -quality quality  (quality preset)
      QSV    — -global_quality 18
      libx264— -crf 18
    """
    global HW
    if HW is None:
        HW = detect_hw_encoders()

    encoder = HW.best if (use_hwaccel and HW) else None

    if encoder == "h264_nvenc":
        codec_args = ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "18",
                      "-preset", "p4"]
        method = "Nvidia NVENC HW H.264"
    elif encoder == "h264_amf":
        codec_args = ["-c:v", "h264_amf", "-quality", "quality"]
        method = "AMD AMF HW H.264"
    elif encoder == "h264_qsv":
        codec_args = ["-c:v", "h264_qsv", "-global_quality", "18",
                      "-preset", "medium"]
        method = "Intel QuickSync HW H.264"
    else:
        codec_args = ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
        method = "libx264 SW H.264"

    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        *codec_args,
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst)
    ]
    rc, _, err = run(cmd, timeout=1800)

    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, f"Re-encode succeeded using {method}"

    # If a GPU encoder failed, fall back to software
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
        rc2, _, err2 = run(cmd_sw, timeout=1800)
        if rc2 == 0 and dst.exists() and dst.stat().st_size > 100:
            return True, f"Re-encode succeeded using libx264 SW (GPU fallback after {method} failed)"

    return False, f"Re-encode failed (rc={rc}): {err.strip()[-200:]}"


def attempt_video_repair(
        src: Path, dst_dir: Path, rel: str,
        issues: List[Issue],
        allow_reencode: bool,
        use_hwaccel: bool) -> Tuple[Optional[Path], List[Issue]]:
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
        strategies.append(("faststart", repair_mp4_faststart))

    strategies.append(("remux", repair_remux))

    if ext in MKV_EXTS and TOOLS.has_mkvtools:
        strategies.append(("mkvmerge", repair_mkv_mkvmerge))

    if allow_reencode:
        strategies.append(("reencode",
                            lambda s, d: repair_reencode(s, d, use_hwaccel)))

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
    fill_ch = "█" if _COLOUR_ENABLED else "#"
    rest_ch = "─" if _COLOUR_ENABLED else "-"
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
        self._log_path: Optional[Path] = None
        self._common_root: Optional[Path] = None
        self._setup_logging()

    # ── logging ───────────────────────────────────────────────────────────────

    def _setup_logging(self):
        self._logger = logging.getLogger("video_repair_win")
        self._logger.setLevel(logging.DEBUG)
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.WARNING)
        self._logger.addHandler(sh)

    def _attach_file_log(self):
        if self.dry_run:
            return
        self._log_path = self.dst / "video_repair.log"
        fh = logging.FileHandler(self._log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        self._logger.addHandler(fh)

    # ── resume / destination health check ─────────────────────────────────────

    # Verdict constants returned by _check_destination
    _DST_MISSING   = "missing"    # dst does not exist — copy normally
    _DST_TRUNCATED = "truncated"  # dst too small — interrupted write, re-copy
    _DST_CORRUPT   = "corrupt"    # dst fails inspection — re-copy from src
    _DST_WARN      = "warn"       # dst has warnings but no errors — skip w/ note
    _DST_CLEAN     = "clean"      # dst passes inspection — skip

    def _check_destination(self, src: Path, dst_path: Path
                           ) -> Tuple[str, str]:
        """
        Inspect the destination file and decide whether to skip or re-copy.

        Returns (verdict, detail_string) where verdict is one of the
        _DST_* constants above.

        Strategy
        --------
        1. Missing / zero-byte / truncated  →  copy (structural check)
        2. Size looks plausible (>= 95% of src)  →  run the full ffprobe +
           format-specific inspection against the DST file.
           - Any error-level issue    →  re-copy (corrupt destination)
           - Warning-level issues     →  skip but flag to user
           - Clean                    →  skip silently
        """
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
                f"({human_size(dst_size)} vs {human_size(src_size)}) "
                f"— likely an interrupted write")

        # Size looks plausible — run a full structural inspection on the DST
        issues = self._inspect(dst_path)

        errors   = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]

        if errors:
            codes = ", ".join(i.code for i in errors[:3])
            return self._DST_CORRUPT, (
                f"destination fails health check ({len(errors)} error(s): {codes}) "
                f"— will re-copy from source")

        size_note = (f"identical size ({human_size(dst_size)})"
                     if dst_size == src_size
                     else f"size ok ({human_size(dst_size)} / {human_size(src_size)})")

        if warnings:
            codes = ", ".join(i.code for i in warnings[:2])
            return self._DST_WARN, (
                f"{size_note} — {len(warnings)} warning(s) in destination: {codes}")

        return self._DST_CLEAN, f"healthy — {size_note}"

    # ── printing ──────────────────────────────────────────────────────────────

    def _p(self, msg: str, end: str = "\n"):
        print(msg, end=end, flush=True)

    def _c(self, text: str, colour: str) -> str:
        return c(text, colour, self.use_colour)

    def _header(self):
        global HW
        w = 72
        self._p(self._c("─" * w, DIM))
        self._p(self._c("  VIDEO COPY + REPAIR UTILITY  ·  Windows", BOLD))
        self._p(self._c(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}", DIM))
        self._p(self._c("─" * w, DIM))
        label_lines = self.sources_label.splitlines() if self.sources_label else ["(none)"]
        self._p(f"  Source  : {self._c(label_lines[0], CYAN)}")
        for ln in label_lines[1:]:
            self._p(f"            {self._c(ln, CYAN)}")
        self._p(f"  Dest    : {self._c(str(self.dst), CYAN)}")
        if self.dry_run:
            self._p(f"  Mode    : {self._c('DRY RUN — no files will be written', YELLOW)}")
        if self.resume:
            self._p(f"  Mode    : {self._c('RESUME — destination files will be health-checked before skipping', CYAN)}")
        if not self.allow_reencode:
            self._p(f"  Mode    : {self._c('Re-encode disabled (--no-reencode)', YELLOW)}")

        # Hardware encoder status
        if self.use_hwaccel and HW:
            if HW.best:
                self._p(f"  HW Accel: {self._c(f'{HW.label} available', GREEN)}")
            else:
                self._p(f"  HW Accel: {self._c('No GPU encoder found — using libx264 SW', YELLOW)}")
        else:
            self._p(f"  HW Accel: {self._c('Disabled (--no-hwaccel)', DIM)}")

        self._p(self._c("─" * w, DIM))
        self._p(self._c("  Tool availability:", DIM))
        for line in TOOLS.report(self.use_colour):
            self._p(line)
        self._p(self._c("─" * w, DIM))

    def _footer(self):
        ok      = sum(1 for r in self.results if r.status == "ok")
        rep     = sum(1 for r in self.results if r.status == "repaired")
        partial = sum(1 for r in self.results if r.status == "partial")
        failed  = sum(1 for r in self.results if r.status in ("failed", "skipped"))
        done    = sum(1 for r in self.results if r.status == "done")
        total   = len(self.results)
        elapsed = sum(r.elapsed for r in self.results)
        w = 72
        self._p(self._c("─" * w, DIM))
        self._p(self._c("  SUMMARY", BOLD))
        self._p(self._c("─" * w, DIM))
        self._p(f"  {'Total files':<28} {total}")
        if done:
            self._p(f"  {'Already copied (skipped)':<28} {self._c(str(done), DIM)}")
        self._p(f"  {'Copied OK (no issues)':<28} {self._c(str(ok), GREEN)}")
        self._p(f"  {'Repaired + copied':<28} {self._c(str(rep), BLUE)}")
        self._p(f"  {'Partial recovery':<28} {self._c(str(partial), YELLOW)}")
        self._p(f"  {'Failed / skipped':<28} {self._c(str(failed), RED)}")
        self._p(f"  {'Time elapsed':<28} {elapsed:.1f}s")
        if self._log_path:
            self._p(f"  {'Log':<28} {self._log_path}")
        self._p(self._c("─" * w, DIM))
        if failed or partial:
            self._p(self._c("\n  ⚠  Files marked FAILED or PARTIAL need manual inspection.", YELLOW))
        else:
            self._p(self._c("\n  ✓  All files processed successfully.", GREEN))
        self._p("")

    def _file_line(self, idx: int, total: int, result: FileResult):
        prog  = bar(idx, total)
        pct   = f"{idx}/{total}"
        rel   = result.rel
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
        strat = (self._c(f" [{result.repair_strategy}]", MAGENTA)
                 if result.repair_strategy else "")
        self._p(f"  {self._c(prog, DIM)} {self._c(pct, DIM):>8}  "
                f"{st}  {rel:<38}  {self._c(size, DIM)}{strat}")

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

    # ── relative path ─────────────────────────────────────────────────────────

    def _rel(self, path: Path) -> str:
        if self._common_root is None:
            parents = [p.parent for p in self.files]
            try:
                # os.path.commonpath handles drive letters and UNC paths correctly
                self._common_root = (
                    Path(os.path.commonpath([str(p) for p in parents]))
                    if parents else Path(".")
                )
            except ValueError:
                # Different drives — can't find common root
                self._common_root = Path(".")
        try:
            return str(path.relative_to(self._common_root))
        except ValueError:
            return path.name

    # ── inspect ───────────────────────────────────────────────────────────────

    def _inspect(self, path: Path) -> List[Issue]:
        issues = []
        try:
            size = path.stat().st_size
        except OSError as e:
            return [Issue("error", "stat_error", f"Cannot stat file: {e}")]

        if size == 0:
            return [Issue("error", "zero_byte", "File is zero bytes")]

        issues += check_container_signature(path)
        issues += check_via_ffprobe(path)

        if path.suffix.lower() in MKV_EXTS:
            issues += check_via_mkvmerge(path)

        # Full decode scan only if structural errors were found
        if any(i.severity == "error" for i in issues):
            sig_err = any(i.code in ("mkv_bad_ebml", "avi_bad_signature",
                                     "wmv_bad_signature") for i in issues)
            if not sig_err:
                issues += check_ffmpeg_decode(path)

        return issues

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self):
        global HW

        # Resolve hardware encoders early so header can report them
        if self.use_hwaccel and TOOLS.ffmpeg:
            HW = detect_hw_encoders()

        self._header()

        if not TOOLS.check_required():
            self._p(self._c("\n  Error: ffmpeg and ffprobe are required.", RED))
            self._p(self._c("  Install with one of:", YELLOW))
            self._p(self._c('    winget install "FFmpeg (Essentials Build)"', CYAN))
            self._p(self._c('    choco install ffmpeg', CYAN))
            self._p(self._c('    scoop install ffmpeg', CYAN))
            self._p(self._c("  Then add ffmpeg\\bin to your PATH.", YELLOW))
            sys.exit(1)

        if not self.dry_run:
            self.dst.mkdir(parents=True, exist_ok=True)
            self._attach_file_log()

        total = len(self.files)
        if total == 0:
            self._p(self._c("  No video files found matching criteria.", YELLOW))
            return

        self._p(f"  Found {self._c(str(total), BOLD)} video file(s) to process.\n")

        seen_rels: Dict[str, int] = {}

        for idx, path in enumerate(self.files, 1):
            t0  = time.perf_counter()
            rel = self._rel(path)

            # Collision handling for files from different dirs with same name
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
                        self._p(f"               {self._c('-', DIM)} {detail}")
                    self._logger.info(f"DONE       {path}  ({detail})")
                    continue

                elif verdict == self._DST_WARN:
                    # Destination has warnings — skip but tell the user
                    result.status      = "done"
                    result.skip_reason = detail
                    result.elapsed     = time.perf_counter() - t0
                    self.results.append(result)
                    self._file_line(idx, total, result)
                    self._p(f"               {self._c('!', YELLOW)} {detail}")
                    self._logger.warning(f"DONE/WARN  {path}  ({detail})")
                    continue

                elif verdict == self._DST_CORRUPT:
                    # Destination is corrupt — fall through to re-copy from src
                    self._p(f"               {self._c('X', RED)} {detail}")
                    self._logger.warning(f"RECOPY     {path}  ({detail})")
                    # Remove the corrupt destination so repair logic writes fresh
                    try:
                        dst_path.unlink()
                    except OSError:
                        pass

                elif verdict == self._DST_TRUNCATED:
                    # Destination is truncated — fall through to re-copy
                    self._p(f"               {self._c('!', YELLOW)} {detail}")
                    self._logger.warning(f"TRUNCATED  {path}  ({detail})")
                    try:
                        dst_path.unlink()
                    except OSError:
                        pass

                # _DST_MISSING → fall through silently to normal processing

            issues       = self._inspect(path)
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
                        self.allow_reencode, self.use_hwaccel)

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
                    f"{result.status.upper():10} {path}  ->  {rel}  "
                    f"sha256={result.sha256[:16] or '-'}..."
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

            show = has_errors or has_warnings or self.verbose
            self._file_line(idx, total, result)
            if show:
                self._issue_lines(result)

        self._p("")
        self._footer()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Copy and repair video files — Windows version.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("sources", nargs="+", metavar="SOURCE",
        help="Files, directories, or glob patterns. Last argument = destination.")
    ap.add_argument("--dry-run",      action="store_true",
        help="Probe and report without writing files")
    ap.add_argument("--verbose",      action="store_true",
        help="Show details for all files, not just problems")
    ap.add_argument("--ext",          type=str, default=None,
        help="Comma-separated extensions, e.g. .mp4,.mkv")
    ap.add_argument("--list",         type=Path, default=None, metavar="FILE",
        help="Text file of source paths/globs (one per line)")
    ap.add_argument("--no-reencode",  action="store_true",
        help="Never attempt re-encode as last resort")
    ap.add_argument("--resume",       action="store_true",
        help="Skip files that already exist in the destination at >= 95%% of source size")
    ap.add_argument("--no-hwaccel",   action="store_true",
        help="Force libx264 software encode (ignore GPU encoders)")
    ap.add_argument("--no-colour",    action="store_true",
        help="Disable ANSI colour output")
    ap.add_argument("--recursive",    action="store_true", default=True)
    ap.add_argument("--no-recursive", dest="recursive", action="store_false")
    args = ap.parse_args()

    # Override colour if requested
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
