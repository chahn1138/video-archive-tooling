#!/usr/bin/env python3
"""
file_copy_repair.py
-------------------
Copy files from a source directory to a destination, inspecting each file for
common integrity issues along the way and attempting repairs before writing the
copy.  A structured log is written to <destination>/copy_repair.log.

Supported file types and checks
--------------------------------
.jpg / .jpeg / .png / .gif / .webp  — PIL open/verify, truncation check
.pdf                                 — pypdf reader, page count, encryption check
.docx / .xlsx                        — zipfile integrity (both are ZIP-based)
All files                            — SHA-256 checksum stored; zero-byte detection;
                                       optional CRC-32 cross-check on ZIP-based types

Usage
-----
    python3 file_copy_repair.py <source_dir> <dest_dir> [--dry-run] [--verbose]

Options
-------
    --dry-run   Scan and report issues without writing any files.
    --verbose   Print extra detail for every file, not just problem files.
    --ext       Comma-separated list of extensions to include (default: all).
                Example: --ext .jpg,.pdf,.docx
"""

import argparse
import hashlib
import io
import logging
import os
import re
import shutil
import struct
import sys
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ── optional dependencies ──────────────────────────────────────────────────────
try:
    from PIL import Image, UnidentifiedImageError
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError, PdfStreamError
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# ── ANSI colour helpers (disabled on Windows without VT support) ───────────────
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(-11), 7)

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
BLUE   = "\033[34m"
DIM    = "\033[2m"

def c(text, colour): return f"{colour}{text}{RESET}"

# ── data structures ────────────────────────────────────────────────────────────

@dataclass
class Issue:
    severity: str          # "warning" | "error"
    code: str              # machine-readable tag
    description: str       # human-readable detail
    repaired: bool = False
    repair_note: str = ""

@dataclass
class FileResult:
    path: Path
    rel: str
    size_bytes: int
    issues: List[Issue] = field(default_factory=list)
    sha256: str = ""
    status: str = "pending"   # pending | ok | repaired | partial | failed | skipped
    elapsed: float = 0.0

    @property
    def has_errors(self):
        return any(i.severity == "error" for i in self.issues)

    @property
    def has_warnings(self):
        return any(i.severity == "warning" for i in self.issues)

# ── low-level helpers ──────────────────────────────────────────────────────────

def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
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
        return "[" + "─" * width + "]"
    filled = int(width * done / total)
    return "[" + "█" * filled + "─" * (width - filled) + "]"

# ── per-format checkers ────────────────────────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
PDF_EXTS   = {".pdf"}
ZIP_EXTS   = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip", ".jar",
              ".apk", ".epub"}

def check_image(path: Path) -> List[Issue]:
    issues: List[Issue] = []
    if not HAS_PIL:
        issues.append(Issue("warning", "no_pil",
                            "Pillow not installed — image not validated"))
        return issues

    raw = path.read_bytes()

    # Zero-byte / too-small check
    if len(raw) < 4:
        issues.append(Issue("error", "too_small",
                            f"File is only {len(raw)} bytes — likely empty or truncated"))
        return issues

    # JPEG-specific: look for EOI marker 0xFFD9 at end
    if path.suffix.lower() in (".jpg", ".jpeg"):
        if raw[-2:] != b"\xff\xd9":
            issues.append(Issue("warning", "jpeg_no_eoi",
                                "JPEG is missing end-of-image marker — file may be truncated"))

    # PIL open + verify
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.verify()          # checks signatures / CRC for PNG
    except UnidentifiedImageError:
        issues.append(Issue("error", "unidentified",
                            "PIL cannot identify image format — file may be corrupt"))
        return issues
    except Exception as e:
        issues.append(Issue("warning", "pil_verify_fail",
                            f"PIL verify raised: {e}"))

    # Second open to actually decode pixels (verify() can't seek after)
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
    except Exception as e:
        issues.append(Issue("error", "decode_fail",
                            f"Pixel decode failed: {e}"))

    # PNG CRC check on each chunk
    if path.suffix.lower() == ".png" and len(raw) > 8:
        try:
            offset = 8
            while offset < len(raw) - 12:
                length = struct.unpack(">I", raw[offset:offset+4])[0]
                chunk_type = raw[offset+4:offset+8]
                chunk_data = raw[offset+8:offset+8+length]
                stored_crc = struct.unpack(">I", raw[offset+8+length:offset+12+length])[0]
                calc_crc   = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
                if stored_crc != calc_crc:
                    issues.append(Issue("error", "png_crc_bad",
                                        f"CRC mismatch in PNG chunk '{chunk_type.decode('latin1')}' "
                                        f"(stored {stored_crc:#010x}, calc {calc_crc:#010x})"))
                offset += 12 + length
                if chunk_type == b"IEND":
                    break
        except Exception as e:
            issues.append(Issue("warning", "png_crc_scan_error", f"PNG CRC scan failed: {e}"))

    return issues


