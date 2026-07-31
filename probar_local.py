"""Prueba local sin tocar GitHub. Carga variables desde un archivo .env
(si existe) y ejecuta el bot una vez.

Uso:
  1. Copia .env.example a .env y rellena tus datos.
  2. python probar_local.py

También puedes probar SOLO el parser con un correo pegado:
  python probar_local.py --solo-parser
"""

import os
import sys
from pathlib import Path


def cargar_env():
    env = Path(__file__).parent / ".env"
    if not env.exists():
        return
    for linea in env.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip())


if __name__ == "__main__":
    cargar_env()

    if "--solo-parser" in sys.argv:
        # No requiere credenciales: pega un correo y ve qué extrae.
        from src.parser import parsear
        print("Pega el texto del correo y termina con Ctrl+Z (Windows) / Ctrl+D:")
        cuerpo = sys.stdin.read()
        print(parsear("Compra con Tarjeta de Crédito", cuerpo, uid="local"))
    else:
        from src.main import main
        main()
