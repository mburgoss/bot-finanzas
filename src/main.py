"""Orquestador. Se ejecuta en cada corrida de GitHub Actions (cada 5 min).

Flujo:
  1. Lee updates de Telegram: comandos (/cuotas, /eliminar, /resumen,
     /categorias) y toques de botones (cuotas y categorías) y actualiza la Sheet.
  2. Lee correos nuevos del banco (compras, transferencias, ingresos) y los
     registra, avisando por Telegram con botones para clasificar.
  3. Regenera el resumen por ciclo y el gasto por categoría.
"""

import calendar
import hashlib
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
from .parser import Movimiento, monto_probable, parsear, parece_movimiento
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

    # Crédito o débito. Va primero porque en un movimiento 'revisar' es el dato
    # que falta para que empiece a contar; en uno normal sirve para corregir un
    # tipo mal detectado. No aplica a ingresos ni transferencias.
    if reg["tipo"] in ("credito", "debito", "revisar"):
        filas.append([
            {"text": ("✓ Crédito" if reg["tipo"] == "credito" else "Crédito"),
             "callback_data": f"t|{mov_id}|credito"},
            {"text": ("✓ Débito" if reg["tipo"] == "debito" else "Débito"),
             "callback_data": f"t|{mov_id}|debito"},
        ])

    if reg["tipo"] == "revisar":
        # El monto es una lectura aproximada: hay que poder arreglarlo sin salir
        # de Telegram, que es justo lo que el aviso viejo no permitía.
        filas.append([{"text": "✏️ Corregir monto",
                       "callback_data": f"m|{mov_id}|x"}])

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

    # Toggle de anulación, solo en su fila al final: es la acción destructiva y no
    # tiene que quedar pegada a los botones de categoría. El callback lleva el
    # estado DESEADO (1 = anular, 0 = restaurar), no una orden de invertir, así
    # que dos toques seguidos no se pisan entre sí.
    anulado = _esta_anulado(reg)
    filas.append([{
        "text": "✓ Eliminado · deshacer" if anulado else "🗑 Eliminar",
        "callback_data": f"b|{mov_id}|{0 if anulado else 1}",
    }])
    return {"inline_keyboard": filas}


