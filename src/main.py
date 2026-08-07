"""Orquestador. Se ejecuta en cada corrida de GitHub Actions (cada 5 min).

Flujo:
  1. Lee updates de Telegram: comandos (/cuotas, /eliminar, /resumen,
     /categorias) y toques de botones (cuotas y categorías) y actualiza la Sheet.
  2. Lee correos nuevos del banco (compras, transferencias, ingresos) y los
     registra, avisando por Telegram con botones para clasificar.
  3. Regenera el resumen por ciclo y el gasto por categoría.
"""

import re
import time
from datetime import date, datetime, timedelta
from html import escape as _esc

import gspread
import requests

# `grafico` (y con él matplotlib) se importa dentro de _grafico_ritmo, no acá:
# si falta la dependencia, se pierde el gráfico pero el bot sigue funcionando.
from . import billing, config, email_reader, telegram_bot
from .formato import delta_pct as _delta_pct
from .formato import mes_corto as _mes_corto
from .formato import nombre_ciclo as _nombre_ciclo
from .formato import pesos as _pesos
from .parser import Movimiento, parsear, parece_movimiento
from .sheets import Store


def _bloque_totales(store, ciclo: str) -> str:
    """Bloque de 2 líneas: lo que se paga en la tarjeta y el total del mes."""
    d = store.desglose_de_ciclo(ciclo)
    return (
        f"<b>{_nombre_ciclo(ciclo)}</b>\n"
        f"Tarjeta (pagas el {config.BILLING_DAY}): <b>{_pesos(d['tarjeta'])}</b>\n"
        f"Total del mes: <b>{_pesos(d['total'])}</b>"
    )


# --- Presentación de un movimiento con sus botones ---
def _teclado_movimiento(store, reg) -> dict:
    """Inline keyboard: fila de cuotas (solo crédito) + botones de categoría.
    La opción elegida se marca con '✓'; 'Otra' abre el modo escritura."""
    mov_id = int(reg["id"])
    filas = []
    if reg["tipo"] == "credito":
        actual_n = int(reg.get("num_cuotas") or 1)
        fila = []
        for n in (1, 3, 6, 12):
            txt = f"✓ {n}" if actual_n == n else str(n)
            fila.append({"text": txt, "callback_data": f"q|{mov_id}|{n}"})
        fila.append({"text": "Otra", "callback_data": f"q|{mov_id}|x"})
        filas.append(fila)

    actual_cat = (reg.get("categoria") or "").strip().lower()
    fila = []
    for idx, (_emoji, nombre) in enumerate(store.categorias()):
        marca = "✓ " if nombre.lower() == actual_cat else ""
        fila.append({"text": f"{marca}{nombre}", "callback_data": f"c|{mov_id}|{idx}"})
        if len(fila) == 2:  # 2 por fila para que los nombres se lean bien
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    filas.append([{"text": "Otra categoría", "callback_data": f"c|{mov_id}|x"}])
    return {"inline_keyboard": filas}


def _render_movimiento(store, reg):
    """Devuelve (texto, teclado) de un movimiento a partir de su fila en la Sheet.
    Los ingresos no se categorizan (no llevan botones)."""
    tipo = reg["tipo"]
    comercio = reg["comercio"]
    monto = int(reg["monto"])
    fecha = date.fromisoformat(reg["fecha"][:10])
    mov_id = int(reg["id"])
    # Período a mostrar: para crédito, el ciclo donde empieza a facturar
    # (ciclo_inicio); para el resto, el ciclo de facturación de la fecha. Así el
    # encabezado coincide con el ciclo donde el movimiento realmente cuenta
    # (evita que movimientos viejos muestren el mes calendario stale).
    ciclo = reg["ciclo_inicio"] if reg["tipo"] == "credito" else billing.ciclo_de_compra(fecha)

    # Todo lo que viene del correo (comercio, nombres) se escapa: un '&' o un '<'
    # en un nombre de comercio rompe el parseo HTML de Telegram y el mensaje se
    # descarta entero.
    if tipo == "ingreso":
        cabecera = "<b>Ingreso recibido</b>"
        detalle = f"De: {_esc(comercio.replace('Transferencia de ', ''))}"
    elif tipo == "transferencia":
        cabecera = "<b>Transferencia enviada</b>"
        detalle = f"A: {_esc(comercio.replace('Transferencia a ', ''))}"
    elif tipo == "credito":
        cabecera = "<b>Compra con crédito</b>"
        detalle = f"{_esc(comercio)}   (****{_esc(str(reg.get('digitos', '')))})"
    else:
        cabecera = "<b>Compra con débito</b>"
        detalle = f"{_esc(comercio)}   (****{_esc(str(reg.get('digitos', '')))})"

    # Se categoriza todo lo que cuenta (gastos e ingresos), para que la suma por
    # categoría cuadre con el total del mes. Solo 'revisar' queda afuera.
    categorizable = tipo in ("credito", "debito", "transferencia", "ingreso")
    signo = "+" if tipo == "ingreso" else ""
    lineas = [
        cabecera,
        detalle,
        f"Monto: {signo}{_pesos(abs(monto))}",
        f"Fecha: {fecha.strftime('%d/%m/%Y')}",
        f"ID: <code>{mov_id}</code>",
    ]
    if tipo == "credito":
        lineas.append(f"Cuotas: <b>{int(reg.get('num_cuotas') or 1)}</b>")
    if categorizable:
        cat = (reg.get("categoria") or "").strip()
        lineas.append(f"Categoría: <b>{_esc(cat)}</b>" if cat
                      else "Categoría: <i>sin asignar — elegí abajo</i>")
    lineas.append("")
    lineas.append(_bloque_totales(store, ciclo))

    teclado = _teclado_movimiento(store, reg) if categorizable else None
    return "\n".join(lineas), teclado


