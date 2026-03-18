# Copyright (c) 2026 ibis-ssl
#
# Ensure protobuf-generated _pb2 files can import each other.
# (protoc generates files that import each other by bare module name)
import os
import sys

_ssl_dir = os.path.dirname(__file__)
if _ssl_dir not in sys.path:
    sys.path.insert(0, _ssl_dir)