def _esta_anulado(reg) -> bool:
    return str(reg.get("estado") or "").lower() == "anulado"


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
    if tipo == "revisar":
        cabecera = "⚠️ <b>No supe leer este correo</b>"
        detalle = f"{_esc(comercio.replace('REVISAR: ', ''))}"
    elif tipo == "ingreso":
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
    # categoría cuadre con el total del mes. 'revisar' también lleva teclado:
    # es justamente el que hay que completar a mano.
    categorizable = tipo in ("credito", "debito", "transferencia", "ingreso", "revisar")
    signo = "+" if tipo == "ingreso" else ""
    anulado = _esta_anulado(reg)
    # Tachado cuando está anulado: el monto sigue a la vista (para saber qué se
    # eliminó) pero se lee de una que ya no suma.
    monto_txt = f"{signo}{_pesos(abs(monto))}"
    lineas = [
        cabecera,
        detalle,
        f"Monto: <s>{monto_txt}</s>" if anulado else f"Monto: {monto_txt}",
        f"Fecha: {fecha.strftime('%d/%m/%Y')}",
        f"ID: <code>{mov_id}</code>",
    ]
    if tipo == "revisar":
        lineas[2] += "  <i>(aproximado)</i>"
        lineas.append("Tipo: <i>sin definir — elegí crédito o débito abajo</i>")
    if anulado and tipo == "revisar":
        lineas.append("<b>Eliminado</b> — completá el tipo y sacalo de eliminado "
                      "para que cuente")
    elif anulado:
        lineas.append("<b>Eliminado</b> — no cuenta en los totales")
    if str(reg.get("message_id") or "").startswith("rec:"):
        lineas.append("🔁 <i>Cargo recurrente</i>")
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
    elif accion == "t":  # crédito / débito
        if not store.actualizar_tipo(mov_id, val):
            telegram_bot.responder_callback(cq["id"], "No pude cambiar el tipo")
            return False
        aviso = "Crédito" if val == "credito" else "Débito"
    elif accion == "m":  # corregir monto — se responde escribiendo el número
        store.set_config("pendiente", f"mon:{mov_id}")
        telegram_bot.responder_callback(cq["id"], "Escribí el monto correcto")
        telegram_bot.enviar(f"¿Cuál es el monto de <code>{mov_id}</code>? "
                            f"Respondé solo con el número.")
        return False
    elif accion == "b":  # eliminar / restaurar — mismo efecto que /eliminar y /restaurar
        if val == "1":
            cambiado = store.eliminar_movimiento(mov_id)
            aviso = "Eliminado, ya no cuenta"
        else:
            cambiado = store.restaurar_movimiento(mov_id)
            aviso = "Restaurado, vuelve a contar"
        if not cambiado:
            telegram_bot.responder_callback(cq["id"], "No pude cambiarlo")
            return False
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
    if tipo_p == "mon":
        # Se aceptan "12.900", "12900" y "$12.900": los puntos son separador de
        # miles en Chile, así que se sacan antes de convertir.
        m = re.search(r"\d[\d.]*", texto.replace(" ", ""))
        if not m:
            return False
        monto = int(m.group().replace(".", ""))
        if not store.actualizar_monto(mov_id, monto):
            return False
        telegram_bot.enviar(f"<code>{mov_id}</code> quedó en <b>{_pesos(monto)}</b>.")
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
            # 'revisar' incluido a propósito: es el que más hace falta poder
            # reabrir por id si se perdió el mensaje original en el chat.
            if reg and reg["tipo"] in ("credito", "debito", "transferencia",
                                       "ingreso", "revisar"):
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


def procesar_comandos(store: Store) -> int:
    """Procesa comandos y toques de botones. Devuelve cuántos MENSAJES escribió
    el usuario: son mensajes que aparecen en el chat y entierran el panel. Los
    toques de botón no cuentan — no dejan mensaje visible."""
    offset = int(store.get_config("telegram_offset", "0") or "0")
    updates = telegram_bot.obtener_updates(offset)
    nuevo_offset = offset
    dirty = False  # ¿hubo cambios? Si sí, se regeneran las hojas 1 vez al final.
    del_usuario = 0

    for up in updates:
        nuevo_offset = max(nuevo_offset, up["update_id"])
        if up.get("message") or up.get("edited_message"):
            del_usuario += 1
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
    return del_usuario


def _fecha_en_ciclo(inicio: date, fin: date, dia: int) -> date:
    """La fecha del ciclo [inicio, fin] que cae en el día del mes `dia`.

    Un ciclo cruza dos meses (del 22 al 21): los días desde el de facturación en
    adelante caen en el primer mes y el resto en el segundo. Si ese día no existe
    en el mes que le toca (31 en febrero), se usa el último día de ese mes."""
    base = inicio if dia >= inicio.day else fin
    ultimo = calendar.monthrange(base.year, base.month)[1]
    d = date(base.year, base.month, min(dia, ultimo))
    return min(max(d, inicio), fin)


