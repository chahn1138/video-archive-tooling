#!/usr/bin/env python3
"""
readiness_check.py
------------------
Probes every dependency required by the Video Copy + Repair scripts and
prints a clear PASS / FAIL / INFO / WARN summary.  Run this before your
first use on a new machine, after an OS upgrade, or whenever a repair
script reports a missing tool.

Usage
-----
    python3 readiness_check.py                # auto-detect platform
    python3 readiness_check.py --platform linux
    python3 readiness_check.py --platform macos
    python3 readiness_check.py --platform windows
    python3 readiness_check.py --no-colour    # plain text output
    python3 readiness_check.py --json         # machine-readable JSON

Exit codes
----------
    0  All required tools present (READY)
    1  One or more required tools missing (NOT READY)
    2  Required tools present but optional tools or packages missing (READY WITH WARNINGS)
"""

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ── ANSI colour ────────────────────────────────────────────────────────────────

_colour_on = True

def _init_colour():
    global _colour_on
    if sys.platform == "win32":
        try:
            import ctypes, ctypes.wintypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            m = ctypes.wintypes.DWORD()
            if k.GetConsoleMode(h, ctypes.byref(m)):
                k.SetConsoleMode(h, m.value | 0x0004)
                _colour_on = True
        except Exception:
            _colour_on = False
    else:
        _colour_on = sys.stdout.isatty()

_init_colour()

RESET  = "\033[0m"; BOLD   = "\033[1m"; DIM    = "\033[2m"
GREEN  = "\033[32m"; YELLOW = "\033[33m"; RED   = "\033[31m"
CYAN   = "\033[36m"; BLUE   = "\033[34m"; MAGENTA = "\033[35m"

def c(text, colour):
    return f"{colour}{text}{RESET}" if _colour_on else text

# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    level:   str          # PASS | FAIL | WARN | INFO | SKIP
    label:   str          # short name shown in output
    detail:  str          # one-line description
    version: str = ""     # version string if detected
    path:    str = ""     # binary path if found
    hint:    str = ""     # install suggestion shown on FAIL/WARN

# ── Subprocess helper ──────────────────────────────────────────────────────────

def run(cmd: List[str], timeout: int = 10) -> Tuple[int, str, str]:
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, **kwargs)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", "not found"
    except Exception as e:
        return -1, "", str(e)

# ── Tool finder — platform-aware ───────────────────────────────────────────────

WINDOWS_PATHS = [
    r"C:\ProgramData\chocolatey\bin",
    os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
    r"C:\ProgramData\scoop\shims",
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
    r"C:\Program Files\MKVToolNix",
    r"C:\Program Files (x86)\MKVToolNix",
]

MACOS_PATHS = [
    "/opt/homebrew/bin",    # Apple Silicon
    "/usr/local/bin",       # Intel Homebrew
    "/opt/local/bin",       # MacPorts
]

def find_tool(name: str) -> Optional[str]:
    """Find a tool binary, checking platform-specific locations before PATH."""
    exe = name + ".exe" if sys.platform == "win32" else name

    if sys.platform == "win32":
        for d in WINDOWS_PATHS:
            p = Path(d) / exe
            if p.exists() and os.access(p, os.X_OK):
                return str(p)
    elif sys.platform == "darwin":
        for d in MACOS_PATHS:
            p = Path(d) / exe
            if p.exists() and os.access(p, os.X_OK):
                return str(p)

    return shutil.which(exe) or shutil.which(name)

# ── Individual checks ──────────────────────────────────────────────────────────

def check_python() -> CheckResult:
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    path = sys.executable
    if vi >= (3, 10):
        return CheckResult("PASS", "Python", f"version {ver}", ver, path)
    return CheckResult("FAIL", "Python", f"version {ver} — 3.10+ required", ver, path,
                       hint="Download from python.org or use your package manager")

def check_tool(name: str, label: str,
               version_flag: str = "--version",
               parse_first_line: bool = True,
               required: bool = True,
               hint: str = "") -> CheckResult:
    path = find_tool(name)
    if not path:
        level = "FAIL" if required else "WARN"
        return CheckResult(level, label, "not found", path=path, hint=hint)

    rc, out, err = run([path, version_flag])
    raw = (out + err).strip()
    first_line = raw.splitlines()[0] if raw else ""
    # Extract a short version number
    import re
    m = re.search(r"(\d+\.\d+[\.\d]*)", first_line)
    ver = m.group(1) if m else "?"

    return CheckResult("PASS", label, f"version {ver}", ver, path)

