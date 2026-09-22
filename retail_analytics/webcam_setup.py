"""Persist webcam setup and produce a self-contained Linux Edge kit."""

import io
import re
import uuid
import zipfile
from pathlib import Path

import yaml
from sqlalchemy import text


def init_webcam_settings(engine) -> None:
    """Add optional settings without changing previously enrolled sources."""
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS webcam_settings (
                tenant_id VARCHAR NOT NULL,
                camera_id VARCHAR(32) NOT NULL,
                device_path VARCHAR(64) NOT NULL,
                pixel_format VARCHAR(16) NOT NULL,
                entry_direction VARCHAR(16) NOT NULL,
                PRIMARY KEY (tenant_id, camera_id),
                UNIQUE (tenant_id, device_path),
                FOREIGN KEY (tenant_id, camera_id)
                    REFERENCES traffic_webcam_sources(tenant_id, camera_id)
                    ON DELETE CASCADE
            )
        """)
        )


def list_webcams(engine, tenant_id: str) -> list[dict]:
    """Return owned webcams, including sources enrolled by older agents."""
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("""
            SELECT s.camera_id, s.name,
                COALESCE(w.device_path, '/dev/video0') AS device_path,
                COALESCE(w.pixel_format, 'mjpeg') AS pixel_format,
                COALESCE(w.entry_direction, 'left_to_right') AS entry_direction
            FROM traffic_webcam_sources s LEFT JOIN webcam_settings w
                ON s.tenant_id=w.tenant_id AND s.camera_id=w.camera_id
            WHERE s.tenant_id=:tid AND NOT EXISTS (SELECT 1 FROM browser_camera_sources b WHERE b.tenant_id=s.tenant_id AND b.camera_id=s.camera_id) ORDER BY s.name, s.camera_id
        """),
                {"tid": tenant_id},
            ).mappings()
        ]


def validate_webcam(
    name: str, device_path: str, pixel_format: str, entry_direction: str
) -> None:
    """Reject paths and options that cannot safely map a Linux video device."""
    if not name.strip() or len(name.strip()) > 100:
        raise ValueError("Informe um nome de até 100 caracteres.")
    if not re.fullmatch(r"/dev/video[0-9]{1,3}", device_path):
        raise ValueError("Use um dispositivo como /dev/video0 ou /dev/video1.")
    if pixel_format not in ("mjpeg", "yuyv422"):
        raise ValueError("Formato de webcam inválido.")
    if entry_direction not in ("left_to_right", "right_to_left"):
        raise ValueError("Sentido de entrada inválido.")


def save_webcam(
    engine,
    tenant_id: str,
    name: str,
    device_path: str,
    pixel_format: str,
    entry_direction: str,
    camera_id: str | None = None,
) -> str:
    """Save owned webcam settings atomically and preserve its crossing history."""
    validate_webcam(name, device_path, pixel_format, entry_direction)
    sources = list_webcams(engine, tenant_id)
    if camera_id and camera_id not in {item["camera_id"] for item in sources}:
        raise ValueError("Webcam não encontrada.")
    if any(
        item["device_path"] == device_path and item["camera_id"] != camera_id
        for item in sources
    ):
        raise ValueError("Esta webcam já está cadastrada. Selecione-a para editar.")
    camera_id = camera_id or uuid.uuid4().hex
    params = {
        "tid": tenant_id,
        "cid": camera_id,
        "name": name.strip(),
        "device": device_path,
        "format": pixel_format,
        "direction": entry_direction,
    }
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO traffic_webcam_sources (tenant_id, camera_id, name)
            VALUES (:tid, :cid, :name)
            ON CONFLICT (tenant_id, camera_id) DO UPDATE SET name=excluded.name
        """),
            params,
        )
        conn.execute(
            text("""
            INSERT INTO webcam_settings
                (tenant_id, camera_id, device_path, pixel_format, entry_direction)
            VALUES (:tid, :cid, :device, :format, :direction)
            ON CONFLICT (tenant_id, camera_id) DO UPDATE SET
                device_path=excluded.device_path, pixel_format=excluded.pixel_format,
                entry_direction=excluded.entry_direction
        """),
            params,
        )
    return camera_id


