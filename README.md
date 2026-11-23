Detector apagado el 13 nov 2026 por decisión del propietario; el repo se conserva como base para un futuro servidor.

El paper (trend4h, squeeze4h, trapped_shorts1h y c1_maker) es otro programa en el mismo repo. No compra ni vende. El horario está en `.github/workflows/trend4h.yml`.

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

## Paper: tendencia, squeeze, trapped shorts y c1

Cuatro libros de papel, separados. trend4h, squeeze4h y trapped_shorts1h arriesgan el **0,25%** de **10.000 USDC** de papel por operación (25 USDC hasta el stop inicial). La comisión de esos tres es **0,07%** por lado. c1_maker arriesga el **0,25%** de **1.000 USDC** (2,50 USDC) y usa las comisiones de abajo. El funding horario se suma aproximadamente en los tres primeros, sobre el nocional de la entrada: si el funding es positivo, el largo lo paga y el corto lo cobra. El R de esos tres es el resultado neto dividido por esos 25 USDC. c1_maker no suma funding: su R sale solo del precio y de las comisiones maker y taker. No hay claves de exchange y no se envía ninguna orden. La API que se usa es la info pública.

Los cuatro comparten el workflow **Paper 4h** (`cron: 2 * * * *`, a las :02 UTC) y la rama `detector-state`. Cada hora se añade una fila por perp a `data/market_log/AAAA-MM-DD.csv` (moneda, mark, OI en contratos, OI en dólares, funding, premium, volumen de 24h, dex). Se guardan 120 días. Esas columnas no cambian: trapped_shorts1h sigue leyendo el OI en contratos de ese fichero. La lógica de 4h solo corre cuando hay una vela de 4h recién cerrada (cierra a las 0, 4, 8, 12, 16 y 20 UTC). Si el job llega tarde, procesa la vela que ya esté cerrada; no se la salta por no ser exactamente las :02.

### Registro para investigación

Después de los libros, la misma pasada escribe tres registros más. Si uno falla, el fallo queda en el log y los libros no se tocan. No hay claves: solo `https://api.hyperliquid.xyz/info` y el websocket público de trades.

**Libro.** `data/orderbook_log/AAAA-MM-DD.csv`, una fila por perp y hora. Entran los 120 perps con más volumen de 24 horas y, además, cualquier perp del dex principal o de un HIP-3 con más de 1 millón de dólares. El 16 nov 2026 el universo listado era de unos 350 nombres y el corte dejaba 137. `l2Book` devuelve como mucho 20 niveles por lado. En BTC, el libro sin agregar no llega al 0,05% del mid, así que el spread en puntos básicos sale del libro a precisión completa y la profundidad sale del agregado más fino que todavía distingue esa banda y la cubre (`nSigFigs` 3 en BTC; 4 y 3, o 3 y 2, cuando el precio cae en un cubo más ancho). La profundidad es nocional (precio por tamaño) de bid y de ask dentro del 0,5%, del 1% y del 2% del mid. El desequilibrio es `(bid − ask) / (bid + ask)`. `complete_*` vale 1 cuando esos niveles llegan hasta el borde de la banda; si vale 0, la cifra es un mínimo porque el libro se cortó antes. Una banda más estrecha que el cubo se deja vacía. El precio de un nivel agregado es el del cubo, así que el borde puede quedar a un cubo de distancia. `sig_*` dice qué agregado se usó (`full` es el libro sin agregar). Se guardan 120 días.

**Listados.** Cada hora se compara el universo (dex principal y HIP-3, con el apalancamiento máximo) con `data/listings_snapshot.json`. La primera hora solo guarda la foto: no anota como alta todo lo que ya cotiza. Desde la siguiente, `data/listings_log.csv` añade una fila con la hora UTC en que se ve el cambio (`listing`, `delisting` o `max_leverage`). Si un dex no responde, sus monedas se copian de la foto anterior y no se dan de baja. Si `perpDexs` falla, los HIP-3 que no se han podido pedir se conservan igual.

**Liquidaciones.** Hyperliquid no tiene un feed público de liquidaciones de todo el mercado, usable desde un job horario.

- El websocket `trades` y `recentTrades` devuelven `coin`, `side`, `px`, `sz`, `hash`, `time`, `tid` y `users`. No hay marca de liquidación. Está así en el tipo `WsTrade` de la documentación y en un print real de `recentTrades` de BTC del 16 nov 2026.
- El campo `liquidation` (`liquidatedUser`, `markPx`, `method`) existe en los fills de una cartera (`userFills`, `userFillsByTime`) y en `userEvents`. Eso es por dirección, con un tope de usuarios en el websocket, y no cubre el mercado sin una lista de carteras. Este programa no la pide.

