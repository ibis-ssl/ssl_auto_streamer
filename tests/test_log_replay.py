# Copyright (c) 2026 ibis-ssl
#
# Use of this source code is governed by an MIT-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/MIT.

"""Tests for SSL log reader, log cutter, and replay pipeline."""

import asyncio
import gzip
import struct
from pathlib import Path

from ssl_auto_streamer.event_detector import EventDetector
from ssl_auto_streamer.ssl.log_reader import (
    MAGIC_HEADER,
    MSG_TYPE_SSL_REFBOX_2013,
    MSG_TYPE_SSL_VISION_2014,
    MSG_TYPE_SSL_VISION_TRACKER_2020,
    SSLLogReader,
)
from ssl_auto_streamer.ssl import ssl_vision_wrapper_tracked_pb2
from ssl_auto_streamer.statler import WorldModelWriter
from ssl_auto_streamer.tools.log_cutter import cut_log

SAMPLE_LOG_PATH = Path(__file__).parent / "data" / "sample_match.log.gz"


def test_log_reader_synthetic_data(tmp_path: Path) -> None:
    """Test reading a synthetically created log file."""
    test_file = tmp_path / "test.log.gz"
    with gzip.open(test_file, "wb") as f:
        f.write(MAGIC_HEADER)
        f.write(struct.pack(">i", 1))

        # Packet 1: dummy payload
        payload_1 = b"hello_referee_dummy"
        f.write(struct.pack(">qii", 1000000000, MSG_TYPE_SSL_REFBOX_2013, len(payload_1)))
        f.write(payload_1)

        # Packet 2: Tracker protobuf
        tracker = ssl_vision_wrapper_tracked_pb2.TrackerWrapperPacket()
        tracker.uuid = "dummy-uuid"
        tracker.tracked_frame.frame_number = 42
        tracker.tracked_frame.timestamp = 2.0
        payload_2 = tracker.SerializeToString()
        f.write(struct.pack(">qii", 2000000000, MSG_TYPE_SSL_VISION_TRACKER_2020, len(payload_2)))
        f.write(payload_2)

    with SSLLogReader(test_file) as reader:
        assert reader.version == 1
        packets = list(reader.iter_packets())
        assert len(packets) == 2

        # First packet
        assert packets[0].message_type == MSG_TYPE_SSL_REFBOX_2013
        assert packets[0].timestamp_ns == 1000000000
        assert packets[0].payload == payload_1

        # Second packet: Tracker
        assert packets[1].message_type == MSG_TYPE_SSL_VISION_TRACKER_2020
        tracker_decoded = packets[1].decode()
        assert isinstance(tracker_decoded, ssl_vision_wrapper_tracked_pb2.TrackerWrapperPacket)
        assert tracker_decoded.tracked_frame.frame_number == 42


def test_log_cutter(tmp_path: Path) -> None:
    """Test log_cutter utility extracts exact packet counts and duration."""
    source_file = tmp_path / "source.log.gz"
    dest_file = tmp_path / "cut.log.gz"

    with gzip.open(source_file, "wb") as f:
        f.write(MAGIC_HEADER)
        f.write(struct.pack(">i", 1))

        # Write 5 packets, 1 sec interval
        for i in range(5):
            data = f"packet_{i}".encode("utf-8")
            ts = i * 1_000_000_000
            f.write(struct.pack(">qii", ts, MSG_TYPE_SSL_REFBOX_2013, len(data)))
            f.write(data)

    # Cut 3 packets starting from sec 1.0 to sec 3.0
    written = cut_log(
        input_path=source_file,
        output_path=dest_file,
        start_sec=1.0,
        duration_sec=2.0,
    )
    assert written == 3

    with SSLLogReader(dest_file) as reader:
        packets = list(reader.iter_packets())
        assert len(packets) == 3
        assert packets[0].timestamp_ns == 1_000_000_000
        assert packets[-1].timestamp_ns == 3_000_000_000
        assert packets[0].payload == b"packet_1"
        assert packets[-1].payload == b"packet_3"


