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
# NUEVO SISTEMA (agosto 2026, a pedido explicito) -- reemplaza el
# anterior de 2 capas (ventana sostenida + bono de ventana reciente)
# por decaimiento exponencial + z-score de confianza estadistica (ver
# momentum.py). En vez de ventanas fijas y un piso de volumen
# inventado a mano, cada evento pesa menos mientras mas viejo es, y el
# umbral de disparo es "que tan lejos esta del 50/50, en desviaciones
# estandar" -- eso ya incorpora el problema del volumen de forma
# matematica, sin necesitar un piso aparte.
# =====================================================================

UMBRAL_Z_ALERTA = momentum.UMBRAL_Z_CONFIANZA_MEDIA    # ~90% de confianza
UMBRAL_Z_CIERRE = momentum.UMBRAL_Z_CONFIANZA_ALTA      # ~95% de confianza, para "gol de cierre"
UMBRAL_Z_1ER_TIEMPO = 1.28                              # ~80%, mas permisivo a proposito
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


def _presiones_y_eventos(historial, minuto_int, lado_favorito, lado_rival):
    """Calcula, para el favorito y el rival, la presion ponderada por
    tiempo (decide quien domina) y el conteo de eventos ponderado por
    tiempo (decide que tan confiable es esa lectura). Ver momentum.py."""
    presion_fav = momentum.presion_ponderada_por_tiempo(historial, minuto_int, lado_favorito)
    presion_riv = momentum.presion_ponderada_por_tiempo(historial, minuto_int, lado_rival)
    n_fav = momentum.eventos_ponderados_por_tiempo(historial, minuto_int, lado_favorito)
    n_riv = momentum.eventos_ponderados_por_tiempo(historial, minuto_int, lado_rival)
    return presion_fav, presion_riv, n_fav, n_riv


def _evaluar_dominancia_general(partido, minuto_int):
    """Devuelve (lado_ganador, dominancia_%, z) si algun lado supera el
    umbral de confianza, o None. lado_ganador es 'favorito' o 'rival'."""
    lado_favorito = "local" if partido["favorito_es_local"] else "visitante"
    lado_rival = "visitante" if partido["favorito_es_local"] else "local"
    historial = partido.get("historial_snapshots", [])

    presion_fav, presion_riv, n_fav, n_riv = _presiones_y_eventos(historial, minuto_int, lado_favorito, lado_rival)
    z, dominancia_fav = momentum.z_score_dominancia(presion_fav, presion_riv, n_fav, n_riv)

    if z >= UMBRAL_Z_ALERTA:
        return "favorito", dominancia_fav, z
    if -z >= UMBRAL_Z_ALERTA:
        return "rival", 1 - dominancia_fav, -z
    return None


def _evaluar_dominancia_1er_tiempo(partido, minuto_int):
    """Mismo mecanismo que la general, pero con un umbral de confianza
    mas bajo (~80% en vez de ~90%) a proposito -- cubre 'algo se esta
    cocinando antes del descanso', no dominancia ya confirmada. Solo
    mira al favorito."""
    lado_favorito = "local" if partido["favorito_es_local"] else "visitante"
    lado_rival = "visitante" if partido["favorito_es_local"] else "local"
    historial = partido.get("historial_snapshots", [])

    presion_fav, presion_riv, n_fav, n_riv = _presiones_y_eventos(historial, minuto_int, lado_favorito, lado_rival)
    z, dominancia_fav = momentum.z_score_dominancia(presion_fav, presion_riv, n_fav, n_riv)

    if z >= UMBRAL_Z_1ER_TIEMPO:
        return dominancia_fav, z
    return None


