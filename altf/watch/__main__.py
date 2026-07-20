"""`python -m altf.watch <session-dir>`."""

import sys

from ..cli import main

if __name__ == "__main__":
    sys.exit(main(["watch", *sys.argv[1:]]))