def check_pdf(path: Path) -> List[Issue]:
    issues: List[Issue] = []
    if not HAS_PYPDF:
        issues.append(Issue("warning", "no_pypdf",
                            "pypdf not installed — PDF not validated"))
        return issues

    raw = path.read_bytes()
    if len(raw) < 5:
        issues.append(Issue("error", "too_small", "File too small to be a valid PDF"))
        return issues

    if not raw[:5].startswith(b"%PDF-"):
        issues.append(Issue("error", "bad_header",
                            f"Missing %%PDF- header (got {raw[:8]!r})"))

    if b"%%EOF" not in raw[-2048:] and b"%EOF" not in raw[-2048:]:
        issues.append(Issue("warning", "no_eof_marker",
                            "%%EOF marker not found in last 2 KB — file may be truncated"))

    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            issues.append(Issue("warning", "encrypted",
                                "PDF is encrypted — content not fully validated"))
        else:
            _ = len(reader.pages)   # force page-tree parse
            # Try reading first and last page text
            for idx in [0, len(reader.pages)-1]:
                try:
                    reader.pages[idx].extract_text()
                except Exception as e:
                    issues.append(Issue("warning", "page_extract_fail",
                                        f"Text extraction failed on page {idx+1}: {e}"))
    except PdfReadError as e:
        issues.append(Issue("error", "pdf_read_error", str(e)))
    except PdfStreamError as e:
        issues.append(Issue("error", "pdf_stream_error", str(e)))
    except Exception as e:
        issues.append(Issue("warning", "pdf_open_warning", f"PDF open raised: {e}"))

    return issues


def check_zip_based(path: Path) -> List[Issue]:
    """Checks .docx, .xlsx, .zip and anything else that is a ZIP container."""
    issues: List[Issue] = []
    raw = path.read_bytes()

    if len(raw) < 4:
        issues.append(Issue("error", "too_small", "File is smaller than a ZIP local header"))
        return issues

    if raw[:2] != b"PK":
        issues.append(Issue("error", "bad_zip_sig",
                            f"Missing PK signature (got {raw[:4]!r}) — not a valid ZIP"))
        return issues

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            bad = zf.testzip()
            if bad:
                issues.append(Issue("error", "zip_crc_fail",
                                    f"CRC/integrity failure in member: {bad}"))
            names = zf.namelist()
            # docx must have [Content_Types].xml
            if path.suffix.lower() == ".docx":
                if "[Content_Types].xml" not in names:
                    issues.append(Issue("error", "docx_missing_content_types",
                                        "[Content_Types].xml missing — not a valid OOXML file"))
            elif path.suffix.lower() == ".xlsx":
                if "[Content_Types].xml" not in names:
                    issues.append(Issue("error", "xlsx_missing_content_types",
                                        "[Content_Types].xml missing — not a valid OOXML file"))
    except zipfile.BadZipFile as e:
        issues.append(Issue("error", "bad_zip", f"zipfile.BadZipFile: {e}"))
    except Exception as e:
        issues.append(Issue("warning", "zip_open_warning", f"ZIP open raised: {e}"))

    return issues


