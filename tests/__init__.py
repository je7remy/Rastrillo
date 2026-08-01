# Suite de tests unitarios + integración. Se ejecuta con:
#   python -m unittest discover -s tests -v
#
# El aviso de `httpx2` NO se silencia aquí. Un filtro puesto en el import no
# sobrevive: `unittest.TextTestRunner.run` hace `warnings.simplefilter(...)`,
# que reemplaza la lista de filtros entera antes de ejecutar los tests. Va en
# el punto de import real — ver `helpers.auth_client`.
