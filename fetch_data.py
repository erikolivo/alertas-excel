"""
fetch_data.py
-------------
REESCRITO para la migracion de API-Football a ESPN (agosto 2026).

Por que: la cuenta de API-Football quedo suspendida (tres cuentas
bloqueadas al intentar reabrir), y el seguimiento en vivo es una pieza
central del proyecto, no negociable. Se investigo el backend JSON no
oficial que usa espn.com (site.api.espn.com), confirmado EN VIVO durante
esta migracion contra un partido real (Queretaro @ Seattle Sounders,
Leagues Cup, 09-ago-2026, minuto 77') antes de escribir este archivo.

DECISION DE DISENO (adaptador): las funciones de aqui devuelven datos en
la MISMA forma que ya esperaba el resto del sistema cuando venian de
API-Football (fixture.id, teams.home.id, league.country...). Asi,
seleccionar_partidos.py y cerrar_resultados.py necesitan cambios
minimos -- toda la traduccion del formato de ESPN vive aqui, en un solo
lugar, igual que este archivo ya hacia con ClubElo y football-data.co.uk.

SIN CAMBIOS (no dependen de API-Football ni de ESPN):
  - ClubElo (obtener_ranking_clubelo)
  - football-data.co.uk (obtener_resultados_liga*, calcular_goal_index)

CAMBIO DE PROVEEDOR (antes API-Football, ahora ESPN):
  - obtener_fixtures_por_fecha(): recorre LIGAS_ESPN (ver abajo). ESPN
    no tiene cupo diario documentado, asi que no aplica el mismo motivo
    de minimizar peticiones que con API-Football -- pero se mantiene la
    prudencia por el riesgo real de throttling NO documentado (ver
    MIGRACION_ESPN.md y cuota_espn.py).
  - obtener_resultado_fixture(): ahora necesita tambien el slug de liga
    (ESPN lo exige en la URL) -- ver el pequeno cambio en cerrar_resultados.py.
  - obtener_info_equipo(): ya no es la via principal de pais (ESPN no
    expone nacionalidad de club tan directo como API-Football) -- queda
    como ultimo recurso, best-effort, usando el pais del estadio.
  - Cuotas reales: NUEVO -- ESPN trae cuotas reales de DraftKings
    embebidas en el mismo scoreboard que ya se pide para fixtures, SIN
    peticion adicional. Ver extraer_favorito_odds_espn().

LO QUE SE PERDIO (documentado para que no se busque en vano):
  - Tiros dentro/fuera del area (insidebox/outsidebox) -- ESPN solo da
    tiros totales y tiros a puerta. Ver el ajuste en momentum.py.
  - xG -- no confirmado que ESPN lo exponga para futbol. El sistema ya
    caia al proxy por tiros cuando xg_disponible=False; sin cambios ahi.

ADVERTENCIA: los slugs de LIGAS_ESPN que NO estan marcados como
"confirmado" siguen el patron estandar de ESPN (pais.numero_division)
pero no se probaron uno por uno. Si alguno falla, el log lo dice
explicitamente (nunca falla en silencio) -- se corrige ese slug puntual
sin tocar el resto. Es facil agregar mas ligas: solo se necesita el
slug correcto en este diccionario.
"""

import os
import csv
import io
import difflib
import requests

TIMEOUT = 20

# =====================================================================
# The Odds API -- SIN CAMBIOS DE LOGICA, se mantiene como respaldo
# SECUNDARIO tras la migracion a ESPN (la fuente primaria de cuota real
# ahora es DraftKings via ESPN, ver extraer_favorito_odds_espn arriba).
# Se usa solo para llenar huecos en ligas que ESPN/DraftKings no cubren.
# =====================================================================

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
_BASE_ODDS_API = "https://api.the-odds-api.com/v4/sports"