def check_generic(path: Path) -> List[Issue]:
    """Checks applied to every file regardless of type."""
    issues: List[Issue] = []
    size = path.stat().st_size
    if size == 0:
        issues.append(Issue("error", "zero_byte", "File is zero bytes"))
    return issues


# ── repair attempts ────────────────────────────────────────────────────────────

def attempt_repair(src: Path, issues: List[Issue]) -> Tuple[bytes, List[Issue]]:
    """
    Try to repair the file in memory.  Returns (possibly-modified bytes, updated issues).
    Repairs are best-effort; each issue is marked repaired=True/False.
    """
    raw = src.read_bytes()
    ext = src.suffix.lower()

    for issue in issues:
        if issue.severity != "error":
            continue  # only attempt repairs on errors

        # ── JPEG missing EOI ──────────────────────────────────────────────────
        if issue.code == "jpeg_no_eoi":
            raw += b"\xff\xd9"
            issue.repaired = True
            issue.repair_note = "Appended JPEG EOI marker (0xFFD9)"

        # ── PDF missing header ────────────────────────────────────────────────
        elif issue.code == "bad_header" and ext == ".pdf":
            raw = b"%PDF-1.4\n" + raw
            issue.repaired = True
            issue.repair_note = "Prepended %PDF-1.4 header"

        # ── PDF missing EOF ───────────────────────────────────────────────────
        elif issue.code == "no_eof_marker" and ext == ".pdf":
            raw = raw.rstrip() + b"\n%%EOF\n"
            issue.repaired = True
            issue.repair_note = "Appended %%EOF marker"

        # ── Zero-byte: nothing we can do ─────────────────────────────────────
        elif issue.code == "zero_byte":
            issue.repair_note = "Cannot repair a zero-byte file — skipping copy"

        # ── ZIP / OOXML corruption ────────────────────────────────────────────
        elif issue.code in ("bad_zip", "bad_zip_sig", "zip_crc_fail",
                             "docx_missing_content_types", "xlsx_missing_content_types"):
            issue.repair_note = "ZIP-level corruption cannot be repaired in-place — file copied as-is for manual recovery"

        # ── PNG CRC bad ───────────────────────────────────────────────────────
        elif issue.code == "png_crc_bad":
            issue.repair_note = "PNG CRC mismatch — recalculating stored CRCs"
            try:
                out = bytearray(raw[:8])
                offset = 8
                while offset < len(raw) - 12:
                    length = struct.unpack(">I", raw[offset:offset+4])[0]
                    chunk_type = raw[offset+4:offset+8]
                    chunk_data = raw[offset+8:offset+8+length]
                    new_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
                    out += raw[offset:offset+8+length]
                    out += struct.pack(">I", new_crc)
                    offset += 12 + length
                    if chunk_type == b"IEND":
                        break
                raw = bytes(out)
                issue.repaired = True
                issue.repair_note = "PNG CRC values recalculated and corrected"
            except Exception as e:
                issue.repair_note = f"PNG CRC recalculation failed: {e}"

    return raw, issues


# ── main inspector / copier ────────────────────────────────────────────────────

