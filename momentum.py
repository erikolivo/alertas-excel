"""
momentum.py
-----------
ADAPTADO a los campos reales que expone ESPN, confirmados EN VIVO el
09-ago-2026 contra Queretaro @ Seattle Sounders (Leagues Cup, minuto
77'). Mismo espiritu que la version anterior (separar por completo la
expectativa pre-partido del momentum en vivo -- ver monitor.py), pero
con menos granularidad de entrada.

CAMBIO DE FONDO vs la version anterior (API-Football):
  - ANTES: tiros dentro del area / fuera del area, pesados distinto.
  - AHORA: ESPN solo da tiros totales (totalShots) y tiros a puerta
    (shotsOnTarget) -- no hay desglose por ubicacion en la cancha. Se
    colapsa a un solo peso para "tiro que no fue a puerta". Se pierde
    matiz de calidad de la ocasion, no la logica de fondo.

GANANCIA que no tenia la version anterior: tarjeta roja (redCards) y
penal (penaltyKickShots) vienen en el MISMO boxscore que ya se pide
para tiros/corners -- ya no hace falta una peticion aparte de eventos
(antes costaba 1 peticion extra por revision).

SUSTITUCIONES: pendiente. El boxscore por equipo de ESPN no trae un
contador de cambios recientes -- bonus_sustituciones() se deja definida
por compatibilidad pero devuelve siempre 0 hasta confirmar si el array
de "plays" del summary trae esa informacion de forma utilizable. No se
inventa un valor sin evidencia real.
"""

import math

# --- Pesos de presion (simplificados: ya no hay tiros dentro/fuera del area) ---
PESO_TIRO_PUERTA = 3
PESO_TIRO_NO_PUERTA = 0.8    # antes: PESO_TIRO_AREA (2) + PESO_TIRO_FUERA_AREA (0.5), promediado
PESO_CORNER = 1
PESO_TIRO_BLOQUEADO = 0.5     # NUEVO -- ESPN lo da (blockedShots), no existia antes

ALPHA_SUAVIZADO = 1.0

TASA_CONVERSION_TIRO_PUERTA = 0.11
TASA_CONVERSION_TIRO_NO_PUERTA = 0.02
TASA_CONVERSION_CORNER = 0.02

FACTOR_URGENCIA_TRAMO_FINAL = 1.2
MINUTO_INICIO_URGENCIA_1ER_TIEMPO = 30
MINUTO_FIN_URGENCIA_1ER_TIEMPO = 45
MINUTO_INICIO_URGENCIA_2DO_TIEMPO = 75

BONUS_POR_CAMBIO_RECIENTE = 0.5
TOPE_BONUS_CAMBIOS = 2.0

VENTANA_MINUTOS_DEFECTO = 15
ZONA_PARIDAD_BAJA = 0.35
ZONA_PARIDAD_ALTA = 0.65


def _minuto_a_entero(minuto):
    try:
        return int(str(minuto).rstrip("'").split("+")[0])
    except (TypeError, ValueError):
        return None


def _stat(stats_dict, nombre, default=0.0):
    """Lee un valor del boxscore de ESPN (dict ya aplanado por
    fetch_data.obtener_boxscore_en_vivo: {nombre_stat: displayValue})."""
    valor = stats_dict.get(nombre)
    if valor is None:
        return default
    try:
        return float(valor)
    except (TypeError, ValueError):
        return default


def _delta_stat(stats_actual, stats_anterior, nombre):
    actual = _stat(stats_actual, nombre)
    if stats_anterior is None:
        return max(0.0, actual)
    anterior = _stat(stats_anterior, nombre)
    return max(0.0, actual - anterior)


