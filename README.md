# Alertas de gol en vivo — v3 (migrado a ESPN, rating propio Glicko-2 + momentum real)

Sistema automático que toma los favoritos publicados en Google Sheets, los
vigila en vivo, y avisa por Telegram con distintos tipos de alerta
según qué tan probable es que se anote un gol pronto. Corre solo,
gratis, en GitHub Actions.

## ⚠️ Léeme primero si vienes de la versión con API-Football

Este proyecto migró de proveedor de datos en vivo tras la suspensión de
la cuenta de API-Football. **Lee `MIGRACION_ESPN.md` completo antes de
desplegar** — ahí está documentado qué cambió, qué se ganó, qué se
perdió, y qué dos archivos (`monitor.py` principalmente) son una
reconstrucción y conviene revisar contra tu versión anterior si todavía
la tienes.

Ya no se necesita la key de API-Football. El secret `API_FOOTBALL_KEY`
puede eliminarse de GitHub Actions — ver la sección de Secrets abajo.

## Qué cambió en esta versión (migración de proveedor)

- **Proveedor de datos en vivo: ESPN** (backend JSON no oficial de
  espn.com) en vez de API-Football. Sin cuenta, sin API key — elimina
  de raíz el problema de cuentas bloqueadas.
- **Cuotas reales gratis**: ESPN trae cuotas de DraftKings embebidas en
  el mismo scoreboard que los fixtures. Es ahora la fuente PRIMARIA de
  cuota real; The Odds API queda como respaldo secundario.
- **Tarjeta roja y penal** se detectan del mismo boxscore de
  tiros/córners — ya no hace falta una petición aparte de eventos.
- **Sin cupo diario documentado** (a diferencia del 100/día de
  API-Football) — ver `cuota_espn.py` y `MIGRACION_ESPN.md` para cómo
  cambia (no desaparece) la filosofía de ahorro de peticiones.
- **`momentum.py` simplificado**: ya no distingue tiros dentro/fuera
  del área (ESPN no lo expone) — colapsa a tiros a puerta vs. tiros que
  no fueron a puerta.

## Lo que NO cambió (rating propio Glicko-2 + momentum real)

### 1. Rating propio (Glicko-2) con seguimiento continuo

- **ClubElo sigue siendo la semilla de arranque**, pero el sistema lleva
  su **propio rating Glicko-2** por equipo, alimentado con cada
  resultado real observado (`cerrar_resultados.py`).
- **Blend progresivo**: el peso del rating propio crece con los
  partidos observados (0% / 20% / 50% / 75% / 100% según 0, 1-3, 4-8,
  9-15, >15 partidos).
- **Bootstrap histórico** (`bootstrap_ligas.py`, uso manual, football-
  data.co.uk — no depende de ESPN ni API-Football).

### 2. Emparejamiento de equipos por país propio + verificación cruzada

- Cada equipo resuelve su propio país (`team_resolver.py`), cacheado
  para siempre. Orden de resolución: liga doméstica (gratis) → Goal
  Index (gratis) → ESPN (best-effort, país del estadio).

### 3. Momentum en vivo, separado de la expectativa pre-partido

- **`momentum.py`** calcula presión y probabilidad de gol usando
  exclusivamente eventos ocurridos dentro del partido.
- **Zona de paridad** (35%-65%): mensaje honesto de "partido abierto".
- **Techo de diferencia**: con 3+ goles, un único aviso de "seguimiento
  cerrado".

### 4. Ciclo de retroalimentación real

- Cada alerta individual queda **auditada** tras el partido
  (`cerrar_resultados.py`).
- El reporte de las 6am incluye acierto por tipo de alerta y por
  madurez del rating propio.

## Las 5 fases

| Fase | Cuándo | Qué hace |
|---|---|---|
| 1. Selección | Desde las 04:00 | Lee los favoritos de Google Sheets y localiza sus fixtures en ESPN para vigilarlos en vivo |
| 2. Resumen | 07:00 | Manda a Telegram la lista de partidos de hoy |
| 3. Vigilancia | Cada 5-15 min (adaptativo) | Boxscore en vivo de ESPN, calcula momentum real, manda la alerta que aplique |
| 4. Cierre | 23:30 | Resuelve resultados vía ESPN, actualiza Glicko-2, audita cada alerta, archiva el día |
| Reporte diario | 06:00 (día siguiente) | Resultados + acierto por tipo de alerta + acierto por madurez + peticiones a ESPN usadas |

## Los tipos de alerta

| Situación | Alerta |
|---|---|
| Favorito perdiendo por 1, momentum a favor del favorito | 🟠 Posible empate |
| Empatando, momentum a favor del favorito | 🟢 Posible victoria del favorito |
| 0-0, antes del min 30, favorito con el momentum | ⏱️ Gana favorito 1er tiempo |
| Empatando o perdiendo, momentum a favor del rival | 🔴 Posible gol del no favorito / ⚠️ Cuidado rival presiona |
| Favorito ganando, momentum sigue a su favor | 🔵 Posible ampliación de marcador |
| Momentum parejo (35%-65%), peligro real de cualquier lado | ⚡ Partido abierto |
| Tarjeta roja detectada | 🟥 Tarjeta roja |
| Penal detectado | 🎯 Penal |
| Min 75-90+, empatado o -1, dominancia acumulada ≥75% | ⏰ Posible gol de cierre |
| Diferencia ≥3 goles | 🏁 Seguimiento cerrado (una sola vez) |

## Cómo agregar una liga nueva