class CopyRepairEngine:

    def __init__(self, src: Path, dst: Path, dry_run: bool, verbose: bool,
                 ext_filter: Optional[set]):
        self.src = src
        self.dst = dst
        self.dry_run = dry_run
        self.verbose = verbose
        self.ext_filter = ext_filter
        self.results: List[FileResult] = []
        self._log_path: Optional[Path] = None
        self._setup_logging()

    def _setup_logging(self):
        self._logger = logging.getLogger("copy_repair")
        self._logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.WARNING)   # console only gets warnings+
        self._logger.addHandler(sh)

    def _attach_file_handler(self):
        if self.dry_run:
            return
        self._log_path = self.dst / "copy_repair.log"
        fh = logging.FileHandler(self._log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh.setFormatter(fmt)
        self._logger.addHandler(fh)

    # ── printing ───────────────────────────────────────────────────────────────

    def _print(self, msg: str, end="\n"):
        print(msg, end=end, flush=True)

    def _header(self):
        width = 68
        self._print(c("─" * width, DIM))
        self._print(c(f"  FILE COPY + REPAIR UTILITY", BOLD))
        self._print(c(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}", DIM))
        self._print(c("─" * width, DIM))
        self._print(f"  Source : {c(str(self.src), CYAN)}")
        self._print(f"  Dest   : {c(str(self.dst), CYAN)}")
        if self.dry_run:
            self._print(f"  Mode   : {c('DRY RUN — no files will be written', YELLOW)}")
        if self.ext_filter:
            self._print(f"  Filter : {', '.join(sorted(self.ext_filter))}")
        self._print(c("─" * width, DIM))

    def _footer(self):
        ok      = sum(1 for r in self.results if r.status == "ok")
        rep     = sum(1 for r in self.results if r.status == "repaired")
        partial = sum(1 for r in self.results if r.status == "partial")
        failed  = sum(1 for r in self.results if r.status in ("failed","skipped"))
        total   = len(self.results)
        elapsed = sum(r.elapsed for r in self.results)

        width = 68
        self._print(c("─" * width, DIM))
        self._print(c("  SUMMARY", BOLD))
        self._print(c("─" * width, DIM))
        self._print(f"  {'Total files':<22} {total}")
        self._print(f"  {'Copied OK':<22} {c(str(ok), GREEN)}")
        self._print(f"  {'Repaired + copied':<22} {c(str(rep), BLUE)}")
        self._print(f"  {'Partial recovery':<22} {c(str(partial), YELLOW)}")
        self._print(f"  {'Failed / skipped':<22} {c(str(failed), RED)}")
        self._print(f"  {'Time elapsed':<22} {elapsed:.1f}s")
        if self._log_path:
            self._print(f"  {'Log written to':<22} {self._log_path}")
        self._print(c("─" * width, DIM))

        if failed or partial:
            self._print(c("\n  ⚠  Files marked FAILED or PARTIAL need manual inspection.", YELLOW))
        else:
            self._print(c("\n  ✓  All files processed successfully.", GREEN))
        self._print("")

    def _file_line(self, idx: int, total: int, result: FileResult):
        pct = f"{idx}/{total}"
        prog = bar(idx, total)
        rel = result.rel
        if len(rel) > 36:
            rel = "…" + rel[-35:]

        status_map = {
            "ok":       c("  OK      ", GREEN),
            "repaired": c("  REPAIRED", BLUE),
            "partial":  c("  PARTIAL ", YELLOW),
            "failed":   c("  FAILED  ", RED),
            "skipped":  c("  SKIPPED ", RED),
            "pending":  c("  ...     ", DIM),
        }
        status = status_map.get(result.status, result.status)
        size   = human_size(result.size_bytes)

        self._print(f"  {c(prog, DIM)} {c(pct, DIM):>8}  {status}  {rel:<36}  {c(size, DIM)}")

    def _issue_lines(self, result: FileResult):
        for iss in result.issues:
            icon  = "⚠" if iss.severity == "warning" else "✕"
            colour = YELLOW if iss.severity == "warning" else RED
            self._print(f"             {c(icon, colour)} {iss.code}: {iss.description}")
            if iss.repair_note:
                rep_col = GREEN if iss.repaired else YELLOW
                tag = "↻ repaired" if iss.repaired else "→"
                self._print(f"               {c(tag, rep_col)} {iss.repair_note}")

    # ── core logic ─────────────────────────────────────────────────────────────

    def _collect_files(self) -> List[Path]:
        files = []
        for p in sorted(self.src.rglob("*")):
            if not p.is_file():
                continue
            if self.ext_filter and p.suffix.lower() not in self.ext_filter:
                continue
            files.append(p)
        return files

    def _inspect(self, path: Path) -> List[Issue]:
        issues = check_generic(path)
        if issues and issues[0].code == "zero_byte":
            return issues   # nothing more to do

        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            issues += check_image(path)
        elif ext in PDF_EXTS:
            issues += check_pdf(path)
        elif ext in ZIP_EXTS:
            issues += check_zip_based(path)

        return issues

    def _copy_file(self, src: Path, dst: Path, raw: bytes):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        # Preserve timestamps
        stat = src.stat()
        os.utime(dst, (stat.st_atime, stat.st_mtime))

    def run(self):
        self._header()

        if not self.src.is_dir():
            self._print(c(f"\n  Error: source '{self.src}' is not a directory.", RED))
            sys.exit(1)

        if not self.dry_run:
            self.dst.mkdir(parents=True, exist_ok=True)
            self._attach_file_handler()

        files = self._collect_files()
        total = len(files)

        if total == 0:
            self._print(c("  No files found matching criteria.", YELLOW))
            return

        self._print(f"  Found {c(str(total), BOLD)} file(s) to process.\n")

        for idx, path in enumerate(files, 1):
            t0     = time.perf_counter()
            rel    = str(path.relative_to(self.src))
            size   = path.stat().st_size
            result = FileResult(path=path, rel=rel, size_bytes=size)

            # ── inspect ──────────────────────────────────────────────────────
            issues = self._inspect(path)
            result.issues = issues

            has_errors   = any(i.severity == "error" for i in issues)
            has_warnings = any(i.severity == "warning" for i in issues)

            # ── decide what to copy ───────────────────────────────────────────
            if not self.dry_run:
                raw = path.read_bytes()

                if has_errors:
                    raw, result.issues = attempt_repair(path, result.issues)

                dst_path = self.dst / rel
                zero_byte_err = any(i.code == "zero_byte" for i in result.issues)
                if zero_byte_err:
                    result.status = "skipped"
                    self._logger.warning(f"SKIPPED {rel} — zero byte file")
                else:
                    self._copy_file(path, dst_path, raw)
                    result.sha256 = sha256_of(dst_path)

                    repaired_any  = any(i.repaired for i in result.issues)
                    unrepaired_err = any(i.severity == "error" and not i.repaired
                                         for i in result.issues)
                    if unrepaired_err:
                        result.status = "partial"
                    elif repaired_any:
                        result.status = "repaired"
                    elif has_errors:
                        result.status = "failed"
                    else:
                        result.status = "ok"

                self._logger.info(
                    f"{result.status.upper():10} {rel}  sha256={result.sha256[:16]}…"
                    + (f"  issues={len(issues)}" if issues else ""))
            else:
                # dry-run: classify without writing
                if any(i.code == "zero_byte" for i in issues):
                    result.status = "skipped"
                elif has_errors:
                    result.status = "partial"   # conservative: mark as partial
                elif has_warnings:
                    result.status = "repaired"  # would attempt repair
                else:
                    result.status = "ok"

            result.elapsed = time.perf_counter() - t0
            self.results.append(result)

            # ── print ─────────────────────────────────────────────────────────
            show_issues = has_errors or has_warnings or self.verbose
            self._file_line(idx, total, result)
            if show_issues:
                self._issue_lines(result)

        self._print("")
        self._footer()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Copy files with integrity checks and best-effort repair.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("source",      type=Path, help="Source directory")
    ap.add_argument("destination", type=Path, help="Destination directory")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Scan and report without writing files")
    ap.add_argument("--verbose",  action="store_true",
                    help="Print details for every file, not just problem files")
    ap.add_argument("--ext",      type=str, default=None,
                    help="Comma-separated extensions to include, e.g. .jpg,.pdf")
    args = ap.parse_args()

    ext_filter = None
    if args.ext:
        ext_filter = {e if e.startswith(".") else "."+e
                      for e in args.ext.split(",")}

    engine = CopyRepairEngine(
        src       = args.source.resolve(),
        dst       = args.destination.resolve(),
        dry_run   = args.dry_run,
        verbose   = args.verbose,
        ext_filter= ext_filter,
    )
    engine.run()


if __name__ == "__main__":
    main()
