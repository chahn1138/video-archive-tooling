#!/usr/bin/env python3
"""
video_copy_repair_linux.py
--------------------------
Copy video files from one or more sources to a destination, inspecting each
file for common integrity issues and attempting repairs before writing.

PLATFORM: Linux (Ubuntu / Debian / Fedora / Arch)

Required tools (install once):
    sudo apt install ffmpeg mkvtoolnix          # Debian / Ubuntu
    sudo dnf install ffmpeg mkvtoolnix          # Fedora
    sudo pacman -S ffmpeg mkvtoolnix            # Arch

Optional Python package (for MKV inspection):
    pip install pymkv2

Supported formats and checks
-----------------------------
  MP4 / MOV / M4V / 3GP   — ffprobe metadata, moov atom detection,
                             stream validity, duration sanity
  MKV / WEBM               — ffprobe + mkvmerge --identify, EBML header
  AVI / WMV / FLV / TS    — ffprobe metadata and stream checks
  All video                — container signature, zero-byte, SHA-256

Repair strategies (in order of preference — least destructive first)
----------------------------------------------------------------------
  1. Remux (stream copy)   — rebuilds container index, fixes moov/cues,
                             no quality loss, fastest
  2. Moov faststart        — relocates moov atom to front of MP4/MOV
  3. MKV remux via mkvmerge — rebuilds Matroska structure cleanly
  4. Re-encode (last resort)— full transcode; quality loss possible,
                              used only when remux fails entirely

Input modes
-----------
  Directory    video_copy_repair_linux.py /videos/ /backup/
  Single file  video_copy_repair_linux.py clip.mp4 /backup/
  Glob         video_copy_repair_linux.py "/nas/*.mkv" /backup/
  Multiple     video_copy_repair_linux.py a.mp4 b.mkv /nas/*.avi /backup/
  List file    video_copy_repair_linux.py --list files.txt /backup/

Options
-------
  --dry-run     Probe and report without writing any files
  --verbose     Show details for every file, not just problem files
  --ext         Comma-separated extensions to include, e.g. .mp4,.mkv
  --list FILE   Text file of source paths/globs (one per line, # = comment)
  --no-reencode Never attempt re-encode, even as last resort
  --recursive   Recurse into sub-directories when given a directory (default: on)
  --no-recursive  Do not recurse
"""

import argparse
import hashlib
import json
import logging
import os
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
RESET  = "\033[0m"; BOLD  = "\033[1m"; DIM   = "\033[2m"
GREEN  = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"
CYAN   = "\033[36m"; BLUE  = "\033[34m"; MAGENTA = "\033[35m"

def c(text, colour): return f"{colour}{text}{RESET}"

# ── Video format sets ──────────────────────────────────────────────────────────
MP4_EXTS  = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".m4a", ".f4v"}
MKV_EXTS  = {".mkv", ".webm", ".mka"}
AVI_EXTS  = {".avi"}
TS_EXTS   = {".ts", ".mts", ".m2ts", ".mxf"}
OTHER_EXTS = {".wmv", ".flv", ".asf", ".rm", ".rmvb", ".vob", ".mpg", ".mpeg",
              ".divx", ".ogv", ".ogg"}
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
    status: str = "pending"
    repair_strategy: str = ""
    elapsed: float = 0.0

# ── Tool discovery ─────────────────────────────────────────────────────────────

class ToolSet:
    """Discovers available external tools at startup and exposes their paths."""

    REQUIRED = ["ffmpeg", "ffprobe"]
    OPTIONAL = ["mkvmerge", "mkvinfo", "mkvpropedit"]

    def __init__(self):
        self.paths: Dict[str, Optional[str]] = {}
        self._discover()

    def _discover(self):
        for tool in self.REQUIRED + self.OPTIONAL:
            self.paths[tool] = shutil.which(tool)

    @property
    def ffmpeg(self) -> str:
        return self.paths["ffmpeg"]

    @property
    def ffprobe(self) -> str:
        return self.paths["ffprobe"]

    @property
    def mkvmerge(self) -> Optional[str]:
        return self.paths["mkvmerge"]

    @property
    def has_mkvtools(self) -> bool:
        return bool(self.paths.get("mkvmerge"))

    def report(self) -> List[str]:
        lines = []
        for tool in self.REQUIRED:
            p = self.paths[tool]
            tag = c("✓", GREEN) if p else c("✕ MISSING", RED)
            lines.append(f"    {tag}  {tool:<14} {p or '— required, please install ffmpeg'}")
        for tool in self.OPTIONAL:
            p = self.paths[tool]
            tag = c("✓", GREEN) if p else c("○ optional", DIM)
            lines.append(f"    {tag}  {tool:<14} {p or '— install mkvtoolnix for MKV repair'}")
        return lines

    def check_required(self) -> bool:
        return all(self.paths[t] for t in self.REQUIRED)


