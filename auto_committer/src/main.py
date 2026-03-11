import logging
import time

from src.committer import DebouncedCommitter
from src.config import DEBOUNCE_SECONDS, VAULT_PATH
from src.watcher import start_observer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("DavyJones Auto-Committer starting. Vault: %s, Debounce: %ds",
                VAULT_PATH, DEBOUNCE_SECONDS)

    committer = DebouncedCommitter()
    observer = start_observer(VAULT_PATH, committer.signal_change)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
