import os
from sqlalchemy import create_engine, text

# Determinar qual banco de dados usar
# Se POSTGRES_URL estiver setado (ex: postgresql://user:pass@localhost:5432/retail), usa Postgres.
# Caso contrário, usa SQLite local.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///retail_analytics.db")

engine = create_engine(DATABASE_URL)

def init_db():
    with engine.begin() as conn:
        # Tabela visits
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS visits (
                tracking_id VARCHAR PRIMARY KEY,
                face_id VARCHAR,
                start_time FLOAT,
                end_time FLOAT,
                dwell_time_seconds FLOAT,
                entered_zones VARCHAR,
                estimated_age FLOAT,
                estimated_gender VARCHAR,
                camera_name VARCHAR,
                date_recorded TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        
        # Tabela embeddings
        # Para pgvector no Postgres, usaríamos o tipo VECTOR. 
        # No SQLite mantemos como texto (JSON).
        if "postgresql" in DATABASE_URL:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS face_embeddings (
                        visitor_id VARCHAR PRIMARY KEY,
                        embedding_vector VECTOR(128)
                    )
                """))
            except Exception as e:
                print(f"Aviso Vector DB: {e}")
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS face_embeddings (
                    visitor_id VARCHAR PRIMARY KEY,
                    embedding_json VARCHAR
                )
            """))

        # Tabela watch_list
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS watch_list (
                face_id VARCHAR PRIMARY KEY,
                tag VARCHAR
            )
        """))

        # Tabela heatmap
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS heatmap_points (
                tracking_id VARCHAR,
                x FLOAT,
                y FLOAT,
                date_recorded TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