TOOLS = ToolSet()

# ── Subprocess helpers ─────────────────────────────────────────────────────────

def run(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", str(e)


def ffprobe_json(path: Path) -> Optional[dict]:
    """Run ffprobe and return parsed JSON, or None on failure."""
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

# ── Container signature check ──────────────────────────────────────────────────

MP4_SIGNATURES = [
    b"ftyp",   # standard MP4 ftyp box (at offset 4)
    b"moov",   # moov-first files
    b"mdat",   # mdat-first (rare)
    b"wide",   # QuickTime
    b"free",   # QuickTime free space atom
]

def check_container_signature(path: Path) -> List[Issue]:
    issues = []
    ext = path.suffix.lower()
    raw = path.read_bytes()[:32]

    if ext in MP4_EXTS:
        # MP4/MOV: bytes 4-8 should be a known box type
        if len(raw) >= 8 and raw[4:8] not in MP4_SIGNATURES:
            issues.append(Issue("warning", "mp4_unknown_box",
                f"First box type '{raw[4:8]}' is not a recognised MP4/MOV atom"))

    elif ext in MKV_EXTS:
        # EBML header: 0x1A 0x45 0xDF 0xA3
        if not raw[:4] == b"\x1a\x45\xdf\xa3":
            issues.append(Issue("error", "mkv_bad_ebml",
                f"Missing EBML header — not a valid Matroska file "
                f"(got {raw[:4].hex()})"))

    elif ext in AVI_EXTS:
        if raw[:4] != b"RIFF" or raw[8:12] != b"AVI ":
            issues.append(Issue("error", "avi_bad_signature",
                f"Missing RIFF/AVI signature (got {raw[:4]} / {raw[8:12]})"))

    return issues


# ── ffprobe-based checks ───────────────────────────────────────────────────────

def check_via_ffprobe(path: Path) -> List[Issue]:
    issues = []
    data = ffprobe_json(path)

    if data is None:
        issues.append(Issue("error", "ffprobe_failed",
            "ffprobe could not read file — likely corrupt or unrecognised format"))
        return issues

    # ffprobe reported an error
    if "error" in data:
        msg = data["error"].get("string", "unknown error")
        issues.append(Issue("error", "ffprobe_error", f"ffprobe: {msg}"))
        return issues

    fmt  = data.get("format", {})
    streams = data.get("streams", [])

    # No streams at all
    if not streams:
        issues.append(Issue("error", "no_streams",
            "File contains no audio or video streams"))
        return issues

    # Check for video stream
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        issues.append(Issue("warning", "no_video_stream",
            "No video stream found — audio-only or stripped container"))

    # Duration sanity
    duration = float(fmt.get("duration", 0) or 0)
    if duration == 0:
        issues.append(Issue("error", "zero_duration",
            "Duration is 0 — container metadata may be missing or corrupt"))
    elif duration < 0:
        issues.append(Issue("error", "negative_duration",
            f"Negative duration ({duration:.2f}s) — corrupt container metadata"))

    # Bit rate sanity
    bit_rate = int(fmt.get("bit_rate", 0) or 0)
    if bit_rate == 0 and duration > 0:
        issues.append(Issue("warning", "zero_bitrate",
            "Bitrate reported as 0 — index may be missing or incomplete"))

    # Check each video stream
    for vs in video_streams:
        codec = vs.get("codec_name", "unknown")
        width  = vs.get("width", 0)
        height = vs.get("height", 0)

        if codec == "none" or codec == "unknown":
            issues.append(Issue("error", "unknown_video_codec",
                f"Video codec not identified: '{codec}'"))

        if width == 0 or height == 0:
            issues.append(Issue("error", "zero_dimensions",
                f"Video dimensions are {width}x{height} — stream header corrupt"))

        # Suspicious frame rate
        r_frame_rate = vs.get("r_frame_rate", "0/1")
        try:
            num, den = map(int, r_frame_rate.split("/"))
            fps = num / den if den else 0
            if fps > 240:
                issues.append(Issue("warning", "unusual_fps",
                    f"Unusually high frame rate: {fps:.1f} fps — metadata may be corrupt"))
        except Exception:
            pass

    # MP4-specific: check for moov atom position using ffprobe tags
    ext = path.suffix.lower()
    if ext in MP4_EXTS:
        is_streamable = fmt.get("tags", {}).get("compatible_brands", "")
        # Check via raw scan for moov position
        moov_issues = check_mp4_moov_atom(path)
        issues.extend(moov_issues)

    return issues


def check_mp4_moov_atom(path: Path) -> List[Issue]:
    """
    Scan the MP4 atom structure to find moov position and detect truncation.
    Reads only the first and last 64KB — safe for very large files.
    """
    issues = []
    size = path.stat().st_size
    if size < 8:
        return [Issue("error", "too_small", f"File is only {size} bytes")]

    with open(path, "rb") as f:
        head = f.read(min(65536, size))
        if size > 65536:
            f.seek(max(0, size - 65536))
            tail = f.read()
        else:
            tail = b""

    combined = head + tail

    has_moov = b"moov" in combined
    has_mdat = b"mdat" in combined or b"mdat" in head

    moov_in_head = b"moov" in head
    moov_in_tail = b"moov" in tail

    if not has_moov:
        issues.append(Issue("error", "mp4_no_moov",
            "moov atom not found — file was likely not finalised after recording. "
            "Repair will attempt ffmpeg remux with -movflags faststart"))
    elif moov_in_tail and not moov_in_head:
        issues.append(Issue("warning", "mp4_moov_at_end",
            "moov atom is at the end of the file (not streamable). "
            "Repair will relocate it to the front (-movflags faststart)"))

    if has_mdat and not has_moov:
        issues.append(Issue("warning", "mp4_mdat_only",
            "mdat (media data) found but no moov index — partial recovery may be possible"))

    return issues


def check_via_mkvmerge(path: Path) -> List[Issue]:
    """MKV-specific structural check using mkvmerge --identify."""
    issues = []
    if not TOOLS.has_mkvtools:
        return issues

    cmd = [TOOLS.mkvmerge, "--identify", "--identification-format", "json", str(path)]
    rc, out, err = run(cmd, timeout=30)

    try:
        data = json.loads(out)
    except Exception:
        if rc != 0:
            issues.append(Issue("warning", "mkvmerge_identify_failed",
                f"mkvmerge could not identify file: {err.strip()[:120]}"))
        return issues

    # mkvmerge exit codes: 0 = ok, 1 = warnings, 2 = errors
    if rc == 2:
        issues.append(Issue("error", "mkvmerge_error",
            f"mkvmerge structural error: {err.strip()[:200]}"))
    elif rc == 1:
        issues.append(Issue("warning", "mkvmerge_warning",
            f"mkvmerge warnings: {err.strip()[:200]}"))

    # Check track count
    tracks = data.get("tracks", [])
    if not tracks:
        issues.append(Issue("error", "mkv_no_tracks", "No tracks found in MKV file"))

    return issues


def check_ffmpeg_decode(path: Path) -> List[Issue]:
    """
    Probe the whole file by decoding without output — catches corrupt packets,
    missing frames, audio sync errors. This is the slowest check but most thorough.
    We run it at warning level so only real decode errors surface.
    """
    issues = []
    cmd = [
        TOOLS.ffmpeg, "-v", "error",
        "-i", str(path),
        "-f", "null", "-",
    ]
    rc, out, err = run(cmd, timeout=300)

    if err.strip():
        # Parse error output — filter noise
        error_lines = []
        for line in err.strip().splitlines():
            line = line.strip()
            # Skip common benign messages
            if any(skip in line.lower() for skip in [
                "deprecated", "last message", "frames successfully",
                "silently", "header missing", "non monotonous"
            ]):
                continue
            if line:
                error_lines.append(line)

        if error_lines:
            sample = error_lines[0][:180]
            total = len(error_lines)
            issues.append(Issue(
                "warning" if rc == 0 else "error",
                "decode_errors",
                f"{total} decode error(s) found. First: {sample}"
                + (f" (+{total-1} more)" if total > 1 else "")
            ))

    return issues


# ── Repair strategies ──────────────────────────────────────────────────────────

def repair_remux(src: Path, dst: Path) -> Tuple[bool, str]:
    """
    Attempt 1: stream-copy remux into a fresh container.
    Fastest, lossless. Fixes most index/atom issues.
    """
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy",
        "-map", "0",
        str(dst)
    ]
    rc, out, err = run(cmd, timeout=600)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "Stream-copy remux succeeded (lossless)"
    return False, f"Remux failed (rc={rc}): {err.strip()[-200:]}"


