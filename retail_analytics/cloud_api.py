from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy import text
from db_config import engine, init_db
import json

app = FastAPI(title="Retail Analytics Cloud API")

# Inicializar DB ao rodar a API
init_db()

# --- Models ---
class VisitPayload(BaseModel):
    tracking_id: str
    face_id: str
    start_time: float
    end_time: float
    dwell_time_seconds: float
    entered_zones: str
    estimated_age: Optional[float] = None
    estimated_gender: Optional[str] = None
    camera_name: str
    embedding_json: Optional[str] = None # Face vector para salvar no Cloud

class HeatmapPayload(BaseModel):
    tracking_id: str
    x: float
    y: float

# --- Dependência de Autenticação Multi-Tenant ---
def get_tenant_from_api_key(x_api_key: str = Header(...)):
    with engine.connect() as conn:
        tenant = conn.execute(
            text("SELECT tenant_id FROM tenants WHERE api_key = :api_key"),
            {"api_key": x_api_key}
        ).fetchone()
        
    if not tenant:
        raise HTTPException(status_code=401, detail="API Key inválida")
    return tenant[0]

# --- Endpoints ---

@app.post("/api/visits")
async def register_visit(payload: VisitPayload, tenant_id: str = Depends(get_tenant_from_api_key)):
    try:
        with engine.begin() as conn:
            # 1. Salvar ou atualizar a visita (com tenant_id)
            existing = conn.execute(
                text("SELECT id FROM visits WHERE tenant_id = :tid AND tracking_id = :trk"),
                {"tid": tenant_id, "trk": payload.tracking_id}
            ).fetchone()
            
            if existing:
                conn.execute(text("""
                    UPDATE visits SET 
                        face_id=:fid, end_time=:end_t, dwell_time_seconds=:dwell, 
                        entered_zones=:zones, estimated_age=:age, estimated_gender=:gender, camera_name=:cam
                    WHERE tenant_id=:tid AND tracking_id=:trk
                """), {
                    "fid": payload.face_id, "end_t": payload.end_time, "dwell": payload.dwell_time_seconds,
                    "zones": payload.entered_zones, "age": payload.estimated_age, "gender": payload.estimated_gender,
                    "cam": payload.camera_name, "tid": tenant_id, "trk": payload.tracking_id
                })
            else:
                conn.execute(text("""
                    INSERT INTO visits 
                    (tenant_id, tracking_id, face_id, start_time, end_time, dwell_time_seconds, entered_zones, estimated_age, estimated_gender, camera_name)
                    VALUES (:tid, :trk, :fid, :start_t, :end_t, :dwell, :zones, :age, :gender, :cam)
                """), {
                    "tid": tenant_id, "trk": payload.tracking_id, "fid": payload.face_id, "start_t": payload.start_time,
                    "end_t": payload.end_time, "dwell": payload.dwell_time_seconds, "zones": payload.entered_zones,
                    "age": payload.estimated_age, "gender": payload.estimated_gender, "cam": payload.camera_name
                })
                
            # 2. Salvar embedding se enviado
            if payload.embedding_json and "VISITOR_" in payload.face_id:
                # Opcionalmente, salvamos o embedding
                conn.execute(text("""
                    INSERT INTO face_embeddings (tenant_id, visitor_id, embedding_json)
                    VALUES (:tid, :vid, :emb)
                    ON CONFLICT(tenant_id, visitor_id) DO NOTHING
                """), {"tid": tenant_id, "vid": payload.face_id, "emb": payload.embedding_json})
                
        return {"status": "success", "tenant_id": tenant_id, "tracking_id": payload.tracking_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/heatmap")
async def register_heatmap(payload: HeatmapPayload, tenant_id: str = Depends(get_tenant_from_api_key)):
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO heatmap_points (tenant_id, tracking_id, x, y) VALUES (:tid, :trk, :x, :y)"),
                {"tid": tenant_id, "trk": payload.tracking_id, "x": payload.x, "y": payload.y}
            )
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/watch_list/{face_id}")
async def check_watch_list(face_id: str, tenant_id: str = Depends(get_tenant_from_api_key)):
    with engine.connect() as conn:
        watch_match = conn.execute(
            text("SELECT tag FROM watch_list WHERE tenant_id = :tid AND face_id = :fid"),
            {"tid": tenant_id, "fid": face_id}
        ).fetchone()
        
    if watch_match:
        return {"matched": True, "tag": watch_match[0]}
    return {"matched": False}
