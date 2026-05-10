Video Copy + Repair Utility
Setup & Operation Guide

Covers all three platform scripts: Linux, macOS, and Windows.
Includes tool installation, Python environment setup, first-run verification,
command reference, and troubleshooting for common issues.

Generated Sun May 03 2026

1.  Overview
The Video Copy + Repair utility copies video files from any source (single files, directories, glob patterns, or list files) to a destination directory, inspecting each file for structural corruption along the way and attempting repairs before writing the output.

Three separate scripts are provided — one per platform — sharing identical logic but with platform-appropriate tool discovery, hardware acceleration, and path handling. They will be merged into a single unified script in a future release.

Platform
Script
Package Manager
Python
Min OS
Linux
video_copy_repair_linux.py
apt / dnf / pacman
3.10+
Ubuntu 20.04+
macOS
video_copy_repair_macos.py
Homebrew
3.10+
Ventura 13+
Windows
video_copy_repair_windows.py
winget / choco / scoop
3.10+
Windows 10 1511+

i
File + repair scripts
This guide covers the VIDEO scripts only. The companion file_copy_repair.py script
handles images, PDFs, and Office documents using different libraries (Pillow, pypdf, zipfile).
Its setup requirements are a strict subset of those listed here.

2.  Prerequisites
The scripts have two layers of dependencies: external command-line tools (ffmpeg, MKVToolNix) and Python packages. All external tools must be installed and on PATH before running the scripts.
2.1  Python
Python 3.10 or later is required on all platforms. Python 3.12 is recommended.

# Check your version
python3 --version          # Linux / macOS
python --version           # Windows

