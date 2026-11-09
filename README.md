# Detector de explosiones (Hyperliquid)

Este programa mira los perpetuos de Hyperliquid (el mercado principal y los de HIP-3) y avisa cuando uno se mueve de golpe con mucho volumen. No compra ni vende nada. Solo avisa.

Antes corría en un ordenador que se duerme. Ahora lo ejecuta GitHub cada 15 minutos, también con el ordenador apagado.

Avisa cuando se cumplen las tres cosas a la vez:

- el precio se ha movido un **8% o más** en la última hora
- el volumen de esa hora es al menos **5 veces** la media de las 24 horas anteriores
- y ese volumen es de al menos **50.000 dólares**

Si en el mismo cuarto de hora saltan varios activos, van todos en **un solo aviso**. El mismo activo no se repite hasta pasadas **4 horas**. Como máximo hay **8 avisos al día**. De **00:00 a 08:00** (hora de Madrid) anota la señal pero no llama al webhook; a las 8 ya puede avisar.

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
7. Entra en esa ejecución. Arriba sale un resumen en castellano. Abre el job **detectar** y el paso **Ejecutar detector**.
8. Busca esta línea:
   - `force_test status=posted` → el webhook ha respondido. La prueba ha llegado.
   - `force_test status=dry_run` → faltan los secretos, o el nombre no coincide.
   - `force_test status=post_failed` y cerca `webhook error: HTTP 401` o `403` → la línea del secreto `EXPLOSION_WEBHOOK_AUTH` no es la que espera el webhook. Vuelve a copiarla.

La prueba manda una lista con un solo objeto que dice que es una prueba, no un movimiento real. No gasta uno de los 8 avisos del día. Puedes lanzarla también de madrugada.

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

Un aviso de verdad tiene esta forma (varios activos irían uno detrás de otro, en la misma lista, en un solo POST):

```json
[
  {
    "dex": "main",
    "coin": "BTC",
    "pct_1h": 9.5,
    "vol_1h_usd": 2500000,
    "vol_mult": 6.2,
    "mark": 100000,
    "dayNtlVlm": 80000000,
    "oi_usd": 12000000,
    "maxLeverage": 40,
    "detected_at_madrid": "2026-06-15 14:15"
  }
]
```

## Cómo leer una ejecución normal

En **Actions** → **Detector de explosiones** → la ejecución de ese cuarto de hora → job **detectar** → paso **Ejecutar detector**.

El resumen de arriba del run dice cuántos perpetuos miró y si envió algo. En el texto del paso, la línea importante es parecida a esta:

```
candidatos=12 velas=12 hits=1 enviados=1 status=posted ['XYZ']
```

Qué significa `status`:

| Valor | Qué ha pasado |
| --- | --- |
| `none` | Nada que avisar. |
| `posted` | El webhook ha respondido bien. |
| `dry_run` | Había una señal, pero faltan los secretos. Aun así cuenta como aviso del día, igual que en el ordenador. |
| `post_failed` | Se intentó enviar y falló. No cuenta: si la señal sigue, lo reintenta en el siguiente ciclo. |
| `quiet_hours_logged` | Es de noche (00:00–08:00, Madrid). Queda anotado y no se envía. |
| `daily_cap_logged` | Ya se han enviado los 8 avisos de hoy. |

Si hay muchos activos moviéndose a la vez, solo pide velas de los **25** más movidos.

La primera hora, todavía no hay un precio de hace una hora guardado. Mientras tanto elige candidatos por el movimiento de 24 horas. A partir de la segunda hora también compara con el precio de hace una hora.

## Dónde se guarda la memoria

Entre una ejecución y la siguiente hay que recordar los precios anteriores, qué activo está en pausa de 4 horas y cuántos avisos van hoy. Eso no puede vivir en el disco del ordenador, y tampoco conviene un commit en `main` cada 15 minutos.

Se guarda en una rama aparte, **`detector-state`**. Esa rama solo tiene el último recuerdo (se reescribe cada vez), así el historial del proyecto no se llena. Dos ejecuciones no pueden escribir a la vez: GitHub deja correr solo una, y al guardar se comprueba que nadie haya escrito en medio. Si la comprobación no cuadra, no se pisa lo que ya había.

En esa rama están:

- `data/detector_state.json` — la memoria
- `signals/explosion_alerts.jsonl` — un historial corto de avisos (como mucho unos cientos de líneas)
- `LEEME.txt` — una nota que dice que no hay que editar esos ficheros a mano

No hace falta abrirla para el día a día. El registro de Actions basta. La rama es pública, como el resto del repositorio: tiene movimientos de mercado, no la URL ni la clave del webhook.

Si un ciclo no consigue leer Hyperliquid, no toca esa memoria. El fallo se ve en rojo y el recuerdo anterior sigue igual.

## Horario, retrasos y los 60 días

El reloj de GitHub va en UTC y lanza el trabajo cada 15 minutos (`*/15 * * * *`). A veces lo empieza unos minutos tarde. Es normal: el detector mira la vela de la última hora, no el segundo exacto.

En un repositorio público, GitHub **apaga los horarios si pasan 60 días sin actividad** en el repositorio. Cada ejecución que guarda estado deja un commit en `detector-state`, y un commit es actividad, así que mientras el detector funcione no deberían cumplirse esos 60 días. Si aun así lo apaga (suele avisar por correo), la pantalla de Actions enseña un aviso y un botón **Enable workflow** (**Habilitar workflow**). Pulsa ese botón y, si quieres, lanza una ejecución manual para no esperar al siguiente cuarto de hora.

El horario solo funciona con el fichero en `main`. En repos públicos, los minutos de Actions no se cobran.

## Si algo falla

- El paso **Ejecutar detector** dice `dry-run` → revisa que los dos secretos existan y que los nombres sean exactos.
- `webhook error: HTTP 401` o `403` → vuelve a pegar `EXPLOSION_WEBHOOK_AUTH`, la línea completa, sin comillas.
- **Guardar estado** en rojo con `403` o `Permission denied` → falta el permiso **Read and write permissions** de la sección de arriba.
- No aparece ninguna ejecución → el pull request no está fusionado en `main`, Actions está desactivado, o el horario se apagó por los 60 días.

Para volver a lanzarlo a mano: **Actions** → **Detector de explosiones** → **Run workflow**. Sin marcar la casilla, hace un ciclo normal. Con la casilla, además manda el POST de prueba.
