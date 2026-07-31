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


def obtener_correos():
    """Generador de (uid, asunto, remitente, cuerpo) de correos recientes del banco."""
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
        # 2) Además, cualquier correo cuyo asunto hable de transferencia
        #    (así detectamos ingresos de bancos que no están en la lista).
        for palabra in ("transferencia", "transferido", "abono"):
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
        yield message_id.strip(), asunto, remitente, cuerpo

    imap.logout()
