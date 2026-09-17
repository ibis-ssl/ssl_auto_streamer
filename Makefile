.PHONY: proto install run clean replay play-log test test-log generate-test-data iterate

SAMPLE_LOG ?= tests/data/sample_match.log.gz
SPEED ?= 1.0

proto:
	uv run python -m grpc_tools.protoc \
		--python_out=ssl_auto_streamer/ssl/ \
		-I=proto/ \
		proto/*.proto

install:
	uv sync --all-groups

run:
	uv run ssl-auto-streamer

replay:
	uv run ssl-auto-streamer --replay-log $(SAMPLE_LOG) --replay-speed $(SPEED)

play-log:
	uv run ssl-log-player $(SAMPLE_LOG) --speed $(SPEED)

generate-test-data:
	uv run ssl-log-generator --all

iterate:
	uv run python scripts/run_commentary_iteration.py $(SAMPLE_LOG)

test:
	PYTHONPATH="" uv run pytest -p no:launch_testing_ros_pytest_entrypoint -p no:launch-testing-ros -p no:ament_lint

test-log:
	PYTHONPATH="" uv run pytest -p no:launch_testing_ros_pytest_entrypoint -p no:launch-testing-ros -p no:ament_lint tests/test_log_replay.py -v

clean:
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	find ssl_auto_streamer/ssl/ -name "*_pb2.py" -delete