def repair_mp4_faststart(src: Path, dst: Path) -> Tuple[bool, str]:
    """
    Attempt 2 (MP4/MOV only): remux with -movflags faststart to move moov to front.
    """
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy",
        "-map", "0",
        "-movflags", "+faststart",
        str(dst)
    ]
    rc, out, err = run(cmd, timeout=600)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "MP4 remux with moov faststart succeeded (moov relocated to front)"
    return False, f"Faststart remux failed (rc={rc}): {err.strip()[-200:]}"


def repair_mkv_mkvmerge(src: Path, dst: Path) -> Tuple[bool, str]:
    """
    Attempt 2 (MKV only): remux using mkvmerge which rebuilds Matroska structure.
    Often recovers files that ffmpeg remux cannot.
    """
    if not TOOLS.has_mkvtools:
        return False, "mkvmerge not available"
    cmd = [
        TOOLS.mkvmerge,
        "--no-global-tags",
        "-o", str(dst),
        str(src)
    ]
    rc, out, err = run(cmd, timeout=600)
    # mkvmerge rc=0 ok, rc=1 warnings (output still usable), rc=2 error
    if rc in (0, 1) and dst.exists() and dst.stat().st_size > 100:
        note = "MKV remux via mkvmerge succeeded"
        if rc == 1:
            note += " (with warnings — inspect output)"
        return True, note
    return False, f"mkvmerge remux failed (rc={rc}): {err.strip()[-200:]}"


