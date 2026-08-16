"""
monitor.py
------------
FASE 3 -- RECONSTRUIDO durante la migracion a ESPN.

AVISO IMPORTANTE, leelo antes de desplegar: el monitor.py ORIGINAL
nunca llego a esta conversacion (se subio el archivo pero su contenido
no paso al chat -- limitacion de la plataforma, no un olvido). Todo lo
que sigue se reconstruyo a partir de:
  (a) la tabla de tipos de alerta descrita en README.md,
  (b) las funciones ya confirmadas de momentum.py,
  (c) el formato de datos de partidos_hoy.json que ya usan
      seleccionar_partidos.py, resumen.py y cerrar_resultados.py.

Los UMBRALES exactos (ej. "momentum >= 65% para alertar") son valores
de partida razonables, NO los que ya tenias calibrados con evidencia
real en el Excel. Si todavia tienes acceso al monitor.py original
(tu computadora, o el historial de git del repo viejo), compara la
logica exacta de cuando NO repetir una alerta ya enviada -- aqui se
simplifico a "no repetir el mismo tipo dentro de una ventana de N
minutos", que puede no ser exactamente lo que ya tenias afinado.

Qué SI cambio de forma segura (evidencia real de esta migracion):
  - Tarjeta roja y penal se detectan del MISMO boxscore de tiros/
    corners (momentum.hubo_tarjeta_roja / hubo_penal) -- ya no hace
    falta la peticion aparte de eventos que mencionaba el README viejo.
"""

import json
import datetime
from pathlib import Path

from fetch_data import obtener_boxscore_en_vivo
from telegram_utils import enviar_mensaje_telegram, escapar_html
import momentum

DATA_DIR = Path(__file__).parent / "data"
ARCHIVO_PARTIDOS = DATA_DIR / "partidos_hoy.json"

DIFERENCIA_TECHO = 3
MINUTO_INICIO_CIERRE = 75
DOMINANCIA_CIERRE = 0.75

MINUTO_MINIMO_ALERTA_MOMENTUM = 15

# Emoji al inicio de cada mensaje segun el TIPO de pronostico (a pedido
# explicito, agosto 2026) -- distinto de la corona junto al nombre del
# equipo, que indica A QUIEN favorece.
EMOJI_TIPO_PRONOSTICO = {
    "favorito_directo": "\U0001F3AF",   # 🎯
    "doble_oportunidad": "\U0001F500",  # 🔀
}
CORONA_FAVORITO = "\U0001F451"  # 👑

# =====================================================================
# SISTEMA DE DOS CAPAS (rediseno a pedido explicito, agosto 2026)
# -----------------------------------------------------------------
# Capa 1 (SOSTENIDO, ventana ~20 min): la base real de la alerta -- si
# por si sola cruza el umbral, dispara. Mide si el favorito viene
# llevando la iniciativa de forma consistente, no solo en un momento.
#
# Capa 2 (RECIENTE, ventana ~5 min, la revision inmediata anterior):
# actua SOLO como un "plus" -- si la intensidad reciente cruza su
# propio umbral duro, se suma un bono al resultado de la Capa 1. NUNCA
# puede disparar una alerta por si sola ni cargar la mayor parte del
# puntaje -- es un empujon sobre una base que ya viene bien encaminada.
#
# PISO MINIMO DE VOLUMEN: si en la ventana sostenida hubo muy pocas
# jugadas en total (entre ambos equipos), el porcentaje no se evalua --
# evita que 1 tiro suelto sin respuesta del rival dispare una alerta
# solo por tener un denominador chico.
# =====================================================================

UMBRAL_DOMINANCIA_GENERAL = 0.70
UMBRAL_INTENSIDAD_RECIENTE = 0.80
BONO_INTENSIDAD_RECIENTE = 0.10
VENTANA_SOSTENIDO_MINUTOS = 20
VOLUMEN_MINIMO_VENTANA = 6.0   # equivalente aprox. a 2 tiros a puerta combinados

