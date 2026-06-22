#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LASER MAZE - CONTROL REMOTO v2.1
================================
Controla el software del juego (laser_maze_v3.2.py) desde otro ordenador
conectado a la misma red WiFi.

Pestaña PARTIDA:
  - Cola de jugadores, iniciar partida y estado en tiempo real.
Pestaña CONFIGURACIÓN:
  - Valores de partida (tiempo, puntos, penalizaciones)
  - Niveles 1/2/3 y modo operador
  - Sonidos (activar/desactivar y volumen)
  - Reset de rankings
  - Puerto serie del Arduino

Funciona en Windows y Mac con Python 3 (no necesita instalar nada más).
"""

import json
import os
import queue
import socket
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# ----------------------------
# Apariencia (misma paleta que el juego)
# ----------------------------
BG_COLOR = "#181a20"
CARD_COLOR = "#232733"
FIELD_BG = "#11141a"
ACCENT = "#39FF14"        # verde flúor del juego
ACCENT_DARK = "#2bcc0f"
TEXT_COLOR = "#e8e8e8"
MUTED = "#9aa0ad"
RED = "#ff4444"
RED_BTN = "#b91c1c"
ORANGE = "#ffaa00"
CYAN = "#00cfff"
GRAY_BTN = "#3a4150"
BLUE_BTN = "#2d6cdf"
BORDER = "#333a47"

FONT = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"

DEFAULT_PORT = 5555


def _app_dir() -> str:
    """Carpeta donde guardar la config.

    - Como script .py: la carpeta del propio script.
    - Como .exe (PyInstaller --onefile): la carpeta del .exe, NO la temporal
      _MEIPASS (que se borra al cerrar), para que la IP se recuerde.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(_app_dir(), "control_remoto_config.json")

SOUND_LABELS = [
    ("music_game",          "Música juego"),
    ("laser_hit",           "FX Láser"),
    ("game_finished",       "FX Fin (con CP)"),
    ("stop_no_checkpoint",  "FX Stop sin CP"),
    ("max_lasers_gameover", "FX GameOver Máx Láser"),
]


