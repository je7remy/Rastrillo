## Qué cambia

<!-- Resumen en una o dos frases. -->

## Por qué

<!-- Contexto, motivación, link a issue si aplica. -->

## Cómo lo he probado

<!-- Concreto: qué tests añadiste / ejecutaste, qué flujo manual probaste
     en el dashboard o el motor, en qué SO. -->

```bash
# Suite completa antes de mergear:
python -m unittest discover -t . -s tests -v
```

## Checklist

- [ ] Suite completa en verde (`unittest discover -t . -s tests`).
- [ ] `python -m py_compile` sobre los ficheros tocados.
- [ ] Sin dependencias nuevas (o justificadas en este PR).
- [ ] Código y comentarios en español.
- [ ] Nada específico de plataforma en `engine.py` (recetas o directorio).
- [ ] Si toca invariantes, se documentan en `CLAUDE.md`.
- [ ] Si toca env vars o flujo de usuario, se actualiza `README.md`.

## Notas

<!-- Lo que el reviewer no podría adivinar leyendo el diff. -->
