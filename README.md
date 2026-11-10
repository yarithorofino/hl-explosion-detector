# Detector de explosiones (Hyperliquid)

Este programa mira los perpetuos de Hyperliquid (el mercado principal y los de HIP-3) y avisa cuando uno se mueve de golpe con mucho volumen. No compra ni vende nada. Solo avisa.

Antes corría en un ordenador que se duerme. Ahora lo ejecuta GitHub cada 15 minutos, también con el ordenador apagado.

Hay dos fases. La primera avisa del impulso (las reglas de abajo, más una señal temprana solo en nombres con al menos 1 millón de dólares de volumen en 24 horas, y solo si varias pistas coinciden). Al mismo tiempo guarda una vigilancia. Si el aviso es un impulso ya hecho, la zona es el retroceso del 38% al 50%, con invalidación y dos objetivos. Si el aviso es absorción (volumen alto con el precio quieto), la vigilancia es de urgencia baja: no pide entrar todavía, espera a que el precio salga de ese rango. La segunda fase avisa `entry_ready` solo si esa entrada aparece y el cobro hasta el primer objetivo es al menos el doble del riesgo. Ese aviso no gasta el cupo de 2 por hora ni el de 10 al día; no se repite para el mismo par y el mismo lado. De noche (00:00–08:00, Madrid) la fase 1 no llama al webhook. La fase 2 tampoco, salvo que en la configuración se ponga `entry_ready_respect_quiet_hours` a false. El estudio de la semana del 10 nov 2026 está en `reports/replay_2026-11-10.md`.

Avisa igual si el precio sube o si baja. Salta si se cumple **cualquiera** de estas reglas (el aviso dice cuáles):

- **Hora contra su propio rango (1h_atr).** El movimiento de la última hora es al menos **3 veces** el ATR de 14 velas de 1 hora (el rango típico, en %) y, además, no baja de un suelo: **3%** si es un grande y **5%** en el resto. El volumen de esa vela es al menos **5 veces** la media de las 24 horas anteriores y al menos **100.000 dólares**.
- **Hora fija (1h_legacy).** El movimiento de la última hora es de al menos **8%**, con el mismo volumen (5 veces la media y 100.000 dólares). Sirve cuando la moneda ya iba muy movida y el ATR pide un salto enorme.
- **Quince minutos (15m_early).** El movimiento de la vela de 15 minutos es de al menos **4%**, con un volumen de al menos **5 veces** la media de las velas de 15 minutos de las 24 horas anteriores y al menos **25.000 dólares**.
- **Escalera de 3 horas (3h_escalera).** La suma del recorrido de las 3 últimas velas de 1 hora (de la apertura de la primera al cierre de la última) es de al menos **10%**. Al menos 2 de esas 3 cierran en esa dirección y marcan máximos más altos (o mínimos más bajos). No pide múltiplo de volumen. El aviso lleva `label: "escalera"`. `tradable` sigue la regla del millón. De noche se calla igual que el resto, salvo que en la configuración se ponga `escalera_ignore_quiet_hours` a true.
- **Absorción (absorption).** Solo si el volumen de 24 horas es de al menos **1 millón**. La vela de 1 hora mueve al menos **8 veces** la mediana y **8 veces** la media de las 48 horas anteriores, el cierre a cierre se queda por debajo del **1%** y el rango de la vela por debajo del **2%**. Una sola vela no avisa. Si se repite dentro de esas 48 horas, el aviso sale con `urgency: "baja"`: es la vigilancia del rango, no una orden de entrar. La dirección sale de dónde cierra la vela dentro de su rango, o del desequilibrio de compradores y vendedores agresivos cuando esa muestra existe. Los nombres líquidos que no se están moviendo se turnan en las pasadas, para que un libro justo en el millón no se quede sin mirar.

Grandes, con suelo del 3%: BTC, ETH y SOL (el nombre exacto del mercado) y, en el mercado principal, cualquier perp cuyo volumen de 24 horas sea de **100 millones de dólares** o más. Un mercado HIP-3 no se vuelve grande solo por volumen. La lista y el umbral están en `data/detector_config.json`.

Para poder avisar hace falta liquidez: volumen de 24 horas de al menos **300.000 dólares**. Si no es el mercado principal (HIP-3), además el interés abierto tiene que ser de al menos **500.000 dólares**.

Si el volumen de 24 horas es de menos de **1 millón**, el aviso sale igual, pero marcado `tradable: false` y `label: "vigilancia, no operable"`. A partir de 1 millón va `tradable: true` y `label: "operable"`.

