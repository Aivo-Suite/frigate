"""Validated HTTP payloads for reliable directional crossing ingestion."""

import math
import time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CrossingEvent(BaseModel):
    """One observed physical transition across a calibrated entrance."""

    event_id: str
    camera_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    tracking_id: str = Field(min_length=1, max_length=128)
    direction: Literal["entry", "exit"]
    occurred_at: float = Field(gt=0, allow_inf_nan=False)
    gate_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        """Canonicalize event identity before enforcing uniqueness."""
        return str(UUID(value))

    @field_validator("occurred_at")
    @classmethod
    def validate_timestamp(cls, value: float) -> float:
        """Accept delayed delivery but reject clock errors far in the future."""
        if not math.isfinite(value) or value > time.time() + 300:
            raise ValueError("Invalid event timestamp")
        return value


class CrossingBatch(BaseModel):
    """Bound request size to keep ingestion work predictable."""

    events: list[CrossingEvent] = Field(min_length=1, max_length=100)


class CounterHeartbeat(BaseModel):
    """Delivery and MQTT health; this does not assert a camera is producing frames."""

    camera_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    mqtt_connected: bool
    frigate_available: bool | None = None
    last_person_event: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    pending_events: int = Field(ge=0)
    gate_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class WebcamSource(BaseModel):
    """A test webcam identity, separate from RTSP provisioning."""

    camera_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=100)
