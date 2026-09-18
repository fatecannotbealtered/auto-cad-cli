"""Allow `python -m auto_cad_cli` alongside `python -m auto_cad_cli.main`."""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
