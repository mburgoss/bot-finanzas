"""Gráfico del resumen nocturno: el gasto acumulado del ciclo en curso contra
los dos ciclos anteriores, cortados a la misma altura (el mismo día del ciclo).

Se renderiza en memoria con matplotlib (backend 'Agg', sin ventanas ni archivos
temporales) y se manda a Telegram como PNG con sendPhoto.

Decisiones de diseño (guía de visualización):
  - Tres colores categóricos validados para daltonismo — azul/naranja/aqua, con
    ΔE CVD >= 9 entre pares adyacentes en ambos temas.
  - Líneas de 2px, punta redonda; grilla hairline sólida y recesiva; el ciclo en
    curso lleva más grosor, relleno al 10% y punto final, así la jerarquía la da
    el peso y no el color.
  - El texto nunca usa el color de la serie: la identidad la carga la línea-clave
    de la leyenda. Los únicos colores en texto son los deltas (+/− con signo).
  - Los montos van en la leyenda además de en el gráfico, porque el aqua queda
    bajo 3:1 contra el fondo claro y necesita etiqueta visible como respaldo.
"""

import io
from datetime import date, timedelta

import matplotlib

matplotlib.use("Agg")  # sin display: obligatorio antes de importar pyplot

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .formato import delta_pct, pesos  # noqa: E402

TEMAS = {
    "claro": {
        "fondo": "#fcfcfb", "tinta": "#0b0b0b", "tinta2": "#52514e",
        "apagado": "#898781", "grilla": "#e1e0d9", "eje": "#c3c2b7",
        "series": ("#2a78d6", "#eb6834", "#1baf7a"),
        "baja": "#006300", "sube": "#d03b3b",
    },
    "oscuro": {
        "fondo": "#1a1a19", "tinta": "#ffffff", "tinta2": "#c3c2b7",
        "apagado": "#898781", "grilla": "#2c2c2a", "eje": "#383835",
        "series": ("#3987e5", "#d95926", "#199e70"),
        "baja": "#0ca30c", "sube": "#d03b3b",
    },
}


def acumular(por_dia: dict, inicio: date, dias: int) -> list:
    """Serie acumulada día a día: `dias` puntos desde `inicio` (día 1 = inicio).

    `por_dia` es {fecha: neto} y puede tener huecos — los días sin movimiento
    repiten el acumulado anterior, que es justo lo que dibuja la curva.
    """
    total, salida = 0, []
    for k in range(dias):
        total += por_dia.get(inicio + timedelta(days=k), 0)
        salida.append(total)
    return salida


def _formato_eje(maximo: int):
    """Ticks cortos: '$0', '$450k', '$1,2M'. Los montos exactos van en la leyenda."""
    def fmt(v, _pos):
        if v == 0:
            return "$0"
        if maximo >= 1_000_000:
            return f"${v / 1e6:.1f}".replace(".0", "").replace(".", ",") + "M"
        return f"${v / 1000:.0f}k"
    return FuncFormatter(fmt)


def _leyenda(ax, series, tema):
    """Bloque leyenda + montos arriba a la izquierda (la zona que las curvas
    acumuladas dejan libre siempre, porque arrancan abajo y suben a la derecha).

    Cumple dos funciones a la vez: identidad de cada serie por línea-clave (nunca
    color solo) y tabla de valores exactos."""
    actual = series[0]["valores"][-1]
    # El gasto del ciclo en curso es LA cifra de la imagen: va en tamaño hero,
    # más grande incluso que el título. Los ciclos anteriores quedan como tabla
    # de comparación, chicos y parejos — la jerarquía la marca el tamaño.
    PASO_HERO, PASO = 0.150, 0.085
    alto = PASO_HERO + PASO * (len(series) - 1) + 0.035

    # Respaldo en color de fondo, por si una curva sube antes de lo esperado.
    ax.add_patch(FancyBboxPatch(
        (0.005, 0.985 - alto), 0.735, alto,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        transform=ax.transAxes, facecolor=tema["fondo"], edgecolor="none",
        zorder=6, clip_on=False))

    y = 0.905
    for i, s in enumerate(series):
        principal = i == 0
        ax.plot([0.03, 0.080], [y, y], transform=ax.transAxes,
                color=tema["series"][i], linewidth=3.0 if principal else 1.9,
                solid_capstyle="round", zorder=7, clip_on=False)
        ax.text(0.100, y, s["etiqueta"], transform=ax.transAxes,
                fontsize=12 if principal else 10.5,
                color=tema["tinta"] if principal else tema["tinta2"],
                va="center", ha="left", zorder=7)
        # Todos los montos comparten el borde derecho: el hero manda por tamaño,
        # no por posición, y la columna sigue leyéndose como columna.
        ax.text(0.600, y, pesos(s["valores"][-1]), transform=ax.transAxes,
                fontsize=22 if principal else 10.5,
                color=tema["tinta"] if principal else tema["tinta2"],
                fontweight="bold" if principal else "normal",
                va="center", ha="right", zorder=7)
        if not principal:
            d = delta_pct(actual, s["valores"][-1])
            # Gastar más que el ciclo de referencia es la dirección mala.
            # El signo menos va tipográfico (−), que el guión ASCII casi no se ve.
            ax.text(0.625, y, f"{d:+d}%".replace("-", "−"),
                    transform=ax.transAxes, fontsize=10.5,
                    color=tema["sube"] if d > 0 else tema["baja"],
                    va="center", ha="left", zorder=7)
        y -= PASO_HERO if principal else PASO


