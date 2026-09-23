"""A paginated, tenant-private visitor photo gallery."""

import base64
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from store_experience import customer_cameras, route_button
from traffic_store import list_counting_sources
from visitor_store import list_visitors

ZONE = ZoneInfo("America/Sao_Paulo")


def day_bounds(day):
    """Return the selected local day's half-open timestamp interval."""
    return (
        datetime.combine(day, time.min, ZONE).timestamp(),
        datetime.combine(day + timedelta(days=1), time.min, ZONE).timestamp(),
    )


@st.dialog("Detalhes da visita", width="large")
def visitor_details(engine, tenant_id, visitor):
    """Show observations and request a clip only after an explicit action."""
    if st.session_state.get("tenant_id") != tenant_id:
        return
    cid = visitor["camera_id"]
    with engine.connect() as c:
        rows = [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT observed_at,zones,revision,ended FROM retail_samples WHERE tenant_id=:t AND camera_id=:c AND tracking_id=:track ORDER BY observed_at LIMIT 5001"
                ),
                {"t": tenant_id, "c": cid, "track": visitor["tracking_id"]},
            ).mappings()
        ]
        profiles = {
            r[0]: json.loads(r[1])
            for r in c.execute(
                text(
                    "SELECT revision,settings FROM retail_profiles WHERE tenant_id=:t AND camera_id=:c"
                ),
                {"t": tenant_id, "c": cid},
            )
        }
    a, b = st.columns([1, 2])
    with a:
        if visitor["photo"]:
            from retail_dashboard import private_image

            private_image(bytes(visitor["photo"]), "Foto do visitante")
        else:
            st.info("Foto não recebida")
    with b:
        st.subheader("Visitante " + visitor["visitor_id"][:8])
        st.write(
            "Primeiro registro: "
            + datetime.fromtimestamp(visitor["first_seen"], ZONE).strftime(
                "%d/%m às %H:%M:%S"
            )
        )
        st.write(
            "Último registro: "
            + datetime.fromtimestamp(visitor["last_seen"], ZONE).strftime("%H:%M:%S")
        )
        st.write(f"Entradas: {visitor['entries']} · Saídas: {visitor['exits']}")
    dwell = {}
    journey = []
    previous = None
    for row in rows[:5000]:
        zones = set(json.loads(row["zones"]))
        byid = {
            z["zone_id"]: z["name"]
            for z in profiles.get(row["revision"], {}).get("zones", [])
        }
        label = " + ".join(byid[z] for z in sorted(zones) if z in byid)
        if label and (not journey or journey[-1]["Área"] != label):
            journey.append(
                {
                    "Área": label,
                    "Horário": datetime.fromtimestamp(
                        row["observed_at"], ZONE
                    ).strftime("%H:%M:%S"),
                }
            )
        if (
            previous
            and not previous["ended"]
            and previous["revision"] == row["revision"]
            and 0 < row["observed_at"] - previous["observed_at"] <= 75
        ):
            for z in zones & set(json.loads(previous["zones"])):
                name = byid.get(z, "Área configurada")
                dwell[name] = (
                    dwell.get(name, 0) + row["observed_at"] - previous["observed_at"]
                )
        previous = row
    if dwell:
        st.subheader("Permanência observada por área")
        st.dataframe(
            [
                {"Área": name, "Segundos observados": round(seconds)}
                for name, seconds in dwell.items()
            ],
            hide_index=True,
            width="stretch",
        )
    if journey:
        st.subheader("Percurso observado")
        st.dataframe(journey, hide_index=True, width="stretch")
    if len(rows) > 5000:
        st.caption("Detalhes limitados às primeiras 5 mil observações desta visita.")
    st.caption(
        "Este cadastro representa um acompanhamento pela câmera. Não identifica a mesma pessoa em visitas diferentes."
    )
    owned = any(c["camera_id"] == cid for c in customer_cameras(engine, tenant_id))
    if owned:
        import time as clock

        from camera_media import create_job
        from retail_dashboard import clip_result

        if st.button("Ver trecho da visita", type="primary"):
            try:
                begin = visitor["first_seen"] - 3
                job = create_job(
                    engine,
                    tenant_id,
                    cid,
                    "clip",
                    begin,
                    min(begin + 20, clock.time() - 1),
                )
                st.session_state["visitor_clip"] = {
                    "visitor_id": visitor["visitor_id"],
                    **job,
                }
            except ValueError as e:
                st.warning(str(e))
        job = st.session_state.get("visitor_clip")
        if job and job["visitor_id"] == visitor["visitor_id"]:
            clip_result(engine, tenant_id, job["job_id"])
        st.caption(
            "O vídeo depende da conexão da câmera e da gravação disponível para esse horário."
        )


def render_visitors(engine, tenant_id, development=False):
    """Show saved observations without inferring identities across visits."""
    if st.session_state.get("tenant_id") != tenant_id:
        return
    st.title("Visitantes")
    st.caption(
        "Consulte as visitas registradas e veja o que foi observado pela câmera."
    )
    left, right = st.columns(2)
    day = left.date_input("Data", datetime.now(ZONE).date(), key="visitor_day")
    try:
        sources = (
            list_counting_sources(engine, tenant_id)
            if development
            else customer_cameras(engine, tenant_id)
        )
        names = {c["camera_id"]: c["name"] for c in sources}
        if not names:
            st.info("Conecte uma câmera da loja para começar a registrar visitantes.")
            route_button("Cadastrar câmera", "Câmeras", "Cadastro", type="primary")
            return
        camera = right.selectbox(
            "Câmera",
            ([None, *names] if development else list(names)),
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
                "Nenhum visitante registrado neste período. Confira a conexão e a imagem da câmera; ausência de registros não confirma ausência de pessoas."
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
                st.markdown("**Visitante " + visitor["visitor_id"][:8] + "**")
                st.text(names.get(visitor["camera_id"], "Câmera removida"))
                st.caption(
                    datetime.fromtimestamp(visitor["first_seen"], ZONE).strftime(
                        "%d/%m/%Y · %H:%M:%S"
                    )
                )
                st.write(f"Entradas: {visitor['entries']} · Saídas: {visitor['exits']}")
                if st.button(
                    "Ver detalhes",
                    key="visitor_" + visitor["visitor_id"],
                    use_container_width=True,
                ):
                    visitor_details(engine, tenant_id, visitor)
                with st.expander("Identificador para integração"):
                    st.code(visitor["visitor_id"], language=None)
        if st.button("Atualizar visitantes"):
            st.rerun()
    except SQLAlchemyError:
        st.error(
            "Não foi possível carregar os visitantes. Tente novamente em instantes."
        )