def _procesar_callback(store, cq) -> bool:
    """Maneja un toque de botón (cuotas o categoría) y edita el mensaje.
    Devuelve True si cambió datos (para regenerar las hojas una vez al final)."""
    partes = (cq.get("data") or "").split("|")
    if len(partes) != 3:
        telegram_bot.responder_callback(cq["id"])
        return False
    accion, sid, val = partes
    try:
        mov_id = int(sid)
    except ValueError:
        telegram_bot.responder_callback(cq["id"])
        return False
    if not store.obtener_movimiento(mov_id):
        telegram_bot.responder_callback(cq["id"], "No encontré ese movimiento")
        return False

    aviso = ""
    if accion == "q":  # cuotas
        if val == "x":
            store.set_config("pendiente", f"cuo:{mov_id}")
            telegram_bot.responder_callback(cq["id"], "Escribí el número de cuotas")
            telegram_bot.enviar(f"¿En cuántas cuotas quedó <code>{mov_id}</code>? "
                                f"Respondé solo con el número.")
            return False
        try:
            store.actualizar_cuotas(mov_id, int(val))
        except ValueError:
            telegram_bot.responder_callback(cq["id"])
            return False
        aviso = f"{val} cuota(s)"
    elif accion == "c":  # categoría
        if val == "x":
            store.set_config("pendiente", f"cat:{mov_id}")
            telegram_bot.responder_callback(cq["id"], "Escribí la categoría nueva")
            telegram_bot.enviar(f"Escribí el nombre de la nueva categoría para "
                                f"<code>{mov_id}</code>.")
            return False
        cats = store.categorias()
        try:
            idx = int(val)
        except ValueError:
            telegram_bot.responder_callback(cq["id"])
            return False
        if not 0 <= idx < len(cats):
            telegram_bot.responder_callback(cq["id"])
            return False
        store.set_categoria(mov_id, cats[idx][1])
        aviso = cats[idx][1]
    else:
        telegram_bot.responder_callback(cq["id"])
        return False

    reg = store.obtener_movimiento(mov_id)
    texto, teclado = _render_movimiento(store, reg)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if chat_id and message_id:
        telegram_bot.editar(chat_id, message_id, texto, teclado)
    telegram_bot.responder_callback(cq["id"], f"Guardado: {aviso}")
    return True


def _resolver_pendiente(store, mov_id: int, tipo_p: str, texto: str) -> bool:
    """Aplica la respuesta escrita a un botón 'Otra' (categoría o cuotas)."""
    if tipo_p == "cat":
        nombre = store.agregar_categoria(texto)
        store.set_categoria(mov_id, nombre)
        telegram_bot.enviar(f"<code>{mov_id}</code> quedó en <b>{_esc(nombre)}</b>.")
        return True
    if tipo_p == "cuo":
        m = re.search(r"\d+", texto)
        if not m:
            return False
        n = int(m.group())
        store.actualizar_cuotas(mov_id, n)
        telegram_bot.enviar(f"<code>{mov_id}</code> quedó en <b>{n} cuotas</b>.")
        return True
    return False


