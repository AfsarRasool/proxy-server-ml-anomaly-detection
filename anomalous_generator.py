import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


def send_request(number, target, proxy_url, timeout):
    import requests

    session = requests.Session()

    # Prevent environment proxy settings from bypassing
    # the proxy for localhost.
    session.trust_env = False

    try:
        response = session.get(
            f"{target}/?request={number}",
            proxies={"http": proxy_url},
            timeout=timeout
        )

        return response.status_code

    except requests.RequestException:
        return None


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Generate controlled request bursts through "
            "the proxy for anomaly data collection."
        )
    )

    parser.add_argument(
        "--target",
        default="http://127.0.0.1:8000",
        help="Authorized local test target."
    )

    parser.add_argument(
        "--proxy",
        default="http://127.0.0.1:8082",
        help="HTTP proxy URL."
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Test duration in seconds."
    )

    parser.add_argument(
        "--requests-per-burst",
        type=int,
        default=40,
        help="Number of requests submitted per burst."
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of concurrent worker threads."
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=5,
        help="Timeout for each request in seconds."
    )

    arguments = parser.parse_args()

    for name in (
        "duration",
        "requests_per_burst",
        "workers"
    ):
        if getattr(arguments, name) <= 0:
            parser.error(
                f"--{name.replace('_', '-')} must be "
                "greater than zero."
            )

    if arguments.timeout <= 0:
        parser.error(
            "--timeout must be greater than zero."
        )

    return arguments


def main():
    args = parse_arguments()

    target_host = urlsplit(args.target).hostname

    if target_host not in {"127.0.0.1", "localhost"}:
        raise ValueError(
            "For safety, this generator accepts only "
            "localhost targets."
        )

    start_time = time.time()
    end_time = start_time + args.duration
    request_number = 0
    requests_sent = 0

    print(
        f"Target: {args.target} | "
        f"Duration: {args.duration}s"
    )

    while time.time() < end_time:
        numbers = range(
            request_number,
            request_number + args.requests_per_burst
        )

        with ThreadPoolExecutor(
            max_workers=args.workers
        ) as executor:
            results = list(
                executor.map(
                    lambda number: send_request(
                        number,
                        args.target,
                        args.proxy,
                        args.timeout
                    ),
                    numbers
                )
            )

        successful = sum(
            result is not None
            for result in results
        )

        requests_sent += len(results)
        elapsed = time.time() - start_time
        remaining = max(
            0,
            args.duration - elapsed
        )

        print(
            f"Requests sent: {requests_sent} | "
            f"Time passed: {elapsed:.1f}s | "
            f"Time remaining: {remaining:.1f}s | "
            f"Successful: {successful}/{len(results)}"
        )

        request_number += args.requests_per_burst
        time.sleep(1)

    elapsed = time.time() - start_time
    print(
        "Anomalous burst completed | "
        f"Requests sent: {requests_sent} | "
        f"Time passed: {elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
