"""Production camera, zone, media and retail analytics pages for store owners."""

import base64
import io
import time
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from camera_media import create_job, finish_job, job_result
from camera_store import list_cameras
from live_preview import load_frame
from retail_store import get_profile, retail_report, save_profile
from sqlalchemy import text
from visitor_dashboard import ZONE, day_bounds

zones_component = components.declare_component(
    "aivo_zones", path=str(Path(__file__).with_name("zone_component"))
)
media_component = components.declare_component(
    "aivo_media", path=str(Path(__file__).with_name("media_component"))
)


def camera_picker(engine, tid, key):
    cameras = {c["camera_id"]: c for c in list_cameras(engine, tid) if c["enabled"]}
    if not cameras:
        st.info(
            "Cadastre a Intelbras em Configurar Câmeras. Depois conecte o Edge da loja para receber imagem e dados reais."
        )
        return None
    return cameras[
        st.selectbox(
            "Câmera", list(cameras), format_func=lambda c: cameras[c]["name"], key=key
        )
    ]


def private_image(body, alt="Imagem da câmera"):
    encoded = base64.b64encode(body).decode("ascii")
    st.markdown(
        '<img alt="'
        + alt
        + '" style="width:100%;max-width:900px;border-radius:12px" src="data:image/jpeg;base64,'
        + encoded
        + '">',
        unsafe_allow_html=True,
    )


@st.fragment(run_every="5s")
def render_cameras(engine, tid):
    if st.session_state.get("tenant_id") != tid:
        return
    st.title("Câmeras da loja")
    st.caption("Intelbras conectada pelo Edge. A câmera permanece na rede da loja.")
    camera = camera_picker(engine, tid, "live_camera")
    if not camera:
        return
    cid = camera["camera_id"]
    profile = get_profile(engine, tid, cid)
    with engine.connect() as c:
        health = (
            c.execute(
                text(
                    "SELECT * FROM retail_edge_health WHERE tenant_id=:t AND camera_id=:c"
                ),
                {"t": tid, "c": cid},
            )
            .mappings()
            .first()
        )
    if not health or time.time() - health["received_at"] > 30:
        st.warning(
            "Edge sem comunicação recente. A imagem e os totais podem estar desatualizados."
        )
    elif health["camera_fps"] <= 0:
        st.error(
            "Edge conectado, mas a câmera não está entregando quadros. Confira IP, usuário, senha e canal."
        )
    else:
        st.success("Câmera online")
        st.caption(
            f"{health['camera_fps']:.1f} quadros/s recebidos · {health['queue_size']} itens na fila do Edge"
        )
        if health["revision"] != profile["revision"]:
            st.info("Configuração salva. Aguardando aplicação no Edge.")
    frame = load_frame(tid, cid)
    if frame:
        with st.expander("Imagem recente da câmera", expanded=True):
            private_image(frame[0])
    else:
        st.info(
            "A prévia aparecerá quando o Edge conectar a câmera. Nenhuma imagem de demonstração é utilizada."
        )
    if st.button("Preparar vídeo ao vivo", type="primary"):
        try:
            old = st.session_state.pop("retail_live_job", None)
            if old:
                finish_job(engine, tid, old["job_id"])
            st.session_state["retail_live_job"] = {
                **create_job(engine, tid, cid, "live"),
                "camera_id": cid,
            }
        except ValueError as e:
            st.warning(str(e))
    job = st.session_state.get("retail_live_job")
    if job and job["camera_id"] == cid:
        media_component(
            token=job["token"], job_id=job["job_id"], key="live_" + job["job_id"]
        )
    st.caption(
        "Vídeo sob demanda, sem áudio, com sessão de até cinco minutos. Fechar ou deixar a página interrompe a transmissão."
    )
    st.divider()
    from intelbras_kit import build_edge_kit

    st.subheader("Instalação do Edge")
    st.caption(
        "Kit para o responsável técnico instalar uma vez no computador da loja. O cliente configura as câmeras e zonas neste painel."
    )
    st.download_button(
        "Baixar kit Intelbras",
        build_edge_kit(),
        file_name="aivo-intelbras-edge.zip",
        mime="application/zip",
    )


