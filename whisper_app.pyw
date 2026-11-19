"""Whisper Video Translator - desktop launcher (run with pythonw so no console window appears).

Starts the engine (app.py) if it is not already running, opens it in its own app window (Edge or Chrome app
mode, no tabs), and stops the engine again when that window is closed.
Use create_shortcut.ps1 once to put an icon on the Desktop and Start menu.
"""
import ctypes, os, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:7860"
DATA = os.path.join(os.environ.get("LOCALAPPDATA", ROOT), "Whisper")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def message(text):
    ctypes.windll.user32.MessageBoxW(0, text, "Whisper Video Translator", 0x40)


def is_up():
    try:
        return urllib.request.urlopen(URL, timeout=2).status == 200
    except Exception:
        return False


def find_browser():
    pf, pf86 = os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")
    for p in (rf"{pf86}\Microsoft\Edge\Application\msedge.exe", rf"{pf}\Microsoft\Edge\Application\msedge.exe",
              rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(p):
            return p
    return None


def main():
    os.chdir(ROOT)
    os.makedirs(DATA, exist_ok=True)
    env = dict(os.environ, WHISPER_NO_BROWSER="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    # installed layout: runtime\ (bundled Python + packages) and bin\ (ffmpeg); otherwise the project's .venv
    bundled = os.path.join(ROOT, "runtime", "python.exe")
    py = bundled if os.path.exists(bundled) else os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    if os.path.exists(bundled):
        env["PYTHONNOUSERSITE"] = "1"
    if os.path.isdir(os.path.join(ROOT, "bin")):
        env["PATH"] = os.path.join(ROOT, "bin") + os.pathsep + env.get("PATH", "")
    engine = None
    if not is_up():
        if not os.path.exists(py):
            message(f"Python environment not found in {ROOT}. See SETUP_GPU_PC.md.")
            return
        log = open(os.path.join(DATA, "engine.log"), "w", encoding="utf-8", errors="replace")
        engine = subprocess.Popen([py, "app.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  creationflags=NO_WINDOW)
        deadline = time.time() + 240
        while not is_up():
            if engine.poll() is not None or time.time() > deadline:
                message(f"Whisper could not start. Details: {os.path.join(DATA, 'engine.log')}")
                return
            time.sleep(1)
    browser = find_browser()
    if not browser:
        os.startfile(URL)  # no Edge/Chrome: default browser; the engine keeps running until you stop it
        return
    # separate profile so this window is its own process and we can wait for it to close
    win = subprocess.Popen([browser, f"--app={URL}", f"--user-data-dir={os.path.join(DATA, 'window')}",
                            "--window-size=1200,900", "--no-first-run"])
    win.wait()
    if engine and engine.poll() is None:
        engine.terminate()


if __name__ == "__main__":
    main()
