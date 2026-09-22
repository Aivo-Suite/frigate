import sqlite3
from sqlalchemy import text
from db_config import engine, init_db
import json

SQLITE_PATH = "retail_analytics.db"

def migrate():
    print("Iniciando migração do SQLite para o PostgreSQL...")
    
    # Garantir que as tabelas existem no destino
    init_db()
    
    try:
        sqlite_conn = sqlite3.connect(SQLITE_PATH)
        cursor = sqlite_conn.cursor()
    except Exception as e:
        print(f"Erro ao conectar no banco SQLite de origem: {e}")
        return

    with engine.begin() as pg_conn:
        
        # 1. Migrar Visits
        print("Migrando tabela visits...")
        cursor.execute("SELECT * FROM visits")
        visits = cursor.fetchall()
        for v in visits:
            pg_conn.execute(text("""
                INSERT INTO visits (tracking_id, face_id, start_time, end_time, dwell_time_seconds, entered_zones, estimated_age, estimated_gender, camera_name, date_recorded)
                VALUES (:tid, :fid, :start_t, :end_t, :dwell, :zones, :age, :gender, :cam, :dt)
                ON CONFLICT (tracking_id) DO NOTHING
            """), {
                "tid": v[0], "fid": v[1], "start_t": v[2], "end_t": v[3], "dwell": v[4],
                "zones": v[5], "dt": v[6], "age": v[7] if len(v)>7 else None,
                "gender": v[8] if len(v)>8 else None, "cam": v[9] if len(v)>9 else "default"
            })
            
        # 2. Migrar Face Embeddings
        print("Migrando tabela face_embeddings...")
        cursor.execute("SELECT * FROM face_embeddings")
        embeddings = cursor.fetchall()
        for e in embeddings:
            # Se for Postgres com pgvector, inserimos o JSON como string e o Postgres converte automaticamente para vetor
            # (Dependendo da string format, pgvector aceita '[1,2,3]')
            vector_str = str(json.loads(e[1])) # Converte de array python pra string de vetor literal
            pg_conn.execute(text("""
                INSERT INTO face_embeddings (visitor_id, embedding_vector) 
                VALUES (:vid, :emb)
                ON CONFLICT (visitor_id) DO NOTHING
            """), {"vid": e[0], "emb": vector_str})
            
        # 3. Migrar Watch List
        print("Migrando tabela watch_list...")
        cursor.execute("SELECT * FROM watch_list")
        for w in cursor.fetchall():
            pg_conn.execute(text("""
                INSERT INTO watch_list (face_id, tag) VALUES (:fid, :tag)
                ON CONFLICT (face_id) DO NOTHING
            """), {"fid": w[0], "tag": w[1]})
            
        # 4. Migrar Heatmap Points
        print("Migrando tabela heatmap_points...")
        cursor.execute("SELECT * FROM heatmap_points")
        for h in cursor.fetchall():
            pg_conn.execute(text("""
                INSERT INTO heatmap_points (tracking_id, x, y, date_recorded) 
                VALUES (:tid, :x, :y, :dt)
            """), {"tid": h[0], "x": h[1], "y": h[2], "dt": h[3]})

    sqlite_conn.close()
    print("Migração concluída com sucesso! 🎉")

if __name__ == "__main__":
    if "postgresql" not in str(engine.url):
        print("Você não configurou a variável DATABASE_URL com um link PostgreSQL. Interrompendo migração.")
    else:
        migrate()