Lo que se guarda, y cómo leerlo:

- `data/liq_trades_log/AAAA-MM-DD.csv` es una liquidación real solo en este sentido: un trade del websocket público cuyo payload trae el campo `liquidation`. Hoy ese fichero sale vacío. Si el campo apareciera, cada fila sería un print visto durante la muestra, no el total de la hora. No se guardan las direcciones de `users`.
- `data/liq_tape_log/AAAA-MM-DD.csv` es la prueba de que la muestra corrió: una fila por hora con los segundos, las monedas, los trades vistos y cuántos traían marca. Son 3 minutos sobre las 40 monedas de más volumen, en paralelo con el resto de lecturas. Una muestra sin marcas no dice que no hubiera liquidaciones: dice que el print público no las señala.
- `data/liq_proxy_log/AAAA-MM-DD.csv` es un proxy, no una liquidación. `proxy` vale 1 cuando coinciden tres cosas: el OI en contratos cae al menos un 2% frente a la fila anterior de `market_log` (exactamente una hora antes; si falta esa fila, el hueco queda en `oi_gap_h` y `proxy` queda en 0), el rango de la vela de 1h recién cerrada, `(máximo − mínimo) / apertura`, es al menos un 1%, y el nocional de esa vela es al menos el doble de la media de las 24 horas cerradas anteriores (hacen falta al menos 12). Un cierre voluntario, un vencimiento o un hueco de la foto pueden dar el mismo dibujo. `oi_drop_pct`, `range_pct` y `vol_mult` van sueltas para poder repetir el cálculo con otros umbrales. No se rellena el pasado.

Para estudiarlos hace falta pandas. Los CSV viven en `detector-state`, no en `main`:

```
git fetch origin detector-state
git checkout origin/detector-state -- data/market_log data/orderbook_log data/liq_proxy_log data/liq_trades_log data/liq_tape_log data/listings_log.csv data/listings_snapshot.json
```

```python
import research_logs
books = research_logs.load_orderbook_log("data")
proxy = research_logs.load_liq_proxy_log("data")
```

Con el universo del 16 nov 2026 (137 nombres), el libro son unos 1–2 minutos (dos o tres `l2Book` por nombre, peso 2) y las velas del proxy unos 4–6 minutos (peso 20 más el extra por velas, con el cupo de 800/min que ya usa el cliente). La cinta de 3 minutos corre a la vez, así que no se suma entera. En total unos 6–12 minutos encima del paper, dentro del tope de 40 del workflow. Una fila realista del libro ocupa unos 200 bytes y una del proxy unos 150: cerca de 1,5 MB al día y unos 180 MB a los 120 días, además del `market_log`. La cinta y los listados no mueven esa cifra.

Un disparo manual con **test** manda, además, un solo POST `test: true` y `paper: true`. Ese objeto no abre ni cierra una posición. La pasada sigue haciendo el paper normal.

### trend4h

Universo fijo del dex principal: AAVE, ADA, ASTER, AVAX, BNB, BTC, CRV, DOGE, ENA, ETH, FARTCOIN, HYPE, LAUNCHCOIN, LINK, LIT, MKR, PAXG, PUMP, SOL, SUI, TAO, XPL, XRP, ZEC, kPEPE. Si una moneda no está en `metaAndAssetCtxs` o está delistada, se anota en el registro y se salta.

En velas de 4h ya cerradas: EMA100 y EMA200 del cierre (hacen falta al menos 600 velas) y ATR(14) de Wilder. Si la EMA100 cruza por encima de la EMA200, largo; si cruza por debajo, corto. La entrada de papel es la apertura de la vela de 4h siguiente. Si esa apertura todavía no está en la respuesta, se usa la marca del momento. El stop inicial es la entrada menos (o más, en corto) 3 ATR de la vela de la señal. No hay take profit.

Se sale si el máximo o el mínimo de una vela de 4h toca el stop (si la vela abre ya al otro lado, el fill es la apertura), si hay un cruce contrario (la salida y la entrada opuesta son la apertura siguiente: se da la vuelta) o al cumplir 180 velas (30 días). Como máximo 10 posiciones, una por moneda. Lo que no cabe se anota `skipped_cap` y no se envía.

### squeeze4h

