"""Cliente mínimo de la API de Telegram: enviar mensajes, botones y leer updates.

En GitHub Actions no hay servidor prendido, así que en cada corrida usamos
getUpdates con un offset guardado en la Sheet (config 'telegram_offset') para
procesar los comandos nuevos (ej: /cuotas <id> <n>) y los toques de botones
(callback_query, ej: elegir categoría o número de cuotas)."""

import requests

from . import config

API = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"


def enviar(texto: str, teclado: dict | None = None, parse_mode: str = "HTML") -> None:
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if teclado is not None:
        payload["reply_markup"] = teclado
    requests.post(f"{API}/sendMessage", json=payload, timeout=30)


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
