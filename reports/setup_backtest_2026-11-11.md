# Preparaciones, diseño 12–31 oct y fuera de muestra 1–11 nov 2026

Una ruptura o un retroceso no tiene objetivo fijo: sale en el stop inicial y, desde +1R, en el Chandelier de 3 ATR. La mitad se puede cerrar en +2R. Solo se veta si el máximo o mínimo de 24h o de 7 días, medido en velas de 4 horas ya cerradas, queda a menos de 1R de la entrada. El retest de soporte sigue exigiendo el primer nivel real y un R:R de 2. La continuación (1h y 4h) solo se toma a favor de Bitcoin: largo si el cierre de 4h de BTC está por encima de su EMA200 de 4h, corto si está por debajo. La ruptura de 20 velas también se evalúa en el gráfico de 4 horas, con el stop de 2 ATR de esas velas y el mismo Chandelier. Ningún número se ha movido para que noviembre salga mejor. Comisión taker de Hyperliquid 0,045% por lado. Comisión/bruto es la suma de las comisiones en R partida por la suma del valor absoluto del resultado bruto en R. El cupo de 6 al día no se aplica aquí.

Ventana de las velas pedidas: 2026-09-20 → 2026-11-11 (1257 velas de 1h en el primer nombre). Perps listados (sin spot, sin delistados): **179**. Por debajo de 1M$ de volumen 24h actual: **112**. En el universo: **67**.
Nombres con velas suficientes para la EMA50: **67**. Con historia corta (se saltan): **0**.
En cada vela el filtro de libro usa la suma de las 24 horas anteriores. Un corto pide 5M$ en esa suma. El interés abierto de HIP-3 no está en las velas. El régimen de BTC usa 981 velas de 4h, 2026-06-01 00:00 UTC → 2026-11-11 08:00 UTC.

## Diseño, 12–31 oct (fecha de Madrid de la entrada)

| Tipo | Lado | Operaciones | Acierto | Bruto (R) | Neto (R) | Comisión/bruto | Racha | Horas |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ruptura 20×1h | largo | 34 | 17.6% | -0.557 | -0.581 | 2.5% | 15 | 37.3 |
| Ruptura 20×1h | corto | 78 | 48.7% | -0.113 | -0.131 | 2.1% | 7 | 112.5 |
| Retroceso EMA20 1h | largo | 151 | 20.5% | -0.453 | -0.494 | 4.3% | 19 | 11.0 |
| Retroceso EMA20 1h | corto | 149 | 37.6% | 0.009 | -0.028 | 3.4% | 6 | 29.9 |
| Ruptura 20×4h | largo | 16 | 6.2% | -0.824 | -0.832 | 0.9% | 15 | 136.5 |
| Ruptura 20×4h | corto | 51 | 62.7% | 0.192 | 0.182 | 1.1% | 3 | 255.9 |
| Retest de soporte | largo | 0 | 0.0% | 0.000 | 0.000 | 0.0% | 0 | 0.0 |
| Retest de soporte | corto | 0 | 0.0% | 0.000 | 0.000 | 0.0% | 0 | 0.0 |

## Fuera de muestra, 1–11 nov (fecha de Madrid de la entrada)

| Tipo | Lado | Operaciones | Acierto | Bruto (R) | Neto (R) | Comisión/bruto | Racha | Horas |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ruptura 20×1h | largo | 0 | 0.0% | 0.000 | 0.000 | 0.0% | 0 | 0.0 |
| Ruptura 20×1h | corto | 17 | 64.7% | 0.342 | 0.322 | 2.6% | 2 | 24.0 |
| Retroceso EMA20 1h | largo | 21 | 0.0% | -0.962 | -0.993 | 3.2% | 21 | 11.6 |
| Retroceso EMA20 1h | corto | 50 | 50.0% | 0.358 | 0.331 | 2.8% | 3 | 35.2 |
| Ruptura 20×4h | largo | 0 | 0.0% | 0.000 | 0.000 | 0.0% | 0 | 0.0 |
| Ruptura 20×4h | corto | 19 | 57.9% | 0.080 | 0.067 | 2.3% | 2 | 69.9 |
| Retest de soporte | largo | 0 | 0.0% | 0.000 | 0.000 | 0.0% | 0 | 0.0 |
| Retest de soporte | corto | 0 | 0.0% | 0.000 | 0.000 | 0.0% | 0 | 0.0 |

## Qué filtro paró las rupturas de canal

Solo cuentan las velas que cerraron fuera del canal de 20 horas. El resto no es una ruptura y no entra en esta tabla. Aceptadas es una vela que pasa los filtros. Si ese nombre ya tiene una posición abierta, no se abre otra: por eso hay menos operaciones que velas aceptadas.

| Filtro | Velas |
| --- | --- |
| Historia corta (EMA50 o Donchian) | 0 |
| Ruptura con la EMA20 del lado contrario | 1464 |
| Volumen por debajo de 1,5× la media de 20 | 1150 |
| Libro de 24h por debajo de 1M$ (largo) | 57 |
| Libro de 24h por debajo de 5M$ (corto) | 715 |
| ATR o stop no calculable | 0 |
| Máximo o mínimo de 24h/7d a menos de 1R | 406 |
| BTC 4h del lado contrario a su EMA200 | 426 |
| EMA200 de BTC todavía sin 200 velas | 0 |
| Aceptadas en 1h | 295 |
| 4h: EMA20 del lado contrario | 268 |
| 4h: volumen por debajo de 1,5× | 314 |
| 4h: libro largo por debajo de 1M$ | 6 |
| 4h: libro corto por debajo de 5M$ | 174 |
| 4h: extremo de 24h/7d a menos de 1R | 47 |
| 4h: BTC del lado contrario | 42 |
| 4h: EMA200 de BTC no lista | 0 |
| Aceptadas en 4h | 185 |

## GRAM, 9–10 nov

Movimiento del 10 nov (cierre contra la apertura, Madrid): 9.8% (máximo 11.9%)
- 11-09 11:00 trend_pullback short entrada 1.023 salida 1.049 resultado -1.04R (-2.63%)

Horas del 9 y el 10 en las que el cierre salió del canal de 20. El motivo es el filtro que paró esa vela, o `taken` si entró:
- 11-10 03:00 precio 1.089, libro 24h 8.04M$, ema
- 11-10 21:00 precio 1.123, libro 24h 14.12M$, regime
- 11-10 22:00 precio 1.154, libro 24h 15.77M$, regime

## LIT, 10 nov

Movimiento del 10 nov: 6.5% (máximo 8.1%)
LIT no abrió una preparación ese día.
Rupturas de canal el 10 nov:
- 11-10 15:00 precio 3.401, libro 27.34M$, regime
- 11-10 22:00 precio 3.472, libro 31.13M$, volume

## Nombres que subieron más de un 6% el 10 nov

El porcentaje es el cierre de ese día en Madrid contra su apertura. «Cazada» significa que hubo una preparación en largo el 9 o el 10 nov, antes o durante ese día.

| Nombre | Cierre/apertura | Máximo/apertura | Preparación |
| --- | --- | --- | --- |
| GRAM | 9.8% | 11.9% | no |
| LIT | 6.5% | 8.1% | no |

