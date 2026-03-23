
import subprocess
import os
import time

gdb_path = r"C:\Users\agtec\CLionProjects\MK3_GCC\arm-gcc\gcc-arm-none-eabi-10.3-2021.10\bin\arm-none-eabi-gdb.exe"

def try_commands(commands):
    process = subprocess.Popen(
        [gdb_path, "--interpreter=mi2", "-q"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    for cmd in commands:
        print(f"Trying: {cmd}")
        process.stdin.write(f"{cmd}\n")
        process.stdin.flush()
        time.sleep(0.1)
        
        # Read until we see a response or prompt
        while True:
            line = process.stdout.readline()
            print(f"  GDB: {line.strip()}")
            if "^done" in line or "^error" in line or "(gdb)" in line:
                break
    
    process.stdin.write("-gdb-exit\n")
    process.stdin.flush()
    process.wait()

if __name__ == "__main__":
    try_commands([
        "-exec-interrupt",
        "-exec-pause",
        "-target-interrupt",
        "-target-pause",
        "interrupt",
        "-interpreter-exec console \"interrupt\"",
        "-interpreter-exec console \"halt\"",
    ])