def _manejar_update(store, up) -> bool:
    """Procesa un update de Telegram (botón, texto o comando).
    Devuelve True si cambió datos (para regenerar las hojas una vez al final)."""
    # Toque de botón (elegir cuotas o categoría).
    cq = up.get("callback_query")
    if cq:
        return _procesar_callback(store, cq)

    msg = up.get("message") or up.get("edited_message")
    if not msg:
        return False
    texto = (msg.get("text") or "").strip()

    # Respuesta escrita a un botón "Otra" (categoría/cuotas nuevas).
    pend = store.get_config("pendiente", "")
    if pend and texto and not texto.startswith("/"):
        tipo_p, _, sid = pend.partition(":")
        mov_id = int(sid) if sid.isdigit() else None
        resuelto = False
        if mov_id is not None and store.obtener_movimiento(mov_id):
            resuelto = _resolver_pendiente(store, mov_id, tipo_p, texto)
        store.set_config("pendiente", "")
        if resuelto:
            return True

    # /cuotas <id> <n>
    m = re.match(r"/cuotas\s+(\d+)\s+(\d+)", texto, re.IGNORECASE)
    if m:
        mov_id, n = int(m.group(1)), int(m.group(2))
        reg = store.actualizar_cuotas(mov_id, n)
        if reg:
            telegram_bot.enviar(
                f"<code>{mov_id}</code> {_esc(reg['comercio'])}: <b>{n} cuotas</b> "
                f"de {_pesos(reg['valor_cuota'])} c/u.\n\n"
                f"{_bloque_totales(store, reg['ciclo_inicio'])}"
            )
            return True
        telegram_bot.enviar(f"No encontré el movimiento <code>{mov_id}</code>.")
        return False

    # /eliminar <id> [monto]   -> devolución total o parcial
    m = re.match(r"/eliminar\s+(\d+)(?:\s+\$?([\d.]+))?", texto, re.IGNORECASE)
    if m:
        mov_id = int(m.group(1))
        if m.group(2):  # devolución parcial
            monto_dev = int(m.group(2).replace(".", ""))
            reg = store.reducir_monto(mov_id, monto_dev)
            if reg and "_devuelto" in reg:
                telegram_bot.enviar(
                    f"Devolución de {_pesos(monto_dev)} en <code>{mov_id}</code> "
                    f"({_esc(reg['comercio'])}). Queda en {_pesos(reg['monto'])}.\n\n"
                    f"{_bloque_totales(store, reg['ciclo_inicio'])}"
                )
                return True
        else:  # devolución total
            reg = store.eliminar_movimiento(mov_id)
        if reg:
            telegram_bot.enviar(
                f"Anulado <code>{mov_id}</code> ({_esc(reg['comercio'])}, "
                f"{_pesos(reg['monto'])}). Ya no cuenta.\n"
                f"¿Fue un error? Recupéralo con <code>/restaurar {mov_id}</code>\n\n"
                f"{_bloque_totales(store, reg['ciclo_inicio'])}"
            )
            return True
        telegram_bot.enviar(f"No encontré el movimiento <code>{mov_id}</code>.")
        return False

    # /restaurar <id>   -> deshace una anulación
    m = re.match(r"/restaurar\s+(\d+)", texto, re.IGNORECASE)
    if m:
        mov_id = int(m.group(1))
        reg = store.restaurar_movimiento(mov_id)
        if reg:
            telegram_bot.enviar(
                f"Restaurado <code>{mov_id}</code> ({_esc(reg['comercio'])}, "
                f"{_pesos(reg['monto'])}). Vuelve a contar.\n\n"
                f"{_bloque_totales(store, reg['ciclo_inicio'])}"
            )
            return True
        telegram_bot.enviar(f"No encontré el movimiento <code>{mov_id}</code>.")
        return False

    # /clasificar [id]  -> reenvía gasto(s) con botones para categorizar
    m = re.match(r"/clasificar(?:\s+(\d+))?", texto, re.IGNORECASE)
    if m:
        if m.group(1):  # un movimiento puntual (aunque ya tenga categoría)
            mov_id = int(m.group(1))
            reg = store.obtener_movimiento(mov_id)
            if reg and reg["tipo"] in ("credito", "debito", "transferencia", "ingreso"):
                txt, tec = _render_movimiento(store, reg)
                telegram_bot.enviar(txt, tec)
            else:
                telegram_bot.enviar(f"<code>{mov_id}</code> no es clasificable.")
        else:  # todos los movimientos sin categoría
            pendientes = store.sin_categoria()
            if not pendientes:
                telegram_bot.enviar("No hay movimientos sin categoría. Todo clasificado.")
            else:
                telegram_bot.enviar(
                    f"Tenés <b>{len(pendientes)}</b> movimiento(s) sin categoría. "
                    f"Elegí la categoría de cada uno:")
                for reg in pendientes[:20]:
                    txt, tec = _render_movimiento(store, reg)
                    telegram_bot.enviar(txt, tec)
                if len(pendientes) > 20:
                    telegram_bot.enviar("… (mostré los primeros 20; repetí /clasificar "
                                        "cuando termines para seguir con el resto)")
        return False

    # /categorias  -> gasto por categoría del ciclo actual
    if re.match(r"/categorias", texto, re.IGNORECASE):
        data = store.regenerar_categorias()  # refresca y da formato a la hoja
        ciclo = billing.ciclo_de_compra(date.today())
        items = sorted(((cat, m2) for (cat, c), m2 in data.items() if c == ciclo),
                       key=lambda x: -x[1])
        if items:
            w_cat = max(len("Categoría"), *(len(i[0]) for i in items))
            w_tot = max(len("Total"), *(len(_pesos(i[1])) for i in items))
            lineas = [f"{'Categoría':<{w_cat}} {'Total':>{w_tot}}"]
            for cat, m2 in items:
                lineas.append(f"{_esc(f'{cat:<{w_cat}}')} {_pesos(m2):>{w_tot}}")
            total = sum(m2 for _, m2 in items)
            lineas.append(f"{'TOTAL':<{w_cat}} {_pesos(total):>{w_tot}}")
            tabla = "<pre>" + "\n".join(lineas) + "</pre>"
            telegram_bot.enviar(
                f"<b>Gasto por categoría — {_nombre_ciclo(ciclo)}</b>\n{tabla}\n"
                f"<i>El detalle mes a mes está en la hoja «Categorías».</i>"
            )
        else:
            telegram_bot.enviar(
                f"Aún no hay gastos categorizados en {_nombre_ciclo(ciclo)}."
            )
        return False

    # /resumen
    if re.match(r"/resumen", texto, re.IGNORECASE):
        t = store.calcular_totales()
        ciclos = sorted(set(t["credito"]) | set(t["otros"]))
        if ciclos:
            filas = []
            for c in ciclos:
                tarjeta = t["credito"].get(c, 0)
                total = tarjeta + t["otros"].get(c, 0)
                anio = c[:4]
                mes = _nombre_ciclo(c).split()[0].capitalize()  # solo el mes
                filas.append((anio, mes, _pesos(tarjeta), _pesos(total)))
            # Ancho de cada columna = el texto más largo (incluido el encabezado).
            w_anio = max(len("Año"), *(len(f[0]) for f in filas))
            w_mes = max(len("Mes"), *(len(f[1]) for f in filas))
            w_tar = max(len("Tarjeta"), *(len(f[2]) for f in filas))
            w_tot = max(len("Total mes"), *(len(f[3]) for f in filas))
            lineas = [f"{'Año':<{w_anio}} {'Mes':<{w_mes}} "
                      f"{'Tarjeta':>{w_tar}} {'Total mes':>{w_tot}}"]
            for anio, mes, tar, tot in filas:
                lineas.append(f"{anio:<{w_anio}} {mes:<{w_mes}} "
                              f"{tar:>{w_tar}} {tot:>{w_tot}}")
            tabla = "<pre>" + "\n".join(lineas) + "</pre>"
            telegram_bot.enviar(
                f"<b>Resumen por mes</b> (tarjeta factura el {config.BILLING_DAY})\n"
                f"{tabla}"
            )
        else:
            telegram_bot.enviar("Aún no hay movimientos registrados.")
        return False

    return False


