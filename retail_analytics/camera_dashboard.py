"""Guided camera enrollment for the authenticated Streamlit tenant."""

import logging
import time

import streamlit as st
from camera_store import build_rtsp_url, delete_camera, list_cameras, save_camera
from cryptography.fernet import InvalidToken
from live_preview import load_frame
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from webcam_setup import build_webcam_kit, list_webcams, save_webcam, validate_webcam

logger = logging.getLogger(__name__)
PREFIX = "camera_wizard_"
DIRECTIONS = {
    "left_to_right": "Da esquerda para a direita",
    "right_to_left": "Da direita para a esquerda",
}


def clear_camera_wizard() -> None:
    """Discard all draft credentials on cancel, navigation or logout."""
    for key in list(st.session_state):
        if key.startswith(PREFIX):
            del st.session_state[key]


def _begin(tenant_id: str, current: dict | None = None) -> None:
    clear_camera_wizard()
    st.session_state[PREFIX + "state"] = {
        "tenant_id": tenant_id,
        "step": 2 if current else 1,
        "kind": current.get("kind", "browser") if current else "browser",
        "camera_id": current.get("camera_id") if current else None,
        "draft": dict(current or {}),
    }


@st.fragment(run_every="5s")
def _connection_status(engine, tenant_id: str, camera_id: str, kind: str) -> None:
    """Show recent observations without equating saved setup with connectivity."""
    if st.session_state.get("tenant_id") != tenant_id:
        return
    try:
        with engine.connect() as conn:
            health = (
                conn.execute(
                    text("""
                SELECT received_at, mqtt_connected, frigate_available
                FROM traffic_counter_health WHERE tenant_id=:tid AND camera_id=:cid
            """),
                    {"tid": tenant_id, "cid": camera_id},
                )
                .mappings()
                .first()
            )
    except SQLAlchemyError:
        st.warning("Não foi possível consultar a conexão agora.")
        return
    if (
        health
        and time.time() - health["received_at"] < 120
        and health["mqtt_connected"]
    ):
        st.success("Contador local conectado ao painel.")
        st.caption(
            "A conexão do contador não confirma, por si só, que há imagem ou detecção."
        )
    else:
        st.info(
            "Aguardando conexão do computador local. O cadastro está salvo, mas o contador está offline."
        )
    if kind == "webcam":
        try:
            frame = load_frame(tenant_id, camera_id)
        except (OSError, RuntimeError):
            frame = None
        if frame:
            st.success("Imagem recente recebida da webcam.")
        else:
            st.caption(
                "Sem imagem ao vivo no momento. A transmissão é opcional e deve ser iniciada no computador da webcam."
            )


def _details(state: dict) -> None:
    draft = state["draft"]
    webcam = state["kind"] == "webcam"
    st.subheader("2. Dados da " + ("webcam" if webcam else "câmera IP"))
    if webcam:
        st.info(
            "Use a webcam embutida ou USB de um computador Linux na loja. Esse computador precisa permanecer ligado para contar e transmitir."
        )
    else:
        st.caption(
            "A câmera Intelbras e o servidor local devem estar na mesma rede da loja."
        )
    with st.form(PREFIX + "details"):
        values = {
            "name": st.text_input(
                "Nome da câmera",
                value=draft.get("name", ""),
                placeholder="Ex.: Entrada da ótica",
                max_chars=100,
                key=PREFIX + "name",
            )
        }
        if webcam:
            direction = draft.get("entry_direction", "left_to_right")
            values["entry_direction"] = st.selectbox(
                "Na imagem, uma entrada acontece…",
                list(DIRECTIONS),
                index=list(DIRECTIONS).index(direction),
                format_func=DIRECTIONS.get,
                key=PREFIX + "direction",
            )
            st.caption(
                "Para o teste, a pessoa deve aparecer de corpo inteiro e atravessar os dois lados da imagem. O caminho inverso contará como saída."
            )
            with st.expander("Opções da webcam"):
                values["device_path"] = st.text_input(
                    "Dispositivo da webcam",
                    value=draft.get("device_path", "/dev/video0"),
                    key=PREFIX + "device",
                )
                values["pixel_format"] = st.selectbox(
                    "Formato de vídeo",
                    ["mjpeg", "yuyv422"],
                    index=["mjpeg", "yuyv422"].index(
                        draft.get("pixel_format", "mjpeg")
                    ),
                    format_func=lambda value: (
                        "MJPEG (padrão)"
                        if value == "mjpeg"
                        else "YUYV (compatibilidade)"
                    ),
                    key=PREFIX + "format",
                )
                st.caption(
                    "Captura em 640 × 480 a 30 fps. Se não abrir, confirme o dispositivo e o formato suportados pela webcam."
                )
        else:
            values.update(
                {
                    "local_ip": st.text_input(
                        "IP Local",
                        value=draft.get("local_ip", ""),
                        placeholder="192.168.1.100",
                        key=PREFIX + "ip",
                    ),
                    "username": st.text_input(
                        "Usuário",
                        value=draft.get("username", "admin"),
                        max_chars=128,
                        key=PREFIX + "user",
                    ),
                    "password": st.text_input(
                        "Senha",
                        value=draft.get("password", ""),
                        type="password",
                        max_chars=1024,
                        help="Ao editar, deixe em branco para manter a senha atual.",
                        key=PREFIX + "password",
                    ),
                    "channel": int(
                        st.number_input(
                            "Canal",
                            min_value=1,
                            max_value=256,
                            value=int(draft.get("channel", 1)),
                            key=PREFIX + "channel",
                        )
                    ),
                    "enabled": st.checkbox(
                        "Câmera ativa",
                        value=bool(draft.get("enabled", True)),
                        key=PREFIX + "enabled",
                    ),
                }
            )
        submitted = st.form_submit_button("Revisar cadastro", type="primary")
    if submitted:
        try:
            if webcam:
                validate_webcam(**values)
            else:
                if not values["name"].strip():
                    raise ValueError("Informe o nome da câmera.")
                build_rtsp_url(
                    values["local_ip"],
                    values["username"],
                    values["password"] or ("unchanged" if state["camera_id"] else ""),
                    values["channel"],
                )
        except ValueError as error:
            st.error(str(error))
        else:
            state["draft"] = values
            state["step"] = 3
            st.rerun()
    if not state["camera_id"] and st.button("Voltar ao tipo de câmera"):
        _begin(state["tenant_id"])
        st.rerun()


