import argparse
import csv
import logging
import select
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit


# ----------------------------
# Configuration
# ----------------------------

PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8082

BUFFER_SIZE = 8192
SOCKET_TIMEOUT = 10

BASE_DIR = Path(__file__).resolve().parent
RAW_DATA_DIR = BASE_DIR / "data" / "raw"
FEATURE_DATA_DIR = BASE_DIR / "data" / "features"


# ----------------------------
# Traffic information
# ----------------------------

from collections import defaultdict


class FeatureExtractor:

    def __init__(self, window_size=10):
        self.window_size = window_size

        # Traffic collected during the CURRENT window.
        self.records = defaultdict(list)

        self.lock = threading.Lock()

    def add_record(self, traffic):
        """
        Add a completed connection/request to the
        current 10-second window.
        """

        with self.lock:
            self.records[traffic.client_ip].append(
                traffic
            )

    def collect_window(self):
        """
        Return everything collected during the current
        window and start a fresh window.
        """

        with self.lock:

            current_window = self.records

            self.records = defaultdict(list)

        return current_window

    def extract_features(self, records):

        if not records:
            return None

        total_connections = len(records)

        failed_connections = sum(
            1
            for record in records
            if not record.success
        )

        request_bytes = sum(
            record.request_bytes
            for record in records
        )

        response_bytes = sum(
            record.response_bytes
            for record in records
        )

        unique_destinations = len({
            record.host
            for record in records
        })

        failure_rate = (
            failed_connections
            / total_connections
        )

        requests_per_second = (
            total_connections
            / self.window_size
        )

        total_bytes = (
            request_bytes
            + response_bytes
        )

        bytes_per_second = (
            total_bytes
            / self.window_size
        )

        # HTTP only
        http_records = [
            record
            for record in records
            if record.method != "CONNECT"
        ]

        if http_records:

            average_response_time = (
                sum(
                    record.response_time
                    for record in http_records
                )
                / len(http_records)
            )

        else:
            average_response_time = 0.0

        # HTTPS tunnels only
        tunnel_records = [
            record
            for record in records
            if record.method == "CONNECT"
        ]

        if tunnel_records:

            average_tunnel_duration = (
                sum(
                    record.connection_duration
                    for record in tunnel_records
                )
                / len(tunnel_records)
            )

        else:
            average_tunnel_duration = 0.0

        return {
            "requests_per_second":
                requests_per_second,

            "total_connections":
                total_connections,

            "failed_connections":
                failed_connections,

            "failure_rate":
                failure_rate,

            "request_bytes":
                request_bytes,

            "response_bytes":
                response_bytes,

            "bytes_per_second":
                bytes_per_second,

            "average_response_time":
                average_response_time,

            "average_tunnel_duration":
                average_tunnel_duration,

            "unique_destinations":
                unique_destinations
        }


@dataclass
class TrafficRecord:
    timestamp: str
    client_ip: str
    method: str
    host: str
    port: int

    request_bytes: int = 0
    response_bytes: int = 0

    # Used for normal HTTP requests
    response_time: float = 0.0

    # Used for HTTPS CONNECT tunnels
    connection_duration: float = 0.0

    success: bool = True


