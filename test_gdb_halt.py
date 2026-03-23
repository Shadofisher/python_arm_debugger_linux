
import os
import shutil
import time
import signal
import subprocess
from gdb_backend import GdbBackend

def test_halt():
    # Try multiple variants for ARM GDB
    gdb_path = (shutil.which("arm-none-eabi-gdb") or 
               shutil.which("arm-zephyr-eabi-gdb") or 
               shutil.which("gdb-multiarch"))
    
    if not gdb_path:
        # Check specific Zephyr SDK path as fallback for this system
        zephyr_gdb = "/home/graeme/zephyr-sdk-0.17.2/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb"
        if os.path.exists(zephyr_gdb):
            gdb_path = zephyr_gdb
        else:
            gdb_path = "gdb"

    print(f"Starting GDB from {gdb_path}...")
    gdb = GdbBackend(gdb_path)
    gdb.start()
    
    # Give it a moment to start
    time.sleep(1)
    
    print("Testing pause() with no target connected...")
    # This might not do much without a target, but let's see if it crashes
    gdb.halt()
    
    print("Checking for responses...")
    time.sleep(1)
    while not gdb.response_queue.empty():
        resp = gdb.response_queue.get()
        print(f"GDB Response: {resp}")

    gdb.stop_session()
    print("Test finished.")

if __name__ == "__main__":
    test_halt()
