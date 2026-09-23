"""The tenant-authenticated Aivo store dashboard."""

import logging

import bcrypt
import streamlit as st
from camera_store import init_camera_db
from db_config import engine, init_db
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from traffic_store import init_traffic_db

logger = logging.getLogger(__name__)
st.set_page_config(
    page_title="Aivo · Inteligência para sua loja", page_icon="◉", layout="wide"
)
st.markdown(
    """
<style>
.stApp {background: #f5f7fb; color: #172338;}
[data-testid="stSidebar"] {background: #fff; border-right: 1px solid #e2e8f0;}
[data-testid="stMetric"] {background: white; border: 1px solid #dce4ef;
    border-radius: 14px; padding: 18px; box-shadow: 0 3px 12px #17233808;}
[data-testid="stMetricValue"] {color: #175cd3;}
h1, h2, h3 {letter-spacing: -.025em;}
h1 {font-size: 2rem !important;} h2 {font-size: 1.45rem !important;} h3 {font-size: 1.15rem !important;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 14px;}
.setup-step {padding: 12px 0 20px; min-height: 100px;}
.setup-step span {font-size: .78rem; font-weight: 600;}
.setup-step strong {display: block; font-size: .95rem; margin: 7px 0;}
.setup-step small {color: #64748b;}
button[kind="primary"] {border-radius: 9px; background: #175cd3; border-color: #175cd3; color: white;}
@media (max-width: 640px) {h1 {font-size: 1.6rem !important;} .setup-step {min-height: 0; padding: 6px 0;}}
[data-testid="stMainBlockContainer"] {padding-top: 2rem; max-width: 1360px;}
</style>
""",
    unsafe_allow_html=True,
)

init_db()
init_camera_db(engine)
init_traffic_db(engine)
st.session_state.setdefault("tenant_id", None)
st.session_state.setdefault("tenant_name", None)


def login():
    """Authenticate a store account, independently of observed visitors."""
    _, content, _ = st.columns([1, 1.3, 1])
    with content:
        st.title("Aivo")
        st.subheader("Sua loja, em números.")
        st.caption("Acompanhe o movimento e os visitantes em um só lugar.")
        with st.form("login_form"):
            username = st.text_input("Usuário")
            password = st.text_input("Senha", type="password")
            if st.form_submit_button(
                "Entrar", type="primary", use_container_width=True
            ):
                try:
                    with engine.connect() as conn:
                        user = conn.execute(
                            text(
                                "SELECT tenant_id, name, password_hash FROM tenants WHERE username=:u"
                            ),
                            {"u": username},
                        ).first()
                    if user and bcrypt.checkpw(password.encode(), user[2].encode()):
                        st.session_state["tenant_id"] = user[0]
                        st.session_state["tenant_name"] = user[1]
                        st.rerun()
                    else:
                        st.error("Usuário ou senha inválidos.")
                except (SQLAlchemyError, ValueError):
                    logger.warning("Dashboard login unavailable")
                    st.error("Não foi possível entrar. Tente novamente em instantes.")


def logout():
    """Revoke camera capture and remove the authenticated state."""
    from browser_dashboard import close_browser_session

    close_browser_session(engine)
    from camera_media import close_dashboard_media

    close_dashboard_media(engine, st.session_state)
    st.session_state.clear()
    st.rerun()


if not st.session_state["tenant_id"]:
    login()
    st.stop()

tid = st.session_state["tenant_id"]

request = st.session_state.pop("nav_request", None)
if request:
    st.session_state["main_page"] = request["page"]
    if request.get("section"):
        st.session_state["camera_section"] = request["section"]
    if request.get("camera_id"):
        for key in ("live_camera", "zone_camera", "history_camera", "behavior_camera"):
            st.session_state[key] = request["camera_id"]
st.sidebar.title("Aivo")
st.sidebar.text(st.session_state["tenant_name"])
st.sidebar.caption("INTELIGÊNCIA PARA SUA LOJA")
dev = st.session_state.get("dev_tools", False)
options = ["Resumo", "Visitantes", "Análise da loja", "Câmeras"] + (
    ["Desenvolvimento"] if dev else []
)
if st.session_state.get("main_page") not in options:
    st.session_state["main_page"] = "Resumo"
page = st.sidebar.radio(
    "Navegação", options, key="main_page", label_visibility="collapsed"
)
st.sidebar.divider()
with st.sidebar.expander("Ferramentas de desenvolvimento"):
    if st.checkbox("Ativar ferramentas de teste", key="dev_tools") != dev:
        st.rerun()
st.sidebar.caption("Dados reais · Horários de Brasília")
if st.sidebar.button("Sair", use_container_width=True):
    logout()
section = None
if page == "Câmeras":
    section = st.radio(
        "Área de câmeras",
        ["Ao vivo", "Zonas e gravação", "Histórico", "Cadastro"],
        horizontal=True,
        key="camera_section",
    )
if page != "Câmeras" or section != "Ao vivo":
    from camera_media import close_dashboard_media

    close_dashboard_media(engine, st.session_state)
if page != "Desenvolvimento":
    from browser_dashboard import close_browser_session

    close_browser_session(engine)
if not (page == "Câmeras" and section == "Cadastro") and page != "Desenvolvimento":
    from camera_dashboard import clear_camera_wizard

    clear_camera_wizard()
try:
    if page == "Resumo":
        from traffic_dashboard import render_traffic_counter

        render_traffic_counter(engine, tid)
    elif page == "Visitantes":
        from visitor_dashboard import render_visitors

        render_visitors(engine, tid)
    elif page == "Análise da loja":
        from retail_dashboard import render_behavior

        render_behavior(engine, tid)
    elif page == "Câmeras":
        from camera_dashboard import render_camera_settings
        from retail_dashboard import render_cameras, render_history, render_zones

        if section == "Cadastro":
            render_camera_settings(engine, tid)
        else:
            {
                "Ao vivo": render_cameras,
                "Zonas e gravação": render_zones,
                "Histórico": render_history,
            }[section](engine, tid)
    elif page == "Desenvolvimento":
        tool = st.radio(
            "Teste",
            [
                "Contagens de teste",
                "Visitantes de teste",
                "Webcam no navegador",
                "Cadastrar fonte de teste",
            ],
            horizontal=True,
        )
        if tool == "Contagens de teste":
            from browser_dashboard import close_browser_session

            close_browser_session(engine)
            from traffic_dashboard import render_traffic_counter

            render_traffic_counter(engine, tid, development=True)
        elif tool == "Visitantes de teste":
            from browser_dashboard import close_browser_session
            from visitor_dashboard import render_visitors

            close_browser_session(engine)
            render_visitors(engine, tid, development=True)
        elif tool == "Webcam no navegador":
            from browser_dashboard import render_browser_camera

            render_browser_camera(engine, tid)
        else:
            from camera_dashboard import render_camera_settings

            render_camera_settings(engine, tid, allow_webcam=True)
except (SQLAlchemyError, ValueError, OSError, PermissionError):
    st.error("Não foi possível carregar esta página. Tente novamente em instantes.")
