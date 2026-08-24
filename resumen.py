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
import sys
from pathlib import Path

from telegram_utils import enviar_mensaje_telegram, escapar_html
from estado_diario import ya_se_hizo, marcar_hecho

ARCHIVO = Path(__file__).parent / "data" / "partidos_hoy.json"
ZONA_HORARIA_LOCAL = datetime.timezone(datetime.timedelta(hours=-5))

# Mismo esquema de emojis que monitor.py (Fase 3), a pedido explicito.
EMOJI_TIPO_PRONOSTICO = {
    "favorito_directo": "\U0001F3AF",   # 🎯
    "doble_oportunidad": "\U0001F500",  # 🔀
}
CORONA_FAVORITO = "\U0001F451"  # 👑


def _hora_local(hora_inicio_utc_iso):
    if not hora_inicio_utc_iso:
        return "?"
    try:
        dt_utc = datetime.datetime.fromisoformat(hora_inicio_utc_iso.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZONA_HORARIA_LOCAL)
        return dt_local.strftime("%H:%M")
    except Exception:
        return hora_inicio_utc_iso


def enviar_resumen(forzar=False):
    """forzar=True (a pedido explicito, agosto 2026) ignora ya_se_hizo()
    -- util para probar el mensaje manualmente sin esperar al dia
    siguiente. Vuelve a mandar el mismo resumen de hoy si ya se habia
    enviado antes; no borra ni duplica nada en partidos_hoy.json, solo
    reenviar el mensaje a Telegram."""
    if not forzar and ya_se_hizo("resumen"):
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
        emoji_tipo = EMOJI_TIPO_PRONOSTICO.get(p.get("tipo_pronostico"), EMOJI_TIPO_PRONOSTICO["favorito_directo"])
        marca_favorito = f" {emoji_tipo}{CORONA_FAVORITO}"
        corona_local = marca_favorito if p.get("favorito_es_local") else ""
        corona_visitante = marca_favorito if not p.get("favorito_es_local") else ""
        titulo = f"{escapar_html(p['local'])}{corona_local} vs {escapar_html(p['visitante'])}{corona_visitante}"
        lineas.append(
            f"\n{hora} -- {titulo} {estado}"
        )
        lineas.append(f"Favorito: <b>{escapar_html(p['favorito'])}</b>")

        cuota_l = p.get("cuota_local_inicial")
        cuota_x = p.get("cuota_empate_inicial")
        cuota_v = p.get("cuota_visitante_inicial")
        if cuota_l or cuota_v:
            partes_cuota = [f"{escapar_html(p['local'])} {cuota_l}" if cuota_l else None,
                             f"Empate {cuota_x}" if cuota_x else None,
                             f"{escapar_html(p['visitante'])} {cuota_v}" if cuota_v else None]
            lineas.append("Cuota: " + " | ".join(c for c in partes_cuota if c))

    exito = enviar_mensaje_telegram("\n".join(lineas))
    if exito:
        marcar_hecho("resumen")
    print(f"Resumen enviado con {len(partidos)} partido(s)." if exito else "Fallo el envio del resumen.")


if __name__ == "__main__":
    enviar_resumen(forzar="--forzar" in sys.argv)