Solo largos. Universo: perps del dex principal con volumen de 24h de al menos 5 millones de dólares. HIP-3 no entra. Se mira cuando cierra una vela de 4h.

La anchura de Bollinger(20, 2) es `(banda alta − banda baja) / media`, con desviación poblacional. `bwpct` es el percentil de esa anchura en las últimas 180 velas de 4h (de 0 a 1). Hace falta que el mínimo de las tres velas anteriores a la de disparo sea 0,05 o menos. El disparo: el cierre supera el máximo de las 20 velas anteriores y el volumen (el de la vela, en moneda) es al menos 1,5 veces la media de esas 20.

La salida es común con trapped shorts. Stop inicial: entrada − 3 ATR(14). El trailing es el máximo desde la entrada menos 5 ATR(14) de la vela que se acaba de cerrar, y solo sube. A los 240 velas se sale en la apertura siguiente. Máximo 10 posiciones, una por moneda.

### trapped_shorts1h

Solo largos, mismo universo líquido de 5 millones, sin HIP-3. Cada hora, sobre la vela de 1h que acaba de cerrar.

El interés abierto sale solo de `data/market_log` (contratos, no dólares). Hasta que ese registro no cubre 7 días el libro no abre nada y el registro dice `warming_up`. No se rellena el pasado. Cuando ya hay historia: el OI de ahora es al menos un 20% mayor que el de hace 168 horas, el precio de las últimas 168 velas de 1h está entre −15% y +5%, y la prima actual es negativa. El disparo: la vela cerrada sube al menos un 3% de la apertura al cierre, con volumen de al menos 3 veces la media de las 20 velas anteriores.

Stop inicial: entrada − 2 ATR(14). Trailing: máximo desde la entrada menos 4 ATR, y solo sube. A las 120 velas se sale. Máximo 5 posiciones, una por moneda.

### c1_maker

Solo cortos, en papel. Universo: todos los perpetuos del dex principal que devuelve `perps_all`. No hay filtro de liquidez y no entra HIP-3. Cada hora, sobre velas de 1h ya cerradas. El volumen de 24h y la profundidad del libro se guardan como referencia; no deciden la señal. El libro en directo es la variante **A**. B y C usan el mismo motor con otros parámetros y salen en el repaso.

Una vela señala solo si las 48 horas de cierres que terminan en ella, y las 24 horas de máximos que terminan en ella, van seguidas. Si falta una hora, esa ventana no cuenta.

- `up48 = cierre / mínimo de esos 48 cierres − 1 >= 0,40`
- `dd24 = cierre / máximo de esos 24 máximos − 1 <= −0,06` en A y B (al menos un 6% por debajo del máximo de 24h, sin tope superior). En C el umbral es **−0,03**.
- `ret1h = cierre / cierre de la hora anterior − 1 <= −0,03`

Si `up48 >= 1` (el cierre ha doblado el mínimo de esos 48 cierres), el aviso lleva `ta_extreme: true`. No cambia la orden. Una moneda no vuelve a señalar hasta pasadas **12** velas de su señal anterior, y tampoco mientras tenga un límite vivo o una posición abierta.

La orden de papel es una venta límite en el cierre más **0,25 ATR(14)**, viva las **3** velas siguientes. No se envía a ningún sitio. Se llena solo si el máximo de una de esas velas queda **por encima** de `límite × 1,0005` (tiene que atravesarlo; tocarlo no basta). El fill es el precio límite, comisión **maker 0,015%**, sin slippage.

1R es la distancia de la entrada al stop. El stop es taker y puede saltar en la vela del fill: se ejecuta en el stop, o en la apertura si la vela abre ya por encima, y ese precio se empeora un **0,01%** (el mismo slippage pequeño del estudio de confluencia). La comisión de esa salida es **0,045%**. El objetivo es una compra límite maker en `entrada − tp_r × 1R`. No puede llenarse en la vela de la entrada, y solo se llena si el mínimo atraviesa el objetivo (`mínimo < objetivo × 0,9995`), al precio del objetivo y con comisión maker 0,015%. Si una vela posterior toca el stop y el objetivo, sale el stop. El funding horario entra en el R con el mismo signo que en los otros libros: si es positivo, el corto lo cobra.

| Variante | dd24 | Stop | Objetivo | Máximo de velas |
| --- | --- | --- | --- | --- |
| A (directo) | ≤ −6% | 1,5 ATR | 1R | 12, al cierre |
| B | ≤ −6% | 2 ATR | 1R | 24, al cierre |
| C | ≤ −3% | 2 ATR | 1,5R | 36, al cierre |

