"""Acceso a Google Sheets con una cuenta de servicio.

Hojas:
  - "Movimientos": registro de cada compra/transferencia.
  - "Resumen": totales por ciclo de facturación (se regenera).
  - "Config": pares clave/valor (telegram_offset, next_id).

El "id" es un contador propio (Config 'next_id'), estable aunque se borren filas.
Las filas se ubican buscando el id en la columna 1, no por posición.
"""

import json
import re
from datetime import date, timedelta

import gspread
from google.oauth2.service_account import Credentials

from . import billing, config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Al leer, pedimos valores CRUDOS: así el formato visual ($ y miles) no afecta
# los cálculos. Los montos vuelven como número y las fechas como texto o serial.
CRUDO = "UNFORMATTED_VALUE"


def _num(v) -> int:
    """Convierte a entero un valor que puede venir como número o como '$1.269'."""
    if isinstance(v, (int, float)):
        return int(v)
    return int(re.sub(r"[^\d-]", "", str(v)) or 0)


def _fecha(v) -> date:
    """Interpreta una fecha venga como texto ISO o como serial de Sheets."""
    if isinstance(v, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(v))
    return date.fromisoformat(str(v)[:10])


def _ciclo(v) -> str:
    """Normaliza un ciclo 'YYYY-MM' aunque Sheets lo haya guardado como fecha."""
    if isinstance(v, (int, float)):
        return (date(1899, 12, 30) + timedelta(days=int(v))).strftime("%Y-%m")
    return str(v)[:7]

MOV_HEADERS = [
    "id", "fecha", "comercio", "monto", "tipo",
    "digitos", "num_cuotas", "valor_cuota", "ciclo_inicio", "message_id", "estado",
    "categoria",
]
COL = {nombre: i + 1 for i, nombre in enumerate(MOV_HEADERS)}  # nombre -> columna (1-based)


def _client() -> gspread.Client:
    info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def ciclo_de_movimiento(fecha: date, tipo: str) -> str:
    """TODOS los movimientos usan el mismo ciclo de facturación (día 22).

    Así crédito, débito, transferencias e ingresos de una misma fecha caen en
    el mismo ciclo y se suman/restan al mismo total (no en meses separados).
    """
    return billing.ciclo_de_compra(fecha)


