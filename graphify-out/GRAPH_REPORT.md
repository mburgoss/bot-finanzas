# Graph Report - bot finanzas personales  (2026-08-16)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 247 nodes · 483 edges · 18 communities (13 shown, 5 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 4 edges (avg confidence: 0.5)
- Token cost: 1,485 input · 152 output

## Graph Freshness
- Built from commit: `3e80cd6b`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Main Application Logic
- Formatting and Charts
- Billing Cycle Management
- Data Parsing and Testing
- Telegram Bot Interface
- Movement Actions and Cache
- Sheet Configuration and Formatting
- Data Store and Totals
- Expense Analytics
- Category Normalization
- Email Reader
- Sheet Visual Setup
- Transaction Entry
- GitHub Actions Workflow
- Gmail IMAP
- Google Sheets API
- Telegram Bot API

## God Nodes (most connected - your core abstractions)
1. `Store` - 48 edges
2. `pesos()` - 16 edges
3. `_resumen_nocturno()` - 14 edges
4. `_panel_al_dia()` - 14 edges
5. `procesar_correos()` - 12 edges
6. `_manejar_update()` - 11 edges
7. `parsear()` - 11 edges
8. `_render_movimiento()` - 10 edges
9. `main()` - 9 edges
10. `ciclo_de_compra()` - 9 edges

## Surprising Connections (you probably didn't know these)
- `procesar_correos()` --uses--> `Movimiento`  [INFERRED]
  src/main.py → src/parser.py
- `_crear_store()` --uses--> `Store`  [INFERRED]
  src/main.py → src/sheets.py
- `procesar_comandos()` --uses--> `Store`  [INFERRED]
  src/main.py → src/sheets.py
- `procesar_correos()` --uses--> `Store`  [INFERRED]
  src/main.py → src/sheets.py
- `main()` --calls--> `mensajes_enviados()`  [EXTRACTED]
  src/main.py → src/telegram_bot.py

## Import Cycles
- None detected.

## Communities (18 total, 5 thin omitted)

### Community 0 - "Main Application Logic"
Cohesion: 0.09
Nodes (37): nombre_ciclo(), 2026-08' -> 'agosto 2026'., _ahora_local(), _bloque_totales(), _contiene_cuenta(), _crear_store(), _de_remitente_conocido(), _entero() (+29 more)

### Community 1 - "Formatting and Charts"
Cohesion: 0.09
Nodes (31): delta_pct(), mes_corto(), pesos(), Formateo compartido por el texto de Telegram y el gráfico del resumen. Vive…, 1269 -> '$1.269' (separador de miles chileno)., agosto 2026' -> 'Agosto' (etiqueta corta para barras y leyendas)., Variación porcentual de `actual` respecto de `previo`, redondeada. Se usa tanto…, acumular() (+23 more)

