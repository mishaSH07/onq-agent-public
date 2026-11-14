import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

app=Path(__file__).resolve().parent
try: urllib.request.urlopen('http://127.0.0.1:8765/api/state',timeout=2)
except Exception:
    subprocess.Popen([sys.executable,str(app/'launch.py')],cwd=app,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(30):
        try:
            urllib.request.urlopen('http://127.0.0.1:8765/api/state',timeout=1)
            break
        except Exception: time.sleep(1)
webbrowser.open('http://127.0.0.1:8765')
