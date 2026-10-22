import sys
import os
import re
import json
import math
import time
import shutil
import threading
import subprocess
import webbrowser
import tkinter as tk
from datetime import datetime, timezone, timedelta
from tkinter import ttk, scrolledtext, messagebox
from google import genai
from google.genai import types

MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]

# Лимиты запросов в день (бесплатный тариф). Обновляйте вручную по консоли Google Cloud
DAILY_LIMITS = {"gemini-3.5-flash-lite": 500}
DEFAULT_DAILY_LIMIT = 500

KEY_URL = "https://aistudio.google.com/apikey"
QUOTA_URL = "https://console.cloud.google.com/iam-admin/quotas"

TIMEOUT_SEC = 60
ATTEMPTS = 2
TRANSIENT = ("503", "429", "500", "504", "UNAVAILABLE", "timed out", "timeout", "Timeout")

# Для exe (PyInstaller) берём папку, где лежит сам app.exe, а не временную _MEI
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

WORKSPACE = os.path.join(BASE, "workspace")
os.makedirs(WORKSPACE, exist_ok=True)
HISTORY_FILE = os.path.join(BASE, "history.json")
CONFIG_FILE = os.path.join(BASE, "agent_config.json")
MAX_HISTORY = 60   # сколько последних сообщений хранить и отправлять модели
MAX_OUT = 8000
RUN_TIMEOUT = 60

NO_KEY_MSG = "API-ключ не задан. Нажмите «Ввести ключ» и вставьте его."

# Опасные шаблоны: даже при включённой галочке подтверждение спрашивается
DANGER_PATTERNS = [
    r"\bsubprocess\b", r"\bos\.system\b", r"\bos\.popen\b", r"\brmtree\b",
    r"\bos\.remove\b", r"\bos\.unlink\b", r"\bos\.rmdir\b", r"\bshutil\b",
    r"\bctypes\b", r"\bwinreg\b", r"\bdiskpart\b", r"\bformat\s+[a-zA-Z]:",
    r"\bdel\s+/", r"\brmdir\b", r"\brd\s+/s", r"\bRemove-Item\b", r"\breg\s+(add|delete)\b",
]

SYSTEM = (
    "Ты агент на ПК пользователя (Windows). Рабочая папка для создания и запуска "
    f"файлов: {WORKSPACE}. Используй инструменты, когда нужно создать, прочитать "
    "или запустить файл. Запускать можно любые скрипты из рабочей папки через "
    "run_script (.py, .bat, .cmd, .ps1, .js, .vbs). Для окон, анимаций и долгих "
    "программ ставь background=true. Отвечай кратко, на языке пользователя, без Markdown."
)

for stream in (sys.stdout, sys.stderr):
    if stream:
        stream.reconfigure(encoding="utf-8")

APP = None
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


# ---------- конфигурация ----------
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        print(f"Не удалось сохранить настройки: {e}")


def quota_day():
    # Суточная квота Google сбрасывается в полночь по тихоокеанскому времени
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        now = datetime.now(timezone(timedelta(hours=-8)))
    return now.strftime("%Y-%m-%d")


# ---------- инструменты ----------
def safe_path(name):
    p = os.path.abspath(os.path.join(WORKSPACE, name))
    if os.path.commonpath([p, WORKSPACE]) != WORKSPACE:
        raise ValueError("Путь вне рабочей папки")
    return p


def is_secret_file(p):
    # файл настроек содержит API-ключ, модели его читать нельзя
    return os.path.normcase(os.path.abspath(p)) == os.path.normcase(CONFIG_FILE)


def find_danger(code):
    found = []
    for pat in DANGER_PATTERNS:
        m = re.search(pat, code or "", re.IGNORECASE)
        if m:
            found.append(m.group(0))
    return found


