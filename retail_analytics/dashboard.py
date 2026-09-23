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
[data-testid="stSidebar"] {background: #e9eef6;}
[data-testid="stMetric"] {background: white; border: 1px solid #dce4ef;
    border-radius: 16px; padding: 22px; box-shadow: 0 3px 12px #17233808;}
[data-testid="stMetricValue"] {color: #175cd3;}
h1, h2, h3 {letter-spacing: -.025em;}
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
    st.session_state.clear()
    st.rerun()


if not st.session_state["tenant_id"]:
    login()
    st.stop()

tid = st.session_state["tenant_id"]
st.sidebar.title("Aivo")
st.sidebar.text(st.session_state["tenant_name"])
st.sidebar.caption("INTELIGÊNCIA PARA SUA LOJA")
page = st.sidebar.radio(
    "Navegação",
    ["Visão geral", "Visitantes", "Webcam no navegador", "Configurar Câmeras"],
)
st.sidebar.divider()
st.sidebar.caption("Dados reais das câmeras conectadas")
if st.sidebar.button("Sair", use_container_width=True):
    logout()
if page not in ("Webcam no navegador", "Configurar Câmeras"):
    from browser_dashboard import close_browser_session

    close_browser_session(engine)
if page != "Configurar Câmeras":
    from camera_dashboard import clear_camera_wizard

    clear_camera_wizard()
if page == "Visão geral":
    from traffic_dashboard import render_traffic_counter

    render_traffic_counter(engine, tid)
elif page == "Visitantes":
    from visitor_dashboard import render_visitors

    render_visitors(engine, tid)
elif page == "Webcam no navegador":
    from browser_dashboard import render_browser_camera

    render_browser_camera(engine, tid)
else:
    from camera_dashboard import render_camera_settings

    render_camera_settings(engine, tid)
