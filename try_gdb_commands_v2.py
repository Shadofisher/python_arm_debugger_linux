
import subprocess
import time

gdb_path = r"C:\Users\agtec\CLionProjects\MK3_GCC\arm-gcc\gcc-arm-none-eabi-10.3-2021.10\bin\arm-none-eabi-gdb.exe"

def run_cmd(cmd):
    process = subprocess.Popen(
        [gdb_path, "--interpreter=mi2", "-q"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Read initial garbage
    while True:
        line = process.stdout.readline()
        if "(gdb)" in line: break
    
    print(f"Executing: {cmd}")
    process.stdin.write(f"{cmd}\n")
    process.stdin.flush()
    
    while True:
        line = process.stdout.readline()
        print(f"  GDB: {line.strip()}")
        if "^done" in line or "^error" in line:
            break
            
    process.stdin.write("-gdb-exit\n")
    process.stdin.flush()
    process.wait()

if __name__ == "__main__":
    for cmd in ["-exec-interrupt", "-exec-pause", "interrupt"]:
        run_cmd(cmd)
        print("-" * 20)
