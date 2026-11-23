"""
YT Downloader - small GUI for yt-dlp (Windows)

Run it:      double-click, or  python yt_downloader_gui.py
Pin it:      click "Create desktop shortcut" inside the app, then right-click the
             new shortcut on your Desktop -> "Pin to taskbar" / "Pin to Start".
             (On Windows 11: right-click -> "Show more options" -> "Pin to taskbar",
              or just drag the shortcut onto the taskbar.)

Everything it needs (yt-dlp, ffmpeg, a JavaScript runtime) is installed
automatically the first time you press Download.
"""

import importlib
import json
import os
import queue
import shutil
import subprocess
import sys
import sysconfig
import threading
import tkinter as tk
from tkinter import filedialog, ttk

# pythonw.exe has no console, so stdout/stderr can be None. Make them safe.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

APP_NAME = "YT Downloader"
SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "YouTube Downloads")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".yt_downloader_gui.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Make the taskbar treat this as its own app (needed for clean pinning) + sharp text
if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YTDownloader.GUI")
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Settings + setup helpers
# ---------------------------------------------------------------------------
def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:  # noqa: BLE001
        pass


def refresh_paths():
    """Make freshly pip-installed packages importable without restarting."""
    try:
        import site

        user_site = site.getusersitepackages()
        if user_site and os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.append(user_site)
    except Exception:  # noqa: BLE001
        pass
    importlib.invalidate_caches()


def pip_install(*packages):
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", "--quiet",
         "--no-warn-script-location", *packages],
        check=True, capture_output=True, creationflags=NO_WINDOW,
    )
    refresh_paths()


def can_import(name):
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def find_in_scripts(exe_name):
    """Look for an installed .exe in the pip Scripts folders."""
    dirs = []
    try:
        dirs.append(sysconfig.get_path("scripts"))
        dirs.append(sysconfig.get_path("scripts", f"{os.name}_user"))
    except Exception:  # noqa: BLE001
        pass
    for d in dirs:
        if d:
            p = os.path.join(d, exe_name + (".exe" if os.name == "nt" else ""))
            if os.path.exists(p):
                return p
    return None


def find_ffmpeg(report):
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        if not can_import("imageio_ffmpeg"):
            report("First-time setup: getting ffmpeg...")
            pip_install("imageio-ffmpeg")
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def find_js_runtime(report):
    for name in ("deno", "node"):
        path = shutil.which(name)
        if path:
            return {name: {"path": path}}
    try:
        report("First-time setup: getting a JavaScript runtime...")
        pip_install("deno")
        path = None
        try:
            import deno

            path = deno.find_deno_bin()
        except Exception:  # noqa: BLE001
            pass
        path = path or find_in_scripts("deno")
        if path:
            return {"deno": {"path": path}}
    except Exception:  # noqa: BLE001
        pass
    return None


