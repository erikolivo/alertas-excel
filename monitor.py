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
from cerrar_resultados import calcular_acierto
import momentum

DATA_DIR = Path(__file__).parent / "data"
ARCHIVO_PARTIDOS = DATA_DIR / "partidos_hoy.json"

MINUTO_INICIO_CIERRE = 75

MINUTO_MINIMO_ALERTA_MOMENTUM = 15

# Emoji al inicio de cada mensaje segun el TIPO de pronostico (a pedido
# explicito, agosto 2026) -- distinto de la corona junto al nombre del
# equipo, que indica A QUIEN favorece. Van pegados juntos (tipo primero,
# corona despues) al lado del nombre del equipo favorito.
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

# =====================================================================
# UMBRAL PROGRESIVO POR DIFERENCIA DE GOLES (agosto 2026, a pedido
# explicito) -- SOLO para favorito_directo, y SOLO para el lado del
# favorito (el umbral del rival no cambia). Mientras mas ventaja tiene
# el favorito, mas dificil que dispare una alerta de "viene otro gol"
# -- cada alerta adicional aporta menos informacion nueva una vez que
# ya se sabe que domina y va ganando.
#
# Se basa en la DIFERENCIA neta actual (favorito - rival), no en el
# conteo absoluto de goles del favorito -- si el rival descuenta, la
# diferencia baja y el umbral vuelve a bajar con ella, sin memoria de
# lo estricto que llego a estar (confirmado a pedido explicito: 3-2 se
# trata igual que 1-0, ambos son diferencia +1).
#
# NOTA para revisiones futuras: se decidio A PROPOSITO mas estricto (no
# al reves, mas facil) mientras mas gana el favorito -- hay un
# argumento real en el sentido contrario (el equipo que pierde puede
# desmoronarse psicologicamente), pero se opto por no adivinar y en
# cambio dejar que cada alerta registre la diferencia de goles al
# momento de enviarse (ver _registrar_alerta) para poder revisar con
# evidencia real del Excel, dentro de unas semanas, si conviene
# invertir esta logica.
# =====================================================================
ESCALON_MAXIMO_UMBRAL = 2
INCREMENTO_POR_ESCALON = 0.4

# Multiplicadores de umbral por prioridad (a pedido explicito, agosto 2026)
# ALTA: sin cambio (multiplicador 1.0)
# MEDIA: +40% mas estricto (multiplicador 1.4)
# BAJA: +80% mas estricto (multiplicador 1.8)
MULTIPLICADOR_PRIORIDAD = {
    "ALTA": 1.0,
    "MEDIA": 1.4,
    "BAJA": 1.8,
}


def _umbral_efectivo_favorito(partido, diferencia):
    if partido.get("tipo_pronostico") != "favorito_directo":
        umbral_base = UMBRAL_Z_ALERTA
    else:
        escalon = max(0, min(diferencia, ESCALON_MAXIMO_UMBRAL))
        umbral_base = UMBRAL_Z_ALERTA + (escalon * INCREMENTO_POR_ESCALON)

    prioridad = partido.get("prioridad", "ALTA")
    multiplicador = MULTIPLICADOR_PRIORIDAD.get(prioridad, 1.0)
    return umbral_base * multiplicador


# =====================================================================
# CHEQUEO "SIGUEN EMPATADOS" (agosto 2026, a pedido explicito) -- red de
# seguridad por tiempo, SOLO para favorito_directo. En los minutos 22,
# 55 y 70, si el marcador sigue empatado, se pregunta con un chequeo
# BLANDO (solo que la presion del favorito sea mayor a la del rival,
# SIN exigir el umbral estadistico de z-score) si el favorito viene
# algo mejor. Su proposito es cubrir los casos donde hay una ventaja
# real pero nunca lo bastante clara como para que el sistema
# estadistico normal (z-score) la detectara por su cuenta.
#
# NUNCA duplica lo que el z-score ya avisó: si "posible_victoria_
# favorito" ya se mando en los ultimos VENTANA_ANTIDUP_CHEQUEO_EMPATE
# minutos, este chequeo se queda callado -- ya se avisó con mas
# certeza que lo que este chequeo blando podria aportar.
# =====================================================================
CHEQUEOS_EMPATE_MINUTOS = [22, 55, 70]
VENTANA_ANTIDUP_CHEQUEO_EMPATE = 25


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


