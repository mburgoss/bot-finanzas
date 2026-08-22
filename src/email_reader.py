"""Lectura de correos por IMAP. Trae correos recientes de los remitentes del banco."""

import email
import imaplib
from datetime import datetime, timedelta
from email.header import decode_header

from . import config


def _decode(valor) -> str:
    if valor is None:
        return ""
    partes = decode_header(valor)
    salida = ""
    for texto, enc in partes:
        if isinstance(texto, bytes):
            salida += texto.decode(enc or "utf-8", errors="replace")
        else:
            salida += texto
    return salida


def _cuerpo(msg) -> str:
    """Extrae el cuerpo del correo, preferentemente texto plano; si no, el HTML."""
    if msg.is_multipart():
        html = ""
        for parte in msg.walk():
            ctype = parte.get_content_type()
            disp = str(parte.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = parte.get_payload(decode=True)
                if payload is None:
                    continue
                charset = parte.get_content_charset() or "utf-8"
                contenido = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain":
                return contenido
            if ctype == "text/html":
                html = contenido
        return html
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def _adjuntos(msg) -> list:
    """[(nombre, bytes)] de los adjuntos del correo.

    Sale gratis: el fetch ya baja el mensaje completo, así que hasta ahora los
    adjuntos se descargaban y se tiraban. El estado de cuenta viene justamente
    como adjunto."""
    if not msg.is_multipart():
        return []
    salida = []
    for parte in msg.walk():
        disp = str(parte.get("Content-Disposition") or "")
        nombre = _decode(parte.get_filename())
        if "attachment" not in disp and not nombre:
            continue
        try:
            datos = parte.get_payload(decode=True)
        except Exception:
            continue
        if datos:
            salida.append((nombre, datos))
    return salida


def obtener_correos():
    """Generador de (uid, asunto, remitente, cuerpo, adjuntos) de correos
    recientes del banco. `adjuntos` es [(nombre, bytes)]."""
    imap = imaplib.IMAP4_SSL(config.IMAP_HOST)
    imap.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
    imap.select("INBOX")

    desde = (datetime.now() - timedelta(days=config.LOOKBACK_DAYS)).strftime("%d-%b-%Y")

    uids = set()
    if config.BANK_SENDERS:
        # 1) Correos de los remitentes conocidos.
        for remitente in config.BANK_SENDERS:
            estado, datos = imap.search(None, f'(SINCE {desde} FROM "{remitente}")')
            if estado == "OK" and datos[0]:
                uids.update(datos[0].split())
        # 2) Además, por asunto. Dos motivos distintos:
        #    - "transferencia/transferido/abono": ingresos de bancos que no están
        #      en la lista de remitentes;
        #    - "estado de cuenta/cartola": el PDF del que sale el corte real del
        #      ciclo. El banco lo manda desde una casilla distinta a la de los
        #      avisos de compra, así que buscarlo solo por remitente no alcanza.
        for palabra in ("transferencia", "transferido", "abono",
                        "estado de cuenta", "cartola"):
            estado, datos = imap.search(None, f'(SINCE {desde} SUBJECT "{palabra}")')
            if estado == "OK" and datos[0]:
                uids.update(datos[0].split())
    else:
        # Sin remitentes configurados: trae todo lo reciente (para pruebas).
        estado, datos = imap.search(None, f"(SINCE {desde})")
        if estado == "OK" and datos[0]:
            uids.update(datos[0].split())

    for uid in sorted(uids):
        estado, datos = imap.fetch(uid, "(RFC822)")
        if estado != "OK" or not datos or not datos[0]:
            continue
        msg = email.message_from_bytes(datos[0][1])
        asunto = _decode(msg.get("Subject"))
        remitente = _decode(msg.get("From"))
        cuerpo = _cuerpo(msg)
        message_id = msg.get("Message-ID") or uid.decode()
        yield message_id.strip(), asunto, remitente, cuerpo, _adjuntos(msg)

    imap.logout()