class Store:
    def __init__(self):
        # Cachés para NO releer la planilla en cada acción (evita el error 429
        # 'Quota exceeded' de la API de Sheets). Se invalidan al escribir.
        self._cfg_cache = None      # dict clave -> valor (Config, se lee 1 vez)
        self._cfg_rows = None       # dict clave -> fila (para escribir sin releer)
        self._mov_cache = None      # registros de Movimientos (se leen 1 vez)
        self._id_row_cache = None   # id -> número de fila
        self.gc = _client()
        self.ss = self.gc.open_by_key(config.SHEET_ID)
        self.mov = self._hoja("Movimientos", MOV_HEADERS)
        self.cfg = self._hoja("Config", ["clave", "valor"])
        self.resumen = self._hoja("Resumen", ["Año", "Mes", "credito", "debito_transf", "total"])
        self.cat = self._hoja("Categorías", ["Categoría"])
        self._asegurar_columna_categoria()  # crítico: debe existir antes de escribir filas
        self._setup_visual()

    def _asegurar_columna_categoria(self):
        """Agrega la columna 'categoria' al header de Movimientos si falta
        (planillas creadas antes de esta función)."""
        encabezados = self.mov.row_values(1)
        if "categoria" in encabezados:
            return
        col = COL["categoria"]
        if self.mov.col_count < col:
            self.mov.add_cols(col - self.mov.col_count)
        self.mov.update_cell(1, col, "categoria")

    def _setup_visual(self):
        """Ajustes de presentación de la planilla. Son cosméticos/no críticos:
        si alguno falla, se ignora y el bot sigue funcionando igual."""
        for paso in (self._alinear_fechas_una_vez,
                     self._migrar_signo_ingresos_una_vez,
                     self._migrar_ciclo_inicio_una_vez,
                     self._colorear_montos_una_vez):
            try:
                paso()
            except Exception as e:  # nunca romper el bot por un tema visual
                print(f"[aviso] {paso.__name__}: {e}")

    def _alinear_fechas_una_vez(self):
        """Alinea a la derecha las columnas fecha y ciclo_inicio de Movimientos.

        Sheets alinea el texto a la izquierda y las fechas a la derecha; como
        las transferencias/ingresos se guardan como texto, quedaban disparejas.
        Se hace una sola vez (se marca en Config) para no gastar llamadas.
        """
        if self.get_config("fmt_fechas_v1") == "1":
            return
        col_f = gspread.utils.rowcol_to_a1(1, COL["fecha"])[:-1]         # 'B'
        col_c = gspread.utils.rowcol_to_a1(1, COL["ciclo_inicio"])[:-1]  # 'I'
        self.mov.batch_format([
            {"range": f"{col_f}2:{col_f}", "format": {"horizontalAlignment": "RIGHT"}},
            {"range": f"{col_c}2:{col_c}", "format": {"horizontalAlignment": "RIGHT"}},
        ])
        self.set_config("fmt_fechas_v1", "1")

    def _migrar_signo_ingresos_una_vez(self):
        """Pasa a negativo el monto de los ingresos ya cargados en positivo, así
        al sumar la columna 'monto' los ingresos restan en vez de sumar."""
        if self.get_config("signo_ingresos_v1") == "1":
            return
        cambios = False
        for i, reg in enumerate(self._registros(), start=2):  # fila 2 = primer dato
            if str(reg.get("tipo") or "").lower() != "ingreso":
                continue
            monto = _num(reg.get("monto"))
            if monto > 0:
                self.mov.update_cell(i, COL["monto"], -monto)
                vc = _num(reg.get("valor_cuota"))
                if vc > 0:
                    self.mov.update_cell(i, COL["valor_cuota"], -vc)
                cambios = True
        if cambios:
            self._invalidar()
        self.set_config("signo_ingresos_v1", "1")

    def _migrar_ciclo_inicio_una_vez(self):
        """Recalcula ciclo_inicio de los movimientos NO-crédito al ciclo de
        facturación de su fecha. Los viejos (previos a unificar ciclos) tenían el
        mes calendario, que no afecta los totales pero se veía mal en la planilla.
        El crédito no se toca (su ciclo_inicio es el ciclo de la 1ª cuota)."""
        if self.get_config("ciclo_inicio_v1") == "1":
            return
        cambios = False
        for i, reg in enumerate(self._registros(), start=2):  # fila 2 = 1er dato
            if reg.get("tipo") not in ("debito", "transferencia", "ingreso"):
                continue
            correcto = billing.ciclo_de_compra(_fecha(reg.get("fecha")))
            if _ciclo(reg.get("ciclo_inicio")) != correcto:
                self.mov.update_cell(i, COL["ciclo_inicio"], correcto)
                cambios = True
        if cambios:
            self._invalidar()
        self.set_config("ciclo_inicio_v1", "1")

    def _colorear_montos_una_vez(self):
        """Formato condicional en la columna 'monto': ingresos (negativos) en
        verde y gastos (positivos) en rojo, para distinguirlos de un vistazo."""
        if self.get_config("cond_fmt_v1") == "1":
            return
        col0 = COL["monto"] - 1  # 0-based
        rango = {"sheetId": self.mov.id, "startRowIndex": 1,
                 "startColumnIndex": col0, "endColumnIndex": col0 + 1}

        def regla(tipo_cond, color):
            return {"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [rango],
                "booleanRule": {
                    "condition": {"type": tipo_cond,
                                  "values": [{"userEnteredValue": "0"}]},
                    "format": {"textFormat": {
                        "bold": True,
                        "foregroundColor": color}},
                },
            }}}

        verde = {"red": 0.11, "green": 0.53, "blue": 0.30}
        rojo = {"red": 0.72, "green": 0.11, "blue": 0.11}
        self.ss.batch_update({"requests": [
            regla("NUMBER_LESS", verde),      # ingresos (monto < 0) -> verde
            regla("NUMBER_GREATER", rojo),    # gastos   (monto > 0) -> rojo
        ]})
        self.set_config("cond_fmt_v1", "1")

    def _hoja(self, nombre, headers):
        try:
            return self.ss.worksheet(nombre)
        except gspread.WorksheetNotFound:
            ws = self.ss.add_worksheet(nombre, rows=1000, cols=max(4, len(headers)))
            ws.append_row(headers)
            return ws

    # --- Config (cacheada en memoria, lectura y escritura sin releer) ---
    def _config(self) -> dict:
        if self._cfg_cache is None:
            self._cfg_cache, self._cfg_rows = {}, {}
            for i, f in enumerate(self.cfg.get_all_records(), start=2):  # fila 2 = 1er dato
                clave = str(f.get("clave"))
                self._cfg_cache[clave] = str(f.get("valor", ""))
                self._cfg_rows[clave] = i
        return self._cfg_cache

    def get_config(self, clave, default=""):
        return self._config().get(clave, default)

    def set_config(self, clave, valor):
        self._config()  # asegura cachés cargados
        if clave in self._cfg_rows:
            self.cfg.update_cell(self._cfg_rows[clave], 2, str(valor))
        else:
            self.cfg.append_row([clave, str(valor)])
            self._cfg_rows[clave] = len(self._cfg_rows) + 2  # nueva fila al final
        self._cfg_cache[clave] = str(valor)

    def _next_id(self) -> int:
        return int(self.get_config("next_id", "1") or "1")

    # --- Categorías ---
    def categorias(self) -> list[tuple[str, str]]:
        """Lista efectiva de (emoji, nombre): las fijas de config + las agregadas
        por el usuario (guardadas en Config como 'categorias_extra')."""
        extra = self.get_config("categorias_extra", "")
        agregadas = [c.strip() for c in extra.split("||") if c.strip()]
        return list(config.CATEGORIAS) + [("🏷️", c) for c in agregadas]

    def agregar_categoria(self, nombre: str) -> str:
        """Agrega una categoría nueva si no existe (ignora may/min). Devuelve el
        nombre canónico (el ya existente si coincide, o el nuevo)."""
        nombre = " ".join(nombre.split()).strip()
        for _emoji, existente in self.categorias():
            if existente.lower() == nombre.lower():
                return existente  # ya existe: reusa su forma canónica
        extra = self.get_config("categorias_extra", "")
        agregadas = [c for c in extra.split("||") if c]
        agregadas.append(nombre)
        self.set_config("categorias_extra", "||".join(agregadas))
        return nombre

    # --- Movimientos (con caché por corrida) ---
    def _registros(self) -> list[dict]:
        """Todas las filas de Movimientos, leídas UNA vez por corrida."""
        if self._mov_cache is None:
            self._mov_cache = self.mov.get_all_records(value_render_option=CRUDO)
        return self._mov_cache

    def _invalidar(self, filas_nuevas=False):
        """Tras escribir en Movimientos: descarta el caché de datos (y el de
        filas si se agregó/quitó alguna)."""
        self._mov_cache = None
        if filas_nuevas:
            self._id_row_cache = None

    @staticmethod
    def _normalizar(reg: dict) -> dict:
        """Normaliza una fila cruda a tipos consistentes para main.py."""
        return {
            "id": _num(reg.get("id")),
            "fecha": _fecha(reg.get("fecha")).isoformat(),
            "comercio": str(reg.get("comercio") or ""),
            "monto": _num(reg.get("monto")),
            "tipo": reg.get("tipo"),
            "digitos": str(reg.get("digitos") or ""),
            "num_cuotas": _num(reg.get("num_cuotas") or 1),
            "valor_cuota": _num(reg.get("valor_cuota")),
            "ciclo_inicio": _ciclo(reg.get("ciclo_inicio")),
            "message_id": str(reg.get("message_id") or ""),
            "estado": str(reg.get("estado") or ""),
            "categoria": str(reg.get("categoria") or ""),
        }

    def message_ids(self) -> set:
        return {str(r.get("message_id")) for r in self._registros() if r.get("message_id")}

    def _fila_de_id(self, mov_id) -> int | None:
        if self._id_row_cache is None:
            ids = self.mov.col_values(COL["id"])  # fila 1 = encabezado
            self._id_row_cache = {str(v): i + 1 for i, v in enumerate(ids) if v}
        return self._id_row_cache.get(str(mov_id))

    def agregar_movimiento(self, mov, num_cuotas=1) -> int:
        """Agrega un movimiento con id estable. Devuelve su id."""
        mov_id = self._next_id()
        # Los ingresos se guardan en negativo para que al sumar la columna
        # 'monto' resten (los gastos suman). Los cálculos usan el tipo, no el signo.
        monto = -abs(mov.monto) if mov.tipo == "ingreso" else mov.monto
        valor_cuota = monto // max(1, num_cuotas)
        ciclo = ciclo_de_movimiento(mov.fecha, mov.tipo)
        fila = [
            mov_id,
            mov.fecha.strftime("%Y-%m-%d"),
            mov.comercio,
            monto,
            mov.tipo,
            mov.digitos,
            num_cuotas,
            valor_cuota,
            ciclo,
            mov.uid,
            "activo",
            "",  # categoria: se asigna después con los botones
        ]
        # RAW: guarda las fechas y textos tal cual (sin convertirlos a serial/número).
        self.mov.append_row(fila, value_input_option="RAW")
        self._invalidar(filas_nuevas=True)
        self.set_config("next_id", mov_id + 1)
        return mov_id

    def agregar_cuotas_en_curso(self, comercio, fecha_compra, valor_cuota,
                                cuotas_restantes, ciclo_inicio) -> int:
        """Carga una compra en cuotas ya en curso (del estado de cuenta).
        Las `cuotas_restantes` empiezan a contar en `ciclo_inicio`."""
        mov_id = self._next_id()
        monto = valor_cuota * cuotas_restantes  # así monto/N = valor_cuota exacto
        fila = [
            mov_id, fecha_compra, comercio, monto, "credito", "",
            cuotas_restantes, valor_cuota, ciclo_inicio, f"backfill:{mov_id}", "activo", "",
        ]
        # RAW: guarda las fechas y textos tal cual (sin convertirlos a serial/número).
        self.mov.append_row(fila, value_input_option="RAW")
        self._invalidar(filas_nuevas=True)
        self.set_config("next_id", mov_id + 1)
        return mov_id

    def obtener_movimiento(self, mov_id) -> dict | None:
        """Devuelve la fila normalizada desde el caché (0 lecturas extra)."""
        for reg in self._registros():
            if str(reg.get("id")) == str(mov_id):
                return self._normalizar(reg)
        return None

    def set_categoria(self, mov_id, categoria: str) -> dict | None:
        """Asigna (o cambia) la categoría de un movimiento."""
        fila = self._fila_de_id(mov_id)
        if not fila:
            return None
        self.mov.update_cell(fila, COL["categoria"], categoria)
        self._invalidar()
        return self.obtener_movimiento(mov_id)

    def sin_categoria(self) -> list[dict]:
        """Movimientos activos que cuentan (crédito/débito/transferencia/ingreso)
        y aún no tienen categoría, normalizados y ordenados por id."""
        pendientes = [
            self._normalizar(reg) for reg in self._registros()
            if str(reg.get("estado") or "").lower() != "anulado"
            and reg.get("tipo") in ("credito", "debito", "transferencia", "ingreso")
            and not str(reg.get("categoria") or "").strip()
        ]
        pendientes.sort(key=lambda r: r["id"])
        return pendientes

    def actualizar_cuotas(self, mov_id, num_cuotas: int) -> dict | None:
        reg = self.obtener_movimiento(mov_id)
        fila = self._fila_de_id(mov_id)
        if not reg or not fila:
            return None
        valor = reg["monto"] // max(1, num_cuotas)
        self.mov.update_cell(fila, COL["num_cuotas"], num_cuotas)
        self.mov.update_cell(fila, COL["valor_cuota"], valor)
        self._invalidar()
        return self.obtener_movimiento(mov_id)

    def actualizar_tipo(self, mov_id, tipo: str) -> dict | None:
        """Cambia crédito <-> débito. Lo usan los movimientos que llegaron sin
        tipo (correo que no se supo leer) y también sirve para corregir un tipo
        mal detectado.

        Al pasar a crédito se deja 1 cuota si no tenía: un movimiento de crédito
        sin num_cuotas no se reparte bien en el ciclo."""
        reg = self.obtener_movimiento(mov_id)
        fila = self._fila_de_id(mov_id)
        if not reg or not fila or tipo not in ("credito", "debito"):
            return None
        self.mov.update_cell(fila, COL["tipo"], tipo)
        if tipo == "credito" and not _num(reg.get("num_cuotas")):
            self.mov.update_cell(fila, COL["num_cuotas"], 1)
            self.mov.update_cell(fila, COL["valor_cuota"], _num(reg["monto"]))
        self._invalidar()
        return self.obtener_movimiento(mov_id)

    def actualizar_monto(self, mov_id, monto: int) -> dict | None:
        """Fija el monto. Para los correos que no se supieron leer, donde el
        monto es una lectura aproximada que hay que poder corregir a mano.

        El signo lo manda el tipo, no lo que se escriba: un ingreso se guarda
        negativo y el resto positivo, que es como cuenta el resto del bot."""
        reg = self.obtener_movimiento(mov_id)
        fila = self._fila_de_id(mov_id)
        if not reg or not fila or monto <= 0:
            return None
        signo = -1 if reg["tipo"] == "ingreso" else 1
        self.mov.update_cell(fila, COL["monto"], signo * abs(int(monto)))
        cuotas = max(1, _num(reg.get("num_cuotas")) or 1)
        self.mov.update_cell(fila, COL["valor_cuota"], signo * abs(int(monto)) // cuotas)
        self._invalidar()
        return self.obtener_movimiento(mov_id)

    def eliminar_movimiento(self, mov_id) -> dict | None:
        """Anula el movimiento (deja de contar) SIN borrarlo. Reversible con restaurar."""
        fila = self._fila_de_id(mov_id)
        if not fila:
            return None
        self.mov.update_cell(fila, COL["estado"], "anulado")
        self._invalidar()
        return self.obtener_movimiento(mov_id)

    def restaurar_movimiento(self, mov_id) -> dict | None:
        """Reactiva un movimiento anulado para que vuelva a contar."""
        fila = self._fila_de_id(mov_id)
        if not fila:
            return None
        self.mov.update_cell(fila, COL["estado"], "activo")
        self._invalidar()
        return self.obtener_movimiento(mov_id)

    def reducir_monto(self, mov_id, monto_devuelto: int) -> dict | None:
        """Devolución parcial: baja el monto. Si queda <= 0, elimina el movimiento."""
        reg = self.obtener_movimiento(mov_id)
        fila = self._fila_de_id(mov_id)
        if not reg or not fila:
            return None
        nuevo = reg["monto"] - monto_devuelto
        if nuevo <= 0:
            return self.eliminar_movimiento(mov_id)
        n = reg["num_cuotas"] or 1
        self.mov.update_cell(fila, COL["monto"], nuevo)
        self.mov.update_cell(fila, COL["valor_cuota"], nuevo // max(1, n))
        self._invalidar()
        info = self.obtener_movimiento(mov_id)
        info["_devuelto"] = monto_devuelto
        return info

    # --- Totales ---
    def calcular_totales(self) -> dict:
        """Devuelve {ciclo: total} sin escribir la hoja Resumen.
        Crédito reparte cuotas por ciclo; débito/transferencia/ingreso van al
        MISMO ciclo de facturación (día 22) que la fecha del movimiento."""
        credito, otros = {}, {}
        for reg in self._registros():
            if str(reg.get("estado") or "").lower() == "anulado":
                continue  # los anulados no cuentan
            if reg["tipo"] not in ("credito", "debito", "transferencia", "ingreso"):
                continue  # p.ej. 'revisar': aún no clasificado, no cuenta
            monto = _num(reg["monto"])
            fecha = _fecha(reg["fecha"])
            if reg["tipo"] == "credito":
                n = _num(reg.get("num_cuotas") or 1)
                # La primera cuota cae en el ciclo_inicio guardado (para compras
                # en curso arrastradas del estado de cuenta puede diferir de la fecha).
                ciclo0 = _ciclo(reg.get("ciclo_inicio")) or ciclo_de_movimiento(fecha, "credito")
                for c in billing.cuotas_por_ciclo(ciclo0, monto, n):
                    credito[c["ciclo"]] = credito.get(c["ciclo"], 0) + c["monto"]
            elif reg["tipo"] == "ingreso":  # dinero recibido: RESTA del total
                mes = billing.ciclo_de_compra(fecha)
                otros[mes] = otros.get(mes, 0) - abs(monto)  # robusto al signo guardado
            else:  # debito o transferencia enviada
                mes = billing.ciclo_de_compra(fecha)
                otros[mes] = otros.get(mes, 0) + abs(monto)
        return {"credito": credito, "otros": otros}

    def total_de_ciclo(self, ciclo: str) -> int:
        t = self.calcular_totales()
        return t["credito"].get(ciclo, 0) + t["otros"].get(ciclo, 0)

    def desglose_de_ciclo(self, ciclo: str) -> dict:
        """Devuelve {'tarjeta': ..., 'total': ...} para un ciclo.

        'tarjeta' = cuotas de crédito que se facturan en el ciclo (lo que se paga
        el día de facturación). 'total' = todo (tarjeta + débito + transferencias
        - ingresos), es decir lo que realmente se gastó en el período.
        """
        t = self.calcular_totales()
        tarjeta = t["credito"].get(ciclo, 0)
        total = tarjeta + t["otros"].get(ciclo, 0)
        return {"tarjeta": tarjeta, "total": total}

    def regenerar_resumen(self) -> dict:
        meses = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        t = self.calcular_totales()
        credito, otros = t["credito"], t["otros"]
        ciclos = sorted(set(credito) | set(otros))
        filas = [["Año", "Mes", "Crédito (cuotas)", "Débito + Transf.", "Total"]]
        totales = {}
        for c in ciclos:
            cr, ot = credito.get(c, 0), otros.get(c, 0)
            anio, mes = c[:4], meses[int(c[5:7])]  # 2026-08 -> "2026", "Agosto"
            filas.append([anio, mes, cr, ot, cr + ot])
            totales[c] = cr + ot
        self.resumen.clear()
        # Asegura que la hoja tenga suficientes columnas (antes tenía 4; ahora 5).
        ancho = len(filas[0])
        if self.resumen.col_count < ancho:
            self.resumen.add_cols(ancho - self.resumen.col_count)
        # RAW para que los meses no se interpreten como fecha (locale es_CL).
        self.resumen.update(filas, value_input_option="RAW")
        return totales

    # --- Gastos por categoría ---
    def calcular_por_categoria(self) -> dict:
        """Devuelve {(categoria, ciclo): monto} incluyendo gastos (crédito/débito/
        transferencia, suman) e ingresos (restan). Solo 'revisar' queda afuera.
        Así la suma por categoría cuadra con el total del mes. El crédito reparte
        sus cuotas por ciclo, igual que en los totales."""
        data = {}
        for reg in self._registros():
            if str(reg.get("estado") or "").lower() == "anulado":
                continue
            tipo = reg.get("tipo")
            if tipo not in ("credito", "debito", "transferencia", "ingreso"):
                continue  # 'revisar' aún sin clasificar: no cuenta
            cat = str(reg.get("categoria") or "").strip() or config.SIN_CATEGORIA
            monto = abs(_num(reg.get("monto")))
            fecha = _fecha(reg.get("fecha"))
            if tipo == "credito":
                n = _num(reg.get("num_cuotas") or 1)
                ciclo0 = _ciclo(reg.get("ciclo_inicio")) or billing.ciclo_de_compra(fecha)
                for c in billing.cuotas_por_ciclo(ciclo0, monto, n):
                    clave = (cat, c["ciclo"])
                    data[clave] = data.get(clave, 0) + c["monto"]
            else:
                ciclo = billing.ciclo_de_compra(fecha)
                clave = (cat, ciclo)
                # Ingreso resta; débito/transferencia suman.
                data[clave] = data.get(clave, 0) + (-monto if tipo == "ingreso" else monto)
        return data

    # --- Ritmo de gasto (por fecha de compra, para el resumen nocturno) ---
    def _netos_en(self, desde: date, hasta: date):
        """Itera (fecha, neto, categoría, tipo) de los movimientos vigentes cuya
        FECHA cae en [desde, hasta]. Cuenta el monto completo en la fecha: los
        gastos suman y los ingresos restan."""
        for reg in self._registros():
            if str(reg.get("estado") or "").lower() == "anulado":
                continue
            tipo = reg.get("tipo")
            if tipo not in ("credito", "debito", "transferencia", "ingreso"):
                continue
            f = _fecha(reg.get("fecha"))
            if not (desde <= f <= hasta):
                continue
            neto = -abs(_num(reg.get("monto"))) if tipo == "ingreso" else abs(_num(reg.get("monto")))
            cat = str(reg.get("categoria") or "").strip() or config.SIN_CATEGORIA
            yield f, neto, cat, tipo

    def gasto_neto_por_fecha(self, desde: date, hasta: date) -> tuple[int, dict]:
        """Suma neta y desglose por categoría del período. Sirve para comparar el
        ritmo de gasto entre ciclos."""
        total, por_cat = 0, {}
        for _f, neto, cat, _tipo in self._netos_en(desde, hasta):
            total += neto
            por_cat[cat] = por_cat.get(cat, 0) + neto
        return total, por_cat

    def gasto_diario(self, desde: date, hasta: date) -> dict:
        """Gasto neto día a día {fecha: neto}, para la curva acumulada del
        gráfico. Solo trae los días con movimiento; los huecos los rellena
        `grafico.acumular`."""
        por_dia = {}
        for f, neto, _cat, _tipo in self._netos_en(desde, hasta):
            por_dia[f] = por_dia.get(f, 0) + neto
        return por_dia

    def desglose_de_gasto(self, desde: date, hasta: date) -> dict:
        """Abre el 'Gastado' del gráfico por tipo, que es lo que hace falta para
        cuadrarlo contra el 'Total del mes'.

        Devuelve {'credito', 'debito', 'transferencia', 'ingreso'} donde crédito
        va a monto COMPLETO el día de la compra (no repartido en cuotas) e
        ingreso viene NEGATIVO. La suma de los cuatro es el 'Gastado'.

        Ojo con la diferencia clave: acá 'credito' es lo que COMPRASTE en el
        período; en calcular_totales() es la CUOTA que se factura en el ciclo,
        que incluye compras de meses anteriores."""
        partes = {"credito": 0, "debito": 0, "transferencia": 0, "ingreso": 0}
        for _f, neto, _cat, tipo in self._netos_en(desde, hasta):
            partes[tipo] = partes.get(tipo, 0) + neto
        return partes

    def deuda_de_tarjeta(self, ciclo_actual: str) -> tuple[int, int]:
        """Cuotas de crédito todavía no facturadas: la de este ciclo y todas las
        que vienen. Devuelve (monto, cuántos ciclos quedan por delante).

        Es un SALDO, no un flujo del mes: responde '¿cuánto debo en total?', que
        no se puede leer de ninguno de los otros números."""
        credito = self.calcular_totales()["credito"]
        pendientes = {c: m for c, m in credito.items() if c >= ciclo_actual}
        return sum(pendientes.values()), len(pendientes)

    def promedio_ciclos(self, inicio_actual: date, n: int = 3) -> int:
        """Promedio del gasto neto de los últimos `n` ciclos completos anteriores
        al actual (solo cuenta los ciclos con movimientos)."""
        totales, ini = [], inicio_actual
        for _ in range(n):
            fin = ini - timedelta(days=1)          # último día del ciclo anterior
            ini = billing.inicio_de_ciclo(fin)     # inicio de ese ciclo
            t, _cat = self.gasto_neto_por_fecha(ini, fin)
            if t:
                totales.append(t)
        return sum(totales) // len(totales) if totales else 0

    def regenerar_categorias(self) -> dict:
        """Reescribe la hoja 'Categorías' como tabla Categoría × Mes (+ Total)."""
        meses = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        data = self.calcular_por_categoria()
        cats = sorted({k[0] for k in data})
        ciclos = sorted({k[1] for k in data})
        encabezado = ["Categoría"] + [f"{meses[int(c[5:7])]} {c[:4]}" for c in ciclos] + ["Total"]
        filas = [encabezado]
        for cat in cats:
            fila, total = [cat], 0
            for c in ciclos:
                v = data.get((cat, c), 0)
                fila.append(v)
                total += v
            fila.append(total)
            filas.append(fila)
        # Fila final: total por mes.
        if cats:
            fila_tot, gran_total = ["Total"], 0
            for c in ciclos:
                s = sum(data.get((cat, c), 0) for cat in cats)
                fila_tot.append(s)
                gran_total += s
            fila_tot.append(gran_total)
            filas.append(fila_tot)
        self.cat.clear()
        ancho = len(filas[0])
        if self.cat.col_count < ancho:
            self.cat.add_cols(ancho - self.cat.col_count)
        # El encabezado como TEXTO ANTES de escribir, para que "Julio 2026" no se
        # interprete como fecha (se veía MM-AAAA) sin importar el locale.
        try:
            self.cat.format("1:1", {"numberFormat": {"type": "TEXT"}})
        except Exception as e:
            print(f"[aviso] texto encabezado Categorías: {e}")
        self.cat.update(filas, value_input_option="RAW")
        try:
            self._formatear_categorias(ancho)  # cosmético: no crítico
        except Exception as e:
            print(f"[aviso] formato Categorías: {e}")
        return data

    def _formatear_categorias(self, ancho: int):
        """Encabezado en negrita/centrado, categorías en negrita y montos en
        pesos alineados a la derecha. Congela la 1ª fila y la 1ª columna."""
        ultima = gspread.utils.rowcol_to_a1(1, ancho)[:-1]  # letra de la última col
        self.cat.batch_format([
            {"range": "1:1", "format": {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
                "backgroundColor": {"red": 0.85, "green": 0.89, "blue": 0.96}}},
            {"range": "A:A", "format": {"textFormat": {"bold": True}}},
            {"range": f"B2:{ultima}", "format": {
                "numberFormat": {"type": "NUMBER", "pattern": "$#,##0;[Red]-$#,##0"},
                "horizontalAlignment": "RIGHT"}},
        ])
        self.cat.freeze(rows=1, cols=1)
