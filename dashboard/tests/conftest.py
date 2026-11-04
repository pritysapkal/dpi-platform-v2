import sys
from pathlib import Path

# app.py does `import queries` (Streamlit puts the script's folder on the path); do the same for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
