import sys
import os
import threading
import queue
import time

# Mock GDB responses
class MockGDBProcess:
    def __init__(self):
        self.stdin = self
        self.stdout = self
        self.stderr = self
        self.responses = queue.Queue()
        self.commands_received = []

    def write(self, data):
        self.commands_received.append(data)
        import re
        match = re.match(r'^(\d+)?(.*)', data.strip())
        token = match.group(1) if match else ""
        cmd = match.group(2) if match else data.strip()
        
        print(f"MOCK RECV: token={token}, cmd={cmd}")

        if "-file-exec-and-symbols" in cmd:
            self.responses.put(f"{token}^done\n")
        elif "-break-insert -t main" in cmd:
            if hasattr(self, 'fail_bp') and self.fail_bp:
                self.responses.put(f'{token}^error,msg="No symbol table is loaded.  Use the \\"file\\" command."\n')
                self.fail_bp = False # Succeed on next retry
            else:
                self.responses.put(f"{token}^done,bkpt={{number=\"1\"}}\n")
        elif "-exec-continue" in cmd:
             self.responses.put(f"{token}^running\n")
             self.responses.put("*stopped,reason=\"breakpoint-hit\",frame={func=\"main\"}\n")
        elif "-stack-info-frame" in cmd:
             self.responses.put(f"{token}^done,frame={{func=\"main\",file=\"main.c\",fullname=\"/path/to/main.c\",line=\"10\"}}\n")
        elif "monitor reset halt" in cmd:
             self.responses.put(f"{token}^done\n")
        elif "interpreter-exec console \"monitor reset halt\"" in cmd:
             self.responses.put(f"{token}^done\n")
        elif "interpreter-exec console \"monitor reset\"" in cmd:
             self.responses.put(f"{token}^done\n")
        else:
             self.responses.put(f"{token}^done\n")

    def flush(self):
        pass

    def readline(self):
        return self.responses.get()

    def buffer(self):
        return self

# Monkeypatch subprocess.Popen
import subprocess
original_popen = subprocess.Popen
def mock_popen(*args, **kwargs):
    return MockGDBProcess()
subprocess.Popen = mock_popen

from gdb_backend import GdbBackend
from ozone_py import OzonePy
import tkinter as tk

def test_run_to_main_reload():
    print("Testing Run to Main with ELF reload...")
    root = tk.Tk()
    app = OzonePy()
    app.target_connected = True
    app.elf_path = "test.elf"
    
    mock_process = app.gdb.process
    mock_process.fail_bp = True # Simulate "No symbol table"
    
    app.run_to_main()
    
    # Process events
    for _ in range(100):
        root.update()
        time.sleep(0.01)
        
    cmds = mock_process.commands_received
    print(f"Commands sent: {cmds}")
    
    assert any("-file-exec-and-symbols" in c for c in cmds), "Should have reloaded ELF"
    assert cmds.count("-break-insert -t main") >= 2, "Should have retried breakpoint"
    
    print("Test Passed!")
    root.destroy()

if __name__ == "__main__":
    try:
        test_run_to_main_reload()
    except Exception as e:
        print(f"Test FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