def test_sample_match_log_header_and_packets() -> None:
    """Test reading the real extracted sample match log file."""
    assert SAMPLE_LOG_PATH.exists(), f"Sample log not found at {SAMPLE_LOG_PATH}"

    with SSLLogReader(SAMPLE_LOG_PATH) as reader:
        assert reader.version == 1

        msg_types = set()
        count = 0
        for pkt in reader.iter_packets(max_packets=1000):
            msg_types.add(pkt.message_type)
            count += 1

        assert count == 1000
        # Should have referee (3), vision (4), and tracker (5) packets
        assert MSG_TYPE_SSL_REFBOX_2013 in msg_types
        assert MSG_TYPE_SSL_VISION_TRACKER_2020 in msg_types


def test_world_model_and_event_detector_with_sample_log() -> None:
    """Test feeding sample match log into WorldModelWriter and EventDetector."""
    assert SAMPLE_LOG_PATH.exists()

    writer = WorldModelWriter()
    detector = EventDetector()

    detected_events = []

    with SSLLogReader(SAMPLE_LOG_PATH) as reader:
        # Process first 5000 packets (~10 seconds of match)
        for pkt in reader.iter_packets(max_packets=5000):
            if pkt.message_type == MSG_TYPE_SSL_VISION_TRACKER_2020:
                tracker = pkt.decode()
                if tracker and tracker.HasField("tracked_frame"):
                    writer.update_from_tracker(tracker.tracked_frame)
                    events = detector.update_from_tracker(tracker.tracked_frame)
                    detected_events.extend(events)

            elif pkt.message_type == MSG_TYPE_SSL_REFBOX_2013:
                referee = pkt.decode()
                if referee:
                    writer.update_from_referee(referee)
                    events = detector.update_from_referee(referee)
                    detected_events.extend(events)

            elif pkt.message_type == MSG_TYPE_SSL_VISION_2014:
                vision = pkt.decode()
                if vision and vision.HasField("geometry"):
                    writer.update_from_geometry(vision.geometry)

    # Validate world model state
    blue_name, yellow_name = writer.get_team_names()
    assert blue_name == "RoboDragons"
    assert yellow_name == "ibis"

    # Robots and ball should be tracked
    snapshot = writer.get_field_snapshot_data()
    assert snapshot["ball"] is not None
    assert len(snapshot["robots_blue"]) > 0
    assert len(snapshot["robots_yellow"]) > 0

    # Ensure events were captured
    event_types = {e.event_type for e in detected_events}
    assert len(event_types) > 0


def test_timed_packets_generator() -> None:
    """Test iter_timed_packets runs with speed=0 (max speed)."""
    assert SAMPLE_LOG_PATH.exists()

    async def _run():
        with SSLLogReader(SAMPLE_LOG_PATH) as reader:
            packets = []
            async for pkt in reader.iter_timed_packets(speed=0, max_packets=50):
                packets.append(pkt)
            return packets

    packets = asyncio.run(_run())
    assert len(packets) == 50


def test_app_dynamic_replay_control() -> None:
    """Test CommentaryApp dynamic start_replay, status, and stop_replay."""
    from ssl_auto_streamer.app import CommentaryApp

    config = {
        "ssl": {},
        "gemini": {"api_key": "test_dummy_key"},
        "web": {"enabled": False},
        "analysis_agent": {"enabled": False},
    }
    app = CommentaryApp(config)
    assert not app.get_replay_status()["active"]

    # Start replay with default speed (1.0x)
    success = app.start_replay(log_path=str(SAMPLE_LOG_PATH))
    assert success is True
    status = app.get_replay_status()
    assert status["active"] is True
    assert status["speed"] == 1.0

    # Stop replay
    stopped = app.stop_replay()
    assert stopped is True
    assert not app.get_replay_status()["active"]
