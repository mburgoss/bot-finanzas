"""Parseo de los correos de aviso del banco.

Los ejemplos son ficticios (nombres, comercios, montos y dígitos inventados).
Soporta dos tipos de correo:
  1. Compra con tarjeta (remitente de avisos del banco):
     "se ha realizado una compra por $10.000 con Tarjeta de Crédito ****0000
      en COMERCIO EJEMPLO el 01/01/2025 12:00."
  2. Transferencia a terceros:
     "...Nombre y Apellido Juan Perez ... Monto $50.000 ...
      lunes 01 de enero de 2025 12:00"

Un `Movimiento` parseado tiene: fecha, comercio, monto (int CLP), digitos y tipo.
tipo ∈ {"credito", "debito", "transferencia", "ingreso"}.
"""

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Movimiento:
    fecha: date          # fecha de la compra / transferencia
    comercio: str        # comercio o destinatario
    monto: int           # monto total en CLP (entero)
    digitos: str         # últimos 4 dígitos ("" si no aplica)
    tipo: str            # "credito" | "debito" | "transferencia" | ""
    uid: str = ""        # id único del correo (para no duplicar)


MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _monto_a_int(texto: str) -> int:
    t = texto.replace("$", "").replace(" ", "").strip().split(",")[0]
    t = t.replace(".", "")
    return int(re.sub(r"[^\d]", "", t) or 0)


def _fecha_numerica(texto: str) -> date:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto.strip(), fmt).date()
        except ValueError:
            continue
    return date.today()


def _fecha_texto_es(dia: str, mes: str, anio: str) -> date:
    m = MESES.get(mes.lower(), date.today().month)
    return date(int(anio), m, int(dia))


def _normalizar_tipo(texto: str) -> str:
    t = texto.lower()
    if "créd" in t or "cred" in t:
        return "credito"
    if "déb" in t or "deb" in t or "redcompra" in t:
        return "debito"
    return ""


def _limpiar_texto(cuerpo: str) -> str:
    texto = re.sub(r"<[^>]+>", " ", cuerpo)      # quita etiquetas HTML
    texto = html.unescape(texto)                 # &eacute; -> é, &nbsp; -> espacio
    texto = texto.replace("\xa0", " ")           # espacio duro -> normal
    return re.sub(r"\s+", " ", texto).strip()    # normaliza espacios


def _sin_acentos(texto: str) -> str:
    """'realizó' -> 'realizo'. Los bancos escriben la misma frase con y sin
    tilde según la plantilla, y una frase clave no puede depender de eso."""
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


# --- Patrón de COMPRA con TARJETA ---
RE_COMPRA = re.compile(
    r"se\s*ha\s*realizado\s*una\s*compra\s*por\s*\$?(?P<monto>[\d.]+)\s*"
    r"con\s*Tarjeta\s*de\s*(?P<tipo>Cr[ée]dito|D[ée]bito)\s*"
    r"\*+(?P<digitos>\d{4})\s*en\s*(?P<comercio>.+?)\s*"
    r"el\s*(?P<fecha>\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE | re.DOTALL,
)

# --- Patrón de CARGO directo a la CUENTA corriente ---
# Ej: "se ha realizado una compra por $10.000 con cargo a Cuenta ****0000 en
#      COMERCIO EJEMPLO el 01/01/2025 12:00." -> se trata como débito.
RE_CARGO_CUENTA = re.compile(
    r"se\s*ha\s*realizado\s*una\s*compra\s*por\s*\$?(?P<monto>[\d.]+)\s*"
    r"con\s*cargo\s*a\s*Cuenta\s*"
    r"\*+(?P<digitos>\d{4})\s*en\s*(?P<comercio>.+?)\s*"
    r"el\s*(?P<fecha>\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE | re.DOTALL,
)

# --- Patrón de TRANSFERENCIA ---
RE_TRANSFER_DEST = re.compile(
    r"Nombre\s*y\s*Apellido\s*(?P<dest>.+?)\s*Rut", re.IGNORECASE | re.DOTALL
)
RE_TRANSFER_MONTO = re.compile(r"Monto\s*\$?(?P<monto>[\d.]+)", re.IGNORECASE)
RE_TRANSFER_FECHA = re.compile(
    r"(?P<dia>\d{1,2})\s*de\s*(?P<mes>[a-záéíóú]+)\s*de\s*(?P<anio>\d{4})",
    re.IGNORECASE,
)

