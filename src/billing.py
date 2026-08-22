"""Lógica del ciclo de facturación y reparto de cuotas.

La tarjeta se factura el día `BILLING_DAY` (ej: 22). El ciclo cubre desde el 22
de un mes hasta el 21 del siguiente. Una compra en N cuotas reparte
`monto / N` en N ciclos sucesivos.

El "mes de facturación" lo representamos como 'YYYY-MM' del día en que se factura.
"""

import calendar
from datetime import date, timedelta

from . import config

# Ciclos reales tomados del estado de cuenta: [(ciclo, inicio, fin)] ordenados.
# Vacío = se usa la regla del día fijo de abajo.
#
# El banco NO factura un día fijo: un estado dice 23/07–19/08 y el siguiente
# 20/08–17/09. No hay fórmula que reproduzca esos cortes, así que se declaran a
# mano en la hoja "Ciclos" — el propio estado de cuenta trae el "PRÓXIMO PERÍODO
# DE FACTURACIÓN", así que es copiar dos fechas por mes.
_DECLARADOS: list[tuple[str, date, date]] = []


def cargar_ciclos(filas) -> None:
    """Fija los ciclos declarados. Los llama el Store al abrir la planilla."""
    global _DECLARADOS
    _DECLARADOS = sorted(filas, key=lambda f: f[1])


# Feriados de Chile de fecha fija. Solo se usan para correr el corte al día hábil
# anterior; los movibles (29/06, 12/10, 31/10) se omiten a propósito: su regla de
# traslado es enredada y marcar un día como feriado cuando no lo es correría el
# corte de más. Para esos meses está la hoja "Ciclos" como excepción.
_FERIADOS_FIJOS = {(1, 1), (5, 1), (5, 21), (6, 20), (7, 16), (8, 15),
                   (9, 18), (9, 19), (11, 1), (12, 8), (12, 25)}


def _domingo_de_pascua(anio: int) -> date:
    """Algoritmo de Meeus/Jones/Butcher, para ubicar Viernes y Sábado Santo."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    return date(anio, mes, (h + l - 7 * m + 114) % 31 + 1)


def _es_habil(d: date) -> bool:
    if d.weekday() >= 5:                       # sábado o domingo
        return False
    if (d.month, d.day) in _FERIADOS_FIJOS:
        return False
    pascua = _domingo_de_pascua(d.year)
    return d not in (pascua - timedelta(days=2), pascua - timedelta(days=1))


def _cierre_de(anio: int, mes: int) -> date:
    """Día en que cierra el estado de ese mes: el CORTE_DIA, adelantado al hábil
    anterior si cae fin de semana o feriado."""
    dia = min(config.CORTE_DIA, calendar.monthrange(anio, mes)[1])
    d = date(anio, mes, dia)
    while not _es_habil(d):
        d -= timedelta(days=1)
    return d


def _ciclo_de(fecha: date):
    """(ciclo, inicio, fin) automático que contiene `fecha`.

    El ciclo termina en el cierre de su mes y arranca el día después del cierre
    del mes anterior. La etiqueta es el mes del cierre, igual que el estado."""
    anio, mes = fecha.year, fecha.month
    if fecha > _cierre_de(anio, mes):          # ya cerró: cae en el mes siguiente
        anio, mes = _sumar_meses(anio, mes, 1)
    a0, m0 = _sumar_meses(anio, mes, -1)
    return (f"{anio:04d}-{mes:02d}",
            _cierre_de(a0, m0) + timedelta(days=1),
            _cierre_de(anio, mes))


def _desde_auto():
    """Primer día en que rige la regla automática.

    Es el ARRANQUE del ciclo que contiene CORTE_DESDE, no la fecha suelta.
    Anclar al ciclo completo importa: si el corte cayera en medio de un ciclo,
    preguntar por un día y preguntar por el inicio de su propio ciclo darían
    respuestas distintas, y de ahí salían ciclos que se esfumaban del gráfico."""
    try:
        return _ciclo_de(date.fromisoformat(config.CORTE_DESDE))[1]
    except (ValueError, TypeError):
        return None


def _ciclo_automatico(fecha: date):
    """El ciclo automático, solo desde que rige el corte nuevo."""
    desde = _desde_auto()
    if desde is None or fecha < desde:
        return None                            # historial viejo: regla del día fijo
    return _ciclo_de(fecha)


def _declarado(fecha: date):
    """Ciclo que contiene `fecha`, en orden de prioridad:

      1. la hoja "Ciclos", que es la excepción declarada a mano;
      2. la regla automática del corte (día 19 corrido al hábil anterior);
      3. None -> la regla vieja del día fijo, para el historial anterior.
    """
    for ciclo, ini, fin in _DECLARADOS:
        if ini <= fecha <= fin:
            return ciclo, ini, fin
    return _ciclo_automatico(fecha)


def _sumar_meses(anio: int, mes: int, delta: int) -> tuple[int, int]:
    total = (anio * 12 + (mes - 1)) + delta
    return total // 12, total % 12 + 1


def ciclo_de_compra(fecha: date, billing_day: int | None = None) -> str:
    """Devuelve el mes de facturación ('YYYY-MM') donde cae una compra.

    Si el día de la compra es < billing_day -> se factura este mes.
    Si es >= billing_day -> se factura el mes siguiente.
    """
    d = _declarado(fecha)
    if d:
        return d[0]
    bd = billing_day or config.BILLING_DAY
    anio, mes = fecha.year, fecha.month
    if fecha.day >= bd:
        anio, mes = _sumar_meses(anio, mes, 1)
    return f"{anio:04d}-{mes:02d}"


def inicio_de_ciclo(fecha: date, billing_day: int | None = None) -> date:
    """Fecha en que empezó el ciclo que contiene `fecha` (el día `billing_day`
    de este mes si la fecha es >= billing_day, o del mes anterior si no)."""
    d = _declarado(fecha)
    if d:
        return d[1]
    bd = billing_day or config.BILLING_DAY
    if fecha.day >= bd:
        return date(fecha.year, fecha.month, bd)
    anio, mes = _sumar_meses(fecha.year, fecha.month, -1)
    return date(anio, mes, bd)


def proximo_inicio_de_ciclo(fecha: date, billing_day: int | None = None) -> date:
    """Fecha de inicio del ciclo siguiente al que contiene `fecha`."""
    d = _declarado(fecha)
    if d:
        return d[2] + timedelta(days=1)
    inicio = inicio_de_ciclo(fecha, billing_day)
    anio, mes = _sumar_meses(inicio.year, inicio.month, 1)
    return date(anio, mes, inicio.day)


def cuotas_por_ciclo(ciclo_inicio: str, monto: int, num_cuotas: int) -> list[dict]:
    """Reparte un monto en cuotas a partir de un ciclo inicial ('YYYY-MM').
    Devuelve [{ciclo: 'YYYY-MM', numero: k, monto: valor_cuota}, ...].

    El resto de la división se suma a la última cuota para cuadrar el total.
    `ciclo_inicio` es el mes de facturación de la PRIMERA cuota.
    """
    num_cuotas = max(1, num_cuotas)
    base = monto // num_cuotas
    resto = monto - base * num_cuotas

    anio0, mes0 = int(ciclo_inicio[:4]), int(ciclo_inicio[5:7])

    salida = []
    for k in range(num_cuotas):
        anio, mes = _sumar_meses(anio0, mes0, k)
        valor = base + (resto if k == num_cuotas - 1 else 0)
        salida.append({"ciclo": f"{anio:04d}-{mes:02d}", "numero": k + 1, "monto": valor})
    return salida
