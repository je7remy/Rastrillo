# Contribuir a Rastrillo

Gracias por considerar contribuir. Esto es lo que necesitas saber para
moverte por el repo sin romper nada.

## Entorno de desarrollo

Clona, instala y verifica:

```bash
git clone https://github.com/je7remy/Rastrillo.git
cd rastrillo
bash install.sh        # Linux/Mac
# .\install.ps1        # Windows PowerShell
```

El instalador crea `.venv/`, mete las dependencias, descarga Chromium y
registra el ejecutable dentro del venv.

Para ejecutar el comando sin activar nada usa los wrappers
(`./rastrillo.sh`, `.\rastrillo.ps1`, `rastrillo.cmd`). Detalles completos
en el [README](README.md).

## Tests

Antes de cualquier cambio, corre la suite completa:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -t . -s tests -v
```

```bash
.venv/bin/python -m unittest discover -t . -s tests -v
```

Son **165 tests**, ~1 minuto en Windows, ~30 s en Linux. Cada test corre
con su propio `RASTRILLO_HOME` en tempdir, sin depender de claves de API
ni red real.

Si añades código nuevo, añade test. Mira `tests/test_engine_html_local.py`
como ejemplo de cómo probar el motor sin tocar sitios reales (servidor
HTTP local + FakePage).

## Reglas del proyecto

1. **Código y comentarios en español.** Consistencia con todo el repo.
2. **Sin dependencias nuevas** salvo justificación clara en el PR.
3. **Nada específico de plataforma en `engine.py`.** Lo específico vive
   en recetas (`rastrillo/recipes/*.json`) o en el directorio JustDeleteMe.
4. **Invariantes que NO se rompen** (ver `CLAUDE.md` para el detalle):
   - Cero contraseñas guardadas en disco.
   - Human-in-the-loop obligatorio para CAPTCHA, 2FA y confirmaciones.
   - Solo cuentas propias: `confirm_owned=true` antes de cualquier acción
     destructiva.
   - `KEEP_PLATFORMS` (tiktok, instagram, linkedin, github) siempre van a
     `skipped`.
5. **Antes de cerrar un cambio**: `python -m py_compile` sobre lo tocado +
   suite completa.

## Flujo de PR

1. Fork + branch con nombre descriptivo (`fix/...`, `feat/...`, `docs/...`).
2. Commit pequeños y atómicos. Mensaje en español.
3. Suite verde antes de abrir el PR.
4. Rellena la plantilla de PR — describe el qué y el por qué.

## Manual interno

`CLAUDE.md` en la raíz contiene el manual detallado: mapa del código,
invariantes, convenciones, descripción de cada test y dónde vive cada
cosa. Si vas a tocar algo no trivial, léelo primero.
