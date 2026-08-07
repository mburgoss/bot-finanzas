"""Cliente mínimo de la API de Telegram: enviar mensajes, botones y leer updates.

En GitHub Actions no hay servidor prendido, así que en cada corrida usamos
getUpdates con un offset guardado en la Sheet (config 'telegram_offset') para
procesar los comandos nuevos (ej: /cuotas <id> <n>) y los toques de botones
(callback_query, ej: elegir categoría o número de cuotas)."""

import json

import requests

from . import config

API = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"


def enviar(texto: str, teclado: dict | None = None, parse_mode: str = "HTML") -> bool:
    """Manda un mensaje. Devuelve False si Telegram lo rechazó.

    El resultado se revisa a propósito: antes se ignoraba, así que un mensaje
    rechazado (por ejemplo, un nombre de comercio con '<' que rompe el parseo
    HTML) desaparecía sin dejar rastro, con el movimiento ya guardado en la
    planilla y marcado como visto. El aviso queda en el log de Actions."""
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if teclado is not None:
        payload["reply_markup"] = teclado
    resp = requests.post(f"{API}/sendMessage", json=payload, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        print(f"[aviso] Telegram respondió algo ilegible (HTTP {resp.status_code})")
        return False
    if not data.get("ok"):
        # description trae el motivo de Telegram, no datos del movimiento.
        print(f"[aviso] Telegram rechazó el mensaje: {data.get('description')!r}")
        return False
    return True


def enviar_foto(imagen: bytes, caption: str = "", parse_mode: str = "HTML") -> dict | None:
    """Manda un PNG (sendPhoto). Devuelve el mensaje creado, o None si Telegram
    lo rechazó, para que quien llama pueda caer al texto en vez de quedarse sin
    resumen. El dict trae 'message_id', necesario para editarlo después.

    El caption admite 1024 caracteres: recortar es tarea de quien llama, acá solo
    se trunca como red de seguridad."""
    resp = requests.post(
        f"{API}/sendPhoto",
        data={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "caption": caption[:1024],
            "parse_mode": parse_mode,
        },
        files={"photo": ("resumen.png", imagen, "image/png")},
        timeout=60,
    )
    try:
        data = resp.json()
    except ValueError:
        print(f"[aviso] Telegram respondió algo ilegible (HTTP {resp.status_code})")
        return None
    if not data.get("ok"):
        print(f"[aviso] Telegram rechazó la foto: {data.get('description')!r}")
        return None
    return data.get("result")


# Motivos por los que el mensaje realmente dejó de existir y corresponde crear
# otro. Cualquier OTRO error (429, 5xx, red) NO lo justifica: tratarlos a todos
# igual es lo que terminó dejando dos fotos fijadas en el chat.
_YA_NO_EXISTE = ("message to edit not found", "message can't be edited",
                 "message identifier is not specified", "message_id_invalid")


def editar_foto(message_id: int, imagen: bytes, caption: str = "",
                parse_mode: str = "HTML") -> str:
    """Reemplaza la imagen de un mensaje ya enviado (editMessageMedia).

    Es lo que sostiene el panel fijado: siempre el MISMO mensaje, con la imagen
    al día. Editar no genera notificación, así que el panel se puede refrescar
    seguido sin molestar.

    Devuelve "ok", "no_existe" (el mensaje se borró: hay que crear otro) o
    "error" (falla pasajera: NO hay que crear otro, se reintenta después)."""
    media = {
        "type": "photo",
        "media": "attach://foto",
        "caption": caption[:1024],
        "parse_mode": parse_mode,
    }
    try:
        resp = requests.post(
            f"{API}/editMessageMedia",
            data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "message_id": message_id,
                "media": json.dumps(media),
            },
            files={"foto": ("panel.png", imagen, "image/png")},
            timeout=60,
        )
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"[aviso] no pude editar el panel ({type(e).__name__})")
        return "error"
    if data.get("ok"):
        return "ok"
    motivo = str(data.get("description") or "").lower()
    print(f"[aviso] Telegram rechazó la edición del panel: {motivo!r}")
    return "no_existe" if any(m in motivo for m in _YA_NO_EXISTE) else "error"


def fijar(message_id: int) -> bool:
    """Fija el mensaje arriba del chat (pinChatMessage), sin notificar."""
    resp = requests.post(
        f"{API}/pinChatMessage",
        json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "disable_notification": True,
        },
        timeout=30,
    )
    try:
        return bool(resp.json().get("ok"))
    except ValueError:
        return False


def desfijar(message_id: int) -> None:
    """Saca de fijados un panel viejo antes de fijar el nuevo, para que no se
    acumulen fotos fijadas. Si falla, no importa: es limpieza, no dato."""
    try:
        requests.post(
            f"{API}/unpinChatMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "message_id": message_id},
            timeout=30,
        )
    except requests.exceptions.RequestException:
        pass


def editar(chat_id, message_id, texto: str, teclado: dict | None = None,
           parse_mode: str = "HTML") -> None:
    """Edita un mensaje ya enviado (para reflejar la cuota/categoría elegida)."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if teclado is not None:
        payload["reply_markup"] = teclado
    requests.post(f"{API}/editMessageText", json=payload, timeout=30)


def responder_callback(callback_id: str, texto: str = "") -> None:
    """Cierra el 'cargando…' del botón (answerCallbackQuery)."""
    requests.post(
        f"{API}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": texto},
        timeout=30,
    )


def obtener_updates(offset: int) -> list[dict]:
    """Devuelve los updates nuevos desde `offset` (mensajes y toques de botones)."""
    resp = requests.get(
        f"{API}/getUpdates",
        params={"offset": offset + 1, "timeout": 0},
        timeout=30,
    )
    data = resp.json()
    return data.get("result", []) if data.get("ok") else []