class TrafficMonitor:

    def __init__(
        self,
        traffic_file=None,
        feature_file=None,
        collection_label="normal",
        session_name="normal_01",
        window_size=10
    ):

        self.traffic_file = Path(
            traffic_file
        ) if traffic_file else (
            RAW_DATA_DIR
            / f"traffic_{session_name}.csv"
        )

        self.feature_file = Path(
            feature_file
        ) if feature_file else (
            FEATURE_DATA_DIR
            / f"features_{session_name}.csv"
        )

        self.traffic_file.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.feature_file.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.collection_label = collection_label

        self.lock = threading.Lock()

        self.feature_extractor = FeatureExtractor(
            window_size=window_size
        )

        self.window_size = window_size

        self.create_traffic_csv()
        self.create_feature_csv()

        # Background worker responsible for producing
        # exactly one feature summary every window.
        self.feature_thread = threading.Thread(
            target=self.feature_collection_loop,
            daemon=True
        )

        self.feature_thread.start()

    def create_traffic_csv(self):

        try:

            with open(
                self.traffic_file,
                "x",
                newline=""
            ) as file:

                writer = csv.writer(file)

                writer.writerow([
                    "timestamp",
                    "client_ip",
                    "method",
                    "host",
                    "port",
                    "request_bytes",
                    "response_bytes",
                    "response_time",
                    "connection_duration",
                    "success"
                ])

        except FileExistsError:
            pass

    def create_feature_csv(self):

        try:

            with open(
                self.feature_file,
                "x",
                newline=""
            ) as file:

                writer = csv.writer(file)

                writer.writerow([
                    "window_start",
                    "window_end",
                    "client_ip",
                    "requests_per_second",
                    "total_connections",
                    "failed_connections",
                    "failure_rate",
                    "request_bytes",
                    "response_bytes",
                    "bytes_per_second",
                    "average_response_time",
                    "average_tunnel_duration",
                    "unique_destinations",
                    "label"
                ])

        except FileExistsError:
            pass

    def save(self, traffic):

        # Save individual raw request.
        self.save_traffic(traffic)

        # Add request to current 10-second ML window.
        self.feature_extractor.add_record(
            traffic
        )

    def save_traffic(self, traffic):

        with self.lock:

            with open(
                self.traffic_file,
                "a",
                newline=""
            ) as file:

                writer = csv.writer(file)

                writer.writerow([
                    traffic.timestamp,
                    traffic.client_ip,
                    traffic.method,
                    traffic.host,
                    traffic.port,
                    traffic.request_bytes,
                    traffic.response_bytes,

                    round(
                        traffic.response_time,
                        4
                    ),

                    round(
                        traffic.connection_duration,
                        4
                    ),

                    traffic.success
                ])

    def feature_collection_loop(self):

        while True:

            # Wait exactly one feature window.
            threading.Event().wait(
                self.window_size
            )

            window_end = datetime.now()

            window_start = (
                window_end
                - timedelta(
                    seconds=self.window_size
                )
            )

            window_data = (
                self.feature_extractor
                .collect_window()
            )

            for client_ip, records in (
                window_data.items()
            ):

                features = (
                    self.feature_extractor
                    .extract_features(records)
                )

                if features:

                    self.save_features(
                        window_start,
                        window_end,
                        client_ip,
                        features
                    )

    def save_features(
        self,
        window_start,
        window_end,
        client_ip,
        features
    ):

        with self.lock:

            with open(
                self.feature_file,
                "a",
                newline=""
            ) as file:

                writer = csv.writer(file)

                writer.writerow([
                    window_start.isoformat(),
                    window_end.isoformat(),
                    client_ip,

                    round(
                        features[
                            "requests_per_second"
                        ],
                        4
                    ),

                    features[
                        "total_connections"
                    ],

                    features[
                        "failed_connections"
                    ],

                    round(
                        features[
                            "failure_rate"
                        ],
                        4
                    ),

                    features[
                        "request_bytes"
                    ],

                    features[
                        "response_bytes"
                    ],

                    round(
                        features[
                            "bytes_per_second"
                        ],
                        4
                    ),

                    round(
                        features[
                            "average_response_time"
                        ],
                        4
                    ),

                    round(
                        features[
                            "average_tunnel_duration"
                        ],
                        4
                    ),

                    features[
                        "unique_destinations"
                    ],

                    self.collection_label
                ])
# ----------------------------
# Proxy server
# ----------------------------