def repair_reencode(src: Path, dst: Path) -> Tuple[bool, str]:
    """
    Last resort: re-encode video as H.264/AAC. Quality loss possible.
    CRF 18 = near-lossless visually; adjust as needed.
    """
    cmd = [
        TOOLS.ffmpeg, "-y",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst)
    ]
    rc, out, err = run(cmd, timeout=1800)
    if rc == 0 and dst.exists() and dst.stat().st_size > 100:
        return True, "Re-encode to H.264/AAC succeeded (quality: CRF 18)"
    return False, f"Re-encode failed (rc={rc}): {err.strip()[-200:]}"


def attempt_video_repair(src: Path, dst_dir: Path, rel: str,
                         issues: List[Issue],
                         allow_reencode: bool) -> Tuple[Optional[Path], List[Issue]]:
    """
    Run repair strategies in order, stopping at the first that produces a valid output.
    Returns (output_path_or_None, updated_issues).
    """
    ext = src.suffix.lower()
    has_errors = any(i.severity == "error" for i in issues)

    if not has_errors:
        # No errors — just copy straight through
        dst = dst_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst, issues

    # Build repair plan based on format and issues
    strategies: List[Tuple[str, callable]] = []

    # For MP4/MOV with moov issues, try faststart first
    moov_issue = any(i.code in ("mp4_no_moov", "mp4_moov_at_end") for i in issues)
    if ext in MP4_EXTS and moov_issue:
        strategies.append(("faststart", repair_mp4_faststart))
    
    # Standard remux is always first or second
    strategies.append(("remux", repair_remux))

    # MKV gets mkvmerge as an extra attempt
    if ext in MKV_EXTS and TOOLS.has_mkvtools:
        strategies.append(("mkvmerge", repair_mkv_mkvmerge))

    # Re-encode as absolute last resort
    if allow_reencode:
        strategies.append(("reencode", repair_reencode))

    dst_dir.mkdir(parents=True, exist_ok=True)

    for strategy_name, strategy_fn in strategies:
        # Use a temp file for the attempt
        suffix = ".mp4" if (strategy_name == "reencode" and ext not in MP4_EXTS) else ext
        with tempfile.NamedTemporaryFile(
            dir=dst_dir, suffix=suffix, delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        success, note = strategy_fn(src, tmp_path)

        if success:
            final = dst_dir / rel
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_path), final)
            # Mark all errors as repaired
            for issue in issues:
                if issue.severity == "error" and not issue.repaired:
                    issue.repaired = True
                    issue.repair_note = note
            return final, issues
        else:
            tmp_path.unlink(missing_ok=True)
            # Record the failed attempt as a warning
            issues.append(Issue("warning", f"{strategy_name}_failed",
                                f"Strategy '{strategy_name}' did not succeed: {note[:120]}"))

    return None, issues


