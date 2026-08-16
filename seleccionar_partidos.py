"""Fase 1: prepara para vigilancia los favoritos diarios de Google Sheets."""

import datetime
import json
from difflib import SequenceMatcher
from pathlib import Path

from fetch_data import obtener_fixtures_por_fecha
from google_favoritos import normalizar, obtener_favoritos_google
from thesportsdb_aliases import nombres_alternativos

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ARCHIVO_SALIDA = DATA_DIR / "partidos_hoy.json"
ARCHIVO_CACHE_ALIAS = DATA_DIR / "alias_equipos_cache.json"
ARCHIVO_PENDIENTES = DATA_DIR / "pendientes_revision.json"
ZONA_HORARIA_LOCAL = datetime.timezone(datetime.timedelta(hours=-5))
VERSION_SELECCION = 5

# Alias FIJADOS A MANO -- para casos que se quieren garantizar sin
# depender de que el fuzzy match o TheSportsDB los resuelvan. La
# mayoria de los casos nuevos ya NO necesitan pasar por aqui: se
# resuelven y se recuerdan solos en ARCHIVO_CACHE_ALIAS (ver
# _registrar_alias_aprendido mas abajo).
ALIAS_EQUIPOS = {
    "wolves": "wolverhampton wanderers",
    "aarhus": "agf",
}
SUFIJOS_EQUIPO = {"fc", "cf", "fk", "ff", "sc", "afc", "ac"}

_cache_alias_memoria = None