# Alerta de primer tiempo: mismo mecanismo, pero mas permisiva a
# proposito -- cubre "algo se esta cocinando antes del descanso", no
# "dominancia abrumadora ya confirmada".
UMBRAL_DOMINANCIA_1ER_TIEMPO = 0.60
BONO_1ER_TIEMPO = 0.05
MINUTO_INICIO_1ER_TIEMPO = 25
MINUTO_FIN_1ER_TIEMPO = 40


def _cargar():
    if not ARCHIVO_PARTIDOS.exists():
        return None
    return json.loads(ARCHIVO_PARTIDOS.read_text(encoding="utf-8"))


def _guardar(datos):
    ARCHIVO_PARTIDOS.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def _en_ventana_horaria(partido):
    """Chequeo local, gratis: da margen razonable antes/despues del
    kickoff -- misma filosofia de siempre, nunca gastar una peticion
    si se puede evitar en frio."""
    try:
        inicio = datetime.datetime.fromisoformat(partido["kickoff_utc"].replace("Z", "+00:00"))
    except Exception:
        return True
    ahora = datetime.datetime.now(datetime.timezone.utc)
    minutos_desde_inicio = (ahora - inicio).total_seconds() / 60
    return -10 <= minutos_desde_inicio <= 130


def _registrar_alerta(partido, tipo, texto, minuto):
    partido.setdefault("alertas_enviadas", []).append({"tipo": tipo, "minuto": minuto, "texto": texto})


def _ya_se_envio_reciente(partido, tipo, minuto_actual, ventana=10):
    minuto_actual_int = momentum._minuto_a_entero(minuto_actual)
    for a in reversed(partido.get("alertas_enviadas", [])):
        if a["tipo"] != tipo:
            continue
        minuto_previo_int = momentum._minuto_a_entero(a["minuto"])
        if minuto_actual_int is None or minuto_previo_int is None:
            return True
        return abs(minuto_actual_int - minuto_previo_int) <= ventana
    return False


def _snapshot_referencia(historial, minuto_actual_int, minutos_atras):
    """Busca el snapshot mas cercano a 'minutos_atras' antes del minuto
    actual, dentro del historial ya guardado. Si el partido no lleva
    tanto tiempo (o hubo huecos), usa el snapshot mas antiguo disponible
    como mejor esfuerzo -- eso naturalmente da menos volumen acumulado,
    y el piso minimo de volumen se encarga de filtrar esos casos."""
    if not historial:
        return None
    objetivo = minuto_actual_int - minutos_atras
    referencia = None
    for s in historial:
        m = momentum._minuto_a_entero(s.get("minuto"))
        if m is None:
            continue
        if m <= objetivo:
            referencia = s
    if referencia is None:
        referencia = historial[0]
    return referencia


def _dominancia(snap_actual, snap_referencia, lado_favorito, lado_rival):
    """Proporcion de presion que le corresponde al favorito entre
    snap_referencia y snap_actual, mas el volumen total combinado
    (para el piso minimo)."""
    presion_fav, _ = momentum.calcular_presion(snap_actual, snap_referencia, lado_favorito)
    presion_riv, _ = momentum.calcular_presion(snap_actual, snap_referencia, lado_rival)
    proporcion = momentum.momentum_relativo(presion_fav, presion_riv)
    volumen = presion_fav + presion_riv
    return proporcion, volumen