def obtener_deportes_odds_api():
    """Listado de sport_keys activos ahora mismo -- gratis, no cuenta
    contra el cupo mensual. Se usa para validar un sport_key antes de
    gastar una peticion real en el."""
    if not ODDS_API_KEY:
        return set()
    try:
        r = requests.get(f"{_BASE_ODDS_API}/", params={"apiKey": ODDS_API_KEY}, timeout=TIMEOUT)
        r.raise_for_status()
        return {d["key"] for d in r.json() if d.get("active")}
    except Exception as e:
        print(f"[AVISO] No se pudo consultar deportes activos de The Odds API: {e}")
        return set()


def obtener_cuotas_liga(sport_key, region="us", mercado="h2h"):
    """Cuotas h2h reales para una liga (sport_key). Registra el uso
    real via los headers de la respuesta (cuota_odds_api.py los usa
    como fuente de verdad del cupo mensual restante)."""
    if not ODDS_API_KEY:
        return []
    url = f"{_BASE_ODDS_API}/{sport_key}/odds/"
    r = requests.get(url, params={"apiKey": ODDS_API_KEY, "regions": region, "markets": mercado}, timeout=TIMEOUT)
    r.raise_for_status()
    try:
        import cuota_odds_api
        cuota_odds_api.actualizar_desde_headers(r.headers)
    except Exception:
        pass
    return r.json()

# =====================================================================
# ESPN -- fixtures, resultados, boxscore en vivo, cuotas embebidas
# =====================================================================

BASE_ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# slug ESPN -> (pais como lo usa CODIGO_LIGA_A_PAIS / PAIS_A_CODIGO_CLUBELO, nombre liga)
# Confirmados EN VIVO durante esta migracion: eng.1, bra.1 (scoreboard
# real consultado), mex.1 y usa.1 (tabla oficial de Leagues Cup),
# concacaf.leagues.cup (partido real seguido en vivo, Queretaro-Seattle).
# El resto sigue el patron estandar pero no se probo uno por uno.
LIGAS_ESPN = {
    "eng.1": ("England", "Premier League"),
    "eng.2": ("England", "Championship"),
    "esp.1": ("Spain", "La Liga"),
    "esp.2": ("Spain", "La Liga 2"),
    "ita.1": ("Italy", "Serie A"),
    "ger.1": ("Germany", "Bundesliga"),
    "fra.1": ("France", "Ligue 1"),
    "ned.1": ("Netherlands", "Eredivisie"),
    "por.1": ("Portugal", "Primeira Liga"),
    "bel.1": ("Belgium", "Jupiler Pro League"),
    "tur.1": ("Turkey", "Super Lig"),
    "gre.1": ("Greece", "Super League 1"),
    "sco.1": ("Scotland", "Premiership"),
    "bra.1": ("Brazil", "Campeonato Brasileiro"),           # confirmado en vivo
    "arg.1": ("Argentina", "Liga Profesional Argentina"),
    "mex.1": ("Mexico", "Liga MX"),                            # confirmado
    "usa.1": ("USA", "Major League Soccer"),                   # confirmado
    "col.1": ("Colombia", "Primera A"),
    "chi.1": ("Chile", "Primera Division"),
    "uru.1": ("Uruguay", "Primera Division"),
    "ecu.1": ("Ecuador", "Liga Pro"),
    "concacaf.leagues.cup": ("World", "Leagues Cup"),          # confirmado en vivo
    "concacaf.champions": ("World", "Concacaf Champions Cup"),
    "conmebol.libertadores": ("World", "CONMEBOL Libertadores"),
    "conmebol.sudamericana": ("World", "CONMEBOL Sudamericana"),
    "uefa.champions": ("World", "UEFA Champions League"),
    "uefa.europa": ("World", "UEFA Europa League"),

    # AGREGADO agosto 2026, a pedido explicito -- confirmados con
    # evidencia real: (1) tabla de slugs verificados de ESPN
    # (github.com/pseudo-r/Public-ESPN-API) y (2) el log real de una
    # corrida donde NO dieron error 400 (ver conversacion del
    # 22-ago-2026). Los que SI daban 400 (Armenia, Azerbaiyan,
    # Bulgaria, Georgia, Hungria, Islandia, Lituania, Montenegro,
    # Serbia, Ucrania, Faroe, Kuwait, Letonia, Polonia, Portugal 2,
    # Grecia 2, Belgica 2) se sacaron -- ESPN simplemente no tiene esas
    # ligas en este endpoint, no es un problema de nombre de slug.
    "fin.1": ("Finland", "Veikkausliiga"),
    "mlt.1": ("Malta", "Premier League"),
    "rou.1": ("Romania", "Liga I"),
    "isr.1": ("Israel", "Ligat Ha'Al"),
    "rus.1": ("Russia", "Premier League"),
    "ksa.1": ("Saudi Arabia", "Pro League"),
    "tur.2": ("Turkey", "1. Lig"),
    "bra.2": ("Brazil", "Serie B"),
    "chi.2": ("Chile", "Primera B"),
    "par.1": ("Paraguay", "Primera Division"),
    "per.1": ("Peru", "Liga 1"),
}


