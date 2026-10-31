"""Process pause watchdog and durable crash recovery; no GUI imports."""
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger(__name__)


def default_recovery_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "WordLookup", "pause-recovery")


def _same(proc, created):
    try:
        return (proc.is_running() and proc.create_time() == created
                and proc.status() not in ("zombie", "dead"))
    except Exception:
        logger.debug("Process no longer has the recorded identity", exc_info=True)
        return False


def _owner_alive(pid, created):
    import psutil
    try:
        return _same(psutil.Process(pid), created)
    except psutil.NoSuchProcess:
        return False


def _write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pause-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def recover_paused_games(recovery_dir=None):
    """Resume only our recorded, still-identical target after both owners died.

    Failed resumes keep their record for the next launch; live guards are never
    interfered with. Returns human-readable outcomes for the application log.
    """
    import psutil
    results = []
    folder = Path(recovery_dir or default_recovery_dir())
    if not folder.exists():
        return results
    for path in folder.glob("*.json"):
        try:
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
            pid, created = int(record["pid"]), float(record["created"])
            if pid <= 0 or pid == os.getpid():
                raise ValueError("Invalid recovery target")
            if (_owner_alive(int(record["parent_pid"]), float(record["parent_created"]))
                    or _owner_alive(int(record["watchdog_pid"]), float(record["watchdog_created"]))):
                continue
            try:
                target = psutil.Process(pid)
                if _same(target, created) and target.status() == psutil.STATUS_STOPPED:
                    target.resume()
                    results.append(f"Відновлено гру після збою: PID {pid}")
                else:
                    results.append(f"Застарілий запис паузи пропущено: PID {pid}")
            except psutil.NoSuchProcess:
                results.append(f"Призупинений процес уже завершився: PID {pid}")
            path.unlink()
        except Exception as error:
            logger.exception("Pause recovery failed for %s", path)
            results.append(f"Не вдалося відновити паузу {path.name}: {type(error).__name__}")
    return results


def watchdog_command(parent_pid, parent_created, pid, created, seconds, recovery_dir):
    args = [str(parent_pid), str(parent_created), str(pid), str(created),
            str(seconds), str(recovery_dir)]
    if getattr(sys, "frozen", False):
        # A packaged executable does not understand Python's -c switch.
        return [sys.executable, "--pause-watchdog", *args]
    return [sys.executable, "-u", str(Path(__file__).resolve()), "--pause-watchdog", *args]


def _watchdog_pipe_fds():
    """Inherited Popen pipes work even in a PyInstaller --windowed executable.

    Windowed Python may set sys.stdin/stdout to None. Use the explicitly
    inherited Windows handles, never attach a console or start another GUI.
    """
    if not sys.platform.startswith("win"):
        return 0, 1
    import ctypes
    from ctypes import wintypes
    import msvcrt
    get_handle = ctypes.windll.kernel32.GetStdHandle
    get_handle.argtypes = [wintypes.DWORD]
    get_handle.restype = wintypes.HANDLE
    handles = [get_handle(-10), get_handle(-11)]
    invalid = ctypes.c_void_p(-1).value
    if any(handle in (None, 0, invalid) for handle in handles):
        raise RuntimeError("Watchdog did not inherit its control pipes")
    return (msvcrt.open_osfhandle(handles[0], os.O_RDONLY | os.O_BINARY),
            msvcrt.open_osfhandle(handles[1], os.O_WRONLY | os.O_BINARY))