def procesar_comandos(store: Store):
    offset = int(store.get_config("telegram_offset", "0") or "0")
    updates = telegram_bot.obtener_updates(offset)
    nuevo_offset = offset
    dirty = False  # ¿hubo cambios? Si sí, se regeneran las hojas 1 vez al final.

    for up in updates:
        nuevo_offset = max(nuevo_offset, up["update_id"])
        try:
            if _manejar_update(store, up):
                dirty = True
        except gspread.exceptions.APIError as e:
            # Límite de la API de Sheets: cortamos acá y seguimos en la próxima
            # corrida. El offset ya avanzado evita quedar en un bucle infinito.
            print(f"[aviso] corté por límite de Sheets: {e}")
            break

    if dirty:
        try:
            store.regenerar_resumen()
            store.regenerar_categorias()
        except gspread.exceptions.APIError as e:
            print(f"[aviso] no pude regenerar las hojas ahora: {e}")

    if nuevo_offset != offset:
        store.set_config("telegram_offset", nuevo_offset)


def _de_remitente_conocido(remitente: str) -> bool:
    r = remitente.lower()
    return any(s in r for s in config.BANK_SENDERS)


def _contiene_cuenta(cuerpo: str) -> bool:
    """True si el cuerpo contiene TU número de cuenta (solo dígitos).

    Se prueba con y sin los ceros de la izquierda: el mismo número aparece como
    '0-080-05-79300-5' en un banco y como '8005793005' en otro, y con una sola
    forma el correo del segundo se descartaba antes de llegar al parser."""
    if not config.DEST_ACCOUNT:
        return False
    digitos = re.sub(r"\D", "", re.sub(r"<[^>]+>", " ", cuerpo))
    if config.DEST_ACCOUNT in digitos:
        return True
    sin_ceros = config.DEST_ACCOUNT.lstrip("0")
    # Menos de 8 dígitos es demasiado corto: daría falsos positivos con montos.
    return len(sin_ceros) >= 8 and sin_ceros in digitos