def _fecha_espn(fecha_iso):
    return fecha_iso.replace("-", "")


def obtener_fixtures_por_fecha(fecha_iso):
    """
    Devuelve los fixtures de 'fecha_iso' (YYYY-MM-DD) desde ESPN, en la
    MISMA forma que antes devolvia API-Football (fixture.id,
    teams.home/away.id/name, league.country/name) para que
    seleccionar_partidos.py no cambie su logica de lectura.

    Ademas guarda internamente el bloque de cuotas de DraftKings (si
    vino) y el slug de liga -- ver extraer_favorito_odds_espn() y los
    campos "_liga_slug" / "_odds_raw" de cada fixture.

    CAMBIO (agosto 2026, a pedido explicito): antes LIGAS_ESPN solo se
    consultaba como respaldo SI la peticion global (".../all/scoreboard")
    fallaba por completo. Se confirmo con evidencia real (log de una
    corrida: "ESPN global: 39 fixtures encontrados" en un dia con 84
    favoritos en la hoja) que el endpoint global de ESPN NO cubre la
    mayoria de ligas menores, aunque la peticion en si tenga exito --
    no es un problema de nombres/alias, el partido simplemente no
    estaba en la respuesta. Por eso ahora LIGAS_ESPN se consulta
    SIEMPRE ademas del global, fusionando por fixture_id (sin duplicar)
    en vez de ser un respaldo de todo o nada.
    """
    fixtures_por_id = {}

    url_global = f"{BASE_ESPN_SITE}/all/scoreboard?dates={_fecha_espn(fecha_iso)}"
    try:
        r = requests.get(url_global, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        try:
            from cuota_espn import registrar_uso
            registrar_uso()
        except Exception:
            pass

        for evento in data.get("events", []):
            try:
                comp = evento["competitions"][0]
                home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
                away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            except (KeyError, IndexError, StopIteration):
                continue
            liga = evento.get("league", {})
            fixtures_por_id[evento["id"]] = {
                "fixture": {"id": evento["id"], "date": evento["date"]},
                "teams": {
                    "home": {"id": home["team"]["id"], "name": home["team"]["displayName"]},
                    "away": {"id": away["team"]["id"], "name": away["team"]["displayName"]},
                },
                "league": {"country": liga.get("country", ""), "name": liga.get("name", "")},
                "_liga_slug": "all",
                "_odds_raw": comp.get("odds"),
            }
        print(f"ESPN global: {len(fixtures_por_id)} fixtures encontrados.")
    except Exception as e:
        print(f"[AVISO] No se pudo consultar el marcador global de ESPN: {e}. Se sigue solo con la lista curada.")

    # SIEMPRE se complementa con LIGAS_ESPN -- el global no cubre todo
    # (ver docstring), aunque la peticion global haya tenido exito.
    nuevos_del_respaldo = 0
    for slug, (pais, nombre_liga) in LIGAS_ESPN.items():
        url = f"{BASE_ESPN_SITE}/{slug}/scoreboard?dates={_fecha_espn(fecha_iso)}"
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[AVISO] No se pudo consultar ESPN para la liga {slug}: {e}")
            continue

        try:
            from cuota_espn import registrar_uso
            registrar_uso()
        except Exception:
            pass

        for evento in data.get("events", []):
            if evento["id"] in fixtures_por_id:
                continue  # ya vino del global, no se duplica
            try:
                comp = evento["competitions"][0]
                home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
                away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            except (KeyError, IndexError, StopIteration):
                continue

            fixtures_por_id[evento["id"]] = {
                "fixture": {"id": evento["id"], "date": evento["date"]},
                "teams": {
                    "home": {"id": home["team"]["id"], "name": home["team"]["displayName"]},
                    "away": {"id": away["team"]["id"], "name": away["team"]["displayName"]},
                },
                "league": {"country": pais, "name": nombre_liga},
                "_liga_slug": slug,
                "_odds_raw": comp.get("odds"),
            }
            nuevos_del_respaldo += 1

    print(f"ESPN (lista curada, {len(LIGAS_ESPN)} liga(s) consultada(s)): "
          f"{nuevos_del_respaldo} fixture(s) adicional(es) que el global no traia.")
    return list(fixtures_por_id.values())


def extraer_favorito_odds_espn(fixture):
    """
    A partir de '_odds_raw' (guardado por obtener_fixtures_por_fecha),
    calcula la probabilidad implicita de AMBOS lados usando la cuota
    REAL de DraftKings que ESPN ya trae embebida en el scoreboard --
    SIN gastar ninguna peticion adicional.

    AMPLIADO (a pedido explicito): antes solo devolvia el lado favorito.
    Ahora devuelve cuota_local/cuota_visitante y probabilidad_local/
    probabilidad_visitante de los DOS lados, para que resumen.py pueda
    mostrar la comparacion completa, no solo la del favorito.

    Devuelve un dict o None si el partido no trae mercado de moneyline
    (comun en ligas chicas -- no es un error, DraftKings simplemente no
    cotiza esa liga).
    """
    odds_list = fixture.get("_odds_raw")
    if not odds_list:
        return None
    bloque = odds_list[0] if isinstance(odds_list, list) else odds_list
    if not bloque or "moneyline" not in bloque:
        return None

    ml = bloque["moneyline"]
    try:
        odds_home = ml["home"]["close"]["odds"]
        odds_away = ml["away"]["close"]["odds"]
    except (KeyError, TypeError):
        return None

    def _implicita(odds_americana):
        odds_americana = float(odds_americana)
        if odds_americana < 0:
            return -odds_americana / (-odds_americana + 100)
        return 100 / (odds_americana + 100)

    p_home = _implicita(odds_home)
    p_away = _implicita(odds_away)
    p_draw = 0.0
    if "draw" in ml:
        try:
            p_draw = _implicita(ml["draw"]["close"]["odds"])
        except (KeyError, TypeError):
            pass

    total = p_home + p_away + p_draw
    if total <= 0:
        return None
    p_home_norm = p_home / total
    p_away_norm = p_away / total

    lado_favorito = "local" if p_home_norm >= p_away_norm else "visitante"
    prob_favorito = p_home_norm if lado_favorito == "local" else p_away_norm

    return {
        "lado_favorito": lado_favorito,
        "probabilidad_favorito": round(prob_favorito, 4),
        "probabilidad_local": round(p_home_norm, 4),
        "probabilidad_visitante": round(p_away_norm, 4),
        "cuota_local": round(1 / p_home_norm, 2) if p_home_norm > 0 else None,
        "cuota_visitante": round(1 / p_away_norm, 2) if p_away_norm > 0 else None,
        "casa_apuestas": "DraftKings (via ESPN)",
    }


def obtener_boxscore_en_vivo(liga_slug, fixture_id):
    """
    Estadisticas EN VIVO de un partido: marcador, minuto, estado, y la
    lista de estadisticas por equipo (tiros, tiros a puerta, corners,
    faltas, offside, posesion, tarjetas, penales, tiros bloqueados).

    Formato CONFIRMADO en vivo el 09-ago-2026 (Queretaro @ Seattle
    Sounders, Leagues Cup, minuto 77'): 'statistics' es una LISTA de
    {"name","displayValue","label"} por equipo, NO un diccionario plano
    como daba API-Football -- por eso momentum.py usa el adaptador
    _stat() que lee de esta forma.

    Nunca lanza excepcion hacia arriba -- devuelve None si algo falla,
    y quien llama cae al ultimo snapshot bueno (igual que ya se hacia
    con ClubElo).
    """
    url = f"{BASE_ESPN_SITE}/{liga_slug}/summary?event={fixture_id}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        try:
            from cuota_espn import registrar_uso
            registrar_uso()
        except Exception:
            pass
    except Exception as e:
        print(f"[AVISO] No se pudo consultar el boxscore de {fixture_id} ({liga_slug}): {e}")
        return None

    header = data.get("header", {})
    competiciones = header.get("competitions", [{}])
    status = competiciones[0].get("status", {}) if competiciones else {}

    boxscore = data.get("boxscore", {})
    equipos_stats = {}
    for equipo in boxscore.get("teams", []):
        home_away = equipo.get("homeAway")
        stats_lista = equipo.get("statistics", [])
        equipos_stats[home_away] = {s["name"]: s.get("displayValue") for s in stats_lista}

    goles_home = goles_away = None
    for comp in competiciones:
        for competitor in comp.get("competitors", []):
            try:
                if competitor.get("homeAway") == "home":
                    goles_home = int(competitor.get("score", 0))
                elif competitor.get("homeAway") == "away":
                    goles_away = int(competitor.get("score", 0))
            except (TypeError, ValueError):
                pass

    return {
        "minuto": status.get("displayClock"),
        "periodo": status.get("period"),
        "estado": status.get("type", {}).get("state"),  # "pre" | "in" | "post"
        "estado_detalle": status.get("type", {}).get("description"),
        "goles_local": goles_home,
        "goles_visitante": goles_away,
        "stats_local": equipos_stats.get("home", {}),
        "stats_visitante": equipos_stats.get("away", {}),
    }


def obtener_resultado_fixture(fixture_id, liga_slug):
    """
    Resultado final, en la MISMA forma que antes daba API-Football
    (info["fixture"]["status"]["short"], info["goals"]["home"/"away"])
    para que cerrar_resultados.py no cambie su logica de lectura -- solo
    la firma gana el parametro liga_slug (ESPN lo exige en la URL,
    API-Football no lo necesitaba).
    """
    box = obtener_boxscore_en_vivo(liga_slug, fixture_id)
    if box is None:
        return None

    mapa_estado = {"post": "FT", "in": "LIVE", "pre": "NS"}
    short = mapa_estado.get(box["estado"], box["estado_detalle"] or "?")

    return {
        "fixture": {"status": {"short": short}},
        "goals": {"home": box["goles_local"], "away": box["goles_visitante"]},
    }


def obtener_info_equipo(team_id):
    """
    Ultimo recurso para inferir pais de un equipo (tier 3 en
    team_resolver._resolver_pais, DESPUES de liga domestica y Goal
    Index). ESPN no expone nacionalidad de club tan directo como
    API-Football -- se usa el pais del estadio como aproximacion
    (funciona bien para equipos domesticos; puede fallar en sedes
    neutrales de torneos internacionales, que es justo el caso menos
    comun y menos critico).

    Best-effort: si falla, devuelve None y el llamador cae al
    comportamiento de "sin verificar" que ya existia.
    """
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/teams/{team_id}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        venue = data.get("team", {}).get("venue", {})
        pais = venue.get("address", {}).get("country")
        return {"country": pais}
    except Exception:
        return None


def buscar_equipo_similar(nombre, candidatos, n=1, corte=0.6):
    """Utilidad de emparejamiento difuso por nombre -- sin cambios, no
    depende de ningun proveedor de datos."""
    if not nombre or not candidatos:
        return []
    return difflib.get_close_matches(nombre, candidatos, n=n, cutoff=corte)


# =====================================================================
# ClubElo -- SIN CAMBIOS, no tiene nada que ver con API-Football/ESPN
# =====================================================================

def obtener_ranking_clubelo(fecha_iso, intentos=3):
    """CSV publico de ClubElo para una fecha dada. Reintentos con
    espera creciente si el sitio (pequeno, gratuito) se sobrecarga."""
    import time
    url = f"http://api.clubelo.com/{fecha_iso}"
    for intento in range(1, intentos + 1):
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            lector = csv.DictReader(io.StringIO(r.text))
            return list(lector)
        except Exception as e:
            print(f"[AVISO] ClubElo intento {intento}/{intentos} fallo: {e}")
            if intento < intentos:
                time.sleep(2 * intento)
    return []


# =====================================================================
# football-data.co.uk -- SIN CAMBIOS, tampoco depende de API-Football
# =====================================================================

LIGAS_FOOTBALL_DATA = {
    "E0": "Premier League", "E1": "Championship",
    "SP1": "La Liga", "SP2": "La Liga 2",
    "I1": "Serie A", "I2": "Serie B",
    "D1": "Bundesliga", "D2": "2. Bundesliga",
    "F1": "Ligue 1", "F2": "Ligue 2",
    "N1": "Eredivisie", "P1": "Primeira Liga",
    "B1": "Jupiler Pro League", "T1": "Super Lig",
    "G1": "Super League 1", "SC0": "Premiership",
}

LIGAS_FOOTBALL_DATA_EXTRA = {
    "ARG": "Liga Profesional Argentina", "BRA": "Campeonato Brasileiro",
    "MEX": "Liga MX", "USA": "Major League Soccer",
}

CODIGO_LIGA_A_PAIS = {
    "E0": "England", "E1": "England", "SP1": "Spain", "SP2": "Spain",
    "I1": "Italy", "I2": "Italy", "D1": "Germany", "D2": "Germany",
    "F1": "France", "F2": "France", "N1": "Netherlands", "P1": "Portugal",
    "B1": "Belgium", "T1": "Turkey", "G1": "Greece", "SC0": "Scotland",
    "ARG": "Argentina", "BRA": "Brazil", "MEX": "Mexico", "USA": "USA",
}

_TEMPORADA_ACTUAL = "2526"


def obtener_resultados_liga(codigo, temporada=_TEMPORADA_ACTUAL):
    url = f"https://www.football-data.co.uk/mmz4281/{temporada}/{codigo}.csv"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:
        print(f"[AVISO] No se pudo descargar {codigo} ({temporada}): {e}")
        return []


def obtener_resultados_liga_multi_temporada(codigo, temporadas):
    resultados = []
    for temporada in temporadas:
        resultados.extend(obtener_resultados_liga(codigo, temporada))
    return resultados


def obtener_resultados_liga_extra(codigo):
    url = f"https://www.football-data.co.uk/new/{codigo}.csv"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:
        print(f"[AVISO] No se pudo descargar liga extra {codigo}: {e}")
        return []


def calcular_goal_index(resultados, ultimos_n=None):
    """
    Goal Index simple por equipo: diferencia entre su promedio de goles
    a favor y en contra (ultimos N partidos si se pasa ultimos_n, si no
    toda la temporada). elo_desde_goal_index.py calibra este valor
    contra Elo real via regresion lineal, asi que cualquier escala
    consistente sirve -- misma formula de siempre, sin cambios.
    """
    partidos_por_equipo = {}
    for fila in resultados:
        home, away = fila.get("HomeTeam"), fila.get("AwayTeam")
        try:
            gh, ga = int(fila["FTHG"]), int(fila["FTAG"])
        except (KeyError, ValueError, TypeError):
            continue
        if home:
            partidos_por_equipo.setdefault(home, []).append((gh, ga))
        if away:
            partidos_por_equipo.setdefault(away, []).append((ga, gh))

    resultado = {}
    for equipo, partidos in partidos_por_equipo.items():
        if ultimos_n:
            partidos = partidos[-ultimos_n:]
        n = len(partidos)
        if n == 0:
            continue
        goles_favor = sum(p[0] for p in partidos) / n
        goles_contra = sum(p[1] for p in partidos) / n
        resultado[equipo] = {
            "goal_index": round(goles_favor - goles_contra, 3),
            "goles_favor_prom": round(goles_favor, 3),
            "goles_contra_prom": round(goles_contra, 3),
            "partidos_jugados": n,
        }
    return resultado
