import os
import shutil
import sys

# Copy the GDB_PATH logic from ozone_py.py to verify it works on this machine
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
            GDB_PATH = shutil.which("gdb") or "arm-none-eabi-gdb"

    # Priority for ST GDB
    if os.path.exists(st_gdb_path):
        import subprocess
        try:
            res = subprocess.run([st_gdb_path, "--version"], capture_output=True, text=True, timeout=1)
            if res.returncode == 0:
                GDB_PATH = st_gdb_path
            else:
                print(f"ST GDB at {st_gdb_path} exists but fails to run. Falling back.")
                if os.path.exists(zephyr_gdb):
                    GDB_PATH = zephyr_gdb
        except Exception:
            if os.path.exists(zephyr_gdb):
                GDB_PATH = zephyr_gdb
    elif GDB_PATH == "gdb" or (shutil.which("gdb") and GDB_PATH == shutil.which("gdb")):
        if os.path.exists(zephyr_gdb):
            GDB_PATH = zephyr_gdb

print(f"Resolved GDB_PATH: {GDB_PATH}")
if os.path.exists(GDB_PATH) or shutil.which(GDB_PATH):
    print("GDB exists and is accessible!")
else:
    print("GDB NOT FOUND!")
    sys.exit(1)
