"""
telegram_utils.py
------------------
SIN CAMBIOS por la migracion a ESPN. Envio de mensajes a Telegram con
un bot propio (gratis). Divide mensajes largos respetando el limite de
4096 caracteres de Telegram, y reporta el detalle exacto si un envio
falla.
"""

import os
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LIMITE_TELEGRAM = 4096
MARGEN_SEGURIDAD = 200
LIMITE_EFECTIVO = LIMITE_TELEGRAM - MARGEN_SEGURIDAD


def escapar_html(texto):
    if texto is None:
        return ""
    return str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _dividir_mensaje(texto, limite=LIMITE_EFECTIVO):
    if len(texto) <= limite:
        return [texto]

    partes = []
    lineas = texto.split("\n")
    actual = ""
    for linea in lineas:
        candidato = (actual + "\n" + linea) if actual else linea
        if len(candidato) > limite:
            if actual:
                partes.append(actual)
            if len(linea) > limite:
                for i in range(0, len(linea), limite):
                    partes.append(linea[i:i + limite])
                actual = ""
            else:
                actual = linea
        else:
            actual = candidato
    if actual:
        partes.append(actual)
    return partes


def _enviar_una_parte(texto):
    if not TOKEN or not CHAT_ID:
        print("[AVISO] Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID. No se envio el mensaje:")
        print(texto)
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML"}, timeout=15)
        if not r.ok:
            print(f"[ERROR] Telegram respondio {r.status_code}: {r.text}")
        r.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] No se pudo enviar el mensaje de Telegram: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] No se pudo enviar el mensaje de Telegram: {e}")
        return False


NOMBRE_PROYECTO = "Alertas Excel"  # antes "Alertas ESPN" -- renombrado a pedido explicito, agosto 2026


def enviar_mensaje_telegram(texto):
    texto_con_encabezado = f"\u2699\uFE0F <b>{NOMBRE_PROYECTO}</b>\n{texto}"
    partes = _dividir_mensaje(texto_con_encabezado)
    if len(partes) > 1:
        print(f"[INFO] Mensaje de {len(texto_con_encabezado)} caracteres supera el limite de Telegram, "
              f"se divide en {len(partes)} partes.")

    exito_total = True
    for i, parte in enumerate(partes, start=1):
        prefijo = f"(parte {i}/{len(partes)})\n" if len(partes) > 1 else ""
        if not _enviar_una_parte(prefijo + parte):
            exito_total = False
    return exito_total
