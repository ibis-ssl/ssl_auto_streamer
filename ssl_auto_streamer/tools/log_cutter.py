# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Log Cutter - utility to extract time segments from RoboCup SSL log files."""

import argparse
import gzip
import logging
import struct
from pathlib import Path
from typing import Optional

from ssl_auto_streamer.ssl.log_reader import MAGIC_HEADER, SSLLogReader

logger = logging.getLogger(__name__)


def cut_log(
    input_path: Path,
    output_path: Path,
    start_sec: float = 0.0,
    duration_sec: Optional[float] = None,
    max_packets: Optional[int] = None,
) -> int:
    """
    Extract a slice of an SSL log file and write to output_path.

    Returns:
        Number of packets written.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input log file not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    is_gz_out = str(output_path).endswith(".gz")
    out_file = gzip.open(output_path, "wb") if is_gz_out else open(output_path, "wb")

    packets_written = 0
    first_ts: Optional[int] = None
    start_ts_rel: Optional[float] = None

    try:
        with SSLLogReader(input_path) as reader:
            # Write header
            out_file.write(MAGIC_HEADER)
            out_file.write(struct.pack(">i", reader.version or 1))

            for pkt in reader.iter_packets():
                if first_ts is None:
                    first_ts = pkt.timestamp_ns

                elapsed_sec = (pkt.timestamp_ns - first_ts) / 1e9

                if elapsed_sec < start_sec:
                    continue

                if start_ts_rel is None:
                    start_ts_rel = elapsed_sec

                if duration_sec is not None:
                    if (elapsed_sec - start_ts_rel) > duration_sec:
                        break

                if max_packets is not None and packets_written >= max_packets:
                    break

                # Write packet header + payload
                header = struct.pack(
                    ">qii", pkt.timestamp_ns, pkt.message_type, len(pkt.payload)
                )
                out_file.write(header)
                out_file.write(pkt.payload)
                packets_written += 1

    finally:
        out_file.close()

    out_size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        f"Cut log created: {output_path} ({packets_written} packets, {out_size_mb:.2f} MB)"
    )
    return packets_written


def main() -> None:
    """CLI entry point for ssl-log-cutter."""
    parser = argparse.ArgumentParser(
        description="Extract a time-slice or packet count from an SSL log file."
    )
    parser.add_argument("input_path", type=Path, help="Input .log or .log.gz file")
    parser.add_argument("output_path", type=Path, help="Output .log or .log.gz file")
    parser.add_argument(
        "--start-sec",
        type=float,
        default=0.0,
        help="Start offset in seconds from log beginning (default: 0.0)",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=None,
        help="Duration in seconds to extract (default: until EOF)",
    )
    parser.add_argument(
        "--max-packets",
        type=int,
        default=None,
        help="Maximum number of packets to extract",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    count = cut_log(
        input_path=args.input_path,
        output_path=args.output_path,
        start_sec=args.start_sec,
        duration_sec=args.duration_sec,
        max_packets=args.max_packets,
    )
    print(f"Successfully cut {count} packets to {args.output_path}")


if __name__ == "__main__":
    main()