### Community 2 - "Billing Cycle Management"
Cohesion: 0.10
Nodes (26): ciclo_de_compra(), cuotas_por_ciclo(), inicio_de_ciclo(), proximo_inicio_de_ciclo(), date, Lógica del ciclo de facturación y reparto de cuotas. La tarjeta se factura el…, Devuelve el mes de facturación ('YYYY-MM') donde cae una compra. Si el día de…, Fecha en que empezó el ciclo que contiene `fecha` (el día `billing_day` de este… (+18 more)

### Community 3 - "Data Parsing and Testing"
Cohesion: 0.16
Nodes (22): Prueba local sin tocar GitHub. Carga variables desde un archivo .env (si…, _fecha_numerica(), _fecha_texto_es(), _limpiar_texto(), _monto_a_int(), monto_probable(), Movimiento, _normalizado() (+14 more)

### Community 4 - "Telegram Bot Interface"
Cohesion: 0.09
Nodes (24): _caption_panel(), _id_guardado(), _panel_al_dia(), Texto del panel: SOLO lo que la imagen no muestra. El ciclo, el gastado y el…, Lee un message_id de la hoja Config, o None si no hay uno usable. Tolera que la…, Mantiene UN mensaje fijado en el chat con el gráfico del ciclo al día. En vez…, borrar(), desfijar() (+16 more)

### Community 5 - "Movement Actions and Cache"
Cohesion: 0.20
Nodes (8): Tras escribir en Movimientos: descarta el caché de datos (y el de filas si se…, Devuelve la fila normalizada desde el caché (0 lecturas extra)., Asigna (o cambia) la categoría de un movimiento., Cambia crédito <-> débito. Lo usan los movimientos que llegaron sin tipo…, Fija el monto. Para los correos que no se supieron leer, donde el monto es una…, Anula el movimiento (deja de contar) SIN borrarlo. Reversible con restaurar., Reactiva un movimiento anulado para que vuelva a contar., Devolución parcial: baja el monto. Si queda <= 0, elimina el movimiento.

### Community 6 - "Sheet Configuration and Formatting"
Cohesion: 0.19
Nodes (6): Alinea a la derecha las columnas fecha y ciclo_inicio de Movimientos. Sheets…, Pasa a negativo el monto de los ingresos ya cargados en positivo, así al sumar…, Recalcula ciclo_inicio de los movimientos NO-crédito al ciclo de facturación de…, Formato condicional en la columna 'monto': ingresos (negativos) en verde y…, Lista efectiva de (emoji, nombre): las fijas de config + las agregadas por el…, Agrega una categoría nueva si no existe (ignora may/min). Devuelve el nombre…

### Community 7 - "Data Store and Totals"
Cohesion: 0.22
Nodes (6): Todas las filas de Movimientos, leídas UNA vez por corrida., Movimientos activos que cuentan (crédito/débito/transferencia/ingreso) y aún no…, Devuelve {ciclo: total} sin escribir la hoja Resumen. Crédito reparte cuotas…, Devuelve {'tarjeta': ..., 'total': ...} para un ciclo. 'tarjeta' = cuotas de…, Cuotas de crédito todavía no facturadas: la de este ciclo y todas las que…, Store

### Community 8 - "Expense Analytics"
Cohesion: 0.22
Nodes (8): _fecha(), date, Interpreta una fecha venga como texto ISO o como serial de Sheets., Itera (fecha, neto, categoría, tipo) de los movimientos vigentes cuya FECHA cae…, Suma neta y desglose por categoría del período. Sirve para comparar el ritmo de…, Gasto neto día a día {fecha: neto}, para la curva acumulada del gráfico. Solo…, Abre el 'Gastado' del gráfico por tipo, que es lo que hace falta para cuadrarlo…, Promedio del gasto neto de los últimos `n` ciclos completos anteriores al…

### Community 9 - "Category Normalization"
Cohesion: 0.18
Nodes (8): _ciclo(), _num(), Normaliza una fila cruda a tipos consistentes para main.py., Convierte a entero un valor que puede venir como número o como '$1.269'., Normaliza un ciclo 'YYYY-MM' aunque Sheets lo haya guardado como fecha., Devuelve {(categoria, ciclo): monto} incluyendo gastos (crédito/débito/…, Reescribe la hoja 'Categorías' como tabla Categoría × Mes (+ Total)., Encabezado en negrita/centrado, categorías en negrita y montos en pesos…

### Community 10 - "Email Reader"
Cohesion: 0.38
Nodes (6): _cuerpo(), _decode(), obtener_correos(), Lectura de correos por IMAP. Trae correos recientes de los remitentes del banco., Extrae el cuerpo del correo, preferentemente texto plano; si no, el HTML., Generador de (uid, asunto, remitente, cuerpo) de correos recientes del banco.

### Community 11 - "Sheet Visual Setup"
Cohesion: 0.29
Nodes (3): _client(), Ajustes de presentación de la planilla. Son cosméticos/no críticos: si alguno…, Agrega la columna 'categoria' al header de Movimientos si falta (planillas…

## Knowledge Gaps
- **4 isolated node(s):** `GitHub Actions Workflow`, `Gmail IMAP`, `Google Sheets API`, `Telegram Bot API`
  These have ≤1 connection - possible missing edges or undocumented components.
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Store` connect `Data Store and Totals` to `Main Application Logic`, `Billing Cycle Management`, `Movement Actions and Cache`, `Sheet Configuration and Formatting`, `Expense Analytics`, `Category Normalization`, `Sheet Visual Setup`, `Transaction Entry`?**
  _High betweenness centrality (0.469) - this node is a cross-community bridge._
- **Why does `procesar_correos()` connect `Main Application Logic` to `Email Reader`, `Data Parsing and Testing`, `Data Store and Totals`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Why does `_panel_al_dia()` connect `Telegram Bot Interface` to `Main Application Logic`, `Formatting and Charts`, `Billing Cycle Management`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Store` (e.g. with `_crear_store()` and `procesar_comandos()`) actually correct?**
  _`Store` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `procesar_correos()` (e.g. with `Movimiento` and `Store`) actually correct?**
  _`procesar_correos()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `GitHub Actions Workflow`, `Gmail IMAP`, `Google Sheets API` to the rest of the system?**
  _4 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Main Application Logic` be split into smaller, more focused modules?**
  _Cohesion score 0.08961593172119488 - nodes in this community are weakly interconnected._