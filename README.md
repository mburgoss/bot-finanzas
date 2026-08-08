# Bot de Finanzas (Telegram + Gmail + Google Sheets)

Bot que lee tu correo, detecta los cobros de tus tarjetas y cuenta,
las **transferencias enviadas y recibidas**, lleva el registro del gasto mensual
(respetando el ciclo de facturación del día **22**) y te avisa por **Telegram**.
Corre solo en **GitHub Actions** cada 5 minutos: tu PC puede estar apagado.

## ¿Cómo funciona?

1. Cada 5 min, GitHub ejecuta el bot.
2. Lee por IMAP los correos de los remitentes conocidos (`BANK_SENDERS`) y también
   cualquier correo cuyo asunto hable de transferencia.
3. Parsea el movimiento: **compra** (monto, comercio, dígitos, crédito/débito),
   **transferencia enviada** o **ingreso** (transferencia recibida).
4. Lo guarda en Google Sheets y te manda un aviso por Telegram con **botones**:
   el número de cuotas (si es crédito) y la **categoría** del gasto.
5. Tocás los botones cuando quieras (aunque se te acumulen varias compras): cada
   aviso lleva sus propios botones, así siempre le pega al movimiento correcto.
6. El resumen por mes y el **gasto por categoría** se recalculan solos (crédito
   reparte las cuotas; los ingresos restan).

> **Filtro de seguridad:** los correos de remitentes que NO están en `BANK_SENDERS`
> solo se procesan si contienen tu número de cuenta (`DEST_ACCOUNT`). Así una
> transferencia real a tu cuenta entra, y los correos ajenos se ignoran.

### Botones (lo más cómodo)
Cada aviso de un movimiento trae botones para clasificarlo sin escribir nada:
- **Cuotas** (solo crédito): `1 · 3 · 6 · 12 · Otra`.
- **Categoría**: `Alimentación · Transporte · Ocio · … · Otra categoría`.

Tocás y el bot **edita el mismo mensaje** marcando lo elegido con `✓`. La opción
**Otra** te deja escribir un valor nuevo (una categoría nueva queda guardada y
aparece como botón la próxima vez). Como el bot revisa cada 5 min, al tocar un
botón puede que Telegram muestre un "cargando…" que se corta; **igual se procesa**
y el mensaje se actualiza en la siguiente corrida (con el cron en 1 min es casi
instantáneo).

### Comandos de Telegram
| Comando | Qué hace |
|---|---|
| `/cuotas 5 12` | Marca el movimiento id `5` como comprado en **12 cuotas** (equivale al botón de cuotas). |
| `/eliminar 5` | **Anula** el movimiento id `5` (deja de contar, pero NO se borra). |
| `/eliminar 5 10000` | Devolución **parcial**: baja $10.000 al movimiento id `5`. |
| `/restaurar 5` | Deshace una anulación: el movimiento id `5` vuelve a contar. |
| `/resumen` | Tabla por mes: **Año · Mes · Tarjeta · Total mes**. |
| `/categorias` | Tabla del gasto por **categoría** del mes en curso. |
| `/clasificar` | Reenvía **todos los gastos sin categoría** con sus botones para etiquetarlos. `/clasificar 5` reabre uno puntual. |

Cada aviso de cobro incluye el **total del mes** y **lo que se paga en la tarjeta**
(las cuotas que caen en ese ciclo), y se actualiza solo cada vez que usas los
botones o `/cuotas`, `/eliminar` o `/restaurar`.

### Categorías
Las categorías son una **lista fija** (editable en `src/config.py`): Alimentación,
Restaurantes, Transporte, Hogar y servicios, Ocio, Compras, Salud, Educación,
Otros. Con **➕ Otra categoría** creás una nueva al vuelo; queda guardada en la
hoja `Config` (`categorias_extra`) y se suma a los botones. El bot **siempre te
pregunta** la categoría de cada movimiento que cuenta —gastos **e ingresos**—,
así la suma por categoría **cuadra** con el total del mes (los ingresos restan).
El total por categoría y por mes vive en la hoja **`Categorías`** (tabla Categoría
× Mes, con formato) y se ve rápido con `/categorias`.

Tipos de movimiento: **crédito** (reparte cuotas por ciclo del día 22), **débito**,
**transferencias enviadas** (cuentan como gasto del mes) e **ingresos**
(transferencias recibidas, que **restan** del total del mes).

