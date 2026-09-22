import os
from sqlalchemy import create_engine, text

# Determinar qual banco de dados usar
# Se POSTGRES_URL estiver setado (ex: postgresql://user:pass@localhost:5432/retail), usa Postgres.
# Caso contrário, usa SQLite local.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///retail_analytics.db")

engine = create_engine(DATABASE_URL)

def init_db():
    if "postgresql" in DATABASE_URL:
        id_col = "SERIAL PRIMARY KEY"
    else:
        id_col = "INTEGER PRIMARY KEY AUTOINCREMENT"

    with engine.begin() as conn:
        # Tabela tenants (Lojas/Clientes do SaaS)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id VARCHAR PRIMARY KEY,
                name VARCHAR,
                username VARCHAR UNIQUE,
                password_hash VARCHAR,
                api_key VARCHAR UNIQUE
            )
        """))

        # Tabela visits
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS visits (
                id {id_col},
                tenant_id VARCHAR,
                tracking_id VARCHAR,
                face_id VARCHAR,
                start_time FLOAT,
                end_time FLOAT,
                dwell_time_seconds FLOAT,
                entered_zones VARCHAR,
                estimated_age FLOAT,
                estimated_gender VARCHAR,
                camera_name VARCHAR,
                date_recorded TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, tracking_id)
            )
        """))
        
        # Tabela embeddings
        if "postgresql" in DATABASE_URL:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS face_embeddings (
                        id {id_col},
                        tenant_id VARCHAR,
                        visitor_id VARCHAR,
                        embedding_vector VECTOR(128),
                        UNIQUE(tenant_id, visitor_id)
                    )
                """))
            except Exception as e:
                print(f"Aviso Vector DB: {e}")
        else:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS face_embeddings (
                    id {id_col},
                    tenant_id VARCHAR,
                    visitor_id VARCHAR,
                    embedding_json VARCHAR,
                    UNIQUE(tenant_id, visitor_id)
                )
            """))

        # Tabela watch_list
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS watch_list (
                id {id_col},
                tenant_id VARCHAR,
                face_id VARCHAR,
                tag VARCHAR,
                UNIQUE(tenant_id, face_id)
            )
        """))

        # Tabela heatmap
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS heatmap_points (
                id {id_col},
                tenant_id VARCHAR,
                tracking_id VARCHAR,
                x FLOAT,
                y FLOAT,
                date_recorded TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
