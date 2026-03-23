import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import serial
import serial.tools.list_ports
import os
import struct
import threading
import time
from datetime import datetime

class BootloaderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("STM32F411 Bootloader Host")
        self.root.geometry("800x600")

        self.file_path = tk.StringVar()
        self.port = tk.StringVar()
        self.baudrate = tk.IntVar(value=115200)
        self.timeout = tk.IntVar(value=5) # Increased timeout for flash erase
        self.status = tk.StringVar(value="Disconnected")
        self.progress_val = tk.DoubleVar(value=0)
        self.console_input = tk.StringVar()
        self.highlight_phrase = tk.StringVar()
        self.highlight_color = tk.StringVar(value="yellow")
        self.is_dark_mode = tk.BooleanVar(value=False)

        self.ser = None
        self.reader_thread = None
        self.running = True

        self.setup_styles()
        self.create_widgets()
        self.apply_theme()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_styles(self):
        self.style = ttk.Style()
        self.style.theme_use('clam')

    def apply_theme(self):
        if self.is_dark_mode.get():
            bg_color = "#2b2b2b"
            fg_color = "#ffffff"
            text_bg = "#1e1e1e"
            text_fg = "#a9b7c6"
            btn_bg = "#3c3f41"
        else:
            bg_color = "#f0f0f0"
            fg_color = "#000000"
            text_bg = "#ffffff"
            text_fg = "#000000"
            btn_bg = "#e1e1e1"

        self.root.configure(bg=bg_color)
        self.style.configure("TFrame", background=bg_color)
        self.style.configure("TLabelframe", background=bg_color, foreground=fg_color)
        self.style.configure("TLabelframe.Label", background=bg_color, foreground=fg_color)
        self.style.configure("TLabel", background=bg_color, foreground=fg_color)
        self.style.configure("TButton", background=btn_bg, foreground=fg_color)
        self.style.configure("TProgressbar", thickness=10)

        self.log_text.configure(bg=text_bg, fg=text_fg, insertbackground=fg_color)
        self.debug_text.configure(bg=text_bg, fg=text_fg, insertbackground=fg_color)
        self.update_highlight_tag()

    def update_highlight_tag(self):
        color = self.highlight_color.get()
        self.debug_text.tag_configure("highlight", background=color, foreground="black")

    def toggle_theme(self):
        self.is_dark_mode.set(not self.is_dark_mode.get())
        self.apply_theme()

    def create_widgets(self):
        # Main Layout: Left for controls, Right for Debug
        main_pane = ttk.PanedWindow(self.root, orient="horizontal")
        main_pane.pack(fill="both", expand=True)

        left_frame = ttk.Frame(main_pane)
        main_pane.add(left_frame, weight=1)

        # Port Selection
        port_frame = ttk.LabelFrame(left_frame, text="Connection Settings")
        port_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(port_frame, text="Serial Port:").grid(row=0, column=0, padx=5, pady=5)
        self.port_combo = ttk.Combobox(port_frame, textvariable=self.port)
        self.port_combo.grid(row=0, column=1, padx=5, pady=5)
        self.refresh_ports()

        ttk.Button(port_frame, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=5, pady=5)

        ttk.Label(port_frame, text="Baudrate:").grid(row=1, column=0, padx=5, pady=5)
        ttk.Entry(port_frame, textvariable=self.baudrate).grid(row=1, column=1, padx=5, pady=5)

        ttk.Label(port_frame, text="Timeout (s):").grid(row=2, column=0, padx=5, pady=5)
        ttk.Entry(port_frame, textvariable=self.timeout).grid(row=2, column=1, padx=5, pady=5)

        self.connect_btn = ttk.Button(port_frame, text="Connect", command=self.toggle_connection)
        self.connect_btn.grid(row=3, column=0, columnspan=3, padx=5, pady=5, sticky="ew")

        # File Selection
        file_frame = ttk.LabelFrame(left_frame, text="Firmware File")
        file_frame.pack(fill="x", padx=10, pady=5)

        ttk.Entry(file_frame, textvariable=self.file_path, width=30).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(file_frame, text="Browse", command=self.browse_file).grid(row=0, column=1, padx=5, pady=5)

        # Actions
        action_frame = ttk.Frame(left_frame)
        action_frame.pack(fill="x", padx=10, pady=10)

        self.upload_btn1 = ttk.Button(action_frame, text="App 1", command=lambda: self.start_upload('u'))
        self.upload_btn1.pack(side="left", padx=5, expand=True, fill="x")

        self.upload_btn2 = ttk.Button(action_frame, text="App 2", command=lambda: self.start_upload('v'))
        self.upload_btn2.pack(side="left", padx=5, expand=True, fill="x")

        # Progress & Log
        progress_frame = ttk.LabelFrame(left_frame, text="Status & Log")
        progress_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.status_label = ttk.Label(progress_frame, textvariable=self.status)
        self.status_label.pack(pady=2)

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_val, maximum=100)
        self.progress_bar.pack(fill="x", padx=10, pady=2)

        self.log_text = tk.Text(progress_frame, height=8, width=40)
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

        # Theme Toggle
        ttk.Button(left_frame, text="Toggle Theme", command=self.toggle_theme).pack(pady=5)

        # Right Frame: Debug Console
        right_frame = ttk.Frame(main_pane)
        main_pane.add(right_frame, weight=2)

        debug_frame = ttk.LabelFrame(right_frame, text="Debug Information (Serial)")
        debug_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.debug_text = tk.Text(debug_frame, wrap="word")
        self.debug_text.pack(side="left", fill="both", expand=True, padx=5, pady=5)

        debug_scroll = ttk.Scrollbar(debug_frame, orient="vertical", command=self.debug_text.yview)
        debug_scroll.pack(side="right", fill="y")
        self.debug_text.configure(yscrollcommand=debug_scroll.set)

        # Console Input
        input_frame = ttk.Frame(right_frame)
        input_frame.pack(fill="x", padx=10, pady=5)

        self.input_entry = ttk.Entry(input_frame, textvariable=self.console_input)
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.input_entry.bind("<Return>", lambda e: self.send_console_msg())

        ttk.Button(input_frame, text="Send", command=self.send_console_msg).pack(side="right")

        ttk.Button(right_frame, text="Clear Debug", command=lambda: self.debug_text.delete(1.0, tk.END)).pack(pady=5)

        # Highlight Settings
        highlight_frame = ttk.LabelFrame(right_frame, text="Highlight Settings")
        highlight_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(highlight_frame, text="Phrase:").grid(row=0, column=0, padx=5, pady=5)
        ttk.Entry(highlight_frame, textvariable=self.highlight_phrase).grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        ttk.Label(highlight_frame, text="Color:").grid(row=0, column=2, padx=5, pady=5)
        color_combo = ttk.Combobox(highlight_frame, textvariable=self.highlight_color, values=["yellow", "green", "cyan", "red", "orange", "magenta"], width=10)
        color_combo.grid(row=0, column=3, padx=5, pady=5)
        color_combo.bind("<<ComboboxSelected>>", lambda e: self.update_highlight_tag())

        highlight_frame.columnconfigure(1, weight=1)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo['values'] = ports
        if ports:
            self.port_combo.current(0)

    def browse_file(self):
        filename = filedialog.askopenfilename(filetypes=[("Binary files", "*.bin"), ("All files", "*.*")])
        if filename:
            self.file_path.set(filename)

    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def debug_log(self, message):
        timestamp = datetime.now().strftime("[%H:%M:%S] ")
        # Prepend timestamp if it's a new line in the text widget
        last_char = self.debug_text.get("end-2c", "end-1c")
        prefix = timestamp if (not last_char or last_char == "\n") else ""
        
        # Replace all internal \n with \n + timestamp
        formatted_message = prefix + message.replace("\n", "\n" + timestamp)
        
        # If the original message ended with \n, the replace operation will have
        # added a timestamp at the very end of formatted_message. Remove it.
        if formatted_message.endswith(timestamp):
            formatted_message = formatted_message[:-len(timestamp)]
        
        start_index = self.debug_text.index(tk.END + "-1c")
        self.debug_text.insert(tk.END, formatted_message)
        
        phrase = self.highlight_phrase.get()
        if phrase:
            search_start = start_index
            while True:
                pos = self.debug_text.search(phrase, search_start, stopindex=tk.END, nocase=True)
                if not pos:
                    break
                end_pos = f"{pos}+{len(phrase)}c"
                self.debug_text.tag_add("highlight", pos, end_pos)
                search_start = end_pos

        self.debug_text.see(tk.END)

    def toggle_connection(self):
        if self.ser and self.ser.is_open:
            self.running = False
            if self.ser:
                self.ser.close()
            self.ser = None
            self.connect_btn.config(text="Connect")
            self.status.set("Disconnected")
            self.log("Disconnected.")
        else:
            try:
                self.ser = serial.Serial(self.port.get(), self.baudrate.get(), timeout=self.timeout.get())
                self.connect_btn.config(text="Disconnect")
                self.status.set("Connected")
                self.log(f"Connected to {self.port.get()}")
                self.running = True
                self.reader_thread = threading.Thread(target=self.serial_reader, daemon=True)
                self.reader_thread.start()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to connect: {str(e)}")

    def serial_reader(self):
        while self.running and self.ser and self.ser.is_open:
            try:
                if self.ser.in_waiting:
                    line = self.ser.read(self.ser.in_waiting).decode(errors='ignore')
                    if line:
                        self.root.after(0, self.debug_log, line)
                else:
                    time.sleep(0.1)
            except Exception as e:
                print(f"Reader Error: {e}")
                break

    def on_closing(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.root.destroy()

    def send_console_msg(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Warning", "Serial port is not connected")
            return
        
        msg = self.console_input.get()
        if not msg:
            return
            
        try:
            # Add CRLF if not present for standard console behavior
            full_msg = msg
            if not full_msg.endswith('\r') and not full_msg.endswith('\n'):
                full_msg += '\r\n'
            
            self.ser.write(full_msg.encode())
            # Local echo to debug console
            self.debug_log(f"> {full_msg}")
            self.console_input.set("")
        except Exception as e:
            self.log(f"Send Error: {e}")

    def start_upload(self, command):
        if not self.ser or not self.ser.is_open:
            messagebox.showerror("Error", "Please connect to a serial port first")
            return
        if not self.file_path.get() or not os.path.exists(self.file_path.get()):
            messagebox.showerror("Error", "Please select a valid binary file")
            return

        threading.Thread(target=self.upload_process, args=(command,), daemon=True).start()

    def upload_process(self, command):
        try:
            self.upload_btn1.state(['disabled'])
            self.upload_btn2.state(['disabled'])
            self.status.set("Uploading...")
            self.progress_val.set(0)

            # Protocol Constants
            PACKET_SIZE = 256
            ACK_BYTE = b'\x06'

            # Temporarily stop the reader thread processing or just let it be.
            # Actually, we need to read specific bytes here, so we might have conflict.
            # Best is to pause reader or handle it carefully.
            # Let's simple use a lock or flag.
            self.running = False # Stop reader
            time.sleep(0.2)
            self.ser.reset_input_buffer()

            # Wake up bootloader/send command
            self.ser.write(command.encode())

            # Wait for command ACK (0x06)
            cmd_ack = self.ser.read(1)
            if cmd_ack != ACK_BYTE:
                received_str = cmd_ack.decode(errors='ignore') if cmd_ack else "None"
                # Try to read any error message from bootloader
                time.sleep(0.1)
                extra = self.ser.read_all().decode(errors='ignore')
                if extra:
                    received_str += " " + extra
                self.log(f"Error: Did not receive command ACK. Received: {received_str} (Hex: {cmd_ack.hex()})")
                self.status.set("Error: No CMD ACK")
                self.running = True
                threading.Thread(target=self.serial_reader, daemon=True).start()
                return

            self.log("Command ACK received.")

            with open(self.file_path.get(), "rb") as f:
                data = f.read()

            file_size = len(data)
            self.log(f"File size: {file_size} bytes")

            # Send length
            self.ser.write(struct.pack("<I", file_size))

            # Wait for ACK (0x06)
            ack = self.ser.read(1)
            if ack != ACK_BYTE:
                received_str = ack.decode(errors='ignore') if ack else "None"
                # Try to read any error message from bootloader
                time.sleep(0.1)
                extra = self.ser.read_all().decode(errors='ignore')
                if extra:
                    received_str += " " + extra
                self.log(f"Error: Did not receive length ACK. Received: {received_str} (Hex: {ack.hex()})")
                self.status.set("Error: No ACK")
                self.running = True
                threading.Thread(target=self.serial_reader, daemon=True).start()
                return

            self.log("Length ACK received. Starting upload...")

            for i in range(0, file_size, PACKET_SIZE):
                packet = data[i:i+PACKET_SIZE]
                self.ser.write(packet)

                # Wait for packet ACK
                ack = self.ser.read(1)
                if ack != ACK_BYTE:
                    received_str = ack.decode(errors='ignore') if ack else "None"
                    self.log(f"Error: No ACK for packet at {i}. Received: {received_str} (Hex: {ack.hex()})")
                    self.status.set("Upload Failed")
                    self.running = True
                    threading.Thread(target=self.serial_reader, daemon=True).start()
                    return

                progress = (i + len(packet)) / file_size * 100
                self.progress_val.set(progress)
                self.status.set(f"Uploading... {int(progress)}%")

            self.log("Upload complete!")
            self.status.set("Success")

            # Read final response
            time.sleep(0.2)
            final_resp = self.ser.read_all().decode(errors='ignore')
            self.log(final_resp.strip())

        except Exception as e:
            self.log(f"Exception: {str(e)}")
            self.status.set("Error")
        finally:
            self.running = True
            threading.Thread(target=self.serial_reader, daemon=True).start()
            self.upload_btn1.state(['!disabled'])
            self.upload_btn2.state(['!disabled'])

if __name__ == "__main__":
    root = tk.Tk()
    app = BootloaderGUI(root)
    root.mainloop()
