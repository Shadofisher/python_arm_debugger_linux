
import time
import queue
from gdb_backend import GdbBackend

def verify():
    # Final working path for ARM-compatible GDB
    gdb_path = "/home/graeme/zephyr-sdk-0.17.2/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb"
    
    print(f"Starting ARM GDB from {gdb_path}...")
    backend = GdbBackend(gdb_path)
    backend.start()
    
    # Wait for GDB to initialize
    time.sleep(1)
    
    print("Setting up GDB environment...")
    backend.send_command("-gdb-set architecture armv7e-m")
    backend.send_command("interpreter-exec console \"set remotetimeout 20\"")
    time.sleep(0.5)
    
    print("Connecting to target at 127.0.0.1:3333...")
    # 1. Connect command
    backend.send_command("-target-select remote 127.0.0.1:3333")
    time.sleep(2)
    
    # 2. Post-connect setup (matching OzonePy)
    print("Setting up post-connection parameters...")
    backend.send_command("-gdb-set target-async on")
    backend.send_command("-gdb-set non-stop on")
    backend.send_command("interpreter-exec console \"maint flush register-cache\"")
    time.sleep(0.5)
    
    # 3. Halt command (to see if it's responsive)
    print("Halting target...")
    backend.halt()
    time.sleep(1)
    
    print("\n--- GDB Response Analysis ---")
    connection_success = False
    
    while not backend.response_queue.empty():
        try:
            resp = backend.response_queue.get_nowait()
            msg_type = resp[0]
            content = resp[-1] # Usually the most interesting part
            
            print(f"[{msg_type}] {resp[1:]}")
            
            # Key markers for success
            all_content = " ".join(str(x) for x in resp)
            if "^connected" in all_content:
                connection_success = True
            if "*stopped" in all_content and 'arch="arm"' in all_content:
                print("[✓] Correct ARM architecture detected!")
        except queue.Empty:
            break
            
    if connection_success:
        print("\n[SUCCESS] Successfully connected to ARM target!")
    else:
        print("\n[FAILED] Could not confirm connection. Check if JLinkGDBServer is running.")

    backend.stop_session()

if __name__ == "__main__":
    verify()
