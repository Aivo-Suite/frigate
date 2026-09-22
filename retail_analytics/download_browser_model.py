"""Fetch the pinned public YOLOX-Nano artifact for Dockerfile.browser."""

import hashlib
import urllib.request
from pathlib import Path

MODEL_URL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx"
EXPECTED = "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"


def main():
    """Verify the artifact before making it available to the build context."""
    root = Path(__file__).with_name("browser_models")
    root.mkdir(exist_ok=True)
    content = urllib.request.urlopen(MODEL_URL, timeout=60).read()
    if hashlib.sha256(content).hexdigest() != EXPECTED:
        raise SystemExit("Model checksum mismatch; build artifact not updated")
    (root / "yolox_nano.onnx").write_bytes(content)
    print("Pinned YOLOX-Nano model verified")


if __name__ == "__main__":
    main()
