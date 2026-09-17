import sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE / "py"))
# the runner loads the basetest plugin with -p basetest; direct runs: PYTHONPATH=py pytest suite -p basetest