Si la mecha de rechazo de la vela que dispara (la de arriba cuando sube, la de abajo cuando baja) ocupa más del **50%** del rango, el aviso lleva `exhaustion_flag: true` y `exhaustion_label: "posible agotamiento"`.

Si en el mismo cuarto de hora saltan varios, van todos en **un solo aviso**. El mismo activo no se repite hasta pasadas **4 horas**. Como máximo hay **10 avisos al día** y **2 por hora** (cada POST cuenta como uno, aunque lleve varios activos). La escalera cuenta igual para esos cupos. Tiene su propia pausa de 4 horas, así un aviso de escalera no impide, una hora después, el aviso del 8% o el de 15 minutos. De **00:00 a 08:00** (hora de Madrid) anota la señal pero no llama al webhook; a las 8 ya puede avisar. La prueba manual no gasta esos cupos.

## Qué tienes que hacer tú

1. En este pull request, pulsa **Merge** para juntarlo con `main`. Hasta que no esté en `main`, el horario no arranca.
2. Crea los dos secretos de abajo. Sin ellos el detector sigue mirando el mercado, pero no envía nada (en el registro pone `dry-run`).
3. Permite que Actions escriba en el repositorio (si no, el último paso falla y el detector olvida lo que vio). Abajo está el sitio exacto.
4. Lanza la prueba manual del webhook.
5. Cuando esa prueba salga bien, **para la copia del ordenador**. Si las dos siguen activas, el aviso puede llegar dos veces cuando el ordenador esté despierto.

### Los dos secretos

Los nombres tienen que ser exactamente estos, en mayúsculas:

```
EXPLOSION_WEBHOOK_URL
EXPLOSION_WEBHOOK_AUTH
```

Dónde crearlos:

1. Abre este repositorio en GitHub.
2. **Settings** (en la interfaz en español: **Configuración**).
3. En el menú de la izquierda, **Secrets and variables** (**Secretos y variables**).
4. **Actions**.
5. **New repository secret** (**Nuevo secreto de repositorio**).

Qué pegar en cada uno:

- **EXPLOSION_WEBHOOK_URL**: solo la dirección del webhook, la que empieza por `https://`.
- **EXPLOSION_WEBHOOK_AUTH**: la línea entera de la cabecera, tal como la copias del panel. Por ejemplo:

```
Authorization: Bearer xxxxx
```

Sin comillas. Puedes dejar el espacio de después de los dos puntos; el programa lo recorta. No pongas la dirección ni el token en este README, en el código, ni en un mensaje o una issue: este repositorio es público y cualquiera podría leerlo. GitHub guarda los secretos cifrados y el programa no los escribe en el registro.

### Permiso para guardar la memoria

1. **Settings** → **Actions** → **General**.
2. En **Workflow permissions**, elige **Read and write permissions**.
3. Pulsa **Save**.

Sin esto, el paso **Guardar estado** falla con un error 403 y cada ejecución empieza de cero.

### Probar el webhook a mano

1. Pestaña **Actions**.
2. A la izquierda, **Detector de explosiones**. Si GitHub pide activar Actions la primera vez, acéptalo.
3. **Run workflow** (en español: **Ejecutar workflow**).
4. Deja la rama en **main**.
5. Marca la casilla **Enviar un POST de prueba al webhook**.
6. Pulsa el botón verde para lanzarlo.
7. Entra en esa ejecución. Arriba sale un resumen en castellano. Abre el job **detectar** y el paso **Pasadas del detector**. La casilla de prueba hace **una sola pasada**, no el bucle de una hora.
8. Busca esta línea:
   - `force_test status=posted` → el webhook ha respondido. La prueba ha llegado.
   - `force_test status=dry_run` → faltan los secretos, o el nombre no coincide.
   - `force_test status=post_failed` y cerca `webhook error: HTTP 401` o `403` → la línea del secreto `EXPLOSION_WEBHOOK_AUTH` no es la que espera el webhook. Vuelve a copiarla.

La prueba manda una lista con un solo objeto que dice que es una prueba, no un movimiento real. No gasta el cupo del día ni el de la hora, y se puede lanzar de madrugada.

Ejemplo de lo que recibe el webhook en la prueba:

```json
[
  {
    "test": true,
    "source": "hl-explosion-detector",
    "message": "POST de prueba manual",
    "detected_at_madrid": "2026-06-15 14:15"
  }
]
```