def _evaluar_dominancia_general(partido, minuto_int):
    lado_favorito = "local" if partido["favorito_es_local"] else "visitante"
    lado_rival = "visitante" if partido["favorito_es_local"] else "local"
    historial = partido.get("historial_snapshots", [])
    snap_actual = historial[-1]

    snap_sostenido = _snapshot_referencia(historial, minuto_int, VENTANA_SOSTENIDO_MINUTOS)
    prop_sostenido, volumen = _dominancia(snap_actual, snap_sostenido, lado_favorito, lado_rival)
    if volumen < VOLUMEN_MINIMO_VENTANA:
        return None  # sin evidencia suficiente todavia -- no se evalua

    snap_reciente = historial[-2] if len(historial) >= 2 else None
    prop_reciente, _ = _dominancia(snap_actual, snap_reciente, lado_favorito, lado_rival)

    # Lado favorito
    score_fav = prop_sostenido
    if prop_reciente >= UMBRAL_INTENSIDAD_RECIENTE:
        score_fav += BONO_INTENSIDAD_RECIENTE
    if score_fav >= UMBRAL_DOMINANCIA_GENERAL:
        return "favorito", min(1.0, score_fav)

    # Lado rival (mismo estandar, simetrico)
    prop_sostenido_riv = 1 - prop_sostenido
    prop_reciente_riv = 1 - prop_reciente
    score_riv = prop_sostenido_riv
    if prop_reciente_riv >= UMBRAL_INTENSIDAD_RECIENTE:
        score_riv += BONO_INTENSIDAD_RECIENTE
    if score_riv >= UMBRAL_DOMINANCIA_GENERAL:
        return "rival", min(1.0, score_riv)

    return None


def _evaluar_dominancia_1er_tiempo(partido, minuto_int):
    """Mismo mecanismo de 2 capas que la general, pero mas permisiva
    (umbral 60% en vez de 70%, bono de solo +5% en vez de +10%) -- a
    proposito, para capturar 'algo se esta cocinando antes del
    descanso', no dominancia ya confirmada. Solo mira al favorito."""
    lado_favorito = "local" if partido["favorito_es_local"] else "visitante"
    lado_rival = "visitante" if partido["favorito_es_local"] else "local"
    historial = partido.get("historial_snapshots", [])
    snap_actual = historial[-1]

    snap_sostenido = _snapshot_referencia(historial, minuto_int, VENTANA_SOSTENIDO_MINUTOS)
    prop_sostenido, volumen = _dominancia(snap_actual, snap_sostenido, lado_favorito, lado_rival)
    if volumen < VOLUMEN_MINIMO_VENTANA:
        return None

    snap_reciente = historial[-2] if len(historial) >= 2 else None
    prop_reciente, _ = _dominancia(snap_actual, snap_reciente, lado_favorito, lado_rival)

    score = prop_sostenido
    if prop_reciente >= UMBRAL_INTENSIDAD_RECIENTE:
        score += BONO_1ER_TIEMPO

    if score >= UMBRAL_DOMINANCIA_1ER_TIEMPO:
        return min(1.0, score)
    return None


def _texto_alerta_favorito(diferencia, minuto_int, score):
    if diferencia <= 0 and minuto_int >= MINUTO_INICIO_CIERRE and score >= DOMINANCIA_CIERRE:
        return "gol_de_cierre", f"\u23F0 Posible gol de cierre -- dominancia acumulada alta ({round(score*100)}%) en el tramo final."
    if diferencia < 0:
        return "posible_empate", f"\U0001F7E0 Posible empate -- el favorito domina ({round(score*100)}%)."
    if diferencia == 0:
        return "posible_victoria_favorito", f"\U0001F7E2 Posible victoria del favorito -- domina claramente ({round(score*100)}%)."
    return "ampliacion_marcador", f"\U0001F535 Posible ampliacion de marcador -- sigue dominando ({round(score*100)}%)."


