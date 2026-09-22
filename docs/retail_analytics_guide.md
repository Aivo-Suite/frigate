# Guia de Operação e Lançamento: Retail Analytics com Frigate

A base da arquitetura de Retail Analytics foi construída! Criamos um serviço auxiliar que fica ao lado do Frigate para processar dados de negócios (Analytics), focando em demografia, zonas de calor, multi-câmeras e reidentificação.

## Arquitetura Base

1. **Bancos de Dados Híbrido (`db_config.py`):** O sistema roda em SQLite local para testes rápidos, mas está arquitetado para usar PostgreSQL em produção pesada, garantindo escalabilidade comercial.
2. **`analytics_daemon.py`:** Um "robozinho" em Python que se conecta silenciosamente ao MQTT do Frigate (`frigate/events`). Ele assiste a cada pessoa detectada, captura fotos, extrai características físicas e rastreia o caminho.
3. **`dashboard.py` (Streamlit):** Uma interface web rica e focada em relatórios que lê o banco de dados e exibe métricas completas de tempo, calor e demografia.

## Pré-requisitos e Instalação

Você precisará instalar as dependências de Python no ambiente onde o Frigate está rodando.

**Instale as dependências principais e de IA:**
```bash
pip install paho-mqtt streamlit pandas deepface tf-keras matplotlib seaborn sqlalchemy psycopg2-binary
```

## Como colocar para rodar (Modo Local / Testes)

Abra dois terminais (ou configure via Docker Compose futuramente).

1. No Terminal 1, inicie o robô que vai ler os eventos:
```bash
cd retail_analytics
python analytics_daemon.py
```

2. No Terminal 2, inicie a interface de relatórios:
```bash
cd retail_analytics
streamlit run dashboard.py
```
Isso abrirá uma página web automaticamente no seu navegador (normalmente `http://localhost:8501`) com o seu Dashboard.

---

## Módulos Avançados (Fase 2 e 3)

### Reidentificação Automática de Anônimos (Zero-Shot Re-ID)
Sempre que uma pessoa sai da loja, o robô solicita a "foto do rosto" dela na API do Frigate, transforma em vetor matemático e compara com visitantes passados.
* A similaridade para agrupar clientes é de >70%. Clientes agrupados aparecem em "Visitantes Conhecidos".

### Demografia Automática (Idade e Gênero)
Aproveitamos a mesma foto de reconhecimento para plugar os analisadores do DeepFace.
* **Dashboard:** Mostra Distribuição por Gênero e Faixa Etária.

### Alertas em Tempo Real (VIPs e Blacklist)
Se a IA confirmar que uma pessoa detectada está na sua *Watch List*, um alerta chega na hora.
* **Configuração:**
  ```bash
  export TELEGRAM_BOT_TOKEN="seu_token_aqui"
  export TELEGRAM_CHAT_ID="seu_chat_id_aqui"
  python analytics_daemon.py
  ```

### Mapas de Calor (Heatmaps)
O robô filtra e grava a posição X/Y dos clientes na câmera a 1 ponto por segundo.
* **Visualização:** A aba "Mapa de Calor" no Dashboard renderiza nuvens de movimento (KDE Plot).

### Jornada Multi-câmera
O daemon extrai a câmera atual via MQTT. A aba "Jornada do Cliente" desenha o fluxo entre câmeras: `cam_entrada (10:00:05) ➔ cam_corredor (10:05:22)`.

---

## Modo Produção: PostgreSQL + pgvector

Para suportar busca de milhares de vetores DeepFace (128 dimensões) muito rápido, utilize o Postgres:

1. **Levantar Banco Profissional:**
```bash
docker compose -f docker-compose.postgres.yml up -d
```

2. **Migrar os Dados Antigos (se houver):**
```bash
export DATABASE_URL="postgresql://retail_user:retail_password@localhost:5432/retail_db"
python migrate_db.py
```

3. **Rodar Robôs apontando para o Postgres:**
Basta manter a variável de ambiente ligada:
```bash
export DATABASE_URL="postgresql://retail_user:retail_password@localhost:5432/retail_db"
python analytics_daemon.py
```
