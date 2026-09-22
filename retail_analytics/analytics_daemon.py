import json
import sqlite3
import os
import requests
import numpy as np
import uuid
import warnings
from datetime import datetime
import paho.mqtt.client as mqtt
import time

# Suppress warnings from deepface/tf
warnings.filterwarnings("ignore")

try:
    from deepface import DeepFace
    DEEPFACE_AVAILABLE = True
except ImportError:
    DEEPFACE_AVAILABLE = False
    print("WARNING: deepface library not found. Zero-shot Re-ID will be disabled. Install with: pip install deepface tf-keras")

# Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC = "frigate/events"
FRIGATE_URL = os.getenv("FRIGATE_URL", "http://localhost:5000")

# Telegram Alerts Configuration (Módulo 2)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "") # Preencha com o token do seu bot
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")     # Preencha com o seu chat ID

# Re-ID Configuration
REID_MODEL = "Facenet"
SIMILARITY_THRESHOLD = 0.70 # Cosine similarity threshold

# In-memory vector store for fast matching
known_faces = {} # { "visitor_id": [embedding_vector] }

# Throttle dictionary for heatmap points to avoid DB spam
last_heatmap_time = {} # { "tracking_id": last_time_seconds }

def load_embeddings():
    global known_faces
    known_faces = {}
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT visitor_id, embedding_json FROM face_embeddings"))
            for row in result:
                # row[0] is visitor_id, row[1] is embedding_json
                known_faces[row[0]] = np.array(json.loads(row[1]))
        print(f"[*] Loaded {len(known_faces)} face embeddings from Database.")
    except Exception as e:
        print(f"[*] Warning: Could not load embeddings: {e}")

def cosine_similarity(v1, v2):
    dot_product = np.dot(v1, v2)
    norm_v1 = np.linalg.norm(v1)
    norm_v2 = np.linalg.norm(v2)
    if norm_v1 == 0 or norm_v2 == 0:
        return 0
    return dot_product / (norm_v1 * norm_v2)

def identify_visitor(snapshot_path):
    if not DEEPFACE_AVAILABLE:
        return "Unknown"
        
    try:
        # Extract face embedding
        results = DeepFace.represent(img_path=snapshot_path, model_name=REID_MODEL, enforce_detection=True)
        if len(results) == 0:
            return "Unknown"
            
        embedding = np.array(results[0]["embedding"])
        
        # Search against known faces
        best_match_id = None
        best_score = -1
        
        for vid, known_emb in known_faces.items():
            score = cosine_similarity(embedding, known_emb)
            if score > best_score:
                best_score = score
                best_match_id = vid
                
        if best_score > SIMILARITY_THRESHOLD:
            print(f"  -> Face Matched! Welcome back {best_match_id} (Score: {best_score:.2f})")
            face_id_result = best_match_id
        else:
            # Create new visitor
            new_vid = f"VISITOR_{str(uuid.uuid4())[:8].upper()}"
            print(f"  -> New Face Detected. Assigned ID: {new_vid}")
            
            # Save to memory and DB
            known_faces[new_vid] = embedding
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO face_embeddings (visitor_id, embedding_json) VALUES (:vid, :emb)"),
                    {"vid": new_vid, "emb": json.dumps(embedding.tolist())}
                )
            face_id_result = new_vid
            
        # Module 1: Extract Demographics
        try:
            demographics = DeepFace.analyze(img_path=snapshot_path, actions=['age', 'gender'], enforce_detection=False)
            if isinstance(demographics, list) and len(demographics) > 0:
                demo = demographics[0]
                age = demo.get('age')
                # get dominant gender
                gender = demo.get('dominant_gender')
                return face_id_result, age, gender
        except Exception as e:
            print(f"  -> Demographics extraction failed: {e}")
            
        return face_id_result, None, None
            
    except Exception as e:
        print(f"  -> No clear face detected in snapshot for Re-ID.")
        return "Unknown", None, None

def process_reid(tracking_id):
    # Fetch snapshot from Frigate API
    snapshot_url = f"{FRIGATE_URL}/api/events/{tracking_id}/snapshot.jpg"
    snapshot_path = f"/tmp/{tracking_id}.jpg"
    
    try:
        response = requests.get(snapshot_url, timeout=5)
        if response.status_code == 200:
            with open(snapshot_path, "wb") as f:
                f.write(response.content)
            
            # Run identification and demographics
            face_id, age, gender = identify_visitor(snapshot_path)
            
            # Cleanup
            if os.path.exists(snapshot_path):
                os.remove(snapshot_path)
                
            return face_id, age, gender
    except Exception as e:
        print(f"  -> Failed to fetch snapshot from Frigate API: {e}")
        
    return "Unknown", None, None