def render_zones(engine, tid):
    if st.session_state.get("tenant_id") != tid:
        return
    st.title("Zonas e gravação")
    camera = camera_picker(engine, tid, "zone_camera")
    if not camera:
        return
    cid = camera["camera_id"]
    current = get_profile(engine, tid, cid)
    settings = current["settings"]
    st.caption(
        "Marque áreas visíveis na imagem. Use uma zona externa e uma interna, sem sobreposição, para contar as passagens pela porta."
    )
    if settings["zones"]:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Área": z["name"],
                        "Função": {
                            "outside": "Externa",
                            "inside": "Interna",
                            "area": "Setor",
                        }[z["role"]],
                        "Alerta após (s)": z["alert_after"],
                    }
                    for z in settings["zones"]
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    options = {z["zone_id"]: z for z in settings["zones"]}
    selected = st.selectbox(
        "Editar área",
        [None, *options],
        format_func=lambda z: options[z]["name"] if z else "Criar nova área",
    )
    existing = options.get(
        selected, {"name": "", "role": "area", "points": [], "alert_after": 0}
    )
    name = st.text_input(
        "Nome da área",
        value=existing["name"],
        max_chars=60,
        key="zone_name_" + str(selected) + cid,
    )
    role = st.selectbox(
        "Função",
        ["area", "outside", "inside"],
        index=["area", "outside", "inside"].index(existing["role"]),
        format_func=lambda r: {
            "area": "Setor da loja",
            "outside": "Lado externo da entrada",
            "inside": "Lado interno da entrada",
        }[r],
        key="zone_role_" + str(selected) + cid,
    )
    alert = int(
        st.number_input(
            "Alertar após permanência contínua (segundos; 0 desativa)",
            min_value=0,
            max_value=7200,
            value=existing["alert_after"] if role == "area" else 0,
            disabled=role != "area",
            key="zone_alert_" + str(selected) + cid + role,
        )
    )
    frame = load_frame(tid, cid)
    points = existing["points"]
    if frame:
        editor_key = cid + ":" + str(selected) + ":" + current["revision"]
        result = zones_component(
            editor_key=editor_key,
            image="data:image/jpeg;base64," + base64.b64encode(frame[0]).decode(),
            points=points,
            key="zones_" + cid + str(selected),
            default=None,
        )
        if result and result.get("editor_key") == editor_key:
            points = result["points"]
        if st.button("Salvar área", type="primary"):
            zone = {
                "zone_id": selected or "aivo_" + uuid.uuid4().hex[:12],
                "name": name,
                "role": role,
                "points": points,
                "alert_after": alert if role == "area" else 0,
            }
            proposed = {
                **settings,
                "zones": [z for z in settings["zones"] if z["zone_id"] != selected]
                + [zone],
            }
            try:
                save_profile(engine, tid, cid, proposed, current["revision"])
                st.rerun()
            except ValueError:
                st.error(
                    "Não foi possível salvar. Confira os pontos, o nome e se já existe uma zona com essa função. Reabra a página se a configuração mudou."
                )
    else:
        st.info("Conecte a câmera para desenhar as zonas sobre uma imagem real.")
    if selected and st.button("Remover esta área"):
        save_profile(
            engine,
            tid,
            cid,
            {
                **settings,
                "zones": [z for z in settings["zones"] if z["zone_id"] != selected],
            },
            current["revision"],
        )
        st.rerun()
    st.divider()
    with st.form("recording_" + cid):
        recording = st.checkbox(
            "Gravar vídeo no Edge para consultar o histórico",
            value=settings["recording"],
        )
        days = st.number_input(
            "Manter gravações no Edge por quantos dias?",
            min_value=1,
            max_value=7,
            value=settings["retention_days"],
        )
        if st.form_submit_button("Salvar gravação"):
            save_profile(
                engine,
                tid,
                cid,
                {**settings, "recording": recording, "retention_days": int(days)},
                current["revision"],
            )
            st.rerun()
    st.caption(
        "Mudanças são aplicadas pelo Edge e podem interromper brevemente esta câmera. Reduzir a retenção permite que o Frigate remova gravações antigas."
    )


@st.fragment(run_every="15s")
def render_behavior(engine, tid):
    if st.session_state.get("tenant_id") != tid:
        return
    st.title("Comportamento e zonas quentes")
    camera = camera_picker(engine, tid, "behavior_camera")
    if not camera:
        return
    day = st.date_input("Data", datetime.now(ZONE).date(), key="behavior_date")
    start, end = day_bounds(day)
    report = retail_report(engine, tid, camera["camera_id"], start, end)
    st.caption(
        "Permanência e percurso observados na área visível. Não indicam intenção de compra e não identificam a mesma pessoa em visitas diferentes."
    )
    if report["limited"]:
        st.warning(
            "Período com muitos registros. Esta visualização mostra uma amostra limitada a 50 mil observações."
        )
    if report["zones"]:
        table = pd.DataFrame(report["zones"])
        table["Minutos observados"] = (table["seconds"] / 60).round(1)
        st.dataframe(
            table.rename(columns={"name": "Área", "tracks": "Rastreamentos"})[
                ["Área", "Rastreamentos", "Minutos observados"]
            ],
            hide_index=True,
            use_container_width=True,
        )
        st.bar_chart(table.set_index("name")["Minutos observados"])
    else:
        st.info(
            "As métricas aparecerão após desenhar zonas e receber observações da Intelbras."
        )
    if report["heat"]:
        st.subheader("Zonas quentes por tempo observado")
        heat = (
            pd.DataFrame(report["heat"])
            .pivot(index="y", columns="x", values="seconds")
            .reindex(index=range(24), columns=range(36), fill_value=0)
            .fillna(0)
        )
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.imshow(
            heat.to_numpy(),
            cmap="YlOrRd",
            origin="upper",
            extent=(0, 1, 1, 0),
            aspect="auto",
        )
        ax.set_xlabel("Largura da imagem")
        ax.set_ylabel("Altura da imagem")
        output = io.BytesIO()
        fig.savefig(output, format="jpeg", bbox_inches="tight", dpi=120)
        private_image(output.getvalue(), "Mapa de permanência")
        plt.close(fig)
        st.caption(
            "Vermelho indica mais segundos observados. Intervalos sem continuidade maior que 75 segundos são desconsiderados; não há estimativa fora do campo da câmera."
        )
    st.subheader("Percursos observados")
    for tracking, steps in list(report["journeys"].items())[:30]:
        with st.expander("Visitante " + tracking):
            st.dataframe(
                [
                    {
                        "Área": s["zone"],
                        "Horário": datetime.fromtimestamp(s["at"], ZONE).strftime(
                            "%H:%M:%S"
                        ),
                    }
                    for s in steps
                ],
                hide_index=True,
            )
    st.subheader("Alertas de permanência")
    if not report["alerts"]:
        st.info("Nenhum alerta de permanência para este período.")
    for alert in report["alerts"]:
        st.write(
            f"Área {alert['zone_id']} · {alert['seconds']:.0f} segundos observados · {datetime.fromtimestamp(alert['occurred_at'], ZONE):%H:%M:%S}"
        )
    st.caption(
        "Alertas são exibidos aqui a partir das observações recebidas. Não enviamos mensagens externas automaticamente."
    )
    if st.button("Atualizar análise"):
        st.rerun()


