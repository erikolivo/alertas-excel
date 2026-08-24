"""
reporte_diario.py
------------------
Reintenta cada 15 min entre las 06:00 y las 07:00.

CAMBIO por la migracion a ESPN: la linea de cupo de API-Football
("usadas/disponibles de 100") se reemplaza por solo "peticiones
realizadas ayer", sin denominador -- ESPN no publica un limite diario
conocido (ver cuota_espn.py). Sigue siendo util para ver tendencias.

Todo lo demas (acierto por partido, por tipo de alerta, por madurez del
rating propio) sigue igual.
"""

import json
import datetime
from pathlib import Path

from telegram_utils import enviar_mensaje_telegram, escapar_html
from estado_diario import ya_se_hizo, marcar_hecho

DATA_DIR = Path(__file__).parent / "data"
DIR_HISTORIAL_DIAS = DATA_DIR / "historial_dias"
ZONA_HORARIA_LOCAL = datetime.timezone(datetime.timedelta(hours=-5))


def _tramo_madurez(n):
    if n == 0:
        return "0 (solo ClubElo)"
    if n <= 3:
        return "1-3"
    if n <= 8:
        return "4-8"
    if n <= 15:
        return "9-15"
    return ">15"


def _resumen_alertas(partidos):
    conteo = {}
    for p in partidos:
        for a in p.get("alertas_enviadas", []):
            tipo = a["tipo"]
            conteo.setdefault(tipo, {"n": 0, "aciertos": 0, "evaluables": 0})
            conteo[tipo]["n"] += 1
            if a.get("acierto") is not None:
                conteo[tipo]["evaluables"] += 1
                if a["acierto"]:
                    conteo[tipo]["aciertos"] += 1
    return conteo


def _resumen_madurez(partidos):
    tramos = {}
    for p in partidos:
        if p.get("acierto") is None:
            continue
        n_min = min(p.get("rating_propio_partidos_local", 0), p.get("rating_propio_partidos_visitante", 0))
        tramo = _tramo_madurez(n_min)
        tramos.setdefault(tramo, {"total": 0, "aciertos": 0})
        tramos[tramo]["total"] += 1
        if p["acierto"]:
            tramos[tramo]["aciertos"] += 1
    return tramos


def enviar_reporte():
    if ya_se_hizo("reporte"):
        print("El reporte de hoy ya se envio antes. Nada que hacer.")
        return

    ayer = (datetime.datetime.now(ZONA_HORARIA_LOCAL).date() - datetime.timedelta(days=1)).isoformat()
    archivo_ayer = DIR_HISTORIAL_DIAS / f"{ayer}.json"

    if not archivo_ayer.exists():
        print(f"Todavia no existe {archivo_ayer} (Fase 4 de ayer puede seguir reintentando). "
              f"Se reintentara en el proximo ciclo.")
        return

    datos = json.loads(archivo_ayer.read_text(encoding="utf-8"))
    partidos = datos.get("partidos", [])

    lineas = [f"\U0001F4CA <b>Resultados de ayer ({ayer})</b>"]

    if not partidos:
        lineas.append("No hubo partidos seleccionados.")
    else:
        for p in partidos:
            if p.get("acierto") is True:
                marca = "\u2705"
            elif p.get("acierto") is False:
                marca = "\u274C"
            else:
                marca = "\u2753"
            resultado_txt = f" (resultado {p['resultado_final']})" if p.get("resultado_final") else " (sin resolver)"
            lineas.append(f"{marca} {escapar_html(p['partido'])} -- favorito: {escapar_html(p['favorito'])}{resultado_txt}")

        resueltos = [p for p in partidos if p.get("acierto") is not None]
        aciertos = sum(1 for p in resueltos if p["acierto"])
        if resueltos:
            pct = round((aciertos / len(resueltos)) * 100, 1)
            lineas.append(f"\nTotal: {aciertos}/{len(resueltos)} aciertos ({pct}%)")

        # NUEVO (agosto 2026, a pedido explicito) -- acierto del grupo
        # de 6 destacados (destacados.py) por separado del total
        # general, para poder comparar con el tiempo si la seleccion al
        # azar rinde distinto al conjunto completo.
        destacados_resueltos = [p for p in resueltos if p.get("es_destacado")]
        if destacados_resueltos:
            aciertos_destacados = sum(1 for p in destacados_resueltos if p["acierto"])
            pct_destacados = round((aciertos_destacados / len(destacados_resueltos)) * 100, 1)
            lineas.append(f"\U0001F3B2 Destacados: {aciertos_destacados}/{len(destacados_resueltos)} aciertos ({pct_destacados}%)")

        resumen_alertas = _resumen_alertas(partidos)
        if resumen_alertas:
            lineas.append("\n\U0001F514 <b>Acierto por tipo de alerta:</b>")
            for tipo, c in sorted(resumen_alertas.items(), key=lambda x: -x[1]["n"]):
                if c["evaluables"] > 0:
                    pct = round((c["aciertos"] / c["evaluables"]) * 100, 1)
                    lineas.append(f"  - {escapar_html(tipo)}: {c['n']} enviadas, {pct}% acierto ({c['aciertos']}/{c['evaluables']} evaluables)")
                else:
                    lineas.append(f"  - {escapar_html(tipo)}: {c['n']} enviadas (sin evaluacion aun)")

        resumen_madurez = _resumen_madurez(partidos)
        if resumen_madurez:
            lineas.append("\n\U0001F4C8 <b>Acierto por madurez del rating propio:</b>")
            orden_tramos = ["0 (solo ClubElo)", "1-3", "4-8", "9-15", ">15"]
            for tramo in orden_tramos:
                if tramo in resumen_madurez:
                    c = resumen_madurez[tramo]
                    pct = round((c["aciertos"] / c["total"]) * 100, 1) if c["total"] else None
                    lineas.append(f"  - n={tramo}: {c['aciertos']}/{c['total']} aciertos ({pct}%)")

    usadas = datos.get("espn_peticiones_usadas")
    if usadas is None:
        usadas = datos.get("api_football_usadas")  # compatibilidad con dias archivados antes de la migracion
    if usadas is not None:
        lineas.append(f"\n\U0001F527 Peticiones a ESPN realizadas ayer: {usadas} "
                       f"(ESPN no publica un limite diario oficial, este numero es solo de referencia)")

    exito = enviar_mensaje_telegram("\n".join(lineas))
    if exito:
        marcar_hecho("reporte")
    print(f"Reporte diario enviado ({len(partidos)} partidos)." if exito else "Fallo el envio del reporte.")


if __name__ == "__main__":
    enviar_reporte()
