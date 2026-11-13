Detector apagado el 13 nov 2026 por decisión del propietario; el repo se conserva como base para un futuro servidor.

# Detector de explosiones (Hyperliquid)

Este programa mira los perpetuos de Hyperliquid (el mercado principal y los de HIP-3, nunca el spot) y avisa cuando tres cosas técnicas se cumplen a la vez en una hora. No compra ni vende. Solo avisa.

GitHub lo ejecuta cada 15 minutos. El horario y las pasadas de cada arranque no han cambiado.

Lo único que llama al webhook, y por tanto despierta el análisis de pago, es un **corto** `confluence_1h`. Un largo que cumple las tres cosas se anota y no se envía.

## Qué se mira

En cada pasada entran **todos** los perpetuos con volumen de 24 horas de al menos **5 millones de dólares**. En un mercado HIP-3, además, el interés abierto tiene que ser de al menos **1 millón**. El spot no se pide.

La hora no espera a que el reloj la cierre. Se arma con las **últimas 4 velas de 15 minutos ya cerradas** (una hora entera de precio y de volumen) y se compara con las velas de 1 hora ya cerradas que terminan antes de que empiece esa ventana. A en punto, esa hora corrida es la hora que acaba de cerrar. A y cuarto, y media y menos cuarto, la ruptura se ve en ese cuarto de hora. El volumen no se prorratea: un trozo de hora multiplicado por cuatro convertiría un pico de 15 minutos en un falso clímax.

Hace falta las tres cosas, en largo:

1. **Tendencia.** El cierre de esa hora está por encima de la EMA20 de 1h, y la EMA20 está por encima de la EMA50.
2. **Ruptura.** Ese cierre está por encima del máximo de las 20 velas de 1h cerradas anteriores (canal de Donchian).
3. **Volumen con presión agresiva.** El nocional de esa hora es al menos **1,5 veces** la media de esas 20 horas, y en esa hora el agresor compró más de lo que vendió (delta: compras menos ventas).

El corto es el espejo (cierre por debajo de la EMA20, EMA20 por debajo de la EMA50, cierre por debajo del mínimo de 20, el mismo volumen y más venta agresiva que compra) y, además, el funding tiene que estar **por encima de −0,01%/h**. Si el funding no viene o es igual o más negativo, el corto no sale. El largo no mira el funding. El largo se escribe en el CSV con estado `shadow_long` y **no se envía**: no gasta uno de los 8 despertares ni abre la pausa de 4 horas de esa moneda. La misma fila no se repite hasta pasadas 4 horas.

El delta no viene en las velas. `recentTrades` solo devuelve los últimos intercambios de unos segundos, así que no se usa como si fuera la hora. Cada ejecución escucha la cinta pública (`trades` por websocket; el lado B es compra agresiva y el lado A es venta). La cobertura de una moneda **empieza cuando el servidor confirma la suscripción**, no cuando se envía: si no hay confirmación, esa hora no cuenta como vista. Si la cinta ha visto al menos el **95%** de la hora, con un hueco de como mucho **3 minutos**, el delta es el de esa cinta (`delta_source: "tape"`). En GitHub el trabajo dura una hora y luego se apaga, y el siguiente a menudo arranca minutos después: esa hora no llega al 95%. Entonces no se inventa un delta en dólares. La presión se mira en el cierre de la propia hora (el close location de Williams: +1 es el máximo, −1 el mínimo). Un largo pide un cierre en la mitad alta, un corto en la mitad baja. El aviso lo dice con `delta_source: "close_location"` y `delta` a null. Un cierre justo en el medio, o una vela sin rango, no pasa.

Tampoco se avisa si el precio de ahora ya está a más de **2 ATR** de 1 hora más allá del nivel de ruptura. Dos es el mismo múltiplo que el stop mínimo: más lejos que eso, la entrada llega tarde. Como un aviso válido está como mucho a 2 ATR del nivel, el stop de 2 ATR queda en el nivel o por debajo. Si el último giro queda más lejos, el stop se abre hasta ese giro.

Cada moneda espera **4 horas** después de un POST. Hay como máximo **8 despertares al día**: cada POST es un despertar, aunque lleve varias monedas. Un largo no cuenta. De **00:00 a 08:00** (Madrid) no hay POST. Un corto se anota `quiet` y, si a las 8 la hora sigue siendo válida, puede salir entonces. La pausa de 4 horas no se gasta de noche ni con un largo.

