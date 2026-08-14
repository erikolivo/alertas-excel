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
ZONA_HORARIA_LOCAL = datetime.timezone(datetime.timedelta(hours=-5))
VERSION_SELECCION = 4

# Variantes frecuentes entre la hoja y ESPN. Se usan solo para localizar
# el fixture: el mensaje final siempre conserva el nombre oficial de ESPN.
ALIAS_EQUIPOS = {
    "wolves": "wolverhampton wanderers",
    "aarhus": "agf",
}
SUFIJOS_EQUIPO = {"fc", "cf", "fk", "ff", "sc", "afc", "ac"}


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
    nombre = normalizar(nombre)
    nombre = " ".join(palabra for palabra in nombre.split() if palabra not in SUFIJOS_EQUIPO)
    return ALIAS_EQUIPOS.get(nombre, nombre)


def _coincide(nombre_hoja, nombre_espn):
    a, b = _normalizar_equipo(nombre_hoja), _normalizar_equipo(nombre_espn)
    return bool(a and b and (a == b or a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.82))


def _lado_favorito(favorito_hoja, local, visitante):
    favorito = normalizar(favorito_hoja)
    palabras = set(favorito.split())
    if favorito in {"local", "home", "1"} or "local" in palabras or "home" in palabras or _coincide(favorito_hoja, local):
        return "local"
    if favorito in {"visitante", "away", "2"} or "visitante" in palabras or "away" in palabras or _coincide(favorito_hoja, visitante):
        return "visitante"
    return None


def _buscar_fixture(entrada, fixtures):
    candidatos = [f for f in fixtures if _coincide(entrada["local"], f["teams"]["home"]["name"]) and _coincide(entrada["visitante"], f["teams"]["away"]["name"])]
    if len(candidatos) == 1:
        return candidatos[0]

    # Si uno de los dos equipos ya coincide, TheSportsDB puede traducir la
    # abreviatura del otro (ej. Wolves -> Wolverhampton Wanderers). Nunca se
    # acepta una coincidencia si no quedan validados ambos equipos.
    candidatos = []
    for fixture in fixtures:
        local_espn = fixture["teams"]["home"]["name"]
        visitante_espn = fixture["teams"]["away"]["name"]
        local_ok = _coincide(entrada["local"], local_espn)
        visitante_ok = _coincide(entrada["visitante"], visitante_espn)
        if local_ok and not visitante_ok:
            alternativos = nombres_alternativos(entrada["visitante"])
            if any(_coincide(alternativo, visitante_espn) for alternativo in alternativos):
                candidatos.append(fixture)
        elif visitante_ok and not local_ok:
            alternativos = nombres_alternativos(entrada["local"])
            if any(_coincide(alternativo, local_espn) for alternativo in alternativos):
                candidatos.append(fixture)
    return candidatos[0] if len(candidatos) == 1 else None


def _partido_para_vigilar(fixture, favorito_hoja, fila_hoja):
    local, visitante = fixture["teams"]["home"], fixture["teams"]["away"]
    lado = _lado_favorito(favorito_hoja, local["name"], visitante["name"])
    if lado is None:
        return None
    favorito = local["name"] if lado == "local" else visitante["name"]
    no_favorito = visitante["name"] if lado == "local" else local["name"]
    return {
        "partido": f"{local['name']} vs {visitante['name']}", "local": local["name"], "visitante": visitante["name"],
        "favorito": favorito, "no_favorito": no_favorito, "favorito_es_local": lado == "local",
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
            continue
        partido = _partido_para_vigilar(fixture, entrada["favorito"], entrada["fila_hoja"])
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