# --- Patrones de INGRESO (transferencias recibidas). Un dict por banco; se
# agregan más a medida que aparezcan otros formatos. Ojo: pueden venir de
# CUALQUIER banco. ---
INGRESO_BANCOS = [
    {
        "nombre": "banco_b",
        "monto": re.compile(r"Monto\s*(?:de\s*)?transferencia\s*\$?(?P<monto>[\d.]+)", re.IGNORECASE),
        "de": re.compile(r"cliente\s+(?P<de>.+?)\s+ha\s+instruido", re.IGNORECASE | re.DOTALL),
        "fecha": re.compile(r"Fecha\s+(?P<fecha>\d{1,2}-\d{1,2}-\d{4})", re.IGNORECASE),
    },
    {
        # Santander, formato "Comprobante Transferencia de fondos" (2026): dejó
        # de decir "ha instruido" y pasó a "realizó una transferencia a tu cuenta".
        "nombre": "santander_comprobante",
        "monto": re.compile(r"Monto\s*transferido\s*\$?\s*(?P<monto>[\d.]+)", re.IGNORECASE),
        "de": re.compile(r"cliente\s+(?P<de>.+?)\s+realiz", re.IGNORECASE | re.DOTALL),
        "fecha": re.compile(r"con\s*fecha\s*(?P<fecha>\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
    },
    {
        # Tenpo, "Comprobante de transferencia exitosa". Ojo: su plantilla rotula
        # "Nombre del destinatario" a QUIEN ENVÍA, así que el nombre se saca de
        # "La transferencia de X por $N a tu cuenta".
        "nombre": "tenpo",
        "monto": re.compile(r"Monto\s*transferencia\s*:?\s*\$?\s*(?P<monto>[\d.]+)", re.IGNORECASE),
        "de": re.compile(r"transferencia\s+de\s+(?P<de>.+?)\s+por\s+\$", re.IGNORECASE | re.DOTALL),
        "fecha": re.compile(r"Fecha\s*:?\s*(?P<fecha>\d{1,2}-\d{1,2}-\d{4})", re.IGNORECASE),
    },
]

# Frases que indican FUERTEMENTE que es un ingreso (transferencia recibida).
# Deben ser específicas para no confundirse con estados de cuenta u otros correos.
# Se comparan contra el texto en minúsculas y SIN TILDES (ver _sin_acentos), así
# que van escritas sin tildes a propósito.
_CLAVES_INGRESO = (
    "transferencia de fondos recibida", "fondos recibida", "transferencia recibida",
    "recibiste una transferencia", "te ha transferido", "te transfiri",
    "transferencia a tu favor", "transferencia a su favor", "abono por transferencia",
    "ha instruido una transferencia de fondos a su cuenta",  # formato de otro banco
    # Formatos que rompieron la detección en agosto 2026 (ver corridas fallidas):
    "realizo una transferencia a tu cuenta",   # Santander "Comprobante"
    "realizo una transferencia a su cuenta",
    "a tu cuenta fue exitosa",                 # Tenpo "Comprobante de transferencia"
    "a su cuenta fue exitosa",
)

# Correos que NUNCA son un ingreso (aunque mencionen "abono", "cuenta", etc.).
_EXCLUIR_INGRESO = (
    "estado de cuenta", "compra con tarjeta", "resumen", "cartola",
    "transferencia a terceros", "comprobante de transferencia a terceros",
)

# Monto genérico: primer "$" seguido de un número (con puntos de miles).
RE_MONTO_GENERICO = re.compile(r"\$\s?(?P<monto>\d{1,3}(?:\.\d{3})+|\d{3,})")

# Nombre del remitente en formatos genéricos de ingreso.
RE_NOMBRE_GENERICO = [
    re.compile(r"cliente\s+(?P<de>.+?)\s+ha\s+instruido", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?P<de>[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+){1,3})\s+(?:te|le)\s+ha\s+(?:transferido|enviado)", re.IGNORECASE),
    re.compile(r"de\s+parte\s+de\s+(?P<de>[A-ZÁÉÍÓÚÑ].+?)(?:\s+por|\.|,)", re.IGNORECASE),
]


def _normalizado(asunto: str, cuerpo: str) -> str:
    return _sin_acentos((asunto + " " + _limpiar_texto(cuerpo)).lower())


def parece_ingreso(asunto: str, cuerpo: str) -> bool:
    """True si el correo parece una transferencia recibida (dinero que entra)."""
    bajo = _normalizado(asunto, cuerpo)
    if any(x in bajo for x in _EXCLUIR_INGRESO):   # estados de cuenta, envíos, etc.
        return False
    return any(clave in bajo for clave in _CLAVES_INGRESO)


# Palabras que delatan que un correo mueve plata, aunque no sepamos leer su
# formato exacto.
_SENIALES_MOVIMIENTO = ("transferencia", "transferido", "transfirio", "abono",
                        "compra", "cargo", "giro", "pago")

# Exclusiones propias, MÁS ANGOSTAS que las de ingreso: acá "compra con tarjeta"
# y "transferencia a terceros" sí son movimientos que queremos ver. Solo se
# descarta lo que agrupa muchos movimientos o no es ninguno.
_EXCLUIR_MOVIMIENTO = ("estado de cuenta", "cartola", "resumen mensual",
                       "resumen de cuenta", "resumen de tu tarjeta")


def parece_movimiento(asunto: str, cuerpo: str) -> bool:
    """True si el correo parece mover plata: trae un monto en pesos y habla de
    una operación, y no es un estado de cuenta ni publicidad.

    Es a propósito más amplia que `parece_ingreso`: sirve de red de seguridad
    para avisar de correos que NO supimos parsear. La red no puede depender de
    `parece_ingreso`, porque cuando esa falla (un banco cambia la redacción) la
    red fallaría con ella y el movimiento se pierde en silencio — que es
    exactamente lo que pasó con Santander y Tenpo en agosto 2026.
    """
    bajo = _normalizado(asunto, cuerpo)
    if any(x in bajo for x in _EXCLUIR_MOVIMIENTO):
        return False
    if not RE_MONTO_GENERICO.search(bajo):
        return False
    return any(s in bajo for s in _SENIALES_MOVIMIENTO)


def _parsear_compra(asunto: str, texto: str, uid: str) -> Movimiento | None:
    m = RE_COMPRA.search(texto)
    if m:
        tipo = _normalizar_tipo(m.group("tipo")) or _normalizar_tipo(asunto)
    else:
        # ¿Es un cargo directo a la cuenta corriente? Cuenta como débito.
        m = RE_CARGO_CUENTA.search(texto)
        tipo = "debito"
    if not m:
        return None
    monto = _monto_a_int(m.group("monto"))
    if monto <= 0:
        return None
    comercio = re.sub(r"\s+", " ", m.group("comercio").strip(" .-"))
    return Movimiento(
        fecha=_fecha_numerica(m.group("fecha")),
        comercio=comercio or "Comercio desconocido",
        monto=monto,
        digitos=m.group("digitos").strip(),
        tipo=tipo,
        uid=uid,
    )


def _parsear_transferencia(texto: str, uid: str) -> Movimiento | None:
    mm = RE_TRANSFER_MONTO.search(texto)
    if not mm:
        return None
    monto = _monto_a_int(mm.group("monto"))
    if monto <= 0:
        return None
    md = RE_TRANSFER_DEST.search(texto)
    dest = re.sub(r"\s+", " ", md.group("dest").strip()) if md else "destinatario"
    mf = RE_TRANSFER_FECHA.search(texto)
    fecha = _fecha_texto_es(mf.group("dia"), mf.group("mes"), mf.group("anio")) \
        if mf else date.today()
    return Movimiento(
        fecha=fecha,
        comercio=f"Transferencia a {dest}",
        monto=monto,
        digitos="",
        tipo="transferencia",
        uid=uid,
    )


def _parsear_ingreso(texto: str, uid: str) -> Movimiento | None:
    """Transferencia recibida (dinero que entra).
    1) Prueba el formato exacto de cada banco conocido.
    2) Si ninguno matchea, usa un parser genérico (monto + nombre best-effort)."""
    # 1) Formatos conocidos.
    for banco in INGRESO_BANCOS:
        mm = banco["monto"].search(texto)
        if not mm:
            continue
        monto = _monto_a_int(mm.group("monto"))
        if monto <= 0:
            continue
        md = banco["de"].search(texto)
        de = re.sub(r"\s+", " ", md.group("de").strip().title()) if md else "alguien"
        mf = banco["fecha"].search(texto)
        fecha = _fecha_numerica(mf.group("fecha")) if mf else date.today()
        return Movimiento(fecha=fecha, comercio=f"Transferencia de {de}",
                          monto=monto, digitos="", tipo="ingreso", uid=uid)

    # 2) Genérico: cualquier banco. Extrae el primer monto y, si puede, un nombre.
    mm = RE_MONTO_GENERICO.search(texto)
    if not mm:
        return None
    monto = _monto_a_int(mm.group("monto"))
    if monto <= 0:
        return None
    de = ""
    for patron in RE_NOMBRE_GENERICO:
        md = patron.search(texto)
        if md:
            de = re.sub(r"\s+", " ", md.group("de").strip().title())
            break
    comercio = f"Transferencia de {de}" if de else "Transferencia recibida"
    mf = re.search(r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b", texto)  # fecha numérica si hay
    fecha = _fecha_numerica(mf.group(1)) if mf else date.today()
    return Movimiento(fecha=fecha, comercio=comercio,
                      monto=monto, digitos="", tipo="ingreso", uid=uid)


def parsear(asunto: str, cuerpo: str, uid: str = "") -> Movimiento | None:
    """Devuelve un Movimiento o None si el correo no es un cobro/transferencia."""
    texto = _limpiar_texto(cuerpo)
    bajo = (asunto + " " + texto).lower()

    # 1) Ingreso (dinero recibido) — prioridad para no confundirlo con gasto.
    if parece_ingreso(asunto, cuerpo):
        ingreso = _parsear_ingreso(texto, uid)
        if ingreso:
            return ingreso

    # 2) Transferencia enviada.
    if "transferencia a terceros" in bajo:
        return _parsear_transferencia(texto, uid)

    # 3) Compra con tarjeta.
    return _parsear_compra(asunto, texto, uid)