Los ingresos pueden llegar de **cualquier banco**: el bot busca correos de
transferencia por asunto (no solo de remitentes conocidos) y usa un parser
genérico. Si llega uno que no logra interpretar, te avisa por Telegram y lo deja
como fila `revisar` en la planilla para que lo completes (nunca lo pierde en
silencio). Para que quede 100% automático, reenvíame el correo del banco nuevo y
agrego su formato + su remitente al `BANK_SENDERS`.

### 📌 Panel fijado (todo a la vista, siempre)
Un **único mensaje fijado arriba del chat** con el gráfico del ciclo y **todos los
totales cuadrados**. No se manda una foto nueva cada vez: se **edita siempre el mismo
mensaje**, así que no se entierra en el historial y **editar no genera notificación**.

Las **dos formas de contar** van como dos tablas **dibujadas dentro de la imagen**,
no en el texto del mensaje. La razón es concreta: el bloque `<pre>` de un caption
de Telegram **se envuelve en pantallas angostas** —en un iPhone parte cada fila en
dos y la tabla deja de leerse como tabla—. Dentro de la imagen el ancho lo fijamos
nosotros, así que las columnas aguantan en cualquier teléfono.

El caption **solo lleva lo que la imagen no muestra**: la deuda total y el sello de
hora. Repetir ahí el ciclo, el gastado y el total era ruido.

La deuda va primera a propósito: **la barra de "Mensaje fijado" arriba del chat
muestra la primera línea del caption**, así que ese es el número que ves sin abrir
nada — y es justamente el que no aparece en ninguna tabla, porque es un saldo y no
un flujo del mes.

```
LO QUE COMPRASTE  (el gráfico)          LO QUE TE COBRAN EL 22
Débito + transf − ingresos   $251.794   Débito + transf − ingresos  $251.794
Crédito comprado (completo)  $191.988   Cuotas que facturan el 22   $651.467
= Gastado hasta hoy          $443.782   = Total mes                 $903.261
        └── la misma primera fila en los dos bloques ──┘
```

Las dos tablas **arrancan con la misma fila a propósito**. Ese es el término que
los dos totales comparten, y ponerlo primero deja ver de una que **lo único que
cambia entre ellos es cómo entra el crédito**.

Los porcentajes contra los meses anteriores y la proyección de cierre **no van en
el texto**: ya están dibujados en la imagen.

Las dos usan **la misma ventana de fechas** (del 22 al 21). La única diferencia es
el crédito: a la izquierda va el **monto completo** el día de la compra (lo que
decidiste gastar), a la derecha va la **cuota** que cae en este ciclo (lo que te
descuentan, e incluye compras de meses anteriores). Por eso los dos totales no
coinciden, y por eso el bloque del medio aparece en los dos lados: es el término
que comparten, y verlo repetido deja cuadrar la cuenta a ojo.

La **deuda total tarjeta** es la suma de todas las cuotas todavía no facturadas
—de este ciclo y de los que vienen—. Es un saldo, no un flujo del mes: no se puede
deducir de ninguno de los otros números.

**Se refresca en cuanto cambia algo**, no cada tanto. El disparador es una firma
del contenido, no el reloj: si los números son los mismos que la última vez, no se
toca. Como el bot corre cada minuto, un movimiento nuevo, una anulación o un cambio
de cuotas aparecen en **menos de un minuto**; y cuando no pasa nada, no se regenera
ni se sube la imagen al pedo.

`PANEL_MINUTOS` (15) ya no gobierna eso: es solo cada cuánto refrescar el sello
"Actualizado" para que el panel no parezca muerto en un día sin movimientos.

El id del mensaje vive en la hoja `Config` (`panel_message_id`): si lo borrás del
chat, la próxima corrida crea uno nuevo y lo vuelve a fijar.

Se apaga con `PANEL_ACTIVO=0`.

> **Por qué acá y no en una app o una web.** Para *solo visualizar*, esto no agrega
> ninguna superficie nueva: los datos ya pasan por Telegram. Una PWA o un dashboard
> implicarían una URL más, una sesión más y un deployment más donde tus movimientos
> pueden leerse. El día que haga falta navegar historial o filtrar, ahí sí conviene
> una web — hoy no.

Al pie va una **dona con la estructura de gasto por categoría** y el total gastado
en el centro. Se muestran las 5 categorías más grandes y el resto se junta en
"Otras": pasados unos seis sectores, los más chicos dejan de distinguirse. Los seis
colores están validados para daltonismo (peor par vecino: ΔE 9,1 en protanopía).