def send_telegram_alert(face_id, tag, age, gender):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    msg = f"🚨 *Alerta Retail Analytics*\nO visitante `{face_id}` ({tag}) acabou de entrar na loja!"
    if age and gender:
        msg += f"\n*Perfil:* {gender}, ~{age} anos"
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    
    try:
        requests.post(url, json=payload, timeout=3)
        print(f"  [>] Telegram alert sent for {face_id}")
    except Exception as e:
        print(f"  [!] Failed to send Telegram alert: {e}")

def save_visit(event_data):
    after = event_data.get("after", {})
    label = after.get("label")
    
    if label != "person":
        return

    tracking_id = after.get("id")
    camera_name = after.get("camera", "default")
    start_time = after.get("start_time")
    end_time = event_data.get("type") == "end" and time.time() or None
    
    if not end_time:
        end_time = start_time + 10 # Fallback
        
    dwell_time = end_time - start_time
    
    # Extract zones entered during the visit
    entered_zones = ",".join(after.get("entered_zones", []))
    
    # Check if Frigate natively assigned a face (manual training)
    face_id = after.get("sub_label")
    age = None
    gender = None
    
    # If not recognized natively, run our Zero-Shot Re-ID and demographics
    if not face_id or face_id == "Unknown" or face_id == "":
        face_id, age, gender = process_reid(tracking_id)

    # Save to Database using SQLAlchemy
    try:
        with engine.begin() as conn:
            # Check if visit exists to do a DB-agnostic Upsert
            existing = conn.execute(text("SELECT tracking_id FROM visits WHERE tracking_id = :tid"), {"tid": tracking_id}).fetchone()
            
            if existing:
                conn.execute(text("""
                    UPDATE visits SET 
                        face_id=:fid, end_time=:end_t, dwell_time_seconds=:dwell, 
                        entered_zones=:zones, estimated_age=:age, estimated_gender=:gender, camera_name=:cam
                    WHERE tracking_id=:tid
                """), {
                    "fid": face_id, "end_t": end_time, "dwell": dwell_time,
                    "zones": entered_zones, "age": age, "gender": gender, "cam": camera_name, "tid": tracking_id
                })
            else:
                conn.execute(text("""
                    INSERT INTO visits 
                    (tracking_id, face_id, start_time, end_time, dwell_time_seconds, entered_zones, estimated_age, estimated_gender, camera_name)
                    VALUES (:tid, :fid, :start_t, :end_t, :dwell, :zones, :age, :gender, :cam)
                """), {
                    "tid": tracking_id, "fid": face_id, "start_t": start_time, "end_t": end_time,
                    "dwell": dwell_time, "zones": entered_zones, "age": age, "gender": gender, "cam": camera_name
                })
            
            # Módulo 2: Checar se o visitante está na Watch List para emitir alerta
            watch_match = conn.execute(text("SELECT tag FROM watch_list WHERE face_id = :fid"), {"fid": face_id}).fetchone()
            if watch_match:
                tag = watch_match[0]
                send_telegram_alert(face_id, tag, age, gender)
                
        print(f"[+] Visit Logged: ID={tracking_id} | Face={face_id} | Cam={camera_name} | Dwell={dwell_time:.1f}s")
    except Exception as e:
        print(f"[-] Error saving visit: {e}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"Connected to MQTT Broker at {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC)
        print(f"Subscribed to topic: {MQTT_TOPIC}")
    else:
        print(f"Failed to connect, return code {rc}\n")

def save_heatmap_point(event_data):
    after = event_data.get("after", {})
    if after.get("label") != "person":
        return
        
    tracking_id = after.get("id")
    box = after.get("box") # [xmin, ymin, xmax, ymax]
    
    if not tracking_id or not box or len(box) != 4:
        return
        
    current_time = time.time()
    # Throttle: Only save 1 point per second per tracking_id
    if current_time - last_heatmap_time.get(tracking_id, 0) < 1.0:
        return
        
    last_heatmap_time[tracking_id] = current_time
    
    # Calculate bottom center point (feet location is best for retail heatmaps)
    x = (box[0] + box[2]) / 2.0
    y = box[3] # ymax is the bottom of the bounding box
    
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO heatmap_points (tracking_id, x, y) VALUES (:tid, :x, :y)"), 
                         {"tid": tracking_id, "x": x, "y": y})
    except Exception as e:
        pass

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        event_type = payload.get("type")
        
        if event_type == "end":
            save_visit(payload)
        elif event_type == "update":
            # Módulo 3: Coletar posições para o Mapa de Calor
            save_heatmap_point(payload)
            
    except Exception as e:
        print(f"Error parsing MQTT message: {e}")

if __name__ == "__main__":
    print("Starting Retail Analytics Daemon with Zero-Shot Re-ID...")
    init_db()
    load_embeddings()
    
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_forever()
    except Exception as e:
        print(f"Connection failed: {e}")