**Bootstrap del rating propio** (football-data.co.uk, sin cambios):
```bash
python bootstrap_ligas.py D1 I1
python bootstrap_ligas.py --extra ARG BRA
```

**Agregar la liga al seguimiento en vivo de ESPN**: agrega el slug
correcto a `LIGAS_ESPN` en `fetch_data.py`. Si no conoces el slug, sigue
el patrón `pais.numero_de_division` (ej. `ned.1`) y revisa el log de la
primera corrida — si falla, el aviso lo dice explícitamente.

## Cómo ponerlo en línea

### 1. Crear el repositorio
```bash
cd alertas-apuestas-espn
git init
git add .
git commit -m "v3: migracion a ESPN"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/alertas-apuestas-espn.git
git push -u origin main
```

### 2. Permisos de escritura
`.../settings/actions` → "Workflow permissions" → **Read and write
permissions** → Save

### 3. Credenciales (Secrets)
`.../settings/secrets/actions` →

| Name | Valor | ¿Sigue haciendo falta? |
|---|---|---|
| `API_FOOTBALL_KEY` | — | **NO** — ya no se usa, puedes eliminarla |
| `TELEGRAM_BOT_TOKEN` | el token de tu bot (@BotFather) | Sí |
| `TELEGRAM_CHAT_ID` | tu chat id | Sí |
| `ODDS_API_KEY` | tu key de The Odds API | Opcional (solo respaldo secundario de cuotas) |

### 4. Configurar los workflows de GitHub Actions

Igual que antes: 4 disparos diarios para Fase 3 (con `sleep` interno
cada 5 min), y ventanas de reintento de 15 min para Fase 2, Fase 4, y
el Reporte diario.

### 5. Probar manualmente
`.../actions` → "Fase 1 - Selección de partidos" → Run workflow →
revisa el log (deberías ver "ESPN: N fixtures encontrados...") → luego
"Fase 2 - Resumen diario".

## Estructura de archivos

```
glicko2.py                 -> algoritmo Glicko-2 (sin cambios)
ratings_store.py           -> rating propio + blend con ClubElo + migracion de llaves (bootstrap Y api-football->espn)
team_resolver.py           -> pais por equipo (liga domestica -> Goal Index -> ESPN best-effort)
momentum.py                -> presion/momentum ADAPTADO a los campos reales de ESPN
fetch_data.py               -> REESCRITO: ESPN (fixtures, boxscore, cuotas DraftKings) + ClubElo + football-data.co.uk (sin cambios)
poisson_model.py           -> rating combinado -> probabilidad pre-partido (sin cambios)
goal_index.py               -> Goal Index mezclado (sin cambios, football-data.co.uk)
cuota_espn.py               -> contador de peticiones a ESPN (diagnostico, sin techo conocido)
cuota_odds_api.py           -> cupo de The Odds API (respaldo secundario, sin cambios)
cuotas_reales.py            -> The Odds API como respaldo (sin cambios de logica)
mapeo_ligas_odds_api.py    -> mapeo liga -> sport_key de The Odds API (sin cambios)
bootstrap_ligas.py         -> carga historica manual (football-data.co.uk, sin cambios)
seleccionar_partidos.py   -> Fase 1: favoritos de Google Sheets + localización en ESPN
google_favoritos.py       -> descarga y valida los favoritos diarios de Google Sheets
resumen.py                  -> Fase 2 (sin cambios funcionales)
monitor.py                  -> Fase 3, RECONSTRUIDA -- leer MIGRACION_ESPN.md
cerrar_resultados.py       -> Fase 4, AJUSTADA a ESPN
reporte_diario.py           -> Reporte de las 6am, AJUSTADO (sin "disponibles" de ESPN)
telegram_utils.py           -> envio de mensajes (sin cambios)
estado_diario.py             -> control de "ya se hizo hoy" (sin cambios)
storage.py                   -> utilidad de JSON, disponible sin forzar su uso (sin cambios)
MIGRACION_ESPN.md           -> NUEVO -- por que se migro, que se verifico, que se gano/perdio
filosofia_proyecto.md       -> principios de diseno (actualizado con addendum v3)
CONSOLIDACION.md            -> historia de la consolidacion original (sin cambios, valor historico)
data/partidos_hoy.json     -> seleccion + memoria del partido (ahora incluye "liga_slug")
data/ratings_propios.json  -> rating Glicko-2 (llaves ahora "espn:<id>")
data/team_country_cache.json -> pais de cada equipo
data/uso_espn.json          -> NUEVO -- contador diario de peticiones a ESPN
data/historial_dias/       -> un archivo JSON por dia
data/estadisticas.xlsx     -> 3 pestanas: resultados, resumen por dia, acierto por tipo de alerta
```

## Limitaciones que debes saber

- **`monitor.py` es una reconstrucción**, no una migración del original
  (ver `MIGRACION_ESPN.md`). Revísalo antes de confiar ciegamente en
  los umbrales de alerta.
- **ESPN no tiene SLA ni cupo documentado** — puede cambiar o fallar
  sin aviso. El sistema nunca falla en silencio (los avisos van al log).
- **Riesgo de IP compartida** en GitHub Actions runners — ver
  `MIGRACION_ESPN.md`.
- **Tiros dentro/fuera del área ya no están disponibles** — `momentum.py`
  usa una aproximación más simple que antes.
- **`LIGAS_ESPN` es una lista curada, no exhaustiva** — fácil de
  expandir, algunos slugs no verificados uno por uno (marcado en el
  código).
