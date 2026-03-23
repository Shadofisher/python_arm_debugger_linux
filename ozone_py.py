import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
import queue
import os
import sys
import re
import shutil
import socket
import subprocess
import json
import time
from gdb_backend import GdbBackend

if os.name == 'nt':
    GDB_PATH = r"C:\Users\agtec\CLionProjects\MK3_GCC\arm-gcc\gcc-arm-none-eabi-10.3-2021.10\bin\arm-none-eabi-gdb.exe"
else:
    # Try multiple variants for ARM GDB
    GDB_PATH = (shutil.which("arm-none-eabi-gdb") or
               shutil.which("arm-zephyr-eabi-gdb") or
               shutil.which("gdb-multiarch"))

    # Known fixed locations for ST and Zephyr
    st_gdb_path = "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.13.3.rel1.linux64_1.0.0.202410170706/tools/bin/arm-none-eabi-gdb"
    zephyr_gdb = "/home/graeme/zephyr-sdk-0.17.2/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb"

    if not GDB_PATH:
        # Check specific known locations
        if os.path.exists(st_gdb_path):
            GDB_PATH = st_gdb_path
        elif os.path.exists(zephyr_gdb):
            GDB_PATH = zephyr_gdb
        else:
            # Fallback to system GDB if no ARM GDB found, but warn later
            GDB_PATH = shutil.which("gdb") or "arm-none-eabi-gdb"

    # Priority for ST GDB if requested or found, as it's the most compatible for ST-LINK tasks
    # But ONLY if it actually runs. We'll check its health later in _check_gdb_working().
    if os.path.exists(st_gdb_path):
        # Check if the ST GDB is actually working (not missing libncurses.so.5)
        try:
            res = subprocess.run([st_gdb_path, "--version"], capture_output=True, text=True, timeout=1)
            if res.returncode == 0:
                GDB_PATH = st_gdb_path
            else:
                print(f"ST GDB at {st_gdb_path} exists but fails to run (missing libncurses.so.5?). Falling back.")
                if os.path.exists(zephyr_gdb):
                    GDB_PATH = zephyr_gdb
        except Exception:
            if os.path.exists(zephyr_gdb):
                GDB_PATH = zephyr_gdb
    elif GDB_PATH == "gdb" or (shutil.which("gdb") and GDB_PATH == shutil.which("gdb")):
        # If we only have generic gdb, try Zephyr as it's ARM-specific
        if os.path.exists(zephyr_gdb):
            GDB_PATH = zephyr_gdb

# Updated STM32CubeIDE 1.19.0 paths for other tools
STLINK_GDBSERVER_1_19 = "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.linux64_2.2.200.202505060755/tools/bin/ST-LINK_gdbserver"
STM32_PROG_CLI_1_19 = "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.2.200.202503041107/tools/bin/STM32_Programmer_CLI"
CUBEPROG_BIN_1_19 = "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.2.200.202503041107/tools/bin"

