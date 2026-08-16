"""Lectura de los favoritos publicados diariamente en Google Sheets."""

import csv
import io
import re
import unicodedata
from datetime import date, datetime


SHEET_ID = "1KnaTUoCLHhgGgmdpBUo_vpbAhQawjSh7LBFpZcHe1cs"
GID = "0"
URL_EXPORTACION = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
TIMEOUT = 30

_ENCABEZADOS = {
    "partido": {"partido", "match", "encuentro", "fixture", "juego"},
    "local": {"local", "home", "equipo local", "casa"},
    "visitante": {"visitante", "away", "equipo visitante", "fuera"},
    "favorito": {"favorito", "favourite", "favorite", "pick", "pronostico", "seleccion", "pronostico pick recomendado"},
    "fecha": {"fecha", "date", "dia"},
    "confianza": {"confianza", "confidence", "certeza"},
}

# Columnas para las que NUNCA se debe aceptar un encabezado con "%" --
# ej. "% Local" / "% Visitante" se normalizan a exactamente "local" /
# "visitante" (el simbolo % se elimina), quedando identicas al
# encabezado real del equipo ("Local" / "Visitante"). Sin este filtro,
# _columna() podia devolver la columna de PORCENTAJE en vez de la del
# NOMBRE del equipo si el porcentaje aparece antes en la hoja.
_TIPOS_QUE_EXCLUYEN_PORCENTAJE = {"local", "visitante", "partido", "favorito"}


def normalizar(texto):
    """Normaliza nombres para compararlos sin perder el original."""
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def _columna(encabezados, tipo):
    candidatos = _ENCABEZADOS[tipo]
    elegibles = [h for h in encabezados if "%" not in h] if tipo in _TIPOS_QUE_EXCLUYEN_PORCENTAJE else encabezados
    return next((h for h in elegibles if normalizar(h) in candidatos or any(c in normalizar(h) for c in candidatos)), None)


def _separar_partido(valor):
    partes = re.split(r"\s+(?:vs\.?|v\.?|versus|[-–—])\s+", str(valor or ""), flags=re.I)
    return (partes[0].strip(), partes[1].strip()) if len(partes) == 2 and all(p.strip() for p in partes) else (None, None)


def _es_fecha_de_hoy(valor, hoy):
    if not str(valor or "").strip():
        return None
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(texto, formato).date() == hoy
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).date() == hoy
    except ValueError:
        return None


def interpretar_csv(contenido, hoy=None):
    """Convierte el CSV en favoritos independientes de ESPN.

    Admite una columna Partido ("Local vs Visitante") o columnas Local y
    Visitante. Falla explícitamente si cambian los encabezados esenciales.
    """
    hoy = hoy or date.today()
    try:
        dialecto = csv.Sniffer().sniff(contenido[:4096], delimiters=",;\t")
    except csv.Error:
        dialecto = csv.excel
    filas = list(csv.DictReader(io.StringIO(contenido), dialect=dialecto))
    if not filas or not filas[0]:
        return []
    encabezados = list(filas[0].keys())
    col_partido, col_local, col_visitante = (_columna(encabezados, tipo) for tipo in ("partido", "local", "visitante"))
    col_favorito, col_fecha = _columna(encabezados, "favorito"), _columna(encabezados, "fecha")
    col_confianza = _columna(encabezados, "confianza")
    if not col_favorito or not (col_partido or (col_local and col_visitante)):
        raise ValueError("La hoja debe tener Favorito (o Pick) y Partido, o las columnas Local y Visitante. Encabezados: " + ", ".join(encabezados))

    favoritos = []
    for numero, fila in enumerate(filas, start=2):
        if col_fecha and _es_fecha_de_hoy(fila.get(col_fecha), hoy) is False:
            continue
        favorito = str(fila.get(col_favorito, "")).strip()
        if favorito.lower() in {"", "-", "n/a", "na", "none"}:
            continue
        local = str(fila.get(col_local, "")).strip() if col_local else ""
        visitante = str(fila.get(col_visitante, "")).strip() if col_visitante else ""
        if not local or not visitante:
            local, visitante = _separar_partido(fila.get(col_partido))
        if local and visitante:
            confianza_texto = str(fila.get(col_confianza, "")).strip() if col_confianza else ""
            favoritos.append({
                "local": local, "visitante": visitante, "favorito": favorito, "fila_hoja": numero,
                "confianza_texto": confianza_texto, "confianza_estrellas": confianza_texto.count("★"),
            })
    return favoritos


def obtener_favoritos_google(hoy=None):
    import requests

    respuesta = requests.get(URL_EXPORTACION, timeout=TIMEOUT, headers={"User-Agent": "alertas-favoritos/1.0"})
    respuesta.raise_for_status()
    return interpretar_csv(respuesta.content.decode("utf-8-sig"), hoy=hoy)