def _evaluar_alertas(partido, snap_actual, snap_anterior, minuto):
    favorito_es_local = partido["favorito_es_local"]
    gl, gv = snap_actual["goles_local"], snap_actual["goles_visitante"]
    goles_favorito = gl if favorito_es_local else gv
    goles_rival = gv if favorito_es_local else gl
    diferencia = goles_favorito - goles_rival

    lado_favorito = "local" if favorito_es_local else "visitante"
    lado_rival = "visitante" if favorito_es_local else "local"

    # --- Eventos discretos: inmediatos, sin filtro de minuto minimo ---
    if abs(diferencia) >= DIFERENCIA_TECHO:
        if partido.get("diferencia_maxima_alcanzada", 0) < DIFERENCIA_TECHO:
            partido["diferencia_maxima_alcanzada"] = abs(diferencia)
            return [("partido_resuelto", "\U0001F3C1 Seguimiento cerrado -- diferencia de 3+ goles.")]
        return []

    if momentum.hubo_tarjeta_roja(snap_actual, snap_anterior, lado_rival) or \
       momentum.hubo_tarjeta_roja(snap_actual, snap_anterior, lado_favorito):
        if not _ya_se_envio_reciente(partido, "tarjeta_roja", minuto, ventana=999):
            return [("tarjeta_roja", "\U0001F7E5 Tarjeta roja detectada.")]

    if momentum.hubo_penal(snap_actual, snap_anterior, lado_favorito) or \
       momentum.hubo_penal(snap_actual, snap_anterior, lado_rival):
        if not _ya_se_envio_reciente(partido, "penal", minuto, ventana=15):
            return [("penal", "\U0001F3AF Penal detectado.")]

    minuto_int = momentum._minuto_a_entero(minuto) or 45
    if minuto_int < MINUTO_MINIMO_ALERTA_MOMENTUM:
        return []

    # --- Alerta de primer tiempo: ventana y umbral propios, mas suave ---
    if gl == 0 and gv == 0 and MINUTO_INICIO_1ER_TIEMPO <= minuto_int <= MINUTO_FIN_1ER_TIEMPO:
        score_1t = _evaluar_dominancia_1er_tiempo(partido, minuto_int)
        if score_1t is not None and not _ya_se_envio_reciente(partido, "alerta_1er_tiempo", minuto_int, ventana=999):
            return [("alerta_1er_tiempo",
                      f"\u23F1\uFE0F Alerta de primer tiempo -- el favorito domina el 0-0 ({round(score_1t*100)}%).")]

    # --- Dominancia general (dos capas), favorito o rival ---
    resultado = _evaluar_dominancia_general(partido, minuto_int)
    if resultado:
        lado_resultado, score = resultado
        if lado_resultado == "favorito":
            tipo, texto = _texto_alerta_favorito(diferencia, minuto_int, score)
        else:
            tipo = "cuidado_rival_presiona"
            texto = f"\u26A0\uFE0F Cuidado -- el rival esta dominando ({round(score*100)}%)."
        if tipo and not _ya_se_envio_reciente(partido, tipo, minuto_int):
            return [(tipo, texto)]

    return []


def _mensaje_partido(partido, minuto, snap_actual, texto, mom_favorito=None, prob_gol_fav=None, prob_gol_riv=None):
    """
    AMPLIADO a pedido explicito: antes solo mostraba tiros a puerta y
    posesion -- insuficiente para que la persona juzgue por si misma si
    de verdad hay ataque real o paridad. Ahora trae TODOS los numeros
    crudos que ya usa el calculo de momentum (no solo la conclusion),
    para que el criterio final sea del usuario, no solo del sistema.

    CORREGIDO (agosto 2026, a pedido explicito): antes esta funcion
    armaba el titulo con local/visitante (orden fijo) pero la fila de
    "favorito vs no_favorito" y TODAS las estadisticas con
    favorito/no_favorito (orden que cambia segun quien sea el
    favorito) -- si el visitante era el favorito, las estadisticas
    salian en orden inverso al titulo, y no habia forma de saber de
    quien era cada numero sin adivinar. Ahora TODO el mensaje respeta
    siempre el orden local -> visitante, sin excepcion; el favorito se
    marca unicamente con la corona junto a su nombre, nunca reordenando
    quien va primero.
    """
    fav_local = partido["favorito_es_local"]
    corona_local = f" {CORONA_FAVORITO}" if fav_local else ""
    corona_visitante = f" {CORONA_FAVORITO}" if not fav_local else ""
    emoji_tipo = EMOJI_TIPO_PRONOSTICO.get(partido.get("tipo_pronostico"), EMOJI_TIPO_PRONOSTICO["favorito_directo"])

    stats_local = snap_actual["stats_local"]
    stats_visitante = snap_actual["stats_visitante"]

    def _n(stats, campo):
        return stats.get(campo, "?")

    titulo = (
        f"<b>{escapar_html(partido['local'])}{corona_local}</b> vs "
        f"<b>{escapar_html(partido['visitante'])}{corona_visitante}</b>"
    )

    lineas = [
        texto,
        f"{emoji_tipo} {titulo} -- min {minuto}",
        f"Marcador: {snap_actual['goles_local']}-{snap_actual['goles_visitante']}",
        f"Favorito: {escapar_html(partido['favorito'])} (Google Sheets)",
        "",
        "\U0001F4CA <i>Estadisticas (siempre Local vs Visitante):</i>",
        f"Tiros totales: {_n(stats_local,'totalShots')} vs {_n(stats_visitante,'totalShots')}",
        f"Tiros a puerta: {_n(stats_local,'shotsOnTarget')} vs {_n(stats_visitante,'shotsOnTarget')}",
        f"Tiros bloqueados: {_n(stats_local,'blockedShots')} vs {_n(stats_visitante,'blockedShots')}",
        f"Corners: {_n(stats_local,'wonCorners')} vs {_n(stats_visitante,'wonCorners')}",
        f"Faltas: {_n(stats_local,'foulsCommitted')} vs {_n(stats_visitante,'foulsCommitted')}",
        f"Posesion: {_n(stats_local,'possessionPct')}% vs {_n(stats_visitante,'possessionPct')}%",
    ]

    if mom_favorito is not None:
        lineas.append("")
        lineas.append(f"Momentum (presion reciente): {round(mom_favorito*100)}% favorito / {round((1-mom_favorito)*100)}% rival")
    if prob_gol_fav is not None and prob_gol_riv is not None:
        lineas.append(f"Prob. de gol en los proximos ~15 min: favorito {round(prob_gol_fav*100)}% / rival {round(prob_gol_riv*100)}%")

    return "\n".join(lineas)