def _delta_goles(snap_actual, snap_anterior, lado):
    """Goles nuevos de ese lado entre dos snapshots. Se usa para
    descontar de 'tiros a puerta' los que ya se concretaron en gol --
    a pedido explicito (agosto 2026): un tiro que ya entro no debe
    seguir empujando la presion como si fuera una amenaza pendiente,
    ya se convirtio, no predice nada hacia adelante."""
    clave_gol = "goles_local" if lado == "local" else "goles_visitante"
    actual = snap_actual.get(clave_gol, 0) or 0
    anterior = (snap_anterior.get(clave_gol, 0) or 0) if snap_anterior else 0
    return max(0, actual - anterior)


def _factor_urgencia(minuto_actual):
    minuto = _minuto_a_entero(minuto_actual)
    if minuto is None:
        return 1.0
    if MINUTO_INICIO_URGENCIA_1ER_TIEMPO <= minuto <= MINUTO_FIN_URGENCIA_1ER_TIEMPO:
        return FACTOR_URGENCIA_TRAMO_FINAL
    if minuto >= MINUTO_INICIO_URGENCIA_2DO_TIEMPO:
        return FACTOR_URGENCIA_TRAMO_FINAL
    return 1.0


def calcular_presion(snap_actual, snap_anterior, lado, xg_disponible=False):
    """
    Score de presion para un lado ('local' o 'visitante'), leyendo del
    boxscore real de ESPN guardado en snap_actual['stats_local'/
    'stats_visitante']. xg_disponible se deja por compatibilidad de
    firma -- ESPN no confirmo traer xG para futbol, siempre cae al
    proxy por tiros.
    """
    clave = "stats_local" if lado == "local" else "stats_visitante"
    stats_actual = snap_actual.get(clave, {})
    stats_anterior = snap_anterior.get(clave, {}) if snap_anterior else None

    total_tiros = _delta_stat(stats_actual, stats_anterior, "totalShots")
    tiros_puerta_bruto = _delta_stat(stats_actual, stats_anterior, "shotsOnTarget")
    goles_nuevos = _delta_goles(snap_actual, snap_anterior, lado)
    tiros_puerta = max(0.0, tiros_puerta_bruto - goles_nuevos)  # el tiro que ya fue gol no sigue empujando presion
    tiros_no_puerta = max(0.0, total_tiros - tiros_puerta_bruto)
    tiros_bloqueados = _delta_stat(stats_actual, stats_anterior, "blockedShots")
    corners = _delta_stat(stats_actual, stats_anterior, "wonCorners")
    posesion = _stat(stats_actual, "possessionPct")

    score = (tiros_puerta * PESO_TIRO_PUERTA) + (tiros_no_puerta * PESO_TIRO_NO_PUERTA) + \
            (corners * PESO_CORNER) + (tiros_bloqueados * PESO_TIRO_BLOQUEADO)

    detalle = {
        "tiros_puerta": tiros_puerta, "tiros_no_puerta": tiros_no_puerta,
        "tiros_bloqueados": tiros_bloqueados, "corners": corners, "posesion": posesion,
    }
    return score, detalle


def bonus_sustituciones(n_cambios_recientes):
    """Pendiente de confirmar con evidencia real si ESPN expone
    sustituciones utilizables en vivo (ver docstring del modulo).
    Devuelve 0 hasta entonces -- no suma ni resta nada al momentum."""
    return 0.0


def momentum_relativo(presion_a, presion_b, alpha=ALPHA_SUAVIZADO):
    """Suavizado tipo Laplace -- evita que una muestra minuscula de una
    lectura de dominio total. Sin presion de ningun lado, da 0.5."""
    return (presion_a + alpha) / (presion_a + presion_b + 2 * alpha)


# =====================================================================
# NUEVO ENFOQUE (agosto 2026, a pedido explicito) -- decaimiento
# exponencial + z-score de confianza estadistica, en vez del sistema de
# 2 capas (ventana sostenida de 20 min + bono de la ventana reciente).
#
# LA IDEA: en vez de cortes duros de tiempo (todo dentro de 20 min
# cuenta igual, todo afuera no cuenta nada), cada evento pesa menos
# mientras mas viejo es, de forma continua. Y en vez de un % de
# dominancia con un piso de volumen fijo e inventado a mano, se calcula
# que tan lejos esta ese % de un reparto 50/50 parejo, EN DESVIACIONES
# ESTANDAR (z-score) -- eso resuelve el problema del volumen de forma
# matematica: con pocos eventos, hace falta un % mas extremo para
# alcanzar el mismo z-score que con muchos eventos.
# =====================================================================

