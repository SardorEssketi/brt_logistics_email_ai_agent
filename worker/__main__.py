"""Background worker: sender + IMAP watcher + notes flusher. Run with `python -m worker`."""
import logging
import signal
import threading

from . import flusher, sender, watcher


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    stop = threading.Event()

    def _stop(*_):
        logging.getLogger("worker").info("Stopping...")
        stop.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    threads = [threading.Thread(target=fn, args=(stop,), name=name, daemon=True)
               for name, fn in (("sender", sender.run), ("watcher", watcher.run), ("flusher", flusher.run))]
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(5)
        for t in threads:
            if not t.is_alive():
                logging.getLogger("worker").error("Thread %s died; exiting so systemd restarts us", t.name)
                stop.set()
    for t in threads:
        t.join(timeout=30)


if __name__ == "__main__":
    main()