def webcam_config(source: dict) -> dict:
    """Create a CPU-only Frigate config with an explicit two-zone counting gate."""
    validate_webcam(
        source["name"],
        source["device_path"],
        source["pixel_format"],
        source["entry_direction"],
    )
    left = "0,0,0.42,0,0.42,1,0,1"
    right = "0.58,0,1,0,1,1,0.58,1"
    outside, inside = (
        (left, right) if source["entry_direction"] == "left_to_right" else (right, left)
    )
    return {
        "mqtt": {"enabled": True, "host": "mqtt", "topic_prefix": "aivo_webcam"},
        "detectors": {"cpu": {"type": "cpu", "num_threads": 2}},
        "record": {"enabled": False},
        "snapshots": {"enabled": False},
        "auth": {"enabled": False},
        "cameras": {
            "webcam_notebook": {
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "/dev/video0",
                            "roles": ["detect"],
                            "input_args": "-f v4l2 -input_format "
                            + source["pixel_format"]
                            + " -video_size 640x480 -framerate 30",
                        }
                    ]
                },
                "detect": {"enabled": True, "width": 640, "height": 480, "fps": 5},
                "objects": {"track": ["person"]},
                "zones": {
                    "aivo_outside": {
                        "coordinates": outside,
                        "inertia": 3,
                        "loitering_time": 0,
                    },
                    "aivo_inside": {
                        "coordinates": inside,
                        "inertia": 3,
                        "loitering_time": 0,
                    },
                },
            }
        },
        "version": "0.18-0",
    }


def build_webcam_kit(source: dict, tenant_id: str) -> bytes:
    """Bundle code and configuration, never cloud credentials or camera images."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tenant_id):
        raise ValueError("Invalid tenant identity")
    cid = source["camera_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", cid):
        raise ValueError("Invalid camera identity")
    config = webcam_config(source)
    shared = {
        "build": {"context": ".", "dockerfile": "Dockerfile.counter"},
        "restart": "no",
        "env_file": [".env"],
        "secrets": ["tenant_api_key"],
        "environment": {"TENANT_API_KEY_FILE": "/run/secrets/tenant_api_key"},
    }
    compose = {
        "name": "aivo-webcam-" + cid[:12],
        "services": {
            "mqtt": {
                "image": "eclipse-mosquitto:2",
                "restart": "no",
                "command": "mosquitto -c /mosquitto/config/mosquitto.conf",
                "volumes": ["./mosquitto.conf:/mosquitto/config/mosquitto.conf:ro"],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "mosquitto_pub",
                        "-h",
                        "127.0.0.1",
                        "-t",
                        "healthcheck",
                        "-m",
                        "ok",
                    ],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 5,
                },
            },
            "frigate": {
                "image": "ghcr.io/blakeblackshear/frigate@sha256:9678a83a76e4730ac7d9ea7428370e32ae656d6b312aaad30d6c69f3fef14d35",
                "restart": "no",
                "shm_size": "256mb",
                "devices": [source["device_path"] + ":/dev/video0"],
                "volumes": ["./config:/config", "/etc/localtime:/etc/localtime:ro"],
                "tmpfs": ["/tmp/cache:size=268435456", "/media/frigate:size=134217728"],
                "ports": ["127.0.0.1:5000:5000"],
                "depends_on": {"mqtt": {"condition": "service_healthy"}},
            },
            "counter": {
                **shared,
                "environment": {
                    **shared["environment"],
                    "MQTT_BROKER": "mqtt",
                    "MQTT_TOPIC_PREFIX": "aivo_webcam",
                    "FRIGATE_CONFIG_PATH": "/frigate/config.yml",
                    "COUNTING_CAMERA_NAME": "webcam_notebook",
                    "COUNTER_DATABASE": "/data/counter.db",
                },
                "volumes": ["./config:/frigate:ro", "./state:/data"],
                "depends_on": {"mqtt": {"condition": "service_healthy"}},
            },
            "live-preview": {
                **shared,
                "profiles": ["live"],
                "command": ["python", "edge_live_preview.py"],
                "depends_on": {"frigate": {"condition": "service_healthy"}},
            },
        },
        "secrets": {"tenant_api_key": {"file": "./.secrets/tenant_api_key"}},
    }
    env = (
        "CLOUD_API_URL=https://frigate.agenticx.ia.br\nCOUNTING_CAMERA_ID="
        + cid
        + "\nEXPECTED_TENANT_ID="
        + tenant_id
        + "\n"
    )
    setup = """#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
umask 077
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo 'Instale Docker Engine e Docker Compose no computador Linux da webcam.'; exit 1
fi
if [[ ! -c DEVICE_PATH ]]; then
  echo 'Webcam não encontrada. Confira o dispositivo no assistente e baixe o pacote novamente.'; exit 1