def confirm(text, danger=None):
    # Опасный код спрашивается всегда, даже при включённой галочке
    if APP.auto_confirm and not danger:
        return True
    if danger:
        text = f"⚠️ ОПАСНЫЙ КОД: {', '.join(danger)}\n\n{text}"
    ev = threading.Event()
    res = [False]

    def show():
        res[0] = messagebox.askyesno("Подтверждение", text)
        ev.set()

    APP.root.after(0, show)
    ev.wait()
    return res[0]


def find_python(windowed=False):
    """Системный Python. В собранном exe sys.executable - это app.exe, он не годится."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    names = ("pyw", "pythonw") if windowed else ("py", "python")
    for name in names + ("py", "python"):
        p = shutil.which(name)
        if p and "WindowsApps" not in p:  # заглушка Microsoft Store не работает
            return p
    return None


def build_command(path):
    """Возвращает (команда, ошибка) для запуска файла по расширению."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".py", ".pyw"):
        py = find_python(windowed=(ext == ".pyw"))
        if not py:
            return None, "Python не найден в системе. Установите Python и добавьте его в PATH."
        return [py, path], None
    if ext in (".bat", ".cmd"):
        return ["cmd.exe", "/c", path], None
    if ext == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path], None
    if ext == ".js":
        node = shutil.which("node")
        if not node:
            return None, "Node.js не найден в системе."
        return [node, path], None
    if ext == ".vbs":
        return ["cscript.exe", "//Nologo", path], None
    return None, f"Неподдерживаемый тип файла: {ext or '(без расширения)'}"


def clean_env():
    # убираем служебные переменные PyInstaller, чтобы дочерний Python не путался
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("_MEIPASS", "_PYI"))}
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def list_files(folder: str = "") -> str:
    """Показывает файлы и папки. folder: абсолютный путь на ПК или пусто для рабочей папки."""
    APP.set_status("Инструмент: список файлов")
    APP.bump()  # после инструмента модель делает ещё один запрос
    try:
        path = folder if folder else WORKSPACE
        return "\n".join(sorted(os.listdir(path))) or "(пусто)"
    except Exception as e:
        return f"Ошибка: {e}"


def read_file(path: str) -> str:
    """Читает текстовый файл. path: абсолютный путь на ПК или имя файла в рабочей папке."""
    APP.set_status(f"Инструмент: чтение {path}")
    APP.bump()
    try:
        p = path if os.path.isabs(path) else safe_path(path)
        if is_secret_file(p):
            return "Доступ запрещён: это файл настроек программы."
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(MAX_OUT + 1)
        return data[:MAX_OUT] + ("\n...[обрезано]" if len(data) > MAX_OUT else "")
    except Exception as e:
        return f"Ошибка: {e}"


def write_file(filename: str, content: str) -> str:
    """Создаёт или перезаписывает файл в рабочей папке. filename: имя файла, например script.py."""
    APP.set_status(f"Инструмент: запись {filename}")
    APP.bump()
    try:
        p = safe_path(filename)
        danger = find_danger(content)
        if not confirm(f"Записать файл?\n\n{p}\n\n({len(content)} символов)", danger):
            return "Пользователь отклонил запись."
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Файл сохранён: {p}"
    except Exception as e:
        return f"Ошибка: {e}"


def run_script(filename: str, background: bool = False) -> str:
    """Запускает скрипт из рабочей папки: .py, .pyw, .bat, .cmd, .ps1, .js, .vbs.
    background=False: ждёт до 60 секунд и возвращает вывод и ошибки.
    background=True: запускает в отдельном окне и не ждёт (для анимаций, окон, долгих программ)."""
    APP.set_status(f"Инструмент: запуск {filename}")
    APP.bump()
    try:
        p = safe_path(filename)
        if not os.path.exists(p):
            return "Файл не найден."
        cmd, err = build_command(p)
        if err:
            return err
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            danger = find_danger(f.read())
        mode = "в фоне, в отдельном окне" if background else "с ожиданием результата"
        if not confirm(f"Запустить скрипт ({mode})?\n\n{p}", danger):
            return "Пользователь отклонил запуск."
        env = clean_env()

        if background:
            subprocess.Popen(
                cmd, cwd=WORKSPACE, env=env, creationflags=CREATE_NEW_CONSOLE,
                stdin=subprocess.DEVNULL,
            )
            return f"Запущено в фоне: {filename}"

        r = subprocess.run(
            cmd, cwd=WORKSPACE, capture_output=True, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=RUN_TIMEOUT, env=env, creationflags=CREATE_NO_WINDOW,
        )
        out = f"Код выхода: {r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        return out[:MAX_OUT]
    except subprocess.TimeoutExpired:
        return (f"Скрипт остановлен: превысил {RUN_TIMEOUT} секунд. "
                "Для долгих программ и окон используй background=true.")
    except Exception as e:
        return f"Ошибка: {e}"


