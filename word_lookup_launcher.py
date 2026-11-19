"""Light entry point: watchdog and secondary launches never initialize Tk."""
import sys
import time

from word_lookup_instance import SingleInstance
from word_lookup_paths import data_dir


def main() -> None:
    startup_started = time.perf_counter()
    if sys.argv[1:2] == ["--self-test"]:
        from word_lookup_selftest import run
        run(sys.argv[2])
        return
    if sys.argv[1:2] == ["--pause-watchdog"]:
        from word_lookup_pause import run_watchdog
        run_watchdog(sys.argv[2:])
        return
    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        raise SystemExit("Word Lookup requires Python 3.11-3.13. Run install.bat with a supported Python.")
    instance = SingleInstance(data_dir())
    try:
        if not instance.acquire():
            instance.request_activation()
            return
        from word_lookup_dpi import configure_dpi
        configure_dpi()
        from word_lookup import main as run_application
        run_application(instance=instance, startup_started=startup_started)
    finally:
        instance.close()


if __name__ == "__main__":
    main()