def procesar_correos(store: Store):
    vistos = store.message_ids()
    nuevos = 0
    # Contadores de diagnóstico: sin esto, un correo que el parser no entiende
    # se descarta sin dejar rastro y no hay forma de saber dónde se perdió.
    # Van como números a propósito: el repo es público y los logs también.
    stats = {"total": 0, "ya_vistos": 0, "ajenos": 0, "sin_parsear": 0}
    for message_id, asunto, remitente, cuerpo in email_reader.obtener_correos():
        stats["total"] += 1
        if message_id in vistos:
            stats["ya_vistos"] += 1
            continue

        # Filtro clave: si el correo NO es de un remitente conocido y NO contiene
        # tu número de cuenta, se ignora (evita procesar correos ajenos).
        conocido = _de_remitente_conocido(remitente)
        tiene_cuenta = _contiene_cuenta(cuerpo)
        if not conocido and not tiene_cuenta:
            stats["ajenos"] += 1
            continue

        mov = parsear(asunto, cuerpo, uid=message_id)
        if not mov:
            stats["sin_parsear"] += 1
            if config.DEBUG_CORREOS:
                # Solo bajo pedido: el asunto puede traer comercio y monto, y el
                # log de un repo público lo ve cualquiera.
                print(f"[debug] no supe leer: remitente={remitente!r} asunto={asunto!r}")
            # Red de seguridad: avisamos de CUALQUIER correo que mueva plata y no
            # hayamos sabido leer. Antes esto exigía parece_ingreso(), o sea que
            # dependía del mismo detector que había fallado — cuando un banco
            # cambiaba la redacción, el movimiento se perdía sin dejar rastro.
            if parece_movimiento(asunto, cuerpo):
                rev = Movimiento(fecha=date.today(), comercio=f"REVISAR: {asunto[:40]}",
                                 monto=0, digitos="", tipo="revisar", uid=message_id)
                rid = store.agregar_movimiento(rev)
                telegram_bot.enviar(
                    f"Recibí un correo que <b>mueve plata</b> pero no supe leerlo:\n"
                    f"\"{_esc(asunto)}\"\n"
                    f"Lo dejé como id <code>{rid}</code> (tipo 'revisar') en la planilla "
                    f"para que lo completes, o reenvíame ese correo y agrego su formato."
                )
                vistos.add(message_id)
                nuevos += 1
            continue
        if mov.digitos in config.CARD_MAP:
            mov.tipo = config.CARD_MAP[mov.digitos]

        mov_id = store.agregar_movimiento(mov, num_cuotas=1)
        reg = store.obtener_movimiento(mov_id)
        texto, teclado = _render_movimiento(store, reg)
        telegram_bot.enviar(texto, teclado)
        vistos.add(message_id)
        nuevos += 1
    print(f"[correos] revisados={stats['total']} ya_vistos={stats['ya_vistos']} "
          f"ajenos={stats['ajenos']} sin_parsear={stats['sin_parsear']} nuevos={nuevos}")
    return nuevos