def procesar_recurrentes(store, hoy: date) -> int:
    """Registra los cargos fijos del ciclo que ya vencieron y todavía no están.

    Cada uno se identifica con un uid 'rec:<nombre>:<ciclo>' guardado en la misma
    columna message_id que usan los correos, así el bot puede correr mil veces al
    día sin duplicar nada.

    Los que piden confirmación nacen anulados: llegan con los mismos botones que
    cualquier movimiento, corregís el monto si cambió y los sacás de eliminado.
    """
    try:
        recurrentes = store.recurrentes()
    except Exception as e:
        # La hoja la edita una persona; que esté rara no puede tumbar la corrida.
        print(f"[aviso] no pude leer los recurrentes: {e}")
        return 0
    if not recurrentes:
        return 0

    inicio = billing.inicio_de_ciclo(hoy)
    fin = billing.proximo_inicio_de_ciclo(hoy) - timedelta(days=1)
    ciclo = billing.ciclo_de_compra(hoy)
    vistos = store.message_ids()
    creados = 0

    for rec in recurrentes:
        uid = f"rec:{rec['nombre'].strip().lower()}:{ciclo}"
        if uid in vistos:
            continue
        fecha = _fecha_en_ciclo(inicio, fin, rec["dia"])
        if fecha > hoy:
            continue        # en este ciclo todavía no le toca
        mov = Movimiento(fecha=fecha, comercio=rec["nombre"], monto=rec["monto"],
                         digitos="", tipo=rec["tipo"], uid=uid)
        mov_id = store.agregar_movimiento(mov)
        if rec["categoria"]:
            store.set_categoria(mov_id, rec["categoria"])
        if rec["confirmar"]:
            store.eliminar_movimiento(mov_id)
        reg = store.obtener_movimiento(mov_id)
        if reg:
            texto, teclado = _render_movimiento(store, reg)
            telegram_bot.enviar(texto, teclado)
        creados += 1
    if creados:
        print(f"[recurrentes] {creados} cargo(s) fijo(s) del ciclo {ciclo}")
    return creados


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
                # Se registra como un movimiento de verdad, con su mejor lectura
                # del monto, y llega con los mismos botones que los demás. Nace
                # ANULADO porque el monto es aproximado y falta el tipo: así no
                # ensucia los totales hasta que Matías lo complete. El aviso que
                # había antes pedía reenviar el correo, algo que no se puede
                # hacer desde Telegram — era un callejón sin salida.
                rev = Movimiento(fecha=date.today(),
                                 comercio=f"REVISAR: {asunto[:40]}",
                                 monto=monto_probable(asunto, cuerpo) or 0,
                                 digitos="", tipo="revisar", uid=message_id)
                rid = store.agregar_movimiento(rev)
                store.eliminar_movimiento(rid)
                reg = store.obtener_movimiento(rid)
                if reg:
                    texto, teclado = _render_movimiento(store, reg)
                    telegram_bot.enviar(texto, teclado)
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


def _barras(items, ancho: int = 10, con_signo: bool = False):
    """Barras de texto monoespaciadas. `items` = lista de (etiqueta, valor).
    Devuelve un bloque <pre> (escalado al mayor) o None si está vacío.

    `con_signo` antepone '+' a los montos: se usa cuando los valores son
    variaciones y no gastos, para que no se confundan con los de la dona."""
    if not items:
        return None
    signo = "+" if con_signo else ""
    maximo = max((v for _, v in items), default=0)
    w_lbl = min(14, max(len(str(l)) for l, _ in items))
    w_amt = max(len(signo + _pesos(v)) for _, v in items)
    filas = []
    for l, v in items:
        n = max(1, round(v / maximo * ancho)) if (maximo > 0 and v > 0) else 0
        barra = ("█" * n).ljust(ancho)
        # El relleno se calcula con el texto crudo y se escapa después, para que
        # una categoría con '&' no rompa el bloque <pre> entero.
        filas.append(f"{_esc(str(l)[:w_lbl].ljust(w_lbl))} {barra} "
                     f"{signo + _pesos(v):>{w_amt}}")
    return "<pre>" + "\n".join(filas) + "</pre>"