Un aviso de verdad tiene esta forma (varios activos irían uno detrás de otro, en la misma lista, en un solo POST). `funding_pct` y `premium_pct` van en porcentaje por hora (el dato de Hyperliquid es un decimal; 0,0000125 pasa a 0,00125%). `oi_change_1h_pct` es el cambio del interés abierto frente a hace una hora, o `null` si todavía no hay ese recuerdo. `trigger` junta los que hayan saltado: `1h_atr`, `1h_legacy`, `15m_early` y `3h_escalera`. `pct_3h` es el recorrido de esas 3 velas. `floor_pct` es el suelo de 1 hora de ese activo (3 o 5). `early_floor_pct` es el 4% del disparo de 15 minutos, cuando ese disparo ha saltado.

```json
[
  {
    "dex": "main",
    "coin": "BTC",
    "trigger": "1h_atr",
    "pct_1h": 4.2,
    "pct_15m": 1.1,
    "atr_pct": 0.8,
    "floor_pct": 3.0,
    "early_floor_pct": null,
    "vol_1h_usd": 2500000,
    "vol_mult": 6.2,
    "mark": 100000,
    "dayNtlVlm": 80000000,
    "oi_usd": 12000000,
    "oi_change_1h_pct": 3.5,
    "funding_pct": 0.00125,
    "premium_pct": -0.0364,
    "wick_ratio": 22.0,
    "exhaustion_flag": false,
    "exhaustion_label": null,
    "tradable": true,
    "label": "operable",
    "maxLeverage": 40,
    "detected_at_madrid": "2026-06-15 14:15"
  }
]
```

## Cómo leer una ejecución normal

En **Actions** → **Detector de explosiones** → la ejecución → job **detectar** → paso **Pasadas del detector**. Si el trabajo ha hecho varias pasadas, el resumen de arriba repite la tabla, una por cuarto de hora.

Arriba del run, el resumen en castellano trae la hora de Madrid, cuántos perpetuos miró, cuántos candidatos hubo, cuántas velas pidió y una tabla. Cada fila es un candidato, con dex, moneda, movimiento de 15 minutos, de 1 hora, de 3 horas y de 24 horas, el múltiplo de volumen de 1 hora, el volumen de 24 horas, el interés abierto, el funding, el ATR, el suelo, qué disparo ha saltado (`disparo`) y el resultado:

| Resultado | Qué ha pasado |
| --- | --- |
| `alerta` | Cumple y entra en el aviso de esta tanda. |
| `no llega` | Se miró y no cumple. |
| `pausa` | Cumpliría, pero ese activo avisó hace menos de 4 horas. |
| `silencio` | Cumple, pero es de noche (00:00–08:00). Queda anotado y no se envía. |
| `tope` | Cumple, pero ya van 10 avisos hoy o 2 en esta hora. |
| `sin velas` | La API no devolvió las velas de ese nombre. |
| `fuera del top` | Pasó el filtro previo, pero este ciclo solo pide velas de los 25 más movidos. |

La misma tabla queda guardada para mirarla después (ver más abajo). En el texto del paso también hay una línea corta:

```
candidatos=12 velas=24 hits=1 enviados=1 status=posted ['XYZ']
```

Qué significa `status`:

| Valor | Qué ha pasado |
| --- | --- |
| `none` | Nada que avisar. |
| `posted` | El webhook ha respondido bien. |
| `dry_run` | Había una señal, pero faltan los secretos. Aun así cuenta como aviso del día y de la hora. |
| `post_failed` | Se intentó enviar y falló. No cuenta: si la señal sigue, lo reintenta en el siguiente ciclo. |
| `quiet_hours_logged` | Es de noche. Queda anotado y no se envía. |
| `daily_cap_logged` | Ya se han enviado los 10 avisos de hoy. |
| `hourly_cap_logged` | Ya se han enviado los 2 avisos de esta hora. |

Si hay muchos activos moviéndose a la vez, solo pide velas de los **25** más movidos (vela de 1 hora y vela de 15 minutos). Para no perderse un salto de 15 minutos ni una escalera lenta, entra como candidato quien se haya movido un **3%** desde la pasada anterior (unos 15 minutos), un **2%** frente a hace una hora, un **6%** frente a hace unas 3 horas, o un **8%** en 24 horas. En la primera ejecución todavía no hay precios guardados, así que solo entra el movimiento de 24 horas; a partir de la segunda ya compara con el cuarto de hora anterior.

## Dónde se guarda la memoria

Entre una ejecución y la siguiente hay que recordar los precios anteriores, qué activo está en pausa de 4 horas y cuántos avisos van hoy. Eso no puede vivir en el disco del ordenador, y tampoco conviene un commit en `main` cada 15 minutos.

