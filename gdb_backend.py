import subprocess
import threading
import queue
import re
import os

class GdbBackend:
    def __init__(self, gdb_path, on_response_callback=None):
        self.gdb_path = gdb_path
        self.process = None
        self.response_queue = queue.Queue()
        self.running = False
        self.token_counter = 0
        self.callbacks = {}
        self.on_response_callback = on_response_callback
        self.target_connected = False

    def start(self):
        if self.process:
            return

        # We use CREATE_NEW_PROCESS_GROUP on Windows to allow sending Ctrl-C
        # although with target-async we prefer -exec-interrupt
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        self.process = subprocess.Popen(
            [self.gdb_path, "--interpreter=mi2", "-q"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=creationflags
        )

        self.running = True
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        # Enable mi-async to allow MI commands like -exec-interrupt
        # while the target is running. This is the proper way in GDB spec.
        self.send_command("-gdb-set mi-async on")

    def stop(self):
        self.running = False
        if self.process:
            try:
                # Close stdin to signal EOF to GDB
                if self.process.stdin:
                    try:
                        self.process.stdin.close()
                    except Exception:
                        pass

                self.process.terminate()
                try:
                    self.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            except Exception:
                pass
            self.process = None

    def halt(self):
        """
        Pauses the execution of the target.
        According to GDB MI specification, the proper way to interrupt
        execution is the -exec-interrupt command.
        """
        if not self.process:
            return False

        # If we aren't connected to a target, -exec-interrupt will fail with
        # "You can't do that when your target is `exec'".
        if not self.target_connected:
            return False

        # Use the MI command as the primary method.
        # With target-async on, this should work even if GDB is "busy".
        try:
            self.process.stdin.write("-exec-interrupt\n")
            self.process.stdin.flush()
            self.response_queue.put(('mi-send', "-exec-interrupt"))
            if self.on_response_callback:
                self.on_response_callback()
        except Exception as e:
            self.response_queue.put(('stderr', f"Error sending -exec-interrupt: {e}"))
            # If primary method fails, we fallback to signals immediately
            self._send_interrupt_signals()

        # We also wait a bit to see if it responds, if not, we try signals
        # But for now, let's keep it simple and see if just -exec-interrupt is enough
        # Actually, some older GDB versions OR some GDB servers still need signals.
        # But sending both simultaneously might be what kills GDB.

        return True

    def _send_interrupt_signals(self):
        """Fallback methods for interrupting GDB if -exec-interrupt fails or is slow."""
        if not self.process:
            return

        # Try sending 0x03 (ETX) which many GDB builds on Windows
        # treat as a SIGINT when received on stdin.
        try:
            # We must use the raw buffer for bytes
            self.process.stdin.buffer.write(b"\x03")
            self.process.stdin.buffer.flush()
            # self.response_queue.put(('mi-send', "<0x03 signal>")) # Removed redundant log
            # if self.on_response_callback:
            #     self.on_response_callback()
        except Exception:
            # If it's a text-mode wrapper, this might fail, try the regular write
            try:
                self.process.stdin.write("\x03")
                self.process.stdin.flush()
                # self.response_queue.put(('mi-send', "<0x03 signal (fallback)>")) # Removed redundant log
                if self.on_response_callback:
                    self.on_response_callback()
            except Exception:
                pass

        # Signal-based fallback
        if os.name == 'nt':
            import signal
            try:
                os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
                self.response_queue.put(('mi-send', "<CTRL-BREAK signal>"))
                if self.on_response_callback:
                    self.on_response_callback()
            except Exception:
                pass
        else:
            import signal
            try:
                os.kill(self.process.pid, signal.SIGINT)
                self.response_queue.put(('mi-send', "<SIGINT signal>"))
                if self.on_response_callback:
                    self.on_response_callback()
            except Exception:
                pass

    def stop_session(self):
        self.running = False
        if self.process:
            try:
                # Try to interrupt first so it can exit cleanly
                self.halt()

                if self.process.stdin:
                    try:
                        self.process.stdin.write("-gdb-exit\n")
                        self.process.stdin.flush()
                    except Exception:
                        pass

                try:
                    self.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.wait(timeout=1.0)
            except Exception:
                pass
            self.process = None

    def send_command(self, command, callback=None):
        if not self.process:
            return

        # Don't send redundant mi-async on, it's already in start()
        if command == "-gdb-set mi-async on" and self.token_counter > 0:
            return

        # We also want to ensure remotetimeout is only set once if it's the same value
        # to avoid flooding the GDB input buffer during connection retries
        if "set remotetimeout" in command and self.target_connected:
             # If we are already connected, we don't need to keep setting it
             pass

        self.token_counter += 1
        token = str(self.token_counter)
        if callback:
            self.callbacks[token] = callback

        full_command = f"{token}{command}\n"
        self.response_queue.put(('mi-send', full_command.strip()))
        if self.on_response_callback:
            self.on_response_callback()
        self.process.stdin.write(full_command)
        self.process.stdin.flush()
        return token

    def _read_stdout(self):
        try:
            while self.running and self.process and self.process.stdout:
                line = self.process.stdout.readline()
                if not line:
                    break

                # Simplified MI2 parsing
                # Format: [token]^result-class[,result=value,...]
                # Format: *exec-async-output
                # Format: +status-async-output
                # Format: =notify-async-output
                # Format: console-stream-output (prefixed with ~)
                # Format: target-stream-output (prefixed with @)
                # Format: log-stream-output (prefixed with &)

                line_stripped = line.strip()
                if not line_stripped:
                    continue

                # Log everything for debugging
                if "stack-list-frames" in line_stripped or "frame=" in line_stripped or "stopped" in line_stripped:
                    self.response_queue.put(('mi-recv-debug', line_stripped))

                self.response_queue.put(('mi-recv', line_stripped))
                if self.on_response_callback:
                    self.on_response_callback()

                # Improved async record parsing (handles tokens and all record types)
                # Async records start with *, +, or =
                # They can optionally be preceded by a token (sequence of digits)
                # Token could be any length of digits, sometimes none.
                # record_class/payload can contain almost anything
                async_match = re.match(r'^(\d+)?([\*\+\=])([\w-]+)(.*)', line_stripped)
                
                # Result record starts with ^
                match = re.match(r'^(\d+)?(\^[\w-]+)(.*)', line_stripped)
                if match:
                    token, result_class, rest = match.groups()
                    if token in self.callbacks:
                        cb = self.callbacks.pop(token)
                        # We must call the callback with the parsed results
                        cb(result_class, rest)
                    self.response_queue.put(('result', token, result_class, rest))
                elif async_match:
                    token, record_type, record_class, payload = async_match.groups()
                    full_payload = record_class + payload
                    if record_type == '*':
                        self.response_queue.put(('exec-async', full_payload))
                    elif record_type == '+':
                        self.response_queue.put(('status-async', full_payload))
                    elif record_type == '=':
                        self.response_queue.put(('notify-async', full_payload))
                elif line.startswith('~'):
                    self.response_queue.put(('console', line[1:-1] if line.endswith('"') else line[1:]))
                elif line.startswith('&'):
                    self.response_queue.put(('log', line[1:-1] if line.endswith('"') else line[1:]))
                elif line == '(gdb)':
                    pass # Prompt
                else:
                    self.response_queue.put(('other', line))
        except (EOFError, ValueError, OSError):
            pass
        except Exception as e:
            self.response_queue.put(('stderr', f"GDB stdout reader error: {e}"))

    def _read_stderr(self):
        try:
            while self.running and self.process and self.process.stderr:
                line = self.process.stderr.readline()
                if not line:
                    break
                self.response_queue.put(('stderr', line.strip()))
                if self.on_response_callback:
                    self.on_response_callback()
        except (EOFError, ValueError, OSError):
            pass
        except Exception as e:
            self.response_queue.put(('stderr', f"GDB stderr reader error: {e}"))

# Example usage:
# gdb = GdbBackend("arm-none-eabi-gdb.exe")
# gdb.start()
# gdb.send_command("-file-exec-and-symbols main.elf")
# gdb.send_command("-target-select remote localhost:3333")