VIDA_MEDIA_MINUTOS = 10.0  # cada 10 min, un evento pesa la mitad


def peso_decaimiento(minutos_de_antiguedad):
    """0.5 ^ (antiguedad / vida_media) -- un evento de hace 10 min pesa
    la mitad que uno de ahora mismo; uno de hace 20 min, un cuarto."""
    antiguedad = max(0.0, minutos_de_antiguedad)
    return 0.5 ** (antiguedad / VIDA_MEDIA_MINUTOS)


def presion_ponderada_por_tiempo(historial_snapshots, minuto_actual, lado):
    """Recorre todo el historial de snapshots del partido (no una
    ventana fija) y suma la presion de cada intervalo entre snapshots
    consecutivos, ponderada por que tan reciente es ese intervalo
    respecto al minuto actual. Eventos viejos siguen contando, pero
    cada vez menos -- sin el corte abrupto de una ventana fija.
    Esta presion (con el peso por TIPO de evento, ver calcular_presion)
    es la que decide QUIEN domina -- para decidir que tan CONFIABLE es
    esa lectura se usa eventos_ponderados_por_tiempo() en cambio (ver
    docstring de z_score_dominancia)."""
    total = 0.0
    minuto_actual_int = _minuto_a_entero(minuto_actual)
    if minuto_actual_int is None or not historial_snapshots:
        return total
    for i in range(1, len(historial_snapshots)):
        snap_prev = historial_snapshots[i - 1]
        snap_actual = historial_snapshots[i]
        presion_intervalo, _ = calcular_presion(snap_actual, snap_prev, lado)
        m = _minuto_a_entero(snap_actual.get("minuto"))
        if m is None:
            continue
        antiguedad = minuto_actual_int - m
        total += presion_intervalo * peso_decaimiento(antiguedad)
    return total


def eventos_ponderados_por_tiempo(historial_snapshots, minuto_actual, lado):
    """Conteo CRUDO de eventos (tiros + corners, SIN el peso x3/x0.8/x1/
    x0.5 por tipo -- solo con el peso por tiempo). Se usa como tamano
    de muestra (n) para el z-score de confianza -- si se usara la
    presion YA ponderada por tipo como 'n', un solo tiro a puerta (peso
    3) pareceria mas evidencia de la que realmente es, solo por su
    peso, no por su cantidad. Aqui cada evento cuenta 1, sin importar
    el tipo -- lo unico que decae es que tan viejo es."""
    total = 0.0
    minuto_actual_int = _minuto_a_entero(minuto_actual)
    if minuto_actual_int is None or not historial_snapshots:
        return total
    clave = "stats_local" if lado == "local" else "stats_visitante"
    for i in range(1, len(historial_snapshots)):
        stats_prev = historial_snapshots[i - 1].get(clave, {})
        stats_actual_i = historial_snapshots[i].get(clave, {})
        n_intervalo = _delta_stat(stats_actual_i, stats_prev, "totalShots") + \
                      _delta_stat(stats_actual_i, stats_prev, "wonCorners")
        m = _minuto_a_entero(historial_snapshots[i].get("minuto"))
        if m is None:
            continue
        antiguedad = minuto_actual_int - m
        total += n_intervalo * peso_decaimiento(antiguedad)
    return total


