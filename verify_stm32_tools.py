import os
import shutil
import subprocess

def check_path(path, name):
    print(f"Checking {name}: {path}")
    if os.path.exists(path):
        print(f"  [OK] Path exists.")
        if os.path.isfile(path):
            if os.access(path, os.X_OK):
                print(f"  [OK] File is executable.")
                return True
            else:
                print(f"  [FAIL] File is NOT executable.")
        elif os.path.isdir(path):
            print(f"  [OK] Directory exists.")
            return True
    else:
        print(f"  [FAIL] Path does NOT exist.")
    return False

def check_stm32_tools():
    # Tools provided by user
    stm32_prog_cli = "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.2.200.202503041107/tools/bin/STM32_Programmer_CLI"
    stlink_gdbserver = "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.linux64_2.2.200.202505060755/tools/bin/ST-LINK_gdbserver"

    # GDB path used in the app (from stm32cubeide 1.19.0)
    st_gdb_path = "/opt/st/stm32cubeide_1.19.0/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.13.3.rel1.linux64_1.0.0.202410170706/tools/bin/arm-none-eabi-gdb"

    results = []
    results.append(check_path(stm32_prog_cli, "STM32_Programmer_CLI"))
    results.append(check_path(stlink_gdbserver, "ST-LINK_gdbserver"))
    results.append(check_path(st_gdb_path, "arm-none-eabi-gdb (CLT)"))

    if all(results):
        print("\nAll critical STM32 tools found and verified.")

        print("\nTrying to run ST-LINK_gdbserver --help...")
        try:
            res = subprocess.run([stlink_gdbserver, "--help"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0 or "Usage" in res.stdout or "Usage" in res.stderr:
                print("  [OK] ST-LINK_gdbserver executed successfully.")
            else:
                print(f"  [WARNING] ST-LINK_gdbserver returned code {res.returncode}")
        except Exception as e:
            print(f"  [FAIL] Could not execute ST-LINK_gdbserver: {e}")

        print("\nTrying to run STM32_Programmer_CLI --version...")
        try:
            res = subprocess.run([stm32_prog_cli, "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0 or "STM32CubeProgrammer" in res.stdout:
                print("  [OK] STM32_Programmer_CLI executed successfully.")
                print(f"  Version info: {res.stdout.splitlines()[0] if res.stdout else 'N/A'}")
            else:
                print(f"  [WARNING] STM32_Programmer_CLI returned code {res.returncode}")
        except Exception as e:
            print(f"  [FAIL] Could not execute STM32_Programmer_CLI: {e}")

        print("\nTrying to run arm-none-eabi-gdb --version...")
        try:
            res = subprocess.run([st_gdb_path, "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                print("  [OK] arm-none-eabi-gdb executed successfully.")
            else:
                print(f"  [FAIL] arm-none-eabi-gdb returned code {res.returncode}")
                if "libncurses.so.5" in res.stderr or "libncurses.so.5" in res.stdout:
                    print("  [DETECTED] Missing libncurses.so.5 library.")
        except Exception as e:
            print(f"  [FAIL] Could not execute arm-none-eabi-gdb: {e}")

    else:
        print("\nSome tools were NOT found. Please check the paths.")

if __name__ == "__main__":
    check_stm32_tools()
