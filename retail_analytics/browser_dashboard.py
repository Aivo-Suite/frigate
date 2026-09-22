"""Embed a permission-based browser webcam without exposing store API keys."""

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from browser_store import (
    create_browser_source,
    issue_grant,
    list_browser_sources,
    revoke_grant,
)
from sqlalchemy.exc import SQLAlchemyError

_component = components.declare_component(
    "aivo_browser_camera", path=str(Path(__file__).with_name("browser_component"))
)


def close_browser_session(engine):
    """Revoke the current page's capability when it leaves capture or logs out."""
    grant = st.session_state.pop("browser_camera_grant", None)
    if grant:
        revoke_grant(engine, grant["tenant_id"], grant["grant_id"])


def render_browser_camera(engine, tenant_id):
    """Offer enrollment, local preview, visual calibration and explicit transmission."""
    if st.session_state.get("tenant_id") != tenant_id:
        return
    st.subheader("Webcam pelo navegador")
    st.caption(
        "Sem instalação. A prévia fica no seu computador; o envio à nuvem começa apenas ao iniciar o monitoramento. Sem áudio."
    )
    try:
        sources = list_browser_sources(engine, tenant_id)
        by_id = {s["camera_id"]: s for s in sources}
        if not sources:
            with st.form("browser_camera_name"):
                name = st.text_input(
                    "Nome da webcam", value="Webcam pelo navegador", max_chars=100
                )
                prepare = st.form_submit_button("Preparar webcam", type="primary")
            if prepare:
                try:
                    create_browser_source(engine, tenant_id, name)
                except ValueError as error:
                    st.error(str(error))
                else:
                    st.rerun()
            st.info(
                "Você precisará permitir a câmera no navegador. Mantenha esta página aberta e o computador ligado durante a contagem."
            )
            return
        cid = st.selectbox(
            "Webcam cadastrada",
            list(by_id),
            format_func=lambda key: by_id[key]["name"],
            key="browser_selected_source",
        )
        source = by_id[cid]
        grant = st.session_state.get("browser_camera_grant")
        if grant and (grant["camera_id"] != cid or grant["tenant_id"] != tenant_id):
            close_browser_session(engine)
            grant = None
        if grant is None:
            grant = {**issue_grant(engine, tenant_id, cid), "tenant_id": tenant_id}
            st.session_state["browser_camera_grant"] = grant
        result = _component(
            token=grant["token"],
            grant_id=grant["grant_id"],
            gate={k: source[k] for k in ("axis", "position", "positive_entry")},
            key="browser_capture_" + cid,
            default=None,
        )
        if (
            result
            and result.get("action") == "renew"
            and result.get("grant_id") == grant["grant_id"]
        ):
            close_browser_session(engine)
            st.rerun()
        st.caption(
            "Piloto: uma webcam em análise por vez, até 5 imagens por segundo. Após parar ou perder a conexão, prepare uma nova sessão. As contagens salvas permanecem em Entradas e Saídas."
        )
        st.info(
            "Conte movimentos com o corpo visível. Ajuste a linha à passagem real; perdas de detecção e cruzamentos simultâneos podem reduzir a precisão. Fechar esta página ou suspender o computador interrompe a análise."
        )
    except (SQLAlchemyError, PermissionError):
        st.error("Não foi possível preparar a webcam. Tente novamente em instantes.")
