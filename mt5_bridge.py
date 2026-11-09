import pathlib, urllib.request
url = "https://raw.githubusercontent.com/Aasum-coder/aureus-mt5-bridge/f4bdf21f45a421ec9db2e2028a180faed6df11a4/mt5_bridge.py"
text = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
if "def send_market" not in text:
    raise SystemExit("Bridge download was not the real file.")
path = pathlib.Path(__file__).resolve()
path.write_text(text, encoding="utf-8")
print("Restored bridge from the last good version. Start it again with the same command.")