def _grafico_ritmo(store, ciclos, dias_ciclo: int, transcurridos: int,
                   proyeccion: int | None, bloques=None, nota=None, torta=None):
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
            # Un ciclo anterior se omite solo si NO tuvo NINGÚN movimiento en todo
            # el ciclo — o sea, es anterior a que existiera el bot. Antes se
            # miraba el total RECORTADO y eso hacía desaparecer el mes pasado los
            # primeros días: con una ventana de un solo día, un ciclo con gastos
            # reales daba 0 y se caía del gráfico, dejando a la vista uno más
            # viejo que por casualidad sí había gastado ese día.
            if series and not _hubo_movimientos(store, ini):
                continue
            series.append({
                "etiqueta": _mes_corto(lbl),
                "valores": grafico.acumular(store.gasto_diario(ini, corte), ini, dias),
            })
        if not any(s["valores"][-1] for s in series):
            return None     # todavía no hay nada que dibujar
        return grafico.ritmo_de_gasto(
            series, dias_ciclo,
            f"Ciclo {ciclos[0][0]} · día {dias} de {dias_ciclo}",
            proyeccion=proyeccion, tema=config.GRAFICO_TEMA, bloques=bloques,
            nota=nota, torta=torta)
    except Exception as e:
        print(f"[aviso] no pude generar el gráfico del resumen: {e}")
        return None


def _hubo_movimientos(store, inicio_ciclo) -> bool:
    """True si ese ciclo tuvo movimientos en ALGÚN momento, mirando el ciclo
    completo y no la ventana recortada a la altura de hoy."""
    fin = billing.proximo_inicio_de_ciclo(inicio_ciclo) - timedelta(days=1)
    return bool(store.gasto_neto_por_fecha(inicio_ciclo, fin)[0])


def _el_panel_bajara() -> bool:
    """True si el panel se va a reacomodar al final del chat en esta corrida.

    Lo consulta el resumen nocturno: cuando el panel baja igual, mandar la foto
    del resumen dejaría dos gráficos idénticos pegados. El mensaje de texto que
    manda el resumen cuenta como uno más, así que con PANEL_MOVER_TRAS=1 el
    panel baja seguro y queda justo debajo."""
    return bool(config.PANEL_ACTIVO and config.PANEL_MOVER_TRAS)


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

    # La MISMA imagen del panel fijado, con tablas y dona: una sola función la
    # arma, así no pueden divergir.
    n = _numeros_del_ciclo(store, ciclos)
    png, _tot = _imagen_del_ciclo(store, ciclos, dias_ciclo, transcurridos, n)

    lineas = [f"<b>Resumen del día · {hoy.strftime('%d/%m/%Y')}</b>", ""]

    if png is None:
        # Sin imagen, el texto tiene que contar la comparación por su cuenta.
        lineas.append(f"<b>Ciclo {ciclo_lbl}</b> · día {transcurridos + 1} de {dias_ciclo}")
        lineas.append(f"Gastado hasta hoy: <b>{_pesos(total_act)}</b>")
        comp = [(_mes_corto(ciclo_lbl), total_act),
                (_mes_corto(lbl_ant1), total_ant1),
                (_mes_corto(lbl_ant2), total_ant2)]
        barras_comp = _barras(comp)
        if barras_comp:
            lineas.append("")
            lineas.append("<b>Comparación a esta altura</b>")
            lineas.append(barras_comp)
        partes = []
        if total_ant1:
            partes.append(f"{_delta_pct(total_act, total_ant1):+d}% vs {_mes_corto(lbl_ant1)}")
        if total_ant2:
            partes.append(f"{_delta_pct(total_act, total_ant2):+d}% vs {_mes_corto(lbl_ant2)}")
        if partes:
            lineas.append(" · ".join(partes) + " (a esta altura del ciclo)")

    # 2) Drivers: categorías que más CRECIERON vs el ciclo anterior. Esto no lo
    #    dice la imagen: la dona muestra en qué se fue la plata este ciclo, no
    #    qué cambió respecto del anterior. Es lo propio del resumen nocturno.
    cats = set(cat_act) | set(cat_ant1)
    crecimiento = sorted(((c, cat_act.get(c, 0) - cat_ant1.get(c, 0)) for c in cats),
                         key=lambda x: -x[1])
    top = [(c, d) for c, d in crecimiento if d > 0][:5]
    barras_top = _barras(top, con_signo=True)
    if barras_top:
        lineas.append("")
        # "Subió" y con el signo + delante del monto: la dona de la imagen lista
        # las mismas categorías con su GASTO, y sin marcar que acá va un delta,
        # los dos números se leían como si fueran lo mismo.
        lineas.append(f"<b>Lo que más subió vs {_mes_corto(lbl_ant1)}</b>")
        lineas.append(barras_top)

    # 3) Cierre proyectado contra el promedio. La proyección sola ya está
    #    dibujada en la imagen; lo que no está —y es el punto— es contra qué
    #    referencia se compara, así que va en una sola frase.
    if promedio and proyeccion:
        estado = ("<b>por encima</b> de tu promedio" if proyeccion > promedio
                  else "dentro de tu promedio")
        lineas.append("")
        lineas.append(f"A este ritmo cerrás en <b>{_pesos(proyeccion)}</b>, "
                      f"{estado} de {_pesos(promedio)}.")

    # Colapsa líneas en blanco de más: cuando hay imagen se saltean bloques
    # enteros y quedaban dos vacías seguidas bajo el título.
    texto = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    if png is None or _el_panel_bajara():
        # Si el panel va a bajar al final del chat en esta misma corrida, mandar
        # acá la foto dejaría DOS gráficos idénticos pegados. El texto notifica y
        # la imagen la pone el panel, justo debajo.
        telegram_bot.enviar(texto)
        return
    # El caption de Telegram admite 1024 caracteres; si no entra, el texto va aparte.
    caption = texto if len(texto) <= 1000 else ""
    if not telegram_bot.enviar_foto(png, caption):
        telegram_bot.enviar(texto)      # Telegram rechazó la foto: al menos el texto
    elif not caption:
        telegram_bot.enviar(texto)


