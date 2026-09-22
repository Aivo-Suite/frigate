"""Apply the additive directional traffic schema."""

from db_config import engine
from traffic_store import init_traffic_db

if __name__ == "__main__":
    init_traffic_db(engine)
    print("Directional traffic schema ready")