def _review(engine, tenant_id: str, state: dict) -> None:
    st.subheader("3. Revise antes de salvar")
    draft = state["draft"]
    webcam = state["kind"] == "webcam"
    st.write("Nome:", draft["name"])
    st.write("Tipo:", "Webcam local" if webcam else "Câmera IP Intelbras")
    if webcam:
        st.write("Dispositivo:", draft["device_path"])
        st.write("Sentido de entrada:", DIRECTIONS[draft["entry_direction"]])
        st.info(
            "Depois de salvar, baixe o pacote e inicie-o no computador da webcam. Salvar não liga a câmera. As contagens serão movimentos reais de teste."
        )
    else:
        st.write("IP Local:", draft["local_ip"])
        st.write("Canal:", draft["channel"])
        st.write("Situação:", "Ativa" if draft["enabled"] else "Desativada")
        st.caption(
            "A senha será armazenada de forma criptografada. O servidor local aplicará a configuração na próxima sincronização."
        )
    if st.button("Confirmar e salvar", type="primary"):
        try:
            if webcam:
                cid = save_webcam(
                    engine, tenant_id, **draft, camera_id=state["camera_id"]
                )
            else:
                cid = save_camera(
                    engine, tenant_id, **draft, camera_id=state["camera_id"]
                )
        except IntegrityError:
            st.error(
                "Já existe uma câmera com esse IP e canal ou com esse dispositivo nesta loja."
            )
        except ValueError as error:
            st.error(str(error))
        except (RuntimeError, InvalidToken, SQLAlchemyError, OSError):
            logger.error("Camera wizard save failed")
            st.error(
                "Não foi possível salvar. Seus dados estão no formulário; tente novamente."
            )
        else:
            kind = state["kind"]
            clear_camera_wizard()
            st.session_state[PREFIX + "state"] = {
                "tenant_id": tenant_id,
                "step": 4,
                "kind": kind,
                "camera_id": cid,
                "draft": {},
            }
            st.rerun()
    if st.button("Voltar aos dados"):
        state["step"] = 2
        st.rerun()


