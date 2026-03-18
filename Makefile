.PHONY: proto install run clean

proto:
	uv run python -m grpc_tools.protoc \
		--python_out=ssl_auto_streamer/ssl/ \
		-I=proto/ \
		proto/*.proto

install:
	uv sync --all-groups

run:
	uv run ssl-auto-streamer

clean:
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	find ssl_auto_streamer/ssl/ -name "*_pb2.py" -delete
