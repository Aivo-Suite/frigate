import streamlit as st
import pandas as pd
from datetime import datetime, date
import matplotlib.pyplot as plt
import seaborn as sns
from sqlalchemy import text
from db_config import engine, init_db

# DB_PATH = "retail_analytics.db" - Removido (agora usa db_config)

import bcrypt

# Inicializar DB
init_db()

# --- Autenticação Multi-Tenant ---
if "tenant_id" not in st.session_state:
    st.session_state["tenant_id"] = None
if "tenant_name" not in st.session_state:
    st.session_state["tenant_name"] = None

def login():
    st.title("🔐 Retail Analytics Cloud")
    st.markdown("Acesse o painel da sua loja.")
    with st.form("login_form"):
        username = st.text_input("Usuário")
        password = st.text_input("Senha", type="password")
        submit = st.form_submit_button("Entrar")
        if submit:
            with engine.connect() as conn:
                user = conn.execute(
                    text("SELECT tenant_id, name, password_hash FROM tenants WHERE username = :u"),
                    {"u": username}
                ).fetchone()
                if user and bcrypt.checkpw(password.encode(), user[2].encode()):
                    st.session_state["tenant_id"] = user[0]
                    st.session_state["tenant_name"] = user[1]
                    st.rerun()
                else:
                    st.error("Credenciais inválidas.")

def logout():
    from browser_dashboard import close_browser_session
    close_browser_session(engine)
    st.session_state.clear()
    st.rerun()

if not st.session_state["tenant_id"]:
    login()
    st.stop()

tid = st.session_state["tenant_id"]
st.sidebar.title(f"🏢 {st.session_state['tenant_name']}")
if st.sidebar.button("Sair"):
    logout()
st.sidebar.divider()
page = st.sidebar.radio("Navegação", ["Entradas e Saídas", "Webcam no navegador", "Análise de Tráfego", "Configurar Câmeras"])
if page not in ("Webcam no navegador", "Configurar Câmeras"):
    from browser_dashboard import close_browser_session
    close_browser_session(engine)
if page == "Webcam no navegador":
    from camera_dashboard import clear_camera_wizard
    clear_camera_wizard()
    from browser_dashboard import render_browser_camera
    render_browser_camera(engine, tid)
    st.stop()
if page != "Configurar Câmeras":
    from camera_dashboard import clear_camera_wizard
    clear_camera_wizard()
if page == "Entradas e Saídas":
    from traffic_dashboard import render_traffic_counter
    render_traffic_counter(engine, tid)
    st.stop()

if page == "Configurar Câmeras":
    from camera_dashboard import render_camera_settings
    render_camera_settings(engine, tid)
    st.stop()


def load_data():
    try:
        df = pd.read_sql_query("SELECT * FROM visits WHERE tenant_id = %(tid)s", engine, params={"tid": tid})
        
        # Convert timestamps to datetime
        if not df.empty:
            df['start_datetime'] = pd.to_datetime(df['start_time'], unit='s')
            df['end_datetime'] = pd.to_datetime(df['end_time'], unit='s')
            df['date'] = df['start_datetime'].dt.date
            
        return df
    except Exception as e:
        return pd.DataFrame()

st.title("📊 Retail Analytics Dashboard")
st.markdown("Visualização de métricas de contagem de pessoas, reidentificação e tempo de permanência.")

df = load_data()

if df.empty:
    st.info("Ainda não há visitas analisadas. Os gráficos serão preenchidos quando houver dados reais enviados pelo servidor local. Configure uma câmera para começar; contagens da webcam ficam em Entradas e Saídas.")