@st.fragment(run_every="5s")
def clip_result(engine, tid, jid):
    if st.session_state.get("tenant_id") != tid:
        return
    job = job_result(engine, tid, jid)
    if not job:
        st.info("Solicitação expirada. Gere outro trecho.")
        return
    if job["status"] == "ready":
        encoded = base64.b64encode(bytes(job["data"])).decode()
        st.markdown(
            '<video controls playsinline style="width:100%;max-height:480px" src="data:video/mp4;base64,'
            + encoded
            + '"></video>',
            unsafe_allow_html=True,
        )
        st.caption(
            "Trecho disponível temporariamente nesta sessão. A gravação original segue a retenção do Edge."
        )
    elif job["status"] in ("failed", "cancelled", "expired"):
        st.warning(
            "Trecho indisponível. Confira a conexão do Edge e a retenção da câmera."
        )
    else:
        st.info("Solicitação enviada. Aguardando o Edge recuperar a gravação…")


def render_history(engine, tid):
    if st.session_state.get("tenant_id") != tid:
        return
    st.title("Histórico da câmera")
    camera = camera_picker(engine, tid, "history_camera")
    if not camera:
        return
    cid = camera["camera_id"]
    day = st.date_input("Data", datetime.now(ZONE).date(), key="history_date")
    start, end = day_bounds(day)
    with engine.connect() as c:
        rows = [
            dict(r)
            for r in c.execute(
                text("""SELECT tracking_id,MIN(observed_at) AS first_seen,MAX(observed_at) AS last_seen
            FROM retail_samples WHERE tenant_id=:t AND camera_id=:c AND observed_at>=:s AND observed_at<:e
            GROUP BY tracking_id ORDER BY first_seen DESC LIMIT 100"""),
                {"t": tid, "c": cid, "s": start, "e": end},
            ).mappings()
        ]
    if rows:
        choice = st.selectbox(
            "Ocorrência",
            range(len(rows)),
            format_func=lambda i: (
                datetime.fromtimestamp(rows[i]["first_seen"], ZONE).strftime("%H:%M:%S")
                + " · "
                + rows[i]["tracking_id"]
            ),
        )
        selected = rows[choice]
        st.caption(
            "O registro representa uma pessoa acompanhada pela câmera, não uma identificação facial."
        )
        if st.button("Ver trecho desta ocorrência", type="primary"):
            try:
                begin = selected["first_seen"] - 3
                finish = min(begin + 20, time.time() - 1)
                job = create_job(engine, tid, cid, "clip", begin, finish)
                st.session_state["retail_clip_job"] = {"camera_id": cid, **job}
            except ValueError as e:
                st.warning(str(e))
    else:
        st.info("Nenhuma ocorrência recebida nesta data.")
    with st.expander("Consultar um horário específico"):
        moment = st.time_input(
            "Horário de Brasília", datetime.now(ZONE).time().replace(microsecond=0)
        )
        if st.button("Buscar 20 segundos de vídeo"):
            try:
                begin = datetime.combine(day, moment, ZONE).timestamp()
                job = create_job(engine, tid, cid, "clip", begin, begin + 20)
                st.session_state["retail_clip_job"] = {"camera_id": cid, **job}
            except ValueError as e:
                st.warning(str(e))
    job = st.session_state.get("retail_clip_job")
    if job and job["camera_id"] == cid:
        clip_result(engine, tid, job["job_id"])