def _cargar_cache_alias():
    global _cache_alias_memoria
    if _cache_alias_memoria is None:
        if ARCHIVO_CACHE_ALIAS.exists():
            try:
                _cache_alias_memoria = json.loads(ARCHIVO_CACHE_ALIAS.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _cache_alias_memoria = {}
        else:
            _cache_alias_memoria = {}
    return _cache_alias_memoria


def _registrar_alias_aprendido(nombre_hoja, nombre_espn):
    """Guarda nombre_de_la_hoja -> nombre_oficial_ESPN la primera vez
    que se resuelve por un camino no trivial (fuzzy o TheSportsDB), asi
    el proximo dia que aparezca ese mismo nombre en la hoja el match es
    directo -- sin gastar peticion a TheSportsDB ni depender de que el
    ratio de similitud vuelva a alcanzar el corte."""
    clave = normalizar(nombre_hoja)
    if not clave or clave == normalizar(nombre_espn):
        return  # ya coincide directo, no hace falta aprender nada
    cache = _cargar_cache_alias()
    if cache.get(clave) == nombre_espn:
        return
    cache[clave] = nombre_espn
    ARCHIVO_CACHE_ALIAS.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _registrar_pendiente(entrada, motivo, fecha):
    """Log acotado de partidos que no se lograron ubicar en ESPN, para
    revisar de vez en cuando (probablemente ligas que ESPN no cubre, o
    un alias nuevo que conviene fijar a mano en ALIAS_EQUIPOS)."""
    pendientes = []
    if ARCHIVO_PENDIENTES.exists():
        try:
            pendientes = json.loads(ARCHIVO_PENDIENTES.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pendientes = []
    pendientes.append({
        "fecha": fecha, "local": entrada["local"], "visitante": entrada["visitante"],
        "favorito": entrada.get("favorito"), "motivo": motivo,
    })
    pendientes = pendientes[-200:]
    ARCHIVO_PENDIENTES.write_text(json.dumps(pendientes, ensure_ascii=False, indent=2), encoding="utf-8")


def fecha_local_hoy():
    return datetime.datetime.now(ZONA_HORARIA_LOCAL).date().isoformat()


def ya_se_completo_hoy():
    if not ARCHIVO_SALIDA.exists():
        return False
    try:
        datos = json.loads(ARCHIVO_SALIDA.read_text(encoding="utf-8"))
        return (
            datos.get("fecha") == fecha_local_hoy()
            and datos.get("seleccion_version") == VERSION_SELECCION
            and all(p.get("fuente_favorito") == "Google Sheets" for p in datos.get("partidos", []))
        )
    except (json.JSONDecodeError, OSError):
        return False


def _normalizar_equipo(nombre):
    nombre_norm = normalizar(nombre)
    equivalente_aprendido = _cargar_cache_alias().get(nombre_norm)
    if equivalente_aprendido:
        nombre_norm = normalizar(equivalente_aprendido)
    nombre_norm = " ".join(palabra for palabra in nombre_norm.split() if palabra not in SUFIJOS_EQUIPO)
    return ALIAS_EQUIPOS.get(nombre_norm, nombre_norm)


def _coincide(nombre_hoja, nombre_espn):
    a, b = _normalizar_equipo(nombre_hoja), _normalizar_equipo(nombre_espn)
    return bool(a and b and (a == b or a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.82))


def _tipo_pronostico(favorito_hoja):
    """La hoja marca la doble oportunidad con el prefijo 'DC' (ej. 'DC
    LOCAL', 'DC VISITANTE'). El lado (local/visitante) que va dentro de
    ese texto ya viene decidido por quien llena la hoja -- es el lado
    de la doble oportunidad que consideraron mas probable (ej. 'DC
    LOCAL' = local o empate). _lado_favorito() ya lo detecta bien
    porque compara por palabra, no por texto exacto -- aqui solo se
    distingue si es 'directo' o 'doble_oportunidad' para poder avisarlo
    al inicio de cada mensaje."""
    palabras = set(normalizar(favorito_hoja).split())
    return "doble_oportunidad" if "dc" in palabras else "favorito_directo"


def _lado_favorito(favorito_hoja, local, visitante):
    favorito = normalizar(favorito_hoja)
    palabras = set(favorito.split())
    if favorito in {"local", "home", "1"} or "local" in palabras or "home" in palabras or _coincide(favorito_hoja, local):
        return "local"
    if favorito in {"visitante", "away", "2"} or "visitante" in palabras or "away" in palabras or _coincide(favorito_hoja, visitante):
        return "visitante"
    return None


def _coincide_con_alternativas(nombre_hoja, nombre_espn):
    """Coincidencia directa (normalizacion + fuzzy) y, si esa falla,
    contra los nombres alternativos que reporte TheSportsDB para el
    nombre de la hoja (ej. 'Viborg' -> 'Viborg FF', 'Wolves' ->
    'Wolverhampton Wanderers')."""
    if _coincide(nombre_hoja, nombre_espn):
        return True
    return any(_coincide(alternativo, nombre_espn) for alternativo in nombres_alternativos(nombre_hoja))


def _buscar_fixture(entrada, fixtures):
    candidatos = [f for f in fixtures if _coincide(entrada["local"], f["teams"]["home"]["name"]) and _coincide(entrada["visitante"], f["teams"]["away"]["name"])]
    if len(candidatos) == 1:
        return candidatos[0]

    # Segundo intento: se prueban los nombres alternativos de
    # TheSportsDB en AMBOS lados de forma independiente (antes solo se
    # probaba en el lado que no habia coincidido directo, asumiendo que
    # el otro si coincidia -- eso dejaba sin cubrir el caso de que los
    # DOS nombres de la hoja difirieran del oficial de ESPN).
    candidatos = [
        f for f in fixtures
        if _coincide_con_alternativas(entrada["local"], f["teams"]["home"]["name"])
        and _coincide_con_alternativas(entrada["visitante"], f["teams"]["away"]["name"])
    ]
    if len(candidatos) != 1:
        return None

    fixture = candidatos[0]
    # Se encontro por un camino no trivial -- se aprende el alias para
    # que el proximo dia sea un match directo sin volver a depender de
    # TheSportsDB ni del fuzzy match.
    _registrar_alias_aprendido(entrada["local"], fixture["teams"]["home"]["name"])
    _registrar_alias_aprendido(entrada["visitante"], fixture["teams"]["away"]["name"])
    return fixture


def _partido_para_vigilar(fixture, favorito_hoja, fila_hoja, confianza_estrellas=0):
    local, visitante = fixture["teams"]["home"], fixture["teams"]["away"]
    lado = _lado_favorito(favorito_hoja, local["name"], visitante["name"])
    if lado is None:
        return None
    favorito = local["name"] if lado == "local" else visitante["name"]
    no_favorito = visitante["name"] if lado == "local" else local["name"]
    return {
        "partido": f"{local['name']} vs {visitante['name']}", "local": local["name"], "visitante": visitante["name"],
        "favorito": favorito, "no_favorito": no_favorito, "favorito_es_local": lado == "local",
        "tipo_pronostico": _tipo_pronostico(favorito_hoja), "confianza_estrellas": confianza_estrellas,
        "fuente_favorito": "Google Sheets", "fila_fuente": fila_hoja,
        "hora_inicio": fixture["fixture"]["date"], "fixture_id": fixture["fixture"]["id"], "liga_slug": fixture.get("_liga_slug"),
        "home_id": local["id"], "away_id": visitante["id"], "kickoff_utc": fixture["fixture"]["date"],
        "resultado_final": None, "acierto": None, "historial_snapshots": [], "alertas_enviadas": [], "diferencia_maxima_alcanzada": 0,
    }


def seleccionar():
    if ya_se_completo_hoy():
        print("La selección de hoy ya se generó antes. Nada que hacer.")
        return
    hoy = fecha_local_hoy()
    try:
        favoritos = obtener_favoritos_google(hoy=datetime.date.fromisoformat(hoy))
    except Exception as error:
        print(f"[ERROR] No se pudieron leer los favoritos de Google Sheets: {error}")
        return
    print(f"Google Sheets: {len(favoritos)} favorito(s) válido(s) para procesar.")
    fixtures = obtener_fixtures_por_fecha(hoy)
    seleccionados, sin_fixture, favorito_invalido, vistos = [], 0, 0, set()
    for entrada in favoritos:
        fixture = _buscar_fixture(entrada, fixtures)
        if fixture is None:
            sin_fixture += 1
            print(f"[AVISO] Fila {entrada['fila_hoja']}: no se encontró en ESPN: {entrada['local']} vs {entrada['visitante']}")
            _registrar_pendiente(entrada, "no_encontrado_en_espn", hoy)
            continue
        partido = _partido_para_vigilar(fixture, entrada["favorito"], entrada["fila_hoja"], entrada.get("confianza_estrellas", 0))
        if partido is None:
            favorito_invalido += 1
            print(f"[AVISO] Fila {entrada['fila_hoja']}: favorito inválido: {entrada['favorito']}")
            continue
        if partido["fixture_id"] not in vistos:
            seleccionados.append(partido)
            vistos.add(partido["fixture_id"])
    seleccionados.sort(key=lambda partido: partido["hora_inicio"])
    ARCHIVO_SALIDA.write_text(json.dumps({"fecha": hoy, "seleccion_version": VERSION_SELECCION, "partidos": seleccionados}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Guardado en {ARCHIVO_SALIDA}: {len(seleccionados)} partido(s) de Google Sheets. Sin fixture: {sin_fixture}; favorito inválido: {favorito_invalido}.")


if __name__ == "__main__":
    seleccionar()