Se guarda en una rama aparte, **`detector-state`**. Esa rama solo tiene el último recuerdo (se reescribe cada vez), así el historial del proyecto no se llena. Dos ejecuciones no pueden escribir a la vez: GitHub deja correr solo una, y al guardar se comprueba que nadie haya escrito en medio. Si la comprobación no cuadra, no se pisa lo que ya había.

En esa rama están:

- `data/detector_state.json` — la memoria (precios, interés abierto, pausas y cupos)
- `signals/explosion_alerts.jsonl` — un historial corto de avisos (como mucho unos cientos de líneas)
- `data/scans/latest.md` — la tabla del último ciclo, igual que el resumen de Actions
- `data/scans/AAAA-MM-DD.csv` — una fila por candidato y por ciclo, para estudiarlo en una hoja de cálculo. Solo se guardan los últimos **14 días**
- `LEEME.txt` — una nota que dice que no hay que editar esos ficheros a mano

No hace falta abrirla para el día a día. El registro de Actions basta. La rama es pública, como el resto del repositorio: tiene movimientos de mercado, no la URL ni la clave del webhook.

Si un ciclo no consigue leer Hyperliquid, no toca esa memoria. El fallo se ve en rojo y el recuerdo anterior sigue igual.

## Horario

El reloj de GitHub va en UTC y el fichero pide un arranque cada 15 minutos (`*/15 * * * *`). En este repositorio público GitHub a menudo **no lo arranca cada cuarto de hora**: las ejecuciones automáticas pueden quedar separadas unos 60 minutos (por ejemplo 03:33, 04:30, 05:35).

Por eso cada arranque no mira el mercado una vez: hace **5 pasadas**. La primera es en cuanto empieza. Luego espera al siguiente minuto :00, :15, :30 o :45, más 20 segundos para que la vela de 15 minutos ya esté cerrada, y vuelve a mirar. Con cinco pasadas entra también el :45; con cuatro el trabajo se quedaba en el :30 y el :45 quedaba sin vigilar unos 25 minutos. No empieza una pasada nueva si el trabajo ya lleva unos 60 minutos. El tope del job es 70 minutos, para que la última pasada pueda terminar. Un arranque a las 08:55 mira hacia las 08:55, 09:00, 09:15, 09:30 y 09:45.

Después de **cada** pasada se guarda la memoria en la rama `detector-state` (pausas, cupos, precios) y la fila del CSV. La siguiente pasada vuelve a leer esa rama, así no se repite un aviso ni se saltan los topes (10 al día, 2 por hora, 4 horas por activo, silencio de 00:00 a 08:00).

Si GitHub llega a lanzar otro trabajo mientras este sigue en marcha, el nuevo espera. No corta al que está guardando. Cuando el que corre termina una pasada y ve que hay otro en la cola, deja el sitio: no duerme hasta el siguiente cuarto de hora. El que esperaba sigue desde ese momento. Así no se barren dos veces los mismos cuartos, y si aun así coinciden un instante, la pausa de 4 horas evita el aviso duplicado.

Para lanzarlo a mano: **Run workflow**. El número de pasadas sale en 5. Márcalo a 1 si solo quieres un vistazo. La casilla del POST de prueba ignora ese número y hace una sola pasada.

En un repositorio público, GitHub **apaga los horarios si pasan 60 días sin actividad** en el repositorio. Cada ejecución que guarda estado deja un commit en `detector-state`, y un commit es actividad, así que mientras el detector funcione no deberían cumplirse esos 60 días. Si aun así lo apaga (suele avisar por correo), la pantalla de Actions enseña un aviso y un botón **Enable workflow** (**Habilitar workflow**). Pulsa ese botón y, si quieres, lanza una ejecución manual para no esperar al siguiente cuarto de hora.

El horario solo funciona con el fichero en `main`. En repos públicos, los minutos de Actions no se cobran.

## Si algo falla

- El paso **Pasadas del detector** dice `dry-run` → revisa que los dos secretos existan y que los nombres sean exactos.
- `webhook error: HTTP 401` o `403` → vuelve a pegar `EXPLOSION_WEBHOOK_AUTH`, la línea completa, sin comillas.
- **Guardar estado** en rojo con `403` o `Permission denied` → falta el permiso **Read and write permissions** de la sección de arriba.
- No aparece ninguna ejecución → el pull request no está fusionado en `main`, Actions está desactivado, o el horario se apagó por los 60 días.

Para volver a lanzarlo a mano: **Actions** → **Detector de explosiones** → **Run workflow**. Sin la casilla hace las pasadas que pongas (5 si no cambias el número). Con la casilla manda el POST de prueba y solo hace una pasada.