def _registrar_alerta(partido, tipo, texto, minuto, diferencia_goles=None):
    partido.setdefault("alertas_enviadas", []).append({
        "tipo": tipo, "minuto": minuto, "texto": texto, "diferencia_goles": diferencia_goles,
    })


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


def _evaluar_dominancia_general(partido, minuto_int, diferencia):
    """Devuelve (lado_ganador, dominancia_%, z) si algun lado supera el
    umbral de confianza, o None. lado_ganador es 'favorito' o 'rival'.
    El umbral del lado favorito escala con la diferencia de goles a su
    favor (ver _umbral_efectivo_favorito); el umbral del rival se
    mantiene fijo."""
    lado_favorito = "local" if partido["favorito_es_local"] else "visitante"
    lado_rival = "visitante" if partido["favorito_es_local"] else "local"
    historial = partido.get("historial_snapshots", [])

    presion_fav, presion_riv, n_fav, n_riv = _presiones_y_eventos(historial, minuto_int, lado_favorito, lado_rival)
    z, dominancia_fav = momentum.z_score_dominancia(presion_fav, presion_riv, n_fav, n_riv)

    umbral_favorito = _umbral_efectivo_favorito(partido, diferencia)
    if z >= umbral_favorito:
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


def _texto_alerta_favorito(diferencia, minuto_int, dominancia_pct, z, prioridad="ALTA"):
    conf = momentum.etiqueta_confianza(z)
    marca_prioridad = f" [{prioridad}]" if prioridad != "ALTA" else ""
    if diferencia <= 0 and minuto_int >= MINUTO_INICIO_CIERRE and z >= UMBRAL_Z_CIERRE:
        return "gol_de_cierre", f"\u23F0 Posible gol de cierre{marca_prioridad} -- dominancia alta ({round(dominancia_pct*100)}%, confianza {conf}) en el tramo final."
    if diferencia < 0:
        return "posible_empate", f"\U0001F7E0 Posible empate{marca_prioridad} -- el favorito domina ({round(dominancia_pct*100)}%, confianza {conf})."
    if diferencia == 0:
        return "posible_victoria_favorito", f"\U0001F7E2 Posible victoria del favorito{marca_prioridad} -- domina claramente ({round(dominancia_pct*100)}%, confianza {conf})."
    return "ampliacion_marcador", f"\U0001F535 Posible ampliacion de marcador{marca_prioridad} -- sigue dominando ({round(dominancia_pct*100)}%, confianza {conf})."


def _evaluar_chequeo_empate(partido, minuto_int, snap_actual, historial):
    """Red de seguridad por tiempo -- ver comentario de las constantes
    CHEQUEOS_EMPATE_MINUTOS mas arriba."""
    if partido.get("tipo_pronostico") != "favorito_directo":
        return None
    if snap_actual["goles_local"] != snap_actual["goles_visitante"]:
        return None  # no esta empatado, no aplica

    for i, checkpoint in enumerate(CHEQUEOS_EMPATE_MINUTOS):
        limite_superior = CHEQUEOS_EMPATE_MINUTOS[i + 1] if i + 1 < len(CHEQUEOS_EMPATE_MINUTOS) else 200
        if not (checkpoint <= minuto_int < limite_superior):
            continue

        tipo_chequeo = f"siguen_empatados_{checkpoint}"
        if _ya_se_envio_reciente(partido, tipo_chequeo, minuto_int, ventana=999):
            return None  # este checkpoint ya se resolvio (se mando una vez)

        # Red de seguridad: si el z-score ya avisó de esto, no duplicar
        if _ya_se_envio_reciente(partido, "posible_victoria_favorito", minuto_int, ventana=VENTANA_ANTIDUP_CHEQUEO_EMPATE):
            return None

        # Chequeo BLANDO: basta con que el favorito tenga alguna ventaja
        # de presion, sin exigir el umbral estadistico de z-score
        lado_favorito = "local" if partido["favorito_es_local"] else "visitante"
        lado_rival = "visitante" if partido["favorito_es_local"] else "local"
        presion_fav = momentum.presion_ponderada_por_tiempo(historial, minuto_int, lado_favorito)
        presion_riv = momentum.presion_ponderada_por_tiempo(historial, minuto_int, lado_rival)
        if presion_fav <= presion_riv:
            return None  # sin ventaja todavia -- se reintenta en el siguiente ciclo, dentro de la misma franja

        return tipo_chequeo, f"\u23F1\uFE0F Siguen empatados (min {checkpoint}+) -- {escapar_html(partido['favorito'])} con ligera ventaja."

    return None