def _tabla(filas) -> str:
    """Bloque <pre> de 'etiqueta ....... $monto', alineado a la derecha.

    `filas` = [(etiqueta, monto)]. La etiqueta que arranca con '=' es el total y
    se marca con ✓, para que se vea de una que las de arriba suman esa.
    Los negativos llevan el signo menos tipográfico, que en monoespaciado no se
    confunde con un guión."""
    textos = [("−" + _pesos(abs(m))) if m < 0 else _pesos(m) for _e, m in filas]
    w_lbl = max(len(e) for e, _m in filas)
    w_amt = max(len(t) for t in textos)
    lineas = []
    for (etiqueta, _m), texto in zip(filas, textos):
        marca = " ✓" if etiqueta.startswith("=") else ""
        lineas.append(f"{etiqueta.ljust(w_lbl)}  {texto:>{w_amt}}{marca}")
    return "<pre>" + "\n".join(lineas) + "</pre>"


MAX_PORCIONES = 5       # top N categorías; el resto se junta en "Otras"


def _numeros_del_ciclo(store, ciclos) -> dict:
    """Todos los números del ciclo en una pasada, para que la imagen y el texto
    no puedan discrepar."""
    _lbl, ini_act, hoy = ciclos[0]
    g = store.desglose_de_gasto(ini_act, hoy)
    otros = g["debito"] + g["transferencia"] + g["ingreso"]   # ingreso ya es negativo
    ciclo_id = billing.ciclo_de_compra(hoy)
    cuotas = store.desglose_de_ciclo(ciclo_id)["tarjeta"]
    deuda, por_delante = store.deuda_de_tarjeta(ciclo_id)
    return {"otros": otros, "credito": g["credito"], "ingresos": g["ingreso"],
            "gastado": g["credito"] + otros, "cuotas": cuotas,
            "total_mes": cuotas + otros, "deuda": deuda, "por_delante": por_delante,
            # Va en el dict para que entre también en la firma del panel: si
            # clasificás un movimiento, la dona cambia y hay que redibujar.
            "categorias": _porciones(store, ini_act, hoy)}


