"""Lógica del ciclo de facturación y reparto de cuotas.

La tarjeta se factura el día `BILLING_DAY` (ej: 22). El ciclo cubre desde el 22
de un mes hasta el 21 del siguiente. Una compra en N cuotas reparte
`monto / N` en N ciclos sucesivos.

El "mes de facturación" lo representamos como 'YYYY-MM' del día en que se factura.
"""

from datetime import date

from . import config


def _sumar_meses(anio: int, mes: int, delta: int) -> tuple[int, int]:
    total = (anio * 12 + (mes - 1)) + delta
    return total // 12, total % 12 + 1


def ciclo_de_compra(fecha: date, billing_day: int | None = None) -> str:
    """Devuelve el mes de facturación ('YYYY-MM') donde cae una compra.

    Si el día de la compra es < billing_day -> se factura este mes.
    Si es >= billing_day -> se factura el mes siguiente.
    """
    bd = billing_day or config.BILLING_DAY
    anio, mes = fecha.year, fecha.month
    if fecha.day >= bd:
        anio, mes = _sumar_meses(anio, mes, 1)
    return f"{anio:04d}-{mes:02d}"


def inicio_de_ciclo(fecha: date, billing_day: int | None = None) -> date:
    """Fecha en que empezó el ciclo que contiene `fecha` (el día `billing_day`
    de este mes si la fecha es >= billing_day, o del mes anterior si no)."""
    bd = billing_day or config.BILLING_DAY
    if fecha.day >= bd:
        return date(fecha.year, fecha.month, bd)
    anio, mes = _sumar_meses(fecha.year, fecha.month, -1)
    return date(anio, mes, bd)


def proximo_inicio_de_ciclo(fecha: date, billing_day: int | None = None) -> date:
    """Fecha de inicio del ciclo siguiente al que contiene `fecha`."""
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
