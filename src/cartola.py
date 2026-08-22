"""Lectura del estado de cuenta (cartola) que el banco manda por correo.

Escrito por Matías Hernán.

El banco NO factura un día fijo — ver el comentario de CORTE_DIA en config.py —
pero cada cartola trae, al final, una línea con el período del mes siguiente:

    PRÓXIMO PERÍODO DE FACTURACIÓN 23/07/2026 19/08/2026

O sea que el corte que viene siempre está publicado un mes antes. Este módulo
abre el PDF adjunto (viene con clave), lee esa línea y la del período que la
cartola misma está facturando, y las devuelve como ciclos listos para la hoja
"Ciclos". Así el corte deja de ser una regla adivinada y pasa a ser un dato.

El PDF es contenido que llega de afuera: acá solo se le sacan cuatro fechas con
una expresión regular y se validan contra el sentido común (un ciclo dura entre
20 y 40 días y no puede caer a años de distancia). Nada de lo que diga el PDF
se ejecuta ni se registra en el log.
"""

import io
import re
from datetime import date

# El período de facturación de una tarjeta ronda el mes. Fuera de esta ventana
# la lectura está mal (una fecha suelta, un PDF con otro formato) y se descarta.
_DIAS_MIN, _DIAS_MAX = 20, 40
# Y no puede estar a años de hoy: acota el daño de una fecha mal leída.
_DIAS_LEJOS = 400

_PROXIMO = re.compile(
    r"PR[OÓ]XIMO\s+PER[IÍ]ODO\s+DE\s+FACTURACI[OÓ]N\D{0,40}?"
    r"(\d{2}/\d{2}/\d{4})\D{1,40}?(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE)
_FACTURADO = re.compile(
    r"PER[IÍ]ODO\s+FACTURADO\D{0,40}?"
    r"(\d{2}/\d{2}/\d{4})\D{1,40}?(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE)

# Un adjunto se mira solo si el asunto o el nombre del archivo hablan de estado
# de cuenta. No es seguridad — la prueba de verdad es que el PDF abra con la
# clave y traiga la línea del período —, es no ponerse a descifrar cada PDF que
# llegue al correo.
# Ojo con qué se pone acá: "tarjeta de crédito" estuvo un rato y era un error,
# porque es el asunto de CUALQUIER aviso de compra del banco. Solo términos que
# aparezcan en un estado de cuenta y en nada más.
_PISTAS = ("estado de cuenta", "estadodecuenta", "eecc", "cartola", "tarjetavisa")

# Un estado de cuenta pesa cientos de KB. Más que esto es otra cosa.
_MAX_BYTES = 8 * 1024 * 1024


def parece_cartola(asunto: str, nombre_archivo: str) -> bool:
    texto = f"{asunto} {nombre_archivo}".lower()
    return nombre_archivo.lower().endswith(".pdf") and any(p in texto for p in _PISTAS)


def _texto(datos: bytes, clave: str) -> str:
    """Texto plano del PDF, abriéndolo con la clave si viene protegido.

    La clave nunca se imprime ni se guarda: entra por acá y muere acá."""
    import pypdf                                   # se importa recién al usarlo

    lector = pypdf.PdfReader(io.BytesIO(datos))
    if lector.is_encrypted:
        if not clave or not lector.decrypt(clave):
            raise ValueError("el PDF pide una clave que no tenemos")
    return "\n".join(p.extract_text() or "" for p in lector.pages)


def _fecha(txt: str) -> date:
    d, m, a = (int(x) for x in txt.split("/"))
    return date(a, m, d)


def _valido(ini: date, fin: date, hoy: date) -> bool:
    dias = (fin - ini).days + 1
    if not _DIAS_MIN <= dias <= _DIAS_MAX:
        return False
    return abs((fin - hoy).days) <= _DIAS_LEJOS


def _ciclo(ini: date, fin: date) -> tuple:
    """(etiqueta, inicio, fin). La etiqueta es el mes en que CIERRA, igual que
    la cartola: la que cierra el 19/08 es el ciclo '2026-08'."""
    return f"{fin.year:04d}-{fin.month:02d}", ini, fin


def ciclos_del_texto(texto: str, hoy: date | None = None) -> list[tuple]:
    """Los ciclos que declara una cartola: el que está facturando y el próximo.

    Devuelve [] si no encuentra fechas creíbles — mejor no tocar nada que
    mover un ciclo con una lectura dudosa."""
    hoy = hoy or date.today()
    salida = []
    for patron in (_FACTURADO, _PROXIMO):
        m = patron.search(texto)
        if not m:
            continue
        try:
            ini, fin = _fecha(m.group(1)), _fecha(m.group(2))
        except ValueError:
            continue
        if _valido(ini, fin, hoy):
            salida.append(_ciclo(ini, fin))
    return salida


def ciclos_del_pdf(datos: bytes, clave: str, hoy: date | None = None) -> list[tuple]:
    """Ciclos declarados por el PDF de la cartola. [] si no se pudo leer."""
    if not datos or len(datos) > _MAX_BYTES:
        return []
    try:
        return ciclos_del_texto(_texto(datos, clave), hoy)
    except Exception as e:
        # Sin detalles: el repo es público y los logs de Actions también.
        print(f"[cartola] no pude leer el PDF ({type(e).__name__})")
        return []
