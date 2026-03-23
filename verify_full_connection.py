
import os
import subprocess
import time
import queue
import signal
from gdb_backend import GdbBackend

def test_full_connection():
    # 1. Configuration
    stlink_gdbserver = "/opt/st/stm32cubeide_1.16.1/plugins/com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.linux64_2.1.400.202404281720/tools/bin/ST-LINK_gdbserver"
    gdb_path = "/home/graeme/zephyr-sdk-0.17.2/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb"

    # Optional cubeprogrammer path if needed for some initialization
    # stm32_prog_cli = "/opt/st/stm32cubeide_1.16.1/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.1.400.202404281720/tools/bin/STM32_Programmer_CLI"

    print("--- Starting ST-LINK GDB Server (1.16.1) ---")
    # Using typical flags for ST-LINK_gdbserver
    # -p 3333 : port
    # -cp <path_to_cubeprog_bin> : usually needed
    cubeprog_bin = "/opt/st/stm32cubeide_1.16.1/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.1.400.202404281720/tools/bin"

    server_cmd = [
        stlink_gdbserver,
        "-p", "3333",
        "-cp", cubeprog_bin,
        "-d", # debug mode for more logs if needed
        "-k"  # kill server on GDB disconnect (or keep it? -k is usually 'init under reset' in some versions, but let's check)
    ]

    # In some versions of ST-LINK_gdbserver -k means 'init under reset' or 'keep alive'
    # According to typical ST usage: -k means 'Connect under reset'

    print(f"Command: {' '.join(server_cmd)}")

    try:
        server_process = subprocess.Popen(
            server_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid
        )
    except Exception as e:
        print(f"[FAIL] Could not start ST-LINK_gdbserver: {e}")
        return

    # Give the server a moment to start and bind to the port
    time.sleep(3)

    # Check if server is still running
    if server_process.poll() is not None:
        stdout, stderr = server_process.communicate()
        print("[FAIL] ST-LINK_gdbserver exited immediately.")
        print(f"STDOUT: {stdout}")
        print(f"STDERR: {stderr}")
        return

    print("[OK] ST-LINK_gdbserver seems to be running.")

    print("\n--- Starting ARM GDB and attempting connection ---")
    backend = GdbBackend(gdb_path)
    backend.start()

    # Wait for GDB to initialize
    time.sleep(1)

    print("Setting up GDB environment...")
    backend.send_command("-gdb-set architecture armv7e-m")
    backend.send_command("interpreter-exec console \"set remotetimeout 20\"")
    time.sleep(0.5)

    print("Connecting to target at 127.0.0.1:3333...")
    backend.send_command("-target-select remote 127.0.0.1:3333")

    # Wait for connection results
    time.sleep(3)

    print("Requesting target status (halt)...")
    backend.halt()
    time.sleep(1)

    print("\n--- GDB Response Analysis ---")
    connection_success = False
    target_info = ""

    while not backend.response_queue.empty():
        try:
            resp = backend.response_queue.get_nowait()
            msg_type = resp[0]
            print(f"[{msg_type}] {resp[1:]}")

            all_content = str(resp)
            if "^connected" in all_content:
                connection_success = True
            if "*stopped" in all_content:
                target_info = all_content
        except queue.Empty:
            break

    if connection_success:
        print("\n[SUCCESS] Successfully connected to target via ST-LINK_gdbserver (1.16.1)!")
        if target_info:
            print(f"Target is halted: {target_info}")
    else:
        print("\n[FAILED] Could not confirm connection to target.")
        print("Checking server output for clues...")
        # We can't easily read from the pipe without blocking, but let's try a quick check
        try:
            # Non-blocking read attempt
            outs, errs = server_process.communicate(timeout=1)
            print(f"Server STDOUT: {outs}")
            print(f"Server STDERR: {errs}")
        except subprocess.TimeoutExpired:
            # Process still running, which is expected if it didn't fail
            pass

    # Cleanup
    print("\n--- Cleaning up ---")
    backend.stop_session()

    try:
        # Kill the process group
        os.killpg(os.getpgid(server_process.pid), signal.SIGTERM)
        print("ST-LINK_gdbserver terminated.")
    except Exception as e:
        print(f"Error terminating server: {e}")

if __name__ == "__main__":
    test_full_connection()
