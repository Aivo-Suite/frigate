"""Read-only setup guidance and navigation for the customer dashboard."""

import time
from html import escape

import streamlit as st
from camera_store import list_cameras
from retail_store import get_profile
from sqlalchemy import text


def customer_cameras(engine, tid):
    """Only active store cameras belong in the default customer experience."""
    return [camera for camera in list_cameras(engine, tid) if camera["enabled"]]


def camera_readiness(engine, tid, cid, now=None):
    """Derive setup from tenant-owned evidence, never from an empty event count."""
    now = time.time() if now is None else now
    profile = get_profile(engine, tid, cid)
    with engine.connect() as c:
        params = {"t": tid, "c": cid, "r": profile["revision"]}
        edge = (
            c.execute(
                text(
                    "SELECT * FROM retail_edge_health WHERE tenant_id=:t AND camera_id=:c"
                ),
                params,
            )
            .mappings()
            .first()
        )
        counter = (
            c.execute(
                text(
                    "SELECT * FROM traffic_counter_health WHERE tenant_id=:t AND camera_id=:c"
                ),
                params,
            )
            .mappings()
            .first()
        )
        directions = set(
            c.execute(
                text(
                    "SELECT DISTINCT direction FROM traffic_crossings WHERE tenant_id=:t AND camera_id=:c AND gate_revision=:r"
                ),
                params,
            ).scalars()
        )
    recent = bool(edge and 0 <= now - edge["received_at"] < 30)
    image = bool(recent and edge["camera_fps"] > 0)
    roles = {zone["role"] for zone in profile["settings"]["zones"]}
    configured = {"outside", "inside"} <= roles
    applied = bool(edge and edge["revision"] == profile["revision"])
    counting = bool(
        image
        and configured
        and applied
        and counter
        and 0 <= now - counter["received_at"] < 120
        and counter["mqtt_connected"]
        and counter["frigate_available"]
        and counter["gate_revision"] == profile["revision"]
    )
    pending = bool(edge and edge["queue_size"] > 0) or bool(
        counter and counter["pending_events"] > 0
    )
    if not recent:
        status, action = (
            "Equipamento desconectado",
            "Confira se o equipamento da loja está ligado e com internet.",
        )
    elif not image:
        status, action = (
            "Sem imagem",
            "Confira a alimentação da câmera, o cabo de rede e os dados do cadastro.",
        )
    elif not configured:
        status, action = (
            "Marque a entrada",
            "Desenhe os lados externo e interno da porta para ativar a contagem.",
        )
    elif not applied:
        status, action = (
            "Aplicando configuração",
            "A imagem chegou. Aguarde a aplicação das novas zonas no equipamento.",
        )
    elif not counting:
        status, action = (
            "Análise interrompida",
            "Há imagem, mas a contagem ainda não está ativa. Solicite a verificação do equipamento.",
        )
    elif pending:
        status, action = (
            "Sincronizando dados",
            "A câmera está online. Há registros aguardando envio; os totais ainda podem mudar.",
        )
    else:
        status, action = "Online", "Imagem e contador conectados."
    return {
        "status": status,
        "action": action,
        "image": image,
        "configured": configured,
        "counting": counting,
        "current": counting and not pending,
        "validated": directions == {"entry", "exit"},
        "last_seen": edge["received_at"] if edge else None,
        "profile": profile,
    }


def navigate(page, section=None, camera_id=None):
    """Apply a requested route before the navigation widgets are constructed."""
    st.session_state["nav_request"] = {
        "page": page,
        "section": section,
        "camera_id": camera_id,
    }
    st.rerun(scope="app")


def route_button(label, page, section=None, camera_id=None, **kwargs):
    if st.button(label, **kwargs):
        navigate(page, section, camera_id)


def render_setup(camera=None, readiness=None):
    """Explain the next actionable installation step without technical jargon."""
    ready = readiness or {}
    done = [
        bool(camera),
        ready.get("image", False),
        ready.get("configured", False),
        ready.get("validated", False),
    ]
    labels = [
        "Cadastrar câmera",
        "Conectar equipamento",
        "Marcar entrada",
        "Validar contagem",
    ]
    notes = [
        "IP e acesso da Intelbras",
        "Receber a imagem da loja",
        "Lados de fora e de dentro",
        "Uma entrada e uma saída",
    ]
    first = next((i for i, value in enumerate(done) if not value), 4)
    with st.container(border=True):
        st.subheader("Prepare sua loja para começar")
        st.caption("Uma instalação inicial. Depois, acompanhe tudo por este painel.")
        for i, column in enumerate(st.columns(4)):
            with column:
                color = "#087f5b" if done[i] else "#175cd3" if i == first else "#64748b"
                label = (
                    "Concluído"
                    if done[i]
                    else "Próximo passo"
                    if i == first
                    else "Pendente"
                )
                st.markdown(
                    f'<div class="setup-step"><span style="color:{color}">{"✓" if done[i] else i + 1} · {label}</span><strong>{escape(labels[i])}</strong><small>{escape(notes[i])}</small></div>',
                    unsafe_allow_html=True,
                )
        cid = camera["camera_id"] if camera else None
        if first == 0:
            route_button(
                "Cadastrar minha Intelbras", "Câmeras", "Cadastro", type="primary"
            )
        elif first == 1:
            st.write(
                "O responsável técnico conecta o equipamento de análise à rede da loja. Não é necessário abrir portas no roteador."
            )
            route_button(
                "Conectar e conferir imagem", "Câmeras", "Ao vivo", cid, type="primary"
            )
        elif first == 2:
            route_button(
                "Marcar os lados da entrada",
                "Câmeras",
                "Zonas e gravação",
                cid,
                type="primary",
            )
        elif first == 3:
            st.write(
                "Faça uma entrada e uma saída pela porta. Confira os números no Resumo e compare com o movimento que você realizou."
            )
            route_button(
                "Conferir imagem e configuração",
                "Câmeras",
                "Ao vivo",
                cid,
                type="primary",
            )
        else:
            st.success(
                "Recebemos passagens nos dois sentidos. Confira a precisão com uma contagem manual na loja."
            )
