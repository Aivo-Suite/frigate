import requests
import time
import random
import uuid

API_URL = "https://frigate.agenticx.ia.br/api"
API_KEY = "23891e90306e4afba996ac2a0018791a"
HEADERS = {"x-api-key": API_KEY}

print("Simulando envio de tráfego (Frigate Edge) para a Nuvem (SaaS)...")

# Simular 3 visitantes
visitors = [
    {"face_id": "VISITOR_A1B2C3", "age": 28, "gender": "Woman", "cam": "camera_frente"},
    {"face_id": "VISITOR_X9Y8Z7", "age": 45, "gender": "Man", "cam": "camera_corredor"},
    {"face_id": "VISITOR_L5M6N7", "age": 32, "gender": "Woman", "cam": "camera_frente"}
]

for v in visitors:
    tracking_id = f"test_{int(time.time())}_{random.randint(100, 999)}"
    start_time = time.time() - random.randint(30, 300)
    end_time = time.time()
    
    # 1. Enviar Visita
    visit_payload = {
        "tracking_id": tracking_id,
        "face_id": v["face_id"],
        "start_time": start_time,
        "end_time": end_time,
        "dwell_time_seconds": end_time - start_time,
        "entered_zones": "entrada,corredor_1",
        "estimated_age": v["age"],
        "estimated_gender": v["gender"],
        "camera_name": v["cam"]
    }
    
    print(f"Enviando visita: {v['face_id']}...")
    res = requests.post(f"{API_URL}/visits", json=visit_payload, headers=HEADERS)
    print(f"Status: {res.status_code} - {res.text}")
    
    # 2. Enviar alguns pontos de Heatmap para esta visita
    for _ in range(10):
        hm_payload = {
            "tracking_id": tracking_id,
            "x": random.uniform(100, 800),
            "y": random.uniform(100, 600)
        }
        requests.post(f"{API_URL}/heatmap", json=hm_payload, headers=HEADERS)
        
    time.sleep(1)

print("Mock de dados concluído! Atualize o dashboard.")