class ProxyServer:

    def __init__(
        self,
        host=PROXY_HOST,
        port=PROXY_PORT,
        collection_label="normal",
        session_name="normal_01",
        window_size=10
    ):
        self.host = host
        self.port = port
        self.monitor = TrafficMonitor(
            collection_label=collection_label,
            session_name=session_name,
            window_size=window_size
        )

    def start(self):

        server_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        # Allows us to restart the proxy without waiting
        # for the operating system to release the port.
        server_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1
        )

        server_socket.bind((self.host, self.port))
        server_socket.listen(50)

        logging.info(
            "Proxy server listening on %s:%s",
            self.host,
            self.port
        )

        try:

            while True:

                browser_socket, browser_address = (
                    server_socket.accept()
                )

                # Every client gets its own worker thread.
                worker = threading.Thread(
                    target=self.handle_client,
                    args=(
                        browser_socket,
                        browser_address
                    ),
                    daemon=True
                )

                worker.start()

        except KeyboardInterrupt:
            logging.info("Proxy server stopped")

        finally:
            server_socket.close()

    def handle_client(
        self,
        browser_socket,
        browser_address
    ):

        start_time = time.perf_counter()

        traffic = None

        try:

            browser_socket.settimeout(
                SOCKET_TIMEOUT
            )

            request = self.receive_request(
                browser_socket
            )

            if not request:
                return

            method, target, headers = (
                self.parse_request(request)
            )

            host, port = self.get_destination(
                method,
                target,
                headers
            )

            traffic = TrafficRecord(
                timestamp=datetime.now().isoformat(),
                client_ip=browser_address[0],
                method=method,
                host=host,
                port=port,
                request_bytes=len(request)
            )

            logging.info(
                "%s | %s | %s:%s",
                browser_address[0],
                method,
                host,
                port
            )

            if method == "CONNECT":

                tunnel_start = time.perf_counter()

                traffic.response_bytes = self.handle_https(
                    browser_socket,
                    host,
                    port
                )

                traffic.connection_duration = (
                    time.perf_counter()
                    - tunnel_start
                )

            else:

                request_start = time.perf_counter()

                traffic.response_bytes = self.handle_http(
                    browser_socket,
                    request,
                    host,
                    port
                )

                traffic.response_time = (
                    time.perf_counter()
                    - request_start
                )
            
        except Exception as error:

            logging.warning(
                "Client %s: %s",
                browser_address[0],
                error
            )

            if traffic:
                traffic.success = False

            try:
                self.send_error(
                    browser_socket,
                    502,
                    "Bad Gateway"
                )

            except OSError:
                pass

        finally:

            if traffic:
                self.monitor.save(traffic)

            browser_socket.close()

    # ----------------------------
    # HTTP parsing
    # ----------------------------

    def receive_request(self, browser_socket):

        request = b""

        while b"\r\n\r\n" not in request:

            data = browser_socket.recv(
                BUFFER_SIZE
            )

            if not data:
                break

            request += data

        return request

    def parse_request(self, request):

        request_text = request.decode(
            "iso-8859-1"
        )

        lines = request_text.split("\r\n")

        request_line = lines[0].split()

        if len(request_line) < 3:
            raise ValueError(
                "Invalid HTTP request"
            )

        method = request_line[0].upper()
        target = request_line[1]

        headers = {}
        # this is to handle the case where there are no headers in the request
        for line in lines[1:]:

            if ":" not in line:
                continue
            # name like "Host" and value like "example.com"
            name, value = line.split(":", 1)

            headers[name.lower()] = (
                value.strip()
            )

        return method, target, headers

    def get_destination(
        self,
        method,
        target,
        headers
    ):

        # HTTPS proxy requests normally look like:
        #
        # CONNECT google.com:443 HTTP/1.1

        if method == "CONNECT":

            if ":" in target:

                host, port = target.rsplit(
                    ":",
                    1
                )

                return host, int(port)

            return target, 443

        # HTTP proxy request:
        #
        # GET http://example.com/page HTTP/1.1

        parsed_url = urlsplit(target)

        if parsed_url.hostname:

            return (
                parsed_url.hostname,
                parsed_url.port or 80
            )

        host = headers.get("host")

        if not host:
            raise ValueError(
                "Host header missing"
            )

        if ":" in host:

            hostname, port = host.rsplit(
                ":",
                1
            )

            if port.isdigit():
                return hostname, int(port)

        return host, 80

    # ----------------------------
    # HTTP forwarding
    # ----------------------------

    def handle_http(
        self,
        browser_socket,
        request,
        host,
        port
    ):

        request = self.prepare_http_request(
            request
        )

        bytes_received = 0

        with socket.create_connection(
            (host, port),
            timeout=SOCKET_TIMEOUT
        ) as web_socket:

            web_socket.sendall(request)

            while True:

                response = web_socket.recv(
                    BUFFER_SIZE
                )

                if not response:
                    break

                browser_socket.sendall(
                    response
                )

                bytes_received += len(
                    response
                )

        return bytes_received

    def prepare_http_request(self, request):

        """
        Browsers send an absolute URL to a proxy:

        GET http://example.com/page HTTP/1.1

        The destination web server expects:

        GET /page HTTP/1.1
        """

        header, separator, body = (
            request.partition(
                b"\r\n\r\n"
            )
        )

        text = header.decode(
            "iso-8859-1"
        )

        lines = text.split("\r\n")

        method, target, version = (
            lines[0].split(" ", 2)
        )

        parsed_url = urlsplit(target)

        if parsed_url.hostname:

            path = parsed_url.path or "/"

            if parsed_url.query:
                path += (
                    "?"
                    + parsed_url.query
                )

            lines[0] = (
                f"{method} "
                f"{path} "
                f"{version}"
            )

        new_headers = []

        for line in lines:

            lower_line = line.lower()

            if lower_line.startswith(
                "proxy-connection:"
            ):
                continue

            if lower_line.startswith(
                "connection:"
            ):
                continue

            new_headers.append(line)

        # Closing the upstream connection makes
        # response forwarding easier and reliable.
        new_headers.append(
            "Connection: close"
        )

        header = "\r\n".join(
            new_headers
        ).encode("iso-8859-1")

        return (
            header
            + separator
            + body
        )

    # ----------------------------
    # HTTPS tunneling
    # ----------------------------

    def handle_https(
        self,
        browser_socket,
        host,
        port
    ):

        web_socket = socket.create_connection(
            (host, port),
            timeout=SOCKET_TIMEOUT
        )

        try:

            browser_socket.sendall(
                b"HTTP/1.1 200 "
                b"Connection Established"
                b"\r\n\r\n"
            )

            # CONNECT now becomes a normal
            # bidirectional TCP tunnel.

            browser_socket.settimeout(None)
            web_socket.settimeout(None)

            return self.tunnel(
                browser_socket,
                web_socket
            )

        finally:
            web_socket.close()

    def tunnel(
        self,
        browser_socket,
        web_socket
    ):

        sockets = [
            browser_socket,
            web_socket
        ]

        server_to_client_bytes = 0

        while True:

            readable, _, _ = select.select(
                sockets,
                [],
                [],
                60
            )

            if not readable:
                break

            for current_socket in readable:

                data = current_socket.recv(
                    BUFFER_SIZE
                )

                if not data:
                    return (
                        server_to_client_bytes
                    )

                if current_socket is (
                    browser_socket
                ):

                    web_socket.sendall(data)

                else:

                    browser_socket.sendall(
                        data
                    )

                    server_to_client_bytes += (
                        len(data)
                    )

        return server_to_client_bytes

    # ----------------------------
    # Error responses
    # ----------------------------

    def send_error(
        self,
        browser_socket,
        status_code,
        message
    ):

        body = (
            f"{status_code} "
            f"{message}\n"
        ).encode()

        response = (
            f"HTTP/1.1 "
            f"{status_code} "
            f"{message}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: "
            f"{len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode()

        browser_socket.sendall(
            response + body
        )


# ----------------------------
# Main
# ----------------------------

def configure_logging():

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        )
    )


