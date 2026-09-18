"""PyInstaller entry point.

Deliberately not `auto_cad_cli/main.py`. PyInstaller runs its entry script as
`__main__` with no package context, so every relative import inside that module
fails and the frozen binary dies before it can emit a single envelope. Importing
the package from the outside keeps that context intact.
"""

from auto_cad_cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