def _finish(engine, tenant_id: str, state: dict, sources: dict) -> None:
    st.subheader("4. Conectar e testar")
    st.success("Cadastro salvo.")
    source = sources.get(state["camera_id"])
    if not source:
        st.warning("Esta câmera não está mais cadastrada.")
        return
    st.write("Câmera:", source["name"])
    if state["kind"] == "webcam":
        st.markdown(
            "1. Baixe e extraia o pacote **no computador Linux da webcam**, com Docker instalado.\n2. Abra um terminal na pasta extraída e execute o comando abaixo.\n3. Informe a chave da loja fornecida na instalação.\n4. Abra **Entradas e Saídas**, selecione a webcam e atravesse a imagem no sentido configurado."
        )
        try:
            kit = build_webcam_kit(source, tenant_id)
        except (OSError, ValueError):
            st.error(
                "Não foi possível preparar o pacote. Entre em contato com o suporte."
            )
        else:
            st.download_button(
                "Baixar configuração da webcam",
                kit,
                file_name="aivo-webcam-" + source["camera_id"][:8] + ".zip",
                mime="application/zip",
            )
        st.code("bash iniciar.sh", language="bash")
        st.caption("Para contagens e imagem ao vivo, use:")
        st.code("bash iniciar.sh --com-imagem", language="bash")
        st.caption(
            "Para desligar tudo: bash parar.sh. A imagem é atualizada uma vez por segundo."
        )
        st.info(
            "Se esta webcam já funciona no notebook, mantenha a instalação atual. Para aplicar alterações de dispositivo ou sentido, pare os serviços e atualize o pacote na mesma pasta, preservando os dados locais."
        )
    else:
        st.info(
            "Mantenha o servidor local ligado e conectado à internet. Ele buscará automaticamente a configuração. Depois, confira a imagem no Frigate local e calibre as zonas da entrada."
        )
    _connection_status(engine, tenant_id, source["camera_id"], state["kind"])


def render_camera_settings(engine, tenant_id: str) -> None:
    """Render a four-step enrollment wizard with tenant-isolated draft state."""
    state = st.session_state.get(PREFIX + "state")
    if not state or state["tenant_id"] != tenant_id:
        _begin(tenant_id)
        state = st.session_state[PREFIX + "state"]
    st.title("Configurar Câmeras")
    st.caption("Adicione uma câmera IP ou use a webcam do computador da loja.")
    try:
        cameras = [{**row, "kind": "ip"} for row in list_cameras(engine, tenant_id)]
        webcams = [{**row, "kind": "webcam"} for row in list_webcams(engine, tenant_id)]
    except SQLAlchemyError:
        st.error("Não foi possível consultar as câmeras. Tente novamente.")
        return
    sources = {row["camera_id"]: row for row in cameras + webcams}
    if sources:
        with st.expander("Câmeras cadastradas", expanded=False):
            selected = st.selectbox(
                "Câmera cadastrada",
                list(sources),
                format_func=lambda cid: (
                    sources[cid]["name"]
                    + (" · Webcam" if sources[cid]["kind"] == "webcam" else " · IP")
                ),
                key=PREFIX + "catalog",
            )
            if st.button("Editar configuração"):
                _begin(tenant_id, sources[selected])
                st.rerun()
            if st.button("Ver conexão e instalação"):
                _begin(tenant_id, sources[selected])
                st.session_state[PREFIX + "state"]["step"] = 4
                st.rerun()
            if sources[selected]["kind"] == "ip":
                with st.form(PREFIX + "delete"):
                    confirmed = st.checkbox("Confirmo a exclusão desta câmera")
                    remove = st.form_submit_button("Excluir câmera")
                if remove:
                    if not confirmed:
                        st.warning("Confirme a exclusão para continuar.")
                    else:
                        try:
                            delete_camera(engine, tenant_id, selected)
                        except SQLAlchemyError:
                            st.error("Não foi possível excluir a câmera.")
                        else:
                            _begin(tenant_id)
                            st.session_state[PREFIX + "notice"] = "Câmera excluída."
                            st.rerun()
    if notice := st.session_state.pop(PREFIX + "notice", None):
        st.success(notice)
    if not (state["kind"] == "browser" and state["step"] == 2):
        from browser_dashboard import close_browser_session

        close_browser_session(engine)
    step = state["step"]
    st.progress(step / 4, text=f"Etapa {step} de 4 · Tipo → Dados → Revisão → Conexão")
    if step == 1:
        st.subheader("1. Qual câmera você quer adicionar?")
        kind = st.radio(
            "Tipo de câmera",
            ["browser", "ip", "webcam"],
            format_func=lambda value: (
                "Webcam pelo navegador (sem instalação)"
                if value == "browser"
                else "Câmera IP Intelbras"
                if value == "ip"
                else "Webcam do computador (embutida ou USB)"
            ),
            key=PREFIX + "kind",
        )
        st.caption(
            "IP: câmera da rede da loja. Webcam: conectada ao computador Linux que fará a análise."
        )
        if st.button("Continuar", type="primary"):
            state["kind"] = kind
            state["step"] = 2
            st.rerun()
    elif step == 2:
        if state["kind"] == "browser":
            from browser_dashboard import render_browser_camera

            render_browser_camera(engine, tenant_id)
        else:
            _details(state)
    elif step == 3:
        _review(engine, tenant_id, state)
    else:
        _finish(engine, tenant_id, state, sources)
    if step > 1 and st.button(
        "Adicionar outra câmera" if step == 4 else "Cancelar cadastro"
    ):
        _begin(tenant_id)
        st.rerun()
