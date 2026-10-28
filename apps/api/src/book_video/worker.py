"""Single-user development worker polling the SQLite task queue."""

import argparse
import time

from book_video.domain.service import DomainService
from book_video.integration import process_one


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process at most one task and exit")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args()
    service = DomainService()
    print("worker ready", flush=True)
    if args.once:
        process_one(service)
        return
    try:
        while True:
            if not process_one(service):
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
