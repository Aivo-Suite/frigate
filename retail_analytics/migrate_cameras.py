"""Apply the additive camera migration without changing existing analytics tables."""

from camera_store import init_camera_db
from db_config import engine

if __name__ == "__main__":
    init_camera_db(engine)
    print("Camera schema ready")