else:
    # Filtro por data
    today = date.today()
    selected_date = st.date_input("Filtrar por data", today)
    
    # Filtrar os dados pela data selecionada
    df_filtered = df[df['date'] == selected_date]
    
    # Criar abas
    tab_geral, tab_heatmap, tab_jornada = st.tabs(["Visão Geral", "Mapa de Calor", "Jornada do Cliente"])
    
    with tab_geral:
        st.header(f"Resumo para o dia: {selected_date.strftime('%d/%m/%Y')}")
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            total_visits = len(df_filtered)
            st.metric(label="Rastreamentos registrados", value=total_visits)
            
        with col2:
            # Calcular visitantes conhecidos/recorrentes
            known_visitors = df_filtered[df_filtered['face_id'] != "Unknown"]['face_id'].nunique()
            st.metric(label="Visitantes Conhecidos (Únicos)", value=known_visitors)
            
        with col3:
            avg_dwell_time = df_filtered['dwell_time_seconds'].mean() if total_visits > 0 else 0
            st.metric(label="Tempo Médio (segundos)", value=f"{avg_dwell_time:.1f}s")
            
        with col4:
            total_unknown = len(df_filtered[df_filtered['face_id'] == "Unknown"])
            st.metric(label="Visitantes Desconhecidos", value=total_unknown)

        st.divider()

        # Módulo 2: Gerenciamento de Alertas
        st.sidebar.header("🚨 Gerenciar Alertas (Watch List)")
        st.sidebar.markdown("Adicione o ID de um visitante para receber alertas no Telegram quando ele entrar na loja.")
        
        with st.sidebar.form("watch_list_form", clear_on_submit=True):
            alert_face_id = st.text_input("Face ID do Visitante (ex: VISITOR_A1B2C3)")
            alert_tag = st.selectbox("Categoria", ["VIP", "Suspeito", "Funcionário"])
            submit_btn = st.form_submit_button("Adicionar à Watch List")
            
            if submit_btn and alert_face_id:
                try:
                    with engine.begin() as conn_wl:
                        conn_wl.execute(
                            text("INSERT INTO watch_list (tenant_id, face_id, tag) VALUES (:tid, :fid, :tag) ON CONFLICT(tenant_id, face_id) DO UPDATE SET tag=:tag"),
                            {"tid": tid, "fid": alert_face_id, "tag": alert_tag}
                        )
                    st.success(f"{alert_face_id} adicionado aos alertas!")
                except Exception as e:
                    st.error(f"Erro ao salvar: {e}")

        # Exibir Watch List atual no sidebar
        try:
            query_wl = text("SELECT face_id, tag FROM watch_list WHERE tenant_id = :tid")
            wl_df = pd.read_sql(query_wl, engine, params={"tid": tid})
            if not wl_df.empty:
                st.sidebar.markdown("**Alertas Ativos:**")
                st.sidebar.dataframe(wl_df, hide_index=True)
        except:
            pass
            
        # Módulo 1: Demografia
        st.subheader("Demografia do Público (Idade e Gênero)")
        
        demo_df = df_filtered.dropna(subset=['estimated_age', 'estimated_gender'])
        
        if not demo_df.empty:
            col_demo1, col_demo2 = st.columns(2)
            
            with col_demo1:
                st.markdown("**Distribuição por Gênero**")
                gender_counts = demo_df['estimated_gender'].value_counts()
                st.bar_chart(gender_counts)
                
            with col_demo2:
                st.markdown("**Faixa Etária**")
                # Create age bins for better visualization
                bins = [0, 18, 25, 35, 45, 55, 100]
                labels = ['0-18', '19-25', '26-35', '36-45', '46-55', '55+']
                demo_df['age_group'] = pd.cut(demo_df['estimated_age'], bins=bins, labels=labels, right=False)
                age_counts = demo_df['age_group'].value_counts().sort_index()
                st.bar_chart(age_counts)
        else:
            st.info("Ainda não há dados demográficos suficientes extraídos para a data de hoje.")

        st.divider()
        
        st.subheader("Últimas Visitas Registradas")
        
        # Formatação da tabela para exibição
        if not df_filtered.empty:
            display_df = df_filtered[['tracking_id', 'face_id', 'start_datetime', 'dwell_time_seconds', 'estimated_age', 'estimated_gender', 'entered_zones']].copy()
            display_df.columns = ["Tracking ID", "Face ID", "Entrada", "Tempo (s)", "Idade Est.", "Gênero Est.", "Zonas"]
            st.dataframe(display_df, use_container_width=True)
        else:
            st.info("Nenhuma visita registrada nesta data.")

    with tab_heatmap:
        st.subheader("Onde os clientes param na loja?")
        st.markdown("Zonas de calor indicam os locais onde as pessoas passam mais tempo (pontos densos).")
        
        try:
            # Busca pontos gravados na data selecionada
            query = f"SELECT x, y FROM heatmap_points WHERE date(date_recorded) = '{selected_date}' AND tenant_id = '{tid}'"
            df_hm = pd.read_sql_query(query, engine)
            
            if not df_hm.empty and len(df_hm) > 5: # Precisa de alguns pontos para o KDE funcionar
                fig, ax = plt.subplots(figsize=(8, 6))
                
                # Opcional: Inverter o eixo Y porque as coordenadas de imagem começam do topo
                ax.invert_yaxis()
                
                # Densidade via Seaborn KDE Plot
                sns.kdeplot(
                    x=df_hm['x'], y=df_hm['y'], 
                    cmap="inferno", fill=True, alpha=0.7, ax=ax,
                    thresh=0.05
                )
                
                # Scatter simples por baixo para dar contraste
                sns.scatterplot(x=df_hm['x'], y=df_hm['y'], color="white", s=5, alpha=0.3, ax=ax)
                
                ax.set_title("Concentração de Movimento (KDE)")
                ax.set_xlabel("Eixo X (Câmera)")
                ax.set_ylabel("Eixo Y (Câmera)")
                
                # Remove os números dos eixos para ficar mais clean
                ax.set_xticks([])
                ax.set_yticks([])
                
                st.pyplot(fig)
            else:
                st.info("Pontos de movimento insuficientes para gerar o mapa de calor nesta data.")
                
        except Exception as e:
            st.error(f"Erro ao carregar mapa de calor: {e}")

    with tab_jornada:
        st.subheader("Jornada Multi-Câmera por Cliente")
        st.markdown("Acompanhe o caminho contínuo de clientes identificados cruzando diferentes câmeras e zonas da loja no dia selecionado.")
        
        jornada_df = df_filtered[df_filtered['face_id'] != 'Unknown'].sort_values('start_datetime')
        if not jornada_df.empty and 'camera_name' in jornada_df.columns:
            # Agrupar por Face ID
            for face_id, group in jornada_df.groupby('face_id'):
                if len(group) > 1: # Apenas jornadas com mais de um passo
                    cameras = group['camera_name'].fillna('Desconhecida').tolist()
                    times = group['start_datetime'].dt.strftime('%H:%M:%S').tolist()
                    
                    # Desenho da jornada: Cam1 (10:00) -> Cam2 (10:05)
                    journey_str = " ➔ ".join([f"**{cam}** ({t})" for cam, t in zip(cameras, times)])
                    
                    with st.expander(f"👤 Visitante: {face_id}"):
                        st.markdown(f"**Caminho:** {journey_str}")
                        st.dataframe(group[['camera_name', 'start_datetime', 'dwell_time_seconds', 'entered_zones']].rename(
                            columns={'camera_name': 'Câmera', 'start_datetime': 'Início', 'dwell_time_seconds': 'Tempo (s)', 'entered_zones': 'Zonas'}
                        ), hide_index=True)
            
            # Se ninguém teve jornada longa
            if len(jornada_df.groupby('face_id').filter(lambda x: len(x) > 1)) == 0:
                st.info("Hoje, todos os clientes identificados foram vistos em apenas uma câmera/evento (sem jornada estendida).")
        else:
            st.info("Nenhuma jornada identificada ou o sistema ainda não está registrando o nome da câmera.")