def z_score_dominancia(presion_ponderada_lado_a, presion_ponderada_lado_b,
                        n_ponderado_lado_a, n_ponderado_lado_b):
    """Que tan lejos esta el reparto de PRESION (ponderada por tipo de
    evento) de un 50/50 parejo, en desviaciones estandar -- usando el
    conteo CRUDO de eventos (sin peso por tipo, solo por tiempo) como
    tamano de muestra (n) para el error estandar. Con pocos eventos,
    hace falta un % de dominancia mas extremo para alcanzar el mismo
    z-score que con muchos -- sin necesitar un piso de volumen fijo
    inventado a mano.

    Devuelve (z, dominancia_lado_a). z positivo = domina lado_a;
    z negativo = domina lado_b."""
    total_presion = presion_ponderada_lado_a + presion_ponderada_lado_b
    if total_presion <= 0:
        return 0.0, 0.5
    dominancia_a = presion_ponderada_lado_a / total_presion

    n_total = n_ponderado_lado_a + n_ponderado_lado_b
    if n_total <= 0:
        return 0.0, dominancia_a
    error_estandar = math.sqrt(0.25 / n_total)
    if error_estandar <= 0:
        return 0.0, dominancia_a
    z = (dominancia_a - 0.5) / error_estandar
    return z, dominancia_a


UMBRAL_Z_CONFIANZA_MEDIA = 1.64   # ~90% de confianza
UMBRAL_Z_CONFIANZA_ALTA = 2.0     # ~95% de confianza


def etiqueta_confianza(z):
    z_abs = abs(z)
    if z_abs >= UMBRAL_Z_CONFIANZA_ALTA:
        return "ALTA"
    if z_abs >= UMBRAL_Z_CONFIANZA_MEDIA:
        return "MEDIA"
    return "BAJA"


def probabilidad_gol_ventana(snap_actual, snap_anterior, lado, minuto_actual, xg_disponible=False):
    clave = "stats_local" if lado == "local" else "stats_visitante"
    stats_actual = snap_actual.get(clave, {})
    stats_anterior = snap_anterior.get(clave, {}) if snap_anterior else None

    total_tiros = _delta_stat(stats_actual, stats_anterior, "totalShots")
    tiros_puerta_bruto = _delta_stat(stats_actual, stats_anterior, "shotsOnTarget")
    goles_nuevos = _delta_goles(snap_actual, snap_anterior, lado)
    tiros_puerta = max(0.0, tiros_puerta_bruto - goles_nuevos)  # el tiro que ya fue gol no sigue empujando presion
    tiros_no_puerta = max(0.0, total_tiros - tiros_puerta_bruto)
    corners = _delta_stat(stats_actual, stats_anterior, "wonCorners")

    lam = (tiros_puerta * TASA_CONVERSION_TIRO_PUERTA) + \
          (tiros_no_puerta * TASA_CONVERSION_TIRO_NO_PUERTA) + \
          (corners * TASA_CONVERSION_CORNER)

    lam *= _factor_urgencia(minuto_actual)
    return 1 - math.exp(-lam)


def zona_momentum(momentum_favorito):
    if momentum_favorito >= ZONA_PARIDAD_ALTA:
        return "favorito"
    if momentum_favorito <= ZONA_PARIDAD_BAJA:
        return "rival"
    return "paridad"


def hubo_tarjeta_roja(snap_actual, snap_anterior, lado):
    """NUEVO vs la version anterior: viene del MISMO boxscore de
    tiros/corners, sin peticion aparte de eventos."""
    clave = "stats_local" if lado == "local" else "stats_visitante"
    stats_anterior = snap_anterior.get(clave, {}) if snap_anterior else None
    return _delta_stat(snap_actual.get(clave, {}), stats_anterior, "redCards") > 0


def hubo_penal(snap_actual, snap_anterior, lado):
    """NUEVO vs la version anterior: idem, mismo boxscore."""
    clave = "stats_local" if lado == "local" else "stats_visitante"
    stats_anterior = snap_anterior.get(clave, {}) if snap_anterior else None
    return _delta_stat(snap_actual.get(clave, {}), stats_anterior, "penaltyKickShots") > 0
