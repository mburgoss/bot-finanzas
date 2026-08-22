"""Configuración central del bot. Todo lo sensible viene de variables de entorno
(GitHub Secrets). Nada de credenciales hardcodeadas en el repo."""

import os
import re

# --- Gmail (IMAP) ---
GMAIL_USER = os.environ["GMAIL_USER"]              # tu correo, ej: tucorreo@gmail.com
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]  # App Password de 16 dígitos
IMAP_HOST = "imap.gmail.com"

# Remitentes del banco a vigilar (en minúsculas). Ajustar según tu banco.
# Ej: "avisos@tubanco.cl", "notificaciones@tubanco.cl"
BANK_SENDERS = [s.strip().lower() for s in os.environ.get("BANK_SENDERS", "").split(",") if s.strip()]

# Cuántos días hacia atrás mirar como máximo (por si el bot estuvo caído).
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))

# Tu número de cuenta (solo dígitos). Un correo que NO viene de un remitente
# conocido solo se procesa si contiene este número (así una transferencia real
# a tu cuenta entra, y correos ajenos se ignoran). Ej: 001234567890
DEST_ACCOUNT = re.sub(r"\D", "", os.environ.get("DEST_ACCOUNT", ""))

# --- Telegram ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- Google Sheets ---
# El JSON de la cuenta de servicio va en el secret GOOGLE_CREDENTIALS (texto completo).
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
SHEET_ID = os.environ["SHEET_ID"]  # el id de la planilla (de la URL)

# --- Reglas de negocio ---
# Día en que se factura la tarjeta de crédito. Regla vieja, se conserva para el
# historial anterior a CORTE_DESDE (así los movimientos viejos no cambian de ciclo).
BILLING_DAY = int(os.environ.get("BILLING_DAY", "22"))

# --- Corte real del estado de cuenta ---
# Aproximación: el banco cierra el día CORTE_DIA y, si cae fin de semana o
# feriado, adelanta al día hábil anterior.
#
# OJO: es una regla de aproximación, no la del banco. Cortes observados en los
# estados de cuenta reales:
#     21/04  19/05  18/06  22/07  19/08  17/09
# La regla acierta 19/05, 19/08 y 17/09 (el 19/09 era sábado y el 18 y 19 son
# Fiestas Patrias), pero falla en 21/04, en 18/06 (el 19/06 era viernes hábil) y
# en 22/07. Por eso rige recién desde CORTE_DESDE, y por eso existe la hoja
# "Ciclos": cada estado de cuenta trae el "PRÓXIMO PERÍODO DE FACTURACIÓN" con
# las dos fechas del mes que viene, así que copiarlas ahí una vez al mes es la
# única fuente confiable. La regla automática solo tapa los huecos.
CORTE_DIA = int(os.environ.get("CORTE_DIA", "19"))
# Desde cuándo rige. Antes de esta fecha se usa BILLING_DAY, para no reasignar de
# ciclo todo el historial ya cargado.
CORTE_DESDE = os.environ.get("CORTE_DESDE", "2026-07-23")

# Mapeo de últimos 4 dígitos -> tipo de tarjeta. Ej: "1234:credito,5678:debito"
# Si un cobro no matchea, se usa lo que diga el propio correo (crédito/débito).
CARD_MAP = {}
for pair in os.environ.get("CARD_MAP", "").split(","):
    if ":" in pair:
        digits, tipo = pair.split(":")
        CARD_MAP[digits.strip()] = tipo.strip().lower()

# Zona horaria para fechas (Chile).
TIMEZONE = os.environ.get("TIMEZONE", "America/Santiago")

# Hora local (0-23) del resumen nocturno automático por Telegram.
RESUMEN_HORA = int(os.environ.get("RESUMEN_HORA", "22"))

# Tema del gráfico del resumen: "claro" u "oscuro" (ver src/grafico.py).
GRAFICO_TEMA = os.environ.get("GRAFICO_TEMA", "claro").strip().lower()

# Panel fijado: un único mensaje con el gráfico, siempre al día, arriba del chat.
PANEL_ACTIVO = os.environ.get("PANEL_ACTIVO", "1").strip().lower() not in ("0", "false", "no")
# Cada cuántos minutos refrescar SOLO el sello de hora cuando los números no
# cambiaron. Cualquier cambio real (movimiento, anulación, cuotas) se refleja en
# la corrida siguiente sin esperar esto: el disparador es el contenido, no el reloj.
PANEL_MINUTOS = int(os.environ.get("PANEL_MINUTOS", "15"))

# Cuántos mensajes tienen que pasar en el chat para que el panel se BAJE al
# final. Editar un mensaje no lo mueve, así que sin esto el panel quedaría con el
# tiempo cientos de mensajes arriba: cada tantos mensajes se borra y se manda de
# nuevo abajo.
#   1  -> queda siempre pegado abajo del último mensaje (por defecto). Desde que
#         la bajada no fija nada, no cuesta avisos; y como el panel nunca se pone
#         viejo, nunca choca con el límite de 48 horas que Telegram impone para
#         borrar un mensaje propio.
#   >1 -> baja cada N mensajes: menos llamadas a la API, algo más de scroll.
#   0  -> se queda quieto donde nació, y ahí sí se fija arriba del chat.
PANEL_MOVER_TRAS = int(os.environ.get("PANEL_MOVER_TRAS", "1"))

# Vista previa a pedido: manda el resumen del día sin esperar a RESUMEN_HORA y
# sin marcar el día como enviado. Se activa desde el input del workflow.
FORZAR_RESUMEN = os.environ.get("FORZAR_RESUMEN", "").strip().lower() in ("1", "true", "yes", "si", "sí")

# Diagnóstico de correos: imprime remitente y asunto de los que no se supieron
# leer. OJO: el repo es público y los logs de Actions también, así que esto deja
# datos del banco a la vista. Prender solo mientras se depura, y apagar después.
DEBUG_CORREOS = os.environ.get("DEBUG_CORREOS", "").strip().lower() in ("1", "true", "yes", "si", "sí")

# --- Categorías de gasto ---
# Lista fija (editable). El orden define los botones de Telegram. Se pueden
# agregar más en caliente con "➕ Otra": esas quedan guardadas en la hoja Config
# (clave 'categorias_extra') y aparecen como botones a partir de ahí.
CATEGORIAS = [
    ("🛒", "Alimentación"),
    ("🍔", "Restaurantes"),
    ("🚗", "Transporte"),
    ("🏠", "Hogar y servicios"),
    ("🎮", "Ocio"),
    ("👕", "Compras"),
    ("💊", "Salud"),
    ("📚", "Educación"),
    ("💸", "Otros"),
]
SIN_CATEGORIA = "Sin categoría"