def _pie_de_tablas(fig, bloques, t, nota=None):
    """Dibuja las tablas al pie de la imagen, en dos columnas.

    Van acá y no en el caption de Telegram por una razón concreta: el bloque
    <pre> del caption se envuelve en pantallas angostas —en un iPhone parte cada
    fila en dos— y la tabla deja de leerse como tabla. Dentro de la imagen el
    ancho lo fijamos nosotros, así que las columnas aguantan en cualquier
    teléfono.

    `bloques` = [(titulo, [(etiqueta, monto, es_total), ...]), ...], máximo dos.
    Cada monto se ancla a la derecha de su columna: la alineación no depende de
    una fuente monoespaciada.
    """
    # Columnas al ras de los márgenes y con el pasillo justo: cada punto de ancho
    # que se gana acá es un punto que la etiqueta no tiene que resignar cuando la
    # letra crece.
    columnas = [(0.100, 0.515), (0.545, 0.980)]
    fig.add_artist(Line2D([0.100, 0.980], [0.315, 0.315],
                          color=t["grilla"], linewidth=0.8))
    for (x_izq, x_der), (titulo, filas) in zip(columnas, bloques):
        fig.text(x_izq, 0.278, titulo.upper(), color=t["apagado"], fontsize=9,
                 fontweight="bold", va="top", ha="left")
        y = 0.215
        for etiqueta, monto, es_total in filas:
            peso = "bold" if es_total else "normal"
            color = t["tinta"] if es_total else t["tinta2"]
            if es_total:    # regla que cierra los sumandos, como en una suma escrita
                fig.add_artist(Line2D([x_izq, x_der], [y + 0.030, y + 0.030],
                                      color=t["eje"], linewidth=0.8))
            texto = ("−" if monto < 0 else "") + f"${abs(int(monto)):,.0f}".replace(",", ".")
            fig.text(x_izq, y, etiqueta, color=color, fontsize=10.5,
                     fontweight=peso, va="top", ha="left")
            fig.text(x_der, y, texto, color=color, fontsize=10.5,
                     fontweight=peso, va="top", ha="right")
            y -= 0.065
    if nota:
        # Al pie de la columna izquierda: aclara que "Débito y transf." va neto,
        # matiz que se perdió al acortar las etiquetas para agrandar la letra.
        fig.text(columnas[0][0], 0.020, nota, color=t["apagado"], fontsize=8.5,
                 va="bottom", ha="left")