El aviso lleva la moneda, el dex, el lado (`short`), las tres confluencias (EMA20, EMA50, nivel Donchian, múltiplo de volumen y delta), la marca, el volumen de 24 horas, el interés abierto, el funding, la prima, el ATR, una entrada (la marca de ahora) y el stop. El stop no ha cambiado: como mínimo 2 ATR, y más lejos si el nivel de ruptura o el último giro quedan más allá. El objetivo que manda es **TP1 a 2R**: dos veces la distancia de la entrada al stop, hacia el lado del corto. TP2 queda a 3R. En el webhook y en el CSV, `tp1` es ese 2R, `rr` es 2, `tp2` es el 3R, `rr_tp2` es 3 y `tp_kind` es `2r`. El largo, que solo se anota, sigue llevando el objetivo estructural de antes (`estructural`, `mixto` o `medido`).

La fase 1 antigua (el salto de 1 hora, el de 15 minutos y la escalera) puede seguir saliendo en la tabla del ciclo, pero **no llama al webhook**. Las señales tempranas (absorción, volumen relativo, compresión, interés abierto, delta de absorción y funding) están apagadas: no se calculan y no hay repaso de sombra del 24 nov. Las preparaciones (`setup_scanner`) y el aviso `entry_ready` también están apagados.

El recuento está en `data/confluence/` de la rama `detector-state`: una fila por corto enviado, largo anotado (`shadow_long`), silencio, sin delta, extendido o parado por el cupo. Se guardan 21 días.

El estudio de velas de 1h de los últimos 14 días (sin delta, y solo en los 30 perps líquidos con más volumen, para caber en el cupo de la API) está en `reports/confluence_replay_2026-11-11.md`. El directo mira todos los que pasan el filtro, no solo esos 30.

## Qué tienes que hacer tú

1. En este pull request, pulsa **Merge** para juntarlo con `main`. Hasta que no esté en `main`, el horario no arranca.
2. Crea los dos secretos de abajo. Sin ellos el detector sigue mirando, pero no envía nada (en el registro pone `dry-run`). Un dry-run de una confluencia de verdad cuenta como despertar del día, para no repetir el mismo aviso cada cuarto de hora.
3. Permite que Actions escriba en el repositorio. Abajo está el sitio exacto.
4. Lanza la prueba manual del webhook.
5. Cuando esa prueba salga bien, **para la copia del ordenador**. Si las dos siguen activas, el aviso puede llegar dos veces.

### Los dos secretos

```
EXPLOSION_WEBHOOK_URL
EXPLOSION_WEBHOOK_AUTH
```

Dónde crearlos: este repositorio → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

- **EXPLOSION_WEBHOOK_URL**: solo la dirección, la que empieza por `https://`.
- **EXPLOSION_WEBHOOK_AUTH**: la línea entera de la cabecera, por ejemplo `Authorization: Bearer xxxxx`.

Sin comillas. El programa no escribe la dirección ni el token en el registro. No los pongas en este README, en el código, ni en una issue: el repositorio es público.

### Permiso para guardar la memoria

1. **Settings** → **Actions** → **General**.
2. En **Workflow permissions**, elige **Read and write permissions**.
3. Pulsa **Save**.

Sin esto, el paso **Guardar estado** falla con un 403 y cada ejecución empieza de cero.

### Probar el webhook a mano

1. **Actions** → **Detector de explosiones** → **Run workflow**.
2. Rama **main**. Marca **Enviar un POST de prueba al webhook**.
3. En el job **detectar**, paso **Pasadas del detector**:
   - `force_test status=posted` → el webhook ha respondido.
   - `force_test status=dry_run` → faltan los secretos.
   - `force_test status=post_failed` con HTTP 401 o 403 → la cabecera no es la que espera el webhook.

La prueba no gasta el cupo ni la pausa, y se puede lanzar de madrugada. Manda un solo objeto de prueba:

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

Un aviso de verdad es una lista. Si en el mismo cuarto de hora saltan varias monedas, van todas en ese único POST. `funding_pct` y `premium_pct` van en porcentaje por hora. `delta` es el nocional comprado menos el vendido en esa hora. `delta_coverage` es la fracción de la hora que la cinta llegó a ver.

