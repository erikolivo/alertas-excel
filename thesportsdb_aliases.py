"""Nombres alternativos de clubes mediante la API gratuita TheSportsDB.

Se consulta únicamente como respaldo de emparejamiento; ESPN continúa siendo
la fuente de fixtures y de datos en vivo. La clave pública 123 está publicada
por TheSportsDB para su API v1 y puede sustituirse por THESPORTSDB_API_KEY.
"""

import os
from functools import lru_cache

import requests


API_KEY = os.environ.get("THESPORTSDB_API_KEY", "123")
URL_BUSQUEDA = f"https://www.thesportsdb.com/api/v1/json/{API_KEY}/searchteams.php"
LIMITE_CONSULTAS_POR_CORRIDA = 25
TIMEOUT = 12
_consultas = 0


@lru_cache(maxsize=256)
def nombres_alternativos(nombre):
    """Devuelve nombres oficiales, cortos y alternativos de un club.

    Si la API no conoce el equipo o alcanza su cuota, devuelve un conjunto
    vacío para que la selección continúe normalmente.
    """
    global _consultas
    if _consultas >= LIMITE_CONSULTAS_POR_CORRIDA or not nombre:
        return set()
    _consultas += 1
    try:
        respuesta = requests.get(URL_BUSQUEDA, params={"t": nombre}, timeout=TIMEOUT)
        respuesta.raise_for_status()
        equipos = respuesta.json().get("teams") or []
    except Exception:
        return set()

    alternativas = set()
    for equipo in equipos:
        for campo in ("strTeam", "strTeamShort", "strTeamAlternate"):
            valor = equipo.get(campo)
            if valor:
                alternativas.update(parte.strip() for parte in str(valor).replace(";", ",").split(",") if parte.strip())
    return alternativas