def parse_arguments():

    parser = argparse.ArgumentParser(
        description=(
            "Run the proxy and collect fixed-window "
            "network traffic features."
        )
    )

    parser.add_argument(
        "--label",
        choices=["normal", "anomalous"],
        default="normal",
        help="Label assigned to every feature window."
    )

    parser.add_argument(
        "--session",
        default=None,
        help=(
            "Session name used in output filenames, "
            "for example normal_01. By default, "
            "<label>_01 is used."
        )
    )

    parser.add_argument(
        "--host",
        default=PROXY_HOST,
        help="Address on which the proxy listens."
    )

    parser.add_argument(
        "--port",
        type=int,
        default=PROXY_PORT,
        help="Port on which the proxy listens."
    )

    parser.add_argument(
        "--window-size",
        type=int,
        default=10,
        help="Feature aggregation window in seconds."
    )

    arguments = parser.parse_args()

    if arguments.session and not all(
        character.isalnum()
        or character in "-_"
        for character in arguments.session
    ):
        parser.error(
            "--session may contain only letters, "
            "numbers, hyphens, and underscores."
        )

    if arguments.window_size <= 0:
        parser.error(
            "--window-size must be greater than zero."
        )

    return arguments


if __name__ == "__main__":

    configure_logging()

    args = parse_arguments()
    session_name = (
        args.session
        or f"{args.label}_01"
    )

    proxy = ProxyServer(
        host=args.host,
        port=args.port,
        collection_label=args.label,
        session_name=session_name,
        window_size=args.window_size
    )

    logging.info(
        "Collection label: %s | Session: %s",
        args.label,
        session_name
    )

    logging.info(
        "Raw traffic file: %s",
        proxy.monitor.traffic_file
    )

    logging.info(
        "Feature file: %s",
        proxy.monitor.feature_file
    )

    proxy.start()