```json
[
  {
    "type": "confluence_1h",
    "coin": "BTC",
    "dex": "main",
    "side": "short",
    "side_label": "corto",
    "ema20": 98420.5,
    "ema50": 97110.2,
    "donchian": 99150.0,
    "vol_mult": 1.8,
    "delta": -12500000.0,
    "delta_source": "tape",
    "close_location": -0.64,
    "buy_ntl": 35500000.0,
    "sell_ntl": 48000000.0,
    "delta_coverage": 0.98,
    "mark": 99540.0,
    "dayNtlVlm": 2500000000.0,
    "oi_usd": 1800000000.0,
    "funding_pct": 0.00125,
    "premium_pct": -0.0364,
    "atr": 420.0,
    "entry": 99540.0,
    "sl": 100380.0,
    "sl_kind": "2atr",
    "tp1": 97860.0,
    "tp2": 97020.0,
    "rr": 2.0,
    "rr_tp2": 3.0,
    "tp_kind": "2r",
    "extension_atr": 0.93,
    "detected_at_madrid": "2026-06-15 14:15"
  }
]
```

## Cómo leer una ejecución

En **Actions** → **Detector de explosiones** → job **detectar** → **Pasadas del detector**.

La primera tabla es la fase 1, que ya no despierta nada. Debajo está la de la confluencia, que sí puede hacer POST.

| Estado | Qué ha pasado |
| --- | --- |
| `posted` | Se envió el webhook. Solo cortos. |
| `dry_run` | Había un corto, pero faltan los secretos. Cuenta como despertar y deja la pausa de 4 horas. |
| `shadow_long` | Largo con las tres confluencias. Anotado, sin POST. No gasta el cupo ni la pausa. |
| `quiet` | Corto de noche. Anotado, sin POST. No gasta la pausa. |
| `tope` | Ya van 8 despertares hoy. |
| `post_failed` | El POST falló. No gasta cupo ni pausa. |
| `sin_delta` | Precio y volumen sí, pero la cinta no cubre la hora. No se envía. |
| `sin_presion` | Hay delta, pero no empuja el lado de la ruptura. No se envía. |
| `extendido` | El precio ya está a más de 2 ATR del nivel. |
| `veto_funding` | El corto no pasa el funding. |

```
confluencia universo=78 velas=140 filas=1 enviados=1 status=posted
```

`enviados` es el número de monedas del POST. El cupo suma 1 por POST. La fase 1, cuando anota, puede decir `watch_only`: eso no es un envío.

## Dónde se guarda la memoria

En la rama **`detector-state`**, reescrita en cada pasada. No lleva secretos.

- `data/detector_state.json` — pausas, cupo y la cinta agregada
- `signals/explosion_alerts.jsonl` — historial corto
- `data/scans/` — tabla de la fase 1 (14 días)
- `data/confluence/AAAA-MM-DD.csv` — filas de confluencia (21 días)
- `LEEME.txt`

`data/shadow/` ya no se escribe. Si un ciclo no lee Hyperliquid, no toca esa memoria.

## Horario

El cron es `*/15 * * * *` (UTC). En este repositorio público GitHub a menudo arranca el horario solo una vez por hora, así que cada arranque hace **5 pasadas**: al empezar, y luego en el :00, :15, :30 y :45 más 20 segundos, sin pasar de unos 60 minutos. El tope del job es 70 minutos.

Durante el trabajo, un proceso aparte escucha la cinta. Cada pasada la guarda. Si la escucha se cae, el ciclo sigue y esas horas quedan en `sin_delta`.

Después de cada pasada se guarda la rama `detector-state`. Si llega otro trabajo, espera; el que corre, al terminar una pasada, le deja el sitio.

En un repositorio público GitHub apaga los horarios tras 60 días sin actividad. Guardar estado es un commit, así que mientras el detector funcione no deberían cumplirse. El horario solo corre con el fichero en `main`.

## Si algo falla

- `dry-run` → revisa los dos secretos.
- HTTP 401 o 403 → vuelve a pegar `EXPLOSION_WEBHOOK_AUTH`, la línea completa.
- **Guardar estado** con 403 → falta **Read and write permissions**.
- No hay ejecuciones → el pull request no está en `main`, o el horario se apagó.
- Muchas filas `sin_delta` al principio → la cinta todavía no ha cubierto una hora. No se envían.
