"""python -m backend: passa da run.py (unico avvio: waitress, log, backup, turni)."""
import os
import runpy

if __name__ == '__main__':
    runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'run.py'), run_name='__main__')