def _evaluar_alertas(partido, snap_actual, snap_anterior, minuto):
    favorito_es_local = partido["favorito_es_local"]
    gl, gv = snap_actual["goles_local"], snap_actual["goles_visitante"]
    goles_favorito = gl if favorito_es_local else gv
    goles_rival = gv if favorito_es_local else gl
    diferencia = goles_favorito - goles_rival

    lado_favorito = "local" if favorito_es_local else "visitante"
    lado_rival = "visitante" if favorito_es_local else "local"

    # --- Eventos discretos: inmediatos, sin filtro de minuto minimo ---
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
    resultado = _evaluar_dominancia_general(partido, minuto_int, diferencia)
    if resultado:
        lado_resultado, dominancia_pct, z = resultado
        prioridad = partido.get("prioridad", "ALTA")
        if lado_resultado == "favorito":
            tipo, texto = _texto_alerta_favorito(diferencia, minuto_int, dominancia_pct, z, prioridad)
        else:
            tipo = "cuidado_rival_presiona"
            conf = momentum.etiqueta_confianza(z)
            marca_prioridad = f" [{prioridad}]" if prioridad != "ALTA" else ""
            texto = f"\u26A0\uFE0F Cuidado{marca_prioridad} -- el rival esta dominando ({round(dominancia_pct*100)}%, confianza {conf})."
        if tipo and not _ya_se_envio_reciente(partido, tipo, minuto_int):
            return [(tipo, texto)]

    # --- Chequeo "siguen empatados" (red de seguridad por tiempo) ---
    historial = partido.get("historial_snapshots", [])
    resultado_chequeo = _evaluar_chequeo_empate(partido, minuto_int, snap_actual, historial)
    if resultado_chequeo:
        return [resultado_chequeo]

    return []


