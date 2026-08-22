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


def _proyectar(fecha: date):
    """Proyecta ciclos hacia adelante desde el último declarado.

    Mientras no cargues el estado nuevo, el ciclo en curso se estima
    manteniendo el día de cierre del último conocido. Es una aproximación
    explícita, y se corrige sola en cuanto agregás la fila real."""
    ciclo, ini, fin = _DECLARADOS[-1]
    for _ in range(60):
        if fecha <= fin:
            return ciclo, ini, fin
        ini = fin + timedelta(days=1)
        anio, mes = _sumar_meses(fin.year, fin.month, 1)
        fin = date(anio, mes, min(fin.day, calendar.monthrange(anio, mes)[1]))
        ciclo = f"{fin.year:04d}-{fin.month:02d}"
    return None


def _declarado(fecha: date):
    """(ciclo, inicio, fin) del ciclo declarado que contiene `fecha`, o None.

    None significa "usá la regla del día fijo": pasa con las fechas anteriores
    al primer ciclo declarado, así el historial viejo no se mueve de lugar."""
    if not _DECLARADOS:
        return None
    for ciclo, ini, fin in _DECLARADOS:
        if ini <= fecha <= fin:
            return ciclo, ini, fin
    if fecha > _DECLARADOS[-1][2]:
        return _proyectar(fecha)
    return None


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
