# Preparaciones, repetición hasta el 11 nov 2026

Una ruptura o un retroceso no tiene objetivo fijo: sale en el stop inicial y, desde +1R, en el Chandelier de 3 ATR. La mitad se puede cerrar en +2R. Solo se veta si el máximo o mínimo de 24h o de 7 días, medido en velas de 4 horas ya cerradas, queda a menos de 1R de la entrada. El retest de soporte sigue exigiendo el primer nivel real y un R:R de 2. Comisión taker de Hyperliquid 0,045% por lado. El cupo de 6 al día no se aplica aquí.

Ventana de las velas pedidas: 2026-09-20 → 2026-11-11 (1257 velas de 1h en el primer nombre). Perps listados (sin spot, sin delistados): **179**. Por debajo de 1M$ de volumen 24h actual: **112**. En el universo: **67**.
Nombres con velas suficientes para la EMA50: **67**. Con historia corta (se saltan): **0**.
En cada vela el filtro de libro usa la suma de las 24 horas anteriores. Un corto pide 5M$ en esa suma. El interés abierto de HIP-3 no está en las velas.

| Tipo | Operaciones | Acierto | Esperanza neta (R) | Esperanza neta (%) | Racha de pérdidas | Horas medias |
| --- | --- | --- | --- | --- | --- | --- |
| Ruptura Donchian 20 | 172 | 44.8% | -0.138 | -1.100% | 10 | 64.8 |
| Retroceso a la EMA20 | 576 | 36.5% | -0.090 | -0.328% | 14 | 22.4 |
| Retest de soporte | 0 | 0.0% | 0.000 | 0.000% | 0 | 0.0 |

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
| Máximo o mínimo de 24h/7d a menos de 1R | 655 |
| Aceptadas | 472 |

## GRAM, 9–10 nov

Movimiento del 10 nov (cierre contra la apertura, Madrid): 9.8% (máximo 11.9%)
- 11-09 11:00 trend_pullback short entrada 1.023 salida 1.049 resultado -1.04R (-2.63%)
- 11-10 21:00 trend_breakout long entrada 1.123 salida 1.149 resultado 0.50R (2.22%)

Horas del 9 y el 10 en las que el cierre salió del canal de 20. El motivo es el filtro que paró esa vela, o `taken` si entró:
- 11-10 03:00 precio 1.089, libro 24h 8.04M$, ema
- 11-10 21:00 precio 1.123, libro 24h 14.12M$, taken
- 11-10 22:00 precio 1.154, libro 24h 15.77M$, major_level

## LIT, 10 nov

Movimiento del 10 nov: 6.5% (máximo 8.1%)
LIT no abrió una preparación ese día.
Rupturas de canal el 10 nov:
- 11-10 15:00 precio 3.401, libro 27.34M$, major_level
- 11-10 22:00 precio 3.472, libro 31.13M$, volume

## Nombres que subieron más de un 6% el 10 nov

El porcentaje es el cierre de ese día en Madrid contra su apertura. «Cazada» significa que hubo una preparación en largo el 9 o el 10 nov, antes o durante ese día.

| Nombre | Cierre/apertura | Máximo/apertura | Preparación |
| --- | --- | --- | --- |
| GRAM | 9.8% | 11.9% | trend_breakout |
| LIT | 6.5% | 8.1% | no |

