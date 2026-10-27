"""Source-tree entry point; works without editable installation or shell activation."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from boneng_mcp.server import main

if __name__ == "__main__":
    main()
