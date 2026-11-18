from pathlib import Path
import sys


def main() -> None:
    """`uv run sabor`: abre a interface Streamlit (argumentos extras vão para o Streamlit)."""
    from streamlit.web import cli

    sys.argv = ['streamlit', 'run', str(Path(__file__).parent / 'ui' / 'app.py'), *sys.argv[1:]]
    sys.exit(cli.main())
