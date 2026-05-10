#!/usr/bin/env python3
"""
make_journal_from_log.py
------------------------
Synthesises a video_repair_journal JSONL file from either:

  (a) A saved copy of the console output from a --resume run, OR
  (b) The video_repair.log file written to the destination

This lets you kill a long-running process and restart it with --resume
using the new journal-aware version, without losing progress.

The script scans the input for lines matching the per-file result format:
    [────...────] N/TOTAL   STATUS   filename   size

Any file with status OK, REPAIRED, or done is written to the journal
as a trusted entry.  FAILED, PARTIAL, and SKIPPED are omitted so the
new run will re-attempt them.

Usage
-----
    # From saved console output (paste into a .txt file first):
    python make_journal_from_log.py  --input  run_output.txt  ^
                                     --source  G:\\video       ^
                                     --dest    C:\\temp

    # From the log file in the destination:
    python make_journal_from_log.py  --input  C:\\temp\\video_repair.log  ^
                                     --source  G:\\video                   ^
                                     --dest    C:\\temp

    # Preview only — don't write anything:
    python make_journal_from_log.py  --input run_output.txt  ^
                                     --source G:\\video --dest C:\\temp  ^
                                     --dry-run

The journal is written to:
    <dest>\\video_repair_journal__<src_slug>__<dst_slug>__00000000-0000.jsonl

The sentinel timestamp 00000000-0000 sorts before any real run timestamp
so the real run's journal (once it starts) will be preferred on the next
restart after the first genuine run.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple


# ── Status sets ────────────────────────────────────────────────────────────────

TRUSTED   = {"ok", "repaired", "done"}
UNTRUSTED = {"failed", "partial", "skipped"}

# ── Line pattern ───────────────────────────────────────────────────────────────
#
# Matches lines like:
#   [████████████────────────────] 1457/4357    OK        Court Jester The.mkv  2.8 GB
#   [────────────────────────────] 1/4357    done      '71.mp4                  1.4 GB
#   [════...════] N/TOTAL   REPAIRED  filename   size
#
# Also matches the plain-text (no-colour) variant:
#   [############################] 1457/4357   OK   filename   size

LINE_RE = re.compile(
    r'\[[\s\S]{10,35}\]\s+'      # progress bar  [███─────] or [###---]
    r'\d+/\d+\s+'               # N/TOTAL
    r'([A-Za-z]+)\s+'           # STATUS (captured)
    r'(.+?)\s+'                 # filename (captured, non-greedy)
    r'([\d.,]+\s*[KMGT]?B)',    # size  e.g. 2.8 GB
    re.IGNORECASE
)

# Also match the video_repair.log format:
#   2026-05-03 11:40:11  INFO      OK         filename  →  rel  sha256=abc...
LOG_RE = re.compile(
    r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+'
    r'\w+\s+'                   # log level
    r'(OK|REPAIRED|PARTIAL|FAILED|SKIPPED|DONE)\s+'  # STATUS
    r'(.+?)\s+(?:→|--)?\s*',   # src path
    re.IGNORECASE
)


def slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r'[^\w.\-]', '_', text)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:maxlen]


def parse_console_output(text: str) -> List[Tuple[str, str]]:
    """
    Parse console-output lines.
    Returns list of (status, filename) tuples for trusted statuses.
    """
    results = []
    for line in text.splitlines():
        # Strip ANSI escape codes
        clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        m = LINE_RE.search(clean)
        if not m:
            continue
        status   = m.group(1).lower().strip()
        filename = m.group(2).strip()
        # Strip trailing whitespace that may be padding
        filename = filename.rstrip()
        if status in TRUSTED:
            results.append((status, filename))
    return results


def parse_log_file(text: str) -> List[Tuple[str, str]]:
    """
    Parse video_repair.log lines.
    Returns list of (status, src_path) tuples for trusted statuses.
    """
    results = []
    for line in text.splitlines():
        m = LOG_RE.match(line.strip())
        if not m:
            continue
        status   = m.group(1).lower()
        src_path = m.group(2).strip()
        if status in TRUSTED:
            results.append((status, src_path))
    return results


def make_journal(entries: List[Tuple[str, str]],
                 src_root: Path,
                 dst: Path,
                 dry_run: bool,
                 from_log: bool) -> Path:
    """
    Write the synthesised journal file.
    Returns the path of the journal written (or that would be written).
    """
    src_slug = slugify(str(src_root))
    dst_slug = slugify(str(dst))
    fname    = (f"video_repair_journal__{src_slug}__{dst_slug}"
                f"__00000000-0000.jsonl")
    jpath    = dst / fname

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    lines   = []
    skipped = []

    for status, name in entries:
        # Derive rel path from the name
        if from_log:
            # name is a full src path — make it relative to src_root
            try:
                rel = str(Path(name).relative_to(src_root))
            except ValueError:
                rel = Path(name).name
        else:
            # name is already the relative display name from console output
            rel = name

        # Try to get real file size from destination
        dst_file = dst / rel
        size = 0
        sha256 = ""
        if dst_file.exists():
            size = dst_file.stat().st_size
        else:
            # Try case-insensitive search (Windows NTFS)
            matches = list(dst.glob(rel)) if '*' not in rel else []
            if matches:
                size = matches[0].stat().st_size

        entry = {
            "ts":       now_str,
            "run":      "00000000-0000",  # sentinel — synthetic journal
            "rel":      rel,
            "src":      str(src_root / rel),
            "status":   status,
            "size":     size,
            "sha256":   sha256,            # unknown — not re-computed here
            "elapsed":  0.0,
            "strategy": "",
            "issues":   [],
        }
        lines.append(entry)

    print(f"\n  Synthesised journal: {fname}")
    print(f"  Trusted entries:     {len(lines)}")
    print(f"  (FAILED/PARTIAL/SKIPPED entries are excluded — they will be re-attempted)\n")

    if lines:
        print("  Sample entries:")
        for e in lines[:5]:
            print(f"    {e['status']:10}  {e['rel']}")
        if len(lines) > 5:
            print(f"    ... and {len(lines)-5} more")
    print()

    if dry_run:
        print("  DRY RUN — no file written.")
        return jpath

    dst.mkdir(parents=True, exist_ok=True)
    with open(jpath, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())

    print(f"  Written: {jpath}")
    print(f"\n  You can now restart with:")
    print(f'    python video_copy_repair_windows.py "{src_root}" "{dst}" --resume')
    print()
    return jpath


def main():
    ap = argparse.ArgumentParser(
        description="Synthesise a video_repair_journal from a previous run's output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--input",   required=True, type=Path,
        help="Path to saved console output (.txt) or video_repair.log")
    ap.add_argument("--source",  required=True, type=Path,
        help="Source directory used in the original run  (e.g. G:\\video)")
    ap.add_argument("--dest",    required=True, type=Path,
        help="Destination directory used in the original run  (e.g. C:\\temp)")
    ap.add_argument("--dry-run", action="store_true",
        help="Show what would be written without writing anything")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"Error: input file not found: {args.input}")
        sys.exit(1)

    text = args.input.read_text(encoding="utf-8-sig", errors="replace")

    # Auto-detect format by looking for log timestamps
    is_log = bool(re.search(
        r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+\w+\s+(OK|REPAIRED|FAILED)',
        text, re.IGNORECASE))

    if is_log:
        print("  Detected: video_repair.log format")
        entries = parse_log_file(text)
    else:
        print("  Detected: console output format")
        entries = parse_console_output(text)

    if not entries:
        print("\n  No trusted entries found in input file.")
        print("  Check that the file contains lines with OK / REPAIRED / done status.")
        sys.exit(1)

    make_journal(
        entries  = entries,
        src_root = args.source.resolve(),
        dst      = args.dest.resolve(),
        dry_run  = args.dry_run,
        from_log = is_log,
    )


if __name__ == "__main__":
    main()