# ── Generic checks ─────────────────────────────────────────────────────────────

def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest()

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def bar(done: int, total: int, width: int = 28) -> str:
    if total == 0: return "[" + "─" * width + "]"
    filled = int(width * done / total)
    return "[" + "█" * filled + "─" * (width - filled) + "]"


# ── Input resolution (mirrors file_copy_repair.py) ───────────────────────────

def resolve_inputs(sources: List[str], recursive: bool,
                   ext_filter: Optional[Set[str]]) -> Tuple[List[Path], List[str]]:
    seen: dict[Path, None] = {}
    errors: List[str] = []

    def _add(p: Path):
        p = p.resolve()
        if ext_filter and p.suffix.lower() not in ext_filter:
            return
        if p not in seen:
            seen[p] = None

    def _expand_dir(d: Path):
        pattern = "**/*" if recursive else "*"
        for p in sorted(d.glob(pattern)):
            if p.is_file() and p.suffix.lower() in ALL_VIDEO_EXTS:
                _add(p)

    for src in sources:
        has_glob = any(ch in src for ch in ("*", "?", "["))
        if has_glob:
            parent  = Path(src).parent
            pattern = Path(src).name
            if any(ch in str(parent) for ch in ("*", "?", "[")):
                parent = Path(".")
                pattern = src
            matches = sorted(Path(parent).glob(pattern))
            if not matches:
                errors.append(f"No files matched glob: {src}")
            for m in matches:
                if m.is_file(): _add(m)
                elif m.is_dir(): _expand_dir(m)
        else:
            p = Path(src).resolve()
            if p.is_dir(): _expand_dir(p)
            elif p.is_file(): _add(p)
            else: errors.append(f"Not found: {src}")

    return list(seen.keys()), errors


