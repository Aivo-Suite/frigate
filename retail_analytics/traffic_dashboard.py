"""An evidence-based store overview with actionable installation guidance."""

import base64
import logging
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from live_preview import load_frame
from retail_store import retail_report
from store_experience import (
    camera_readiness,
    customer_cameras,
    render_setup,
    route_button,
)
from traffic_store import daily_report, list_counting_sources
from visitor_dashboard import ZONE, day_bounds
from visitor_store import list_visitors

logger = logging.getLogger(__name__)


def metric_values(report, visitors, current):
    """Do not present missing telemetry as a measured zero."""
    evidence = report["entries"] + report["exits"] + visitors["total"] > 0
    show = evidence or current
    return [
        report["entries"] if show else "—",
        report["exits"] if show else "—",
        visitors["total"] if show else "—",
    ]


@st.fragment(run_every="15s")
def render_traffic_counter(engine, tenant_id: str, development=False) -> None:
    if st.session_state.get("tenant_id") != tenant_id:
        return
    st.title("Contagens de teste" if development else "Resumo")
    st.caption("Movimento, visitantes e os próximos passos da sua loja.")
    cameras = (
        list_counting_sources(engine, tenant_id)
        if development
        else customer_cameras(engine, tenant_id)
    )
    if not cameras:
        if development:
            st.info("Nenhuma fonte de teste cadastrada.")
        else:
            render_setup()
            st.subheader("Seu movimento aparecerá aqui")
            st.caption(
                "Após conectar a Intelbras e marcar a entrada, você verá as contagens, os horários de maior movimento e os visitantes registrados."
            )
            for column, label in zip(
                st.columns(3), ["Entradas", "Saídas", "Visitantes registrados"]
            ):
                column.metric(label, "—")
            st.info(
                "Sem dados da loja. Nenhuma câmera Intelbras ativa está cadastrada."
            )
        return
    names = {c["camera_id"]: c["name"] for c in cameras}
    left, right = st.columns([2, 1])
    cid = left.selectbox(
        "Câmera",
        list(names),
        format_func=names.get,
        key="summary_camera_dev" if development else "summary_camera",
    )
    today = datetime.now(ZONE).date()
    day = right.date_input(
        "Período",
        today,
        max_value=today,
        key="summary_date_dev" if development else "summary_date",
    )
    camera = next(c for c in cameras if c["camera_id"] == cid)
    is_test = camera.get("is_test", False)
    ready = None if is_test else camera_readiness(engine, tenant_id, cid)
    if is_test:
        st.warning(
            "Webcam de teste: estes movimentos não representam o fluxo de clientes da loja."
        )
    elif not ready["validated"]:
        render_setup(camera, ready)
    if ready:
        if ready["current"]:
            st.success("Online · " + camera["name"] + " · Contagem conectada")
        else:
            st.warning(ready["status"] + " · " + ready["action"])
        if ready["last_seen"]:
            st.caption(
                "Última comunicação: "
                + datetime.fromtimestamp(ready["last_seen"], ZONE).strftime(
                    "%d/%m às %H:%M:%S"
                )
            )
    report = daily_report(engine, tenant_id, day, camera_id=cid)
    start, end = day_bounds(day)
    visitors = list_visitors(engine, tenant_id, start, end, cid, limit=1)
    current = bool(ready and ready["current"] and day == today)
    values = metric_values(report, visitors, current)
    evidence = report["entries"] + report["exits"] + visitors["total"] > 0
    if not current:
        st.info(
            "Registros recebidos · Os totais podem estar incompletos em períodos sem conexão."
            if evidence
            else "Sem dados atualizados para este período. Isso não significa que a loja ficou sem movimento."
        )
    prior = (
        daily_report(engine, tenant_id, day - timedelta(days=1), camera_id=cid)
        if day < today and evidence
        else None
    )
    columns = st.columns(4)
    for column, label, value, field in zip(
        columns[:3],
        ["Entradas", "Saídas", "Visitantes registrados"],
        values,
        ["entries", "exits", None],
    ):
        delta = None
        if prior and field and report[field] > 0 and prior[field] > 0:
            delta = f"{(report[field] / prior[field] - 1) * 100:+.0f}% de registros vs. dia anterior"
        column.metric(label, value, delta=delta, delta_color="off")
    peak = max(report["hourly"], key=lambda h: h["Entradas"])
    columns[3].metric(
        "Maior movimento de entrada", peak["Hora"] if report["entries"] else "—"
    )
    st.caption(
        "Entradas e saídas são passagens observadas. Visitantes são acompanhamentos registrados, não pessoas únicas. Comparações consideram registros recebidos e não comprovam cobertura contínua."
    )
    st.subheader("Movimento ao longo do dia")
    if evidence or current:
        hourly = pd.DataFrame(report["hourly"])
        st.bar_chart(
            hourly, x="Hora", y=["Entradas", "Saídas"], color=["#175cd3", "#94a3b8"]
        )
        with st.expander("Consultar e exportar números"):
            st.dataframe(hourly, hide_index=True, width="stretch")
            st.download_button(
                "Baixar CSV",
                hourly.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"contagem-{day.isoformat()}.csv",
                mime="text/csv",
            )
    else:
        st.caption("O gráfico será preenchido com as passagens recebidas da câmera.")
    if ready and ready["configured"]:
        behavior = retail_report(engine, tenant_id, cid, start, end)
        sectors = [z for z in behavior["zones"] if z["seconds"] > 0]
        st.subheader("Permanência por área")
        if sectors:
            st.dataframe(
                [
                    {
                        "Área": z["name"],
                        "Minutos observados": round(z["seconds"] / 60, 1),
                        "Acompanhamentos": z["tracks"],
                    }
                    for z in sectors
                ],
                hide_index=True,
                width="stretch",
            )
            if behavior["limited"]:
                st.caption(
                    "Visualização limitada às primeiras 50 mil observações do período."
                )
        else:
            st.caption(
                "A permanência aparecerá quando pessoas forem acompanhadas nas áreas marcadas."
            )
        route_button("Explorar análise da loja", "Análise da loja", camera_id=cid)
    if st.button("Atualizar resumo"):
        st.rerun()


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
