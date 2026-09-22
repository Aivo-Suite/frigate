"""A compact entrance dashboard for the first optical-store pilot."""

import base64
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from live_preview import load_frame
from sqlalchemy.exc import SQLAlchemyError
from traffic_store import daily_report, list_counting_sources

logger = logging.getLogger(__name__)


@st.fragment(run_every="5s")
def render_traffic_counter(engine, tenant_id: str) -> None:
    """Display directional counts without labeling tracking records as visitors."""
    if st.session_state.get("tenant_id") != tenant_id:
        return
    st.title("Entradas e Saídas")
    st.caption(
        "Passagens observadas pela câmera da entrada. Uma mesma pessoa pode entrar ou sair mais de uma vez."
    )
    timezone_name = "America/Sao_Paulo"
    timezone = ZoneInfo(timezone_name)
    selected_date = st.date_input("Dia da contagem", datetime.now(timezone).date())
    st.caption("Horários de Brasília (America/Sao_Paulo).")
    if st.button("Atualizar contagem"):
        st.rerun()
    try:
        cameras = list_counting_sources(engine, tenant_id)
        names = {
            camera["camera_id"]: camera["name"]
            + (" (teste)" if camera["is_test"] else "")
            for camera in cameras
        }
        selected_camera = st.selectbox(
            "Câmera da entrada",
            list(names) if names else [""],
            format_func=lambda cid: names[cid] if cid else "Todas as câmeras",
        )
        report = daily_report(
            engine, tenant_id, selected_date, timezone_name, selected_camera or None
        )
    except SQLAlchemyError:
        logger.error("Directional traffic report failed")
        st.error("Não foi possível consultar a contagem. Tente novamente.")
        return
    if (
        selected_camera
        and next(
            camera for camera in cameras if camera["camera_id"] == selected_camera
        )["is_test"]
    ):
        st.warning(
            "Webcam de teste: contagens de movimentos reais captados pela câmera. Estes testes ainda não representam o fluxo de clientes da loja."
        )
    if (
        selected_camera
        and next(
            camera for camera in cameras if camera["camera_id"] == selected_camera
        )["is_test"]
    ):
        st.subheader("Webcam ao vivo")
        st.caption(
            "Prévia sem áudio, com atualização aproximada de uma imagem por segundo. Disponível enquanto o notebook transmite."
        )
        render_live_preview(tenant_id, selected_camera)
    if not cameras and not report["health"]:
        st.info(
            "Cadastre a câmera em Configurar Câmeras. Depois, ative o contador no servidor local e calibre os lados externo e interno da porta."
        )
    elif not report["health"]:
        st.warning(
            "Contador ainda sem comunicação. A ativação depende do servidor local e da calibração da entrada."
        )
    else:
        for health in report["health"]:
            name = names.get(health["camera_id"], "Câmera removida")
            fresh = time.time() - health["received_at"] < 120
            if not fresh:
                st.warning(
                    f"{name}: sem comunicação recente com o contador. Os totais podem estar incompletos."
                )
            elif not health["mqtt_connected"] or health["frigate_available"] is False:
                st.warning(
                    f"{name}: contador conectado à nuvem, mas a recepção dos eventos do Frigate está interrompida."
                )
            else:
                st.success(f"{name}: contador conectado à nuvem e ao MQTT.")
            seen = datetime.fromtimestamp(health["received_at"], timezone).strftime(
                "%d/%m %H:%M:%S"
            )
            st.caption(
                f"Última comunicação: {seen}. Eventos aguardando envio na última comunicação: {health['pending_events']}."
            )
    if report["entries"] == 0 and report["exits"] == 0:
        st.info(
            "Nenhum cruzamento recebido neste dia. Isso não confirma que a loja ficou sem movimento."
        )
    first, second, third = st.columns(3)
    first.metric("Entradas registradas", report["entries"])
    second.metric("Saídas registradas", report["exits"])
    third.metric("Saldo de passagens", report["net_flow"])
    st.caption(
        "O saldo é entradas menos saídas no período; não representa a quantidade de pessoas atualmente na loja. A contagem inclui funcionários e acompanhantes."
    )
    st.subheader("Movimento por hora")
    hourly = pd.DataFrame(report["hourly"])
    st.bar_chart(hourly, x="Hora", y=["Entradas", "Saídas"])
    st.download_button(
        "Baixar contagem por hora",
        hourly.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"contagem-{selected_date.isoformat()}.csv",
        mime="text/csv",
    )
    st.caption(
        "Dados atrasados são atribuídos ao horário em que a passagem ocorreu. Conectividade do contador não garante que a câmera esteja entregando vídeo; valide a imagem durante a instalação."
    )


@st.fragment(run_every="1s")
def render_live_preview(tenant_id: str, camera_id: str) -> None:
    """Send the image only through the authenticated Streamlit session."""
    if st.session_state.get("tenant_id") != tenant_id:
        return
    try:
        frame = load_frame(tenant_id, camera_id)
    except (OSError, RuntimeError):
        frame = None
    if frame is None:
        st.info(
            "Webcam offline. A imagem aparecerá aqui quando a transmissão no notebook for ativada."
        )
        return
    picture, age = frame
    encoded = base64.b64encode(picture).decode("ascii")
    st.markdown(
        '<img alt="Webcam ao vivo" style="width:100%;max-width:640px;border-radius:8px" src="data:image/jpeg;base64,'
        + encoded
        + '">',
        unsafe_allow_html=True,
    )
    st.caption(f"Ao vivo · última imagem recebida há {age:.0f} s")
