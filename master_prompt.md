Video Copy + Repair Utility
Master Build Prompt

This document is a complete, self-contained specification for building the Video Copy + Repair
Utility from scratch. A skilled Python developer following this prompt should be able to
reproduce all scripts, their behaviour, and the design intent behind every decision,
without access to any prior conversation or source code.

What is built: Three platform-specific Python scripts (Linux, macOS, Windows) that
copy video files from any source to a destination, inspecting each file for structural
corruption, attempting repairs in a defined order, maintaining a crash-safe journal,
and resuming intelligently across restarts — all with live progress display.

Generated Mon May 04 2026

1.  Purpose & Scope
The tool was designed to solve a real-world problem: migrating a large video library
(4000+ files, several TB) from one storage location to another, where some files may
be corrupt due to age, bad sectors, interrupted recordings, or previous failed transfers.

The key requirements that shaped every design decision:

- Inspect every file structurally before copying, not just blindly copy it
- Attempt automated repair when corruption is detected, using the least-destructive strategy first
- Show live progress — never look like it has hung, even on a 20 GB file
- Support resuming a large run after interruption without re-processing what was already done
- On resume, verify destination files rather than blindly trusting their presence
- Trust prior certified work (the journal) to avoid redundant re-inspection on subsequent restarts
- Write a complete log and a machine-readable journal for reporting and auditability
- Run natively on Linux, macOS, and Windows without modification

Scope: video files only (.mp4 .mkv .avi .mov .wmv .flv .ts .webm and others).
A companion script file_copy_repair.py handles images, PDFs, and Office documents
using pure Python libraries (Pillow, pypdf, zipfile) and shares the same input model.

2.  Architecture Overview
Three scripts are provided rather than one unified script so that each platform version
can be solid and independently testable before merging. The planned merge target is
video_copy_repair.py with a platform.system() dispatch at the top.

Script
Platform
Key Difference
video_copy_repair_linux.py
Linux
apt/dnf/pacman install; shutil.which for tool discovery; libx264 SW encode
video_copy_repair_macos.py
macOS
Homebrew paths (/opt/homebrew first); VideoToolbox HW encode; case-insensitive dedup
video_copy_repair_windows.py
Windows
Multi-path tool discovery; NVENC/AMF/QSV HW encode; CREATE_NO_WINDOW; BOM-safe file reading

All three scripts share identical: input resolution logic, check pipeline, repair strategy pipeline, RunJournal class, run_watched() spinner, size_scaled_timeout(), resume logic, JSONL journal format, and summary output structure.

3.  Input Model
The last positional argument is always the destination. Everything before it is a source.
Sources can be freely mixed. All inputs are resolved to a deduplicated list of concrete
file paths before processing begins.

# Single file
python3 script.py  clip.mp4  /backup/

# Directory (recursive by default)
python3 script.py  /nas/Videos/  /backup/

# Glob pattern (quote to prevent shell expansion on Linux/macOS)
python3 script.py  "/nas/*.mkv"  /backup/

