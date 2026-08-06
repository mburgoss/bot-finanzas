"""Formateo compartido por el texto de Telegram y el gráfico del resumen.

Vive aparte para que `main` y `grafico` muestren los mismos pesos, los mismos
nombres de mes y el mismo redondeo de porcentajes.
"""

MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def pesos(n) -> str:
    """1269 -> '$1.269' (separador de miles chileno)."""
    return f"${int(n):,.0f}".replace(",", ".")


def nombre_ciclo(ciclo: str) -> str:
    """'2026-08' -> 'agosto 2026'."""
    anio, mes = ciclo[:4], int(ciclo[5:7])
    return f"{MESES[mes]} {anio}"


def mes_corto(ciclo_lbl: str) -> str:
    """'agosto 2026' -> 'Agosto' (etiqueta corta para barras y leyendas)."""
    return ciclo_lbl.split()[0].capitalize()


def delta_pct(actual: int, previo: int) -> int:
    """Variación porcentual de `actual` respecto de `previo`, redondeada.

    Se usa tanto en el texto como en el gráfico para que nunca muestren
    números distintos. Devuelve 0 si no hay base con la que comparar.
    """
    if not previo:
        return 0
    return round((actual - previo) * 100 / abs(previo))
