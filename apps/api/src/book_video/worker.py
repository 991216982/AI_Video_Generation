"""Stage 0 worker entry point; no external queue is connected yet."""

import argparse
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Check worker startup and exit")
    args = parser.parse_args()
    print("worker ready (stage 0 stub)", flush=True)
    if not args.once:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