class ConnectionProgress:
    def __init__(self, parent, title):
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.geometry("400x120")
        self.top.resizable(False, False)
        # Fix: winfo_x/y might return 0 if window not mapped yet
        parent.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - 200
        y = parent.winfo_y() + (parent.winfo_height() // 2) - 60
        self.top.geometry(f"+{x}+{y}")
        self.top.transient(parent)
        self.top.grab_set()

        self.label = ttk.Label(self.top, text="Initializing...")
        self.label.pack(pady=10)

        self.progress = ttk.Progressbar(self.top, length=300, mode='determinate')
        self.progress.pack(pady=10)
        self.progress['value'] = 0

    def update(self, value, text=None):
        if not self.top.winfo_exists():
            return
        self.progress['value'] = value
        if text:
            self.label.config(text=text)
        self.top.update()

    def close(self):
        if hasattr(self, 'top') and self.top and self.top.winfo_exists():
            self.top.destroy()

class OzonePy(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OzonePy Debugger")
        self.geometry("1200x800")

        self.debug_log_text = None

        self.gdb = GdbBackend(GDB_PATH, on_response_callback=self._on_gdb_response_callback)
        self.elf_path = ""
        self.source_files = []
        self.filtered_source_files = []
        self.breakpoints = []
        self.watches = []
        self.live_watches = []
        self.live_watch_update_interval = tk.IntVar(value=1000) # ms
        self.live_watch_timer_id = None
        self.current_source = ""
        self.current_line = 0
        self.os_type = tk.StringVar(value="Windows" if os.name == 'nt' else "Linux")
        self.file_filter = tk.StringVar(value="All Files")
        self.gdb_server_address = tk.StringVar(value="localhost:3333")
        self.gdb_architecture = tk.StringVar(value="armv7e-m") # Default for many Cortex-M4F/M7
        self.gdb_tdesc_file = tk.StringVar(value="")
        self.gdb_preconnect_cmds = tk.StringVar(value="set remotetimeout 30")
        # Try a few common default paths for J-Link
        default_jlink_paths = [
            r"C:\Program Files\SEGGER\JLink_V926\JLinkGDBServerCL.exe",
            r"C:\Program Files\SEGGER\JLink\JLinkGDBServerCL.exe"
        ] if os.name == 'nt' else [
            "/usr/bin/JLinkGDBServerCLExe",
            "/opt/SEGGER/JLink/JLinkGDBServerCLExe"
        ]
        actual_default = "JLinkGDBServerCL.exe" if os.name == 'nt' else "JLinkGDBServerCLExe"
        for p in default_jlink_paths:
            if os.path.exists(p):
                actual_default = p
                break
        self.jlink_server_path = tk.StringVar(value=actual_default)
        self.jlink_device = tk.StringVar(value="EFM32GG11B820F2048GL192")
        self.jlink_interface = tk.StringVar(value="SWD")
        self.jlink_speed = tk.StringVar(value="4000")
        self.jlink_script = tk.StringVar(value="default.jlinkscript")
        self.openocd_server_path = tk.StringVar(value="openocd.exe" if os.name == 'nt' else "openocd")
        self.openocd_config_path = tk.StringVar(value="openocd.cfg")
        self.jlink_process = None
        self.openocd_process = None

        # ST-LINK GDB Server settings
        default_stlink_paths = [
            r"C:\ST\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.win32\tools\bin\ST-LINK_gdbserver.exe",
            r"C:\ST\STM32CubeCLT\STLink-gdb-server\bin\ST-LINK_gdbserver.exe",
        ] if os.name == 'nt' else [
            "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.linux64_2.2.200.202505060755/tools/bin/ST-LINK_gdbserver",
            "/opt/st/stm32cubeide_1.16.1/plugins/com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.linux64_2.1.400.202404281720/tools/bin/ST-LINK_gdbserver",
            "/opt/st/stm32cubeclt_1.19.0/STLink-gdb-server/bin/ST-LINK_gdbserver",
            "/usr/local/bin/ST-LINK_gdbserver",
        ]
        actual_stlink_default = "ST-LINK_gdbserver.exe" if os.name == 'nt' else "ST-LINK_gdbserver"
        for p in default_stlink_paths:
            if os.path.exists(p):
                actual_stlink_default = p
                break
        if actual_stlink_default == ("ST-LINK_gdbserver.exe" if os.name == 'nt' else "ST-LINK_gdbserver"):
            found = shutil.which("ST-LINK_gdbserver")
            if found:
                actual_stlink_default = found

        default_cubeprog_paths = [
            r"C:\ST\STM32CubeProgrammer\bin",
            r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin",
            r"C:\ST\STM32CubeProgrammer",
            r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer",
        ] if os.name == 'nt' else [
            "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.2.200.202505060755/tools/bin",
            "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.2.200.202503041107/tools/bin",
            "/opt/st/stm32cubeclt_1.19.0/STM32CubeProgrammer/bin",
            "/opt/st/stm32cubeclt_1.19.0/STM32CubeProgrammer",
            "/opt/st/stm32cubeide_1.16.1/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.1.400.202404281720/tools/bin",
            os.path.expanduser("~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin"),
        ]
        actual_cubeprog_default = ""
        for p in default_cubeprog_paths:
            if os.path.exists(p):
                actual_cubeprog_default = p
                break

        self.stlink_server_path = tk.StringVar(value=actual_stlink_default)
        self.stlink_frequency = tk.StringVar(value="8000")   # kHz
        self.stlink_serial = tk.StringVar(value="")          # empty = auto-detect first ST-LINK
        self.stlink_apid = tk.StringVar(value="0")           # AP index (0 = default core)
        self.stlink_persistent = tk.BooleanVar(value=False)  # keep server alive after GDB disconnects
        self.stlink_attach = tk.BooleanVar(value=False)      # attach to running target (no reset)
        self.stlink_init_under_reset = tk.BooleanVar(value=True)  # -k flag
        self.stlink_cubeprogrammer_path = tk.StringVar(value=actual_cubeprog_default)
        self.stlink_device = tk.StringVar(value="")          # -d <device_name>
        self.stlink_process = None
        self.target_connected = False
        self.is_connecting = False
        self.is_running = False
        self.server_log_thread = None
        self.debug_log_text = None
        self.debug_window = None

        # Color settings
        self.color_comments = tk.StringVar(value="#008000") # Green
        self.color_code_fg = tk.StringVar(value="#000000")  # Black
        self.color_code_bg = tk.StringVar(value="#ffffff")  # White
        self.color_breakpoint = tk.StringVar(value="#ffcccc") # Light red
        self.color_current_line = tk.StringVar(value="#add8e6") # Light blue
        self.show_inline_vars = tk.BooleanVar(value=True)

        # Recent files
        self.recent_files = []
        self.recent_files_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recent_files.json")
        self._load_recent_files()

        # Proper cleanup on window close
        self.protocol("WM_DELETE_WINDOW", self.quit)

        # Check if JLinkGDBServerCL is in path
        jlink_exe = "JLinkGDBServerCL.exe" if os.name == 'nt' else "JLinkGDBServerCLExe"
        if not shutil.which(jlink_exe):
            # Try setting a default that might be on path
            self.jlink_server_path.set(jlink_exe)

        self._setup_styles()
        self._setup_ui()
        self.bind("<<GdbResponse>>", self._poll_gdb_responses)

        # UI is ready, now we can verify GDB and log results to the debug log if needed
        self._check_gdb_working()

        self.gdb.start()
        self._start_periodic_poll()

    def _check_gdb_working(self):
        """Verify that GDB can be executed and supports ARM architecture."""
        global GDB_PATH
        self.debug_log(f"Verifying GDB at {GDB_PATH}...", "info")
        try:
            # Try to run GDB and check architecture support
            # Use -ex "set architecture arm" to see if it's supported
            res = subprocess.run([GDB_PATH, "-q", "-batch", "-ex", "set architecture arm"],
                                capture_output=True, text=True, timeout=5)

            if res.returncode != 0:
                self.debug_log(f"GDB check failed with return code {res.returncode}", "error")
                self.debug_log(f"GDB stderr: {res.stderr}", "error")

                if "libncurses.so.5" in res.stderr or "error while loading shared libraries" in res.stderr:
                    msg = f"GDB at '{GDB_PATH}' is missing required libraries.\n\n"
                    msg += f"Error: {res.stderr.strip()}\n\n"
                    msg += "This is common for STM32CubeCLT's GDB on modern Linux.\n"
                    msg += "To fix this, run: sudo apt install libncurses5 libtinfo5\n\n"
                    msg += "Would you like to try using the Zephyr SDK GDB as a fallback?"
                    if os.path.exists("/home/graeme/zephyr-sdk-0.17.2/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb"):
                        if messagebox.askyesno("GDB Library Error", msg):
                            GDB_PATH = "/home/graeme/zephyr-sdk-0.17.2/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb"
                            self.gdb.gdb_path = GDB_PATH
                            self.log(f"Switched to fallback working GDB: {GDB_PATH}")
                            # Restart GDB with new path
                            self.gdb.stop()
                            self.gdb.start()
                    else:
                        messagebox.showerror("GDB Library Error", msg)
                elif "Undefined item: \"arm\"" in res.stderr or "Undefined item: \"arm\"" in res.stdout:
                    msg = f"The selected GDB ('{GDB_PATH}') does not support ARM architecture.\n\n"
                    msg += "Please select an arm-none-eabi-gdb or gdb-multiarch."
                    messagebox.showwarning("GDB Architecture Error", msg)
            else:
                self.debug_log("GDB verification successful (ARM supported).", "info")

        except Exception as e:
            self.debug_log(f"GDB verification failed to execute: {e}", "error")
            messagebox.showwarning("GDB Execution Error", f"Failed to execute GDB at {GDB_PATH}:\n{e}")

    def _setup_styles(self):
        style = ttk.Style()
        # Use a more modern theme if available
        available_themes = style.theme_names()
        if 'vista' in available_themes:
            style.theme_use('vista')
        elif 'clam' in available_themes:
            style.theme_use('clam')

        # Global style
        style.configure('.', font=('Segoe UI', 10))
        style.configure('Toolbar.TFrame', background='#f8f9fa')
        style.configure('Toolbar.TButton', padding=5, font=('Segoe UI', 9))
        style.configure('Status.TLabel', font=('Segoe UI', 9), padding=2)
        style.configure('Location.TLabel', font=('Segoe UI', 9, 'bold'), padding=2)

        # Style for Treeview to make it look cleaner
        style.configure('Treeview', font=('Segoe UI', 9), rowheight=22)
        style.configure('Treeview.Heading', font=('Segoe UI', 9, 'bold'))

    def _setup_ui(self):
        # --- Menubar ---
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open ELF...", command=self.load_elf)

        # Recent Files Menu
        self.recent_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Recent Files", menu=self.recent_menu)
        self._update_recent_files_menu()

        # OS Selection Menu
        os_menu = tk.Menu(file_menu, tearoff=0)
        os_menu.add_radiobutton(label="Windows", variable=self.os_type, value="Windows")
        os_menu.add_radiobutton(label="Linux", variable=self.os_type, value="Linux")
        file_menu.add_cascade(label="Operating System", menu=os_menu)

        file_menu.add_command(label="GDB Server Settings...", command=self.set_gdb_server)
        file_menu.add_command(label="J-Link Server Settings...", command=self.set_jlink_settings)
        file_menu.add_command(label="Load J-Link Script...", command=self.load_jlink_script)
        file_menu.add_command(label="OpenOCD Server Settings...", command=self.set_openocd_settings)
        file_menu.add_command(label="ST-LINK Server Settings...", command=self.set_stlink_settings)
        file_menu.add_command(label="Color Settings...", command=self.set_colors)
        file_menu.add_command(label="Show Debug Log", command=self.show_debug_log)

        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        self.files_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Source Files", menu=self.files_menu)

        self.debug_menu = tk.Menu(menubar, tearoff=0)

        # Connect Target Submenu
        self.connect_menu = tk.Menu(self.debug_menu, tearoff=0)
        self._update_connect_menu()
        self.debug_menu.add_cascade(label="Connect Target", menu=self.connect_menu)

        self.debug_menu.add_command(label="Download", command=self.download)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Go / Continue (F5)", command=self.go)
        self.debug_menu.add_command(label="Pause (F6)", command=self.pause)
        self.debug_menu.add_command(label="Stop (F9)", command=self.stop_debug)
        self.debug_menu.add_command(label="Disconnect", command=self.disconnect_target)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Reset", command=lambda: self.reset_target(run_to_main=False))
        self.debug_menu.add_command(label="Run to Main", command=self.run_to_main)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Delete All Breakpoints", command=self.delete_all_breakpoints)
        self.debug_menu.add_separator()
        self.debug_menu.add_checkbutton(label="Show Inline Variables", variable=self.show_inline_vars, command=self._on_toggle_inline_vars)
        menubar.add_cascade(label="Debug", menu=self.debug_menu)

        self.config(menu=menubar)

        # --- Toolbar ---
        self.toolbar = ttk.Frame(self, style='Toolbar.TFrame', relief=tk.RAISED, borderwidth=1)
        self.toolbar.pack(side=tk.TOP, fill=tk.X)

        actions_frame = ttk.Frame(self.toolbar)
        actions_frame.pack(side=tk.LEFT, padx=5, pady=2)

        # Buttons with Unicode icons
        btn_config = [
            ("⤓ Load", self.download),
            ("▶ Go", self.go),
            ("⏸ Pause", self.pause),
            ("⏹ Stop", self.stop_debug),
            ("⟳ Reset", lambda: self.reset_target(run_to_main=False)),
            (None, None),
            ("↷ Over", self.step_over),
            ("↓ Into", self.step),
            ("⌘ Main", self.run_to_main),
        ]

        self.toolbar_btns = {}
        self.default_button_bg = '#f8f9fa' # Matches Toolbar.TFrame
        for text, cmd in btn_config:
            if text is None:
                ttk.Separator(actions_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=5, fill=tk.Y)
                continue
            # Use tk.Button for background color control
            btn = tk.Button(actions_frame, text=text, command=cmd,
                            font=('Segoe UI', 9), width=10,
                            relief=tk.FLAT, bd=1, bg=self.default_button_bg,
                            activebackground='#e0e0e0')
            btn.pack(side=tk.LEFT, padx=1)

            # Add hover effects to simulate ttk.Button behavior
            btn.bind("<Enter>", lambda e, b=btn: b.config(relief=tk.RAISED))
            btn.bind("<Leave>", lambda e, b=btn: b.config(relief=tk.FLAT))

            # Extract name to store it in a dict for easy access
            name = text.split(' ')[1] if ' ' in text else text
            self.toolbar_btns[name] = btn

        ttk.Label(self.toolbar, text="  Source:").pack(side=tk.LEFT)
        self.file_combo = ttk.Combobox(self.toolbar, width=50, state="readonly")
        self.file_combo.pack(side=tk.LEFT, padx=5)
        self.file_combo.bind("<<ComboboxSelected>>", self._on_file_selected)

        # --- Status Bars (at the bottom) ---
        self.status_bar = ttk.Label(self, text="Disconnected", relief=tk.SUNKEN, anchor=tk.W, style='Status.TLabel')
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.location_bar = ttk.Label(self, text="Stopped at: ---", foreground="#0055aa", relief=tk.FLAT, anchor=tk.W, style='Location.TLabel')
        self.location_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=5)

        # --- Main Layout (Vertical PanedWindow for Console) ---
        self.root_pw = ttk.PanedWindow(self, orient=tk.VERTICAL)
        self.root_pw.pack(fill=tk.BOTH, expand=True)

        # Upper horizontal PanedWindow (Source + Sidebar)
        self.upper_pw = ttk.PanedWindow(self.root_pw, orient=tk.HORIZONTAL)
        self.root_pw.add(self.upper_pw, weight=3)

        # Left: Source view with Line Numbers
        source_container = ttk.Frame(self.upper_pw)
        self.upper_pw.add(source_container, weight=3)

        self.line_numbers = tk.Text(source_container, width=4, padx=4, takefocus=0, border=0,
                                   background='#e0e0e0', state='disabled', wrap='none', font=('Consolas', 10))
        self.line_numbers.pack(side=tk.LEFT, fill=tk.Y)

        self.source_text = tk.Text(source_container, wrap=tk.NONE, undo=False,
                                   foreground=self.color_code_fg.get(), background=self.color_code_bg.get(),
                                   font=('Consolas', 10), borderwidth=0, padx=5)
        self.source_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.source_text.tag_configure("breakpoint", background=self.color_breakpoint.get())
        self.source_text.tag_configure("hit_breakpoint", background=self.color_current_line.get())
        self.source_text.tag_configure("current_line", background=self.color_current_line.get(), foreground="black")
        self.source_text.tag_configure("comment", foreground=self.color_comments.get())
        self.source_text.tag_configure("inline_var", foreground="gray", font=("Consolas", 10, "italic"))

        self.line_numbers.tag_configure("breakpoint", background=self.color_breakpoint.get(), foreground="black")
        self.line_numbers.tag_configure("hit_breakpoint", background=self.color_current_line.get(), foreground="black")
        self.line_numbers.tag_configure("current_line", background=self.color_current_line.get(), foreground="black")

        src_scroll = ttk.Scrollbar(source_container, command=self._on_scroll)
        src_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.source_text.config(yscrollcommand=src_scroll.set)
        self.line_numbers.config(yscrollcommand=src_scroll.set)

        self.line_numbers.bind("<Button-1>", self._on_line_click)
        self.source_text.bind("<MouseWheel>", self._on_mousewheel)
        self.line_numbers.bind("<MouseWheel>", self._on_mousewheel)
        self.source_text.bind("<Motion>", self._on_source_hover)
        self.source_text.bind("<Leave>", self._hide_tooltip)
        self.tooltip = None
        self.last_hover_word = None

        # Right: Sidebar using Notebook (Tabs)
        self.sidebar_tabs = ttk.Notebook(self.upper_pw)
        self.upper_pw.add(self.sidebar_tabs, weight=1)

        # Source Files tab
        files_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(files_frame, text="Files")

        # Filter Frame
        filter_frame = ttk.Frame(files_frame)
        filter_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)
        ttk.Label(filter_frame, text="Filter:").pack(side=tk.LEFT)
        self.file_filter_combo = ttk.Combobox(filter_frame, textvariable=self.file_filter,
                                              values=["All Files", "*.c Files"], state="readonly", width=12)
        self.file_filter_combo.pack(side=tk.LEFT, padx=5)
        self.file_filter_combo.bind("<<ComboboxSelected>>", lambda e: self._update_file_list_ui())

        self.files_tree = ttk.Treeview(files_frame, columns=("path",), show="tree")
        self.files_tree.heading("#0", text="File")
        self.files_tree.column("path", width=0, stretch=tk.NO)
        self.files_tree.pack(fill=tk.BOTH, expand=True)
        self.files_tree.bind("<Double-1>", self._on_file_tree_double_click)

        # Breakpoints tab
        bp_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(bp_frame, text="Breakpoints")
        self.bp_tree = ttk.Treeview(bp_frame, columns=("file", "line"), show="headings")
        self.bp_tree.heading("file", text="File")
        self.bp_tree.heading("line", text="Line")
        self.bp_tree.pack(fill=tk.BOTH, expand=True)
        self.bp_tree.bind("<Button-3>", self._on_bp_right_click)

        # Watch tab
        watch_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(watch_frame, text="Watches")
        self.watch_tree = ttk.Treeview(watch_frame, columns=("value",), show="tree headings")
        self.watch_tree.heading("#0", text="Variable")
        self.watch_tree.heading("value", text="Value")
        self.watch_tree.pack(fill=tk.BOTH, expand=True)
        self.watch_tree.bind("<Button-3>", self._on_watch_right_click)
        self.watch_tree.bind("<Double-1>", self._on_watch_double_click)
        self.watch_tree.bind("<<TreeviewOpen>>", self._on_watch_expand)
        self.watch_tree.tag_configure('changed', background='lightblue')
        ttk.Button(watch_frame, text="Add Watch", command=self.add_watch).pack(fill=tk.X)

        # Live Watch tab
        live_watch_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(live_watch_frame, text="Live")
        self.live_watch_tree = ttk.Treeview(live_watch_frame, columns=("value",), show="tree headings")
        self.live_watch_tree.heading("#0", text="Variable")
        self.live_watch_tree.heading("value", text="Value")
        self.live_watch_tree.pack(fill=tk.BOTH, expand=True)
        self.live_watch_tree.bind("<Button-3>", self._on_live_watch_right_click)
        self.live_watch_tree.bind("<Double-1>", self._on_live_watch_double_click)
        self.live_watch_tree.bind("<<TreeviewOpen>>", self._on_watch_expand)
        self.live_watch_tree.tag_configure('changed', background='lightgreen')
        
        # Status Label at the bottom of the Live Watch window
        self.live_watch_status = ttk.Label(live_watch_frame, text="Live Watch: Idle", style='Status.TLabel')
        self.live_watch_status.pack(side=tk.BOTTOM, fill=tk.X)

        live_ctrl_frame = ttk.Frame(live_watch_frame)
        live_ctrl_frame.pack(fill=tk.X)
        ttk.Button(live_ctrl_frame, text="Add Live Watch", command=self.add_live_watch).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(live_ctrl_frame, text="Rate (ms):").pack(side=tk.LEFT, padx=2)
        rate_entry = ttk.Entry(live_ctrl_frame, textvariable=self.live_watch_update_interval, width=6)
        rate_entry.pack(side=tk.LEFT, padx=2)

        # Registers tab
        reg_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(reg_frame, text="Registers")
        self.reg_tree = ttk.Treeview(reg_frame, columns=("value",), show="tree headings")
        self.reg_tree.heading("#0", text="Register")
        self.reg_tree.heading("value", text="Value")
        self.reg_tree.pack(fill=tk.BOTH, expand=True)

        # Call Stack tab
        stack_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(stack_frame, text="Call Stack")
        self.stack_tree = ttk.Treeview(stack_frame, columns=("func", "file", "line"))
        self.stack_tree.heading("#0", text="Level")
        self.stack_tree.heading("func", text="Function")
        self.stack_tree.heading("file", text="File")
        self.stack_tree.heading("line", text="Line")
        self.stack_tree.column("#0", width=50)
        self.stack_tree.column("func", width=150)
        self.stack_tree.column("file", width=150)
        self.stack_tree.column("line", width=50)
        self.stack_tree.pack(fill=tk.BOTH, expand=True)
        self.stack_tree.bind("<Double-1>", self._on_stack_frame_double_click)

        # Memory tab
        mem_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(mem_frame, text="Memory")
        mem_ctrl_frame = ttk.Frame(mem_frame)
        mem_ctrl_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(mem_ctrl_frame, text="Addr:").pack(side=tk.LEFT)
        self.mem_addr_entry = ttk.Entry(mem_ctrl_frame, width=12)
        self.mem_addr_entry.pack(side=tk.LEFT, padx=2)
        self.mem_addr_entry.insert(0, "0x20000000")
        ttk.Button(mem_ctrl_frame, text="Read", width=5, command=self.read_memory).pack(side=tk.LEFT)
        self.mem_text = tk.Text(mem_frame, font=("Consolas", 10), wrap=tk.NONE)
        self.mem_text.pack(fill=tk.BOTH, expand=True)

        # Context Menus
        self.bp_menu = tk.Menu(self, tearoff=0)
        self.bp_menu.add_command(label="Delete Breakpoint", command=self.delete_selected_breakpoint)
        self.bp_menu.add_separator()
        self.bp_menu.add_command(label="Delete All Breakpoints", command=self.delete_all_breakpoints)

        self.source_menu = tk.Menu(self, tearoff=0)
        self.source_menu.add_command(label="Add to Watch", command=self._add_selection_to_watch)
        self.source_menu.add_command(label="Add to Live Watch", command=self._add_selection_to_live_watch)
        self.source_text.bind("<Button-3>", self._on_source_right_click)

        self.watch_menu = tk.Menu(self, tearoff=0)
        self.watch_menu.add_command(label="Show Memory View", command=lambda: self._show_memory_for_watch(is_live=False))
        self.watch_menu.add_command(label="Add to Live Watch", command=self._add_watch_to_live)
        self.watch_menu.add_separator()
        self.watch_menu.add_command(label="Delete Watch", command=self.delete_selected_watch)

        self.live_watch_menu = tk.Menu(self, tearoff=0)
        self.live_watch_menu.add_command(label="Show Memory View", command=lambda: self._show_memory_for_watch(is_live=True))
        self.live_watch_menu.add_command(label="Delete Live Watch", command=self.delete_selected_live_watch)

        # --- Console (Bottom) ---
        console_frame = ttk.Frame(self.root_pw)
        self.root_pw.add(console_frame, weight=1)

        self.console_output = tk.Text(console_frame, height=8, state=tk.DISABLED,
                                     bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10),
                                     padx=5, pady=5)
        self.console_output.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.console_input = ttk.Entry(console_frame, font=("Consolas", 10))
        self.console_input.pack(side=tk.BOTTOM, fill=tk.X)
        self.console_input.bind("<Return>", self._on_console_enter)

        # Global keyboard shortcuts
        self.bind("<F5>", lambda e: self.go())
        self.bind("<F6>", lambda e: self.pause())
        self.bind("<F9>", lambda e: self.stop_debug())
        self.bind("<F7>", lambda e: self.step())
        self.bind("<F8>", lambda e: self.step_over())

    def _update_ui_for_execution_state(self, running):
        self.is_running = running
        state = tk.DISABLED if running else tk.NORMAL
        if hasattr(self, 'toolbar_btns'):
            if 'Go' in self.toolbar_btns:
                self.toolbar_btns['Go'].config(state=state)
                if not running:
                    # Reset Go background when stopped
                    self.toolbar_btns['Go'].config(bg=self.default_button_bg)

            if 'Pause' in self.toolbar_btns:
                # Pause button should be enabled when running
                if running:
                    self.toolbar_btns['Pause'].config(state=tk.NORMAL)

        if hasattr(self, 'debug_menu'):
            try:
                self.debug_menu.entryconfig("Go / Continue (F5)", state=state)
            except Exception:
                pass
        self.update()

    def log(self, text, tag=None):
        if hasattr(self, 'console_output') and self.console_output.winfo_exists():
            try:
                self.console_output.config(state=tk.NORMAL)
                self.console_output.insert(tk.END, f"{text}\n")
                self.console_output.see(tk.END)
                self.console_output.config(state=tk.DISABLED)
            except tk.TclError:
                pass
        self.debug_log(text, tag)
        try:
            self.update()
        except tk.TclError:
            pass

    def debug_log(self, text, tag=None):
        if self.debug_log_text and tk.Text.winfo_exists(self.debug_log_text):
            try:
                self.debug_log_text.config(state=tk.NORMAL)
                self.debug_log_text.insert(tk.END, f"{text}\n", tag)
                self.debug_log_text.see(tk.END)
                self.debug_log_text.config(state=tk.DISABLED)
                self.update()
            except tk.TclError:
                # Widget likely destroyed during update/access
                pass
        else:
            # Fallback if window not open or destroyed
            print(f"DEBUG: {text}")

    def show_debug_log(self):
        if self.debug_window and tk.Toplevel.winfo_exists(self.debug_window):
            self.debug_window.lift()
            return

        self.debug_window = tk.Toplevel(self)
        self.debug_window.title("OzonePy Debug Log")
        self.debug_window.geometry("800x600")

        frame = ttk.Frame(self.debug_window)
        frame.pack(fill=tk.BOTH, expand=True)

        self.debug_log_text = tk.Text(frame, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10))
        self.debug_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(frame, command=self.debug_log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.debug_log_text.config(yscrollcommand=scrollbar.set)

        self.debug_log_text.tag_configure("error", foreground="#f44336")
        self.debug_log_text.tag_configure("info", foreground="#2196f3")
        self.debug_log_text.tag_configure("server", foreground="#ffeb3b")
        self.debug_log_text.tag_configure("mi-send", foreground="#4caf50")
        self.debug_log_text.tag_configure("mi-recv", foreground="#8bc34a")

        self.debug_log("--- Debug Log Started ---", "info")

    def _on_console_enter(self, event):
        cmd = self.console_input.get()
        if cmd:
            self.log(f"> {cmd}")
            self.gdb.send_command(cmd)
            self.console_input.delete(0, tk.END)

    def _on_gdb_response_callback(self):
        # This is called from the GDB backend threads.
        # Use event_generate to safely notify the main UI thread.
        try:
            self.event_generate("<<GdbResponse>>", when="tail")
        except Exception:
            # Might happen during shutdown
            pass

    def _start_periodic_poll(self):
        """A robust periodic poll that ensures the event loop wakes up and
        processes GDB responses even if the event-driven mechanism is sluggish."""
        self._poll_gdb_responses()
        self.after(50, self._start_periodic_poll)

        # Start live watch timer if not already running
        if self.live_watch_timer_id is None:
            self._schedule_live_watch_update()

    def _schedule_live_watch_update(self):
        interval = 1000
        try:
            interval = int(self.live_watch_update_interval.get())
            if interval < 100: interval = 100 # Minimum 100ms
        except:
            pass
        self.live_watch_timer_id = self.after(interval, self._update_live_watches)

    def _update_live_watches(self):
        if self.target_connected and self.live_watches:
            start_time = time.time()
            def update_callback(result_class, rest):
                if result_class == "^done":
                    # GDB MI -var-update output can be:
                    # changelist=[{name="var1",value="1",in_scope="true",type_changed="false",has_more="0"},...]
                    # We use a non-greedy match for the value to handle nested quotes if any (though unlikely in values).
                    # and allow for other attributes like in_scope, type_changed etc.
                    # This regex matches each variable object in the changelist.
                    item_matches = re.findall(r'\{name="([^"]+)",value="([^"]+)"[^}]*\}', rest)

                    # Also handle simplified format if it exists (some GDB versions or scenarios)
                    if not item_matches:
                        item_matches = re.findall(r'name="([^"]+)",value="([^"]+)"', rest)

                    if item_matches:
                        self.debug_log(f"Live watch changes: {item_matches}", "info")
                        for gdb_name, new_val in item_matches:
                            self._update_watch_value_recursive(self.live_watches, gdb_name, new_val)

                    # Always refresh UI if we got a ^done, to clear old highlights if needed
                    self._refresh_live_watch_tree_values()
                    self.update_idletasks() # Refresh UI
                    
                    end_time = time.time()
                    elapsed = (end_time - start_time) * 1000
                    try:
                        interval = int(self.live_watch_update_interval.get())
                    except:
                        interval = 1000
                    self.live_watch_status.config(text=f"Live Watch: Updated in {elapsed:.1f}ms (Rate: {interval}ms)")
                else:
                    self.debug_log(f"Live watch update failed: {result_class} {rest}", "error")
                    self.live_watch_status.config(text=f"Live Watch: Error ({result_class})")

                # Re-schedule after processing
                self._schedule_live_watch_update()

            self.gdb.send_command("-var-update --all-values *", update_callback)
        else:
            if not self.target_connected:
                self.live_watch_status.config(text="Live Watch: Not connected")
            elif not self.live_watches:
                self.live_watch_status.config(text="Live Watch: No variables")
            self._schedule_live_watch_update()

    def _update_live_watches_for_step(self):
        """Immediately update live watches after a step or breakpoint hit."""
        if self.target_connected and self.live_watches:
            def update_callback(result_class, rest):
                if result_class == "^done":
                    item_matches = re.findall(r'\{name="([^"]+)",value="([^"]+)"[^}]*\}', rest)
                    if not item_matches:
                        item_matches = re.findall(r'name="([^"]+)",value="([^"]+)"', rest)

                    if item_matches:
                        for gdb_name, new_val in item_matches:
                            self._update_watch_value_recursive(self.live_watches, gdb_name, new_val)
                    self._refresh_live_watch_tree_values()
                    self.update_idletasks()
                # We don't reschedule here as the timer loop is still running

            self.gdb.send_command("-var-update --all-values *", update_callback)

    def _refresh_live_watch_tree_values(self):
        def update_item_recursive(items, watches):
            watch_map = {w['gdb_name']: w for w in watches}
            for item_id in items:
                tags = self.live_watch_tree.item(item_id, "tags")
                if tags:
                    gdb_name = tags[0]
                    if gdb_name in watch_map:
                        w = watch_map[gdb_name]
                        val_str = w['value']
                        item_tags = [w['gdb_name']]
                        if w.get('changed'):
                            item_tags.append('changed')
                            if w['previous_value'] is not None:
                                val_str = f"{w['value']} (was: {w['previous_value']})"

                        self.live_watch_tree.item(item_id, values=(val_str,), tags=tuple(item_tags))
                        if 'children' in w:
                            update_item_recursive(self.live_watch_tree.get_children(item_id), w['children'])

        update_item_recursive(self.live_watch_tree.get_children(""), self.live_watches)

    def _show_arch_warning(self):
        if not hasattr(self, '_last_arch_warning_time'):
            self._last_arch_warning_time = 0

        import time
        now = time.time()
        # Don't show too many popups
        if now - self._last_arch_warning_time < 30:
            return

        self._last_arch_warning_time = now
        msg = "GDB reported 'unknown architecture \"arm\"' while parsing target description.\n\n"
        msg += "This usually means the GDB binary you are using is not built with ARM support.\n"
        msg += f"Currently using: {GDB_PATH}\n\n"
        msg += "Please ensure you have 'arm-none-eabi-gdb' (part of Arm GNU Toolchain) installed and in your PATH, or specify the correct path in Settings."
        self.after(0, lambda: messagebox.showwarning("Architecture Warning", msg))

    def _poll_gdb_responses(self, event=None):
        processed_any = False
        try:
            while True:
                try:
                    resp = self.gdb.response_queue.get_nowait()
                except (queue.Empty, AttributeError):
                    break

                processed_any = True
                resp_type, *data = resp
                if resp_type == 'console':
                    self.log(data[0])
                    if "unknown architecture \"arm\"" in data[0]:
                        self._show_arch_warning()
                elif resp_type == 'log':
                    self.log(f"LOG: {data[0]}")
                    if "unknown architecture \"arm\"" in data[0]:
                        self._show_arch_warning()
                elif resp_type == 'exec-async':
                    self._handle_exec_async(data[0])
                elif resp_type == 'result':
                    # data = (token, result_class, rest)
                    if data[1] == "^connected":
                        self.target_connected = True
                        if hasattr(self, 'status_bar') and self.status_bar.winfo_exists():
                            self.status_bar.config(text="Connected to target", foreground="green")
                        self.log("Connected successfully.")
                elif resp_type == 'mi-send':
                    self.debug_log(f"MI SEND: {data[0]}", "mi-send")
                elif resp_type == 'mi-recv-debug':
                    self.debug_log(f"MI RECV DEBUG: {data[0]}", "debug")
                elif resp_type == 'mi-recv':
                    self.debug_log(f"MI RECV: {data[0]}", "mi-recv")
                elif resp_type == 'stderr':
                    self.debug_log(f"GDB ERR: {data[0]}", "error")
        except Exception as e:
            try:
                self.debug_log(f"Error in polling GDB: {e}", "error")
            except Exception:
                pass

        if processed_any:
            try:
                self.update_idletasks()
                # self.update() # Avoid excessive update() calls which might trigger TclError
            except (tk.TclError, AttributeError):
                pass

    def _handle_exec_async(self, data):
        # e.g. stopped,reason="breakpoint-hit",frame={...}
        if data.startswith("stopped"):
            self._update_ui_for_execution_state(False)
            fullname = None
            line = None
            is_bp_hit = 'reason="breakpoint-hit"' in data
            is_interrupted = 'reason="signal-received"' in data or 'reason="interrupted"' in data

            if is_interrupted or is_bp_hit:
                if is_interrupted:
                    self.log("Execution interrupted.")
                else:
                    self.log("Breakpoint hit.")
                # Switch to Call Stack tab
                if hasattr(self, "sidebar_tabs"):
                    for i in range(self.sidebar_tabs.index("end")):
                        if self.sidebar_tabs.tab(i, "text") == "Call Stack":
                            self.sidebar_tabs.select(i)
                            break
            match = re.search(r'fullname="([^"]+)"', data)
            if match:
                fullname = match.group(1).replace(r'\\', '\\')

            line_match = re.search(r'line="(\d+)"', data)
            if line_match:
                line = int(line_match.group(1))

            func_name = "unknown"
            func_match = re.search(r'func="([^"]+)"', data)
            if func_match:
                func_name = func_match.group(1)

            if fullname and line:
                self.current_line = line
                self.location_bar.config(text=f"Stopped at: {os.path.basename(fullname)}:{line} in {func_name}")
                self._update_source_view(fullname, line, is_hit=is_bp_hit)
            elif line:
                self.current_line = line
                # If only line is available, use current source if applicable
                self.location_bar.config(text=f"Stopped at line: {line} in {func_name}")
                if self.current_source:
                    self._update_source_view(self.current_source, line, is_hit=is_bp_hit)
            else:
                # If frame info is missing, and we are not in the middle of a command, request it explicitly
                def frame_callback(result_class, rest):
                    if result_class == "^done":
                        # frame={level="0",addr="0x...",func="...",file="...",fullname="...",line="..."}
                        f_match = re.search(r'fullname="([^"]+)"', rest)
                        l_match = re.search(r'line="(\d+)"', rest)
                        fn_match = re.search(r'func="([^"]+)"', rest)

                        f_name = f_match.group(1).replace(r'\\', '\\') if f_match else None
                        l_num = int(l_match.group(1)) if l_match else 0
                        f_func = fn_match.group(1) if fn_match else "unknown"

                        if f_name and l_num:
                            self.current_line = l_num
                            self.location_bar.config(text=f"Stopped at: {os.path.basename(f_name)}:{l_num} in {f_func}")
                            self._update_source_view(f_name, l_num, is_hit=is_bp_hit)
                        elif l_num:
                            self.current_line = l_num
                            self.location_bar.config(text=f"Stopped at line: {l_num} in {f_func}")
                            if self.current_source:
                                self._update_source_view(self.current_source, l_num, is_hit=is_bp_hit)
                        self.update() # Force refresh

                # Check if we should request frame info. Avoid requesting if we are likely in a transient state.
                if data.startswith("stopped"):
                     self.gdb.send_command("-stack-info-frame", frame_callback)

            self._update_watches()
            self._update_live_watches_for_step()
            self._update_registers()
            self.debug_log("DEBUG: Triggering Call Stack update from _handle_exec_async", "debug")
            self._update_call_stack()
            self._update_inline_variables()
            self.read_memory()
        elif data.startswith("running"):
            self._update_ui_for_execution_state(True)
            self.location_bar.config(text="Running...")

        self.update() # Force refresh after handling async event

    def load_elf(self):
        path = filedialog.askopenfilename(filetypes=[("ELF files", "*.elf"), ("All files", "*.*")])
        if path:
            self._open_elf_path(path)

    def _open_elf_path(self, path):
        self.elf_path = path
        # Normalize for GDB MI
        gdb_path = path.replace('\\', '/')

        def on_loaded(result_class, rest):
            if result_class == "^done":
                self.log(f"Loaded ELF: {path}")
                self._request_source_files()
                self._add_to_recent_files(path)
            else:
                self.log(f"Failed to load ELF: {rest}", "error")

        self.gdb.send_command(f'-file-exec-and-symbols "{gdb_path}"', on_loaded)

    def _load_recent_files(self):
        try:
            if os.path.exists(self.recent_files_path):
                with open(self.recent_files_path, 'r') as f:
                    self.recent_files = json.load(f)
        except Exception as e:
            print(f"Error loading recent files: {e}")
            self.recent_files = []

    def _save_recent_files(self):
        try:
            with open(self.recent_files_path, 'w') as f:
                json.dump(self.recent_files, f)
        except Exception as e:
            print(f"Error saving recent files: {e}")

    def _add_to_recent_files(self, path):
        path = os.path.abspath(path)
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[:10]  # Keep last 10
        self._save_recent_files()
        self._update_recent_files_menu()

    def _update_recent_files_menu(self):
        self.recent_menu.delete(0, tk.END)
        if not self.recent_files:
            self.recent_menu.add_command(label="No recent files", state=tk.DISABLED)
        else:
            for path in self.recent_files:
                base = os.path.basename(path)
                label = f"{base} ({path})"

                # Use a helper function to capture the path correctly
                def make_command(p):
                    return lambda: self._open_elf_path(p)

                self.recent_menu.add_command(label=label, command=make_command(path))

    def _request_source_files(self):
        def callback(result_class, rest):
            if result_class == "^done":
                # Parse filenames from output
                # Format: files=[{file="...",fullname="..."}]
                files = re.findall(r'fullname="([^"]+)"', rest)
                # Cleanup, normalize and sort
                cleaned_files = sorted(list(set([os.path.abspath(os.path.normpath(f.replace(r'\\', '\\'))) for f in files])))
                self.source_files = cleaned_files

                # Update UI with filtering
                self._update_file_list_ui(initial_load=True)

        self.gdb.send_command("-file-list-exec-source-files", callback)

    def _update_file_list_ui(self, initial_load=False):
        if not self.source_files:
            return

        filter_val = self.file_filter.get()
        if filter_val == "*.c Files":
            self.filtered_source_files = [f for f in self.source_files if f.lower().endswith(".c")]
        else:
            self.filtered_source_files = self.source_files

        # Update combobox in toolbar
        display_names = [os.path.basename(f) for f in self.filtered_source_files]
        self.file_combo['values'] = display_names

        # Update "Source Files" menu
        self.files_menu.delete(0, tk.END)
        for f in self.filtered_source_files:
            base = os.path.basename(f)
            # Use a helper function to avoid late binding issues in lambda
            def make_command(path):
                return lambda: self._update_source_view(path, 0)
            self.files_menu.add_command(label=base, command=make_command(f))

        # Update "Source Files" tab/tree
        if hasattr(self, 'files_tree'):
            for item in self.files_tree.get_children():
                self.files_tree.delete(item)
            for f in self.filtered_source_files:
                self.files_tree.insert("", tk.END, text=os.path.basename(f), values=(f,))

        # Initial load: select main.c if possible
        if initial_load and self.filtered_source_files:
            found_main = False
            for i, f in enumerate(self.filtered_source_files):
                if os.path.basename(f).lower() == "main.c":
                    self.file_combo.current(i)
                    self._update_source_view(f, 1)
                    found_main = True
                    break
            if not found_main:
                self.file_combo.current(0)
                self._update_source_view(self.filtered_source_files[0], 0)
        elif self.current_source:
            # Maintain current selection if it's in the filtered list
            if self.current_source in self.filtered_source_files:
                idx = self.filtered_source_files.index(self.current_source)
                self.file_combo.current(idx)
            else:
                # Still show it in the combobox text area, even if not in the dropdown
                self.file_combo.set(os.path.basename(self.current_source))

    def _on_file_selected(self, event):
        idx = self.file_combo.current()
        if 0 <= idx < len(self.filtered_source_files):
            fullname = self.filtered_source_files[idx]
            self._update_source_view(fullname, 0)

    def _on_file_tree_double_click(self, event):
        item = self.files_tree.identify_row(event.y)
        if item:
            path = self.files_tree.item(item)['values'][0]
            self._update_source_view(path, 0)

    def set_gdb_server(self):
        d = tk.Toplevel(self)
        d.title("GDB Server Settings")
        d.geometry("400x420")

        ttk.Label(d, text="GDB Server Address (host:port):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.gdb_server_address).pack(fill=tk.X, padx=5)

        def test_connection():
            address = self.gdb_server_address.get()
            try:
                host_port = address.split(':')
                if len(host_port) == 2:
                    host, port = host_port
                    if host == "localhost": host = "127.0.0.1"
                    if self._is_port_in_use(port):
                        messagebox.showinfo("Test Connection", f"SUCCESS: Port {port} on {host} is OPEN and reachable.")
                    else:
                        messagebox.showwarning("Test Connection", f"FAILED: Port {port} on {host} is CLOSED.\n\nEnsure your GDB server is running and listening on this port.")
                else:
                    messagebox.showerror("Test Connection", "Invalid address format. Use host:port (e.g., localhost:3333)")
            except Exception as e:
                messagebox.showerror("Test Connection", f"Error testing connection: {e}")

        ttk.Button(d, text="Test Connection", command=test_connection).pack(pady=5)

        ttk.Label(d, text="GDB Architecture (e.g., armv7e-m, armv6-m, auto):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.gdb_architecture).pack(fill=tk.X, padx=5)
        ttk.Label(d, text="Common values: armv7e-m (M4F/M7), armv7-m (M3), armv6-m (M0)",
                  font=('Segoe UI', 8)).pack(padx=5, anchor=tk.W)

        ttk.Label(d, text="Target Description File (XML, optional):").pack(padx=5, pady=2, anchor=tk.W)
        tdesc_frame = ttk.Frame(d)
        tdesc_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(tdesc_frame, textvariable=self.gdb_tdesc_file).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(tdesc_frame, text="Browse", command=lambda: self.gdb_tdesc_file.set(
            filedialog.askopenfilename(filetypes=[("XML", "*.xml"), ("All files", "*.*")]))).pack(side=tk.RIGHT)

        ttk.Label(d, text="Pre-connect commands (comma separated):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.gdb_preconnect_cmds).pack(fill=tk.X, padx=5)
        ttk.Label(d, text="Example: set remote register-packet-size fixed, set remote g-packet-max-size 1024",
                  font=('Segoe UI', 8)).pack(padx=5, anchor=tk.W)

        def save():
            self._update_connect_menu()
            self.log(f"GDB Settings updated: {self.gdb_server_address.get()}, Arch: {self.gdb_architecture.get()}")
            d.destroy()

        btn_frame = ttk.Frame(d)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=d.destroy).pack(side=tk.LEFT, padx=5)

    def set_jlink_settings(self):
        d = tk.Toplevel(self)
        d.title("J-Link Server Settings")
        d.geometry("450x380")

        ttk.Label(d, text="J-Link Server Path:").pack(padx=5, pady=2, anchor=tk.W)
        path_frame = ttk.Frame(d)
        path_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(path_frame, textvariable=self.jlink_server_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="Browse", command=lambda: self.jlink_server_path.set(
            filedialog.askopenfilename(filetypes=[("Executable", "*.exe;*"), ("All files", "*.*")]))).pack(side=tk.RIGHT)

        ttk.Label(d, text="Device:").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.jlink_device).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="Interface:").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Combobox(d, textvariable=self.jlink_interface, values=["SWD", "JTAG"]).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="Speed (kHz):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.jlink_speed).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="J-Link Script:").pack(padx=5, pady=2, anchor=tk.W)
        script_frame = ttk.Frame(d)
        script_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(script_frame, textvariable=self.jlink_script).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(script_frame, text="Browse", command=self.load_jlink_script).pack(side=tk.RIGHT)

        ttk.Button(d, text="Close", command=d.destroy).pack(pady=10)

    def load_jlink_script(self):
        path = filedialog.askopenfilename(filetypes=[("J-Link Script", "*.jlinkscript"), ("All files", "*.*")])
        if path:
            self.jlink_script.set(path)
            self.log(f"J-Link script set to: {path}")

    def set_openocd_settings(self):
        d = tk.Toplevel(self)
        d.title("OpenOCD Server Settings")
        d.geometry("400x200")

        ttk.Label(d, text="OpenOCD Path:").pack(padx=5, pady=2, anchor=tk.W)
        path_frame = ttk.Frame(d)
        path_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(path_frame, textvariable=self.openocd_server_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="Browse", command=lambda: self.openocd_server_path.set(
            filedialog.askopenfilename(filetypes=[("Executable", "*.exe;*"), ("All files", "*.*")]))).pack(side=tk.RIGHT)

        ttk.Label(d, text="Config File:").pack(padx=5, pady=2, anchor=tk.W)
        cfg_frame = ttk.Frame(d)
        cfg_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(cfg_frame, textvariable=self.openocd_config_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cfg_frame, text="Browse", command=lambda: self.openocd_config_path.set(
            filedialog.askopenfilename(filetypes=[("Config", "*.cfg"), ("All files", "*.*")]))).pack(side=tk.RIGHT)

        ttk.Button(d, text="Close", command=d.destroy).pack(pady=10)

    def set_stlink_settings(self):
        d = tk.Toplevel(self)
        d.title("ST-LINK GDB Server Settings")
        d.geometry("500x480")
        d.resizable(True, True)

        ttk.Label(d, text="ST-LINK_gdbserver Path:").pack(padx=5, pady=2, anchor=tk.W)
        path_frame = ttk.Frame(d)
        path_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(path_frame, textvariable=self.stlink_server_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="Browse", command=lambda: self.stlink_server_path.set(
            filedialog.askopenfilename(title="Select ST-LINK_gdbserver",
                filetypes=[("Executable", "*.exe;*"), ("All files", "*.*")])
        )).pack(side=tk.RIGHT)

        ttk.Label(d, text="STM32CubeProgrammer Path (-cp):").pack(padx=5, pady=2, anchor=tk.W)
        cp_frame = ttk.Frame(d)
        cp_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(cp_frame, textvariable=self.stlink_cubeprogrammer_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cp_frame, text="Browse", command=lambda: self.stlink_cubeprogrammer_path.set(
            filedialog.askdirectory(title="Select STM32CubeProgrammer Installation Directory")
        )).pack(side=tk.RIGHT)

        ttk.Label(d, text="Frequency (kHz) [--frequency]:").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.stlink_frequency).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="ST-LINK Serial Number [-i] (leave blank for first found):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.stlink_serial).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="Target MCU Device [-d] (e.g. STM32F411xE, leave blank for auto):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.stlink_device).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="AP Index [-m] (0 = default core, use for multi-core):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=self.stlink_apid).pack(fill=tk.X, padx=5)

        options_frame = ttk.LabelFrame(d, text="Options")
        options_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Checkbutton(options_frame, text="Initialize device under reset [-k]",
                        variable=self.stlink_init_under_reset).pack(anchor=tk.W, padx=5, pady=2)
        ttk.Checkbutton(options_frame, text="Attach to running target, no reset [-g]",
                        variable=self.stlink_attach).pack(anchor=tk.W, padx=5, pady=2)
        ttk.Checkbutton(options_frame, text="Persistent mode - keep server alive [-e]",
                        variable=self.stlink_persistent).pack(anchor=tk.W, padx=5, pady=2)

        ttk.Button(d, text="Close", command=d.destroy).pack(pady=10)

    def connect_stlink_auto(self):
        self.debug_log("Starting ST-LINK auto-connect flow", "info")
        port_str = self.gdb_server_address.get().split(':')[-1]
        try:
            port = int(port_str)
        except ValueError:
            port = 61234

        # Kill any existing ST-LINK gdbserver to avoid port conflicts and stale sessions
        if self._is_port_in_use(port):
             self.debug_log(f"Port {port} in use, attempting to kill existing process...", "warn")
             if os.name != 'nt':
                 try:
                     subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
                 except: pass
             time.sleep(0.5)

        self.debug_log(f"Checking if port {port} is in use...", "info")
        if self._is_port_in_use(port):
            self.debug_log(f"Port {port} IS in use.", "info")
            if self.stlink_process and self.stlink_process.poll() is None:
                self.log(f"ST-LINK Server already running on port {port}. Connecting...")
                self.connect_target(self.gdb_server_address.get())
                return
            else:
                self.debug_log(f"Port {port} occupied by external process.", "error")
                resp = messagebox.askyesno("Port in Use",
                    f"Port {port} is already in use by another process.\n"
                    "Do you want to try connecting to it anyway?\n\n"
                    "Select 'No' to cancel.")
                if resp:
                    self.connect_target(self.gdb_server_address.get())
                return

        self.debug_log(f"Port {port} is free.", "info")
        server_path = self.stlink_server_path.get()

        if os.path.basename(server_path) == server_path:
            found_path = shutil.which(server_path)
            if found_path:
                server_path = found_path

        if not os.path.exists(server_path) and not shutil.which(server_path):
            self._show_error("ST-LINK Error",
                f"ST-LINK_gdbserver not found at: {server_path}\n"
                "Please install STM32CubeIDE or STM32CubeCLT and set the correct path\n"
                "in File → ST-LINK Server Settings.")
            return

        cmd = [server_path, "-p", str(port)]

        freq = self.stlink_frequency.get().strip()
        if freq:
            cmd.extend(["--frequency", freq])

        serial = self.stlink_serial.get().strip()
        if serial:
            cmd.extend(["-i", serial])

        apid = self.stlink_apid.get().strip()
        if apid and apid != "0":
            cmd.extend(["-m", apid])

        if self.stlink_init_under_reset.get() and not self.stlink_attach.get():
            cmd.append("-k")
        else:
            # Always use -k if not attaching, as it's more reliable for STM32CubeIDE gdbserver
            if not self.stlink_attach.get():
                cmd.append("-k")

        if self.stlink_attach.get():
            cmd.append("-g")

        if self.stlink_persistent.get():
            cmd.append("-e")

        cubeprog = self.stlink_cubeprogrammer_path.get().strip()
        cubeprog_bin = ""
        if cubeprog:
            # Check if this path contains STM32_Programmer_CLI
            exe_name = "STM32_Programmer_CLI.exe" if os.name == 'nt' else "STM32_Programmer_CLI"
            if os.path.isdir(cubeprog):
                if os.path.exists(os.path.join(cubeprog, exe_name)):
                    cubeprog_bin = cubeprog
                elif os.path.exists(os.path.join(cubeprog, "bin", exe_name)):
                    cubeprog_bin = os.path.join(cubeprog, "bin")
                elif os.path.exists(cubeprog):
                    cubeprog_bin = cubeprog
            elif os.path.isfile(cubeprog) and os.path.basename(cubeprog) == exe_name:
                cubeprog_bin = os.path.dirname(cubeprog)
            elif os.path.exists(cubeprog):
                cubeprog_bin = cubeprog

        if cubeprog_bin:
            cmd.extend(["-cp", cubeprog_bin])

        # Enable SWD mode by default for STM32, as it's the most common
        # NOTE: In version 7.11.0, -d is used for SWD
        cmd.append("-d")

        device = self.stlink_device.get().strip()
        if not device and cubeprog_bin:
            # Try to auto-detect device using STM32_Programmer_CLI
            cli_path = os.path.join(cubeprog_bin, "STM32_Programmer_CLI.exe" if os.name == 'nt' else "STM32_Programmer_CLI")
            if os.path.exists(cli_path):
                self.log(f"Attempting to auto-detect target MCU using {cli_path}...")
                detect_cmd = [cli_path, "-c", "port=SWD"]
                if serial:
                    detect_cmd.extend(["sn=" + serial])
                detect_cmd.append("-q")

                try:
                    # Run it and capture output
                    res = subprocess.run(detect_cmd, capture_output=True, text=True, timeout=10)
                    self.debug_log(f"CLI output: {res.stdout}", "info")
                    match = re.search(r"Device name\s*:\s*(STM32[A-Z0-9x/]+)", res.stdout)
                    if match:
                        device = match.group(1)
                        # Normalize common patterns, e.g. STM32F411xC/E -> STM32F411xE
                        if "/" in device:
                            parts = device.split("/")
                            base = parts[0]
                            ext = parts[-1]
                            if base.endswith("x"):
                                device = base + ext
                            else:
                                device = base + ext
                        self.log(f"Auto-detected MCU: {device}")
                    else:
                        self.log("Auto-detection failed: Device name not found in CLI output.")
                except Exception as e:
                    self.log(f"Auto-detection failed: {e}")

        # In this version of STLink-gdb-server, -d is a switch for SWD mode (already added above),
        # not for specifying a device name. Specifying --memory-map <device> causes the server
        # to print the map and exit immediately (code 0).
        # For now, we rely on auto-detection or the server's internal logic.
        if not device:
            self.log("Warning: No target MCU specified or detected. ST-LINK Server might fail.")

        progress = ConnectionProgress(self, "Connecting to ST-LINK")
        progress.update(10, "Starting ST-LINK GDB server process...")

        self.log(f"Starting ST-LINK GDB Server: {' '.join(cmd)}")
        try:
            self.stlink_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            self._read_server_output(self.stlink_process)

            def check_and_connect(attempts=15):
                if not self.stlink_process:
                    progress.close()
                    return

                if self.stlink_process.poll() is not None:
                    # (Error handling code...)
                    pass

                # Version 7.11.0 is very picky. Some versions exit on port probes.
                # However, the user's setup seems to require the probe to succeed or 
                # at least the server to stay alive.
                # If we've seen the "Waiting" message, we consider it ready.
                output = "\n".join(getattr(self.stlink_process, 'stdout_buffer', []))
                if "Waiting for debugger connection..." in output:
                    progress.update(60, "ST-LINK Server ready (from output). Starting stabilization delay...")
                    self.log("ST-LINK Server ready (detected via output). Waiting 3s for stabilization...")
                    self.after(3000, lambda: self._do_connect_after_server_ready(port, progress))
                    return

                if attempts > 0:
                    val = 10 + (15 - attempts) * 3
                    progress.update(val, f"Waiting for ST-LINK Server... ({attempts} attempts left)")
                    self.log(f"Waiting for ST-LINK Server to initialize... ({attempts} attempts left)")
                    self.after(1000, lambda: check_and_connect(attempts - 1))
                else:
                    # Fallback if output check fails but process is still running
                    progress.update(60, "ST-LINK Server timeout check. Trying connection anyway...")
                    self.log("ST-LINK Server initialize timeout. Attempting connection anyway...")
                    self._do_connect_after_server_ready(port, progress)

            check_and_connect()
        except Exception as e:
            progress.close()
            self.log(f"Failed to start ST-LINK Server: {e}")
            self._show_error("ST-LINK Error", f"Failed to start ST-LINK GDB Server:\n{e}")

    def _do_connect_after_server_ready(self, port, progress):
        post_cmd = None if self.stlink_attach.get() else "monitor reset halt"
        address = self.gdb_server_address.get()
        # Ensure address matches the port we just started the server on if it's localhost
        if "localhost" in address or "127.0.0.1" in address:
            address = f"127.0.0.1:{port}"

        self.connect_target(address,
                            post_connect_cmd=post_cmd,
                            progress=progress)

    def set_colors(self):
        from tkinter import colorchooser
        d = tk.Toplevel(self)
        d.title("Color Settings")
        d.geometry("350x300")

        def choose_color(var, tag_name=None, is_bg=False, is_text_attr=False):
            color = colorchooser.askcolor(initialcolor=var.get(), title=f"Choose Color")[1]
            if color:
                var.set(color)
                if tag_name:
                    if is_bg:
                        self.source_text.tag_configure(tag_name, background=color)
                        self.line_numbers.tag_configure(tag_name, background=color)
                    else:
                        self.source_text.tag_configure(tag_name, foreground=color)
                if is_text_attr:
                    if tag_name == "source_fg":
                        self.source_text.config(foreground=color)
                    elif tag_name == "source_bg":
                        self.source_text.config(background=color)
                self._refresh_source_tags()

        rows = [
            ("Comments:", self.color_comments, "comment", False, False),
            ("Code Foreground:", self.color_code_fg, "source_fg", False, True),
            ("Code Background:", self.color_code_bg, "source_bg", True, True),
            ("Breakpoints:", self.color_breakpoint, "breakpoint", True, False),
            ("Current Line:", self.color_current_line, "current_line", True, False),
        ]

        for label, var, tag, is_bg, is_attr in rows:
            f = ttk.Frame(d)
            f.pack(fill=tk.X, padx=10, pady=5)
            ttk.Label(f, text=label, width=20).pack(side=tk.LEFT)
            sample = tk.Frame(f, width=20, height=20, background=var.get(), relief=tk.RAISED, borderwidth=1)
            sample.pack(side=tk.LEFT, padx=5)

            def make_cmd(v=var, t=tag, b=is_bg, a=is_attr, s=sample):
                choose_color(v, t, b, a)
                s.config(background=v.get())

            ttk.Button(f, text="Set", width=5, command=make_cmd).pack(side=tk.RIGHT)

        ttk.Button(d, text="Close", command=d.destroy).pack(pady=10)

    def _show_error(self, title, message):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry("500x300")
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        label = ttk.Label(frame, text=title, font=("Helvetica", 12, "bold"))
        label.pack(pady=5, anchor=tk.W)

        text = tk.Text(frame, height=10, wrap=tk.WORD)
        text.insert("1.0", message)
        text.config(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=5)

        def copy():
            self.clipboard_clear()
            self.clipboard_append(message)
            messagebox.showinfo("Copied", "Error message copied to clipboard.")

        ttk.Button(btn_frame, text="Copy to Clipboard", command=copy).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="OK", command=dialog.destroy).pack(side=tk.RIGHT, padx=5)

        self.wait_window(dialog)

    def _update_connect_menu(self):
        self.connect_menu.delete(0, tk.END)
        addr = self.gdb_server_address.get()
        self.connect_menu.add_command(label=f"SEGGER J-Link (Auto-start Server)", command=self.connect_jlink_auto)
        self.connect_menu.add_command(label=f"OpenOCD (Auto-start Server)", command=self.connect_openocd_auto)
        self.connect_menu.add_command(label=f"ST-LINK (Auto-start Server)", command=self.connect_stlink_auto)
        self.connect_menu.add_separator()
        self.connect_menu.add_command(label=f"Generic GDB Server ({addr})", command=lambda: self.connect_target(addr))
        self.connect_menu.add_command(label=f"Existing OpenOCD ({addr})", command=lambda: self.connect_target(addr))

    def _read_server_output(self, process):
        if not process:
            return

        # Store output buffers to capture what reader threads get
        process.stdout_buffer = []
        process.stderr_buffer = []

        def reader(pipe, prefix, buffer):
            try:
                while True:
                    if not pipe:
                        break
                    line = pipe.readline()
                    if not line:
                        break

                    line_stripped = line.strip()
                    buffer.append(line_stripped)
                    # Use after() to update UI from thread
                    self.after(0, lambda l=line_stripped, p=prefix: self.log(f"{p}{l}"))
            except (EOFError, ValueError, OSError):
                pass
            except Exception as e:
                self.after(0, lambda: self.log(f"DEBUG: Server output reader error: {e}"))
            finally:
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass

        # Use separate threads for stdout and stderr as selectors on Windows
        # can be problematic with non-socket objects (pipes).
        t1 = threading.Thread(target=reader, args=(process.stdout, "SERVER OUT: ", process.stdout_buffer), daemon=True)
        t2 = threading.Thread(target=reader, args=(process.stderr, "SERVER ERR: ", process.stderr_buffer), daemon=True)
        t1.start()
        t2.start()

    def _is_port_in_use(self, port):
        """Checks if a local TCP port is in use."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                return s.connect_ex(('127.0.0.1', int(port))) == 0
        except Exception:
            return False

    def connect_jlink_auto(self):
        self.debug_log("Starting J-Link auto-connect flow", "info")
        # Check if port is already in use
        port_str = self.gdb_server_address.get().split(':')[-1]
        try:
            port = int(port_str)
        except ValueError:
            port = 3333

        self.debug_log(f"Checking if port {port} is in use...", "info")
        if self._is_port_in_use(port):
            self.debug_log(f"Port {port} IS in use.", "info")
            # Port is in use. Is it our process?
            if self.jlink_process and self.jlink_process.poll() is None:
                self.log(f"J-Link Server already running on port {port}. Connecting...")
                self.connect_target(self.gdb_server_address.get())
                return
            else:
                # Port in use by someone else or a ghost process
                self.debug_log(f"Port {port} occupied by external process.", "error")
                resp = messagebox.askyesno("Port in Use",
                    f"Port {port} is already in use by another process (likely a previous session).\n"
                    "Do you want to try connecting to it anyway?\n\n"
                    "Select 'No' to cancel and check for ghost processes.")
                if resp:
                    self.connect_target(self.gdb_server_address.get())
                return

        self.debug_log(f"Port {port} is free.", "info")
        server_path = self.jlink_server_path.get()
        # If the path is just the filename, try to find it in the system's PATH
        if os.path.basename(server_path) == server_path:
            found_path = shutil.which(server_path)
            if found_path:
                server_path = found_path
            else:
                # If it's not in the PATH, try searching in some common directories
                common_paths = [
                    r"C:\Program Files\SEGGER\JLink_V926\JLinkGDBServerCL.exe",
                    r"C:\Program Files\SEGGER\JLink\JLinkGDBServerCL.exe",
                    r"C:\Program Files (x86)\SEGGER\JLink\JLinkGDBServerCL.exe",
                    "/usr/bin/JLinkGDBServerCLExe",
                    "/opt/SEGGER/JLink/JLinkGDBServerCLExe"
                ]
                # Also check all SEGGER folders for versioned directories
                segger_root = r"C:\Program Files\SEGGER"
                if os.path.exists(segger_root):
                    try:
                        for entry in os.listdir(segger_root):
                            if entry.startswith("JLink"):
                                p = os.path.join(segger_root, entry, "JLinkGDBServerCL.exe")
                                if p not in common_paths:
                                    common_paths.append(p)
                    except Exception:
                        pass

                for p in common_paths:
                    if os.path.exists(p):
                        server_path = p
                        self.jlink_server_path.set(p) # Update the setting
                        break

        if not os.path.exists(server_path) and not shutil.which(server_path):
            self._show_error("J-Link Error", f"J-Link Server not found at: {server_path}\nPlease download and add it to your PATH or specify the correct path.")
            return

        device = self.jlink_device.get()
        interface = self.jlink_interface.get()
        speed = self.jlink_speed.get()
        port = self.gdb_server_address.get().split(':')[-1]
        script = self.jlink_script.get()

        cmd = [
            server_path,
            "-device", device,
            "-if", interface.lower(),
            "-speed", speed,
            "-port", port,
            "-silent",
            "-singlerun"
        ]

        if script and os.path.exists(script):
            cmd.extend(["-jlinkscriptfile", script])

        # Show progress bar
        progress = ConnectionProgress(self, "Connecting to J-Link")
        progress.update(10, "Starting J-Link process...")

        self.log(f"Starting J-Link Server: {' '.join(cmd)}")
        try:
            self.jlink_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            # Start threads to log server output
            self._read_server_output(self.jlink_process)

            # Give it more time and check if it's still alive
            def check_and_connect(attempts=10):
                if not self.jlink_process:
                    progress.close()
                    return

                if self.jlink_process.poll() is not None:
                    progress.close()
                    # Server died immediately
                    # Give reader threads a tiny bit of time to catch up
                    import time
                    time.sleep(0.1)

                    stdout_captured = "\n".join(getattr(self.jlink_process, 'stdout_buffer', []))
                    stderr_captured = "\n".join(getattr(self.jlink_process, 'stderr_buffer', []))

                    if not stdout_captured or not stderr_captured:
                        try:
                            # Try one last read if buffers are empty
                            out, err = self.jlink_process.communicate(timeout=0.5)
                            stdout_captured = stdout_captured or out
                            stderr_captured = stderr_captured or err
                        except Exception:
                            pass

                    err_msg = f"J-Link Server exited immediately with code {self.jlink_process.returncode}\n"
                    err_msg += f"STDOUT: {stdout_captured or 'None'}\n"
                    err_msg += f"STDERR: {stderr_captured or 'None'}"

                    self.log(err_msg)
                    self._show_error("J-Link Error", err_msg)
                    return

                if attempts > 0:
                    val = 10 + (5 - attempts) * 10
                    progress.update(val, f"Waiting for J-Link Server to initialize... ({attempts} attempts left)")
                    self.log(f"Waiting for J-Link Server to initialize... ({attempts} attempts left)")
                    self.after(1000, lambda: check_and_connect(attempts - 1))
                else:
                    progress.update(60, "J-Link Server initialized. Connecting GDB...")
                    self.log("J-Link Server initialized. Connecting GDB and resetting target...")
                    self.connect_target(self.gdb_server_address.get(), post_connect_cmd="monitor reset halt", progress=progress)

            check_and_connect()
        except Exception as e:
            progress.close()
            self.log(f"Failed to start J-Link Server: {e}")
            self._show_error("J-Link Error", f"Failed to start J-Link Server:\n{e}")

    def connect_openocd_auto(self):
        self.debug_log("Starting OpenOCD auto-connect flow", "info")
        # Check if port is already in use
        port_str = self.gdb_server_address.get().split(':')[-1]
        try:
            port = int(port_str)
        except ValueError:
            port = 3333

        self.debug_log(f"Checking if port {port} is in use...", "info")
        if self._is_port_in_use(port):
            self.debug_log(f"Port {port} IS in use.", "info")
            if self.openocd_process and self.openocd_process.poll() is None:
                self.log(f"OpenOCD already running on port {port}. Connecting...")
                self.connect_target(self.gdb_server_address.get())
                return
            else:
                self.debug_log(f"Port {port} occupied by external process.", "error")
                resp = messagebox.askyesno("Port in Use",
                    f"Port {port} is already in use.\n"
                    "Do you want to try connecting to it anyway?")
                if resp:
                    self.connect_target(self.gdb_server_address.get())
                return

        self.debug_log(f"Port {port} is free.", "info")
        server_path = self.openocd_server_path.get()
        config_path = self.openocd_config_path.get()

        if not os.path.exists(server_path) and not shutil.which(server_path):
            self._show_error("OpenOCD Error", f"OpenOCD not found at: {server_path}")
            return

        if not os.path.exists(config_path):
            self._show_error("OpenOCD Error", f"Config file not found: {config_path}")
            return

        # Show progress bar
        progress = ConnectionProgress(self, "Connecting to OpenOCD")
        progress.update(10, "Starting OpenOCD process...")

        cmd = [server_path, "-f", config_path]

        self.log(f"Starting OpenOCD: {' '.join(cmd)}")
        try:
            self.openocd_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            self._read_server_output(self.openocd_process)

            def check_and_connect(attempts=10):
                if not self.openocd_process:
                    progress.close()
                    return

                if self.openocd_process.poll() is not None:
                    progress.close()
                    # Server died immediately
                    import time
                    time.sleep(0.1)

                    stdout_captured = "\n".join(getattr(self.openocd_process, 'stdout_buffer', []))
                    stderr_captured = "\n".join(getattr(self.openocd_process, 'stderr_buffer', []))

                    if not stdout_captured or not stderr_captured:
                        try:
                            out, err = self.openocd_process.communicate(timeout=0.5)
                            stdout_captured = stdout_captured or out
                            stderr_captured = stderr_captured or err
                        except Exception:
                            pass

                    err_msg = f"OpenOCD exited immediately with code {self.openocd_process.returncode}\n"
                    err_msg += f"STDOUT: {stdout_captured or 'None'}\n"
                    err_msg += f"STDERR: {stderr_captured or 'None'}"

                    self.log(err_msg)
                    self._show_error("OpenOCD Error", err_msg)
                    return

                if attempts > 0:
                    val = 10 + (5 - attempts) * 10
                    progress.update(val, f"Waiting for OpenOCD to initialize... ({attempts} attempts left)")
                    self.log(f"Waiting for OpenOCD to initialize... ({attempts} attempts left)")
                    self.after(1000, lambda: check_and_connect(attempts - 1))
                else:
                    progress.update(60, "OpenOCD initialized. Connecting GDB...")
                    self.log("OpenOCD initialized. Connecting GDB and resetting target...")
                    self.connect_target(self.gdb_server_address.get(), post_connect_cmd="monitor reset halt", progress=progress)

            check_and_connect()
        except Exception as e:
            progress.close()
            self.log(f"Failed to start OpenOCD: {e}")
            self._show_error("OpenOCD Error", f"Failed to start OpenOCD:\n{e}")

    def connect_target(self, address=None, post_connect_cmd=None, progress=None):
        if self.is_connecting:
            self.debug_log("Connection already in progress. Ignoring request.", "warn")
            return

        self.is_connecting = True

        if address is None:
            address = self.gdb_server_address.get()

        # Normalize localhost to 127.0.0.1 for more reliable resolution
        address = address.replace("localhost", "127.0.0.1")

        self.debug_log(f"Target connection requested for {address}", "info")
        self.log(f"Connecting to target at {address} (OS: {self.os_type.get()})...")
        self.status_bar.config(text=f"Connecting to {address}...")

        # Set remotetimeout explicitly before connection to avoid timeouts
        self.debug_log("Setting remotetimeout to 30...", "info")
        self.gdb.send_command('interpreter-exec console "set remotetimeout 30"')

        if progress:
            progress.update(70, f"Connecting GDB to {address}...")
        else:
            # If manually connecting, show a quick progress bar
            progress = ConnectionProgress(self, "Connecting to Target")
            progress.update(70, f"Connecting GDB to {address}...")

        # Diagnostic: Check if port is open before connecting
        # (Removed check as it gives false negatives with some gdbserver versions)

        def on_connect(result_class, rest, retry_count=5):
            def handle_on_connect():
                self.debug_log(f"Connection result: {result_class} {rest}", "mi-recv" if result_class in ("^done", "^connected") else "error")
                if result_class in ("^done", "^connected"):
                    self.target_connected = True
                    self.gdb.target_connected = True
                    self.is_connecting = False
                    self.status_bar.config(text=f"Connected to {address}", foreground="green")
                    self.log(f"Connected to {address} successfully.")

                    # Clear register cache now that we are connected
                    self.gdb.send_command('interpreter-exec console "maint flush register-cache"')

                    progress.update(90, "Finalizing connection...")

                    if post_connect_cmd:
                        self.debug_log(f"Sending post-connection command: {post_connect_cmd}", "info")
                        if post_connect_cmd == "monitor reset halt":
                            self.reset_target(run_to_main=True)
                        else:
                            self.gdb.send_command(f'interpreter-exec console "{post_connect_cmd}"')

                    progress.update(100, "Connected.")
                    self.after(500, progress.close)
                    messagebox.showinfo("Connect Target", f"Successfully connected to target at {address}")
                else:
                    if ("undocumented errno 10061" in rest or "Connection timed out" in rest or "You can't do that when your target is `exec'" in rest or "Connection reset by peer" in rest) and retry_count > 0:
                        self.log(f"Connection issue detected ('{rest}'). Retrying in 2 seconds... ({retry_count} retries left)")
                        progress.update(70, f"Retrying connection ({retry_count})...")
                        self.after(2000, lambda: self.gdb.send_command(f"-target-select remote {address}",
                                                                      lambda rc, r: on_connect(rc, r, retry_count - 1)))
                    else:
                        self.target_connected = False
                        self.gdb.target_connected = False
                        self.is_connecting = False
                        self.status_bar.config(text="Connection Failed", foreground="red")
                        self.log(f"Connection failed: {rest}")
                        progress.close()

                        # Enhanced error reporting
                        if "Truncated register" in rest:
                            msg = f"Failed to connect to target at {address}\n\n"
                            msg += f"Error: {rest}\n\n"
                            msg += "This error usually means GDB's architecture setting doesn't match the target hardware.\n\n"
                            msg += "Steps to resolve:\n"
                            msg += "1. Go to GDB Server Settings and ensure 'Architecture' is correct (e.g., 'armv7e-m' for Cortex-M4F).\n"
                            msg += "2. Verify you are using an ARM-compatible GDB (like arm-none-eabi-gdb).\n"
                            msg += "3. If using J-Link, ensure your J-Link software is up to date."
                            self._show_error("Connect Target - Truncated Register Error", msg)
                        elif "Connection timed out" in rest or "Connection reset by peer" in rest:
                            msg = f"Failed to connect to target at {address}\n\n"
                            msg += f"Error: {rest}\n\n"
                            msg += "The connection was refused or reset. This usually means the GDB server is not fully ready, already has a connection, or is unreachable.\n\n"
                            msg += "Steps to resolve:\n"
                            msg += "1. Verify your GDB server (J-Link, ST-LINK, etc.) is running and listening on the correct port.\n"
                            msg += "2. Ensure no other debugger or GDB instance is already connected to this server.\n"
                            msg += "3. If using ST-LINK, ensure you are using the STMicroelectronics version of GDB (arm-none-eabi-gdb from STM32CubeCLT).\n"
                            msg += "4. Try increasing the 'remotetimeout' in Pre-connect commands (e.g., 'set remotetimeout 20')."
                            self._show_error("Connect Target - Connection Error", msg)
                        else:
                            self._show_error("Connect Target", f"Failed to connect to target at {address}\n{rest}")

            self.after(0, handle_on_connect)

        self.gdb.start()

        # Sequentially set up environment before connecting
        setup_cmds = []

        # Architecture
        arch = self.gdb_architecture.get()
        if arch and arch.lower() != "auto":
            setup_cmds.append(f"-gdb-set architecture {arch}")

        # Target description
        tdesc = self.gdb_tdesc_file.get()
        if tdesc:
            # We use interpreter-exec for regular GDB commands not directly in MI
            setup_cmds.append(f'interpreter-exec console "set tdesc filename {tdesc}"')

        # Custom pre-connect commands
        custom_cmds = self.gdb_preconnect_cmds.get().split(',')
        for cmd in custom_cmds:
            cmd = cmd.strip()
            if cmd:
                if cmd.startswith('-'):
                    setup_cmds.append(cmd)
                else:
                    setup_cmds.append(f'interpreter-exec console "{cmd}"')

        def run_setup(cmds):
            if not cmds:
                progress.update(80, "Connecting...")
                self.gdb.send_command(f"-target-select remote {address}", on_connect)
                return

            cmd = cmds.pop(0)
            self.debug_log(f"Setup command: {cmd}", "info")

            def setup_callback(result_class, rest):
                def handle_setup():
                    if result_class == "^error":
                        self.log(f"Setup command '{cmd}' failed: {rest}", "error")
                        if "Undefined set remote command" in rest:
                            self.log("Tip: This GDB version does not support this command. Remove it from Settings -> Pre-connect commands.", "info")
                    run_setup(cmds)
                self.after(0, handle_setup)

            self.gdb.send_command(cmd, setup_callback)

        run_setup(setup_cmds)

    def reset_target(self, retry_count=3, run_to_main=True, reset_cmd="monitor reset halt"):
        if not self.target_connected:
            self.log("Not connected to target. Connect first.")
            return

        self.log(f"Resetting processor (attempts remaining: {retry_count}, command: '{reset_cmd}')...")

        def on_reset(result_class, rest):
            self.debug_log(f"on_reset callback: class={result_class}, rest={rest}", "info")
            def handle_reset():
                if result_class != "^done":
                    # If the reset command itself is not supported or failed
                    if "Unknown reset option" in str(rest) or "Protocol error" in str(rest) or "not supported" in str(rest):
                        # Try alternative reset commands based on what we've already tried
                        next_cmd = None
                        if reset_cmd == "monitor reset halt":
                            next_cmd = "monitor reset"
                        elif reset_cmd == "monitor reset":
                            next_cmd = "monitor reset init"
                        elif reset_cmd == "monitor reset init":
                            next_cmd = "kill"
                        
                        if next_cmd:
                            self.log(f"Command '{reset_cmd}' failed or not supported. Trying '{next_cmd}'...")
                            self.after(200, lambda: self.reset_target(retry_count, run_to_main, next_cmd))
                            return

                    if retry_count > 1:
                        self.log(f"Reset failed: {rest}. Retrying...")
                        self.after(500, lambda: self.reset_target(retry_count - 1, run_to_main, reset_cmd))
                    else:
                        self.log(f"Reset failed after multiple attempts: {rest}")
                else:
                    self.log(f"Target reset successful (using '{reset_cmd}').")
                    self._update_ui_for_execution_state(False)

                    if run_to_main:
                        # After successful reset, set breakpoint at main and run
                        self.debug_log("Setting temporary breakpoint at main", "info")
                        # Use -t for temporary breakpoint (deleted once hit)
                        self.gdb.send_command("-break-insert -t main", bp_callback)
                    else:
                        # If not running to main, we still need to refresh the UI
                        # to show where we are (usually reset vector)
                        self._handle_exec_async("stopped")
            self.after(0, handle_reset)

        def bp_callback(result_class, rest):
            if result_class == "^done":
                self.log("Temporary breakpoint set at main. Running to main...")
                # Add a small delay to ensure GDB is ready to continue
                self.after(100, self.go)
            else:
                if "No symbol table is loaded" in str(rest) and self.elf_path:
                    self.log("Symbol table is missing. Attempting to reload ELF...")
                    # Normalize for GDB MI
                    gdb_path = self.elf_path.replace('\\', '/')
                    
                    def on_reload(rel_result_class, rel_rest):
                        if rel_result_class == "^done":
                            self.log(f"Reloaded ELF: {self.elf_path}. Retrying breakpoint at main.")
                            self.gdb.send_command("-break-insert -t main", bp_callback)
                        else:
                            self.log(f"Failed to reload ELF: {rel_rest}", "error")
                            self._handle_exec_async("stopped")
                    
                    self.gdb.send_command(f'-file-exec-and-symbols "{gdb_path}"', on_reload)
                else:
                    self.log(f"Failed to set breakpoint at main: {rest}")
                    self.log("Try checking if the correct ELF is loaded with debug symbols.")
                    # We stay halted after reset if main is not found
                    self._handle_exec_async("stopped")

        self.debug_log(f"Sending reset command: {reset_cmd}", "info")

        # Ensure we are halted before resetting for better reliability
        # But 'kill' doesn't need halting as it stops/restarts
        if self.is_running and reset_cmd != "kill":
            self.log("Target is running, interrupting before reset...")
            self.gdb.halt()
            self.after(200, lambda: self.gdb.send_command(f'interpreter-exec console "{reset_cmd}"', on_reset))
        else:
            if reset_cmd == "kill":
                self.gdb.send_command('interpreter-exec console "kill"', on_reset)
            else:
                self.gdb.send_command(f'interpreter-exec console "{reset_cmd}"', on_reset)

    def run_to_main(self):
        if not self.target_connected:
            self.log("Not connected to target. Connect first.")
            return
        
        # We explicitly request to run to main after reset
        # Some targets need a bit of time or a specific sequence
        # Try a more forceful reset sequence if needed
        self.debug_log("Starting Run to Main sequence", "info")
        self.reset_target(run_to_main=True)

    def download(self):
        self.gdb.send_command("load")
        self.log("Downloading ELF to target...")

    def go(self):
        self._reset_watch_changed_flags()
        # Some targets might report being stopped even if they are about to run
        # Let's ensure we are in the correct state
        self._update_ui_for_execution_state(True)
        # Highlight Go button and reset Pause button background
        if hasattr(self, 'toolbar_btns'):
            if 'Go' in self.toolbar_btns:
                self.toolbar_btns['Go'].config(bg='lightgreen')
            if 'Pause' in self.toolbar_btns:
                self.toolbar_btns['Pause'].config(bg=self.default_button_bg)

        self.debug_log("Sending -exec-continue", "mi-send")
        def callback(result_class, rest):
            if result_class == "^error":
                self._update_ui_for_execution_state(False)
                self.log(f"Error continuing: {rest}")
                # Try to refresh state if continue failed
                self._handle_exec_async("stopped")
        self.gdb.send_command("-exec-continue", callback)

    def pause(self):
        # We need to ensure that when we pause, we are ready to receive the 'stopped' event
        # and update the UI accordingly.
        if not self.target_connected:
            self.log("Not connected to target.")
            return

        self.log("Interrupting execution...")
        self.debug_log("Pause requested", "info")
        
        # Requirement: "When pressing pause the call stack sould be shown in its own dropdown"
        # Since we use tabs, we switch to the Call Stack tab.
        # It will be updated when GDB confirms it stopped.
        if hasattr(self, "sidebar_tabs"):
            try:
                # Find index of Call Stack tab
                for i in range(self.sidebar_tabs.index("end")):
                    if self.sidebar_tabs.tab(i, "text") == "Call Stack":
                        self.sidebar_tabs.select(i)
                        break
            except Exception as e:
                self.debug_log(f"Error switching to Call Stack tab: {e}")

        # Primary method: -exec-interrupt
        if self.gdb.halt():
            # Highlight Pause button and reset Go button background
            if hasattr(self, 'toolbar_btns'):
                if 'Pause' in self.toolbar_btns:
                    self.toolbar_btns['Pause'].config(bg='#ffcccc') # light red
                if 'Go' in self.toolbar_btns:
                    self.toolbar_btns['Go'].config(bg=self.default_button_bg)

            # If halt was successful (signal sent), we will wait for the 'stopped' async record.
            # But just in case, let's also update the status bar to show we are waiting.
            self.location_bar.config(text="Interrupting...")
            self.update() # Force UI update to show the new status immediately
            
            # Update call stack while waiting (it will fetch the last known state)
            self._update_call_stack()

            # If after 1 second we haven't stopped, try more aggressive methods
            def check_if_stopped():
                if self.target_connected and self.is_running:
                    self.debug_log("Target still running after 1s, trying signals", "warning")
                    self.gdb._send_interrupt_signals()
            
            self.after(1000, check_if_stopped)
        else:
            self.log("Failed to send interrupt signal.")

    def stop_debug(self):
        # Stop the debugger session.
        # Requirement: "must stop the debugger and jump to the line in the source code where the code stopped"
        if self.target_connected:
            self.log("Stopping debug session and fetching last known position...")

            # To "jump to the line in question" before closing, we should first pause if running,
            # wait for the stop event, and then close.
            # But usually 'Stop' means immediate termination.
            # If the user wants to see where it was, they should probably 'Halt' first.
            # However, we can try to improve this by ensuring the session stop is clean.

            self.gdb.stop_session()
        else:
            self.gdb.stop_session()

        self.target_connected = False
        self.gdb.target_connected = False
        self._update_ui_for_execution_state(False)
        # Reset Pause button color if it was red
        if hasattr(self, 'toolbar_btns') and 'Pause' in self.toolbar_btns:
            self.toolbar_btns['Pause'].config(bg=self.default_button_bg)
        self.status_bar.config(text="Disconnected", foreground="black")
        self.log("Debug session stopped.")

    def disconnect_target(self):
        self.gdb.send_command("-target-detach")
        self.target_connected = False
        self.gdb.target_connected = False
        self._update_ui_for_execution_state(False)
        # Reset Pause button color if it was red
        if hasattr(self, 'toolbar_btns') and 'Pause' in self.toolbar_btns:
            self.toolbar_btns['Pause'].config(bg=self.default_button_bg)
        self.status_bar.config(text="Disconnected", foreground="black")
        self.log("Detached from target.")

    def step(self):
        self._reset_watch_changed_flags()
        self.gdb.send_command("-exec-step")

    def step_over(self):
        self._reset_watch_changed_flags()
        self.gdb.send_command("-exec-next")

    def _on_mousewheel(self, event):
        # On Windows, event.delta is typically +/- 120
        # Scroll both widgets
        move = -1 if event.delta > 0 else 1
        self.source_text.yview_scroll(move, "units")
        self.line_numbers.yview_scroll(move, "units")
        return "break" # Prevent default behavior

    def _on_scroll(self, *args):
        self.source_text.yview(*args)
        self.line_numbers.yview(*args)

    def _on_line_click(self, event):
        # Toggle breakpoint on click
        # Use @x,y to get the line number from the line_numbers widget
        # Make sure we get the correct line even if clicked slightly to the side
        line_idx = self.line_numbers.index(f"@{event.x},{event.y}").split('.')[0]
        line = int(line_idx)
        if self.current_source:
            self.toggle_breakpoint(self.current_source, line)

    def toggle_breakpoint(self, filename, line):
        # Normalize filename
        filename = os.path.abspath(os.path.normpath(filename))

        # Check if line contains code
        line_content = self.source_text.get(f"{line}.0", f"{line}.end").strip()
        if not line_content:
            self.log(f"No code on line {line}, breakpoint not set.")
            return

        # Check if exists
        exists = False
        for bp in self.breakpoints:
            if os.path.abspath(os.path.normpath(bp['file'])) == filename and bp['line'] == line:
                self.gdb.send_command(f"-break-delete {bp['number']}")
                self.breakpoints.remove(bp)
                exists = True
                break

        if not exists:
            def bp_callback(result_class, rest):
                if result_class == "^done":
                    match = re.search(r'number="(\d+)"', rest)
                    if match:
                        self.breakpoints.append({'number': match.group(1), 'file': filename, 'line': line})
                        self._refresh_bp_tree()
                        self._refresh_source_tags()

            # Normalize path for GDB
            gdb_filename = filename.replace('\\', '/')
            self.gdb.send_command(f'-break-insert "{gdb_filename}:{line}"', bp_callback)
        else:
            self._refresh_bp_tree()
            self._refresh_source_tags()

    def _on_bp_right_click(self, event):
        item = self.bp_tree.identify_row(event.y)
        if item:
            self.bp_tree.selection_set(item)
            self.bp_menu.post(event.x_root, event.y_root)

    def delete_selected_breakpoint(self):
        selected_item = self.bp_tree.selection()
        if not selected_item:
            return

        idx = self.bp_tree.index(selected_item[0])
        if 0 <= idx < len(self.breakpoints):
            bp = self.breakpoints[idx]
            self.gdb.send_command(f"-break-delete {bp['number']}")
            self.breakpoints.pop(idx)
            self._refresh_bp_tree()
            self._refresh_source_tags()

    def delete_all_breakpoints(self):
        if not self.breakpoints:
            return

        self.gdb.send_command("-break-delete")
        self.breakpoints.clear()
        self._refresh_bp_tree()
        self._refresh_source_tags()
        self.log("All breakpoints deleted.")

    def _refresh_bp_tree(self):
        for item in self.bp_tree.get_children():
            self.bp_tree.delete(item)
        for bp in self.breakpoints:
            self.bp_tree.insert("", tk.END, values=(os.path.basename(bp['file']), bp['line']))

    def _refresh_source_tags(self):
        self.source_text.tag_remove("breakpoint", "1.0", tk.END)
        self.line_numbers.tag_remove("breakpoint", "1.0", tk.END)
        for bp in self.breakpoints:
            if bp['file'] == self.current_source:
                self.source_text.tag_add("breakpoint", f"{bp['line']}.0", f"{bp['line']}.end")
                self.line_numbers.tag_add("breakpoint", f"{bp['line']}.0", f"{bp['line']}.end")

    def add_watch(self):
        var_name = simpledialog.askstring("Add Watch", "Variable name:")
        if var_name:
            self.add_watch_with_name(var_name)

    def add_live_watch(self):
        var_name = simpledialog.askstring("Add Live Watch", "Variable name:")
        if var_name:
            self.add_live_watch_with_name(var_name)

    def add_watch_with_name(self, var_name):
        self._add_watch_generic(var_name, is_live=False)

    def add_live_watch_with_name(self, var_name):
        self._add_watch_generic(var_name, is_live=True)

    def _add_watch_generic(self, var_name, is_live=False):
        var_name = var_name.strip()
        if not var_name:
            return

        target_list = self.live_watches if is_live else self.watches
        target_tree = self.live_watch_tree if is_live else self.watch_tree
        tab_index = 3 if is_live else 2

        # Check if already added
        for w in target_list:
            if w['name'] == var_name:
                messagebox.showinfo("Duplicate Watch", f"Watch '{var_name}' already exists.")
                # Highlight in tree
                for item in target_tree.get_children(""):
                    if target_tree.item(item, "text") == var_name:
                        self.sidebar_tabs.select(tab_index)
                        target_tree.selection_set(item)
                        target_tree.see(item)
                        break
                return

        def watch_callback(result_class, rest):
            if result_class == "^done":
                match = re.search(r'name="([^"]+)"', rest)
                if match:
                    gdb_var_name = match.group(1)
                    val_match = re.search(r'value="([^"]+)"', rest)
                    val = val_match.group(1) if val_match else "?"
                    num_child_match = re.search(r'numchild="(\d+)"', rest)
                    num_children = int(num_child_match.group(1)) if num_child_match else 0

                    watch_data = {
                        'name': var_name,
                        'gdb_name': gdb_var_name,
                        'value': val,
                        'previous_value': None,
                        'changed': False,
                        'num_children': num_children,
                        'children_fetched': False
                    }
                    target_list.append(watch_data)
                    if is_live:
                        self._refresh_live_watch_tree()
                    else:
                        self._refresh_watch_tree()
            else:
                self.log(f"Failed to add watch for '{var_name}': {rest}")

        self.gdb.send_command(f'-var-create - * "{var_name}"', watch_callback)

    def _refresh_watch_tree(self):
        for item in self.watch_tree.get_children():
            self.watch_tree.delete(item)
        self._build_tree_recursive("", self.watches, self.watch_tree)

    def _refresh_live_watch_tree(self):
        for item in self.live_watch_tree.get_children():
            self.live_watch_tree.delete(item)
        self._build_tree_recursive("", self.live_watches, self.live_watch_tree)

    def _build_tree_recursive(self, parent_id, watches, tree):
        for w in watches:
            item_id = tree.insert(parent_id, tk.END, text=w['name'], values=(w['value'],), tags=(w['gdb_name'],))
            if w.get('changed'):
                tree.item(item_id, tags=(w['gdb_name'], 'changed'))
                if w['previous_value'] is not None:
                    tree.item(item_id, values=(f"{w['value']} (was: {w['previous_value']})",))

            if w['num_children'] > 0:
                if w.get('children_fetched'):
                    self._build_tree_recursive(item_id, w['children'], tree)
                else:
                    # Insert a dummy child to show the [+] expander
                    tree.insert(item_id, tk.END, text="Loading...")

    def _on_watch_expand(self, event):
        tree = event.widget
        item_id = tree.focus()
        if not item_id:
            return

        # Get gdb_name from tags
        tags = tree.item(item_id, "tags")
        if not tags:
            return
        gdb_name = tags[0]

        # Determine if it's live watch tree or normal watch tree
        is_live = (tree == self.live_watch_tree)
        target_list = self.live_watches if is_live else self.watches

        # Find watch in our list (could be a nested child)
        def find_watch(watches, gdb_name):
            for w in watches:
                if w['gdb_name'] == gdb_name:
                    return w
                if 'children' in w:
                    found = find_watch(w['children'], gdb_name)
                    if found:
                        return found
            return None

        watch = find_watch(target_list, gdb_name)
        if not watch or watch.get('children_fetched'):
            return

        def list_children_callback(result_class, rest):
            if result_class == "^done":
                # Parse children=[child={name="...",exp="...",numchild="...",type="...",value="..."...},...]
                children_data = []
                # Use regex to find all child blocks
                child_matches = re.finditer(r'child=\{name="([^"]+)",exp="([^"]+)",numchild="(\d+)"(?:,type="[^"]+")?,value="([^"]+)"', rest)
                for m in child_matches:
                    c_gdb_name, c_exp, c_numchild, c_value = m.groups()
                    children_data.append({
                        'name': c_exp,
                        'gdb_name': c_gdb_name,
                        'value': c_value,
                        'previous_value': None,
                        'changed': False,
                        'num_children': int(c_numchild),
                        'children_fetched': False
                    })

                watch['children'] = children_data
                watch['children_fetched'] = True

                # Update UI
                self.after(0, lambda: self._update_tree_with_children(item_id, children_data, tree))

        self.gdb.send_command(f"-var-list-children --all-values {gdb_name}", list_children_callback)

    def _update_tree_with_children(self, parent_id, children, tree):
        # Remove dummy child
        for child in tree.get_children(parent_id):
            tree.delete(child)

        for c in children:
            child_id = tree.insert(parent_id, tk.END, text=c['name'], values=(c['value'],), tags=(c['gdb_name'],))
            if c['num_children'] > 0:
                tree.insert(child_id, tk.END, text="Loading...")

    def _on_watch_right_click(self, event):
        item = self.watch_tree.identify_row(event.y)
        if item:
            self.watch_tree.selection_set(item)
            self.watch_menu.post(event.x_root, event.y_root)

    def _add_watch_to_live(self):
        item_ids = self.watch_tree.selection()
        if not item_ids:
            return

        item_id = item_ids[0]
        # Get the name of the variable from the treeview text
        # If it's a child, we want its full expression if possible
        tags = self.watch_tree.item(item_id, "tags")
        if not tags:
            return
        gdb_name = tags[0]

        def path_callback(result_class, rest):
            if result_class == "^done":
                expr = None
                if isinstance(rest, dict):
                    expr = rest.get("path_expr")
                elif "path_expr=" in rest:
                    import re
                    m = re.search(r'path_expr="([^"]+)"', rest)
                    if m: expr = m.group(1)

                if expr:
                    self.add_live_watch_with_name(expr)
                else:
                    # Fallback to simple name
                    var_name = self.watch_tree.item(item_id, "text")
                    if var_name:
                        self.add_live_watch_with_name(var_name)
            else:
                # Fallback to simple name
                var_name = self.watch_tree.item(item_id, "text")
                if var_name:
                    self.add_live_watch_with_name(var_name)

        self.gdb.send_command(f"-var-info-path-expression {gdb_name}", path_callback)

    def _on_live_watch_right_click(self, event):
        item = self.live_watch_tree.identify_row(event.y)
        if item:
            self.live_watch_tree.selection_set(item)
            self.live_watch_menu.post(event.x_root, event.y_root)

    def _on_live_watch_double_click(self, event):
        # Could implement edit value if needed
        pass

    def _show_memory_for_watch(self, is_live=False):
        tree = self.live_watch_tree if is_live else self.watch_tree
        item_ids = tree.selection()
        if not item_ids:
            return

        item_id = item_ids[0]
        tags = tree.item(item_id, "tags")
        if not tags:
            return
        gdb_name = tags[0]

        # We need to get the expression to evaluate its address
        # GDB var objects have names like var1, var2 etc.
        # We can use -var-info-path-expression to get the C expression

        def path_callback(result_class, rest):
            if result_class == "^done":
                # Result might be in a dict or a string
                expr = None
                if isinstance(rest, dict):
                    expr = rest.get("path_expr")
                elif "path_expr=" in rest:
                    import re
                    m = re.search(r'path_expr="([^"]+)"', rest)
                    if m: expr = m.group(1)

                if expr:
                    # Evaluate address of this expression
                    self.gdb.send_command(f'-data-evaluate-expression "&({expr})"', addr_callback)
                else:
                    # Fallback
                    self.gdb.send_command(f'-var-info-expression {gdb_name}', info_expr_callback)
            else:
                self.gdb.send_command(f'-var-info-expression {gdb_name}', info_expr_callback)

        def info_expr_callback(result_class, rest):
            if result_class == "^done":
                expr = None
                if isinstance(rest, dict):
                    expr = rest.get("exp")
                elif "exp=" in rest:
                    import re
                    m = re.search(r'exp="([^"]+)"', rest)
                    if m: expr = m.group(1)

                if expr:
                    self.gdb.send_command(f'-data-evaluate-expression "&({expr})"', addr_callback)

        def addr_callback(result_class, rest):
            if result_class == "^done":
                addr_val = None
                if isinstance(rest, dict):
                    addr_val = rest.get("value")
                elif "value=" in rest:
                    import re
                    m = re.search(r'value="([^"]+)"', rest)
                    if m: addr_val = m.group(1)

                if addr_val:
                    # Strip any GDB extra info like "(int *) 0x..." or "0x... <symbol>"
                    # Example: 0x20000000 <my_var>
                    if " <" in addr_val:
                        addr_val = addr_val.split(" <")[0]
                    if " " in addr_val:
                        addr_val = addr_val.split(" ")[-1]

                    self.mem_addr_entry.delete(0, tk.END)
                    self.mem_addr_entry.insert(0, addr_val)
                    self.sidebar_tabs.select(6) # Memory tab is index 6
                    self.read_memory()
            else:
                msg = rest.get("msg", "Unknown error") if isinstance(rest, dict) else rest
                self.log(f"Error getting address: {msg}")

        self.gdb.send_command(f"-var-info-path-expression {gdb_name}", path_callback)

    def _on_watch_double_click(self, event):
        # Identify the row and column clicked
        item_id = self.watch_tree.identify_row(event.y)
        column = self.watch_tree.identify_column(event.x)

        if not item_id or column != "#1": # Column #1 is 'Value'
            return

        tags = self.watch_tree.item(item_id, "tags")
        if not tags:
            return
        gdb_name = tags[0]

        # Get current value (without the 'was: ...' part if present)
        # We find the watch object in self.watches
        def find_watch_recursive(watches, name):
            for w in watches:
                if w['gdb_name'] == name:
                    return w
                if 'children' in w:
                    found = find_watch_recursive(w['children'], name)
                    if found: return found
            return None

        watch = find_watch_recursive(self.watches, gdb_name)
        if not watch:
            return

        current_val = watch['value']

        # Use simpledialog for editing
        new_val = simpledialog.askstring("Edit Value", f"New value for {watch['name']}:", initialvalue=current_val)

        if new_val is not None and new_val != current_val:
            self._submit_watch_value(gdb_name, new_val)

    def _submit_watch_value(self, gdb_name, new_val):
        self.log(f"Setting {gdb_name} to {new_val}...")

        def assign_callback(result_class, rest):
            if result_class == "^done":
                self.log(f"Successfully set value.")
                # After setting, update all watches to reflect changes
                self._update_watches()
            else:
                msg = rest.get("msg", "Unknown error") if isinstance(rest, dict) else rest
                self._show_error("GDB Error", f"Failed to set value: {msg}")

        # -var-assign NAME EXPRESSION
        self.gdb.send_command(f'-var-assign {gdb_name} "{new_val}"', assign_callback)

    def delete_selected_watch(self):
        self._delete_watch_generic(is_live=False)

    def delete_selected_live_watch(self):
        self._delete_watch_generic(is_live=True)

    def _delete_watch_generic(self, is_live=False):
        tree = self.live_watch_tree if is_live else self.watch_tree
        watch_list = self.live_watches if is_live else self.watches

        item_id = tree.selection()
        if not item_id:
            return

        item_id = item_id[0]
        tags = tree.item(item_id, "tags")
        if not tags:
            return
        gdb_name = tags[0]

        for i, w in enumerate(watch_list):
            if w['gdb_name'] == gdb_name:
                self.gdb.send_command(f"-var-delete {gdb_name}")
                watch_list.pop(i)
                if is_live:
                    self._refresh_live_watch_tree()
                else:
                    self._refresh_watch_tree()
                return

        parent_id = tree.parent(item_id)
        if parent_id:
            messagebox.showinfo("Delete Watch", "To delete a member, delete the parent structure.")

    def _on_source_right_click(self, event):
        try:
            # Always select the word under cursor on right-click
            # unless the click is already within an existing selection
            click_pos = self.source_text.index(f"@{event.x},{event.y}")

            has_sel = False
            try:
                sel_start = self.source_text.index("sel.first")
                sel_end = self.source_text.index("sel.last")
                if self.source_text.compare(sel_start, "<=", click_pos) and \
                   self.source_text.compare(click_pos, "<=", sel_end):
                    has_sel = True
            except tk.TclError:
                pass

            if not has_sel:
                self.source_text.tag_remove("sel", "1.0", tk.END)
                start = self.source_text.index(f"{click_pos} wordstart")
                end = self.source_text.index(f"{click_pos} wordend")
                self.source_text.tag_add("sel", start, end)
        except Exception:
            pass

        self.source_menu.post(event.x_root, event.y_root)

    def _add_selection_to_watch(self):
        self._add_selection_to_watch_generic(is_live=False)

    def _add_selection_to_live_watch(self):
        self._add_selection_to_watch_generic(is_live=True)

    def _add_selection_to_watch_generic(self, is_live=False):
        try:
            selection = self.source_text.get("sel.first", "sel.last").strip()
            if selection:
                # Sanitize selection (e.g. remove leading/trailing punctuation if word boundaries weren't perfect)
                selection = re.sub(r'^[^\w*]+|[^\w]+$', '', selection)

            title = "Add Live Watch" if is_live else "Add Watch"
            # Use dialog for better UX (pre-fill with selection)
            var_name = simpledialog.askstring(title, "Variable name:", initialvalue=selection)
            if var_name:
                if is_live:
                    self.add_live_watch_with_name(var_name)
                else:
                    self.add_watch_with_name(var_name)
        except tk.TclError:
            # If no selection, just show regular dialog
            if is_live:
                self.add_live_watch()
            else:
                self.add_watch()

    def _update_watches(self):
        def update_callback(result_class, rest):
            if result_class == "^done":
                # Parse changelist=[{name="...",value="...",in_scope="...",type_changed="..."...},...]
                # GDB MI returns a list of changed variables.
                changes = re.findall(r'name="([^"]+)",value="([^"]+)"', rest)
                if changes:
                    for gdb_name, new_val in changes:
                        self._update_watch_value_recursive(self.watches, gdb_name, new_val)
                    self._refresh_watch_tree_values()
                    self.update() # Force refresh

        self.gdb.send_command("-var-update --all-values *", update_callback)

    def _update_watch_value_recursive(self, watches, gdb_name, new_val):
        for w in watches:
            if w['gdb_name'] == gdb_name:
                # Handle value change
                if w['value'] != new_val:
                    w['previous_value'] = w['value']
                    w['value'] = new_val
                    w['changed'] = True
                else:
                    # If value is same, we might want to keep the 'changed' flag for
                    # one more cycle to let the user see it, or clear it.
                    # Here we clear it to match typical 'live' behavior.
                    w['changed'] = False
                return True
            if 'children' in w and w.get('children_fetched'):
                if self._update_watch_value_recursive(w['children'], gdb_name, new_val):
                    return True
        return False

    def _refresh_watch_tree_values(self):
        # Update values in the existing tree items without rebuilding the whole tree
        def update_item_recursive(items, watches):
            watch_map = {w['gdb_name']: w for w in watches}
            for item_id in items:
                tags = self.watch_tree.item(item_id, "tags")
                if tags:
                    gdb_name = tags[0]
                    if gdb_name in watch_map:
                        w = watch_map[gdb_name]
                        val_str = w['value']
                        item_tags = [w['gdb_name']]
                        if w.get('changed'):
                            item_tags.append('changed')
                            if w['previous_value'] is not None:
                                val_str = f"{w['value']} (was: {w['previous_value']})"

                        self.watch_tree.item(item_id, values=(val_str,), tags=tuple(item_tags))
                        if 'children' in w:
                            update_item_recursive(self.watch_tree.get_children(item_id), w['children'])

        update_item_recursive(self.watch_tree.get_children(""), self.watches)

    def _reset_watch_changed_flags(self):
        def reset_recursive(watches):
            for w in watches:
                w['changed'] = False
                w['previous_value'] = None
                if 'children' in w:
                    reset_recursive(w['children'])

        reset_recursive(self.watches)
        self._refresh_watch_tree_values()

    def read_memory(self):
        addr = self.mem_addr_entry.get().strip()
        if not addr:
            return

        # Read 256 bytes by default
        def callback(result_class, rest):
            if result_class == "^done":
                # Parse memory=[{begin="...",end="...",contents="..."}]
                match = re.search(r'contents="([0-9a-fA-F]+)"', rest)
                if match:
                    hex_data = match.group(1)
                    formatted = self._format_hex_dump(addr, hex_data)
                    self.mem_text.config(state=tk.NORMAL)
                    self.mem_text.delete('1.0', tk.END)
                    self.mem_text.insert('1.0', formatted)
                    self.mem_text.config(state=tk.DISABLED)
                    self.update() # Force refresh
                else:
                    self.log(f"Failed to read memory at {addr}: No contents in response")
            else:
                self.log(f"Failed to read memory at {addr}: {rest}")

        self.gdb.send_command(f'-data-read-memory-bytes {addr} 256', callback)

    def _format_hex_dump(self, start_addr, hex_data):
        try:
            addr_int = int(start_addr, 16)
        except ValueError:
            addr_int = 0

        rows = []
        for i in range(0, len(hex_data), 32): # 16 bytes per row (32 hex chars)
            chunk = hex_data[i:i+32]
            if not chunk:
                break

            # Hex part
            hex_part = " ".join([chunk[j:j+2] for j in range(0, len(chunk), 2)])
            # ASCII part
            ascii_part = ""
            for j in range(0, len(chunk), 2):
                byte_hex = chunk[j:j+2]
                byte_val = int(byte_hex, 16)
                if 32 <= byte_val <= 126:
                    ascii_part += chr(byte_val)
                else:
                    ascii_part += "."

            rows.append(f"{addr_int + (i//2):08X}: {hex_part:<47}  {ascii_part}")

        return "\n".join(rows)

    def _update_registers(self):
        def reg_callback(result_class, rest):
            if result_class == "^done":
                # Parse register-values=[{number="0",value="0x..."},...]
                vals = re.findall(r'number="(\d+)",value="([^"]+)"', rest)
                for item in self.reg_tree.get_children():
                    self.reg_tree.delete(item)
                for num, val in vals:
                    self.reg_tree.insert("", tk.END, text=f"r{num}", values=(val,))
                self.update() # Force refresh

        self.gdb.send_command("-data-list-register-values x", reg_callback)

    def _update_call_stack(self):
        self.debug_log("DEBUG: _update_call_stack() called", "debug")
        def stack_callback(result_class, rest):
            self.debug_log(f"DEBUG: stack_callback received: class='{result_class}', rest='{rest}'", "debug")
            def update_ui():
                if not self.winfo_exists():
                    return
                try:
                    if result_class == "^done":
                        self.debug_log(f"STACK RECV: {rest}", "mi-recv")
                        
                        frames = []
                        pos = 0
                        # Sometimes rest starts with stack=[...], if so, find start of list
                        list_match = re.search(r'stack=\[', rest)
                        if list_match:
                            pos = list_match.end() - 1 # include [
                        
                        # Robustly find frame={ or frame = {
                        while True:
                            match = re.search(r'frame\s*=\s*\{', rest[pos:])
                            if not match:
                                break
                            
                            start_idx = pos + match.end() - 1 
                            
                            brace_count = 0
                            end_idx = -1
                            for i in range(start_idx, len(rest)):
                                if rest[i] == '{':
                                    brace_count += 1
                                elif rest[i] == '}':
                                    brace_count -= 1
                                    if brace_count == 0:
                                        end_idx = i
                                        break
                        
                            if end_idx != -1:
                                frame_str = rest[start_idx+1 : end_idx]
                                frames.append(frame_str)
                                pos = end_idx + 1
                            else:
                                self.debug_log(f"STACK ERROR: Unbalanced braces in {rest[pos:pos+50]}...", "error")
                                break
                    
                        if not frames:
                            self.debug_log(f"STACK ERROR: No frames found in {rest}. Trying secondary fallback parser.", "error")
                            # Fallback to simple re.findall for frame={...} if brace balancing failed to find anything
                            frames = re.findall(r'frame=\{([^}]+)\}', rest)
                            if not frames:
                                self.debug_log("STACK ERROR: Secondary fallback also failed.", "error")

                        for item in self.stack_tree.get_children():
                            self.stack_tree.delete(item)
                    
                        for i, frame_str in enumerate(frames):
                            # More flexible field matching
                            f_match = re.search(r'fullname\s*=\s*"([^"]+)"', frame_str)
                            if not f_match:
                                f_match = re.search(r'file\s*=\s*"([^"]+)"', frame_str)
                            
                            l_match = re.search(r'line\s*=\s*"(\d+)"', frame_str)
                            fn_match = re.search(r'func\s*=\s*"([^"]+)"', frame_str)
                            lvl_match = re.search(r'level\s*=\s*"(\d+)"', frame_str)
                        
                            fullname = f_match.group(1).replace(r'\\', '\\') if f_match else ""
                            line = l_match.group(1) if l_match else ""
                            func = fn_match.group(1) if fn_match else "???"
                            level = lvl_match.group(1) if lvl_match else str(i)
                        
                            self.stack_tree.insert("", tk.END, text=level, values=(func, os.path.basename(fullname), line), tags=(fullname,))
                        
                        self.update_idletasks()
                        self.update()
                    else:
                        self.debug_log(f"STACK ERROR: {result_class} {rest}", "error")
                except (tk.TclError, AttributeError, Exception) as e:
                    try:
                        self.debug_log(f"STACK UI ERROR: {e}", "error")
                    except:
                        pass

            # Ensure UI updates happen on the main thread
            self.after(0, update_ui)

        self.debug_log("DEBUG: Sending -stack-list-frames command", "debug")
        # Use a small delay to ensure GDB has updated its internal stack state
        # Request with -stack-list-frames --high-frame 9
        # but also try a simpler command if that fails in some GDB versions
        self.after(100, lambda: self.gdb.send_command("-stack-list-frames 0 9", stack_callback))

    def _on_stack_frame_double_click(self, event):
        item = self.stack_tree.selection()[0]
        fullname = self.stack_tree.item(item, "tags")[0]
        line_str = self.stack_tree.item(item, "values")[2]
        if fullname and line_str:
            self._update_source_view(fullname, int(line_str))

    def _update_source_view(self, fullname, line, is_hit=False):
        fullname = os.path.abspath(os.path.normpath(fullname))
        if not os.path.exists(fullname):
            self.log(f"Warning: Source file not found: {fullname}")
            # Try finding it relative to ELF if it's a relative path in GDB
            if not os.path.isabs(fullname) and self.elf_path:
                elf_dir = os.path.dirname(self.elf_path)
                alt_path = os.path.abspath(os.path.normpath(os.path.join(elf_dir, fullname)))
                if os.path.exists(alt_path):
                    fullname = alt_path
                else:
                    return
            else:
                return

        if self.current_source != fullname:
            self.current_source = fullname
            # Update combobox if needed
            if fullname in self.filtered_source_files:
                idx = self.filtered_source_files.index(fullname)
                self.file_combo.current(idx)
            else:
                self.file_combo.set(os.path.basename(fullname))

            # Update Tree selection if needed
            if hasattr(self, 'files_tree'):
                for item in self.files_tree.get_children():
                    if self.files_tree.item(item)['values'][0] == fullname:
                        self.files_tree.selection_set(item)
                        self.files_tree.see(item)
                        break

            with open(fullname, 'r') as f:
                lines = f.readlines()

            self.source_text.config(state=tk.NORMAL)
            self.source_text.delete('1.0', tk.END)
            self.source_text.insert('1.0', "".join(lines))
            self.source_text.config(state=tk.DISABLED)

            self.line_numbers.config(state=tk.NORMAL)
            self.line_numbers.delete('1.0', tk.END)
            for i in range(1, len(lines) + 1):
                self.line_numbers.insert(tk.END, f"{i}\n")
            self.line_numbers.config(state=tk.DISABLED)

            self._apply_syntax_highlighting()
            self._refresh_source_tags()

        self.source_text.tag_remove("current_line", '1.0', tk.END)
        self.line_numbers.tag_remove("current_line", '1.0', tk.END)
        self.source_text.tag_remove("hit_breakpoint", '1.0', tk.END)
        self.line_numbers.tag_remove("hit_breakpoint", '1.0', tk.END)
        self.source_text.tag_remove("inline_var", '1.0', tk.END)

        # Remove previous inline variables from text
        self._clear_inline_variables()

        if line > 0:
            self.current_line = line
            tag_name = "hit_breakpoint" if is_hit else "current_line"
            self.source_text.tag_add(tag_name, f"{line}.0", f"{line}.end")
            self.line_numbers.tag_add(tag_name, f"{line}.0", f"{line}.end")
            self.source_text.see(f"{line}.0")
            self.line_numbers.see(f"{line}.0")

        self.update() # Force refresh

    def _apply_syntax_highlighting(self):
        # Simple syntax highlighting for comments
        self.source_text.tag_remove("comment", "1.0", tk.END)
        start = "1.0"
        while True:
            idx = self.source_text.search(r"//", start, stopindex=tk.END, regexp=True)
            if not idx:
                break
            line_end = self.source_text.index(f"{idx} lineend")
            self.source_text.tag_add("comment", idx, line_end)
            start = line_end

        # Block comments
        start = "1.0"
        while True:
            idx = self.source_text.search(r"/\*", start, stopindex=tk.END, regexp=True)
            if not idx:
                break
            end_idx = self.source_text.search(r"\*/", idx, stopindex=tk.END, regexp=True)
            if not end_idx:
                end_idx = tk.END
            else:
                end_idx = self.source_text.index(f"{end_idx} + 2 chars")
            self.source_text.tag_add("comment", idx, end_idx)
            start = end_idx

    def _clear_inline_variables(self):
        self.source_text.config(state=tk.NORMAL)
        start = "1.0"
        while True:
            idx = self.source_text.search(" // [", start, stopindex=tk.END)
            if not idx:
                break
            line_end = self.source_text.index(f"{idx} lineend")
            self.source_text.delete(idx, line_end)
            start = idx # Next search starts from where we deleted
        self.source_text.config(state=tk.DISABLED)

    def _update_inline_variables(self):
        if not self.show_inline_vars.get():
            return
        if not self.current_line or not self.current_source:
            return

        def vars_callback(result_class, rest):
            if result_class == "^done":
                # variables=[{name="var",value="val"},...]
                vars_list = re.findall(r'name="([^"]+)",value="([^"]+)"', rest)
                if vars_list:
                    self._show_inline_vars(vars_list)

        self.gdb.send_command("-stack-list-variables --values 1", vars_callback)

    def _show_inline_vars(self, vars_list):
        if not self.show_inline_vars.get():
            return
        self.source_text.config(state=tk.NORMAL)
        line = self.current_line
        inline_str = " // [" + ", ".join([f"{name}={val}" for name, val in vars_list]) + "]"

        # We only show it for the CURRENT line where we stopped
        pos = f"{line}.end"
        self.source_text.insert(pos, inline_str, "inline_var")
        self.source_text.config(state=tk.DISABLED)
        self.update() # Force refresh

    def _on_toggle_inline_vars(self):
        if not self.show_inline_vars.get():
            self._clear_inline_variables()
        else:
            self._update_inline_variables()

    def _on_source_hover(self, event):
        # Get index under mouse
        idx = self.source_text.index(f"@{event.x},{event.y}")
        line, char = map(int, idx.split('.'))

        # Get the word at this position
        line_text = self.source_text.get(f"{line}.0", f"{line}.end")

        # Simple regex to find the word under cursor
        # We look for alphanumeric and underscores
        word = ""
        # Find start of word
        start = char
        while start > 0 and (line_text[start-1].isalnum() or line_text[start-1] == '_'):
            start -= 1
        # Find end of word
        end = char
        while end < len(line_text) and (line_text[end].isalnum() or line_text[end] == '_'):
            end += 1

        if start < end:
            word = line_text[start:end]

        if not word or not word[0].isalpha() and word[0] != '_':
            self._hide_tooltip()
            return

        if word == self.last_hover_word:
            return

        self.last_hover_word = word

        def callback(result_class, rest):
            if result_class == "^done":
                # rest looks like: value="0x123", or value="{...}"
                match = re.search(r'value="([^"]+)"', rest)
                if match:
                    val = match.group(1)
                    self.after(0, lambda: self._show_tooltip(event, f"{word}: {val}"))
                else:
                    self._hide_tooltip()
            else:
                self._hide_tooltip()

        # Only query if we are actually debugging
        if hasattr(self, 'gdb') and self.gdb.process:
             # Use -data-evaluate-expression to get the value
             self.gdb.send_command(f'-data-evaluate-expression "{word}"', callback)
        else:
             self._hide_tooltip()

    def _show_tooltip(self, event, text):
        self._hide_tooltip()

        # Create tooltip window
        x = event.x_root + 15
        y = event.y_root + 10

        self.tooltip = tk.Toplevel(self)
        self.tooltip.wm_overrideredirect(True)
        self.tooltip.wm_geometry(f"+{x}+{y}")

        label = tk.Label(self.tooltip, text=text, justify='left',
                         background="#ffffe0", relief='solid', borderwidth=1,
                         font=("tahoma", "9", "normal"))
        label.pack(ipadx=1)

    def _hide_tooltip(self, event=None):
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None
            self.last_hover_word = None

    def quit(self):
        if hasattr(self, 'gdb'):
            self.gdb.stop()

        # Capture processes to stop
        procs = [self.jlink_process, self.openocd_process, self.stlink_process]
        # Clear references to avoid race conditions in reader threads
        self.jlink_process = None
        self.openocd_process = None
        self.stlink_process = None

        for process in procs:
            if process:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
        super().quit()
        self.destroy()

if __name__ == "__main__":
    try:
        app = OzonePy()
        app.mainloop()
    except KeyboardInterrupt:
        print("Interrupt received, exiting...")
        sys.exit(0)