# Multiple mixed sources
python3 script.py  a.mp4  b.mkv  /nas/*.avi  /backup/

# List file (one path or glob per line; # = comment)
python3 script.py  --list  files.txt  /backup/

# Windows — backslash paths, no quoting needed (cmd does not expand globs)
python  script.py  D:\Videos\  "D:\*.mkv"  E:\Backup\

resolve_inputs() — the function that handles all of the above:
- Detects glob characters 
- Directories are walked recursively (
- Plain file paths are added directly with existence check
- List files are read with 
- Deduplication uses a 
- Only files whose extension is in 

Relative path logic — the _rel() method on the engine:
- Computes the common root of all source file parents using 
- Each file's display name and destination sub-path is relative to that common root
- If files come from entirely different drives (Windows), falls back to filename only
- Collision handling: if two files from different dirs share a name, appends 

4.  Inspection Pipeline
Every file passes through _inspect() which calls checks in a fixed order,
stopping the decode scan early if the container signature is fundamentally wrong.

Check
Function
Applies to
Notes
Zero-byte
check_generic()
All files
Returns immediately — nothing to inspect or repair
Container signature
check_container_signature()
All files
Reads first 32 bytes; checks ftyp/EBML/RIFF magic bytes
ffprobe metadata
check_via_ffprobe()
All video
Duration, bitrate, stream count, codec, dimensions, fps
MP4 moov atom
check_mp4_moov_atom()
MP4/MOV
Scans head+tail 64KB only — safe on huge files
MKV structure
check_via_mkvmerge()
MKV/WebM
Only if mkvtoolnix installed; exit codes 0=ok 1=warn 2=err
Full decode probe
check_ffmpeg_decode()
Files with errors only
Skipped if signature is totally invalid to avoid hangs

Issue severity levels:
- error
- warning

i
Decode probe is conditional
check_ffmpeg_decode() is expensive — it decodes the entire file.
It only runs when check_via_ffprobe() or check_via_mkvmerge() found an error.
If the container signature is wrong (EBML missing, bad RIFF), it is skipped entirely
to avoid a hang on a file that ffmpeg cannot even open.

5.  Subprocess Design — run_watched()
Silent hanging was the primary UX problem. The solution is run_watched() — a
streaming subprocess wrapper that shows a live spinner while any external tool is running.

5.1  How it works
- Launches the subprocess with 
- Two daemon threads drain stdout and stderr line by line as they arrive
- The stderr drain keeps the last meaningful line received
- The main thread loops at 120ms intervals printing a spinner + elapsed time + last stderr line
- The spinner uses braille animation chars 
- The spinner overwrites a single line using 
- On completion, the spinner line is blanked with spaces then 

5.2  size_scaled_timeout()
Every subprocess call has a timeout scaled to the file being processed:

def size_scaled_timeout(path, base=60, bps=20*1024*1024):
    # Assumes ffmpeg can process ~20 MB/s on slow hardware
    # Floor = base seconds.  Ceiling = 3600s (1 hour).
    size = path.stat().st_size
    return min(max(base, size // bps), 3600)

# Examples:
#   500 MB  →  60s  (floor)
#     2 GB  →  160s
#    20 GB  →  1000s
#   200 GB  →  3600s  (ceiling)

If a probe times out, the issue is recorded as decode_probe_timeout (warning level)
and processing continues — a timeout is not a failure, just an inability to fully verify.

5.3  Windows: CREATE_NO_WINDOW
On Windows, every subprocess call passes creationflags=0x08000000 to prevent
ffmpeg/mkvmerge spawning a visible console window that flashes on screen for each file.

6.  Repair Strategies
Repairs are attempted in order — stopping at the first strategy that produces a valid
output file. Each strategy writes to a temp file; only on success is the temp file
atomically moved to the final destination. A failed attempt leaves no partial output.

#
Strategy
What it does
Quality
When used
1
MP4 moov faststart
Remux MP4 with -movflags +faststart, relocating moov atom to front
Lossless
MP4/MOV with moov_at_end or mp4_no_moov issue
2
Stream-copy remux
ffmpeg -c copy — rebuilds container index from raw streams
Lossless
All formats with any error
3
MKV remux via mkvmerge
Rebuilds Matroska structure; often recovers what ffmpeg cannot
Lossless
MKV/WebM when mkvtoolnix installed
4
Re-encode (last resort)
Full transcode to H.264/AAC using best available encoder
Near-lossless (CRF 18)
Only when all remux strategies fail; skip with --no-reencode

6.1  Re-encode encoder selection
The re-encode strategy auto-selects the best available encoder by querying
ffmpeg -encoders once at startup. Preference order per platform:

Platform
Priority order
Linux
libx264 (software only unless NVENC manually configured)
macOS
h264_videotoolbox (Apple HW) → libx264 SW fallback
Windows
h264_nvenc (Nvidia) → h264_amf (AMD) → h264_qsv (Intel QuickSync) → libx264 SW

Quality settings: NVENC uses -rc vbr -cq 18; VideoToolbox uses -q:v 60; QSV uses -global_quality 18; libx264 uses -crf 18.
All are visually near-lossless. CRF 18 produces files slightly smaller than the original.

!
Re-encode is lossy
Even at CRF 18, re-encoding introduces generational quality loss.
If the source still exists, prefer sourcing a fresh copy over re-encoding.
Use --no-reencode to prevent this strategy from ever being attempted.

7.  Resume System
The --resume flag activates a two-tier skip system. Without it, every file is
always inspected and copied from scratch, regardless of what is in the destination.

7.1  Tier 1: Journal fast-path (zero inspection cost)
If a file's relative path appears in the loaded run journal with a trusted status
(ok, repaired, or done), it is skipped instantly — no ffprobe, no health-check,
no subprocess call at all. A dictionary lookup in memory.

Rationale: The journal records work done by this script. If the script already
inspected and copied a file and certified it healthy, that certification should be trusted
unconditionally. Re-inspecting 4000 files on every restart would defeat the purpose.

7.2  Tier 2: Destination health-check (for un-journaled files)
If a file is not in the journal, the destination is inspected before deciding to skip.
These might be pre-existing files, files from a different run, or interrupted copies.

Verdict
Condition
Action
MISSING
Destination does not exist
Copy normally
TRUNCATED
Destination < 95% of source size, or zero bytes
Delete destination, copy fresh
CORRUPT
Destination size ok but ffprobe reports errors
Delete destination, copy from source (with repair)
WARN
Destination size ok, only warnings
Skip but print warning line; write to journal
CLEAN
Destination size ok, no issues
Skip silently; write to journal for next restart

The 95% size threshold is intentional: a legitimately remuxed/repaired file may be slightly smaller than the original (stripped padding, recalculated headers). An interrupted write of a 2 GB file that produced 200 MB will clearly fall below 95%.

i
Journal is written for health-checked files too
When a destination passes the health-check (CLEAN or WARN verdict), the result is written
to the journal immediately. On the next restart, that file takes the Tier 1 fast-path
rather than being health-checked again. The system converges toward maximum efficiency.

8.  Run Journal — RunJournal Class
The RunJournal class manages the JSONL journal file. It is instantiated once per run
and lives for the lifetime of the engine.

8.1  File naming
video_repair_journal__<src_slug>__<dst_slug>__<timestamp>.jsonl

# Example:
video_repair_journal__G_video__C_temp__20260503-1037.jsonl

# Slug: sanitise path to safe filename chars, max 48 chars
# Timestamp: YYYYMMDD-HHMMSS from run start

On startup, the engine scans the destination for journals matching the current
src_slug + dst_slug pair (ignoring the timestamp) and loads the newest one.
If more than one exists with the same slug but different timestamps, the newest
(by mtime) is loaded. Error if two share an identical timestamp string.

8.2  JSONL format — one JSON object per line
{
  "ts":       "2026-05-03T10:52:11",  // ISO timestamp of completion
  "run":      "20260503-1037",         // Run identifier (start timestamp)
  "rel":      "Court Jester The.mkv", // Relative path in destination
  "src":      "G:\\video\\Court Jester The.mkv",
  "status":   "ok",                   // ok | repaired | partial | failed | skipped | done
  "size":     1047527424,             // Source file size in bytes
  "sha256":   "a3f8b2...",            // SHA-256 of the destination file
  "elapsed":  1204.2,                 // Seconds taken
  "strategy": "stream-copy",          // Repair strategy used (empty if no repair)
  "issues":   [                       // Empty array if no issues
    {"severity":"warning","code":"mp4_moov_at_end","repaired":true,"note":"..."}
  ]
}

8.3  Trusted statuses
Only entries with these statuses are indexed and used for the Tier 1 fast-path:
ok  repaired  done

Entries with partial, failed, or skipped are ignored on load — those files
will be re-attempted on every resume run until they either succeed or are given up on manually.

8.4  Append-then-consolidate pattern
During a run, each completed file is appended to the journal immediately using an fsync'd write. This ensures the journal is accurate up to the last completed
file even if the process is killed mid-run.

At the end of a clean run, consolidate() merges all entries and writes a single
fresh file using write-to-temp / atomic rename:
- Write all merged entries to 
- fsync the temp file
- Rename temp file over the current journal (atomic on same volume)
- Delete the previously loaded journal if it was a different file

A crash at step 1-2 leaves the old journal intact. A crash at step 3-4 leaves a
harmless orphan .tmp file. No data is ever lost.

9.  Output & Status Codes
9.1  Per-file status codes
Status
Colour
Meaning
ok
Green
Copied cleanly — no issues detected
repaired
Blue
Issue found and fully fixed — verify playback recommended
partial
Yellow
Repair attempted; some errors remain — manual inspection needed
failed
Red
All repair strategies exhausted — needs specialist tools
skipped
Red
Zero-byte source file — nothing to recover
done
Dim
Already exists in destination and was certified (journal or health-check)

9.2  Console progress format
  [████████████────────────────] 1457/4357    OK        Court Jester The.mkv      2.8 GB
  [████████████────────────────] 1458/4357  REPAIRED    Ran.mkv                   1.1 GB  [remux]
               ✕ mp4_no_moov: moov atom not found — recording likely interrupted
                 ↻ repaired  Stream-copy remux succeeded (lossless)

The spinner line (during subprocess calls):
  ⠼  ffmpeg decode-probe       47.3s  [h264 @ 0x7f...] missing picture in access unit

The spinner uses \r to overwrite a single line. Log output above is never disturbed.
On completion the spinner line is cleared with spaces before the file result line prints.

9.3  Files written to destination
File
Purpose
video_repair.log
Human-readable append log; opened with mode='a' so it accumulates across restarts
video_repair_journal__*.jsonl
Machine-readable JSONL; one per run; consolidated at clean finish

10.  Platform-Specific Details
10.1  Linux
- Tool discovery: 
- Colour: ANSI codes work in all modern Linux terminals; no special setup needed
- Re-encode: libx264 software only by default; NVENC requires manual ffmpeg build
- Install: 

10.2  macOS
- Tool discovery: checks 
- Apple Silicon PATH fix: if ffmpeg not found, prints 
- VideoToolbox: detected via 
- Gatekeeper: if ffmpeg is quarantined, suggests 
- Case-insensitive dedup: HFS+/APFS is case-insensitive; dedup keys are lowercased
- Path expansion: 
- Install: 

10.3  Windows
- ANSI colour: enabled via 

- Tool discovery: checks Chocolatey, Scoop (per-user and global), common manual paths, and Program Files\MKVToolNix
- Glob handling: cmd.exe and PowerShell do NOT expand 
- UNC paths: 
- BOM-safe list files: 
- CREATE_NO_WINDOW: passed to all subprocess calls to prevent console flash
- GPU encoders: NVENC → AMF → QSV → libx264 in that preference order
- Install: 

11.  CLI Flags
Flag
Default
Behaviour
--dry-run
off
Probe and report without writing any files. Safe on read-only media. Journal is not written.
--resume
off
Activate two-tier skip system: journal fast-path first, then destination health-check.
--verbose
off
Print detail lines for every file, not just files with issues.
--ext .mp4,.mkv
all video
Only process files with listed extensions. Dot prefix optional.
--list FILE
(none)
Read source paths/globs from a text file. One per line. # = comment.
--no-reencode
off
Never attempt re-encode as last resort. Use when quality loss is unacceptable.
--no-hwaccel
off
Force libx264/SW encode. Useful for reproducible output. macOS/Windows only.
--recursive
on
Recurse into sub-directories when given a directory source.
--no-recursive
(off)
Do not recurse. Only files directly in the given directory.
--no-colour
off
Disable ANSI colour. Windows only flag. Auto-disabled on non-TTY stdout.

12.  Issue Codes Reference
All formats
Code
Severity
Description
zero_byte
error
File is zero bytes — nothing to inspect or recover
stat_error
error
Cannot stat file — OS-level read error
read_error
error
Cannot read file bytes — permissions or disk error
decode_errors
warning/error
ffmpeg reported decode errors during full probe
decode_probe_timeout
warning
Full decode probe timed out — file very large or severely corrupt

MP4 / MOV
Code
Severity
Description
mp4_unknown_box
warning
First 4-byte atom type is not a recognised MP4 atom
mp4_no_moov
error
moov atom not found — recording interrupted before finalisation
mp4_moov_at_end
warning
moov atom at end of file — not streamable; repair relocates it
mp4_mdat_only
warning
mdat block found but no moov — partial recovery may be possible
zero_duration
error
Duration is 0 — container metadata missing or corrupt
negative_duration
error
Negative duration — corrupt container metadata
zero_bitrate
warning
Bitrate reported as 0 — index may be missing
unknown_video_codec
error
Video codec not identified
zero_dimensions
error
Video dimensions are 0x0 — stream header corrupt
unusual_fps
warning
Frame rate > 240 fps — metadata may be corrupt
ffprobe_failed
error
ffprobe could not read file at all
ffprobe_error
error
ffprobe reported an error in its JSON output
no_streams
error
File contains no audio or video streams
no_video_stream
warning
No video stream — audio-only or stripped container
too_small
error
File smaller than minimum valid container size

MKV / WebM
Code
Severity
Description
mkv_bad_ebml
error
Missing EBML header — not a valid Matroska file
mkv_no_tracks
error
No tracks found in MKV structure
mkvmerge_error
error
mkvmerge reported a structural error (exit code 2)
mkvmerge_warning
warning
mkvmerge reported warnings (exit code 1)
mkvmerge_identify_failed
warning
mkvmerge could not identify the file

AVI / WMV
Code
Severity
Description
avi_bad_signature
error
Missing RIFF/AVI signature in first 12 bytes
wmv_bad_signature
warning
Missing ASF GUID header — may not be valid WMV

13.  Dependencies
External tools
Tool
Required
Used for
Install
ffmpeg
Yes
Remux, repair, re-encode, full decode probe
apt install ffmpeg / brew install ffmpeg / winget install FFmpeg
ffprobe
Yes
Metadata inspection (installed with ffmpeg)
(same as ffmpeg)
mkvmerge
Optional
MKV structure rebuild; better MKV recovery than ffmpeg
apt install mkvtoolnix / brew install mkvtoolnix / winget install MKVToolNix
mkvinfo
Optional
Part of MKVToolNix
(same as mkvmerge)
mkvpropedit
Optional
Part of MKVToolNix
(same as mkvmerge)

Python packages
Package
Required
Used for
pymkv2
Optional
Enhanced MKV inspection; gracefully absent if not installed

Python standard library only — no other pip installs needed
The scripts use only: argparse hashlib json logging os pathlib re shutil struct subprocess sys tempfile threading time dataclasses datetime typing

14.  Companion Scripts
Script
Purpose
file_copy_repair.py
Copies and repairs images (JPEG/PNG/GIF/WebP), PDFs, and Office documents (DOCX/XLSX). Same input model. Pure Python — no external tools required. Uses Pillow, pypdf, zipfile.
readiness_check.py
Probes all dependencies and prints PASS/FAIL/WARN per item. Also detects hardware encoders. Exit codes: 0=READY 1=NOT READY 2=READY WITH WARNINGS. Supports --json for machine-readable output.
setup_guide.docx
9-section Word document covering tool installation for all three platforms, command reference, status code meanings, troubleshooting, and a quick-reference card.

file_copy_repair.py checks
Format
Library
Checks performed
JPEG
Pillow
PIL verify, pixel decode, EOI marker check (0xFFD9 at end)
PNG
Pillow + struct/zlib
PIL verify, pixel decode, per-chunk CRC validation and correction
PDF
pypdf
Header signature, EOF marker, page-tree parse, text extraction test
DOCX / XLSX
zipfile
ZIP integrity test, required OOXML member presence
All
hashlib
Zero-byte check, SHA-256 stored in log

15.  Build Instructions
The following instructions are addressed to the LLM or developer building this tool.

15.1  Produce three scripts
Build video_copy_repair_linux.py, video_copy_repair_macos.py, and
video_copy_repair_windows.py as described. They share all logic but differ in
tool discovery, hardware acceleration, ANSI handling, and path handling as detailed above.

!
Do not merge until all three are independently verified
The three-script approach was chosen deliberately so each can be tested on its native
platform before merging. Build and test each separately. The merge to a single
video_copy_repair.py with platform.system() dispatch is a planned future step.

15.2  Key implementation constraints
- Python 3.10+ minimum. Use walrus operator (:=) freely. Use match/case if it helps.
- run_watched()
- All subprocess calls must use 
- The journal 
- The journal 
- _inspect()
- Repair strategies must write to a temp file; only move to final destination on success
- The 95% size threshold in 
- Journal trusted statuses are exactly: 
- On Windows, 

15.3  Structural outline
Each script should follow this top-level structure:
- r"""
- Standard library imports
- ANSI colour constants and 
- Video format extension sets (
- @dataclass Issue
- class RunJournal
- Platform tool discovery (
- Hardware acceleration detection (
- _subprocess_kwargs()
- ffprobe_json()
- Container signature checks: 
- ffprobe checks: 
- Format-specific checks: 
- Repair functions: 
- attempt_video_repair()
- Helpers: 
- Input resolution: 
- class VideoRepairEngine
- def main()

15.4  Testing checkpoints
After building each script, verify these scenarios in order:
- Syntax check: 
- Help text: 
- Fresh copy of 1 healthy file — should show OK, write journal with 1 line
- Fresh copy of 1 file with moov at end — should show REPAIRED
- Zero-byte file — should show SKIPPED
- --resume
- --resume
- Run 1: copy 3 files. Run 2: --resume same 3 + 1 new file. 3 should be journal fast-path; 1 should copy
- Interrupt mid-run (Ctrl+C). Restart with --resume. Should continue from where it stopped

15.5  Common pitfalls to avoid
- Never use 
- Never call 
- Never modify the journal file in place — always write-to-temp / atomic rename in consolidate()
- Never mark 
- On Windows: always pass 
- On macOS: check 
- The 
- Do not hardcode 300s or 600s timeouts — always use 
- The module docstring must be 

16.  Future Work & Known Limitations
Planned
- Merge all three platform scripts into a single 
- Tkinter GUI wrapper using the same engine — progress bar, file list, log panel
- Report generation: 
- SHA-256 verification mode: compare source and destination checksums post-copy
- Email/webhook notification on completion for unattended overnight runs

Known limitations
- Severely truncated MP4 files (missing large mdat chunk) cannot be recovered by open-source tools — commercial tools like Stellar Repair may help
- Files with corrupt codec stream data (not just container) cannot be repaired — only the container can be rebuilt
- Linux re-encode is software only unless NVENC is manually compiled into ffmpeg
- The journal slug truncates at 48 chars — two very long paths that differ only after position 48 could collide (extremely unlikely in practice)
- The 95% size threshold in the health-check assumes the destination was written by this script — externally re-encoded files with different codecs will always fail the size check and be re-copied

17.  Quick Reference — All Commands
# ── Readiness check ─────────────────────────────────────────────────────
python3 readiness_check.py
python3 readiness_check.py --platform windows
python3 readiness_check.py --json

# ── First run (Linux) ───────────────────────────────────────────────────
python3 video_copy_repair_linux.py  /nas/Videos/  /backup/  --dry-run
python3 video_copy_repair_linux.py  /nas/Videos/  /backup/

# ── Resume after interruption ────────────────────────────────────────────
python3 video_copy_repair_linux.py  /nas/Videos/  /backup/  --resume

# ── Single file ──────────────────────────────────────────────────────────
python3 video_copy_repair_linux.py  "My Film.mkv"  /backup/

# ── Glob ─────────────────────────────────────────────────────────────────
python3 video_copy_repair_linux.py  "/nas/*.mkv"  /backup/

# ── List file ────────────────────────────────────────────────────────────
python3 video_copy_repair_linux.py  --list  files.txt  /backup/

# ── Filter extensions, no re-encode ─────────────────────────────────────
python3 video_copy_repair_linux.py  /nas/  /backup/  --ext .mkv,.mp4  --no-reencode

# ── Windows equivalents ──────────────────────────────────────────────────
python  video_copy_repair_windows.py  G:\Videos\  E:\Backup\
python  video_copy_repair_windows.py  G:\Videos\  E:\Backup\  --resume
python  video_copy_repair_windows.py  G:\Videos\  E:\Backup\  --no-colour  > run.log

# ── macOS equivalents ────────────────────────────────────────────────────
python3 video_copy_repair_macos.py  /Volumes/NAS/Videos/  ~/backup/
python3 video_copy_repair_macos.py  /Volumes/NAS/Videos/  ~/backup/  --resume  --no-hwaccel
