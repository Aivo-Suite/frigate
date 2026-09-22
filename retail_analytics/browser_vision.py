"""CPU person detection and conservative short-lived browser trajectories."""

import hashlib
import io
import json
import math
import uuid

import numpy as np
from browser_store import validate_gate
from PIL import Image, UnidentifiedImageError


def overlap(a, b):
    """Intersection over union for normalized bounding boxes."""
    w, h = (
        max(0, min(a[2], b[2]) - max(a[0], b[0])),
        max(0, min(a[3], b[3]) - max(a[1], b[1])),
    )
    inter = w * h
    return inter / max(
        1e-9, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    )


class PersonDetector:
    """Run the official YOLOX-Nano ONNX model with one CPU inference thread."""

    def __init__(self, path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.model = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input = self.model.get_inputs()[0].name
        assert self.model.get_inputs()[0].shape == [1, 3, 416, 416]
        grid = []
        for stride in (8, 16, 32):
            grid.extend(
                (x, y, stride)
                for y in range(416 // stride)
                for x in range(416 // stride)
            )
        self.grid = np.array(grid, dtype=np.float32)

    def detect(self, body):
        """Decode a bounded JPEG and return person boxes without retaining the image."""
        if not body or len(body) > 160000:
            raise ValueError("Frame too large")
        try:
            with Image.open(io.BytesIO(body)) as image:
                if (
                    image.format != "JPEG"
                    or not 32 <= image.width <= 640
                    or not 32 <= image.height <= 480
                ):
                    raise ValueError("Invalid frame dimensions")
                image.load()
                width, height = image.size
                ratio = min(416 / width, 416 / height)
                resized = image.convert("RGB").resize(
                    (int(width * ratio), int(height * ratio)), Image.Resampling.BILINEAR
                )
                padded = np.full((416, 416, 3), 114, dtype=np.float32)
                padded[: resized.height, : resized.width] = np.asarray(resized)[
                    :, :, ::-1
                ]
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise ValueError("Invalid image") from error
        data = np.ascontiguousarray(padded.transpose(2, 0, 1)[None])
        output = self.model.run(None, {self.input: data})[0][0]
        confidence = output[:, 4] * output[:, 5]
        indices = np.where(
            (confidence >= 0.45) & (np.argmax(output[:, 5:], axis=1) == 0)
        )[0]
        boxes = []
        for idx in indices:
            stride = self.grid[idx, 2]
            cx, cy = (output[idx, :2] + self.grid[idx, :2]) * stride / ratio
            bw, bh = np.exp(np.clip(output[idx, 2:4], -10, 10)) * stride / ratio
            box = [
                float(np.clip((cx - bw / 2) / width, 0, 1)),
                float(np.clip((cy - bh / 2) / height, 0, 1)),
                float(np.clip((cx + bw / 2) / width, 0, 1)),
                float(np.clip((cy + bh / 2) / height, 0, 1)),
            ]
            if box[2] > box[0] and box[3] > box[1]:
                boxes.append((float(confidence[idx]), box))
        keep = []
        for score, box in sorted(boxes, key=lambda item: item[0], reverse=True):
            if not any(overlap(box, other) > 0.45 for _, other in keep):
                keep.append((score, box))
            if len(keep) >= 20:
                break
        return [box for _, box in keep]


class LineTracker:
    """Track short continuous paths; disappearance and reconnect never count as exits."""

    def __init__(self, camera_id, session_id, gate):
        self.camera_id = camera_id
        self.session_id = session_id
        self.gate = validate_gate(gate)
        self.revision = hashlib.sha256(
            json.dumps(self.gate, sort_keys=True).encode()
        ).hexdigest()
        self.tracks = {}
        self.next_id = 0
        self.last_time = 0

    def update(self, boxes, now):
        """Associate unambiguous detections and confirm crossings beyond a dead band."""
        if now <= self.last_time:
            return [], []
        self.last_time = now
        self.tracks = {
            tid: t for tid, t in self.tracks.items() if now - t["time"] <= 1.2
        }
        candidates = []
        for index, box in enumerate(boxes):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            for tid, track in self.tracks.items():
                prev = track["box"]
                distance = math.hypot(
                    cx - (prev[0] + prev[2]) / 2, cy - (prev[1] + prev[3]) / 2
                )
                iou = overlap(box, prev)
                if iou > 0.05 and distance < 0.22:
                    candidates.append((iou - distance, index, tid))
        assigned = {}
        used = set()
        for score, index, tid in sorted(candidates, reverse=True):
            if index not in assigned and tid not in used:
                competing = [
                    c
                    for c in candidates
                    if (c[1] == index or c[2] == tid) and c != (score, index, tid)
                ]
                if competing and max(c[0] for c in competing) > score - 0.08:
                    # Ambiguous overlap resets continuity instead of guessing identities.
                    continue
                assigned[index] = tid
                used.add(tid)
        events = []
        annotated = []
        for index, box in enumerate(boxes):
            tid = assigned.get(index)
            if tid is None:
                self.next_id += 1
                tid = self.next_id
                self.tracks[tid] = {
                    "hits": 0,
                    "side": None,
                    "candidate": None,
                    "stable": 0,
                    "crossed": -1e9,
                    "seq": 0,
                }
            track = self.tracks[tid]
            track.update(box=box, time=now, hits=track["hits"] + 1)
            coordinate = (box[0] + box[2]) / 2 if self.gate["axis"] == "x" else box[3]
            delta = coordinate - self.gate["position"]
            side = 1 if delta > 0.04 else -1 if delta < -0.04 else None
            if side is not None:
                track["stable"] = (
                    track["stable"] + 1 if track["candidate"] == side else 1
                )
                track["candidate"] = side
                if track["stable"] >= 2 and track["hits"] >= 3:
                    if (
                        track["side"] is not None
                        and track["side"] != side
                        and now - track["crossed"] >= 1
                    ):
                        track["seq"] += 1
                        entry = (side == 1) == self.gate["positive_entry"]
                        events.append(
                            {
                                "event_id": str(
                                    uuid.uuid5(
                                        uuid.NAMESPACE_URL,
                                        f"{self.session_id}:{tid}:{track['seq']}",
                                    )
                                ),
                                "camera_id": self.camera_id,
                                "tracking_id": f"{self.session_id}:{tid}",
                                "direction": "entry" if entry else "exit",
                                "occurred_at": now,
                                "gate_revision": self.revision,
                            }
                        )
                        track["crossed"] = now
                    track["side"] = side
            else:
                track["candidate"] = None
                track["stable"] = 0
            if track["hits"] >= 3:
                annotated.append({"id": tid, "box": box})
        return events, annotated