def _ahora_local():
    """Fecha/hora actual en la zona configurada (cae a UTC si no hay tzdata)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(config.TIMEZONE))
    except Exception:
        return datetime.now()


def _barras(items, ancho: int = 10):
    """Barras de texto monoespaciadas. `items` = lista de (etiqueta, valor).
    Devuelve un bloque <pre> (escalado al mayor) o None si está vacío."""
    if not items:
        return None
    maximo = max((v for _, v in items), default=0)
    w_lbl = min(14, max(len(str(l)) for l, _ in items))
    w_amt = max(len(_pesos(v)) for _, v in items)
    filas = []
    for l, v in items:
        n = max(1, round(v / maximo * ancho)) if (maximo > 0 and v > 0) else 0
        barra = ("█" * n).ljust(ancho)
        # El relleno se calcula con el texto crudo y se escapa después, para que
        # una categoría con '&' no rompa el bloque <pre> entero.
        filas.append(f"{_esc(str(l)[:w_lbl].ljust(w_lbl))} {barra} {_pesos(v):>{w_amt}}")
    return "<pre>" + "\n".join(filas) + "</pre>"


def _grafico_ritmo(store, ciclos, dias_ciclo: int, transcurridos: int,
                   proyeccion: int | None):
    """PNG del gasto acumulado del ciclo vs los anteriores, o None si no se pudo.

    `ciclos` es [(etiqueta, inicio, corte, total)] con el ciclo en curso primero.
    matplotlib se importa acá adentro a propósito: si falta la dependencia o el
    dibujo falla, se pierde el gráfico pero el resumen de texto igual sale.
    """
    try:
        from . import grafico

        dias = transcurridos + 1
        series = []
        for lbl, ini, corte, total in ciclos:
            if series and not total:
                continue    # un ciclo anterior sin movimientos es una línea en cero
            series.append({
                "etiqueta": _mes_corto(lbl),
                "valores": grafico.acumular(store.gasto_diario(ini, corte), ini, dias),
            })
        if not any(s["valores"][-1] for s in series):
            return None     # todavía no hay nada que dibujar
        return grafico.ritmo_de_gasto(
            series, dias_ciclo,
            f"Ciclo {ciclos[0][0]} · día {dias} de {dias_ciclo}",
            proyeccion=proyeccion, tema=config.GRAFICO_TEMA)
    except Exception as e:
        print(f"[aviso] no pude generar el gráfico del resumen: {e}")
        return None


def _ventanas_de_ciclo(hoy: date):
    """Ventanas del ciclo en curso y los DOS anteriores, cortadas al mismo día.

    Devuelve (dias_ciclo, transcurridos, [(etiqueta, inicio, corte), ...]) con el
    ciclo en curso primero. Lo usan el resumen nocturno y el panel fijado, para
    que no puedan discrepar en los cortes."""
    ini_act = billing.inicio_de_ciclo(hoy)
    prox = billing.proximo_inicio_de_ciclo(hoy)
    dias_ciclo = (prox - ini_act).days
    transcurridos = (hoy - ini_act).days                       # 0 el primer día

    ini_ant1 = billing.inicio_de_ciclo(ini_act - timedelta(days=1))
    ini_ant2 = billing.inicio_de_ciclo(ini_ant1 - timedelta(days=1))
    # Misma altura del ciclo, sin pasarse: un ciclo anterior puede ser más corto
    # que el actual (28 vs 31 días), y sin el tope la ventana se metería en el
    # ciclo siguiente y contaría movimientos que no le corresponden.
    corte_ant1 = min(ini_ant1 + timedelta(days=transcurridos), ini_act - timedelta(days=1))
    corte_ant2 = min(ini_ant2 + timedelta(days=transcurridos), ini_ant1 - timedelta(days=1))

    ciclos = [
        (_nombre_ciclo(billing.ciclo_de_compra(hoy)), ini_act, hoy),
        (_nombre_ciclo(billing.ciclo_de_compra(ini_ant1)), ini_ant1, corte_ant1),
        (_nombre_ciclo(billing.ciclo_de_compra(ini_ant2)), ini_ant2, corte_ant2),
    ]
    return dias_ciclo, transcurridos, ciclos


def _resumen_nocturno(store, hoy: date):
    """Arma y envía el resumen del día: un gráfico del gasto acumulado del ciclo
    contra los DOS ciclos anteriores a la misma altura, más las categorías que
    más crecieron y una proyección vs el promedio.

    El gráfico va como foto y el texto como caption. Si el gráfico no se pudo
    generar o Telegram lo rechaza, cae al resumen de solo texto con las barras
    ASCII de siempre — el resumen nunca se pierde por un problema de imagen."""
    dias_ciclo, transcurridos, ciclos = _ventanas_de_ciclo(hoy)
    ((ciclo_lbl, ini_act, _h), (lbl_ant1, ini_ant1, corte_ant1),
     (lbl_ant2, ini_ant2, corte_ant2)) = ciclos

    total_act, cat_act = store.gasto_neto_por_fecha(ini_act, hoy)
    total_ant1, cat_ant1 = store.gasto_neto_por_fecha(ini_ant1, corte_ant1)
    total_ant2, _c2 = store.gasto_neto_por_fecha(ini_ant2, corte_ant2)

    promedio = store.promedio_ciclos(ini_act, n=3)
    # Proyección de cierre: recién desde el 4º día. Con uno o dos días de datos
    # el ×31 dispara la escala del gráfico y no dice nada del mes.
    proyeccion = (total_act * dias_ciclo // (transcurridos + 1)
                  if total_act > 0 and 3 <= transcurridos < dias_ciclo - 1 else None)

    png = _grafico_ritmo(store,
                         [(ciclo_lbl, ini_act, hoy, total_act),
                          (lbl_ant1, ini_ant1, corte_ant1, total_ant1),
                          (lbl_ant2, ini_ant2, corte_ant2, total_ant2)],
                         dias_ciclo, transcurridos, proyeccion)

    lineas = [f"<b>Resumen del día · {hoy.strftime('%d/%m/%Y')}</b>", ""]
    lineas.append(f"<b>Ciclo {ciclo_lbl}</b> · día {transcurridos + 1} de {dias_ciclo}")
    lineas.append(f"Gastado hasta hoy: <b>{_pesos(total_act)}</b>")

    # 1) Comparación con los dos ciclos anteriores a la misma altura.
    #    Si hay gráfico, él muestra las curvas y acá van solo los porcentajes;
    #    si no, las barras de texto siguen contando la comparación.
    partes = []
    if total_ant1:
        partes.append(f"{_delta_pct(total_act, total_ant1):+d}% vs {_mes_corto(lbl_ant1)}")
    if total_ant2:
        partes.append(f"{_delta_pct(total_act, total_ant2):+d}% vs {_mes_corto(lbl_ant2)}")

    if png is None:
        comp = [(_mes_corto(ciclo_lbl), total_act),
                (_mes_corto(lbl_ant1), total_ant1),
                (_mes_corto(lbl_ant2), total_ant2)]
        barras_comp = _barras(comp)
        if barras_comp:
            lineas.append("")
            lineas.append("<b>Comparación a esta altura</b>")
            lineas.append(barras_comp)
    if partes:
        lineas.append(" · ".join(partes) + " (a esta altura del ciclo)")

    # 2) Drivers: categorías que más crecieron vs el ciclo anterior -> BARRAS
    #    (a nivel categoría, no repite la comparación de ciclos).
    cats = set(cat_act) | set(cat_ant1)
    crecimiento = sorted(((c, cat_act.get(c, 0) - cat_ant1.get(c, 0)) for c in cats),
                         key=lambda x: -x[1])
    top = [(c, d) for c, d in crecimiento if d > 0][:5]
    barras_top = _barras(top)
    if barras_top:
        lineas.append("")
        lineas.append(f"<b>Categorías que más crecieron vs {_mes_corto(lbl_ant1)}</b>")
        lineas.append(barras_top)

    # 3) Proyección del ciclo vs promedio.
    if promedio and proyeccion:
        estado = "por encima del promedio" if proyeccion > promedio else "dentro del promedio"
        lineas.append("")
        lineas.append(f"<b>Proyección de cierre:</b> {_pesos(proyeccion)}")
        lineas.append(f"<b>Promedio de ciclos:</b> {_pesos(promedio)}")
        lineas.append(f"Estado: {estado}")

    texto = "\n".join(lineas)
    if png is None:
        telegram_bot.enviar(texto)
        return
    # El caption de Telegram admite 1024 caracteres; si no entra, el texto va aparte.
    caption = texto if len(texto) <= 1000 else ""
    if not telegram_bot.enviar_foto(png, caption):
        telegram_bot.enviar(texto)      # Telegram rechazó la foto: al menos el texto
    elif not caption:
        telegram_bot.enviar(texto)


def _panel_al_dia(store, ahora, hubo_cambios: bool):
    """Mantiene UN mensaje fijado en el chat con el gráfico del ciclo al día.

    En vez de mandar una foto nueva cada vez (que se entierra en el historial),
    edita siempre el mismo mensaje: queda fijado arriba del chat, siempre
    actualizado. Editar no genera notificación, así que refrescarlo no molesta.
    El id vive en la hoja Config ('panel_message_id').

    Solo se refresca si hubo un movimiento nuevo o si pasaron PANEL_MINUTOS:
    regenerar la imagen en cada corrida (una por minuto) sería puro gasto.
    """
    if not config.PANEL_ACTIVO:
        return

    # La marca se guarda naive: entre corridas puede faltar tzdata y mezclar
    # aware con naive haría reventar la resta.
    ahora_naive = ahora.replace(tzinfo=None)
    # str() a propósito: la hoja puede devolver el valor como número o como
    # fecha ya interpretada, no siempre como el texto que se guardó.
    ultimo = str(store.get_config("panel_actualizado", "") or "")
    if not hubo_cambios and ultimo:
        try:
            minutos = (ahora_naive - datetime.fromisoformat(ultimo)).total_seconds() / 60
            if 0 <= minutos < config.PANEL_MINUTOS:
                return
        except (ValueError, TypeError):
            pass    # marca ilegible: se regenera y se reescribe

    hoy = ahora.date()
    dias_ciclo, transcurridos, ciclos = _ventanas_de_ciclo(hoy)
    totales = [store.gasto_neto_por_fecha(ini, corte)[0] for _lbl, ini, corte in ciclos]
    total_act = totales[0]

    proyeccion = (total_act * dias_ciclo // (transcurridos + 1)
                  if total_act > 0 and 3 <= transcurridos < dias_ciclo - 1 else None)
    png = _grafico_ritmo(store,
                         [(lbl, ini, corte, tot)
                          for (lbl, ini, corte), tot in zip(ciclos, totales)],
                         dias_ciclo, transcurridos, proyeccion)
    if png is None:
        return

    ciclo_lbl, lbl_ant1, lbl_ant2 = (c[0] for c in ciclos)
    partes = []
    if totales[1]:
        partes.append(f"{_delta_pct(total_act, totales[1]):+d}% vs {_mes_corto(lbl_ant1)}")
    if totales[2]:
        partes.append(f"{_delta_pct(total_act, totales[2]):+d}% vs {_mes_corto(lbl_ant2)}")

    lineas = [f"<b>Ciclo {ciclo_lbl}</b> · día {transcurridos + 1} de {dias_ciclo}",
              f"Gastado: <b>{_pesos(total_act)}</b>"]
    if partes:
        lineas.append(" · ".join(partes) + " (a esta altura)")
    if proyeccion:
        lineas.append(f"Proyección de cierre: {_pesos(proyeccion)}")
    lineas.append(f"<i>Actualizado {ahora.strftime('%d/%m %H:%M')}</i>")
    caption = "\n".join(lineas)

    mid = str(store.get_config("panel_message_id", "") or "").strip()
    if mid.isdigit() and telegram_bot.editar_foto(int(mid), png, caption):
        store.set_config("panel_actualizado", ahora_naive.isoformat(timespec="seconds"))
        return

    # No había panel, o el mensaje ya no existe (lo borraste): se crea y se fija.
    msg = telegram_bot.enviar_foto(png, caption)
    if not msg:
        return
    store.set_config("panel_message_id", str(msg["message_id"]))
    store.set_config("panel_actualizado", ahora_naive.isoformat(timespec="seconds"))
    telegram_bot.fijar(msg["message_id"])


def _quizas_resumen_nocturno(store):
    """Envía el resumen una vez al día, a partir de RESUMEN_HORA (hora local).

    Con FORZAR_RESUMEN (input 'forzar_resumen' del workflow) sale igual, sin
    importar la hora, y sin marcar el día como enviado: es una vista previa, así
    que el resumen real de la noche igual va a salir a su hora."""
    ahora = _ahora_local()
    hoy_iso = ahora.date().isoformat()
    forzado = config.FORZAR_RESUMEN
    if not forzado:
        if ahora.hour < config.RESUMEN_HORA:
            return
        if store.get_config("ultimo_resumen") == hoy_iso:
            return  # ya se envió hoy
    try:
        _resumen_nocturno(store, ahora.date())
        if not forzado:
            store.set_config("ultimo_resumen", hoy_iso)
    except Exception as e:
        print(f"[aviso] resumen nocturno: {e}")


def _crear_store(intentos: int = 3):
    """Abre la planilla con reintentos ante errores transitorios de Google:
    - APIError 429/500/502/503/504.
    - Errores de red o respuestas no-JSON (5xx que devuelven HTML, que hacen
      reventar a gspread con JSONDecodeError al construir el APIError).
    Es seguro reintentar: aún no se procesó nada."""
    for intento in range(1, intentos + 1):
        try:
            return Store()
        except gspread.exceptions.APIError as e:
            codigo = getattr(getattr(e, "response", None), "status_code", None)
            if intento == intentos or codigo not in (429, 500, 502, 503, 504):
                raise
            print(f"[aviso] Sheets respondió {codigo}; reintento {intento}/{intentos - 1}")
            time.sleep(3 * intento)
        except requests.exceptions.RequestException as e:
            # Red caída o respuesta de error sin JSON: transitorio, reintentar.
            if intento == intentos:
                raise
            print(f"[aviso] Sheets respondió mal ({type(e).__name__}); "
                  f"reintento {intento}/{intentos - 1}")
            time.sleep(3 * intento)


def main():
    store = _crear_store()
    procesar_comandos(store)
    nuevos = procesar_correos(store)
    if nuevos:
        try:
            store.regenerar_resumen()
            store.regenerar_categorias()
        except gspread.exceptions.APIError as e:
            print(f"[aviso] no pude regenerar las hojas ahora: {e}")
    _quizas_resumen_nocturno(store)
    try:
        _panel_al_dia(store, _ahora_local(), hubo_cambios=bool(nuevos))
    except Exception as e:
        # El panel es un extra: si falla, no puede tumbar la corrida que ya
        # registró los movimientos.
        print(f"[aviso] no pude actualizar el panel: {e}")
    print(f"Listo. {nuevos} movimiento(s) nuevo(s).")


if __name__ == "__main__":
    main()