Las porciones son **gasto**, no ingresos: en una dona no se puede dibujar un sector
negativo, así que un ingreso no aparece como categoría.

### 🌙 Resumen nocturno automático
Cada noche (por defecto a las **22:00**, hora `TIMEZONE`) el bot te manda **la misma
imagen del panel fijado** —curva, tablas y dona— con el texto del resumen debajo.
Una sola función arma esa imagen, así que las dos no pueden divergir.

El texto solo lleva lo que la imagen **no** dice:
- Las **categorías que más crecieron** respecto al ciclo pasado, en barras de texto.
  La dona muestra en qué se fue la plata *este* ciclo; esto muestra qué *cambió*.
- La **proyección** vs tu **promedio** de los últimos ciclos.

Cada dato aparece una sola vez.

El ritmo se mide por **fecha de compra** (monto completo el día que compraste), para poder
comparar ciclos día a día. Se envía una sola vez por día. La hora se ajusta con el secret
opcional `RESUMEN_HORA` (0–23) y el tema del gráfico con `GRAFICO_TEMA`
(`claro` u `oscuro`; se define en el propio workflow, no es un secret).

**Para verlo sin esperar a la noche:** pestaña **Actions** → "Bot Finanzas" →
**Run workflow**, y marcá **`forzar_resumen`** (ahí mismo podés elegir el `tema`
para comparar claro vs oscuro). El resumen sale al toque y **no** cuenta como el
de la noche: el de las 22:00 igual va a llegar a su hora.

Si el gráfico no se puede generar (falta `matplotlib`, Telegram rechaza la imagen), el
resumen igual sale en texto con las barras ASCII de siempre — nunca se pierde.

### ¿Y si me equivoco? (todo es reversible)
- **Marqué mal las cuotas:** reenvía `/cuotas <id> <n>` con el número correcto.
- **Anulé algo por error:** `/restaurar <id>` lo devuelve. Anular nunca borra la fila.
- **Devolución parcial equivocada:** corrige el valor en la columna **monto** de la
  planilla a mano (el bot recalcula solo).
- **Escape universal:** puedes **editar la planilla de Google directamente**
  (monto, num_cuotas, tipo, estado…) y el bot respeta esos cambios en la próxima corrida.
  Las filas con `estado = anulado` aparecen tachadas y en gris.

---

## 🔧 Instalación (una sola vez, ~30 min)

Necesitas 4 cosas: **Telegram**, **Gmail**, **Google Sheets** y **GitHub**.

### 1) Crear el bot de Telegram
1. En Telegram abre **@BotFather** → `/newbot` → ponle nombre.
2. Copia el **token** que te da (algo como `123456:ABC...`). → será `TELEGRAM_TOKEN`.
3. Escríbele algo a tu nuevo bot (un "hola") para que exista una conversación.
4. Para tu **chat_id**: abre en el navegador
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca `"chat":{"id":123456789`.
   Ese número es `TELEGRAM_CHAT_ID`.

### 2) App Password de Gmail (cuenta `tucorreo@gmail.com`)
> Es la cuenta que **recibe** los correos del banco.
1. Activa la **verificación en 2 pasos** en esa cuenta de Google.
2. Ve a **myaccount.google.com → Seguridad → Contraseñas de aplicaciones**.
3. Crea una para "Correo" → te da 16 caracteres. → será `GMAIL_APP_PASSWORD`.
   (Así el bot entra por IMAP sin usar tu clave real.)

### 3) Google Sheets + cuenta de servicio
1. Crea una planilla nueva en Google Sheets. De su URL copia el **ID**
   (`docs.google.com/spreadsheets/d/`**`ESTE_ID`**`/edit`). → será `SHEET_ID`.
   No hace falta crear pestañas: el bot crea "Movimientos", "Resumen", "Config" y
   "Categorías" solo.
2. Ve a **console.cloud.google.com** → crea un proyecto.
3. Habilita **Google Sheets API** (APIs y servicios → Biblioteca).
4. Crea una **cuenta de servicio** (IAM → Cuentas de servicio) → **Claves → Agregar
   clave → JSON**. Se descarga un archivo `.json`. → su contenido será `GOOGLE_CREDENTIALS`.
