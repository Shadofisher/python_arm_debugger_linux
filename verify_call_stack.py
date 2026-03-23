
import tkinter as tk
from tkinter import ttk
import re
import os
import threading
import queue

class MockGdb:
    def __init__(self):
        self.response_queue = queue.Queue()
        self.callbacks = {}
        self.token_counter = 0

    def send_command(self, command, callback=None):
        self.token_counter += 1
        token = str(self.token_counter)
        if callback:
            self.callbacks[token] = callback
        
        # Simulate asynchronous response
        if "-stack-list-frames" in command:
            # Typical MI response for stack-list-frames
            # Note: real MI uses comma separated frame={...} blocks
            response = 'stack=[frame={level="0",addr="0x08000440",func="main",file="main.c",fullname="/home/user/project/main.c",line="25",arch="armv7e-m"},frame={level="1",addr="0x08000210",func="Reset_Handler",file="startup_stm32f407vgtx.s",fullname="/home/user/project/startup_stm32f407vgtx.s",line="80",arch="armv7e-m"}]'
            # In a real scenario, the callback is called from the reader thread
            threading.Thread(target=lambda: callback("^done", response)).start()

class MockApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Call Stack Verification")
        self.geometry("600x400")
        
        self.gdb = MockGdb()
        
        # UI setup for Call Stack
        self.sidebar_tabs = ttk.Notebook(self)
        self.sidebar_tabs.pack(fill=tk.BOTH, expand=True)
        
        self.stack_frame = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(self.stack_frame, text="Call Stack")
        
        self.stack_tree = ttk.Treeview(self.stack_frame, columns=("Function", "File", "Line"), show='headings')
        self.stack_tree.heading("Function", text="Function")
        self.stack_tree.heading("File", text="File")
        self.stack_tree.heading("Line", text="Line")
        self.stack_tree.column("#0", width=50) # Level column
        self.stack_tree.pack(fill=tk.BOTH, expand=True)
        
        self.debug_text = tk.Text(self, height=10)
        self.debug_text.pack(fill=tk.BOTH, expand=True)

        self.btn = ttk.Button(self, text="Update Stack", command=self._update_call_stack)
        self.btn.pack()

    def debug_log(self, text, tag=None):
        print(f"DEBUG [{tag}]: {text}")
        self.debug_text.insert(tk.END, f"{text}\n")
        self.debug_text.see(tk.END)

    def _update_call_stack(self):
        def stack_callback(result_class, rest):
            def update_ui():
                if not self.winfo_exists():
                    return
                try:
                    if result_class == "^done":
                        self.debug_log(f"STACK RECV: {rest}", "mi-recv")
                        
                        frames = []
                        pos = 0
                        while True:
                            match = re.search(r'frame=\{', rest[pos:])
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
                                self.debug_log(f"STACK ERROR: Unbalanced braces", "error")
                                break
                    
                        if not frames:
                            self.debug_log(f"STACK ERROR: No frames found", "error")

                        for item in self.stack_tree.get_children():
                            self.stack_tree.delete(item)
                    
                        for i, frame_str in enumerate(frames):
                            f_match = re.search(r'fullname="([^"]+)"', frame_str)
                            if not f_match:
                                f_match = re.search(r'file="([^"]+)"', frame_str)
                            
                            l_match = re.search(r'line="(\d+)"', frame_str)
                            fn_match = re.search(r'func="([^"]+)"', frame_str)
                        
                            fullname = f_match.group(1).replace(r'\\', '\\') if f_match else ""
                            line = l_match.group(1) if l_match else ""
                            func = fn_match.group(1) if fn_match else "???"
                        
                            self.stack_tree.insert("", tk.END, text=str(i), values=(func, os.path.basename(fullname), line), tags=(fullname,))
                        
                        self.update_idletasks()
                        self.update()
                        print("UI updated successfully via after()")
                    self.update()
                except Exception as e:
                    print(f"UI update failed: {e}")

            # Ensure UI updates happen on the main thread
            self.after(0, update_ui)

        self.gdb.send_command("-stack-list-frames --high-frame 9", stack_callback)

if __name__ == "__main__":
    app = MockApp()
    # Automatically trigger update after 1 second
    app.after(1000, app._update_call_stack)
    # Stop after 3 seconds
    app.after(3000, app.destroy)
    app.mainloop()
    
    print("Verification script finished.")
