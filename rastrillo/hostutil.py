"""Normalización de host / slug — funciones compartidas.

Antes vivía en 3 sitios con 3 semánticas distintas:

  - `discovery._slugify` y `recipes_auto._slug` eran idénticas → ahora
    `slugify`.
  - `discovery._host_from_url` (host completo, corta querystring, hace
    strip, solo quita `www.`) → ahora `host_from_url`.
  - `resolver._host_of` (host completo, corta puerto, NO hace strip,
    NO corta querystring, solo quita `www.`) → ahora `host_of`.

NO unificamos las tres últimas en una sola función: las diferencias son
sutiles pero relevantes (querystring vs puerto, strip vs no strip) y
tests de caracterización (tests/test_hostutil_caracterizacion.py)
congelan la salida exacta para que nadie vuelva a colapsarlas a ciegas.
"""
from __future__ import annotations
import re


# Prefijos que `slugify` quita del host antes de quedarse con el primer
# token. Importante: el bucle los aplica EN ORDEN y SECUENCIALMENTE;
# "m.old.reddit.com" → "old.reddit.com" → "reddit.com" → "reddit".
_SLUG_PREFIXES = ("www.", "m.", "old.", "new.")


def slugify(name: str) -> str:
    """Normaliza 'Reddit', 'old.reddit.com' o una URL a 'reddit'.

    Pasos: lower, quita esquema, quita path, quita prefijos `_SLUG_PREFIXES`
    en orden y de forma secuencial, se queda con el primer token (antes
    del primer `.`), y elimina cualquier carácter no alfanumérico.
    """
    s = (name or "").strip().lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0]
    for prefix in _SLUG_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.split(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", s)


def host_from_url(url: str) -> str:
    """Devuelve el host limpio de una URL (`'reddit.com'`, `'old.reddit.com'`).

    Pasos: strip + lower, quita esquema, quita path y querystring, quita
    SOLO el primer `www.`. NO corta puerto. NO toca `m.`/`old.`/`new.`.
    """
    if not url:
        return ""
    s = url.strip().lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def host_of(url: str) -> str:
    """Host normalizado para clave de throttle (sin `www.` ni puerto).

    OJO: difiere de `host_from_url` en tres cosas:
      - NO hace `.strip()` (espacios al inicio/final del input se conservan
        en el resultado).
      - Corta en `:` (descarta el puerto).
      - NO corta en `?` (querystring queda pegada al host si no hay `/`).
    """
    if not url:
        return ""
    s = url.lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0].split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s
