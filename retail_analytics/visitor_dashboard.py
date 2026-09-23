"""A paginated, tenant-private visitor photo gallery."""

import base64
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError
from traffic_store import list_counting_sources
from visitor_store import list_visitors

ZONE = ZoneInfo("America/Sao_Paulo")


def day_bounds(day):
    """Return the selected local day's half-open timestamp interval."""
    return (
        datetime.combine(day, time.min, ZONE).timestamp(),
        datetime.combine(day + timedelta(days=1), time.min, ZONE).timestamp(),
    )


def render_visitors(engine, tenant_id):
    """Show saved observations without inferring identities across visits."""
    if st.session_state.get("tenant_id") != tenant_id:
        return
    st.title("Visitantes")
    st.caption(
        "Pessoas observadas, com foto e identificador para futura integração com seu CRM."
    )
    left, right = st.columns(2)
    day = left.date_input("Data", datetime.now(ZONE).date(), key="visitor_day")
    try:
        names = {
            c["camera_id"]: c["name"] for c in list_counting_sources(engine, tenant_id)
        }
        camera = right.selectbox(
            "Câmera",
            [None, *names],
            format_func=lambda c: names.get(c, "Todas as câmeras"),
        )
        start, end = day_bounds(day)
        first = list_visitors(engine, tenant_id, start, end, camera)
        a, b = st.columns(2)
        a.metric("Visitantes registrados", first["total"])
        b.metric("Com foto", first["with_photo"])
        st.caption(
            "Um cadastro por rastreamento contínuo. Retornos ou perda de acompanhamento podem gerar outro cadastro para a mesma pessoa."
        )
        if not first["total"]:
            st.info(
                "Nenhum visitante registrado nesta data. Inicie a webcam e permaneça visível por alguns instantes para gerar o primeiro cadastro real."
            )
            return
        pages = max(1, (first["total"] + 11) // 12)
        page = st.selectbox("Página", range(1, pages + 1))
        result = (
            first
            if page == 1
            else list_visitors(engine, tenant_id, start, end, camera, (page - 1) * 12)
        )
        for index, visitor in enumerate(result["items"]):
            if index % 3 == 0:
                columns = st.columns(3)
            with columns[index % 3], st.container(border=True):
                if visitor["photo"]:
                    encoded = base64.b64encode(bytes(visitor["photo"])).decode("ascii")
                    st.markdown(
                        '<img alt="Foto do visitante" style="width:100%;max-width:180px;border-radius:12px" src="data:image/jpeg;base64,'
                        + encoded
                        + '">',
                        unsafe_allow_html=True,
                    )
                else:
                    st.info("Foto não recebida")
                st.subheader("Visitante " + visitor["visitor_id"][:8])
                st.text(names.get(visitor["camera_id"], "Câmera removida"))
                st.caption(
                    datetime.fromtimestamp(visitor["first_seen"], ZONE).strftime(
                        "%d/%m/%Y · %H:%M:%S"
                    )
                )
                st.write(f"Entradas: {visitor['entries']} · Saídas: {visitor['exits']}")
                with st.expander("Identificador para integração"):
                    st.code(visitor["visitor_id"], language=None)
        if st.button("Atualizar visitantes"):
            st.rerun()
    except SQLAlchemyError:
        st.error(
            "Não foi possível carregar os visitantes. Tente novamente em instantes."
        )
