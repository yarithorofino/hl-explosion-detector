# Confluencia 1h, replay de velas cerradas

Cada vela de 1h ya cerrada se puntúa con la regla de las 00: tendencia (cierre sobre la EMA20 y EMA20 sobre la EMA50), ruptura del Donchian de 20, volumen al menos 1,5 veces la media de esas 20 horas, stop no más ajustado que 2 ATR y descarte si el cierre ya está a más de 2 ATR del nivel. El corto pide además funding por encima de −0,01%/h. El cupo es 8 POSTs al día (varias monedas en la misma hora son un solo POST) y cada moneda espera 4 horas. De 00:00 a 08:00 (Madrid) no se envía.

El delta no está en las velas públicas, así que **aquí no se aplica**. En directo no hay POST si la cinta no cubrió al menos el 95% de la hora. Estas cifras son un techo, no los despertares reales.

Para no pasarse del cupo de la API se han usado los **30** perps líquidos con más volumen de 24h ahora mismo, de 78 que pasan el filtro (24h ≥ 5M$ y, en HIP-3, OI ≥ 1M$) sobre 350 perps. Con velas: **30**. Errores: **0**. Ventana (cierre de la vela, Madrid): 2026-10-28 22:11 → 2026-11-11 22:11.

El volumen de 24h de cada vela es la suma del nocional de esa hora y las 23 anteriores. El OI de HIP-3 es el de hoy. La salida mira 48 horas; si la misma vela toca stop y TP1, cuenta como stop. El R neto descuenta 0,045% de comisión y 0,01% de slippage por lado, como un peor precio de entrada y de salida. No se ha cambiado ningún umbral después de ver el resultado.

## Embudo (sin delta)

| Paso | Velas |
| --- | ---: |
| Libro de 24h por debajo de 5M$ en esa vela | 499 |
| Historia corta o con hueco | 0 |
| No es ruptura con tendencia y volumen | 9132 |
| Ya a más de 2 ATR del nivel | 62 |
| Corto con funding en −0,01%/h o sin dato | 32 |
| Regla de precio, antes de cupo y silencio | 326 |
| En silencio 00:00–08:00 | 67 |
| En pausa de 4h | 65 |
| Por encima del cupo de 8 POSTs | 25 |
| Se habrían enviado (sin delta) | 169 |

**11.27 avisos por día** (169 en 15 días de calendario) en estos 30 nombres, sin exigir delta.

## Resultado de los que se habrían enviado

| Día | Avisos | TP1 | SL | Abierto | Abierto por falta de horas |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-10-28 | 1 | 0 | 0 | 1 | 0 |
| 2026-10-29 | 16 | 7 | 3 | 6 | 0 |
| 2026-10-30 | 9 | 2 | 0 | 7 | 0 |
| 2026-10-31 | 1 | 1 | 0 | 0 | 0 |
| 2026-11-01 | 6 | 5 | 1 | 0 | 0 |
| 2026-11-02 | 14 | 10 | 3 | 1 | 0 |
| 2026-11-03 | 4 | 1 | 1 | 2 | 0 |
| 2026-11-04 | 20 | 11 | 4 | 5 | 0 |
| 2026-11-05 | 19 | 10 | 0 | 9 | 0 |
| 2026-11-06 | 6 | 3 | 1 | 2 | 0 |
| 2026-11-07 | 5 | 4 | 1 | 0 | 0 |
| 2026-11-08 | 6 | 4 | 2 | 0 | 0 |
| 2026-11-09 | 26 | 15 | 9 | 2 | 0 |
| 2026-11-10 | 14 | 7 | 5 | 2 | 2 |
| 2026-11-11 | 22 | 16 | 0 | 6 | 6 |
| **Total** | **169** | **96** | **30** | **43** | **8** |

De los que ya tocaron un extremo, TP1 ganó en **96** y el stop en **30** (76.2% a favor del TP1). Abiertos: **43**, de ellos **8** porque aún no hay 48 horas.

Lados: long 70, short 99. Objetivo: estructural 101, medido 52, mixto 16.

## Largos y cortos

El R medio es solo de los que ya tocaron TP1 o el stop. Cada lado paga 0,055% (0,045% de comisión más 0,01% de slippage) y el riesgo es la distancia prevista entre la entrada y el stop.

| Grupo | Avisos | TP1 | SL | Abierto | Resueltos | R neto medio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Largo | 70 | 48 | 14 | 8 | 62 | -0.123 |
| Corto, todos (funding ya > −0,01%/h) | 99 | 48 | 16 | 35 | 64 | 0.015 |
| Corto de ruptura, sin agotamiento | 85 | 47 | 14 | 24 | 61 | 0.017 |
| Corto de agotamiento | 14 | 1 | 2 | 11 | 3 | -0.014 |

De 99 cortos enviados, 85 son ruptura limpia y 14 llevan agotamiento. La mecha superior de la hora de la señal pasa de la mitad del rango en 0, el funding pasa de +0,01%/h en 2, y la prima pasa de +0,1% en 14. Sin prima en el historial de funding: 0.

La regla de corto del equipo, además del funding por encima de −0,01%/h que ya se exige para enviar, deja fuera el agotamiento. Esos cortos de ruptura limpia son los que la pasarían.

## Sesgo de mirada al futuro

| Comprobación | Resultado |
| --- | --- |
| La entrada es el cierre de la vela de la señal, el precio que existe cuando esa hora cierra. No es el máximo de una vela posterior ni la apertura de la siguiente. | Sin sesgo |
| El recorrido hacia TP1 y el stop empieza en la vela siguiente. La vela de la señal no se camina. | Sin sesgo |
| Si una vela posterior toca el stop y el TP1, cuenta como stop. | Sin sesgo |
| El Donchian, el ATR y el giro del stop salen de las velas que ya habían cerrado antes de abrir la vela de la señal. | Sin sesgo |
| La EMA20 y la EMA50 dan un paso con el cierre de la señal. Ese cierre ya se conoce en el momento del aviso; no entra ninguna vela posterior. Es la misma cuenta que el detector en directo. | Sin sesgo |
| El funding y la prima son la liquidación de esa hora (el print cae unos milisegundos después del cierre). No se usa la hora siguiente: la ventana aceptada es de 5 segundos, y el siguiente print es una hora después. | Sin sesgo |

No había una cifra calculada con una vela que todavía no existía. No se ha rehecho el replay por ese motivo.

## Qué no mide esto

- El delta. En directo es obligatorio.
- Las horas corridas de :15, :30 y :45.
- Los perps líquidos fuera de estos 30 (el directo sí los mira todos).
- El interés abierto histórico de HIP-3.
- El R de los avisos que siguen abiertos. El R medio de arriba solo usa los que ya salieron.