def check_ffmpeg() -> CheckResult:
    return check_tool("ffmpeg", "ffmpeg", "-version", required=True,
                      hint="Install: apt install ffmpeg  /  brew install ffmpeg  /  winget install \"FFmpeg (Essentials Build)\"")

def check_ffprobe() -> CheckResult:
    return check_tool("ffprobe", "ffprobe", "-version", required=True,
                      hint="Installed alongside ffmpeg — if ffprobe is missing, reinstall ffmpeg")

def check_mkvmerge() -> CheckResult:
    return check_tool("mkvmerge", "mkvmerge", "--version", required=False,
                      hint="Install: apt install mkvtoolnix  /  brew install mkvtoolnix  /  winget install MKVToolNix")

def check_mkvinfo() -> CheckResult:
    return check_tool("mkvinfo", "mkvinfo", "--version", required=False,
                      hint="Part of MKVToolNix — install MKVToolNix to get mkvinfo")

def check_mkvpropedit() -> CheckResult:
    return check_tool("mkvpropedit", "mkvpropedit", "--version", required=False,
                      hint="Part of MKVToolNix — install MKVToolNix to get mkvpropedit")

def check_python_package(pkg_import: str, pkg_name: str,
                         required: bool = False,
                         hint: str = "") -> CheckResult:
    import importlib.util as _ilu
    import importlib.metadata as _ilm
    spec = _ilu.find_spec(pkg_import)
    if spec is None:
        level = "FAIL" if required else "INFO"
        msg   = f"not installed — {pkg_name}"
        return CheckResult(level, pkg_name, msg,
                           hint=hint or f"pip install {pkg_name}")
    try:
        ver = _ilm.version(pkg_name)
    except Exception:
        ver = "installed"
    return CheckResult("PASS", pkg_name, f"version {ver}", ver)

def check_pymkv2() -> CheckResult:
    return check_python_package("pymkv", "pymkv2", required=False,
                                hint="pip install pymkv2  (optional — enhances MKV inspection)")

def check_pillow() -> CheckResult:
    return check_python_package("PIL", "Pillow", required=False,
                                hint="pip install pillow  (required for file_copy_repair.py image checks)")

def check_pypdf() -> CheckResult:
    return check_python_package("pypdf", "pypdf", required=False,
                                hint="pip install pypdf  (required for file_copy_repair.py PDF checks)")

# ── Hardware encoder detection ─────────────────────────────────────────────────

def check_hw_encoders() -> List[CheckResult]:
    results = []
    ffmpeg_path = find_tool("ffmpeg")
    if not ffmpeg_path:
        return [CheckResult("SKIP", "HW encoders",
                            "ffmpeg not found — cannot detect hardware encoders")]

    rc, out, err = run([ffmpeg_path, "-hide_banner", "-encoders"], timeout=10)
    combined = out + err

    encoders = {
        "h264_nvenc":       ("Nvidia NVENC",        "Windows / Linux — requires GeForce GTX 600+ or RTX"),
        "h264_amf":         ("AMD AMF",              "Windows — requires Radeon RX 400+ or recent APU"),
        "h264_qsv":         ("Intel QuickSync",      "Windows / Linux — requires 6th-gen Core (Skylake)+"),
        "h264_videotoolbox":("Apple VideoToolbox",   "macOS — available on all Apple Silicon and T2 Intel Macs"),
        "libx264":          ("libx264 (software)",   "Always available — universal fallback"),
    }

    found_any_hw = False
    for enc_name, (label, note) in encoders.items():
        if enc_name in combined:
            is_hw = enc_name != "libx264"
            if is_hw:
                found_any_hw = True
            results.append(CheckResult(
                "INFO" if not is_hw else "PASS",
                label,
                f"available — {note}",
                path=enc_name
            ))

    if not found_any_hw:
        results.append(CheckResult("INFO", "HW encoders",
                                   "none detected — re-encode will use libx264 software"))
    return results

