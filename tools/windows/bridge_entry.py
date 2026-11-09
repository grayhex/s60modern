"""PyInstaller entry, including private frozen Gammu worker dispatch."""
import multiprocessing
import sys

from symport.bridge.main import main

if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