La salida por tiempo es el cierre de esa última vela, empeorado el mismo 0,01%, con comisión taker. Una moneda a la vez, como máximo **10**. Cada una arriesga 2,50 USDC. Lo que no cabe se anota `skipped_cap` y no se avisa.

Si las tres velas no atraviesan el límite, la señal se anota `expired-unfilled`. El contrafactual entra a taker en la apertura siguiente (el mismo 0,01% en contra) y recorre el mismo stop, objetivo, plazo y funding. No ocupa una de las 10 plazas.

En cada pasada, los límites que siguen vivos leen el `l2Book` público: mejor ask, nocional ask a un precio igual o mejor que el límite, y si el límite queda dentro del spread (`inside`), por detrás del mejor ask (`beyond`) o cruzando el bid (`through`). Un fallo de una moneda no tira el libro. Si el libro entero falla, el fallo queda en el log y los otros tres no se tocan.

```
python3 c1_replay.py --days 120
```

### Avisos

Solo hay POST en una entrada, en una salida (con R) y en el resumen semanal. El webhook es el mismo de antes (`EXPLOSION_WEBHOOK_URL` y `EXPLOSION_WEBHOOK_AUTH`). Si faltan, la pasada sigue y anota `dry-run`. El cuerpo es una lista, como el detector. La dirección y el token no se escriben en el registro.

Una entrada lleva `type` `trend4h_entry`, `squeeze4h_entry` o `trapped_shorts1h_entry`, y `paper: true`, moneda, lado, entrada, stop, ATR, EMA100 y EMA200 (null si ese libro no las usa), la hora de la vela y la marca. Una salida es `*_exit` y añade el R y el motivo (`stop`, `reverse` o `time`). `skipped_cap` no se envía.

c1_maker avisa al dejar el límite (`c1_maker_order`), al fill (`c1_maker_fill`) y al salir (`c1_maker_exit`). La salida lleva `exit_reason` `tp`, `sl`, `time` o `expired-unfilled`. El cuerpo lleva `book: "c1_maker"`, `variant: "A"`, `paper: true`, la moneda, `side: "short"`, el límite, la entrada (null hasta el fill), `sl`, `tp`, el R cuando ya se conoce, `up48`, `dd24`, `ret1h` y `ta_extreme`. `skipped_cap` no se envía. En `expired-unfilled` el R es el del contrafactual taker si esa salida ya ha ocurrido dentro de las tres velas; si sigue abierto, el R queda en el CSV cuando cierra y el POST de la caducidad lleva `r` null.

El resumen semanal lleva `type` `trend4h_weekly`. Sale en la primera pasada a partir del lunes a las 08:00 UTC, una vez por semana ISO. Si esa hora no corre (el cron horario puede caer en 07:29 o en 09:21) o el POST no responde 2xx, se reintenta en la siguiente pasada. Un fallo de este POST no toca los libros. Arriba van las cifras de trend4h (posiciones abiertas, cierres, R acumulada, acierto). `books` repite eso para los cuatro libros, con el recuento de la última semana. En c1_maker ese recuento usa los cierres con fill (`status: closed`), funding incluido. Los límites sin fill están en el CSV.

### Dónde queda el papel

En la rama **`detector-state`**, junto a la memoria del detector:

- `data/trend4h/open.json` y `data/trend4h/trades.csv`
- `data/squeeze4h/open.json` y `data/squeeze4h/trades.csv`
- `data/trapped_shorts1h/open.json` y `data/trapped_shorts1h/trades.csv`
- `data/c1_maker/open.json` y `data/c1_maker/trades.csv`
- `data/paper/runner.json` — semana ISO ya enviada (`last_weekly_sent`, por ejemplo `2026-W47`), solo tras un POST 2xx
- `data/market_log/AAAA-MM-DD.csv` — OI horario; las columnas no cambian
- `data/orderbook_log/AAAA-MM-DD.csv` — profundidad y spread
- `data/listings_log.csv` y `data/listings_snapshot.json` — altas, bajas y apalancamiento
- `data/liq_proxy_log/AAAA-MM-DD.csv` — proxy de OI, rango y volumen
- `data/liq_trades_log/AAAA-MM-DD.csv` — trades públicos con campo `liquidation`
- `data/liq_tape_log/AAAA-MM-DD.csv` — muestra de la cinta

El mismo permiso de escritura de Actions vale para los dos workflows. No se pisan entre sí si no corren a la vez: cada uno restaura la rama y la vuelve a guardar entera.