def vigilar():
    datos = _cargar()
    if not datos:
        print("No hay partidos_hoy.json todavia. Se reintentara en el proximo ciclo.")
        return

    hubo_cambios = False
    for partido in datos["partidos"]:
        if partido.get("acierto") is not None or not partido.get("fixture_id"):
            continue
        if not _en_ventana_horaria(partido):
            continue

        liga_slug = partido.get("liga_slug")
        if not liga_slug:
            print(f"[AVISO] {partido['partido']} no tiene liga_slug guardado, no se puede vigilar.")
            continue

        box = obtener_boxscore_en_vivo(liga_slug, partido["fixture_id"])
        if box is None or box.get("estado") != "in":
            continue

        snap_actual = {
            "minuto": box["minuto"], "goles_local": box["goles_local"],
            "goles_visitante": box["goles_visitante"],
            "stats_local": box["stats_local"], "stats_visitante": box["stats_visitante"],
        }
        historial = partido.setdefault("historial_snapshots", [])
        snap_anterior = historial[-1] if historial else None
        historial.append(snap_actual)
        hubo_cambios = True

        alertas = _evaluar_alertas(partido, snap_actual, snap_anterior, box["minuto"])
        if alertas:
            lado_favorito = "local" if partido["favorito_es_local"] else "visitante"
            lado_rival = "visitante" if partido["favorito_es_local"] else "local"
            presion_fav, _ = momentum.calcular_presion(snap_actual, snap_anterior, lado_favorito)
            presion_riv, _ = momentum.calcular_presion(snap_actual, snap_anterior, lado_rival)
            mom_favorito = momentum.momentum_relativo(presion_fav, presion_riv)
            prob_gol_fav = momentum.probabilidad_gol_ventana(snap_actual, snap_anterior, lado_favorito, box["minuto"])
            prob_gol_riv = momentum.probabilidad_gol_ventana(snap_actual, snap_anterior, lado_rival, box["minuto"])

        for tipo, texto in alertas:
            mensaje = _mensaje_partido(partido, box["minuto"], snap_actual, texto,
                                        mom_favorito=mom_favorito, prob_gol_fav=prob_gol_fav, prob_gol_riv=prob_gol_riv)
            if enviar_mensaje_telegram(mensaje):
                _registrar_alerta(partido, tipo, texto, box["minuto"])

    if hubo_cambios:
        _guardar(datos)


if __name__ == "__main__":
    vigilar()