def _porciones(store, desde, hasta):
    """Gasto por categoría del período, de mayor a menor, con la cola agrupada
    en "Otras". Devuelve una lista de tuplas (no dict) para que sea hasheable
    en la firma del panel.

    Solo entran los gastos: un ingreso no es una categoría de gasto y en una
    dona no se puede dibujar un sector negativo. Por eso estas porciones suman
    el gasto BRUTO, que es lo que va al centro de la dona — no el 'Gastado hasta
    hoy', que va neto de ingresos."""
    _total, por_cat = store.gasto_neto_por_fecha(desde, hasta)
    positivas = sorted(((c, m) for c, m in por_cat.items() if m > 0),
                       key=lambda x: -x[1])
    if len(positivas) <= MAX_PORCIONES + 1:
        return tuple(positivas)
    cola = sum(m for _c, m in positivas[MAX_PORCIONES:])
    return tuple(positivas[:MAX_PORCIONES]) + (("Otras", cola),)


def _bloques_panel(n: dict):
    """Las dos tablas que van dibujadas AL PIE DE LA IMAGEN.

    Las dos arrancan por la misma fila a propósito: es el término que ambos
    totales comparten, y ponerlo primero deja ver de una que lo único que cambia
    entre ellos es cómo entra el crédito.
    Las etiquetas son cortas por una razón de ancho, no de estilo: con montos de
    siete dígitos, una etiqueta larga se monta encima del número. El matiz de que
    el débito va NETO de ingresos lo aclara el caption, justo cuando hay ingresos
    que descontar.
    """
    compartida = ("Débito y transf.", n["otros"], False)
    return [
        ("Lo que compraste (el gráfico)", [
            compartida,
            ("Crédito comprado", n["credito"], False),
            ("Gastado hasta hoy", n["gastado"], True),
        ]),
        (f"Lo que te cobran el {config.BILLING_DAY}", [
            compartida,
            ("Cuotas del ciclo", n["cuotas"], False),
            ("Total mes", n["total_mes"], True),
        ]),
    ]


def _caption_panel(ahora, n: dict) -> str:
    """Texto del panel: SOLO lo que la imagen no muestra.

    El ciclo, el gastado y el total ya están dibujados arriba; repetirlos acá
    era ruido. Queda la deuda —que no aparece en ninguna tabla porque es un
    saldo, no un flujo del mes— y el sello de hora.

    La deuda va primera a propósito: la barra de "Mensaje fijado" arriba del
    chat muestra la primera línea del caption, así que ese es el número que se
    ve sin abrir nada."""
    lineas = []
    if n["deuda"]:
        plural = "ciclo" if n["por_delante"] == 1 else "ciclos"
        lineas.append(f"Deuda total tarjeta: <b>{_pesos(n['deuda'])}</b> "
                      f"({n['por_delante']} {plural} por delante)")
    lineas.append(f"<i>Actualizado {ahora.strftime('%d/%m %H:%M')}</i>")
    return "\n".join(lineas)


def _imagen_del_ciclo(store, ciclos, dias_ciclo, transcurridos, n):
    """La imagen completa del ciclo: curva, tablas y dona de categorías.

    La arman igual el panel fijado y el resumen de las 22, para que sean
    exactamente la misma imagen y no dos versiones que puedan divergir."""
    totales = [store.gasto_neto_por_fecha(ini, corte)[0] for _lbl, ini, corte in ciclos]
    total_act = totales[0]
    proyeccion = (total_act * dias_ciclo // (transcurridos + 1)
                  if total_act > 0 and 3 <= transcurridos < dias_ciclo - 1 else None)
    porciones = n["categorias"]
    torta = ({"total": sum(m for _c, m in porciones), "porciones": list(porciones)}
             if porciones else None)
    png = _grafico_ritmo(store,
                         [(lbl, ini, corte, tot)
                          for (lbl, ini, corte), tot in zip(ciclos, totales)],
                         dias_ciclo, transcurridos, proyeccion,
                         bloques=_bloques_panel(n),
                         nota=(f"«Débito y transf.» va neto de {_pesos(abs(n['ingresos']))} "
                               f"de ingresos recibidos" if n["ingresos"] else None),
                         torta=torta)
    return png, totales