def run_watchdog(args):
    import psutil
    commands = queue.Queue()
    input_fd, output_fd = _watchdog_pipe_fds()

    def emit(message):
        os.write(output_fd, (message + "\n").encode("utf-8", errors="replace"))

    def read_commands():
        pending = b""
        try:
            while True:
                chunk = os.read(input_fd, 4096)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    commands.put(line.decode("ascii", errors="replace"))
        finally:
            commands.put(None)

    threading.Thread(target=read_commands, daemon=True).start()
    paused = False
    record_path = None
    try:
        parent = psutil.Process(int(args[0]))
        parent_created = float(args[1])
        target = psutil.Process(int(args[2]))
        created = float(args[3])
        ttl = max(0.2, min(300.0, float(args[4])))
        if not _same(parent, parent_created) or not _same(target, created):
            raise RuntimeError("Process identity changed")
        if target.pid in (os.getpid(), parent.pid) or target.status() == psutil.STATUS_STOPPED:
            raise RuntimeError("Target is not eligible for pause")
        emit("READY")
        if commands.get(timeout=5) == "ARM" and _same(parent, parent_created):
            if not _same(target, created):
                raise RuntimeError("Target exited before pause")
            watchdog = psutil.Process()
            record_path = Path(args[5]) / f"{target.pid}-{created:.6f}.json"
            # Persist before suspend: even a simultaneous kill of both owners
            # cannot lose the target identity after suspension.
            _write_record(record_path, {
                "pid": target.pid, "created": created,
                "parent_pid": parent.pid, "parent_created": parent_created,
                "watchdog_pid": watchdog.pid, "watchdog_created": watchdog.create_time(),
            })
            target.suspend()
            paused = True
            deadline = time.monotonic() + ttl
            emit("PAUSED")
            while time.monotonic() < deadline and _same(parent, parent_created) and _same(target, created):
                try:
                    command = commands.get(timeout=0.05)
                    if command is None or command == "RESUME":
                        break
                except queue.Empty:
                    continue
    except Exception as error:
        emit("ERROR: " + type(error).__name__ + ": " + str(error))
    finally:
        recovered = not paused
        if paused:
            if not _same(target, created):
                emit("TARGET_EXITED")
                recovered = True
            else:
                for attempt in range(3):
                    try:
                        target.resume()
                        emit("RESUMED")
                        recovered = True
                        break
                    except Exception as error:
                        if attempt == 2:
                            emit("ERROR: Resume failed: " + str(error))
                        time.sleep(0.1)
        if recovered and record_path is not None:
            try:
                record_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove completed pause record")


class GamePauseGuard:
    """Контрольована пауза одного PID з перевіркою часу створення і зовнішнім сторожем."""

    def __init__(self, pid, created, seconds=20, recovery_dir=None):
        self.pid, self.created, self.seconds = int(pid), float(created), float(seconds)
        self.recovery_dir = recovery_dir or default_recovery_dir()
        self.process = None
        self.lines = queue.Queue()
        self.last_status = ""
        self.reader = None

    def _read_output(self):
        pending = b""
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self.last_status = line.decode("utf-8", errors="replace").strip()
                    self.lines.put(self.last_status)
        finally:
            self.lines.put(None)

    def _expect(self, expected):
        try:
            line = self.lines.get(timeout=4)
        except queue.Empty:
            raise RuntimeError("Сторож паузи не відповів вчасно")
        if line != expected:
            raise RuntimeError(line or "Сторож паузи завершився")

    def start(self):
        if self.process is not None:
            raise RuntimeError("Пауза вже запущена")
        import psutil
        if self.pid == os.getpid() or self.pid <= 0:
            raise ValueError("Не можна призупинити Word Lookup або системний PID")
        parent = psutil.Process()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        self.process = subprocess.Popen(
            watchdog_command(parent.pid, parent.create_time(), self.pid, self.created,
                             self.seconds, self.recovery_dir),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=flags)
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        try:
            self._expect("READY")
            self.process.stdin.write(b"ARM\n")
            self.process.stdin.flush()
            self._expect("PAUSED")
        except Exception:
            self.release()
            raise
        return self

    @property
    def active(self):
        return self.process is not None and self.process.poll() is None

    def release(self):
        if self.process is None:
            return True, ""
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.write(b"RESUME\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                logger.debug("Watchdog pipe already closed", exc_info=True)
            finally:
                try:
                    self.process.stdin.close()
                except OSError:
                    logger.debug("Watchdog pipe close failed", exc_info=True)
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Не вбиваємо сторожа: він ще має відновити гру після свого ліміту.
            return False, "Сторож ще працює; автоматичне відновлення — після ліміту паузи"
        if self.reader:
            self.reader.join(timeout=0.2)
        if self.process.stdout:
            self.process.stdout.close()
        return self.last_status in ("RESUMED", "TARGET_EXITED"), self.last_status


if __name__ == "__main__" and sys.argv[1:2] == ["--pause-watchdog"]:
    run_watchdog(sys.argv[2:])