fi
mkdir -p .secrets state
if [[ ! -s .secrets/tenant_api_key ]]; then
  read -rsp 'Chave da loja (TENANT_API_KEY): ' aivo_key
  printf '\\n'
  [[ -n "$aivo_key" ]] || exit 1
  printf '%s' "$aivo_key" > .secrets/tenant_api_key
  unset aivo_key
fi
chmod 600 .secrets/tenant_api_key
docker compose run --rm --build --no-deps counter python verify_setup.py
if [[ "${1:-}" == "--com-imagem" ]]; then
  docker compose --profile live up -d --build
else
  docker compose up -d --build
fi
echo 'Serviços iniciados. Confira Entradas e Saídas no painel. Para parar: bash parar.sh'
""".replace("DEVICE_PATH", source["device_path"])
    verify = '''"""Verify the key belongs to the configured store before opening the webcam."""
import os
from pathlib import Path
import httpx
key = Path(os.environ["TENANT_API_KEY_FILE"]).read_text().strip()
try:
    response = httpx.get(os.environ["CLOUD_API_URL"] + "/api/config/cameras",
                        headers={"x-api-key": key}, timeout=15, follow_redirects=False)
    response.raise_for_status()
    if response.json()["tenant_id"] != os.environ["EXPECTED_TENANT_ID"]:
        raise ValueError("Wrong store")
except (httpx.HTTPError, KeyError, ValueError):
    raise SystemExit("Chave inválida, de outra loja ou conexão indisponível. Confira .secrets/tenant_api_key.") from None
print("Loja confirmada")
'''
    files = {
        "compose.yml": yaml.safe_dump(compose, sort_keys=False),
        "config/config.yml": yaml.safe_dump(config, sort_keys=False),
        ".env": env,
        "iniciar.sh": setup,
        "parar.sh": '#!/usr/bin/env bash\nset -euo pipefail\ncd -- "$(dirname -- "$0")"\ndocker compose --profile live stop\n',
        "verify_setup.py": verify,
        ".dockerignore": ".secrets/\n.env\nstate/\nconfig/\n",
        "mosquitto.conf": "listener 1883\nallow_anonymous true\npersistence false\n",
        "LEIA-ME.txt": (
            "AIVO | Webcam local (Linux + Docker)\n\n"
            "1. Extraia este ZIP em uma pasta no computador da webcam.\n"
            "2. Feche outros aplicativos que estejam usando a câmera.\n"
            "3. Abra o terminal nessa pasta e execute: bash iniciar.sh\n"
            "   Para também transmitir imagem ao painel: bash iniciar.sh --com-imagem\n"
            "4. Informe a chave da sua loja quando solicitada (não acompanha o ZIP).\n"
            "5. Abra Entradas e Saídas no painel e selecione esta webcam.\n"
            "6. Para desligar câmera, contagem e imagem: bash parar.sh\n\n"
            "A imagem é opcional, atualizada uma vez por segundo e visível após login.\n"
            "Para mudar de imagem+contagem para só contagem, pare antes de iniciar.\n"
            "Teste com o corpo visível atravessando os dois lados da imagem.\n"
            "Uma entrada atravessa de "
            + (
                "esquerda para direita"
                if source["entry_direction"] == "left_to_right"
                else "direita para esquerda"
            )
            + ".\n"
            "O movimento contrário é uma saída. Não equivale à ocupação da loja.\n"
            "Aguarde a inicialização e a primeira detecção; o CPU pode ser lento.\n"
            "A calibração inicial é para testes; ajuste as zonas ao posicionar na loja.\n"
            "Após editar dispositivo ou sentido no painel, pare os serviços e substitua\n"
            "os arquivos pelo novo pacote na MESMA pasta, preservando state e .secrets.\n"
            "Para diagnóstico: docker compose logs --tail 60 frigate counter\n"
            "Se a câmera não abrir, confira o dispositivo e tente YUYV no assistente.\n"
            "A porta local 5000 precisa estar livre. Nenhuma porta precisa ser aberta no roteador.\n"
        ),
    }
    root = Path(__file__).parent
    for filename in (
        "Dockerfile.counter",
        "requirements.counter.txt",
        "crossing_counter.py",
        "edge_counter.py",
        "edge_live_preview.py",
    ):
        files[filename] = (root / filename).read_text()
    files["Dockerfile.counter"] += "\nCOPY verify_setup.py ./\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return buffer.getvalue()
