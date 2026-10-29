"""`python -m luadetect` 的進入點。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
