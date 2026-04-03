# OzonePy Debugger User Manual

OzonePy is a graphical front-end for ARM GDB, designed to provide a modern and intuitive debugging experience for embedded systems. It supports various debug probes (J-Link, ST-LINK, OpenOCD) and provides a comprehensive set of features for debugging ARM-based microcontrollers.

## 1. Getting Started

### 1.1 Loading an ELF File
To start debugging, you first need to load your project's ELF file.
- Go to `File` -> `Open ELF...` and select your compiled `.elf` file.
- OzonePy will automatically locate and display the source files associated with the ELF.

### 1.2 Configuring the Debug Probe
Before connecting to the target, configure your debug probe settings:
- **J-Link**: `File` -> `J-Link Server Settings...` (Set Host, Port, Device Name, Interface, Speed).
- **ST-LINK**: `File` -> `ST-LINK Server Settings...`.
- **OpenOCD**: `File` -> `OpenOCD Server Settings...`.
- **GDB Server**: `File` -> `GDB Server Settings...` for manual GDB server connections.

### 1.3 Connecting to the Target
- Go to `Debug` -> `Connect Target` and choose your probe (e.g., `Connect via J-Link`).
- The status bar at the bottom will show "Connected" when successful.

### 1.4 Download and Run
- **Download**: Click the `⤓ Load` button in the toolbar or `Debug` -> `Download` to flash the ELF to the target.
- **Run to Main**: Click the `⌘ Main` button or `Debug` -> `Run to Main` to reset the target and stop at the `main()` function.

### 1.5 Remote GDB & SSH Tunneling
OzonePy supports connecting to a GDB server running on a remote machine using SSH tunneling. This allows you to debug hardware that is not directly connected to your local computer.

- Go to **Debug** -> **Connect** -> **Connect to Remote GDB Server...**
- **Target GDB Server Info**:
    - **Host**: The address of the GDB server *as seen from the remote machine* (usually `127.0.0.1`).
    - **Port**: The port the GDB server is listening on (e.g., `2331` for J-Link, `3333` for OpenOCD).
- **SSH Tunnel Settings**:
    - Check **Use SSH Tunnel**.
    - **SSH Host**: The IP address or hostname of the remote machine you can SSH into.
    - **SSH User**: Your username on the remote machine.
    - **SSH Identity (Optional)**: Path to your private SSH key.
- Click **Connect**. OzonePy will set up the tunnel and connect automatically.

---

## 2. Execution Control

OzonePy provides several ways to control the execution of your program:

| Action | Toolbar Button | Keyboard Shortcut | Description |
| :--- | :--- | :--- | :--- |
| **Go / Continue** | ▶ Go | `F5` | Resume program execution. |
| **Pause** | ⏸ Pause | `F6` | Interrupt the running program. |
| **Step Into** | ↓ Into | | Execute the next line of code, stepping into functions. |
| **Step Over** | ↷ Over | | Execute the next line of code without entering functions. |
| **Reset** | ⟳ Reset | | Reset the target microcontroller. |
| **Stop** | ⏹ Stop | `F9` | Stop the debugging session. |

---

## 3. Source Code View & Breakpoints

### 3.1 Navigating Source Files
- The **Files** tab in the sidebar shows a tree view of all source files. Double-click a file to open it.
- Use the **Source** dropdown in the toolbar to quickly switch between open files.

### 3.2 Breakpoints
- **Set/Clear Breakpoint**: Click on the line number in the source view to toggle a breakpoint.
- **Breakpoint Indicators**:
    - Red line number: Active breakpoint.
    - Blue line highlight: Current execution line.
- **Breakpoints Tab**: View and manage all active breakpoints. Right-click a breakpoint in the list to:
    - **Delete**: Remove the breakpoint.
    - **Edit Condition**: Set a C-style expression (e.g., `i == 10`). The program will only stop if the expression is true.
    - **Set Dependency**: Make this breakpoint depend on another one being hit first.
    - **Set Ignore Count**: Skip the breakpoint a specified number of times.

### 3.3 Watchpoints (Data Breakpoints)
- Click `Add Watchpoint` in the Breakpoints tab.
- Choose between **Write**, **Read**, or **Access** (Read/Write) watchpoints on a memory address or variable.

---

## 4. Debug Information Windows

### 4.1 Watches & Expressions
- The **Watches** tab allows you to monitor variables and complex C expressions.
- Click `Add Watch / Expression` to add a new item.
- Double-click a value to modify it manually.
- Values that changed since the last stop are highlighted in light blue.

### 4.2 Live Watch
- The **Live** tab allows you to monitor variables in real-time while the target is running (requires probe support like J-Link).
- Configure the update rate in milliseconds at the bottom of the tab.

### 4.3 Registers
- The **Registers** tab shows the current state of CPU registers.
- Use `Export Registers` to save the current state to a file.

### 4.4 Call Stack
- The **Call Stack** tab shows the chain of function calls that led to the current execution point.
- Double-click a frame to jump to that location in the source code and view local variables for that context.

### 4.5 Threads / RTOS Awareness
- The **Threads** tab displays active threads or RTOS tasks (if detected).
- Shows thread ID, name, state (Running, Blocked, etc.), and current frame.

### 4.6 Memory View
- The **Memory** tab allows you to inspect and modify raw memory.
- Enter an address (e.g., `0x20000000`) and click `Read`.
- Right-click in the memory view to:
    - **Edit**: Modify a byte.
    - **Plot Memory**: Open a real-time graph of an array in memory.
    - **Export Memory**: Save a range of memory to a file (CSV, JSON, BIN).

---

## 5. Advanced Features

### 5.1 Code Coverage
- Enable via `File` -> `Enable Code Coverage`.
- The **Coverage** tab shows hits per function and overall percentage.
- Source lines that have been executed are highlighted (if supported).

### 5.2 Hover Tooltips
- Hover your mouse over any variable in the source code to see its current value in a small tooltip.

### 5.3 Inline Variables
- Toggle `Debug` -> `Show Inline Variables` to see the values of variables directly at the end of the source lines where they are used.

### 5.4 Debug Log
- Go to `File` -> `Show Debug Log` to see the raw communication between OzonePy and GDB. This is useful for troubleshooting.

### 5.5 GDB Console
- A full-featured GDB console is available at the bottom of the interface.
- You can enter any GDB command manually.
- Output from GDB (including command responses and status updates) is displayed in the console window.

---

## 6. Customization
- **Colors**: Customize the source code highlighter and UI colors via `File` -> `Color Settings...`.
- **OS Selection**: Set your host operating system via `File` -> `Operating System` to ensure correct path handling.

### 6.1 SSH Tunnelling (Deep Dive)
SSH Tunneling is particularly useful when the debugging hardware (like a J-Link) is connected to a computer in a different location (the "Remote Host"), but you want to run the OzonePy debugger on your local machine.

- **Requirements**: The Remote Host must have an SSH server running and have the GDB server (J-Link, OpenOCD, etc.) already started and listening.
- **Mechanism**: OzonePy uses a subprocess to create a secure tunnel: `ssh -L [local_port]:[target_host]:[target_port] [user]@[ssh_host]`.
- **Target Host/Port**: These are relative to the *Remote Host*. Usually, if the GDB server is running on the same machine you SSH into, you use `127.0.0.1`.
- **Persistence**: The tunnel remains active as long as the debug session is open. Closing the debugger or disconnecting will terminate the SSH tunnel.