def _id_guardado(store, clave) -> int | None:
    """Lee un message_id de la hoja Config, o None si no hay uno usable.

    Tolera que la planilla lo devuelva como número: guardamos "12345" pero
    Sheets puede devolver 12345 o incluso 12345.0, y un `.isdigit()` sobre eso
    daba False — el panel creía que no existía y creaba otro."""
    crudo = str(store.get_config(clave, "") or "").strip()
    m = re.fullmatch(r"(\d+)(?:\.0*)?", crudo)
    return int(m.group(1)) if m else None


def _toca_bajar_el_panel(store, en_chat: int) -> bool:
    """Lleva la cuenta de cuánto se enterró el panel y dice si toca bajarlo.

    El contador vive en la hoja Config y solo se escribe cuando efectivamente
    aparecieron mensajes nuevos: escribirlo en cada corrida serían 1.440
    escrituras diarias contra la API de Sheets para no cambiar nada."""
    if not config.PANEL_MOVER_TRAS:
        return False
    acumulado = _entero(store.get_config("panel_enterrado", 0)) + en_chat
    if acumulado >= config.PANEL_MOVER_TRAS:
        return True
    if en_chat:
        store.set_config("panel_enterrado", str(acumulado))
    return False


def _entero(valor) -> int:
    """La hoja puede devolver el contador como texto, entero o decimal."""
    m = re.fullmatch(r"(\d+)(?:\.0*)?", str(valor or "").strip())
    return int(m.group(1)) if m else 0