5. Abre ese JSON, copia el campo `client_email` (algo como
   `...@...iam.gserviceaccount.com`) y **comparte tu planilla con ese correo como Editor**
   (botón Compartir en la Sheet). ⚠️ Sin esto el bot no puede escribir.

### 4) Subir a GitHub y configurar los secretos
1. Crea un repositorio **privado** en GitHub (ej: `bot-finanzas`).
2. Sube estos archivos (desde esta carpeta):
   ```bash
   git init
   git add .
   git commit -m "Bot de finanzas inicial"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/bot-finanzas.git
   git push -u origin main
   ```
3. En el repo: **Settings → Secrets and variables → Actions → New repository secret**.
   Crea uno por cada valor:

   | Secret | Valor |
   |---|---|
   | `GMAIL_USER` | `tucorreo@gmail.com` |
   | `GMAIL_APP_PASSWORD` | los 16 caracteres del paso 2 |
   | `BANK_SENDERS` | `avisos@tubanco.cl,transferencias@tubanco.cl,notificaciones@otrobanco.cl` |
   | `TELEGRAM_TOKEN` | token de BotFather |
   | `TELEGRAM_CHAT_ID` | tu chat id |
   | `GOOGLE_CREDENTIALS` | **todo** el contenido del archivo JSON |
   | `SHEET_ID` | id de la planilla |
   | `BILLING_DAY` | `22` |
   | `DEST_ACCOUNT` | tu número de cuenta, solo dígitos (ej: `001234567890`) |
   | `CARD_MAP` | (opcional) `1234:credito` |
   | `RESUMEN_HORA` | (opcional) hora del resumen nocturno, 0–23 (por defecto `22`) |

4. Ve a la pestaña **Actions**, elige "Bot Finanzas" y dale **Run workflow** para
   probar de inmediato (sin esperar los 5 min). La primera vez es necesario
   ejecutarlo a mano una vez para que el cron programado empiece a correr solo.

---

## Probar en tu PC antes de subir (opcional)
```bash
pip install -r requirements.txt
cp .env.example .env      # y rellena tus datos
python probar_local.py
```
Para probar solo el parser con un correo pegado:
```bash
python probar_local.py --solo-parser
```

---

## Estructura
```
src/
  config.py        # lee los secretos/variables de entorno
  email_reader.py  # IMAP: trae correos por remitente y por asunto
  parser.py        # compras, transferencias enviadas e ingresos (multi-banco)
  billing.py       # ciclo de facturación (día 22) y reparto de cuotas
  sheets.py        # lee/escribe Google Sheets (ids estables, estado, categorías)
  telegram_bot.py  # envía avisos con botones, edita mensajes y lee updates
  main.py          # orquesta todo (esto ejecuta GitHub Actions)
.github/workflows/finanzas.yml   # el cron cada 5 min
```

## Notas
- **Todos los movimientos usan el mismo ciclo de facturación (día 22):** una compra
  o transferencia del 26/07 cae en el ciclo de *agosto*, no en julio calendario.
  Así crédito, débito, transferencias e ingresos de una fecha suman/restan al mismo
  total. **Crédito** reparte cada cuota en su ciclo; **ingresos** restan.
- **Ingresos en negativo:** en la hoja `Movimientos` el `monto` de los ingresos se
  guarda negativo, así al seleccionar la columna la suma da el gasto neto real. Los
  cálculos del bot usan el *tipo*, no el signo. Los montos van pintados: **ingresos
  en verde, gastos en rojo**.
- **Categorías:** columna `categoria` en `Movimientos` + hoja `Categorías`
  (Categoría × Mes). Se asignan con los botones del aviso o escribiendo una nueva.
- Cada movimiento tiene un `id` estable y una columna `estado` (activo/anulado);
  anular (`/eliminar`) nunca borra la fila, se puede `/restaurar`.
- Para agregar el formato de un banco nuevo, se edita `src/parser.py` (compras en
  `PATRONES`/`RE_COMPRA`, ingresos en `INGRESO_BANCOS`) y se suma su remitente al
  secret `BANK_SENDERS`. Hay prueba lista en `probar_local.py --solo-parser`.
- **Los cron de GitHub son "mejor esfuerzo":** pueden atrasarse varios minutos e
  incluso saltarse corridas cuando hay mucha carga. La primera vez hay que lanzar
  el workflow a mano una vez para que el cron programado empiece.
- Todo es gratis: repo **público** = minutos de Actions ilimitados. (Privado:
  2.000 min/mes.)
