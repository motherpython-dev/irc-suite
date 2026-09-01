"""Точка входа. Работает и из исходников, и внутри собранного exe."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap.main import main

if __name__ == "__main__":
    sys.exit(main())