def _mensaje_partido(partido, minuto, snap_actual, texto, dominancia_fav=None, z=None):
    """
    AMPLIADO a pedido explicito: antes solo mostraba tiros a puerta y
    posesion -- insuficiente para que la persona juzgue por si misma si
    de verdad hay ataque real o paridad. Ahora trae TODOS los numeros
    crudos que ya usa el calculo de momentum (no solo la conclusion),
    para que el criterio final sea del usuario, no solo del sistema.

    CORREGIDO (agosto 2026, a pedido explicito): TODO el mensaje
    respeta siempre el orden local -> visitante, sin excepcion; el
    favorito se marca unicamente con la corona junto a su nombre, nunca
    reordenando quien va primero.

    REESTRUCTURADO de nuevo (agosto 2026, a pedido explicito):
      - El titulo "Alertas Excel" ya no va aqui -- telegram_utils.py lo
        antepone automaticamente a CUALQUIER mensaje que se envie (ver
        NOMBRE_PROYECTO), asi que no hace falta repetirlo.
      - Orden del mensaje: (1) tipo de alerta, (2) estadisticas
        acumuladas -- tiros a puerta, tiros totales, corners, tiros
        bloqueados, posesion, faltas, y la confianza estadistica como
        ULTIMA linea de ese mismo bloque (ya no es una seccion aparte),
        (3) datos del enfrentamiento (equipos, marcador, favorito,
        cuota inicial) al final, como referencia.
      - El emoji de tipo de pronostico (🎯/🔀) ahora va PEGADO a la
        corona (ej. "🎯👑"), junto al nombre del equipo favorito -- ya
        no antecede a toda la linea del titulo.
      - "Faltas" = faltas que ESE equipo cometio (foulsCommitted de
        ESPN), no las que recibio.
    """
    fav_local = partido["favorito_es_local"]
    emoji_tipo = EMOJI_TIPO_PRONOSTICO.get(partido.get("tipo_pronostico"), EMOJI_TIPO_PRONOSTICO["favorito_directo"])
    marca_favorito = f" {emoji_tipo}{CORONA_FAVORITO}"
    corona_local = marca_favorito if fav_local else ""
    corona_visitante = marca_favorito if not fav_local else ""

    stats_local = snap_actual["stats_local"]
    stats_visitante = snap_actual["stats_visitante"]

    def _n(stats, campo):
        return stats.get(campo, "?")

    titulo = (
        f"<b>{escapar_html(partido['local'])}{corona_local}</b> vs "
        f"<b>{escapar_html(partido['visitante'])}{corona_visitante}</b>"
    )

    lineas = [texto, ""]

    lineas.append("\U0001F4CA <i>Estadisticas acumuladas del partido (siempre Local vs Visitante):</i>")
    lineas.append(f"Tiros a puerta: {_n(stats_local,'shotsOnTarget')} vs {_n(stats_visitante,'shotsOnTarget')}")
    lineas.append(f"Tiros totales: {_n(stats_local,'totalShots')} vs {_n(stats_visitante,'totalShots')}")
    lineas.append(f"Corners: {_n(stats_local,'wonCorners')} vs {_n(stats_visitante,'wonCorners')}")
    lineas.append(f"Tiros bloqueados: {_n(stats_local,'blockedShots')} vs {_n(stats_visitante,'blockedShots')}")
    lineas.append(f"Posesion: {_n(stats_local,'possessionPct')}% vs {_n(stats_visitante,'possessionPct')}%")
    lineas.append(f"Faltas: {_n(stats_local,'foulsCommitted')} vs {_n(stats_visitante,'foulsCommitted')}")

    if dominancia_fav is not None and z is not None:
        conf = momentum.etiqueta_confianza(z)
        lado_domina = partido['favorito'] if z >= 0 else partido['no_favorito']
        dominancia_mostrada = dominancia_fav if z >= 0 else (1 - dominancia_fav)
        lineas.append(f"Confianza: {conf} ({round(dominancia_mostrada*100)}% a favor de {escapar_html(lado_domina)}, z={z:.2f})")

    lineas.append("")
    lineas.append(f"{titulo} -- min {minuto}")
    lineas.append(f"Marcador: {snap_actual['goles_local']}-{snap_actual['goles_visitante']}")
    lineas.append(f"Favorito: {escapar_html(partido['favorito'])}")

    cuota_l = partido.get("cuota_local_inicial")
    cuota_x = partido.get("cuota_empate_inicial")
    cuota_v = partido.get("cuota_visitante_inicial")
    if cuota_l or cuota_v:
        partes_cuota = [f"{escapar_html(partido['local'])} {cuota_l}" if cuota_l else None,
                         f"Empate {cuota_x}" if cuota_x else None,
                         f"{escapar_html(partido['visitante'])} {cuota_v}" if cuota_v else None]
        lineas.append("Cuota inicial: " + " | ".join(p for p in partes_cuota if p))

    return "\n".join(lineas)