# ── macOS-specific checks ──────────────────────────────────────────────────────

def check_macos_gatekeeper() -> Optional[CheckResult]:
    """Warn if ffmpeg may be quarantined by Gatekeeper."""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None
    rc, out, err = run(["xattr", "-l", ffmpeg], timeout=5)
    if "com.apple.quarantine" in (out + err):
        return CheckResult("WARN", "Gatekeeper",
                           f"ffmpeg at {ffmpeg} is quarantined — it may be blocked",
                           hint="Run: sudo xattr -rd com.apple.quarantine " + ffmpeg)
    return CheckResult("PASS", "Gatekeeper", "ffmpeg is not quarantined")

def check_homebrew() -> Optional[CheckResult]:
    brew = shutil.which("brew") or \
           ("/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").exists() else None) or \
           ("/usr/local/bin/brew"    if Path("/usr/local/bin/brew").exists()    else None)
    if not brew:
        return CheckResult("WARN", "Homebrew",
                           "not found — required to install ffmpeg and mkvtoolnix on macOS",
                           hint='Install: /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
    rc, out, err = run([brew, "--version"], timeout=10)
    ver_line = (out + err).strip().splitlines()[0] if (out + err).strip() else "?"
    return CheckResult("PASS", "Homebrew", ver_line)

# ── Windows-specific checks ────────────────────────────────────────────────────

def check_windows_ansi() -> CheckResult:
    """Check whether the current terminal supports ANSI colour codes."""
    try:
        import ctypes, ctypes.wintypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        m = ctypes.wintypes.DWORD()
        if k.GetConsoleMode(h, ctypes.byref(m)):
            if m.value & 0x0004:
                return CheckResult("PASS", "ANSI colour", "VT processing already enabled")
            # Try enabling it
            if k.SetConsoleMode(h, m.value | 0x0004):
                return CheckResult("PASS", "ANSI colour", "VT processing enabled successfully")
        return CheckResult("WARN", "ANSI colour",
                           "could not enable VT processing — use --no-colour or Windows Terminal",
                           hint="Switch to Windows Terminal for full colour support")
    except Exception as e:
        return CheckResult("WARN", "ANSI colour", f"check failed: {e}")

def check_windows_python_path() -> CheckResult:
    """Warn if python or pip are not reachable from PATH."""
    py  = shutil.which("python") or shutil.which("python3")
    pip = shutil.which("pip")    or shutil.which("pip3")
    if py and pip:
        return CheckResult("PASS", "Python PATH", f"python and pip found on PATH")
    missing = []
    if not py:  missing.append("python")
    if not pip: missing.append("pip")
    return CheckResult("WARN", "Python PATH",
                       f"not on PATH: {', '.join(missing)}",
                       hint="Reinstall Python and tick 'Add Python to PATH'")

# ── Platform dispatch ──────────────────────────────────────────────────────────

def gather_results(target_platform: str) -> List[CheckResult]:
    results: List[CheckResult] = []

    # ── Universal ──────────────────────────────────────────────────────────────
    results.append(check_python())
    results.append(check_ffmpeg())
    results.append(check_ffprobe())
    results.append(check_mkvmerge())
    results.append(check_mkvinfo())
    results.append(check_mkvpropedit())

    # ── Python packages ────────────────────────────────────────────────────────
    results.append(CheckResult("INFO", "───", "Python packages"))
    results.append(check_pymkv2())
    results.append(check_pillow())
    results.append(check_pypdf())

    # ── Hardware encoders ──────────────────────────────────────────────────────
    results.append(CheckResult("INFO", "───", "Hardware encoders"))
    results.extend(check_hw_encoders())

    # ── Platform-specific ──────────────────────────────────────────────────────
    if target_platform == "macos":
        results.append(CheckResult("INFO", "───", "macOS-specific"))
        hb = check_homebrew()
        if hb: results.append(hb)
        gk = check_macos_gatekeeper()
        if gk: results.append(gk)

    elif target_platform == "windows":
        results.append(CheckResult("INFO", "───", "Windows-specific"))
        results.append(check_windows_ansi())
        results.append(check_windows_python_path())

    return results

# ── Output ─────────────────────────────────────────────────────────────────────

LEVEL_STYLE = {
    "PASS": (GREEN,   "PASS"),
    "FAIL": (RED,     "FAIL"),
    "WARN": (YELLOW,  "WARN"),
    "INFO": (CYAN,    "INFO"),
    "SKIP": (DIM,     "SKIP"),
}

def print_results(results: List[CheckResult], target_platform: str):
    w = 72
    print(c("─" * w, DIM))
    print(c(f"  READINESS CHECK — Video Copy + Repair", BOLD))
    print(c(f"  Platform: {target_platform.upper()}  ·  "
            f"{platform.system()} {platform.release()}  ·  {platform.machine()}", DIM))
    print(c("─" * w, DIM))

    for r in results:
        if r.label == "───":
            print(f"\n  {c(r.detail, DIM)}")
            continue

        colour, tag = LEVEL_STYLE.get(r.level, (DIM, r.level))
        tag_str  = c(f"[{tag}]", colour)
        label    = f"{r.label:<20}"
        detail   = r.detail
        ver_str  = f" ({r.version})" if r.version and r.version not in r.detail else ""
        path_str = f"  {c(r.path, DIM)}" if r.path and r.level == "PASS" and r.path != r.version else ""

        print(f"  {tag_str}  {c(label, BOLD)}{detail}{ver_str}{path_str}")

        if r.hint and r.level in ("FAIL", "WARN"):
            print(f"           {c('→', YELLOW)} {c(r.hint, YELLOW)}")

    print()
    print(c("─" * w, DIM))

    # ── Overall verdict ────────────────────────────────────────────────────────
    fails  = [r for r in results if r.level == "FAIL"]
    warns  = [r for r in results if r.level == "WARN"]

    if fails:
        verdict = c("  NOT READY", RED + BOLD)
        detail  = c(f"  {len(fails)} required tool(s) missing — see FAIL lines above", RED)
        code = 1
    elif warns:
        verdict = c("  READY WITH WARNINGS", YELLOW + BOLD)
        detail  = c(f"  {len(warns)} optional item(s) missing — see WARN lines above", YELLOW)
        code = 2
    else:
        verdict = c("  READY", GREEN + BOLD)
        detail  = c("  All required tools present — scripts can run", GREEN)
        code = 0

    print(f"  Result:{verdict}")
    print(detail)
    print(c("─" * w, DIM))
    print()
    return code

def print_json(results: List[CheckResult], target_platform: str) -> int:
    fails = [r for r in results if r.level == "FAIL"]
    warns = [r for r in results if r.level == "WARN"]
    if fails:
        status, code = "NOT_READY", 1
    elif warns:
        status, code = "READY_WITH_WARNINGS", 2
    else:
        status, code = "READY", 0

    out = {
        "platform": target_platform,
        "system":   platform.system(),
        "machine":  platform.machine(),
        "status":   status,
        "checks": [
            {
                "level":   r.level,
                "label":   r.label,
                "detail":  r.detail,
                "version": r.version,
                "path":    r.path,
                "hint":    r.hint,
            }
            for r in results if r.label != "───"
        ]
    }
    print(json.dumps(out, indent=2))
    return code

# ── CLI ────────────────────────────────────────────────────────────────────────

def detect_platform() -> str:
    s = platform.system().lower()
    if s == "darwin":   return "macos"
    if s == "windows":  return "windows"
    return "linux"

def main():
    ap = argparse.ArgumentParser(
        description="Probe dependencies for the Video Copy + Repair scripts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--platform", choices=["linux", "macos", "windows"], default=None,
                    help="Target platform to check (default: auto-detect)")
    ap.add_argument("--no-colour", action="store_true",
                    help="Disable ANSI colour output")
    ap.add_argument("--json", action="store_true",
                    help="Output machine-readable JSON instead of human text")
    args = ap.parse_args()

    if args.no_colour:
        global _colour_on
        _colour_on = False

    target = args.platform or detect_platform()
    results = gather_results(target)

    if args.json:
        code = print_json(results, target)
    else:
        code = print_results(results, target)

    sys.exit(code)

if __name__ == "__main__":
    main()