def _panel_al_dia(store, ahora, en_chat: int = 0):
    """Mantiene UN mensaje fijado en el chat con el gráfico del ciclo al día.

    En vez de mandar una foto nueva cada vez (que se entierra en el historial),
    edita siempre el mismo mensaje: queda fijado arriba del chat, siempre
    actualizado. Editar no genera notificación, así que refrescarlo no molesta.
    El id vive en la hoja Config ('panel_message_id').

    Se refresca en cuanto CAMBIA algo. El disparador es una firma del contenido,
    no el reloj: si los números son los mismos que la última vez, no se toca. Así
    el bot puede correr cada minuto y el panel reacciona en menos de un minuto a
    un movimiento nuevo, a una anulación o a un cambio de cuotas, sin regenerar
    y subir la imagen 1.440 veces al día para mover un sello de hora.

    PANEL_MINUTOS pasa a ser solo eso: cada cuánto refrescar el "Actualizado"
    cuando no cambió nada, para que el panel no parezca muerto.

    `en_chat` son los mensajes que aparecieron en el chat en esta corrida. Cada
    tantos (PANEL_MOVER_TRAS) el panel se BAJA al final: se borra y se manda de
    nuevo abajo, porque editar un mensaje no lo mueve y con el tiempo quedaría
    cientos de mensajes arriba.
    """
    if not config.PANEL_ACTIVO:
        return

    bajar = _toca_bajar_el_panel(store, en_chat)

    hoy = ahora.date()
    dias_ciclo, transcurridos, ciclos = _ventanas_de_ciclo(hoy)
    # Los números son baratos (el Store cachea la planilla); la imagen no. Por
    # eso se calculan primero y con ellos se decide si vale la pena dibujar.
    n = _numeros_del_ciclo(store, ciclos)
    caption = _caption_panel(ahora, n)

    # La firma sale de los NÚMEROS, no del caption: desde que el caption dejó de
    # repetir los totales, firmarlo habría dejado ciego al panel ante un cambio
    # en el gasto. Incluye el día del ciclo porque la curva avanza con él.
    cuerpo = repr(sorted(n.items())) + f"|{transcurridos}|{dias_ciclo}"
    firma = hashlib.sha1(cuerpo.encode("utf-8")).hexdigest()[:16]

    # La marca se guarda naive: entre corridas puede faltar tzdata y mezclar
    # aware con naive haría reventar la resta.
    ahora_naive = ahora.replace(tzinfo=None)
    # str() a propósito: la hoja puede devolver el valor como número o como
    # fecha ya interpretada, no siempre como el texto que se guardó.
    # `bajar` gana sobre la firma: la movida es de posición, no de contenido, y
    # si esperáramos a que cambien los números el panel podría no bajar nunca.
    if not bajar and firma == str(store.get_config("panel_firma", "") or ""):
        ultimo = str(store.get_config("panel_actualizado", "") or "")
        try:
            minutos = (ahora_naive - datetime.fromisoformat(ultimo)).total_seconds() / 60
            if 0 <= minutos < config.PANEL_MINUTOS:
                return      # mismos números y el sello todavía es fresco
        except (ValueError, TypeError):
            pass            # marca ilegible: se regenera y se reescribe

    png, _totales = _imagen_del_ciclo(store, ciclos, dias_ciclo, transcurridos, n)
    if png is None:
        return

    def _marcar():
        # La firma se guarda DESPUÉS de que Telegram acepta: guardarla antes
        # hacía que un envío fallido quedara registrado como hecho y la corrida
        # siguiente ni lo reintentara.
        store.set_config("panel_firma", firma)
        store.set_config("panel_actualizado", ahora_naive.isoformat(timespec="seconds"))

    mid = _id_guardado(store, "panel_message_id")
    if mid and not bajar:
        estado = telegram_bot.editar_foto(mid, png, caption)
        if estado == "ok":
            _marcar()
            return
        if estado == "error":
            # Falla pasajera (429, 5xx, red). NO se crea un panel nuevo: eso es
            # lo que dejaba fotos fijadas de más. Se reintenta en la corrida
            # siguiente, que es dentro de un minuto.
            return

    # Acá se llega por tres caminos: no había panel, el mensaje ya no existe, o
    # toca bajarlo al final del chat. El viejo se borra (y si no se puede, al
    # menos se desfija) para no dejar dos fotos dando vueltas.
    if mid:
        if not telegram_bot.borrar(mid):
            telegram_bot.desfijar(mid)
    # Siempre silencioso: el panel es ambiente, no una novedad. Lo que sí avisa
    # son las compras y el resumen de la noche.
    msg = telegram_bot.enviar_foto(png, caption, silencioso=True)
    if not msg:
        return
    store.set_config("panel_message_id", str(msg["message_id"]))
    store.set_config("panel_enterrado", "0")     # arranca de cero abajo de todo
    _marcar()
    # Fijar SOLO si el panel se queda quieto. Cuando baja al final del chat el
    # fijado no aporta nada —ya está a la vista— y cada pinChatMessage deja un
    # "fijó una foto" en la conversación, que es justo el ruido que queríamos
    # sacar. Las dos configuraciones son coherentes entre sí:
    #   PANEL_MOVER_TRAS > 0  -> se mueve, no se fija, cero avisos
    #   PANEL_MOVER_TRAS = 0  -> se queda quieto y fijado, como antes
    if not config.PANEL_MOVER_TRAS:
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
    del_usuario = procesar_comandos(store)
    nuevos = procesar_correos(store)
    nuevos += procesar_recurrentes(store, _ahora_local().date())
    if nuevos:
        try:
            store.regenerar_resumen()
            store.regenerar_categorias()
        except gspread.exceptions.APIError as e:
            print(f"[aviso] no pude regenerar las hojas ahora: {e}")
    _quizas_resumen_nocturno(store)
    try:
        # Todo lo que apareció en el chat antes del panel: lo que escribió el
        # usuario más lo que mandó el bot. Con eso el panel sabe cuánto se
        # enterró y cuándo le toca bajar al final.
        _panel_al_dia(store, _ahora_local(),
                      en_chat=del_usuario + telegram_bot.mensajes_enviados())
    except Exception as e:
        # El panel es un extra: si falla, no puede tumbar la corrida que ya
        # registró los movimientos.
        print(f"[aviso] no pude actualizar el panel: {e}")
    print(f"Listo. {nuevos} movimiento(s) nuevo(s).")


if __name__ == "__main__":
    main()
