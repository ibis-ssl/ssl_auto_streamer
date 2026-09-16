#!/usr/bin/env python3
# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Commentary Iteration Runner.

Automates the cycle of:
1. Replaying an SSL match log (real or scenario) through ssl-auto-streamer
2. Capturing the generated AI session activity log
3. Analyzing the session for compliance (sentence count, tone, assertions, repetitions)
4. Reporting evaluation summary and metrics
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analyze_ai_session import parse_session_file, print_report  # noqa: E402


def get_session_files(log_dir: Path) -> set:
    """Get set of existing session file paths."""
    return set(log_dir.glob("ai_session_*.jsonl"))


def run_iteration(
    log_path: Path,
    speed: float = 1.0,
    log_dir: Path = Path("logs/ai"),
    strict: bool = False,
    verbose: bool = False,
) -> int:
    """Run commentary replay and evaluate results."""
    if not log_path.exists():
        print(f"Error: Target log file not found: {log_path}", file=sys.stderr)
        return 1

    log_dir.mkdir(parents=True, exist_ok=True)
    before_files = get_session_files(log_dir)

    cmd = [
        sys.executable,
        "-m",
        "ssl_auto_streamer.main",
        "--replay-log",
        str(log_path),
        "--auto-start",
        "--exit-on-replay-finish",
        "--web-port",
        "0",
        "--audio-output-mode",
        "off",
    ]

    print("=" * 80)
    print(f" Starting Commentary Iteration on: {log_path.name}")
    print(f" Full Path: {log_path}")
    print(f" Command  : {' '.join(cmd)}")
    print("=" * 80)

    start_time = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except KeyboardInterrupt:
        print("\n[INFO] Iteration run interrupted by user.")
        return 130

    duration = time.time() - start_time
    print(f"\n[INFO] Replay execution completed in {duration:.1f}s (exit code: {proc.returncode})")

    if proc.returncode != 0:
        print("\n--- Process Output ---")
        print(proc.stdout[-2000:])
        print(f"Error: ssl-auto-streamer failed with code {proc.returncode}", file=sys.stderr)
        return proc.returncode

    # Detect newly created session file
    after_files = get_session_files(log_dir)
    new_files = list(after_files - before_files)

    if new_files:
        new_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        session_file = new_files[0]
    else:
        # Fallback to the latest modified file
        all_files = list(after_files)
        if not all_files:
            print(f"Error: No AI session logs found in {log_dir}", file=sys.stderr)
            return 1
        all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        session_file = all_files[0]
        print(f"[NOTE] No new session file detected; inspecting latest: {session_file.name}")

    print(f"[INFO] Analyzing AI Activity Log: {session_file.name}\n")
    session_info = parse_session_file(session_file)
    summary = print_report(session_info, verbose=verbose)

    issues_count = summary.get("issues_count", 0)
    if issues_count > 0:
        print(f"[RESULT] Completed with {issues_count} issue(s) detected.")
        return 1 if strict else 0
    else:
        print("[RESULT] Clean run! All compliance and quality checks passed.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run commentary development iteration on an SSL log.")
    parser.add_argument(
        "log_path",
        nargs="?",
        default="tests/data/sample_match.log.gz",
        help="Path to SSL log file (.log or .log.gz) to replay (default: tests/data/sample_match.log.gz)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any rule issues are detected",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Display verbose thoughts and tool call details",
    )

    args = parser.parse_args()
    target_log = Path(args.log_path)
    if not target_log.is_absolute():
        target_log = PROJECT_ROOT / target_log

    code = run_iteration(
        target_log,
        strict=args.strict,
        verbose=args.verbose,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