If Python is not installed:
- Linux: sudo apt install python3  (or use your distro's package manager)
- macOS: brew install python  (after Homebrew is installed)
- Windows: winget install Python.Python.3.12  or download from python.org

!
Windows PATH for Python
The Python installer on Windows offers to 'Add Python to PATH' — always tick this box.
Without it, python and pip commands will not work from cmd.exe or PowerShell.
2.2  FFmpeg (required)
FFmpeg provides both ffmpeg (repair/re-encode) and ffprobe (inspection). Both binaries are needed. They are always installed together.

Linux
sudo apt install ffmpeg             # Debian / Ubuntu / Raspberry Pi OS
sudo dnf install ffmpeg             # Fedora (may need RPM Fusion repo)
sudo pacman -S ffmpeg               # Arch / Manjaro
sudo zypper install ffmpeg          # openSUSE

macOS
# Step 1 — install Homebrew if not already present
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Step 2 — install ffmpeg
brew install ffmpeg

# Apple Silicon note: Homebrew installs to /opt/homebrew/bin
# Add this to ~/.zshrc if ffmpeg is not found after install:
export PATH="/opt/homebrew/bin:$PATH"

>
Gatekeeper on macOS
If macOS blocks ffmpeg with 'cannot be opened because the developer cannot be verified', run:
sudo xattr -rd com.apple.quarantine /opt/homebrew/bin/ffmpeg

Windows
Choose one package manager — all three install the same ffmpeg build:

# Option A — winget (built in to Windows 10 2004+ / Windows 11)
winget install "FFmpeg (Essentials Build)"

# Option B — Chocolatey  (https://chocolatey.org — requires admin PowerShell)
choco install ffmpeg

# Option C — Scoop  (https://scoop.sh — no admin needed)
scoop install ffmpeg

# Option D — Manual
# Download from: https://www.gyan.dev/ffmpeg/builds/
# Extract to C:\ffmpeg\  then add C:\ffmpeg\bin to your System PATH

!
Windows PATH for ffmpeg
After a manual install, you must add ffmpeg\bin to PATH manually:
System Properties > Advanced > Environment Variables > Path > New.
Winget, Chocolatey, and Scoop handle this automatically.
2.3  MKVToolNix (optional — required for MKV repair)
MKVToolNix provides mkvmerge, which can rebuild Matroska structures that ffmpeg cannot recover. Install it if you work with .mkv or .webm files.

# Linux
sudo apt install mkvtoolnix         # Debian / Ubuntu
sudo dnf install mkvtoolnix         # Fedora

# macOS
brew install mkvtoolnix

# Windows
winget install MKVToolNix
# or: choco install mkvtoolnix
# or: download installer from https://mkvtoolnix.download/
2.4  Python packages
One optional package extends inspection capability for MKV files:

pip3 install pymkv2                 # Linux / macOS
pip install pymkv2                  # Windows

i
Virtual environments
If you prefer to isolate dependencies, create a venv first:
python3 -m venv venv && source venv/bin/activate   # Linux / macOS
python -m venv venv && venv\Scripts\activate        # Windows

3.  Verification — Readiness Checker
Before running the repair scripts on real files, run the readiness checker. It probes every dependency, reports versions, detects hardware encoders, and prints a clear PASS / FAIL summary.

python3 readiness_check.py          # Linux / macOS
python  readiness_check.py          # Windows

# Restrict check to one platform
python3 readiness_check.py --platform linux
python3 readiness_check.py --platform macos
python3 readiness_check.py --platform windows

A successful run looks like:

  READINESS CHECK — Video Copy + Repair
  ──────────────────────────────────────────────────────────────────────
  [PASS]  Python 3.12.3 — 3.10+ required
  [PASS]  ffmpeg 6.1.1 — /usr/bin/ffmpeg
  [PASS]  ffprobe 6.1.1 — /usr/bin/ffprobe
  [PASS]  mkvmerge 82.0 — /usr/bin/mkvmerge
  [INFO]  pymkv2 installed
  [INFO]  Hardware encoder: none detected (will use libx264)
  ──────────────────────────────────────────────────────────────────────
  Result: READY  — all required tools present

4.  Running the Scripts
4.1  Basic usage
The last positional argument is always the destination directory. Everything before it is a source (file, directory, or glob):

# Linux / macOS
python3 video_copy_repair_linux.py  /media/drive/Videos/  /backup/
python3 video_copy_repair_macos.py  /Volumes/NAS/Videos/  ~/backup/

# Windows (cmd.exe or PowerShell)
python  video_copy_repair_windows.py  D:\Videos\  E:\Backup\
4.2  Input modes
All three scripts accept the same flexible input syntax:

# Single file
python3 video_copy_repair_linux.py  clip.mp4  /backup/

# Glob pattern (quote to prevent shell expansion on Linux/macOS)
python3 video_copy_repair_linux.py  "/nas/*.mkv"  /backup/

# Multiple mixed sources
python3 video_copy_repair_linux.py  a.mp4  b.mkv  /recordings/*.avi  /backup/

# List file (one path or glob per line, # = comment)
python3 video_copy_repair_linux.py  --list  my_files.txt  /backup/

i
Windows glob note
On Windows, cmd.exe and PowerShell do NOT expand *.mp4 before passing to Python.
Quoting is optional on Windows — both '*.mp4' and *.mp4 work correctly.
On Linux/macOS, always quote glob patterns to prevent the shell expanding them.
4.3  Command-line options
Option
Description
--dry-run
Probe and report all issues without writing any output files. Safe to run on read-only media.
--verbose
Print details for every file processed, not just files with problems.
--ext .mp4,.mkv
Only process files with the listed extensions. Comma-separated, dot prefix optional.
--list FILE
Read source paths/globs from a text file. One entry per line. Lines starting with # are ignored.
--no-reencode
Skip the re-encode (last-resort) repair strategy. Use when you cannot accept any quality loss.
--no-hwaccel
Force libx264 software encode. Useful for reproducible output or when GPU encoders behave unexpectedly.
--no-recursive
Do not recurse into sub-directories when a directory is given as source.
--no-colour
(Windows only) Disable ANSI colour output — useful when piping to a file or running in older terminals.

5.  Understanding the Output
5.1  File status codes
Status
Meaning
OK
File passed all checks — copied as-is
REPAIRED
Issue found and successfully fixed — verify playback
PARTIAL
Repair attempted; some errors remain — manual inspection needed
FAILED
All repair strategies exhausted — file needs specialist tools
SKIPPED
Zero-byte file — nothing to recover

Files marked PARTIAL or FAILED are still copied to the destination (where possible) so that specialist recovery tools can be applied manually. They are never silently dropped.
5.2  Repair strategies
When a file has errors, the script attempts repairs in order — stopping as soon as one succeeds:

#
Strategy
What it does
Quality
1
Stream-copy remux
Rebuilds container index — fixes moov atom, cue positions, broken headers
Lossless — identical bitstream
2
Moov faststart
MP4/MOV only — relocates moov atom to front so file is streamable
Lossless
3
MKV remux via mkvmerge
Rebuilds Matroska structure using MKVToolNix — often recovers where ffmpeg cannot
Lossless
4
Re-encode (last resort)
Full transcode using best available GPU encoder, then SW fallback
Near-lossless (CRF 18 / VBR CQ 18). Slow on large files.

!
Re-encode quality
Re-encoding (strategy 4) introduces generational quality loss even at CRF 18.
If the original source still exists elsewhere, sourcing a fresh copy is always preferable.
Use --no-reencode to prevent this strategy from being attempted.
5.3  The repair log
Every run writes a video_repair.log file to the destination directory containing:
- Timestamp and status for every file processed
- SHA-256 checksum of each output file (for integrity verification)
- Full source path and destination relative path
- Issue count per file

The log is plain UTF-8 text and can be opened in any text editor or imported into a spreadsheet.

6.  Hardware Acceleration
Re-encoding is only used as a last resort, but when it is triggered, the scripts automatically use the fastest available encoder:

Linux
- No automatic GPU detection. Uses libx264 software encode by default.
- To enable NVENC on Linux: install nvidia-cuda-toolkit and an ffmpeg build with NVENC support.
macOS
- VideoToolbox (Apple hardware encoder) is detected and used automatically on all Apple Silicon (M1/M2/M3/M4) and Intel Macs with T2 chip.
- Falls back to libx264 if VideoToolbox is unavailable.
- Use --no-hwaccel to force software encode.
Windows
- Nvidia NVENC, AMD AMF, and Intel QuickSync (QSV) are detected in that preference order.
- Falls back through each automatically if the preferred encoder fails.
- Final fallback is always libx264 software encode.
- Use --no-hwaccel to skip GPU detection entirely.

7.  Troubleshooting
'ffmpeg not found' or 'ffprobe not found'
The scripts check PATH and known install locations. If the tool is installed but not found:
- Linux / macOS: run which ffmpeg to confirm the install path. Ensure /usr/bin or /opt/homebrew/bin is in your PATH.
- macOS Apple Silicon: add export PATH="/opt/homebrew/bin:$PATH" to ~/.zshrc and restart your terminal.
- Windows: verify that the ffmpeg\bin directory is in your System PATH (not just User PATH). Restart your terminal after changing PATH.

# Confirm ffmpeg is reachable from your shell
ffmpeg -version
ffprobe -version
Colour codes appear as garbage characters (e.g. ^[[32m)
This happens in terminals that do not support ANSI escape codes:
- Windows: use Windows Terminal instead of the old cmd.exe, or run with --no-colour
- Piping to a file: always add --no-colour when redirecting output
- Git Bash: ANSI usually works, but if not, run python3 -c "import sys; print(sys.platform)" to confirm you are using Windows Python
MKV repair not attempted
MKV repair via mkvmerge is only available if MKVToolNix is installed. Run the readiness checker to confirm:
python3 readiness_check.py
If mkvmerge is missing, install MKVToolNix (see section 2.3). The scripts will fall back to ffmpeg remux for MKV files when mkvmerge is absent.
Re-encode is very slow
Re-encoding (strategy 4) is CPU-intensive for large files. On a machine with a compatible GPU:
- macOS: VideoToolbox is used automatically — no action needed.
- Windows: ensure your Nvidia / AMD / Intel drivers are up to date. NVENC requires GeForce GTX 600 series or newer.
- Linux: software encode only unless you manually configure NVENC support in ffmpeg.
For very large libraries of severely corrupt files, consider running overnight.
File shows PARTIAL after repair
PARTIAL means the script attempted all applicable strategies but at least one structural error could not be resolved. Common causes:
- Missing moov atom in an MP4 where the mdat block is also damaged — the container index cannot be rebuilt from incomplete data.
- Matroska file with corrupt codec private data — the stream metadata is unrecoverable without the original recording.
- File truncated mid-GOP (Group of Pictures) — the decoder cannot reconstruct the missing reference frames.

For PARTIAL files, consider specialist commercial tools (Stellar Repair for Video, Wondershare Repairit) or, if the recording device is available, attempt re-capture.

8.  Companion Script — file_copy_repair.py
A separate script handles non-video file types: images (JPEG, PNG, GIF, WebP), PDF documents, and Office files (DOCX, XLSX, PPTX). It shares the same input model (single files, directories, globs, list files) and produces an identical log format.

Additional dependencies for file_copy_repair.py
pip3 install pillow pypdf python-docx openpyxl   # Linux / macOS
pip  install pillow pypdf python-docx openpyxl   # Windows

No external command-line tools are required — all checks and repairs are performed in pure Python.

9.  Quick Reference
# ── Readiness check ──────────────────────────────────────────────────
python3 readiness_check.py

# ── Dry run (no files written) ───────────────────────────────────────
python3 video_copy_repair_linux.py   /source/  /dest/  --dry-run

# ── Copy a whole directory ───────────────────────────────────────────
python3 video_copy_repair_linux.py   /media/Videos/  /backup/

# ── Glob — all MP4s in a folder ──────────────────────────────────────
python3 video_copy_repair_linux.py   "/nas/recordings/*.mp4"  /backup/

# ── Single file ──────────────────────────────────────────────────────
python3 video_copy_repair_linux.py   holiday.mkv  /backup/

# ── Multiple mixed sources ───────────────────────────────────────────
python3 video_copy_repair_linux.py   a.mp4  b.mkv  /archive/*.avi  /backup/

# ── List file ────────────────────────────────────────────────────────
python3 video_copy_repair_linux.py   --list files.txt  /backup/

# ── Only MKV and MP4, skip re-encode ─────────────────────────────────
python3 video_copy_repair_linux.py   /source/  /dest/  --ext .mkv,.mp4  --no-reencode

# ── Verbose — show all files, not just problems ───────────────────────
python3 video_copy_repair_linux.py   /source/  /dest/  --verbose

# ── Windows equivalents (replace script name; paths use backslash) ────
python  video_copy_repair_windows.py  D:\Videos\  E:\Backup\
python  video_copy_repair_windows.py  "D:\Videos\*.mkv"  E:\Backup\  --no-colour