def format_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_score(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def shade(hex_color: str, factor: float) -> str:
    """Oscurece/aclara un color #rrggbb."""
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        r = max(0, min(255, int(r * factor)))
        g = max(0, min(255, int(g * factor)))
        b = max(0, min(255, int(b * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


class FlatButton(tk.Label):
    """Botón plano basado en Label: se ve igual en Windows, Mac y Linux
    (los tk.Button de Mac ignoran los colores y el texto sale invisible)."""

    def __init__(self, parent, text, command=None, bg=BLUE_BTN, fg="white",
                 font=None, padx=16, pady=8, width=None, hover=None):
        super().__init__(
            parent, text=text, bg=bg, fg=fg,
            font=font or (FONT, 11, "bold"),
            padx=padx, pady=pady, cursor="hand2", width=width,
        )
        self._bg = bg
        self._hover = hover or shade(bg, 0.82)
        self._command = command
        self.bind("<Enter>", lambda e: self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg))
        self.bind("<Button-1>", self._on_click)

    def _on_click(self, _event):
        if self._command:
            self._command()

    def set_colors(self, bg, fg):
        self._bg = bg
        self._hover = shade(bg, 0.82)
        self.config(bg=bg, fg=fg)


# ----------------------------
# Cliente de red (hilo lector)
# ----------------------------
class GameClient:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.incoming = queue.Queue()   # mensajes JSON recibidos
        self._lock = threading.Lock()

    def connect(self, host: str, port: int, timeout=4.0):
        self.disconnect()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.settimeout(None)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self.sock = s
        self.connected = True
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()

    def _reader(self, s):
        buf = b""
        while True:
            try:
                data = s.recv(4096)
            except Exception:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                    self.incoming.put(msg)
                except Exception:
                    pass
        # conexión perdida
        if self.sock is s:
            self.connected = False
            self.incoming.put({"type": "__disconnected__"})

    def send(self, payload: dict) -> bool:
        if not self.connected or self.sock is None:
            return False
        try:
            data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            with self._lock:
                self.sock.sendall(data)
            return True
        except Exception:
            self.connected = False
            self.incoming.put({"type": "__disconnected__"})
            return False

    def disconnect(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None


# ----------------------------
# Aplicación
# ----------------------------
class RemoteApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Laser Maze - Control Remoto")
        self.configure(bg=BG_COLOR)
        self.geometry("860x800")
        self.minsize(700, 640)

        self.client = GameClient()
        self.last_state = None
        self._loading_config = False   # evita re-enviar lo que llega del juego
        self.sound_rows = {}
        self._last_lasers = 0
        self._laser_after = None
        self._laser_times = []      # instantes de los últimos toques de láser
        self._laser_alarm = False   # 10 toques seguidos con la misma cadencia
        self._alarm_interval = 0.0  # cadencia detectada (segundos)
        self._alarm_watchdog = None # temporizador que apaga la alarma

        self._setup_styles()
        self._build_ui()
        self._load_config()
        self.after(100, self._poll_incoming)
        # Conexión automática al abrir (con la última IP usada)
        self.after(400, self._auto_connect)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook", background=BG_COLOR, borderwidth=0,
                        tabmargins=[0, 0, 0, 0])
        style.configure(
            "TNotebook.Tab", background=CARD_COLOR, foreground=MUTED,
            font=(FONT, 12, "bold"), padding=[22, 9], borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#000000")],
            padding=[("selected", [26, 12])],
            expand=[("selected", [0, 0, 0, 0])],
        )
        # Tablas de ranking
        style.configure(
            "Treeview", background=FIELD_BG, fieldbackground=FIELD_BG,
            foreground=TEXT_COLOR, rowheight=26, borderwidth=0,
            font=(FONT, 11),
        )
        style.configure(
            "Treeview.Heading", background=ACCENT, foreground="#000000",
            font=(FONT, 10, "bold"), relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#000000")],
        )
        style.map("Treeview.Heading", background=[("active", ACCENT)])
        style.configure(
            "Dark.Horizontal.TScale", background=CARD_COLOR,
            troughcolor=FIELD_BG, bordercolor=CARD_COLOR,
            lightcolor=ACCENT, darkcolor=ACCENT, arrowcolor=ACCENT,
        )
        style.configure(
            "TCombobox", fieldbackground=FIELD_BG, background=GRAY_BTN,
            foreground=TEXT_COLOR, arrowcolor=TEXT_COLOR,
            bordercolor=BORDER, lightcolor=CARD_COLOR, darkcolor=CARD_COLOR,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", FIELD_BG)],
            foreground=[("readonly", TEXT_COLOR)],
        )
        style.configure(
            "Vertical.TScrollbar", background=GRAY_BTN, troughcolor=BG_COLOR,
            bordercolor=BG_COLOR, arrowcolor=TEXT_COLOR,
        )
        # Desplegable del combobox oscuro
        self.option_add("*TCombobox*Listbox.background", FIELD_BG)
        self.option_add("*TCombobox*Listbox.foreground", TEXT_COLOR)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#000000")

    # ---- helpers de construcción ----
    def _card(self, parent, title):
        """Tarjeta con barra de título verde flúor, como en el juego."""
        outer = tk.Frame(parent, bg=CARD_COLOR,
                         highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="x", pady=(0, 12))
        bar = tk.Frame(outer, bg=ACCENT)
        bar.pack(fill="x")
        tk.Label(bar, text=title, font=(FONT, 12, "bold"),
                 bg=ACCENT, fg="#000000").pack(pady=5)
        body = tk.Frame(outer, bg=CARD_COLOR)
        body.pack(fill="x", padx=16, pady=12)
        return body

    def _entry(self, parent, var, width=10, justify="center", font_size=13):
        return tk.Entry(
            parent, textvariable=var, width=width, font=(FONT, font_size),
            bg=FIELD_BG, fg=ACCENT, insertbackground=ACCENT,
            relief="flat", justify=justify,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )

    # ================= UI =================
    def _build_ui(self):
        # Cabecera
        header = tk.Frame(self, bg=BG_COLOR)
        header.pack(fill="x", padx=24, pady=(18, 8))
        tk.Label(header, text="LASER MAZE", font=(FONT, 24, "bold"),
                 fg=ACCENT, bg=BG_COLOR).pack(side="left")
        tk.Label(header, text="  CONTROL REMOTO", font=(FONT, 24),
                 fg=TEXT_COLOR, bg=BG_COLOR).pack(side="left")

        # --- Conexión ---
        conn_outer = tk.Frame(self, bg=CARD_COLOR,
                              highlightbackground=BORDER, highlightthickness=1)
        conn_outer.pack(fill="x", padx=24, pady=(0, 10))
        conn = tk.Frame(conn_outer, bg=CARD_COLOR)
        conn.pack(fill="x", padx=12, pady=10)

        tk.Label(conn, text="IP del juego:", font=(FONT, 11),
                 fg=MUTED, bg=CARD_COLOR).pack(side="left", padx=(0, 6))
        self.ip_var = tk.StringVar()
        self._entry(conn, self.ip_var, width=15, justify="left",
                    font_size=12).pack(side="left", ipady=4)

        tk.Label(conn, text="Puerto:", font=(FONT, 11),
                 fg=MUTED, bg=CARD_COLOR).pack(side="left", padx=(15, 6))
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self._entry(conn, self.port_var, width=6,
                    font_size=12).pack(side="left", ipady=4)

        self.btn_connect = FlatButton(conn, "CONECTAR",
                                      command=self.toggle_connection,
                                      bg=BLUE_BTN, fg="white")
        self.btn_connect.pack(side="left", padx=16)

        self.conn_dot = tk.Label(conn, text="●", font=(FONT, 14, "bold"),
                                 fg=RED, bg=CARD_COLOR)
        self.conn_dot.pack(side="left")
        self.conn_label = tk.Label(conn, text="Desconectado", font=(FONT, 11),
                                   fg=MUTED, bg=CARD_COLOR)
        self.conn_label.pack(side="left", padx=(5, 0))

        # --- Pestañas ---
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=24, pady=(0, 18))

        self.tab_game = tk.Frame(self.notebook, bg=BG_COLOR)
        self.tab_ranking = tk.Frame(self.notebook, bg=BG_COLOR)
        self.tab_history = tk.Frame(self.notebook, bg=BG_COLOR)
        self.tab_config = tk.Frame(self.notebook, bg=BG_COLOR)
        self.notebook.add(self.tab_game, text="  PARTIDA  ")
        self.notebook.add(self.tab_ranking, text="  RANKINGS  ")
        self.notebook.add(self.tab_history, text="  HISTORIAL  ")
        self.notebook.add(self.tab_config, text="  CONFIGURACIÓN  ")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_game_tab()
        self._build_ranking_tab()
        self._build_history_tab()
        self._build_config_tab()

    def _on_tab_changed(self, _event=None):
        # Al entrar en RANKINGS o HISTORIAL, pedir datos frescos
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return
        if (current in (self.tab_ranking, self.tab_history)
                and self.client.connected):
            self.client.send({"cmd": "get_ranking"})

    # ---------- Pestaña PARTIDA ----------
    def _build_game_tab(self):
        root = tk.Frame(self.tab_game, bg=BG_COLOR)
        root.pack(fill="both", expand=True, padx=2, pady=12)

        # Estado
        estado_body = self._card(root, "ESTADO DE LA PARTIDA")

        self.estado_label = tk.Label(estado_body, text="— SIN CONEXIÓN —",
                                     font=(FONT, 26, "bold"), fg=MUTED,
                                     bg=CARD_COLOR)
        self.estado_label.pack(pady=(2, 0))

        self.jugador_label = tk.Label(estado_body, text="", font=(FONT, 15),
                                      fg=TEXT_COLOR, bg=CARD_COLOR)
        self.jugador_label.pack(pady=(0, 10))

        datos = tk.Frame(estado_body, bg=CARD_COLOR)
        datos.pack()

        self.tiempo_value = self._make_stat(datos, 0, "TIEMPO", "00:00", CYAN)
        self.puntos_value = self._make_stat(datos, 1, "PUNTOS", "0", ACCENT)
        self.lasers_value = self._make_stat(datos, 2, "LÁSERES", "0", RED)

        # Indicadores: niveles 1/2/3 y aviso de láser tocado
        extra = tk.Frame(estado_body, bg=CARD_COLOR)
        extra.pack(pady=(12, 0))

        self.level_dots = {}
        for i, (key, label) in enumerate([("LEVEL1", "NIVEL 1"),
                                          ("LEVEL2", "NIVEL 2"),
                                          ("LEVEL3", "NIVEL 3")]):
            f = tk.Frame(extra, bg=CARD_COLOR)
            f.grid(row=0, column=i, padx=14)
            dot = tk.Label(f, text="●", font=(FONT, 15, "bold"),
                           fg=GRAY_BTN, bg=CARD_COLOR)
            dot.pack(side="left")
            txt = tk.Label(f, text=f"{label} OFF", font=(FONT, 11, "bold"),
                           fg=MUTED, bg=CARD_COLOR)
            txt.pack(side="left", padx=(5, 0))
            self.level_dots[key] = {"dot": dot, "txt": txt, "label": label}

        # Bolita verde cuando se toca un láser (1 segundo apagado).
        # Si se detectan 10 toques seguidos con la misma cadencia -> alarma roja.
        lf = tk.Frame(extra, bg=CARD_COLOR)
        lf.grid(row=0, column=3, padx=(30, 0))
        self.laser_dot = tk.Label(lf, text="●", font=(FONT, 20, "bold"),
                                  fg=CARD_COLOR, bg=CARD_COLOR)
        self.laser_dot.pack(side="left")
        self.laser_txt = tk.Label(lf, text="LÁSER TOCADO", font=(FONT, 11, "bold"),
                                  fg=MUTED, bg=CARD_COLOR)
        self.laser_txt.pack(side="left", padx=(5, 0))

        # Botón parar partida (solo visible con partida en curso)
        self.btn_stop = FlatButton(
            estado_body, "■   PARAR PARTIDA",
            command=self.stop_game,
            bg=RED_BTN, fg="white", font=(FONT, 13, "bold"), pady=10,
        )

        # Cola
        cola_outer = tk.Frame(root, bg=CARD_COLOR,
                              highlightbackground=BORDER, highlightthickness=1)
        cola_outer.pack(fill="both", expand=True, pady=(0, 12))
        bar = tk.Frame(cola_outer, bg=ACCENT)
        bar.pack(fill="x")
        tk.Label(bar, text="COLA DE JUGADORES (SIGUIENTE RONDA)",
                 font=(FONT, 12, "bold"), bg=ACCENT, fg="#000000").pack(pady=5)
        cola_body = tk.Frame(cola_outer, bg=CARD_COLOR)
        cola_body.pack(fill="both", expand=True, padx=16, pady=12)

        add_row = tk.Frame(cola_body, bg=CARD_COLOR)
        add_row.pack(fill="x")

        self.name_var = tk.StringVar()
        name_entry = self._entry(add_row, self.name_var, width=10,
                                 justify="left", font_size=15)
        name_entry.pack(side="left", fill="x", expand=True, ipady=7)
        name_entry.bind("<Return>", lambda e: self.add_player())

        FlatButton(add_row, "+ AÑADIR A COLA", command=self.add_player,
                   bg=ORANGE, fg="#000000").pack(side="left", padx=(10, 0))

        list_row = tk.Frame(cola_body, bg=CARD_COLOR)
        list_row.pack(fill="both", expand=True, pady=(10, 0))

        self.queue_list = tk.Listbox(
            list_row, font=(FONT, 15), bg=FIELD_BG, fg=TEXT_COLOR,
            selectbackground=ACCENT, selectforeground="#000000",
            relief="flat", activestyle="none", exportselection=False,
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.queue_list.pack(side="left", fill="both", expand=True)
        self.queue_list.bind("<<ListboxSelect>>", self._on_queue_select)

        btns = tk.Frame(list_row, bg=CARD_COLOR)
        btns.pack(side="left", fill="y", padx=(10, 0))

        # Botón "iniciar" del jugador seleccionado (aparece al seleccionar)
        self.btn_start_sel = FlatButton(
            btns, "▶ INICIAR", command=self.start_selected,
            bg=ACCENT, fg="#000000", font=(FONT, 11, "bold"),
            padx=12, pady=10, width=14,
        )
        self.btn_start_sel.config(wraplength=130, justify="center")

        self.btn_quitar = FlatButton(
            btns, "Quitar", command=self.remove_selected,
            bg=GRAY_BTN, fg="white", font=(FONT, 10, "bold"),
            padx=12, pady=6, width=14,
        )
        self.btn_quitar.pack(pady=(0, 6))
        FlatButton(btns, "Vaciar cola", command=self.clear_queue,
                   bg=GRAY_BTN, fg="white", font=(FONT, 10, "bold"),
                   padx=12, pady=6, width=14).pack()

    def _make_stat(self, parent, col, title, value, color):
        f = tk.Frame(parent, bg=FIELD_BG, padx=30, pady=10,
                     highlightbackground=BORDER, highlightthickness=1)
        f.grid(row=0, column=col, padx=10)
        tk.Label(f, text=title, font=(FONT, 9, "bold"), fg=MUTED,
                 bg=FIELD_BG).pack()
        lbl = tk.Label(f, text=value, font=(FONT, 24, "bold"), fg=color,
                       bg=FIELD_BG)
        lbl.pack()
        return lbl

    # ---------- Pestaña RANKINGS ----------
    def _build_ranking_tab(self):
        root = tk.Frame(self.tab_ranking, bg=BG_COLOR)
        root.pack(fill="both", expand=True, padx=2, pady=12)

        cols = tk.Frame(root, bg=BG_COLOR)
        cols.pack(fill="both", expand=True)
        for i in range(3):
            cols.columnconfigure(i, weight=1, uniform="rk")
        cols.rowconfigure(0, weight=1)

        self.ranking_trees = {}
        titles = [("daily", "HOY"), ("monthly", "ESTE MES"),
                  ("alltime", "HISTÓRICO")]
        for i, (key, title) in enumerate(titles):
            outer = tk.Frame(cols, bg=CARD_COLOR,
                             highlightbackground=BORDER, highlightthickness=1)
            outer.grid(row=0, column=i, sticky="nsew",
                       padx=(0 if i == 0 else 10, 0))
            bar = tk.Frame(outer, bg=ACCENT)
            bar.pack(fill="x")
            tk.Label(bar, text=title, font=(FONT, 12, "bold"),
                     bg=ACCENT, fg="#000000").pack(pady=5)

            body = tk.Frame(outer, bg=CARD_COLOR)
            body.pack(fill="both", expand=True, padx=8, pady=8)

            tree = ttk.Treeview(
                body, columns=("pos", "player", "score"),
                show="headings", height=12,
            )
            tree.heading("pos", text="#")
            tree.heading("player", text="JUGADOR")
            tree.heading("score", text="PUNTOS")
            tree.column("pos", width=34, anchor="center", stretch=False)
            tree.column("player", width=110, anchor="w")
            tree.column("score", width=80, anchor="e", stretch=False)
            tree.pack(fill="both", expand=True)
            self.ranking_trees[key] = tree

        tk.Label(root,
                 text="Se actualizan automáticamente al terminar cada partida.",
                 font=(FONT, 10), fg=MUTED, bg=BG_COLOR).pack(pady=(8, 0))

    def _render_ranking(self, msg: dict):
        for key, tree in self.ranking_trees.items():
            tree.delete(*tree.get_children())
            for i, rec in enumerate(msg.get(key, []), start=1):
                tree.insert("", "end", values=(
                    i, rec.get("player", ""),
                    format_score(rec.get("score", 0)),
                ))

    # ---------- Pestaña HISTORIAL ----------
    def _build_history_tab(self):
        root = tk.Frame(self.tab_history, bg=BG_COLOR)
        root.pack(fill="both", expand=True, padx=2, pady=12)

        outer = tk.Frame(root, bg=CARD_COLOR,
                         highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="both", expand=True)
        bar = tk.Frame(outer, bg=ACCENT)
        bar.pack(fill="x")
        tk.Label(bar, text="HISTÓRICO DE PARTIDAS", font=(FONT, 12, "bold"),
                 bg=ACCENT, fg="#000000").pack(pady=5)

        body = tk.Frame(outer, bg=CARD_COLOR)
        body.pack(fill="both", expand=True, padx=8, pady=8)

        cols = ("fecha", "hora", "jugador", "tiempo", "lasers", "puntos")
        self.history_tree = ttk.Treeview(body, columns=cols,
                                         show="headings", height=18)
        headers = [("fecha", "DÍA", 95, "center"),
                   ("hora", "HORA", 75, "center"),
                   ("jugador", "JUGADOR", 160, "w"),
                   ("tiempo", "TIEMPO", 80, "center"),
                   ("lasers", "LÁSERES", 80, "center"),
                   ("puntos", "PUNTOS", 90, "e")]
        for key, txt, width, anchor in headers:
            self.history_tree.heading(key, text=txt)
            stretch = key == "jugador"
            self.history_tree.column(key, width=width, anchor=anchor,
                                     stretch=stretch)

        scroll = ttk.Scrollbar(body, orient="vertical",
                               command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.history_tree.pack(side="left", fill="both", expand=True)

        tk.Label(root,
                 text="Tiempo = tiempo jugado. "
                      "Se muestran las últimas 200 partidas.",
                 font=(FONT, 10), fg=MUTED, bg=BG_COLOR).pack(pady=(8, 0))

    def _render_history(self, msg: dict):
        self.history_tree.delete(*self.history_tree.get_children())
        for rec in msg.get("history", []):
            self.history_tree.insert("", "end", values=(
                rec.get("fecha", ""),
                rec.get("hora", ""),
                rec.get("player", ""),
                format_time(rec.get("time_played", rec.get("time_left", 0))),
                rec.get("lasers", 0),
                format_score(rec.get("score", 0)),
            ))

    # ---------- Pestaña CONFIGURACIÓN ----------
    def _build_config_tab(self):
        canvas = tk.Canvas(self.tab_config, bg=BG_COLOR, highlightthickness=0)
        scroll = ttk.Scrollbar(self.tab_config, orient="vertical",
                               command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        root = tk.Frame(canvas, bg=BG_COLOR)
        win = canvas.create_window((0, 0), window=root, anchor="nw")
        root.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        # Rueda del ratón (Windows/Mac/Linux)
        def _on_wheel(event):
            if event.num == 4:
                canvas.yview_scroll(-2, "units")
            elif event.num == 5:
                canvas.yview_scroll(2, "units")
            else:
                step = -1 if event.delta > 0 else 1
                canvas.yview_scroll(step * 2, "units")

        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.bind_all(seq, _on_wheel)

        inner = tk.Frame(root, bg=BG_COLOR)
        inner.pack(fill="both", expand=True, padx=2, pady=12)

        # --- Valores de partida ---
        c1 = self._card(inner, "VALORES DE PARTIDA")
        grid = tk.Frame(c1, bg=CARD_COLOR)
        grid.pack(anchor="w")

        self.cfg_vars = {}
        self.cfg_entries = {}
        fields = [
            ("initial_time", "Tiempo inicial (segundos):"),
            ("initial_points", "Puntos iniciales:"),
            ("time_penalty", "Penalización por segundo:"),
            ("laser_penalty", "Penalización por láser:"),
        ]
        for i, (key, label) in enumerate(fields):
            tk.Label(grid, text=label, font=(FONT, 13), fg=TEXT_COLOR,
                     bg=CARD_COLOR, anchor="e").grid(
                row=i, column=0, sticky="e", padx=(0, 10), pady=6)
            var = tk.StringVar()
            ent = self._entry(grid, var, width=9)
            ent.grid(row=i, column=1, sticky="w", pady=6, ipady=4)
            self.cfg_vars[key] = var
            self.cfg_entries[key] = ent

        FlatButton(c1, "GUARDAR VALORES", command=self.save_game_values,
                   bg=BLUE_BTN, fg="white").pack(anchor="w", pady=(10, 0))

        # --- Niveles y modo ---
        c2 = self._card(inner, "NIVELES Y MODO OPERADOR")
        lv_row = tk.Frame(c2, bg=CARD_COLOR)
        lv_row.pack(anchor="w")

        self.level_btns = {}
        for i, (key, label) in enumerate([("LEVEL1", "NIVEL 1"),
                                          ("LEVEL2", "NIVEL 2"),
                                          ("LEVEL3", "NIVEL 3"),
                                          ("MODO", "MODO OPERADOR")]):
            b = FlatButton(lv_row, f"{label}\nOFF",
                           command=lambda k=key: self.toggle_level(k),
                           bg=GRAY_BTN, fg=MUTED,
                           font=(FONT, 11, "bold"), width=14, pady=10)
            b.grid(row=0, column=i, padx=(0, 10))
            self.level_btns[key] = {"btn": b, "on": False, "label": label}

        # --- Sonidos ---
        c3 = self._card(inner, "SONIDOS")
        s_grid = tk.Frame(c3, bg=CARD_COLOR)
        s_grid.pack(fill="x")
        s_grid.columnconfigure(2, weight=1)

        for i, (key, label) in enumerate(SOUND_LABELS):
            tk.Label(s_grid, text=label, font=(FONT, 12, "bold"),
                     fg=TEXT_COLOR, bg=CARD_COLOR, anchor="w").grid(
                row=i, column=0, sticky="w", pady=5, padx=(0, 10))

            tgl = FlatButton(s_grid, "OFF",
                             command=lambda k=key: self.toggle_sound(k),
                             bg=GRAY_BTN, fg=MUTED,
                             font=(FONT, 10, "bold"), width=4,
                             padx=8, pady=4)
            tgl.grid(row=i, column=1, padx=4)

            name_lbl = tk.Label(s_grid, text="—", font=(FONT, 11), fg=MUTED,
                                bg=CARD_COLOR, anchor="w")
            name_lbl.grid(row=i, column=2, sticky="w", padx=10)

            vol_var = tk.DoubleVar(value=80.0)
            pct_lbl = tk.Label(s_grid, text="80%", font=(FONT, 11, "bold"),
                               fg=ACCENT, bg=CARD_COLOR, width=5)

            scale = ttk.Scale(
                s_grid, from_=0, to=100, orient="horizontal",
                variable=vol_var, length=170, style="Dark.Horizontal.TScale",
                command=lambda v, l=pct_lbl: l.config(
                    text=f"{int(float(v))}%"),
            )
            scale.grid(row=i, column=3, padx=(6, 4))
            pct_lbl.grid(row=i, column=4)
            scale.bind("<ButtonRelease-1>", lambda e, k=key: self.send_sound(k))

            self.sound_rows[key] = {"on": False, "toggle": tgl,
                                    "vol": vol_var, "name": name_lbl,
                                    "pct": pct_lbl}

        tk.Label(c3, text="El archivo de audio se elige en el ordenador del juego.",
                 font=(FONT, 10), fg=MUTED, bg=CARD_COLOR
                 ).pack(anchor="w", pady=(8, 0))

        # --- Rankings ---
        c4 = self._card(inner, "RESET DE RANKINGS")
        r_row = tk.Frame(c4, bg=CARD_COLOR)
        r_row.pack(anchor="w")

        resets = [("daily", "Reset HOY", GRAY_BTN, "white"),
                  ("monthly", "Reset ESTE MES", GRAY_BTN, "white"),
                  ("alltime", "Reset HISTÓRICO", GRAY_BTN, "white"),
                  ("all", "Reset los 3", RED_BTN, "white")]
        for i, (which, label, bg, fg) in enumerate(resets):
            FlatButton(r_row, label, bg=bg, fg=fg,
                       font=(FONT, 10, "bold"), padx=14, pady=7,
                       command=lambda w=which, l=label: self.reset_ranking(w, l)
                       ).grid(row=0, column=i, padx=(0, 10))

        # --- Puerto serie ---
        c5 = self._card(inner, "PUERTO SERIE (ARDUINO DEL JUEGO)")
        p_row = tk.Frame(c5, bg=CARD_COLOR)
        p_row.pack(anchor="w")

        self.serial_var = tk.StringVar(value="Auto (detectar)")
        self.port_combo = ttk.Combobox(p_row, textvariable=self.serial_var,
                                       font=(FONT, 11), width=22,
                                       state="readonly")
        self.port_combo.grid(row=0, column=0, padx=(0, 12), ipady=3)

        FlatButton(p_row, "APLICAR PUERTO", command=self.apply_serial,
                   bg=BLUE_BTN, fg="white", font=(FONT, 10, "bold"),
                   padx=14, pady=7).grid(row=0, column=1, padx=(0, 12))

        self.serial_state_lbl = tk.Label(p_row, text="", font=(FONT, 11, "bold"),
                                         fg=MUTED, bg=CARD_COLOR)
        self.serial_state_lbl.grid(row=0, column=2)

    # ================= Config local del remoto =================
    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.ip_var.set(cfg.get("ip", ""))
            self.port_var.set(str(cfg.get("port", DEFAULT_PORT)))
        except Exception:
            pass

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"ip": self.ip_var.get().strip(),
                     "port": int(self.port_var.get() or DEFAULT_PORT)}, f,
                )
        except Exception:
            pass

    # ================= Conexión =================
    def _auto_connect(self):
        """Al abrir, intenta conectar con la última IP guardada (en silencio)."""
        if self.client.connected:
            return
        if self.ip_var.get().strip():
            self._do_connect(silent=True)

    def toggle_connection(self):
        if self.client.connected:
            self.client.disconnect()
            self._set_connected(False)
            return

        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showwarning(
                "Falta IP", "Escribe la IP del ordenador del juego.\n"
                "La verás en la pantalla de Configuración del juego.",
                parent=self)
            return
        self._do_connect(silent=False)

    def _do_connect(self, silent: bool):
        ip = self.ip_var.get().strip()
        try:
            port = int(self.port_var.get() or DEFAULT_PORT)
        except ValueError:
            if not silent:
                messagebox.showwarning("Puerto", "Puerto no válido.",
                                       parent=self)
            return

        self.btn_connect.config(text="...")
        self.conn_label.config(text="Conectando...", fg=MUTED)
        self.update_idletasks()
        try:
            self.client.connect(ip, port)
            self._set_connected(True)
            self._save_config()
        except Exception as e:
            self._set_connected(False)
            if not silent:
                messagebox.showerror(
                    "No se pudo conectar",
                    f"No se pudo conectar con {ip}:{port}\n\n{e}\n\n"
                    "Comprueba que:\n"
                    "• El juego (v3.2) está abierto en el otro ordenador\n"
                    "• Los dos equipos están en la misma red WiFi\n"
                    "• El firewall permite el puerto " + str(port),
                    parent=self,
                )

    def _set_connected(self, ok: bool):
        if ok:
            self.conn_dot.config(fg=ACCENT)
            self.conn_label.config(text="Conectado", fg=TEXT_COLOR)
            self.btn_connect.config(text="DESCONECTAR")
            self.btn_connect.set_colors(GRAY_BTN, "white")
        else:
            self.conn_dot.config(fg=RED)
            self.conn_label.config(text="Desconectado", fg=MUTED)
            self.btn_connect.config(text="CONECTAR")
            self.btn_connect.set_colors(BLUE_BTN, "white")
            self.estado_label.config(text="— SIN CONEXIÓN —", fg=MUTED)
            self.jugador_label.config(text="")
            self.btn_stop.pack_forget()
            self._laser_times = []
            self._set_laser_alarm(False)

    def _require_connection(self) -> bool:
        if not self.client.connected:
            messagebox.showwarning("Sin conexión",
                                   "Conéctate primero al ordenador del juego.")
            return False
        return True

    # ================= Acciones PARTIDA =================
    def add_player(self):
        if not self._require_connection():
            return
        name = self.name_var.get().strip()
        if not name:
            return
        self.client.send({"cmd": "add_player", "name": name})
        self.name_var.set("")

    def _selected_name(self):
        sel = self.queue_list.curselection()
        if not sel:
            return None
        text = self.queue_list.get(sel[0])
        return text.split(". ", 1)[1] if ". " in text else text

    def _on_queue_select(self, _event=None):
        name = self._selected_name()
        if name:
            self.btn_start_sel.config(text=f"▶ INICIAR\n{name}")
            self.btn_start_sel.pack(pady=(0, 10), before=self.btn_quitar)
        else:
            self.btn_start_sel.pack_forget()

    def start_selected(self):
        if not self._require_connection():
            return
        name = self._selected_name()
        if not name:
            return
        self.client.send({"cmd": "start", "name": name})

    def stop_game(self):
        if not self._require_connection():
            return
        # Para directamente, sin diálogo (en Mac podía quedar oculto)
        # y SIN guardar puntuación: el jugador queda preparado de nuevo.
        self.client.send({"cmd": "stop"})

    def remove_selected(self):
        if not self._require_connection():
            return
        name = self._selected_name()
        if not name:
            return
        self.client.send({"cmd": "remove_player", "name": name})

    def clear_queue(self):
        if not self._require_connection():
            return
        if messagebox.askyesno("Vaciar cola", "¿Vaciar toda la cola de jugadores?",
                               parent=self):
            self.client.send({"cmd": "clear_queue"})

    # ================= Acciones CONFIGURACIÓN =================
    def save_game_values(self):
        if not self._require_connection():
            return
        values = {}
        for key, var in self.cfg_vars.items():
            try:
                values[key] = int(var.get())
            except ValueError:
                messagebox.showerror(
                    "Error", "Todos los valores deben ser números enteros.")
                return
        self.client.send({"cmd": "set_config", "values": values})

    def toggle_level(self, key):
        if not self._require_connection():
            return
        new_state = not self.level_btns[key]["on"]
        self.client.send({"cmd": "set_level", "level": key, "on": new_state})

    def toggle_sound(self, key):
        if not self._require_connection():
            return
        row = self.sound_rows[key]
        row["on"] = not row["on"]
        self._style_sound_toggle(key)
        self.send_sound(key)

    def _style_sound_toggle(self, key):
        row = self.sound_rows[key]
        if row["on"]:
            row["toggle"].config(text="ON")
            row["toggle"].set_colors(ACCENT, "#000000")
        else:
            row["toggle"].config(text="OFF")
            row["toggle"].set_colors(GRAY_BTN, MUTED)

    def send_sound(self, key):
        if self._loading_config:
            return
        if not self.client.connected:
            return
        row = self.sound_rows[key]
        self.client.send({
            "cmd": "set_sound",
            "key": key,
            "enabled": bool(row["on"]),
            "volume": float(row["vol"].get()) / 100.0,
        })

    def reset_ranking(self, which, label):
        if not self._require_connection():
            return
        if messagebox.askyesno("Confirmar", f"¿Seguro? {label}", parent=self):
            self.client.send({"cmd": "reset_ranking", "which": which})

    def apply_serial(self):
        if not self._require_connection():
            return
        self.client.send({"cmd": "set_serial", "port": self.serial_var.get()})

    # ================= Recepción =================
    def _poll_incoming(self):
        try:
            while True:
                msg = self.client.incoming.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_incoming)

    def _handle_message(self, msg: dict):
        mtype = msg.get("type", "")

        if mtype == "__disconnected__":
            self._set_connected(False)
        elif mtype == "error":
            messagebox.showwarning("Juego", msg.get("msg", "Error"))
        elif mtype == "info":
            messagebox.showinfo("Juego", msg.get("msg", ""))
        elif mtype == "estado":
            self.last_state = msg
            self._render_state(msg)
        elif mtype == "config":
            self._render_config(msg)
        elif mtype == "ranking":
            self._render_ranking(msg)
            self._render_history(msg)

    def _render_state(self, st: dict):
        estado = st.get("estado", "")
        jugador = st.get("jugador", "")

        if estado == "en_curso":
            self.estado_label.config(text="PARTIDA EN CURSO", fg=ACCENT)
            self.jugador_label.config(text=f"Jugando: {jugador}")
        elif estado == "esperando_jugador":
            self.estado_label.config(text=f"ESPERANDO A {jugador.upper()}",
                                     fg=ORANGE)
            self.jugador_label.config(text="Pulsa el botón verde para empezar")
        elif estado == "terminada":
            self.estado_label.config(text="PARTIDA TERMINADA", fg=CYAN)
            self.jugador_label.config(text=f"Último jugador: {jugador}")
        else:
            self.estado_label.config(text="EN ESPERA", fg=MUTED)
            self.jugador_label.config(text="")

        self.tiempo_value.config(text=format_time(st.get("tiempo", 0)))
        self.puntos_value.config(text=format_score(st.get("puntos", 0)))
        self.lasers_value.config(text=str(st.get("lasers", 0)))

        # Botón PARAR solo con partida en curso
        if estado == "en_curso":
            self.btn_stop.pack(fill="x", pady=(14, 0))
        else:
            self.btn_stop.pack_forget()

        # Bolita verde durante 1 segundo cuando se toca un láser
        lasers = int(st.get("lasers", 0))
        if lasers > self._last_lasers:
            self._register_laser_hit()
        elif lasers < self._last_lasers:
            # Partida nueva: reiniciar detección
            self._laser_times = []
            self._set_laser_alarm(False)
        self._last_lasers = lasers

        # Cola (conservar la selección si el jugador sigue en cola)
        selected = self._selected_name()
        cola = list(st.get("cola", []))
        self.queue_list.delete(0, "end")
        for i, name in enumerate(cola, start=1):
            self.queue_list.insert("end", f"{i}. {name}")
        if selected in cola:
            self.queue_list.selection_set(cola.index(selected))
        self._on_queue_select()

    def _register_laser_hit(self):
        """Anota el toque, enciende la bolita y vigila la cadencia."""
        import time as _time
        now = _time.time()
        self._laser_times.append(now)
        self._laser_times = self._laser_times[-10:]

        # ¿10 toques seguidos con la misma frecuencia? -> láser bloqueado
        if len(self._laser_times) == 10:
            intervals = [
                self._laser_times[i + 1] - self._laser_times[i]
                for i in range(9)
            ]
            mean = sum(intervals) / len(intervals)
            tolerance = max(0.25, mean * 0.2)
            regular = all(abs(d - mean) <= tolerance for d in intervals)
            self._alarm_interval = mean
            self._set_laser_alarm(regular)

        # Si la alarma sigue activa, reiniciar el vigilante: si deja de
        # llegar la señal con esa cadencia, la alarma se apaga sola.
        if self._laser_alarm:
            self._restart_alarm_watchdog()
        else:
            self._laser_blip()

    def _restart_alarm_watchdog(self):
        if self._alarm_watchdog is not None:
            try:
                self.after_cancel(self._alarm_watchdog)
            except Exception:
                pass
        timeout_ms = int(max(3.0, self._alarm_interval * 2.5) * 1000)
        self._alarm_watchdog = self.after(timeout_ms, self._alarm_timeout)

    def _alarm_timeout(self):
        """No han llegado más toques con la cadencia detectada: todo OK."""
        self._alarm_watchdog = None
        if self._laser_alarm:
            self._laser_times = []
            self._set_laser_alarm(False)

    def _set_laser_alarm(self, on: bool):
        if on == self._laser_alarm:
            return
        self._laser_alarm = on
        if not on and self._alarm_watchdog is not None:
            try:
                self.after_cancel(self._alarm_watchdog)
            except Exception:
                pass
            self._alarm_watchdog = None
        if on:
            if self._laser_after is not None:
                try:
                    self.after_cancel(self._laser_after)
                except Exception:
                    pass
                self._laser_after = None
            self.laser_dot.config(fg=RED)
            self.laser_txt.config(text="LÁSER BLOQUEADO O DESCALIBRADO",
                                  fg=RED)
        else:
            self.laser_dot.config(fg=CARD_COLOR)
            self.laser_txt.config(text="LÁSER TOCADO", fg=MUTED)

    def _laser_blip(self):
        self.laser_dot.config(fg=ACCENT)
        if self._laser_after is not None:
            try:
                self.after_cancel(self._laser_after)
            except Exception:
                pass
        self._laser_after = self.after(1000, self._laser_blip_off)

    def _laser_blip_off(self):
        self._laser_after = None
        if not self._laser_alarm:
            self.laser_dot.config(fg=CARD_COLOR)

    def _render_config(self, cfg: dict):
        self._loading_config = True
        try:
            # Valores de partida (no pisar el campo que se está editando)
            focused = self.focus_get()
            for key, var in self.cfg_vars.items():
                if key in cfg and self.cfg_entries[key] is not focused:
                    var.set(str(cfg[key]))

            # Niveles (pestaña configuración)
            levels = cfg.get("levels", {})
            for key, info in self.level_btns.items():
                on = bool(levels.get(key, False))
                info["on"] = on
                info["btn"].config(
                    text=f"{info['label']}\n{'ON' if on else 'OFF'}")
                if on:
                    info["btn"].set_colors(ACCENT, "#000000")
                else:
                    info["btn"].set_colors(GRAY_BTN, MUTED)

            # Niveles (indicadores de la pantalla principal)
            for key, ind in self.level_dots.items():
                on = bool(levels.get(key, False))
                if on:
                    ind["dot"].config(fg=ACCENT)
                    ind["txt"].config(text=f"{ind['label']} ON", fg=TEXT_COLOR)
                else:
                    ind["dot"].config(fg=GRAY_BTN)
                    ind["txt"].config(text=f"{ind['label']} OFF", fg=MUTED)

            # Sonidos
            sounds = cfg.get("sounds", {})
            for key, row in self.sound_rows.items():
                s = sounds.get(key)
                if not s:
                    continue
                row["on"] = bool(s.get("enabled", True))
                self._style_sound_toggle(key)
                vol = float(s.get("volume", 0.8)) * 100.0
                row["vol"].set(vol)
                row["pct"].config(text=f"{int(vol)}%")
                row["name"].config(text=s.get("name", "—"))

            # Versión del juego (para comprobar que está actualizado)
            if self.client.connected:
                ver = cfg.get("version")
                if ver:
                    self.conn_label.config(text=f"Conectado · juego v{ver}")
                else:
                    self.conn_label.config(
                        text="Conectado · juego DESACTUALIZADO")

            # Puerto serie
            ports = ["Auto (detectar)"] + list(cfg.get("ports", []))
            current = cfg.get("serial_port", "") or "Auto (detectar)"
            if current not in ports:
                ports.append(current)
            self.port_combo["values"] = ports
            self.serial_var.set(current)
            if cfg.get("serial_connected"):
                self.serial_state_lbl.config(text="●  Consola conectada",
                                             fg=ACCENT)
            else:
                self.serial_state_lbl.config(text="●  Consola sin conexión",
                                             fg=RED)
        finally:
            self._loading_config = False

    def _on_close(self):
        self.client.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = RemoteApp()
    app.mainloop()