def _mensaje_partido_finalizado(partido, gh, gv):
    """
    NUEVO (agosto 2026, a pedido explicito) -- aviso INMEDIATO cuando
    ESPN marca el partido como terminado, sin esperar al reporte de las
    6am del dia siguiente. Ya incluye si el pronostico acerto o no,
    usando el mismo criterio que cerrar_resultados.py (calcular_acierto
    compartido -- una sola fuente de verdad, para que este aviso en
    vivo y la auditoria nocturna nunca queden desincronizados).
    """
    acierto = calcular_acierto(partido, gh, gv)
    marca = "\u2705 Acierto" if acierto else "\u274C Fallo"

    fav_local = partido["favorito_es_local"]
    emoji_tipo = EMOJI_TIPO_PRONOSTICO.get(partido.get("tipo_pronostico"), EMOJI_TIPO_PRONOSTICO["favorito_directo"])
    marca_favorito = f" {emoji_tipo}{CORONA_FAVORITO}"
    corona_local = marca_favorito if fav_local else ""
    corona_visitante = marca_favorito if not fav_local else ""
    titulo = (
        f"<b>{escapar_html(partido['local'])}{corona_local}</b> vs "
        f"<b>{escapar_html(partido['visitante'])}{corona_visitante}</b>"
    )

    lineas = [
        "\U0001F3C1 Partido finalizado",
        "",
        f"{titulo}",
        f"Resultado final: {gh}-{gv}",
        f"Favorito: {escapar_html(partido['favorito'])}",
        f"{marca}",
    ]
    return "\n".join(lineas)


def vigilar():
    datos = _cargar()
    if not datos:
        print("No hay partidos_hoy.json todavia. Se reintentara en el proximo ciclo.")
        return

    hubo_cambios = False
    for partido in datos["partidos"]:
        # NUEVO: una vez que se manda el aviso de finalizado, ya no se
        # vuelve a consultar este partido en NINGUN ciclo posterior --
        # ahorro de peticiones (ya no tiene sentido seguir gastando
        # cupo de ESPN en un partido que ya termino). 'acierto' NO se
        # toca aqui a proposito -- eso lo sigue decidiendo
        # cerrar_resultados.py esa noche, con su propio flujo completo
        # (rating propio Glicko-2 + auditoria de cada alerta
        # individual), sin interferencia de este aviso en vivo.
        if partido.get("aviso_final_enviado") or not partido.get("fixture_id"):
            continue
        if not _en_ventana_horaria(partido):
            continue

        liga_slug = partido.get("liga_slug")
        if not liga_slug:
            print(f"[AVISO] {partido['partido']} no tiene liga_slug guardado, no se puede vigilar.")
            continue

        box = obtener_boxscore_en_vivo(liga_slug, partido["fixture_id"])
        if box is None:
            continue

        if box.get("estado") == "post":
            mensaje = _mensaje_partido_finalizado(partido, box["goles_local"], box["goles_visitante"])
            if enviar_mensaje_telegram(mensaje):
                partido["aviso_final_enviado"] = True
                hubo_cambios = True
            continue

        if box.get("estado") != "in":
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

        favorito_es_local = partido["favorito_es_local"]
        goles_favorito = box["goles_local"] if favorito_es_local else box["goles_visitante"]
        goles_rival = box["goles_visitante"] if favorito_es_local else box["goles_local"]
        diferencia_actual = goles_favorito - goles_rival

        alertas = _evaluar_alertas(partido, snap_actual, snap_anterior, box["minuto"])
        if alertas:
            lado_favorito = "local" if favorito_es_local else "visitante"
            lado_rival = "visitante" if favorito_es_local else "local"
            presion_fav, presion_riv, n_fav, n_riv = _presiones_y_eventos(historial, box["minuto"], lado_favorito, lado_rival)
            z, dominancia_fav = momentum.z_score_dominancia(presion_fav, presion_riv, n_fav, n_riv)

        for tipo, texto in alertas:
            mensaje = _mensaje_partido(partido, box["minuto"], snap_actual, texto,
                                        dominancia_fav=dominancia_fav, z=z)
            if enviar_mensaje_telegram(mensaje):
                _registrar_alerta(partido, tipo, texto, box["minuto"], diferencia_goles=diferencia_actual)

    if hubo_cambios:
        _guardar(datos)


if __name__ == "__main__":
    vigilar()
