import os
import subprocess
import sys
from sdd.config import load_env

load_env()
url = os.getenv("DATABASE_URL", "")
if url.startswith("postgresql"):
    os.environ["SDD_TEST_POSTGRES_URL"] = url
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q"]))