def load_list_file(path: Path) -> List[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# ── Engine ─────────────────────────────────────────────────────────────────────

class VideoRepairEngine:

    def __init__(self, files: List[Path], dst: Path, dry_run: bool,
                 verbose: bool, ext_filter: Optional[Set[str]],
                 allow_reencode: bool, resume: bool = False,
                 sources_label: str = ""):
        self.files = files
        self.dst = dst
        self.dry_run = dry_run
        self.verbose = verbose
        self.ext_filter = ext_filter
        self.allow_reencode = allow_reencode
        self.sources_label = sources_label
        self.resume         = resume
        self.results: List[FileResult] = []
        self._log_path: Optional[Path] = None
        self._common_root: Optional[Path] = None
        self._setup_logging()

    # ── logging ───────────────────────────────────────────────────────────────

    def _setup_logging(self):
        self._logger = logging.getLogger("video_repair")
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

    def _p(self, msg: str, end="\n"):
        print(msg, end=end, flush=True)

    def _header(self):
        w = 72
        self._p(c("─" * w, DIM))
        self._p(c("  VIDEO COPY + REPAIR UTILITY  ·  Linux", BOLD))
        self._p(c(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}", DIM))
        self._p(c("─" * w, DIM))
        label_lines = self.sources_label.splitlines() if self.sources_label else ["(none)"]
        self._p(f"  Source : {c(label_lines[0], CYAN)}")
        for ln in label_lines[1:]:
            self._p(f"           {c(ln, CYAN)}")
        self._p(f"  Dest   : {c(str(self.dst), CYAN)}")
        if self.dry_run:
            self._p(f"  Mode   : {c('DRY RUN — no files will be written', YELLOW)}")
        if not self.allow_reencode:
            self._p(f"  Mode   : {c('Re-encode disabled (--no-reencode)', YELLOW)}")
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
        pct  = f"{idx}/{total}"
        prog = bar(idx, total)
        rel  = result.rel
        if len(rel) > 38: rel = "…" + rel[-37:]
        status_map = {
            "ok":       c("  OK      ", GREEN),
            "repaired": c("  REPAIRED", BLUE),
            "partial":  c("  PARTIAL ", YELLOW),
            "failed":   c("  FAILED  ", RED),
            "skipped":  c("  SKIPPED ", RED),
            "done":     c("  done    ", DIM),
        }
        st   = status_map.get(result.status, result.status)
        size = human_size(result.size_bytes)
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

    # ── relative path ─────────────────────────────────────────────────────────

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

    # ── inspect ───────────────────────────────────────────────────────────────

    def _inspect(self, path: Path) -> List[Issue]:
        issues = []
        size = path.stat().st_size

        if size == 0:
            issues.append(Issue("error", "zero_byte", "File is zero bytes"))
            return issues

        issues += check_container_signature(path)
        issues += check_via_ffprobe(path)

        ext = path.suffix.lower()
        if ext in MKV_EXTS:
            issues += check_via_mkvmerge(path)

        # Only do slow decode-scan if structural checks found problems
        if any(i.severity == "error" for i in issues):
            # Skip decode check if signature is totally wrong (would just hang)
            sig_err = any(i.code in ("mkv_bad_ebml", "avi_bad_signature") for i in issues)
            if not sig_err:
                issues += check_ffmpeg_decode(path)

        return issues

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self):
        self._header()

        if not TOOLS.check_required():
            self._p(c("\n  Error: ffmpeg and ffprobe are required. Install with:", RED))
            self._p(c("    sudo apt install ffmpeg", CYAN))
            sys.exit(1)

        if not self.dry_run:
            self.dst.mkdir(parents=True, exist_ok=True)
            self._attach_file_log()

        total = len(self.files)
        if total == 0:
            self._p(c("  No video files found matching criteria.", YELLOW))
            return

        self._p(f"  Found {c(str(total), BOLD)} video file(s) to process.\n")

        seen_rels: dict[str, int] = {}

        for idx, path in enumerate(self.files, 1):
            t0   = time.perf_counter()
            rel  = self._rel(path)

            # Deduplicate destination names
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
                        path, self.dst, rel, result.issues, self.allow_reencode)

                    if out_path and out_path.exists():
                        result.sha256 = sha256_of(out_path)
                        repaired_any   = any(i.repaired for i in result.issues)
                        unrepaired_err = any(i.severity == "error" and not i.repaired
                                             for i in result.issues)
                        if unrepaired_err:
                            result.status = "partial"
                        elif repaired_any:
                            result.status = "repaired"
                            # Find which strategy succeeded
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

            show_issues = has_errors or has_warnings or self.verbose
            self._file_line(idx, total, result)
            if show_issues:
                self._issue_lines(result)

        self._p("")
        self._footer()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Copy and repair video files — Linux version.",
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
    ap.add_argument("--resume",       action="store_true",
        help="Skip files already in the destination at >= 95%% of source size")
    ap.add_argument("--no-reencode",  action="store_true",
        help="Never attempt re-encode as last resort")
    ap.add_argument("--recursive",    action="store_true", default=True)
    ap.add_argument("--no-recursive", dest="recursive", action="store_false")
    args = ap.parse_args()

    *raw_sources, destination = args.sources
    dst = Path(destination).resolve()

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
        ext_filter = ALL_VIDEO_EXTS   # default: all known video extensions

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
        sources_label = label,
    ).run()


if __name__ == "__main__":
    main()