TOOLS = [list_files, read_file, write_file, run_script]


# ---------- хранение истории ----------
def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = [
            types.Content(role=m["role"], parts=[types.Part(text=m["text"])])
            for m in data if m.get("role") in ("user", "model") and m.get("text")
        ]
    except Exception:
        return []
    items = items[-MAX_HISTORY:]
    while items and items[0].role != "user":
        items.pop(0)
    return items


def save_history(history):
    try:
        data = [{"role": c.role, "text": c.parts[0].text} for c in history]
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_FILE)
    except Exception as e:
        print(f"Не удалось сохранить историю: {e}")


class UniversalAIAgentApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Universal AI Agent")
        self.root.geometry("850x700")

        self.client = None
        self.history = load_history()
        self.init_error = None
        self.bad_models = set()
        self.status_text = ""
        self.status_since = 0.0
        self.status_countdown = None
        self.key_win = None

        self.config = load_config()
        self.auto_confirm = bool(self.config.get("auto_confirm", False))
        self.auto_var = tk.BooleanVar(value=self.auto_confirm)
        self.current_model = MODELS[0]
        self.usage_lock = threading.Lock()

        self.init_client()

        self.setup_ui()
        self.setup_clipboard()
        self.show_start()
        self.tick()

        if not self.client:
            # ключа нет: сразу открываем окно ввода
            self.root.after(400, self.open_key_dialog)

    # ---------- API-ключ ----------
    def get_api_key(self):
        key = str(self.config.get("api_key", "")).strip()
        return key or os.environ.get("GEMINI_API_KEY", "").strip()

    def init_client(self):
        key = self.get_api_key()
        self.client = None
        if not key:
            self.init_error = NO_KEY_MSG
            return
        try:
            self.client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=TIMEOUT_SEC * 1000),
            )
            self.init_error = None
        except Exception as e:
            self.init_error = f"Ошибка инициализации: {e}"

    def set_api_key(self, key):
        key = "".join(key.split())  # убираем пробелы и переводы строк при вставке
        with self.usage_lock:
            if key:
                self.config["api_key"] = key
            else:
                self.config.pop("api_key", None)
            save_config(self.config)
        self.init_client()
        if self.client:
            self.append_bot_msg("✅ API-ключ сохранён.")
        else:
            self.append_bot_msg(f"❌ {self.init_error}")

    def open_key_dialog(self):
        if self.key_win is not None and self.key_win.winfo_exists():
            self.key_win.lift()
            self.key_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.key_win = win
        win.title("API-ключ Gemini")
        win.resizable(False, False)
        win.transient(self.root)

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text="Вставьте API-ключ Gemini (правый клик или Ctrl+V):").pack(anchor="w")

        var = tk.StringVar(value=str(self.config.get("api_key", "")))
        entry = ttk.Entry(frm, textvariable=var, width=58, show="•")
        entry.pack(fill=tk.X, pady=(6, 4))
        self.add_context_menu(entry, readonly=False)

        show_var = tk.BooleanVar(value=False)

        def toggle():
            entry.config(show="" if show_var.get() else "•")

        ttk.Checkbutton(frm, text="Показать ключ", variable=show_var, command=toggle).pack(anchor="w")
        ttk.Button(
            frm, text="Где взять ключ", command=lambda: webbrowser.open(KEY_URL)
        ).pack(anchor="w", pady=(6, 0))

        def save():
            key = var.get().strip()
            if not key:
                messagebox.showwarning("Ключ", "Ключ пустой.", parent=win)
                return
            win.destroy()
            self.set_api_key(key)

        def remove():
            if messagebox.askyesno("Ключ", "Удалить сохранённый ключ?", parent=win):
                win.destroy()
                self.set_api_key("")

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="Сохранить", command=save).pack(side=tk.LEFT)
        ttk.Button(btns, text="Удалить ключ", command=remove).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side=tk.RIGHT)

        win.bind("<Return>", lambda e: save())
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 120
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        entry.focus_set()
        try:
            win.wait_visibility()
            win.grab_set()
        except tk.TclError:
            pass

    # ---------- счётчик запросов ----------
    def _usage(self):
        # вызывать под self.usage_lock
        day = quota_day()
        usage = self.config.get("usage")
        if not isinstance(usage, dict) or usage.get("date") != day:
            usage = {"date": day, "models": {}}
            self.config["usage"] = usage
        return usage

    def bump(self, model=None):
        model = model or self.current_model
        with self.usage_lock:
            usage = self._usage()
            usage["models"][model] = usage["models"].get(model, 0) + 1
            save_config(self.config)

    def usage_label(self):
        model = self.current_model
        with self.usage_lock:
            used = self._usage()["models"].get(model, 0)
        limit = DAILY_LIMITS.get(model, DEFAULT_DAILY_LIMIT)
        pct = used / limit * 100
        return f"{model}: {used}/{limit} за день ({pct:.1f}%)", pct

    def open_workspace(self):
        try:
            os.makedirs(WORKSPACE, exist_ok=True)
            os.startfile(WORKSPACE)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{e}")

    def show_start(self):
        if self.init_error:
            self.append_bot_msg(f"❌ {self.init_error}")
        if self.history:
            for c in self.history:
                if c.role == "user":
                    self.append_user_msg(c.parts[0].text)
                else:
                    self.append_bot_msg(c.parts[0].text)
            self.append_bot_msg(f"— история восстановлена ({len(self.history)} сообщ.) —")
        elif not self.init_error:
            self.append_bot_msg(f"Привет! Я могу создавать, читать и запускать файлы.\nРабочая папка: {WORKSPACE}")

    def setup_ui(self):
        top = ttk.Frame(self.root)
        top.pack(padx=10, pady=(10, 0), fill=tk.X)
        ttk.Checkbutton(
            top,
            text="Без всплывающих окон (запись и запуск файлов; опасный код всё равно спросит)",
            variable=self.auto_var,
            command=self.on_toggle_auto,
        ).pack(side=tk.LEFT)

        info = ttk.Frame(self.root)
        info.pack(padx=10, pady=(5, 0), fill=tk.X)
        ttk.Button(info, text="Ввести ключ", command=self.open_key_dialog).pack(side=tk.LEFT)
        ttk.Button(info, text="Получить ключ", command=lambda: webbrowser.open(KEY_URL)).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(info, text="Квоты", command=lambda: webbrowser.open(QUOTA_URL)).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(info, text="Папка", command=self.open_workspace).pack(side=tk.LEFT, padx=(5, 10))
        self.usage_lbl = tk.Label(info, text="", font=("Segoe UI", 10))
        self.usage_lbl.pack(side=tk.LEFT)

        self.chat_area = scrolledtext.ScrolledText(
            self.root, wrap=tk.WORD, state="disabled", font=("Segoe UI", 10)
        )
        self.chat_area.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)
        self.chat_area.tag_config("user", foreground="#0055ff", font=("Segoe UI", 10, "bold"))
        self.chat_area.tag_config("bot", foreground="#1b1b1b")

        self.status = ttk.Label(self.root, text="", font=("Segoe UI", 10))
        self.status.pack(padx=10, anchor="w")

        frame = ttk.Frame(self.root)
        frame.pack(padx=10, pady=(0, 10), fill=tk.X)

        self.entry_msg = ttk.Entry(frame, font=("Segoe UI", 11))
        self.entry_msg.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.entry_msg.bind("<Return>", lambda e: self.send_message())
        self.entry_msg.focus()

        self.btn_clear = ttk.Button(frame, text="Очистить", command=self.clear_history)
        self.btn_clear.pack(side=tk.RIGHT, padx=(5, 0))
        self.btn_send = ttk.Button(frame, text="Отправить", command=self.send_message)
        self.btn_send.pack(side=tk.RIGHT)

    def on_toggle_auto(self):
        # обычный bool читается из рабочего потока безопасно
        self.auto_confirm = self.auto_var.get()
        with self.usage_lock:
            self.config["auto_confirm"] = self.auto_confirm
            save_config(self.config)

    # ---------- буфер обмена ----------
    def setup_clipboard(self):
        self.add_context_menu(self.chat_area, readonly=True)
        self.add_context_menu(self.entry_msg, readonly=False)
        self.chat_area.bind("<Button-1>", lambda e: self.chat_area.focus_set(), add="+")
        # Ctrl+C/V/X/A на любой раскладке (по коду клавиши)
        self.root.bind_all("<Control-KeyPress>", self.on_ctrl_key)

    def add_context_menu(self, widget, readonly):
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Копировать", command=lambda: self.do_copy(widget))
        if not readonly:
            menu.add_command(label="Вырезать", command=lambda: self.do_cut(widget))
            menu.add_command(label="Вставить", command=lambda: self.do_paste(widget))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: self.do_select_all(widget))

        def popup(event):
            widget.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", popup)

    def get_selection(self, widget):
        try:
            if isinstance(widget, tk.Text):
                return widget.get(tk.SEL_FIRST, tk.SEL_LAST)
            if widget.selection_present():
                return widget.selection_get()
        except tk.TclError:
            pass
        return ""

    def do_copy(self, widget):
        text = self.get_selection(widget)
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        return "break"

    def do_cut(self, widget):
        if isinstance(widget, tk.Text):
            return "break"
        text = self.get_selection(widget)
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            widget.delete(widget.index(tk.SEL_FIRST), widget.index(tk.SEL_LAST))
        return "break"

    def do_paste(self, widget):
        if isinstance(widget, tk.Text):
            return "break"
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        if widget.selection_present():
            widget.delete(widget.index(tk.SEL_FIRST), widget.index(tk.SEL_LAST))
        widget.insert(widget.index(tk.INSERT), text)
        return "break"

    def do_select_all(self, widget):
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
        else:
            widget.select_range(0, tk.END)
            widget.icursor(tk.END)
        return "break"

    def on_ctrl_key(self, event):
        # Латинские буквы обрабатываются Tk сами — не дублируем
        if event.keysym.lower() in ("c", "v", "x", "a"):
            return None
        widget = self.root.focus_get()
        if widget is None or not isinstance(widget, (tk.Text, tk.Entry, ttk.Entry)):
            return None
        actions = {67: self.do_copy, 86: self.do_paste, 88: self.do_cut, 65: self.do_select_all}
        action = actions.get(event.keycode)
        if action:
            return action(widget)
        return None

    def clear_history(self):
        if str(self.btn_send["state"]) == "disabled":
            return
        if not messagebox.askyesno("Очистить", "Удалить всю историю общения?"):
            return
        self.history = []
        try:
            os.remove(HISTORY_FILE)
        except OSError:
            pass
        self.chat_area.config(state="normal")
        self.chat_area.delete("1.0", tk.END)
        self.chat_area.config(state="disabled")
        self.append_bot_msg("История очищена.")

    def set_status(self, text, countdown=None):
        self.status_text = text
        self.status_since = time.time()
        self.status_countdown = countdown

    def tick(self):
        if self.status_text:
            elapsed = int(time.time() - self.status_since)
            if self.status_countdown is None:
                label = f"{self.status_text} ({elapsed} с)"
            else:
                left = max(0, math.ceil(self.status_countdown - (time.time() - self.status_since)))
                label = f"{self.status_text}. Повтор через {left} с"
            self.status.config(text=label)
        else:
            self.status.config(text="")

        text, pct = self.usage_label()
        color = "#c00000" if pct >= 90 else ("#b36b00" if pct >= 70 else "#1b1b1b")
        self.usage_lbl.config(text=text, fg=color)

        self.root.after(200, self.tick)

    def pause(self, seconds, text):
        self.set_status(text, countdown=seconds)
        time.sleep(seconds)

    def _append(self, prefix, text, tag):
        self.chat_area.config(state="normal")
        self.chat_area.insert(tk.END, f"{prefix}: {text}\n\n", tag)
        self.chat_area.see(tk.END)
        self.chat_area.config(state="disabled")

    def append_user_msg(self, text):
        self._append("Вы", text, "user")

    def append_bot_msg(self, text):
        self._append("AI Агент", text, "bot")

    def send_message(self):
        query = self.entry_msg.get().strip()
        if not query or str(self.btn_send["state"]) == "disabled":
            return
        if not self.client:
            self.append_bot_msg(f"❌ {self.init_error or NO_KEY_MSG}")
            self.open_key_dialog()
            return
        self.entry_msg.delete(0, tk.END)
        self.append_user_msg(query)
        self.btn_send.config(state="disabled")
        threading.Thread(target=self.process_task, args=(query,), daemon=True).start()

    @staticmethod
    def clean(text):
        return text.replace("**", "").replace("__", "")

    def make_config(self, thinking_off):
        kw = dict(tools=TOOLS, system_instruction=SYSTEM)
        if thinking_off:
            kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**kw)

    def remember(self, contents, text):
        hist = contents + [types.Content(role="model", parts=[types.Part(text=text)])]
        hist = hist[-MAX_HISTORY:]
        while hist and hist[0].role != "user":
            hist.pop(0)
        self.history = hist
        save_history(hist)

    def ask(self, query):
        contents = self.history + [
            types.Content(role="user", parts=[types.Part(text=query)])
        ]
        last_err = None
        models = [m for m in MODELS if m not in self.bad_models] or MODELS

        for model in models:
            thinking_off = True
            attempt = 0
            while attempt < ATTEMPTS:
                try:
                    self.current_model = model
                    self.set_status(f"Запрос к {model}")
                    self.bump(model)
                    r = self.client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=self.make_config(thinking_off),
                    )
                    text = self.clean(r.text or "(пустой ответ)")
                    self.remember(contents, text)
                    if model != models[0]:
                        text += f"\n\n[ответила резервная модель: {model}]"
                    return text
                except Exception as e:
                    err = str(e)
                    last_err = e
                    if thinking_off and ("thinking" in err.lower() or "400" in err or "INVALID_ARGUMENT" in err):
                        thinking_off = False
                        continue
                    if "404" in err or "NOT_FOUND" in err:
                        self.bad_models.add(model)
                        break
                    if any(t in err for t in TRANSIENT):
                        attempt += 1
                        if attempt < ATTEMPTS:
                            self.pause(2, f"{model} занята")
                        continue
                    raise
            if model != models[-1]:
                self.pause(1, f"{model} недоступна, переключаюсь")
        raise last_err

    def process_task(self, query):
        if not self.client:
            self.root.after(0, self.finish, f"❌ {self.init_error or NO_KEY_MSG}")
            return
        try:
            text = self.ask(query)
        except Exception as e:
            text = f"⚠️ Ошибка связи с сервером: {e}"
            if "API key" in str(e) or "API_KEY_INVALID" in str(e):
                text += "\n\nПохоже, ключ неверный. Нажмите «Ввести ключ» и замените его."
        self.root.after(0, self.finish, text)

    def finish(self, text):
        self.status_text = ""
        self.append_bot_msg(text)
        self.btn_send.config(state="normal")


if __name__ == "__main__":
    root = tk.Tk()
    app = UniversalAIAgentApp(root)
    APP = app
    root.mainloop()