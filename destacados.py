"""
destacados.py
--------------
NUEVO (agosto 2026, a pedido explicito) -- Fase 2.5: a las 7:30am, de
los partidos del dia que SI tienen seguimiento en vivo disponible
(fixture_id ya localizado en ESPN), elige 6 al azar SIN repetirse,
divididos en 2 grupos de 3.

Cada partido elegido queda marcado en partidos_hoy.json con
"es_destacado": True y "grupo_destacado": 1 o 2 -- reporte_diario.py
usa esa marca para mostrar el % de acierto de este grupo por separado
del total general (a pedido explicito, para poder comparar con el
tiempo si la seleccion al azar rinde distinto al conjunto completo).

Reintenta cada 5 min dentro de una ventana corta (igual filosofia que
Fase 2), y se autoprotege con estado_diario ("destacados") para no
repetir el sorteo si ya se hizo hoy -- salvo que se llame con
--forzar (util para pruebas manuales desde Actions).
"""

import json
import random
import sys
import datetime
from pathlib import Path

from telegram_utils import enviar_mensaje_telegram, escapar_html
from estado_diario import ya_se_hizo, marcar_hecho

ARCHIVO = Path(__file__).parent / "data" / "partidos_hoy.json"
ZONA_HORARIA_LOCAL = datetime.timezone(datetime.timedelta(hours=-5))

EMOJI_TIPO_PRONOSTICO = {
    "favorito_directo": "\U0001F3AF",   # 🎯
    "doble_oportunidad": "\U0001F500",  # 🔀
}
CORONA_FAVORITO = "\U0001F451"  # 👑

CANTIDAD_DESTACADOS = 6
CANTIDAD_GRUPOS = 2


def _hora_local(hora_inicio_utc_iso):
    if not hora_inicio_utc_iso:
        return "?"
    try:
        dt_utc = datetime.datetime.fromisoformat(hora_inicio_utc_iso.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZONA_HORARIA_LOCAL)
        return dt_local.strftime("%H:%M")
    except Exception:
        return hora_inicio_utc_iso


def _titulo_partido(p):
    emoji_tipo = EMOJI_TIPO_PRONOSTICO.get(p.get("tipo_pronostico"), EMOJI_TIPO_PRONOSTICO["favorito_directo"])
    marca_favorito = f" {emoji_tipo}{CORONA_FAVORITO}"
    corona_local = marca_favorito if p.get("favorito_es_local") else ""
    corona_visitante = marca_favorito if not p.get("favorito_es_local") else ""
    return f"{escapar_html(p['local'])}{corona_local} vs {escapar_html(p['visitante'])}{corona_visitante}"


def elegir_destacados(partidos):
    """Devuelve (grupos, elegidos) -- grupos es una lista de listas de
    partidos, elegidos es la lista plana (para marcar en el JSON)."""
    disponibles = [p for p in partidos if p.get("fixture_id")]
    cantidad = min(CANTIDAD_DESTACADOS, len(disponibles))
    elegidos = random.sample(disponibles, cantidad) if cantidad else []

    grupos = [[] for _ in range(CANTIDAD_GRUPOS)]
    for i, p in enumerate(elegidos):
        grupo_idx = i % CANTIDAD_GRUPOS
        p["es_destacado"] = True
        p["grupo_destacado"] = grupo_idx + 1
        grupos[grupo_idx].append(p)
    return grupos, elegidos


def enviar_destacados(forzar=False):
    if not forzar and ya_se_hizo("destacados"):
        print("Los destacados de hoy ya se enviaron antes. Nada que hacer.")
        return

    if not ARCHIVO.exists():
        print("Fase 1 todavia no ha generado partidos_hoy.json. Se reintentara en el proximo ciclo.")
        return

    datos = json.loads(ARCHIVO.read_text(encoding="utf-8"))
    partidos = datos.get("partidos", [])

    grupos, elegidos = elegir_destacados(partidos)

    if not elegidos:
        exito = enviar_mensaje_telegram(
            "\U0001F3B2 Hoy no hay partidos con seguimiento en vivo disponible para destacar."
        )
        if exito:
            marcar_hecho("destacados")
        print("Destacados: 0 partidos disponibles hoy." if exito else "Fallo el envio de destacados.")
        return

    lineas = [f"\U0001F3B2 <b>Selección destacada de hoy</b> ({len(elegidos)} partido(s), {CANTIDAD_GRUPOS} grupos)"]
    for idx, grupo in enumerate(grupos, start=1):
        if not grupo:
            continue
        lineas.append(f"\n<b>Grupo {idx}</b>")
        for p in grupo:
            hora = _hora_local(p.get("hora_inicio"))
            lineas.append(f"{hora} -- {_titulo_partido(p)}")
            lineas.append(f"Favorito: <b>{escapar_html(p['favorito'])}</b>")

    exito = enviar_mensaje_telegram("\n".join(lineas))
    if exito:
        ARCHIVO.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
        marcar_hecho("destacados")
    print(f"Destacados enviados: {len(elegidos)} partido(s) en {CANTIDAD_GRUPOS} grupos." if exito else "Fallo el envio de destacados.")


if __name__ == "__main__":
    enviar_destacados(forzar="--forzar" in sys.argv)
