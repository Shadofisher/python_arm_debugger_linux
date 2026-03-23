
import subprocess
import time

gdb_path = r"C:\Users\agtec\CLionProjects\MK3_GCC\arm-gcc\gcc-arm-none-eabi-10.3-2021.10\bin\arm-none-eabi-gdb.exe"

def try_0x03():
    process = subprocess.Popen(
        [gdb_path, "--interpreter=mi2", "-q"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False
    )
    
    # Read initial garbage
    while True:
        line = process.stdout.readline()
        if b"(gdb)" in line: break
    
    print("Sending 0x03 byte...")
    process.stdin.write(b"\x03")
    process.stdin.flush()
    time.sleep(0.5)
    
    # See if it prints anything
    while True:
        # Use non-blocking read if possible or short timeout
        line = process.stdout.readline()
        print(f"  GDB: {line.strip()}")
        if not line: break
        if b"(gdb)" in line: break
            
    process.stdin.write(b"-gdb-exit\n")
    process.stdin.flush()
    process.wait()

if __name__ == "__main__":
    try_0x03()