def ritmo_de_gasto(series, dias_ciclo: int, subtitulo: str,
                   proyeccion: int | None = None, tema: str = "claro",
                   bloques=None, nota=None) -> bytes:
    """Devuelve el PNG (bytes) del gráfico de ritmo de gasto.

    `series` es una lista de {"etiqueta": str, "valores": [acumulado por día]},
    con el ciclo en curso primero y todas del mismo largo (hasta hoy).
    `proyeccion` es el cierre estimado del ciclo; si viene, se dibuja punteada
    desde hoy hasta el final del ciclo.
    """
    t = TEMAS.get(tema, TEMAS["claro"])
    dias_con_datos = len(series[0]["valores"])
    x = list(range(1, dias_con_datos + 1))

    plt.rcParams["font.family"] = "DejaVu Sans"
    # Con tablas al pie la imagen crece: el gráfico conserva su alto y las
    # tablas se llevan el espacio nuevo, no se lo quitan a la curva.
    fig, ax = plt.subplots(figsize=(6.4, 5.5 if bloques else 4.0), dpi=190)
    fig.patch.set_facecolor(t["fondo"])
    ax.set_facecolor(t["fondo"])

    # --- Chrome: grilla horizontal hairline, sin marco, ticks sin varilla ---
    ax.grid(axis="y", color=t["grilla"], linewidth=0.8)
    ax.set_axisbelow(True)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    ax.spines["bottom"].set_color(t["eje"])
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(colors=t["apagado"], labelsize=9.5, length=0, pad=6)

    # --- Series: el ciclo en curso manda por grosor y relleno, no por color ---
    for i, s in enumerate(series):
        principal = i == 0
        ax.plot(x, s["valores"], color=t["series"][i],
                linewidth=2.2 if principal else 1.6,
                solid_capstyle="round", solid_joinstyle="round",
                zorder=5 - i * 0.1)
        if principal:
            ax.fill_between(x, 0, s["valores"], color=t["series"][i],
                            alpha=0.10, linewidth=0, zorder=1)

    hoy_y = series[0]["valores"][-1]

    # --- Proyección de cierre: punteada, que es lo que el punteado sí significa ---
    if proyeccion and dias_con_datos < dias_ciclo:
        ax.plot([dias_con_datos, dias_ciclo], [hoy_y, proyeccion],
                color=t["series"][0], linewidth=1.4, linestyle=(0, (3, 3)),
                alpha=0.55, zorder=3)
        ax.plot([dias_ciclo], [proyeccion], marker="o", markersize=5,
                markerfacecolor=t["fondo"], markeredgecolor=t["series"][0],
                markeredgewidth=1.4, zorder=6)
        # "proy." y no "proyección": con la leyenda más ancha por la cifra hero,
        # el texto largo quedaba tapado por el respaldo de la leyenda.
        ax.annotate(f"proy. {pesos(proyeccion)}",
                    xy=(dias_ciclo, proyeccion), xytext=(-6, 9),
                    textcoords="offset points", ha="right", va="bottom",
                    fontsize=9, color=t["apagado"], zorder=6)

    # Punto de hoy: anillo del color del fondo para que no se pise con la línea.
    ax.plot([dias_con_datos], [hoy_y], marker="o", markersize=7,
            color=t["series"][0], markeredgecolor=t["fondo"],
            markeredgewidth=2, zorder=7)

    # --- Escalas ---
    tope = max([max(s["valores"]) for s in series] + [proyeccion or 0])
    piso = min([min(s["valores"]) for s in series] + [0])
    respiro = max(tope - piso, 1) * 0.14
    ax.set_ylim(piso - (respiro * 0.25 if piso < 0 else 0), tope + respiro)
    ax.set_xlim(0.5, dias_ciclo + 0.5)
    ax.yaxis.set_major_formatter(_formato_eje(tope))
    # Pocas líneas de grilla: sin esto, un ciclo recién empezado (rango chico)
    # se llena de diez gridlines.
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, min_n_ticks=3))

    ticks = [1] + [d for d in range(5, dias_ciclo + 1, 5)]
    ax.set_xticks(ticks)
    ax.set_xlabel("día del ciclo", fontsize=9.5, color=t["apagado"], labelpad=8)

    _leyenda(ax, series, t)

    # --- Encabezado, tablas y nota al pie, fuera del área de trazado ---
    if bloques:
        fig.subplots_adjust(left=0.105, right=0.975, top=0.855, bottom=0.42)
        fig.text(0.105, 0.968, "Ritmo de gasto", color=t["tinta"],
                 fontsize=16, fontweight="bold", va="top", ha="left")
        fig.text(0.100, 0.918, subtitulo, color=t["tinta2"],
                 fontsize=11, va="top", ha="left")
        _pie_de_tablas(fig, bloques, t, nota)
        # Arriba de la línea divisoria: abajo choca con el total de la derecha.
        nota_y = 0.322
    else:
        fig.subplots_adjust(left=0.105, right=0.975, top=0.80, bottom=0.145)
        fig.text(0.105, 0.955, "Ritmo de gasto", color=t["tinta"],
                 fontsize=13.5, fontweight="bold", va="top", ha="left")
        fig.text(0.105, 0.885, subtitulo, color=t["tinta2"],
                 fontsize=10, va="top", ha="left")
        nota_y = 0.028
    if len(series) > 1:
        fig.text(0.975, nota_y, "cada ciclo cortado al mismo día",
                 color=t["apagado"], fontsize=8.5, va="bottom", ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=t["fondo"])
    plt.close(fig)
    return buf.getvalue()