class QuietLogger:
    """yt-dlp logger that prints nothing (the GUI shows status instead)."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def fmt_speed(bytes_per_sec):
    if not bytes_per_sec:
        return ""
    return f"{bytes_per_sec / 1_000_000:.1f} MB/s"


# ---------------------------------------------------------------------------
# The GUI
# ---------------------------------------------------------------------------
class App:
    BG = "#f5f6f8"

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.env = None          # filled in after first-time setup
        self.busy = False
        self.indeterminate = False

        settings = load_settings()
        self.folder = tk.StringVar(value=settings.get("folder", DEFAULT_DIR))
        self.url = tk.StringVar()

        root.title(APP_NAME)
        root.geometry("580x330")
        root.resizable(False, False)
        root.configure(bg=self.BG)
        try:
            root.attributes("-topmost", False)
        except Exception:  # noqa: BLE001
            pass

        style = ttk.Style()
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except Exception:  # noqa: BLE001
            pass
        style.configure("TFrame", background=self.BG)
        style.configure("TLabel", background=self.BG, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=5)
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=8)

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="YouTube link").grid(row=0, column=0, sticky="w")
        self.url_entry = ttk.Entry(frame, textvariable=self.url, font=("Segoe UI", 10))
        self.url_entry.grid(row=1, column=0, sticky="ew", pady=(2, 10), ipady=3)
        ttk.Button(frame, text="Paste", command=self.paste).grid(
            row=1, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(frame, text="Save to").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.folder, font=("Segoe UI", 10)).grid(
            row=3, column=0, sticky="ew", pady=(2, 12), ipady=3)
        ttk.Button(frame, text="Browse...", command=self.browse).grid(
            row=3, column=1, padx=(8, 0), pady=(2, 12))

        self.btn = ttk.Button(frame, text="Download MP4", style="Big.TButton",
                              command=self.start)
        self.btn.grid(row=4, column=0, columnspan=2, sticky="ew")

        self.bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 4))

        self.status = tk.Label(frame, text="Paste a YouTube link and press Download.",
                               bg=self.BG, fg="#444", font=("Segoe UI", 9),
                               wraplength=530, justify="left", anchor="w")
        self.status.grid(row=6, column=0, columnspan=2, sticky="w")

        bottom = ttk.Frame(frame)
        bottom.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(bottom, text="Open folder", command=self.open_folder).pack(side="left")
        ttk.Button(bottom, text="Create desktop shortcut (to pin)",
                   command=self.create_shortcut).pack(side="left", padx=8)

        self.url_entry.bind("<Return>", lambda e: self.start())
        self.url_entry.focus_set()

        # Prefill with a YouTube link if one is on the clipboard
        try:
            clip = root.clipboard_get().strip()
            if "youtube.com" in clip or "youtu.be" in clip:
                self.url.set(clip)
        except Exception:  # noqa: BLE001
            pass

        root.after(100, self.poll)

    # ----- small UI helpers -------------------------------------------------
    def set_status(self, text, color="#444"):
        self.status.config(text=text, fg=color)

    def paste(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except Exception:  # noqa: BLE001
            self.set_status("Clipboard is empty.", "#b00020")

    def browse(self):
        chosen = filedialog.askdirectory(initialdir=self.folder.get() or DEFAULT_DIR)
        if chosen:
            self.folder.set(os.path.normpath(chosen))
            save_settings({"folder": self.folder.get()})

    def open_folder(self):
        folder = self.folder.get().strip() or DEFAULT_DIR
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", folder])

    def create_shortcut(self):
        if os.name != "nt":
            self.set_status("Shortcuts are only supported on Windows.", "#b00020")
            return
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        ps = f'''
$d = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $d "{APP_NAME}.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = "{pyw}"
$s.Arguments = '"{SCRIPT_PATH}"'
$s.WorkingDirectory = "{SCRIPT_DIR}"
$s.IconLocation = "{pyw},0"
$s.Description = "{APP_NAME}"
$s.Save()
Write-Output $lnk
'''
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, creationflags=NO_WINDOW, check=True)
            self.set_status(
                f"Shortcut created on your Desktop. Right-click it -> Pin to taskbar / "
                f"Pin to Start.\n({result.stdout.strip()})", "#1b7f3b")
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Could not create the shortcut: {e}", "#b00020")

    # ----- thread -> UI messages --------------------------------------------
    def post(self, kind, value=None):
        self.q.put((kind, value))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "status":
                    self.set_status(val)
                elif kind == "indeterminate":
                    if not self.indeterminate:
                        self.bar.configure(mode="indeterminate")
                        self.bar.start(12)
                        self.indeterminate = True
                elif kind == "progress":
                    if self.indeterminate:
                        self.bar.stop()
                        self.bar.configure(mode="determinate")
                        self.indeterminate = False
                    self.bar["value"] = val
                elif kind == "done":
                    self.finish()
                    self.bar["value"] = 100
                    self.set_status(f"Done! Saved: {val}", "#1b7f3b")
                    self.url.set("")
                    self.url_entry.focus_set()
                elif kind == "error":
                    self.finish()
                    self.bar["value"] = 0
                    self.set_status(f"Could not download: {val}", "#b00020")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def finish(self):
        if self.indeterminate:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.indeterminate = False
        self.busy = False
        self.btn.config(state="normal")

    # ----- download ----------------------------------------------------------
    def start(self):
        if self.busy:
            return
        url = self.url.get().strip()
        if not url:
            self.set_status("Please paste a YouTube link first.", "#b00020")
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        folder = self.folder.get().strip() or DEFAULT_DIR
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Can't use that folder: {e}", "#b00020")
            return
        save_settings({"folder": folder})

        self.busy = True
        self.btn.config(state="disabled")
        self.bar["value"] = 0
        self.set_status("Starting...")
        threading.Thread(target=self.worker, args=(url, folder), daemon=True).start()

    def ensure_ready(self):
        if self.env:
            return self.env

        self.post("indeterminate")
        if not (can_import("yt_dlp") and can_import("yt_dlp_ejs")):
            self.post("status", "First-time setup: installing yt-dlp (about a minute)...")
            pip_install("yt-dlp[default]")

        report = lambda msg: self.post("status", msg)  # noqa: E731
        self.env = {
            "ffmpeg": find_ffmpeg(report),
            "js": find_js_runtime(report),
        }
        return self.env

    def worker(self, url, folder):
        try:
            env = self.ensure_ready()
            import yt_dlp

            if env["ffmpeg"]:
                attempts = [
                    "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*+ba/b",
                    "bv*+ba/b",
                    None,
                ]
            else:
                attempts = ["b[ext=mp4]/b", None]

            def progress_hook(d):
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    done = d.get("downloaded_bytes") or 0
                    if total:
                        self.post("progress", done / total * 100)
                        self.post("status",
                                  f"Downloading... {done / total * 100:.0f}%  "
                                  f"{fmt_speed(d.get('speed'))}")
                elif d["status"] == "finished":
                    self.post("status", "Almost done, finishing up...")

            def pp_hook(d):
                if d["status"] == "started":
                    self.post("indeterminate")
                    self.post("status", "Merging video and audio into MP4...")

            last_error = None
            for fmt in attempts:
                opts = {
                    "outtmpl": os.path.join(folder, "%(title)s.%(ext)s"),
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "logger": QuietLogger(),
                    "windowsfilenames": True,
                    "remote_components": ["ejs:github"],
                    "progress_hooks": [progress_hook],
                    "postprocessor_hooks": [pp_hook],
                }
                if env["js"]:
                    opts["js_runtimes"] = env["js"]
                if env["ffmpeg"]:
                    opts["ffmpeg_location"] = env["ffmpeg"]
                    opts["merge_output_format"] = "mp4"
                if fmt:
                    opts["format"] = fmt

                try:
                    self.post("status", "Fetching video info...")
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    downloads = info.get("requested_downloads") or []
                    path = downloads[0].get("filepath") if downloads else None
                    name = os.path.basename(path) if path else info.get("title", "video")
                    self.post("done", name)
                    return
                except yt_dlp.utils.DownloadError as e:
                    last_error = e
                    if "format is not available" in str(e).lower():
                        self.post("status", "That format isn't available, trying another...")
                        continue
                    break

            msg = str(last_error).replace("ERROR: ", "", 1) if last_error else "Unknown error"
            if "format is not available" in msg.lower() and not env["js"]:
                msg += ("\n\nNo JavaScript runtime was found. Open a terminal, run "
                        "'winget install DenoLand.Deno', then restart this app.")
            self.post("error", msg)

        except Exception as e:  # noqa: BLE001
            self.post("error", str(e))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()