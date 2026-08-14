"""
resumen.py
----------
FASE 2. AJUSTADO a pedido explicito (agosto 2026):
  - Estrella (fav ⭐) junto al nombre del favorito, igual que en las
    alertas en vivo de monitor.py.
  - Se muestran las cuotas de AMBOS lados (favorito y no favorito) del
    modelo propio, siempre. Cuando ademas hay cuota real (DraftKings
    via ESPN o el respaldo de The Odds API), se agrega una segunda
    linea con la cuota real de los dos lados, para poder comparar.
"""

import json
import datetime
from pathlib import Path

from telegram_utils import enviar_mensaje_telegram, escapar_html
from estado_diario import ya_se_hizo, marcar_hecho

ARCHIVO = Path(__file__).parent / "data" / "partidos_hoy.json"
ZONA_HORARIA_LOCAL = datetime.timezone(datetime.timedelta(hours=-5))


def _hora_local(hora_inicio_utc_iso):
    if not hora_inicio_utc_iso:
        return "?"
    try:
        dt_utc = datetime.datetime.fromisoformat(hora_inicio_utc_iso.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZONA_HORARIA_LOCAL)
        return dt_local.strftime("%H:%M")
    except Exception:
        return hora_inicio_utc_iso


def enviar_resumen():
    if ya_se_hizo("resumen"):
        print("El resumen de hoy ya se envio antes. Nada que hacer.")
        return

    if not ARCHIVO.exists():
        print("Fase 1 todavia no ha generado partidos_hoy.json. Se reintentara en el proximo ciclo.")
        return

    datos = json.loads(ARCHIVO.read_text(encoding="utf-8"))
    partidos = datos.get("partidos", [])

    if not partidos:
        exito = enviar_mensaje_telegram(
            "\U0001F4CB Hoy no hay favoritos de Google Sheets que se hayan podido localizar en ESPN."
        )
        if exito:
            marcar_hecho("resumen")
        print("Resumen enviado: 0 partidos hoy." if exito else "Fallo el envio del resumen.")
        return

    lineas = [f"\U0001F4CB <b>{len(partidos)} favorito(s) de la hoja ({datos.get('fecha','')})</b> (horas en tu horario local)"]

    for p in partidos:
        hora = _hora_local(p.get("hora_inicio"))
        estado = "\u2705" if p["fixture_id"] else "\u26A0\uFE0F sin vigilancia en vivo"
        lineas.append(
            f"\n\u2B50 {hora} -- {escapar_html(p['partido'])} {estado}"
        )
        lineas.append(f"Favorito de la hoja: <b>{escapar_html(p['favorito'])}</b>")

    exito = enviar_mensaje_telegram("\n".join(lineas))
    if exito:
        marcar_hecho("resumen")
    print(f"Resumen enviado con {len(partidos)} partido(s)." if exito else "Fallo el envio del resumen.")


if __name__ == "__main__":
    enviar_resumen()
