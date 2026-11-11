# Confluencia 1h, replay de velas cerradas

Cada vela de 1h ya cerrada se puntúa con la regla de las 00: tendencia (cierre sobre la EMA20 y EMA20 sobre la EMA50), ruptura del Donchian de 20, volumen al menos 1,5 veces la media de esas 20 horas, stop no más ajustado que 2 ATR y descarte si el cierre ya está a más de 2 ATR del nivel. El corto pide además funding por encima de −0,01%/h. El cupo es 8 POSTs al día (varias monedas en la misma hora son un solo POST) y cada moneda espera 4 horas. De 00:00 a 08:00 (Madrid) no se envía.

El delta no está en las velas públicas, así que **aquí no se aplica**. En directo no hay POST si la cinta no cubrió al menos el 95% de la hora. Estas cifras son un techo, no los despertares reales.

Para no pasarse del cupo de la API se han usado los **30** perps líquidos con más volumen de 24h ahora mismo, de 78 que pasan el filtro (24h ≥ 5M$ y, en HIP-3, OI ≥ 1M$) sobre 350 perps. Con velas: **30**. Errores: **0**. Ventana (cierre de la vela, Madrid): 2026-10-28 20:47 → 2026-11-11 20:47.

El volumen de 24h de cada vela es la suma del nocional de esa hora y las 23 anteriores. El OI de HIP-3 es el de hoy. La salida mira 48 horas; si la misma vela toca stop y TP1, cuenta como stop. Sin comisión. No se ha cambiado ningún umbral después de ver el resultado.

## Embudo (sin delta)

| Paso | Velas |
| --- | ---: |
| Libro de 24h por debajo de 5M$ en esa vela | 521 |
| Historia corta o con hueco | 0 |
| No es ruptura con tendencia y volumen | 9137 |
| Ya a más de 2 ATR del nivel | 63 |
| Corto con funding en −0,01%/h o sin dato | 32 |
| Regla de precio, antes de cupo y silencio | 327 |
| En silencio 00:00–08:00 | 63 |
| En pausa de 4h | 66 |
| Por encima del cupo de 8 POSTs | 25 |
| Se habrían enviado (sin delta) | 173 |

**11.53 avisos por día** (173 en 15 días de calendario) en estos 30 nombres, sin exigir delta.

## Resultado de los que se habrían enviado

| Día | Avisos | TP1 | SL | Abierto | Abierto por falta de horas |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-10-28 | 2 | 0 | 1 | 1 | 0 |
| 2026-10-29 | 17 | 7 | 3 | 7 | 0 |
| 2026-10-30 | 10 | 2 | 0 | 8 | 0 |
| 2026-10-31 | 1 | 1 | 0 | 0 | 0 |
| 2026-11-01 | 6 | 5 | 1 | 0 | 0 |
| 2026-11-02 | 14 | 11 | 2 | 1 | 0 |
| 2026-11-03 | 5 | 1 | 2 | 2 | 0 |
| 2026-11-04 | 20 | 11 | 5 | 4 | 0 |
| 2026-11-05 | 20 | 10 | 0 | 10 | 0 |
| 2026-11-06 | 6 | 3 | 1 | 2 | 0 |
| 2026-11-07 | 5 | 4 | 1 | 0 | 0 |
| 2026-11-08 | 6 | 4 | 2 | 0 | 0 |
| 2026-11-09 | 26 | 15 | 9 | 2 | 0 |
| 2026-11-10 | 14 | 7 | 5 | 2 | 2 |
| 2026-11-11 | 21 | 16 | 0 | 5 | 5 |
| **Total** | **173** | **97** | **32** | **44** | **7** |

De los que ya tocaron un extremo, TP1 ganó en **97** y el stop en **32** (75.2% a favor del TP1). Abiertos: **44**, de ellos **7** porque aún no hay 48 horas.

Lados: long 72, short 101. Objetivo: estructural 102, medido 55, mixto 16.

## Qué no mide esto

- El delta. En directo es obligatorio.
- Las horas corridas de :15, :30 y :45.
- Los perps líquidos fuera de estos 30 (el directo sí los mira todos).
- El interés abierto histórico de HIP-3.
- Comisiones (0,09% ida y vuelta; el stop mínimo es 2 ATR).
