"""Build an installation kit containing code and templates, never tenant secrets."""

import io
import zipfile
from pathlib import Path


def build_edge_kit():
    root = Path(__file__).parent
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as kit:
        for name in (
            "README.md",
            "compose.yml",
            "install.sh",
            "Dockerfile.edge",
            "requirements.intelbras.txt",
            "mosquitto.conf",
            ".dockerignore",
            "config/config.yml",
        ):
            kit.write(root / "edge_kit" / name, name)
        for name in (
            "intelbras_edge.py",
            "edge_provisioner.py",
            "edge_counter.py",
            "crossing_counter.py",
            "retail_store.py",
            "visitor_store.py",
        ):
            kit.write(root / name, name)
    return output.getvalue()