def _texto_alerta_favorito(diferencia, minuto_int, dominancia_pct, z):
    conf = momentum.etiqueta_confianza(z)
    if diferencia <= 0 and minuto_int >= MINUTO_INICIO_CIERRE and z >= UMBRAL_Z_CIERRE:
        return "gol_de_cierre", f"\u23F0 Posible gol de cierre -- dominancia alta ({round(dominancia_pct*100)}%, confianza {conf}) en el tramo final."
    if diferencia < 0:
        return "posible_empate", f"\U0001F7E0 Posible empate -- el favorito domina ({round(dominancia_pct*100)}%, confianza {conf})."
    if diferencia == 0:
        return "posible_victoria_favorito", f"\U0001F7E2 Posible victoria del favorito -- domina claramente ({round(dominancia_pct*100)}%, confianza {conf})."
    return "ampliacion_marcador", f"\U0001F535 Posible ampliacion de marcador -- sigue dominando ({round(dominancia_pct*100)}%, confianza {conf})."


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

    if momentum.hubo_tarjeta_roja(snap_actual, snap_anterior, lado_rival):
        if not _ya_se_envio_reciente(partido, "tarjeta_roja", minuto, ventana=999):
            equipo = partido['visitante'] if lado_rival == "visitante" else partido['local']
            return [("tarjeta_roja", f"\U0001F7E5 Tarjeta roja para {equipo}.")]
    if momentum.hubo_tarjeta_roja(snap_actual, snap_anterior, lado_favorito):
        if not _ya_se_envio_reciente(partido, "tarjeta_roja", minuto, ventana=999):
            equipo = partido['local'] if lado_favorito == "local" else partido['visitante']
            return [("tarjeta_roja", f"\U0001F7E5 Tarjeta roja para {equipo}.")]

    if momentum.hubo_penal(snap_actual, snap_anterior, lado_favorito):
        if not _ya_se_envio_reciente(partido, "penal", minuto, ventana=15):
            equipo = partido['local'] if lado_favorito == "local" else partido['visitante']
            return [("penal", f"\U0001F3AF Penal para {equipo}.")]
    if momentum.hubo_penal(snap_actual, snap_anterior, lado_rival):
        if not _ya_se_envio_reciente(partido, "penal", minuto, ventana=15):
            equipo = partido['visitante'] if lado_rival == "visitante" else partido['local']
            return [("penal", f"\U0001F3AF Penal para {equipo}.")]

    minuto_int = momentum._minuto_a_entero(minuto) or 45
    if minuto_int < MINUTO_MINIMO_ALERTA_MOMENTUM:
        return []

    # --- Alerta de primer tiempo: ventana y umbral propios, mas suave ---
    if gl == 0 and gv == 0 and MINUTO_INICIO_1ER_TIEMPO <= minuto_int <= MINUTO_FIN_1ER_TIEMPO:
        score_1t = _evaluar_dominancia_1er_tiempo(partido, minuto_int)
        if score_1t is not None and not _ya_se_envio_reciente(partido, "alerta_1er_tiempo", minuto_int, ventana=999):
            return [("alerta_1er_tiempo",
                      f"\u23F1\uFE0F Alerta de primer tiempo -- el favorito domina el 0-0 ({round(score_1t*100)}%).")]

    # --- Dominancia general (decaimiento exponencial + z-score), favorito o rival ---
    resultado = _evaluar_dominancia_general(partido, minuto_int)
    if resultado:
        lado_resultado, dominancia_pct, z = resultado
        if lado_resultado == "favorito":
            tipo, texto = _texto_alerta_favorito(diferencia, minuto_int, dominancia_pct, z)
        else:
            tipo = "cuidado_rival_presiona"
            conf = momentum.etiqueta_confianza(z)
            texto = f"\u26A0\uFE0F Cuidado -- el rival esta dominando ({round(dominancia_pct*100)}%, confianza {conf})."
        if tipo and not _ya_se_envio_reciente(partido, tipo, minuto_int):
            return [(tipo, texto)]

    return []


def _mensaje_partido(partido, minuto, snap_actual, texto, dominancia_fav=None, z=None):
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

    AJUSTADO de nuevo (agosto 2026, a pedido explicito):
      - Se quita "(Google Sheets)" de la linea de favorito -- no aporta nada.
      - Se agrega la cuota inicial del partido (si se guardo al seleccionarlo).
      - Se reemplaza "Momentum/Prob. de gol" por la nueva metrica de
        dominancia ponderada por decaimiento exponencial + z-score de
        confianza estadistica (ver momentum.py) -- las estadisticas
        acumuladas del partido completo se SIGUEN mostrando igual que
        antes, esta es una linea ADICIONAL, no un reemplazo.
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
        f"Favorito: {escapar_html(partido['favorito'])}",
    ]

    cuota_l = partido.get("cuota_local_inicial")
    cuota_x = partido.get("cuota_empate_inicial")
    cuota_v = partido.get("cuota_visitante_inicial")
    if cuota_l or cuota_v:
        partes_cuota = [f"{escapar_html(partido['local'])} {cuota_l}" if cuota_l else None,
                         f"Empate {cuota_x}" if cuota_x else None,
                         f"{escapar_html(partido['visitante'])} {cuota_v}" if cuota_v else None]
        lineas.append("Cuota inicial: " + " | ".join(p for p in partes_cuota if p))

    lineas += [
        "",
        "\U0001F4CA <i>Estadisticas acumuladas del partido (siempre Local vs Visitante):</i>",
        f"Tiros totales: {_n(stats_local,'totalShots')} vs {_n(stats_visitante,'totalShots')}",
        f"Tiros a puerta: {_n(stats_local,'shotsOnTarget')} vs {_n(stats_visitante,'shotsOnTarget')}",
        f"Tiros bloqueados: {_n(stats_local,'blockedShots')} vs {_n(stats_visitante,'blockedShots')}",
        f"Corners: {_n(stats_local,'wonCorners')} vs {_n(stats_visitante,'wonCorners')}",
        f"Faltas: {_n(stats_local,'foulsCommitted')} vs {_n(stats_visitante,'foulsCommitted')}",
        f"Posesion: {_n(stats_local,'possessionPct')}% vs {_n(stats_visitante,'possessionPct')}%",
    ]

    if dominancia_fav is not None and z is not None:
        conf = momentum.etiqueta_confianza(z)
        lado_domina = partido['favorito'] if z >= 0 else partido['no_favorito']
        dominancia_mostrada = dominancia_fav if z >= 0 else (1 - dominancia_fav)
        lineas.append("")
        lineas.append(f"\U0001F4C8 Dominancia ponderada (mas peso a lo reciente): "
                       f"{round(dominancia_mostrada*100)}% a favor de {escapar_html(lado_domina)}")
        lineas.append(f"Confianza estadistica: {conf} (z={z:.2f})")

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
            presion_fav, presion_riv, n_fav, n_riv = _presiones_y_eventos(historial, box["minuto"], lado_favorito, lado_rival)
            z, dominancia_fav = momentum.z_score_dominancia(presion_fav, presion_riv, n_fav, n_riv)

        for tipo, texto in alertas:
            mensaje = _mensaje_partido(partido, box["minuto"], snap_actual, texto,
                                        dominancia_fav=dominancia_fav, z=z)
            if enviar_mensaje_telegram(mensaje):
                _registrar_alerta(partido, tipo, texto, box["minuto"])

    if hubo_cambios:
        _guardar(datos)


if __name__ == "__main__":
    vigilar()
