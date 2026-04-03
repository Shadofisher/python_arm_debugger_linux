import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
import queue
import os
import sys
import getpass
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

    # Find paths for other GDB variants
    SYSTEM_GDB = shutil.which("gdb")
    ARM_GDB_PATH = GDB_PATH # Default discovered ARM GDB

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
        self.ssh_tunnel_process = None
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
        self.coverage_enabled = tk.BooleanVar(value=False)
        self.all_functions = [] # List of strings: ["func1", "func2", ...]
        self.hit_functions = {} # Dict: {"func1": hit_count, "func2": hit_count, ...}
        self.enabled_functions = {} # Dict: {"func1": True, "func2": False, ...}
        self.global_coverage_all_checked = tk.BooleanVar(value=True)
        self.overall_coverage_pct = tk.DoubleVar(value=0.0)
        self._coverage_update_pending = False

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

        file_menu.add_checkbutton(label="Enable Code Coverage", variable=self.coverage_enabled, command=self._on_toggle_coverage)
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

        self.line_numbers = tk.Text(source_container, width=4, padx=5, pady=5, takefocus=0, border=0,
                                   highlightthickness=0, background='#e0e0e0', state='disabled', wrap='none',
                                   font=('Consolas', 10), spacing1=0, spacing2=0, spacing3=0)
        self.line_numbers.pack(side=tk.LEFT, fill=tk.Y)

        self.source_text = tk.Text(source_container, wrap=tk.NONE, undo=False,
                                   foreground=self.color_code_fg.get(), background=self.color_code_bg.get(),
                                   font=('Consolas', 10), borderwidth=0, highlightthickness=0, padx=5, pady=5,
                                   spacing1=0, spacing2=0, spacing3=0)
        self.source_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.source_text.tag_configure("breakpoint", background=self.color_breakpoint.get(), spacing1=0, spacing2=0, spacing3=0)
        self.source_text.tag_configure("hit_breakpoint", background=self.color_current_line.get(), spacing1=0, spacing2=0, spacing3=0)
        self.source_text.tag_configure("current_line", background=self.color_current_line.get(), foreground="black", spacing1=0, spacing2=0, spacing3=0)
        self.source_text.tag_configure("comment", foreground=self.color_comments.get(), spacing1=0, spacing2=0, spacing3=0)
        self.source_text.tag_configure("inline_var", foreground="gray", font=("Consolas", 10, "italic"), spacing1=0, spacing2=0, spacing3=0)

        self.line_numbers.tag_configure("breakpoint", background=self.color_breakpoint.get(), foreground="black", spacing1=0, spacing2=0, spacing3=0)
        self.line_numbers.tag_configure("hit_breakpoint", background=self.color_current_line.get(), foreground="black", spacing1=0, spacing2=0, spacing3=0)
        self.line_numbers.tag_configure("current_line", background=self.color_current_line.get(), foreground="black", spacing1=0, spacing2=0, spacing3=0)
        self.line_numbers.tag_configure("inline_var_padding", font=("Consolas", 10, "italic"), spacing1=0, spacing2=0, spacing3=0)

        self.src_scroll = ttk.Scrollbar(source_container, command=self._on_scroll)
        self.src_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.source_text.config(yscrollcommand=self._on_source_scroll_update)
        self.line_numbers.config(yscrollcommand=self.src_scroll.set)

        self.line_numbers.bind("<Button-1>", self._on_line_click)
        self.line_numbers.bind("<Button-3>", self._on_line_right_click)
        self.source_text.bind("<MouseWheel>", self._on_mousewheel)
        self.line_numbers.bind("<MouseWheel>", self._on_mousewheel)
        self.source_text.bind("<Button-4>", self._on_mousewheel)
        self.source_text.bind("<Button-5>", self._on_mousewheel)
        self.line_numbers.bind("<Button-4>", self._on_mousewheel)
        self.line_numbers.bind("<Button-5>", self._on_mousewheel)
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

        bp_ctrl_frame = ttk.Frame(bp_frame)
        bp_ctrl_frame.pack(fill=tk.X)
        ttk.Button(bp_ctrl_frame, text="Add Watchpoint", command=self.add_watchpoint).pack(side=tk.LEFT, fill=tk.X, expand=True)

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
        ttk.Button(watch_frame, text="Add Watch / Expression", command=self.add_watch).pack(fill=tk.X)

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

        # Coverage tab
        coverage_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(coverage_frame, text="Coverage")

        # Summary Area
        coverage_summary_frame = ttk.Frame(coverage_frame)
        coverage_summary_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        # Checkbox to toggle all functions at once
        self.global_toggle_cb = ttk.Checkbutton(coverage_summary_frame, variable=self.global_coverage_all_checked, command=self._on_global_toggle_all)
        self.global_toggle_cb.pack(side=tk.LEFT, padx=(0, 5))

        ttk.Label(coverage_summary_frame, text="Overall Code Coverage:", font=('Segoe UI', 10, 'bold')).pack(side=tk.LEFT)
        self.coverage_pct_label = ttk.Label(coverage_summary_frame, text="0.0%", font=('Segoe UI', 10, 'bold'), foreground="blue")
        self.coverage_pct_label.pack(side=tk.LEFT, padx=5)

        # Functions List
        coverage_list_frame = ttk.Frame(coverage_frame)
        coverage_list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        ttk.Label(coverage_list_frame, text="Functions:").pack(anchor=tk.W)

        # We'll use the #0 (tree) column as our checkbox column.
        self.coverage_tree = ttk.Treeview(coverage_list_frame, columns=("hits",), show="tree headings")
        self.coverage_tree.heading("#0", text="[X] Function")
        self.coverage_tree.heading("hits", text="Hits")
        self.coverage_tree.column("#0", width=150)
        self.coverage_tree.column("hits", width=50, anchor=tk.CENTER)
        self.coverage_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.coverage_tree.bind("<Button-1>", self._on_tree_click)

        coverage_scroll = ttk.Scrollbar(coverage_list_frame, orient="vertical", command=self.coverage_tree.yview)
        coverage_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.coverage_tree.configure(yscrollcommand=coverage_scroll.set)

        ttk.Button(coverage_frame, text="Reset Coverage", command=self._reset_coverage).pack(fill=tk.X, padx=5, pady=2)

        # Registers tab
        reg_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(reg_frame, text="Registers")
        self.reg_tree = ttk.Treeview(reg_frame, columns=("value",), show="tree headings")
        self.reg_tree.heading("#0", text="Register")
        self.reg_tree.heading("value", text="Value")
        self.reg_tree.pack(fill=tk.BOTH, expand=True)
        ttk.Button(reg_frame, text="Export Registers", command=self.export_registers).pack(fill=tk.X, padx=5, pady=2)

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

        # Threads tab
        threads_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(threads_frame, text="Threads")
        self.threads_tree = ttk.Treeview(threads_frame, columns=("id", "target-id", "name", "state", "frame"), show="headings")
        self.threads_tree.heading("id", text="ID")
        self.threads_tree.heading("target-id", text="Target ID")
        self.threads_tree.heading("name", text="Name")
        self.threads_tree.heading("state", text="State")
        self.threads_tree.heading("frame", text="Frame")
        self.threads_tree.column("id", width=30, anchor=tk.CENTER)
        self.threads_tree.column("target-id", width=100)
        self.threads_tree.column("name", width=100)
        self.threads_tree.column("state", width=70)
        self.threads_tree.column("frame", width=200)
        self.threads_tree.pack(fill=tk.BOTH, expand=True)
        self.threads_tree.bind("<Double-1>", self._on_thread_double_click)

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
        ttk.Button(mem_ctrl_frame, text="Export", width=6, command=self.export_memory).pack(side=tk.LEFT)
        ttk.Button(mem_ctrl_frame, text="Plot", width=5, command=self.show_memory_plotter).pack(side=tk.LEFT)
        self.mem_text = tk.Text(mem_frame, font=("Consolas", 10), wrap=tk.NONE)
        self.mem_text.pack(fill=tk.BOTH, expand=True)

        # Context Menus
        self.bp_menu = tk.Menu(self, tearoff=0)
        self.bp_menu.add_command(label="Breakpoint Properties...", command=self._show_selected_bp_properties)
        self.bp_menu.add_command(label="Delete Breakpoint", command=self.delete_selected_breakpoint)
        self.bp_menu.add_separator()
        self.bp_menu.add_command(label="Delete All Breakpoints", command=self.delete_all_breakpoints)

        self.source_menu = tk.Menu(self, tearoff=0)
        self.source_menu.add_command(label="Add to Watch", command=self._add_selection_to_watch)
        self.source_menu.add_command(label="Add to Live Watch", command=self._add_selection_to_live_watch)
        self.source_menu.add_separator()
        self.source_menu.add_command(label="Run to Cursor", command=self.run_to_cursor)
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

    def _show_selected_bp_properties(self):
        selected_item = self.bp_tree.selection()
        if not selected_item:
            return

        idx = self.bp_tree.index(selected_item[0])
        if 0 <= idx < len(self.breakpoints):
            bp = self.breakpoints[idx]
            self._show_breakpoint_properties(bp)

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
                    msg = data[0]
                    if hasattr(self, 'download_progress') and self.download_progress:
                        if "Loading section" in msg:
                            self.download_progress.update(self.download_progress.progress['value'], msg.strip())
                        elif " KB of " in msg:
                            # 10 KB of 40 KB
                            match = re.search(r'(\d+) KB of (\d+) KB', msg)
                            if match:
                                sent = int(match.group(1))
                                total = int(match.group(2))
                                if total > 0:
                                    percent = (sent * 100) // total
                                    self.download_progress.update(percent)
                    if hasattr(self, 'collecting_functions') and self.collecting_functions:
                        self._process_console_for_functions(data[0])
                    if "unknown architecture \"arm\"" in data[0]:
                        self._show_arch_warning()
                elif resp_type == 'log':
                    self.log(f"LOG: {data[0]}")
                    if "unknown architecture \"arm\"" in data[0]:
                        self._show_arch_warning()
                elif resp_type == 'status-async':
                    # e.g., download,section=".text",section-size="1000",total-size="2000",total-sent="1000"
                    if data[0].startswith("download"):
                        payload = data[0]
                        section_match = re.search(r'section="([^"]+)"', payload)
                        total_size_match = re.search(r'total-size="(\d+)"', payload)
                        total_sent_match = re.search(r'total-sent="(\d+)"', payload)
                        
                        if total_size_match and total_sent_match:
                            total = int(total_size_match.group(1))
                            sent = int(total_sent_match.group(1))
                            if total > 0:
                                percent = (sent * 100) // total
                                section = section_match.group(1) if section_match else "unknown"
                                if hasattr(self, 'download_progress') and self.download_progress:
                                    self.download_progress.update(percent, f"Downloading section {section} ({percent}%)")
                elif resp_type == 'exec-async':
                    # data[0] could be e.g. stopped,reason="breakpoint-hit",...
                    if data[0].startswith("thread-created"):
                        self._update_threads()
                    elif data[0].startswith("thread-exited"):
                        self._update_threads()
                    elif data[0].startswith("thread-selected"):
                        self._update_threads()
                    self._handle_exec_async(data[0])
                elif resp_type == 'result':
                    # data = (token, result_class, rest)
                    if data[1] == "^done":
                        if hasattr(self, 'collecting_functions') and self.collecting_functions:
                            self.collecting_functions = False
                            self.debug_log("Finished collecting functions for coverage.")
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
            is_watchpoint_hit = 'reason="watchpoint-trigger"' in data or 'reason="access-watchpoint-trigger"' in data or 'reason="read-watchpoint-trigger"' in data
            is_interrupted = 'reason="signal-received"' in data or 'reason="interrupted"' in data

            if is_interrupted or is_bp_hit or is_watchpoint_hit:
                if is_interrupted:
                    self.log("Execution interrupted.")
                elif is_watchpoint_hit:
                    self.log("Watchpoint hit.")
                else:
                    self.log("Breakpoint hit.")
                    # Handle breakpoint dependencies
                    bp_num_match = re.search(r'bkptno="(\d+)"', data)
                    if bp_num_match:
                        hit_num = bp_num_match.group(1)
                        # Mark it as satisfied
                        for bp in self.breakpoints:
                            if bp['number'] == hit_num:
                                bp['is_satisfied'] = True
                                break
                        
                        # Now check if any other breakpoints can be enabled
                        for bp in self.breakpoints:
                            deps = bp.get('depends_on', [])
                            if deps:
                                # A breakpoint is satisfied if all its dependencies are satisfied
                                all_satisfied = True
                                for dep_num in deps:
                                    dep_satisfied = False
                                    for other_bp in self.breakpoints:
                                        if other_bp['number'] == dep_num and other_bp.get('is_satisfied'):
                                            dep_satisfied = True
                                            break
                                    if not dep_satisfied:
                                        all_satisfied = True # Wait, no! If one dep is not satisfied, all_satisfied is false
                                        all_satisfied = False
                                        break
                                
                                if all_satisfied:
                                    self.log(f"Dependencies met for breakpoint {bp['number']}, enabling.")
                                    self.gdb.send_command(f"-break-enable {bp['number']}")
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
                        self.update_idletasks() # Force refresh

                # Check if we should request frame info. Avoid requesting if we are likely in a transient state.
                if data.startswith("stopped"):
                     self.gdb.send_command("-stack-info-frame", frame_callback)

            self._update_watches()
            self._update_live_watches_for_step()
            self._update_registers()
            self._update_threads()
            self.debug_log("DEBUG: Triggering Call Stack update from _handle_exec_async", "debug")
            self._update_call_stack()
            self._update_inline_variables()
            self.read_memory()
            if self.coverage_enabled.get():
                if func_name != "unknown":
                    self._update_coverage_stats(func_name)
                else:
                    # If func_name is unknown, try to get it from PC if possible
                    self._update_on_the_fly_coverage()
        elif data.startswith("running"):
            self._update_ui_for_execution_state(True)
            self.location_bar.config(text="Running...")
            self.update_idletasks() # Force refresh after handling async event

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
                if self.coverage_enabled.get():
                    self._get_all_functions()
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
        d.geometry("400x480")

        # Parse current address
        host_port = self.gdb_server_address.get().split(':')
        current_host = host_port[0] if len(host_port) > 0 else "localhost"
        current_port = host_port[1] if len(host_port) > 1 else "3333"

        host_var = tk.StringVar(value=current_host)
        port_var = tk.StringVar(value=current_port)

        ttk.Label(d, text="GDB Server Host (IP or hostname):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=host_var).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="GDB Server Port:").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=port_var).pack(fill=tk.X, padx=5)

        def test_connection():
            host = host_var.get().strip() or "localhost"
            port = port_var.get().strip() or "3333"
            try:
                test_host = "127.0.0.1" if host == "localhost" else host
                if self._is_port_in_use(port):
                    messagebox.showinfo("Test Connection", f"SUCCESS: Port {port} on {host} is OPEN and reachable.")
                else:
                    messagebox.showwarning("Test Connection", f"FAILED: Port {port} on {host} is CLOSED.\n\nEnsure your GDB server is running and listening on this port.")
            except Exception as e:
                messagebox.showerror("Test Connection", f"Error testing connection: {e}")

        ttk.Button(d, text="Test Connection", command=test_connection).pack(pady=5)

        ttk.Label(d, text="GDB Architecture (ARM variants):").pack(padx=5, pady=2, anchor=tk.W)
        arch_list = ["armv7e-m", "armv7-m", "armv6-m", "armv8-m.main", "armv8-m.base", "auto"]
        arch_combo = ttk.Combobox(d, textvariable=self.gdb_architecture, values=arch_list)
        arch_combo.pack(fill=tk.X, padx=5)
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
            new_host = host_var.get().strip() or "localhost"
            new_port = port_var.get().strip() or "3333"
            self.gdb_server_address.set(f"{new_host}:{new_port}")
            self._update_connect_menu()
            self.log(f"GDB Settings updated: {self.gdb_server_address.get()}, Arch: {self.gdb_architecture.get()}")
            d.destroy()

        def connect():
            save()
            self.after(100, lambda: self.connect_target(self.gdb_server_address.get()))

        btn_frame = ttk.Frame(d)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Connect", command=connect).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=d.destroy).pack(side=tk.LEFT, padx=5)

    def set_jlink_settings(self):
        d = tk.Toplevel(self)
        d.title("J-Link Server Settings")
        d.geometry("450x380")

        # Use local variables to allow Cancel to work correctly
        path_var = tk.StringVar(value=self.jlink_server_path.get())
        device_var = tk.StringVar(value=self.jlink_device.get())
        interface_var = tk.StringVar(value=self.jlink_interface.get())
        speed_var = tk.StringVar(value=self.jlink_speed.get())
        script_var = tk.StringVar(value=self.jlink_script.get())

        ttk.Label(d, text="J-Link Server Path:").pack(padx=5, pady=2, anchor=tk.W)
        path_frame = ttk.Frame(d)
        path_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(path_frame, textvariable=path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="Browse", command=lambda: path_var.set(
            filedialog.askopenfilename(filetypes=[("Executable", "*.exe;*"), ("All files", "*.*")]))).pack(side=tk.RIGHT)

        ttk.Label(d, text="Device:").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=device_var).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="Interface:").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Combobox(d, textvariable=interface_var, values=["SWD", "JTAG"]).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="Speed (kHz):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=speed_var).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="J-Link Script:").pack(padx=5, pady=2, anchor=tk.W)
        script_frame = ttk.Frame(d)
        script_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(script_frame, textvariable=script_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        def browse_script():
            path = filedialog.askopenfilename(filetypes=[("J-Link Script", "*.jlinkscript"), ("All files", "*.*")])
            if path: script_var.set(path)
        ttk.Button(script_frame, text="Browse", command=browse_script).pack(side=tk.RIGHT)

        def save():
            self.jlink_server_path.set(path_var.get())
            self.jlink_device.set(device_var.get())
            self.jlink_interface.set(interface_var.get())
            self.jlink_speed.set(speed_var.get())
            self.jlink_script.set(script_var.get())
            self.log("J-Link settings updated.")
            d.destroy()

        def connect():
            save()
            self.after(100, self.connect_jlink_auto)

        btn_frame = ttk.Frame(d)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Connect", command=connect).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=d.destroy).pack(side=tk.LEFT, padx=5)

    def load_jlink_script(self):
        path = filedialog.askopenfilename(filetypes=[("J-Link Script", "*.jlinkscript"), ("All files", "*.*")])
        if path:
            self.jlink_script.set(path)
            self.log(f"J-Link script set to: {path}")

    def set_openocd_settings(self):
        d = tk.Toplevel(self)
        d.title("OpenOCD Server Settings")
        d.geometry("400x200")

        path_var = tk.StringVar(value=self.openocd_server_path.get())
        cfg_var = tk.StringVar(value=self.openocd_config_path.get())

        ttk.Label(d, text="OpenOCD Path:").pack(padx=5, pady=2, anchor=tk.W)
        path_frame = ttk.Frame(d)
        path_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(path_frame, textvariable=path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="Browse", command=lambda: path_var.set(
            filedialog.askopenfilename(filetypes=[("Executable", "*.exe;*"), ("All files", "*.*")]))).pack(side=tk.RIGHT)

        ttk.Label(d, text="Config File:").pack(padx=5, pady=2, anchor=tk.W)
        cfg_frame = ttk.Frame(d)
        cfg_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(cfg_frame, textvariable=cfg_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cfg_frame, text="Browse", command=lambda: cfg_var.set(
            filedialog.askopenfilename(filetypes=[("Config", "*.cfg"), ("All files", "*.*")]))).pack(side=tk.RIGHT)

        def save():
            self.openocd_server_path.set(path_var.get())
            self.openocd_config_path.set(cfg_var.get())
            self.log("OpenOCD settings updated.")
            d.destroy()

        def connect():
            save()
            self.after(100, self.connect_openocd_auto)

        btn_frame = ttk.Frame(d)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Connect", command=connect).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=d.destroy).pack(side=tk.LEFT, padx=5)

    def set_stlink_settings(self):
        d = tk.Toplevel(self)
        d.title("ST-LINK GDB Server Settings")
        d.geometry("500x480")
        d.resizable(True, True)

        path_var = tk.StringVar(value=self.stlink_server_path.get())
        cp_var = tk.StringVar(value=self.stlink_cubeprogrammer_path.get())
        freq_var = tk.StringVar(value=self.stlink_frequency.get())
        serial_var = tk.StringVar(value=self.stlink_serial.get())
        device_var = tk.StringVar(value=self.stlink_device.get())
        apid_var = tk.StringVar(value=self.stlink_apid.get())
        reset_var = tk.BooleanVar(value=self.stlink_init_under_reset.get())
        attach_var = tk.BooleanVar(value=self.stlink_attach.get())
        persistent_var = tk.BooleanVar(value=self.stlink_persistent.get())

        ttk.Label(d, text="ST-LINK_gdbserver Path:").pack(padx=5, pady=2, anchor=tk.W)
        path_frame = ttk.Frame(d)
        path_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(path_frame, textvariable=path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="Browse", command=lambda: path_var.set(
            filedialog.askopenfilename(title="Select ST-LINK_gdbserver",
                filetypes=[("Executable", "*.exe;*"), ("All files", "*.*")])
        )).pack(side=tk.RIGHT)

        ttk.Label(d, text="STM32CubeProgrammer Path (-cp):").pack(padx=5, pady=2, anchor=tk.W)
        cp_frame = ttk.Frame(d)
        cp_frame.pack(fill=tk.X, padx=5)
        ttk.Entry(cp_frame, textvariable=cp_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cp_frame, text="Browse", command=lambda: cp_var.set(
            filedialog.askdirectory(title="Select STM32CubeProgrammer Installation Directory")
        )).pack(side=tk.RIGHT)

        ttk.Label(d, text="Frequency (kHz) [--frequency]:").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=freq_var).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="ST-LINK Serial Number [-i] (leave blank for first found):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=serial_var).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="Target MCU Device [-d] (e.g. STM32F411xE, leave blank for auto):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=device_var).pack(fill=tk.X, padx=5)

        ttk.Label(d, text="AP Index [-m] (0 = default core, use for multi-core):").pack(padx=5, pady=2, anchor=tk.W)
        ttk.Entry(d, textvariable=apid_var).pack(fill=tk.X, padx=5)

        options_frame = ttk.LabelFrame(d, text="Options")
        options_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Checkbutton(options_frame, text="Initialize device under reset [-k]",
                        variable=reset_var).pack(anchor=tk.W, padx=5, pady=2)
        ttk.Checkbutton(options_frame, text="Attach to running target, no reset [-g]",
                        variable=attach_var).pack(anchor=tk.W, padx=5, pady=2)
        ttk.Checkbutton(options_frame, text="Persistent mode - keep server alive [-e]",
                        variable=persistent_var).pack(anchor=tk.W, padx=5, pady=2)

        def save():
            self.stlink_server_path.set(path_var.get())
            self.stlink_cubeprogrammer_path.set(cp_var.get())
            self.stlink_frequency.set(freq_var.get())
            self.stlink_serial.set(serial_var.get())
            self.stlink_device.set(device_var.get())
            self.stlink_apid.set(apid_var.get())
            self.stlink_init_under_reset.set(reset_var.get())
            self.stlink_attach.set(attach_var.get())
            self.stlink_persistent.set(persistent_var.get())
            self.log("ST-LINK settings updated.")
            d.destroy()

        def connect():
            save()
            self.after(100, self.connect_stlink_auto)

        btn_frame = ttk.Frame(d)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Save", command=save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Connect", command=connect).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=d.destroy).pack(side=tk.LEFT, padx=5)

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
        self.connect_menu.add_command(label=f"Connect to GDB Server ({addr})", command=lambda: self._prompt_arch_and_connect(addr))
        self.connect_menu.add_command(label="Connect to Remote GDB Server...", command=self._prompt_remote_connect)

    def _prompt_remote_connect(self):
        """Show a dialog to input IP, Port and select architecture with optional SSH tunneling."""
        d = tk.Toplevel(self)
        d.title("Remote GDB Connection")
        d.geometry("500x600")
        d.transient(self)
        d.grab_set()

        # Target Host/Port
        ttk.Label(d, text="Target GDB Server Info (as seen from Remote Host):", font=("", 10, "bold")).pack(pady=(15, 5))
        
        host_frame = ttk.Frame(d)
        host_frame.pack(fill=tk.X, padx=40)
        ttk.Label(host_frame, text="Host:").pack(side=tk.LEFT)
        host_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(host_frame, textvariable=host_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))

        port_frame = ttk.Frame(d)
        port_frame.pack(fill=tk.X, padx=40, pady=5)
        ttk.Label(port_frame, text="Port:").pack(side=tk.LEFT)
        port_var = tk.StringVar(value="3333")
        ttk.Entry(port_frame, textvariable=port_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))

        # Architecture
        ttk.Label(d, text="Target Architecture:").pack(pady=(5, 0))
        arch_list = [
            "arm", "armv7e-m", "armv7-m", "armv6-m", "armv8-m.main", "armv8-m.base",
            "i386", "i386:x86-64", "mips", "powerpc", "riscv", "auto"
        ]
        arch_display = {
            "i386": "x86 (i386)",
            "i386:x86-64": "x86_64",
            "arm": "ARM (generic)",
            "armv7e-m": "ARM Cortex-M4/M7 (armv7e-m)",
            "armv7-m": "ARM Cortex-M3 (armv7-m)",
            "armv6-m": "ARM Cortex-M0 (armv6-m)",
            "armv8-m.main": "ARM Cortex-M33 (armv8-m.main)",
            "armv8-m.base": "ARM Cortex-M23 (armv8-m.base)",
            "mips": "MIPS",
            "powerpc": "PowerPC",
            "riscv": "RISC-V",
            "auto": "Auto-detect"
        }
        display_list = [arch_display.get(a, a) for a in arch_list]
        selected_display = tk.StringVar(value=arch_display.get(self.gdb_architecture.get(), self.gdb_architecture.get()))

        combo = ttk.Combobox(d, textvariable=selected_display, values=display_list, state="readonly")
        combo.pack(fill=tk.X, padx=40, pady=5)

        # SSH Tunneling section
        ttk.Separator(d, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=20, pady=15)
        
        use_ssh_var = tk.BooleanVar(value=False)
        ssh_check = ttk.Checkbutton(d, text="Use SSH Tunnel", variable=use_ssh_var)
        ssh_check.pack()

        ssh_frame = ttk.LabelFrame(d, text="SSH Tunnel Settings")
        ssh_frame.pack(fill=tk.X, padx=40, pady=10)

        # SSH Host
        ttk.Label(ssh_frame, text="SSH Host:").pack(padx=5)
        ssh_host_var = tk.StringVar(value="192.168.0.120")
        ttk.Entry(ssh_frame, textvariable=ssh_host_var).pack(fill=tk.X, padx=5, pady=2)

        # SSH User
        ttk.Label(ssh_frame, text="SSH User:").pack(padx=5)
        ssh_user_var = tk.StringVar(value=getpass.getuser())
        ttk.Entry(ssh_frame, textvariable=ssh_user_var).pack(fill=tk.X, padx=5, pady=2)

        # SSH Key
        ttk.Label(ssh_frame, text="SSH Identity (Optional):").pack(padx=5)
        ssh_key_var = tk.StringVar(value="")
        key_entry_frame = ttk.Frame(ssh_frame)
        key_entry_frame.pack(fill=tk.X, padx=5, pady=2)
        ttk.Entry(key_entry_frame, textvariable=ssh_key_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        def browse_key():
            fn = filedialog.askopenfilename(title="Select SSH Key", initialdir=os.path.expanduser("~/.ssh"))
            if fn: ssh_key_var.set(fn)
        
        ttk.Button(key_entry_frame, text="...", width=3, command=browse_key).pack(side=tk.LEFT)

        def on_connect():
            host = host_var.get().strip()
            port = port_var.get().strip()
            display_val = selected_display.get()

            if not host or not port:
                return

            final_arch = "auto"
            for k, v in arch_display.items():
                if v == display_val:
                    final_arch = k
                    break

            self.gdb_architecture.set(final_arch)

            # Switch GDB executable if needed
            is_x86 = final_arch.startswith('i386')
            current_gdb = self.gdb.gdb_path
            target_gdb = None
            if is_x86:
                if SYSTEM_GDB and "arm" not in os.path.basename(SYSTEM_GDB).lower():
                    target_gdb = SYSTEM_GDB
                else:
                    self.log("Warning: No suitable x86 GDB found, using current.", "error")
            else:
                target_gdb = ARM_GDB_PATH

            if target_gdb and target_gdb != current_gdb:
                self.log(f"Switching GDB to {target_gdb} for architecture {final_arch}")
                self.gdb.restart_with_path(target_gdb)

            # SSH Tunnel logic
            if self.ssh_tunnel_process:
                try:
                    self.ssh_tunnel_process.terminate()
                except Exception:
                    pass
                self.ssh_tunnel_process = None

            address = f"{host}:{port}"
            if use_ssh_var.get():
                ssh_host = ssh_host_var.get().strip()
                ssh_user = ssh_user_var.get().strip()
                ssh_key = ssh_key_var.get().strip()

                if not ssh_host or not ssh_user:
                    messagebox.showerror("SSH Error", "SSH Host and User are required for tunneling.")
                    return

                # Pick a random free local port for tunneling
                import socket
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', 0))
                    local_port = s.getsockname()[1]

                ssh_cmd = ["ssh", "-N", "-L", f"{local_port}:{host}:{port}", f"{ssh_user}@{ssh_host}"]
                # Fix for "Too many authentication failures" - only use the specified key if provided, or only keys from agent
                ssh_cmd.extend(["-o", "IdentitiesOnly=yes"])
                if ssh_key:
                    ssh_cmd.extend(["-i", ssh_key])
                
                self.log(f"Starting SSH tunnel: {' '.join(ssh_cmd)}")
                try:
                    self.ssh_tunnel_process = subprocess.Popen(ssh_cmd)
                    address = f"127.0.0.1:{local_port}"
                    # Wait a bit for the tunnel to establish
                    self.log(f"Waiting for SSH tunnel to establish on localhost:{local_port}...")
                    d.after(1000, lambda: self._finish_connect(d, address))
                    return
                except Exception as e:
                    self.log(f"Failed to start SSH tunnel: {e}", "error")
                    messagebox.showerror("SSH Error", f"Failed to start SSH tunnel: {e}")
                    return

            self._finish_connect(d, address)

        def on_cancel():
            d.destroy()

        btn_frame = ttk.Frame(d)
        btn_frame.pack(pady=20)
        ttk.Button(btn_frame, text="Connect", command=on_connect).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side=tk.LEFT, padx=5)

        # Center dialog
        d.update_idletasks()
        x = (d.winfo_screenwidth() // 2) - (d.winfo_width() // 2)
        y = (d.winfo_screenheight() // 2) - (d.winfo_height() // 2)
        d.geometry(f'+{x}+{y}')

        self.wait_window(d)

    def _finish_connect(self, dialog, address):
        """Finalize the connection after potential tunnel setup."""
        if dialog.winfo_exists():
            dialog.destroy()
        self.connect_target(address)

    def _prompt_arch_and_connect(self, address):
        """Show a dialog to select the architecture before connecting."""
        d = tk.Toplevel(self)
        d.title("Select Architecture")
        d.geometry("350x250")
        d.transient(self)
        d.grab_set()

        ttk.Label(d, text=f"Select target architecture for\n{address}:", justify=tk.CENTER).pack(pady=10)

        # Common architectures
        arch_list = [
            "arm", "armv7e-m", "armv7-m", "armv6-m", "armv8-m.main", "armv8-m.base",
            "i386", "i386:x86-64", "mips", "powerpc", "riscv", "auto"
        ]
        
        # Mapping for user-friendly names
        arch_display = {
            "i386": "x86 (i386)",
            "i386:x86-64": "x86_64",
            "arm": "ARM (generic)",
            "armv7e-m": "ARM Cortex-M4/M7 (armv7e-m)",
            "armv7-m": "ARM Cortex-M3 (armv7-m)",
            "armv6-m": "ARM Cortex-M0 (armv6-m)",
            "armv8-m.main": "ARM Cortex-M33 (armv8-m.main)",
            "armv8-m.base": "ARM Cortex-M23 (armv8-m.base)",
            "mips": "MIPS",
            "powerpc": "PowerPC",
            "riscv": "RISC-V",
            "auto": "Auto-detect"
        }

        display_list = [arch_display.get(a, a) for a in arch_list]
        selected_display = tk.StringVar(value=arch_display.get(self.gdb_architecture.get(), self.gdb_architecture.get()))

        combo = ttk.Combobox(d, textvariable=selected_display, values=display_list, state="readonly")
        combo.pack(fill=tk.X, padx=20, pady=10)

        def on_connect():
            # Find the actual GDB architecture string from the display name
            display_val = selected_display.get()
            final_arch = "auto"
            for k, v in arch_display.items():
                if v == display_val:
                    final_arch = k
                    break
            
            self.gdb_architecture.set(final_arch)
            
            # Switch GDB executable if needed based on architecture
            is_x86 = final_arch.startswith('i386')
            current_gdb = self.gdb.gdb_path
            
            target_gdb = None
            if is_x86:
                if SYSTEM_GDB and "arm" not in os.path.basename(SYSTEM_GDB).lower():
                    target_gdb = SYSTEM_GDB
                else:
                    self.log("Warning: No suitable x86 GDB found, using current.", "error")
            else:
                target_gdb = ARM_GDB_PATH

            if target_gdb and target_gdb != current_gdb:
                self.log(f"Switching GDB to {target_gdb} for architecture {final_arch}")
                self.gdb.restart_with_path(target_gdb)

            d.destroy()
            self.connect_target(address)

        def on_cancel():
            d.destroy()

        btn_frame = ttk.Frame(d)
        btn_frame.pack(pady=20)
        ttk.Button(btn_frame, text="Connect", command=on_connect).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side=tk.LEFT, padx=5)

        # Center dialog
        d.update_idletasks()
        width = d.winfo_width()
        height = d.winfo_height()
        x = (d.winfo_screenwidth() // 2) - (width // 2)
        y = (d.winfo_screenheight() // 2) - (height // 2)
        d.geometry(f'+{x}+{y}')
        
        self.wait_window(d)

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

                    if self.coverage_enabled.get():
                        self._get_all_functions()
                        self._start_coverage_timer()

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

        # Reset dependency status when resetting target
        for bp in self.breakpoints:
            bp['is_satisfied'] = False
            if bp.get('depends_on'):
                self.gdb.send_command(f"-break-disable {bp['number']}")

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
        if hasattr(self, 'download_progress') and self.download_progress:
            self.download_progress.close()
        
        # Ensure async mode is on for remote targets
        self.gdb.send_command("-gdb-set mi-async on")
        self.gdb.send_command("-gdb-set target-async on")
        
        self.download_progress = ConnectionProgress(self, "Downloading ELF")
        self.download_progress.update(0, "Starting download...")
        
        def on_download_complete(result_class, rest):
            if hasattr(self, 'download_progress') and self.download_progress:
                self.download_progress.close()
                self.download_progress = None
            if result_class == "^done":
                self.log("Download complete.")
            else:
                self.log(f"Download failed: {rest}")

        self.gdb.send_command("load", on_download_complete)
        self.log("Downloading ELF to target...")

    def go(self):
        self._reset_watch_changed_flags()
        self.gdb.send_command("-gdb-set mi-async on")
        self.gdb.send_command("-gdb-set target-async on")
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

        self.gdb.send_command("-gdb-set mi-async on")
        self.gdb.send_command("-gdb-set target-async on")
        self.log("Interrupting execution...")
        self.debug_log("Pause requested", "info")

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
        if self.ssh_tunnel_process:
            try:
                self.ssh_tunnel_process.terminate()
            except Exception:
                pass
            self.ssh_tunnel_process = None
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
        self.gdb.send_command("-gdb-set mi-async on")
        self.gdb.send_command("-gdb-set target-async on")
        self.gdb.send_command("-exec-step")

    def step_over(self):
        self._reset_watch_changed_flags()
        self.gdb.send_command("-gdb-set mi-async on")
        self.gdb.send_command("-gdb-set target-async on")
        self.gdb.send_command("-exec-next")

    def _on_mousewheel(self, event):
        # On Windows, event.delta is typically +/- 120
        # On Linux/macOS, it might vary.
        # Scroll both widgets
        move = -1 if (event.delta > 0 or (hasattr(event, 'num') and event.num == 4)) else 1
        self.source_text.yview_scroll(move, "units")
        self.line_numbers.yview_scroll(move, "units")
        return "break" # Prevent default behavior

    def _on_scroll(self, *args):
        self.source_text.yview(*args)
        self.line_numbers.yview(*args)
        # Ensure they are perfectly in sync by forcing the same yview
        self.line_numbers.yview_moveto(self.source_text.yview()[0])

    def _on_source_scroll_update(self, *args):
        self.line_numbers.yview_moveto(args[0])
        if hasattr(self, 'src_scroll'):
            self.src_scroll.set(*args)

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
                        self.breakpoints.append({
                            'number': match.group(1), 
                            'file': filename, 
                            'line': line,
                            'count': 0,
                            'condition': '',
                            'depends_on': [],
                            'is_satisfied': False
                        })
                        self._refresh_bp_tree()
                        self._refresh_source_tags()

            # Normalize path for GDB
            gdb_filename = filename.replace('\\', '/')
            self.gdb.send_command(f'-break-insert "{gdb_filename}:{line}"', bp_callback)
        else:
            self._refresh_bp_tree()
            self._refresh_source_tags()

    def _on_line_right_click(self, event):
        # Determine which line was right-clicked
        line_idx = self.line_numbers.index(f"@{event.x},{event.y}").split('.')[0]
        line = int(line_idx)
        
        # Check if there is a breakpoint on this line
        target_bp = None
        if self.current_source:
            filename = os.path.abspath(os.path.normpath(self.current_source))
            for bp in self.breakpoints:
                if os.path.abspath(os.path.normpath(bp['file'])) == filename and bp['line'] == line:
                    target_bp = bp
                    break
        
        if target_bp:
            self._show_breakpoint_properties(target_bp)
        else:
            # Maybe show a menu to add a breakpoint? 
            # For now, let's just ignore if there's no breakpoint there.
            pass

    def _show_breakpoint_properties(self, bp):
        dialog = tk.Toplevel(self)
        dialog.title(f"Breakpoint Properties - {os.path.basename(bp['file'])}:{bp['line']}")
        dialog.geometry("400x450")
        dialog.transient(self)
        dialog.grab_set()

        main_frame = ttk.Frame(dialog, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1 & 2: List of all breakpoints with checkboxes
        ttk.Label(main_frame, text="Breakpoints List (Check to set as dependency):").pack(anchor=tk.W)
        
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        canvas = tk.Canvas(list_frame, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Track which breakpoints are selected as dependencies
        # In a real GDB scenario, "happening before" could mean many things.
        # We'll interpret it as a requirement: break only if the dependency was hit.
        # However, GDB doesn't have a simple way to do this without scripts.
        # We will just store it in the BP data for now as requested.
        
        dep_vars = {}
        current_deps = bp.get('depends_on', [])

        for other_bp in self.breakpoints:
            if other_bp['number'] == bp['number']:
                continue
            
            var = tk.BooleanVar(value=(other_bp['number'] in current_deps))
            dep_vars[other_bp['number']] = var
            
            text = f"BP {other_bp['number']}: {os.path.basename(other_bp['file'])}:{other_bp['line']}"
            cb = ttk.Checkbutton(scrollable_frame, text=text, variable=var)
            cb.pack(anchor=tk.W, padx=5, pady=2)

        # 4: Count that determines how many times it should be hit
        count_frame = ttk.Frame(main_frame)
        count_frame.pack(fill=tk.X, pady=10)
        ttk.Label(count_frame, text="Count (1: stop on 1st hit, 5: stop on 5th hit, 0: always stop):").pack(side=tk.LEFT)
        
        hit_count_var = tk.StringVar(value=str(bp.get('count', 0)))
        count_entry = ttk.Entry(count_frame, textvariable=hit_count_var, width=10)
        count_entry.pack(side=tk.LEFT, padx=5)

        # Condition entry (Extra feature, usually goes with conditional BPs)
        cond_frame = ttk.Frame(main_frame)
        cond_frame.pack(fill=tk.X, pady=5)
        ttk.Label(cond_frame, text="Condition (GDB expr):").pack(side=tk.LEFT)
        cond_var = tk.StringVar(value=bp.get('condition', ''))
        cond_entry = ttk.Entry(cond_frame, textvariable=cond_var)
        cond_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        def save_and_close():
            try:
                new_count = int(hit_count_var.get())
            except ValueError:
                new_count = 0
            
            new_deps = [num for num, var in dep_vars.items() if var.get()]
            new_condition = cond_var.get().strip()
            
            bp['count'] = new_count
            bp['depends_on'] = new_deps
            bp['condition'] = new_condition
            
            # Apply to GDB
            # 1. Count (GDB's ignore count is count-1)
            # If count is 5, we ignore 4 times and stop on the 5th.
            # If count is 1, we ignore 0 times and stop on the 1st.
            # If count is 0, we ignore 0 times and stop on the 1st (always stop).
            gdb_ignore_count = max(0, new_count - 1) if new_count > 0 else 0
            self.gdb.send_command(f"-break-after {bp['number']} {gdb_ignore_count}")
            
            # 2. Condition
            if new_condition:
                self.gdb.send_command(f"-break-condition {bp['number']} {new_condition}")
            else:
                # To clear condition in GDB MI, you usually send empty condition? 
                # Actually -break-condition <number> without expression might work or error.
                # Usually one might use a condition that's always true, or use the CLI command.
                self.gdb.send_command(f"condition {bp['number']}") # CLI command to clear
            
            # 3. Dependencies - this is tricky in GDB. 
            # We'll just log it for now as a "mock" implementation of the UI requirement.
            if new_deps:
                self.log(f"Breakpoint {bp['number']} now depends on {new_deps}")
                # When dependencies are set, we disable the breakpoint initially
                self.gdb.send_command(f"-break-disable {bp['number']}")
            else:
                # If no dependencies, make sure it's enabled
                self.gdb.send_command(f"-break-enable {bp['number']}")
            
            dialog.destroy()

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=10)
        ttk.Button(btn_frame, text="Save", command=save_and_close).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

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
            info = []
            if bp.get('count', 0) > 0:
                info.append(f"count: {bp['count']}")
            if bp.get('condition'):
                info.append(f"cond: {bp['condition']}")
            if bp.get('depends_on'):
                info.append(f"deps: {len(bp['depends_on'])}")
            
            if bp.get('type') == 'watchpoint':
                val = f"Watch: {bp.get('expression', '?')}"
                line = "-"
            else:
                val = os.path.basename(bp['file'])
                line = bp['line']

            if info:
                val += f" ({', '.join(info)})"
            
            self.bp_tree.insert("", tk.END, values=(val, line))

    def _refresh_source_tags(self):
        self.source_text.tag_remove("breakpoint", "1.0", tk.END)
        self.line_numbers.tag_remove("breakpoint", "1.0", tk.END)
        self.source_text.tag_remove("hit_breakpoint", "1.0", tk.END)
        self.line_numbers.tag_remove("hit_breakpoint", "1.0", tk.END)
        self.source_text.tag_remove("current_line", "1.0", tk.END)
        self.line_numbers.tag_remove("current_line", "1.0", tk.END)

        # Normalize once
        cur_src = os.path.abspath(os.path.normpath(self.current_source)) if self.current_source else None

        # Add current line tag first (if it's the current file)
        if cur_src and self.current_line > 0:
            # We don't know yet if it's hit_breakpoint or current_line,
            # we'll decide that based on breakpoints in the next loop.
            pass

        # Manage breakpoints and current line highlights
        for bp in self.breakpoints:
            bp_file = os.path.abspath(os.path.normpath(bp['file']))
            if bp_file == cur_src:
                # Is this line also the current execution line?
                is_hit = (bp['line'] == self.current_line)
                tag_name = "hit_breakpoint" if is_hit else "breakpoint"
                self.source_text.tag_add(tag_name, f"{bp['line']}.0", f"{bp['line']}.end")
                self.line_numbers.tag_add(tag_name, f"{bp['line']}.0", f"{bp['line']}.end")
                # Add italic padding to line numbers if current line has italic inline vars to keep heights matched
                if self.show_inline_vars.get():
                    self.line_numbers.tag_add("inline_var_padding", f"{bp['line']}.0", f"{bp['line']}.end")

        # Finally, if there's a current line that was not marked as hit_breakpoint
        if cur_src and self.current_line > 0:
            # Check if it was already tagged as hit_breakpoint
            tags = self.source_text.tag_names(f"{self.current_line}.0")
            if "hit_breakpoint" not in tags:
                self.source_text.tag_add("current_line", f"{self.current_line}.0", f"{self.current_line}.end")
                self.line_numbers.tag_add("current_line", f"{self.current_line}.0", f"{self.current_line}.end")
                if self.show_inline_vars.get():
                    self.line_numbers.tag_add("inline_var_padding", f"{self.current_line}.0", f"{self.current_line}.end")

    def add_watchpoint(self):
        dialog = tk.Toplevel(self)
        dialog.title("Add Watchpoint")
        dialog.geometry("300x150")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="Expression:").pack(padx=5, pady=5)
        expr_entry = ttk.Entry(dialog)
        expr_entry.pack(fill=tk.X, padx=5)
        
        type_var = tk.StringVar(value="write")
        ttk.Radiobutton(dialog, text="Write", variable=type_var, value="write").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(dialog, text="Read", variable=type_var, value="read").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(dialog, text="Access (R/W)", variable=type_var, value="access").pack(side=tk.LEFT, padx=5)

        def add():
            expr = expr_entry.get().strip()
            if not expr: return
            
            mode = type_var.get()
            flag = ""
            if mode == "read": flag = "-r"
            elif mode == "access": flag = "-a"
            
            def callback(result_class, rest):
                if result_class == "^done":
                    match = re.search(r'number="(\d+)"', rest)
                    if match:
                        self.breakpoints.append({
                            'number': match.group(1),
                            'type': 'watchpoint',
                            'expression': expr,
                            'mode': mode,
                            'file': '', # Not file based
                            'line': 0
                        })
                        self._refresh_bp_tree()
                else:
                    self.log(f"Failed to set watchpoint: {rest}")

            self.gdb.send_command(f"-break-watch {flag} {expr}", callback)
            dialog.destroy()

        ttk.Button(dialog, text="Add", command=add).pack(side=tk.BOTTOM, pady=5)

    def run_to_cursor(self):
        if not self.current_source: return
        
        # Get line from cursor position
        try:
            line = int(self.source_text.index(tk.INSERT).split('.')[0])
        except Exception: return

        filename = os.path.abspath(os.path.normpath(self.current_source)).replace('\\', '/')
        
        def callback(result_class, rest):
            if result_class == "^done":
                # Successfully set temporary breakpoint, now continue
                self.go()
            else:
                self.log(f"Run to Cursor failed: {rest}")

        self.gdb.send_command(f'-break-insert -t "{filename}:{line}"', callback)

    def add_watch(self):
        var_name = simpledialog.askstring("Add Watch", "Variable or Expression:")
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

    def read_memory(self, count=256, callback=None):
        addr = self.mem_addr_entry.get().strip()
        if not addr:
            return

        def default_callback(result_class, rest):
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

        cb = callback if callback else default_callback
        self.gdb.send_command(f'-data-read-memory-bytes {addr} {count}', cb)

    def export_memory(self):
        addr = self.mem_addr_entry.get().strip()
        if not addr:
            messagebox.showwarning("Export Memory", "Please enter a memory address first.")
            return

        count = simpledialog.askinteger("Export Memory", f"Number of bytes to export from {addr}:", initialvalue=256, minvalue=1)
        if not count:
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".bin",
            filetypes=[("Binary files", "*.bin"), ("CSV files", "*.csv"), ("JSON files", "*.json"), ("All files", "*.*")],
            title="Export Memory"
        )
        if not file_path:
            return

        def callback(result_class, rest):
            if result_class == "^done":
                match = re.search(r'contents="([0-9a-fA-F]+)"', rest)
                if not match:
                    self.after(0, lambda: messagebox.showerror("Export Memory", "Failed to read memory contents."))
                    return
                
                hex_data = match.group(1)
                data = bytes.fromhex(hex_data)

                try:
                    if file_path.endswith(".csv"):
                        with open(file_path, "w") as f:
                            f.write("Address,Value\n")
                            start_addr = int(addr, 16)
                            for i, b in enumerate(data):
                                f.write(f"0x{start_addr + i:08X},0x{b:02X}\n")
                    elif file_path.endswith(".json"):
                        start_addr = int(addr, 16)
                        export_data = {
                            "address": addr,
                            "length": len(data),
                            "data": [f"0x{b:02X}" for b in data]
                        }
                        with open(file_path, "w") as f:
                            json.dump(export_data, f, indent=4)
                    else: # Default to BIN
                        with open(file_path, "wb") as f:
                            f.write(data)
                    
                    self.after(0, lambda: messagebox.showinfo("Export Memory", f"Successfully exported {len(data)} bytes to {os.path.basename(file_path)}"))
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror("Export Memory", f"Error writing file: {e}"))
            else:
                self.after(0, lambda: messagebox.showerror("Export Memory", f"GDB Error: {rest}"))

        self.gdb.send_command(f'-data-read-memory-bytes {addr} {count}', callback)

    def export_registers(self):
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("JSON files", "*.json"), ("All files", "*.*")],
            title="Export Registers"
        )
        if not file_path:
            return

        def reg_callback(result_class, rest):
            if result_class == "^done":
                vals = re.findall(r'number="(\d+)",value="([^"]+)"', rest)
                try:
                    if file_path.endswith(".json"):
                        export_data = {f"r{num}": val for num, val in vals}
                        with open(file_path, "w") as f:
                            json.dump(export_data, f, indent=4)
                    else: # Default to CSV
                        with open(file_path, "w") as f:
                            f.write("Register,Value\n")
                            for num, val in vals:
                                f.write(f"r{num},{val}\n")
                    
                    self.after(0, lambda: messagebox.showinfo("Export Registers", f"Successfully exported {len(vals)} registers to {os.path.basename(file_path)}"))
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror("Export Registers", f"Error writing file: {e}"))
            else:
                self.after(0, lambda: messagebox.showerror("Export Registers", f"GDB Error: {rest}"))

        self.gdb.send_command("-data-list-register-values x", reg_callback)

    def show_memory_plotter(self):
        # Create a non-modal configuration dialog
        plot_win = tk.Toplevel(self)
        plot_win.title("Memory Plotter")
        plot_win.geometry("400x300")

        ttk.Label(plot_win, text="Address:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        addr_var = tk.StringVar(value=self.mem_addr_entry.get())
        ttk.Entry(plot_win, textvariable=addr_var).grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

        ttk.Label(plot_win, text="Count (elements):").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        count_var = tk.IntVar(value=64)
        ttk.Entry(plot_win, textvariable=count_var).grid(row=1, column=1, padx=5, pady=5, sticky=tk.EW)

        ttk.Label(plot_win, text="Data Type:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        type_var = tk.StringVar(value="uint8")
        type_combo = ttk.Combobox(plot_win, textvariable=type_var, values=["uint8", "int8", "uint16", "int16", "uint32", "int32"])
        type_combo.grid(row=2, column=1, padx=5, pady=5, sticky=tk.EW)

        ttk.Label(plot_win, text="Refresh (ms):").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        refresh_var = tk.IntVar(value=1000)
        ttk.Entry(plot_win, textvariable=refresh_var).grid(row=3, column=1, padx=5, pady=5, sticky=tk.EW)

        canvas = tk.Canvas(plot_win, bg="white", height=150)
        canvas.grid(row=4, column=0, columnspan=2, padx=5, pady=5, sticky=tk.NSEW)

        plot_win.grid_rowconfigure(4, weight=1)
        plot_win.grid_columnconfigure(1, weight=1)

        running_plot = [False]

        def update_plot():
            if not plot_win.winfo_exists() or not running_plot[0]:
                return

            addr = addr_var.get()
            count = count_count = count_var.get()
            d_type = type_var.get()
            
            # Map type to byte size
            size_map = {"uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "uint32": 4, "int32": 4}
            elem_size = size_map.get(d_type, 1)
            total_bytes = count * elem_size

            def callback(result_class, rest):
                if result_class == "^done" and "contents" in rest:
                    match = re.search(r'contents="([0-9a-fA-F]+)"', rest)
                    if match:
                        hex_data = match.group(1)
                        data_bytes = bytes.fromhex(hex_data)
                        values = []
                        for i in range(0, len(data_bytes), elem_size):
                            chunk = data_bytes[i:i+elem_size]
                            if len(chunk) < elem_size: break
                            
                            if d_type == "uint8": val = chunk[0]
                            elif d_type == "int8": val = int.from_bytes(chunk, "little", signed=True)
                            elif d_type == "uint16": val = int.from_bytes(chunk, "little", signed=False)
                            elif d_type == "int16": val = int.from_bytes(chunk, "little", signed=True)
                            elif d_type == "uint32": val = int.from_bytes(chunk, "little", signed=False)
                            elif d_type == "int32": val = int.from_bytes(chunk, "little", signed=True)
                            else: val = chunk[0]
                            values.append(val)
                        
                        self.after(0, lambda: draw_values(values))
                
                if running_plot[0]:
                    self.after(refresh_var.get(), update_plot)

            self.gdb.send_command(f'-data-read-memory-bytes {addr} {total_bytes}', callback)

        def draw_values(values):
            if not canvas.winfo_exists(): return
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if not values or len(values) < 2: return

            min_v = min(values)
            max_v = max(values)
            if max_v == min_v:
                max_v += 1
                min_v -= 1
            
            span = max_v - min_v
            dx = w / (len(values) - 1)
            
            points = []
            for i, v in enumerate(values):
                x = i * dx
                y = h - ((v - min_v) / span * h)
                points.append((x, y))
            
            for i in range(len(points) - 1):
                canvas.create_line(points[i][0], points[i][1], points[i+1][0], points[i+1][1], fill="blue")
            
            canvas.create_text(5, 5, anchor=tk.NW, text=f"Max: {max_v}")
            canvas.create_text(5, h-5, anchor=tk.SW, text=f"Min: {min_v}")

        def toggle():
            if running_plot[0]:
                running_plot[0] = False
                btn_start.config(text="Start Plotting")
            else:
                running_plot[0] = True
                btn_start.config(text="Stop Plotting")
                update_plot()

        btn_start = ttk.Button(plot_win, text="Start Plotting", command=toggle)
        btn_start.grid(row=5, column=0, columnspan=2, pady=5)
        
        plot_win.protocol("WM_DELETE_WINDOW", lambda: [running_plot.__setitem__(0, False), plot_win.destroy()])

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

    def _update_threads(self):
        def threads_callback(result_class, rest):
            if result_class == "^done":
                # rest looks like: threads=[{id="1",target-id="Thread 1.1",name="main",state="stopped",frame={...}},...]
                # current-thread-id="1"
                
                # Extract the threads list
                threads = []
                pos = 0
                threads_match = re.search(r'threads=\[', rest)
                if threads_match:
                    pos = threads_match.end() - 1
                    # Robustly find each thread={...}
                    while True:
                        match = re.search(r'\{', rest[pos:])
                        if not match: break
                        
                        start_idx = pos + match.start()
                        brace_count = 0
                        end_idx = -1
                        for i in range(start_idx, len(rest)):
                            if rest[i] == '{': brace_count += 1
                            elif rest[i] == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    end_idx = i
                                    break
                        if end_idx != -1:
                            thread_str = rest[start_idx+1 : end_idx]
                            threads.append(thread_str)
                            pos = end_idx + 1
                        else: break

                current_thread_id = None
                curr_match = re.search(r'current-thread-id="(\d+)"', rest)
                if curr_match:
                    current_thread_id = curr_match.group(1)

                def update_ui():
                    if not self.winfo_exists(): return
                    for item in self.threads_tree.get_children():
                        self.threads_tree.delete(item)
                    
                    for thread_str in threads:
                        tid = re.search(r'id="([^"]+)"', thread_str)
                        target_id = re.search(r'target-id="([^"]+)"', thread_str)
                        name = re.search(r'name="([^"]+)"', thread_str)
                        state = re.search(r'state="([^"]+)"', thread_str)
                        # Frame might be complex, just get func/line
                        frame_func = re.search(r'func="([^"]+)"', thread_str)
                        frame_line = re.search(r'line="(\d+)"', thread_str)
                        
                        tid_val = tid.group(1) if tid else "?"
                        target_val = target_id.group(1) if target_id else "?"
                        name_val = name.group(1) if name else ""
                        state_val = state.group(1) if state else "?"
                        
                        f_func = frame_func.group(1) if frame_func else ""
                        f_line = frame_line.group(1) if frame_line else ""
                        frame_val = f"{f_func}:{f_line}" if f_func else ""

                        item = self.threads_tree.insert("", tk.END, values=(tid_val, target_val, name_val, state_val, frame_val))
                        if tid_val == current_thread_id:
                            self.threads_tree.selection_set(item)

                self.after(0, update_ui)

        self.gdb.send_command("-thread-info", threads_callback)

    def _on_thread_double_click(self, event):
        selection = self.threads_tree.selection()
        if not selection: return
        
        thread_id = self.threads_tree.item(selection[0], "values")[0]
        
        def select_callback(result_class, rest):
            if result_class == "^done":
                self.log(f"Switched to thread {thread_id}")
                # Update UI for the new thread
                self._update_registers()
                self._update_call_stack()
                # Also request frame info to update source view
                self.gdb.send_command("-stack-info-frame", self._get_frame_info_callback())

        self.gdb.send_command(f"-thread-select {thread_id}", select_callback)

    def _get_frame_info_callback(self):
        def callback(result_class, rest):
            if result_class == "^done":
                f_match = re.search(r'fullname="([^"]+)"', rest)
                l_match = re.search(r'line="(\d+)"', rest)
                if f_match and l_match:
                    self._update_source_view(f_match.group(1).replace(r'\\', '\\'), int(l_match.group(1)))
        return callback

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

                            if self.coverage_enabled.get():
                                self._update_coverage_stats(func)

                            self.stack_tree.insert("", tk.END, text=level, values=(func, os.path.basename(fullname), line), tags=(fullname,))

                        self.update_idletasks()
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

            self.line_numbers.yview_moveto(self.source_text.yview()[0])

            self._apply_syntax_highlighting()
            self._refresh_source_tags()

        self.source_text.tag_remove("current_line", '1.0', tk.END)
        self.line_numbers.tag_remove("current_line", '1.0', tk.END)
        self.line_numbers.tag_remove("inline_var_padding", '1.0', tk.END)
        self.source_text.tag_remove("hit_breakpoint", '1.0', tk.END)
        self.line_numbers.tag_remove("hit_breakpoint", '1.0', tk.END)
        self.source_text.tag_remove("inline_var", '1.0', tk.END)

        # Remove previous inline variables from text
        self._clear_inline_variables()

        if line > 0:
            self.current_line = line
            # Highlights are now handled by _refresh_source_tags
            self._refresh_source_tags()
            self.source_text.see(f"{line}.0")
            self.line_numbers.see(f"{line}.0")

        self.update_idletasks() # Force refresh

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

    def _on_toggle_coverage(self):
        if self.coverage_enabled.get():
            self._get_all_functions()
            self._start_coverage_timer()
        else:
            self._update_coverage_ui()

    def _start_coverage_timer(self):
        if self.coverage_enabled.get() and self.target_connected:
            self._update_on_the_fly_coverage()
            self.after(1000, self._start_coverage_timer)

    def _update_on_the_fly_coverage(self):
        if not self.is_running:
            return

        def pc_callback(result_class, rest):
            if result_class == "^done":
                # Extract value="0x..."
                match = re.search(r'value="([^"]+)"', rest)
                if match:
                    pc_val = match.group(1)
                    # Get function name for this PC
                    def func_callback(res_class, res_rest):
                        if res_class == "^done":
                            # console output like "0x08000100 in main ()\n"
                            f_match = re.search(r'in\s+([\w\d_]+)', res_rest)
                            if f_match:
                                self._update_coverage_stats(f_match.group(1))

                    self.gdb.send_command(f'-interpreter-exec console "info symbol {pc_val}"', func_callback)

        # We try to get PC even if running, though many GDB servers won't allow this
        # unless non-stop mode is on. But if it fails, it just won't update "on the fly".
        self.gdb.send_command("-data-evaluate-expression $pc", pc_callback)

    def _reset_coverage(self):
        self.hit_functions = {}
        for func in self.all_functions:
            self.hit_functions[func] = 0
        self._update_coverage_ui()

    def _get_all_functions(self):
        if not self.elf_path:
            return

        # Attempt to use 'nm' to get all functions from the ELF file
        # 'nm' is typically in the same directory as 'gdb'
        nm_path = self.gdb.gdb_path.replace("-gdb", "-nm").replace("gdb", "nm")
        if not os.path.exists(nm_path):
            # Fallback to system 'nm'
            nm_path = shutil.which("arm-none-eabi-nm") or shutil.which("nm") or "nm"

        try:
            self.debug_log(f"Running nm to get functions from {self.elf_path} using {nm_path}", "info")
            # -S: print size
            # --defined-only: only defined symbols
            # -n: sort numerically
            # We want all text (code) symbols: T or t
            res = subprocess.run([nm_path, "-S", "--defined-only", self.elf_path], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                self.all_functions = []
                self.hit_functions = {}
                self.enabled_functions = {}
                # Parse nm output. Format: <address> <size> <type> <name>
                # Type 'T' or 't' for functions in .text section
                lines = res.stdout.splitlines()
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 4:
                        sym_type = parts[2]
                        sym_name = parts[3]
                        if sym_type.upper() == 'T' and not sym_name.startswith('_'):
                            if sym_name not in self.all_functions:
                                self.all_functions.append(sym_name)
                                self.hit_functions[sym_name] = 0
                                self.enabled_functions[sym_name] = True

                self.all_functions.sort()
                self._update_coverage_ui()
                self.debug_log(f"Extracted {len(self.all_functions)} functions from ELF using nm.", "info")
                return
        except Exception as e:
            self.debug_log(f"Error running nm: {e}. Falling back to GDB 'info functions'.", "warn")

        # Fallback to GDB CLI 'info functions'
        if self.gdb.process:
            self.gdb.send_command('-interpreter-exec console "info functions"', self._parse_functions_callback)

    def _parse_functions_callback(self, result_class, rest):
        if result_class == "^done":
            self.collecting_functions = True
            self.all_functions = []
            self.hit_functions = {}
            self.enabled_functions = {}
            # The output comes as 'console' type responses in _poll_gdb_responses.

    def _process_console_for_functions(self, text):
        # Look for function names in "File path/to/file.c:\nstatic int func_name(args);\n"
        # or just "int func_name(args);"
        # GDB info functions output often has lines like:
        # File main.c:
        # 12:	void main(void);
        # 15:	static int helper(int);
        # Non-debugging symbols:
        # 0x08000100  Reset_Handler

        # Regex to match function names in info functions output:
        # 1. Debug symbols: [line_num]:\t[return_type] [func_name]([args]);
        # 2. Non-debug symbols: [address]  [func_name]

        debug_matches = re.findall(r'^\d+:\s+[\w\*\s]+?\s+([\w\d_]+)\s*\(', text, re.MULTILINE)
        non_debug_matches = re.findall(r'^0x[0-9a-fA-F]+\s+([\w\d_]+)$', text, re.MULTILINE)

        found_count = 0
        for m in debug_matches + non_debug_matches:
            if m.startswith('_'):
                continue
            if m not in self.all_functions:
                self.all_functions.append(m)
                self.hit_functions[m] = 0
                self.enabled_functions[m] = True
                found_count += 1

        if found_count > 0:
            self.all_functions.sort()
            self._update_coverage_ui()
            self.debug_log(f"Discovered {found_count} new functions. Total: {len(self.all_functions)}", "info")

    def _update_coverage_stats(self, func_name):
        if not self.coverage_enabled.get() or not func_name:
            return

        # Check if function is enabled for coverage
        if not self.enabled_functions.get(func_name, True):
            return

        if func_name in self.hit_functions:
            self.hit_functions[func_name] += 1
        else:
            # If it's a new function not in our initial list (e.g. dynamic or just found)
            if func_name not in self.all_functions:
                self.all_functions.append(func_name)
                self.all_functions.sort()
                self.enabled_functions[func_name] = True
            self.hit_functions[func_name] = 1

        self.after(0, self._schedule_coverage_ui_update)

    def _schedule_coverage_ui_update(self):
        if not self._coverage_update_pending:
            self._coverage_update_pending = True
            # Debounce: only update UI at most every 100ms
            self.after(100, self._do_coverage_ui_update)

    def _do_coverage_ui_update(self):
        self._coverage_update_pending = False
        if self.winfo_exists():
            self._update_coverage_ui()

    def _update_coverage_ui(self):
        # Only count functions that are enabled for coverage
        enabled_funcs = [f for f in self.all_functions if self.enabled_functions.get(f, True)]
        total = len(enabled_funcs)

        if total > 0:
            hit_count = sum(1 for func in enabled_funcs if self.hit_functions.get(func, 0) > 0)
            pct = (hit_count / total) * 100
            self.overall_coverage_pct.set(pct)
            self.coverage_pct_label.config(text=f"{pct:.1f}% ({hit_count}/{total} functions)")
        else:
            self.coverage_pct_label.config(text="0.0% (0/0 functions)")

        # Update Treeview
        # To avoid flickering, we can update existing items or clear and rebuild
        # Map existing items by function name (strip "☑ " or "☐ ")
        current_items = {}
        for item in self.coverage_tree.get_children():
            text = self.coverage_tree.item(item)['text']
            if len(text) > 2:
                func_name = text[2:]
                current_items[func_name] = item

        for func in self.all_functions:
            hits = self.hit_functions.get(func, 0)
            enabled = self.enabled_functions.get(func, True)
            check_mark = "☑" if enabled else "☐"
            display_name = f"{check_mark} {func}"

            if func in current_items:
                self.coverage_tree.item(current_items[func], text=display_name, values=(hits,))
            else:
                self.coverage_tree.insert("", tk.END, text=display_name, values=(hits,))

        # Clean up any items that might have been removed from all_functions
        all_funcs_set = set(self.all_functions)
        for func_name, item in current_items.items():
            if func_name not in all_funcs_set:
                self.coverage_tree.delete(item)

        self.last_hover_word = None

    def _on_tree_click(self, event):
        item_id = self.coverage_tree.identify_row(event.y)
        if not item_id:
            return

        column = self.coverage_tree.identify_column(event.x)
        # Only toggle if clicking the first column (#0)
        if column == "#0":
            display_name = self.coverage_tree.item(item_id, "text")
            # Extract function name (skip "☑ " or "☐ ")
            func_name = display_name[2:]

            # Toggle enabled state
            current_state = self.enabled_functions.get(func_name, True)
            self.enabled_functions[func_name] = not current_state

            # Update UI
            self._update_coverage_ui()

    def _on_global_toggle_all(self):
        new_state = self.global_coverage_all_checked.get()
        for func in self.all_functions:
            self.enabled_functions[func] = new_state
        self._update_coverage_ui()

    def quit(self):
        if hasattr(self, 'gdb'):
            self.gdb.stop()

        # Capture processes to stop
        procs = [self.jlink_process, self.openocd_process, self.stlink_process, self.ssh_tunnel_process]
        # Clear references to avoid race conditions in reader threads
        self.jlink_process = None
        self.openocd_process = None
        self.stlink_process = None
        self.ssh_tunnel_process = None

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
