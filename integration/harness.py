#!/usr/bin/env python3
"""Real HTTP tests for simple_cache"""

import argparse
import http.client
import json
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


TIMEOUT = 12
MULTI_WORKERS = 4
CONCURRENT_CLIENTS = 8
HEADER_DELAY = 1.0
CHUNK_DELAY = 0.15
CHUNKS = (b"first-body-bytes\n", b"second-chunk\n", b"third-chunk\n", b"last-chunk\n")
BODY = b"".join(CHUNKS)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait(event, description):
    require(event.wait(TIMEOUT), f"Timeout waiting for {description}")


class OriginResponse:
    def __init__(self, header_delay, chunk_delay, streaming):
        self.header_delay = header_delay
        self.chunk_delay = chunk_delay
        self.lock = threading.Lock()
        self.release_headers = threading.Event()
        self.release_body = threading.Event()
        self.cancelled = threading.Event()

        if not streaming:
            self.release_body.set()

        self.reset()

    def reset(self):
        with self.lock:
            self.count = 0
            self.finished = 0
            self.final_started = []
            self.errors = []

    def snapshot(self):
        with self.lock:
            return SimpleNamespace(
                request_count=self.count,
                completed_responses=self.finished,
                final_chunk_times=list(self.final_started),
                errors=list(self.errors),
            )


class Origin(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), OriginHandler)
        self.responses = {}
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve_forever, name="origin")
        self.thread.start()

    def add(self, path, response):
        with self.lock:
            self.responses[path] = response

    def close(self):
        with self.lock:
            for response in self.responses.values():
                response.cancelled.set()
                response.release_headers.set()
                response.release_body.set()
        self.shutdown()
        self.server_close()
        self.thread.join(TIMEOUT)
        require(not self.thread.is_alive(), "Origin server did not stop")


class OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(TIMEOUT)

    def log_message(self, *_):
        pass

    def do_GET(self):
        with self.server.lock:
            response = self.server.responses.get(self.path)

        if response is None:
            self.send_error(404)
            return

        with response.lock:
            response.count += 1

        try:
            wait(response.release_headers, "origin header release")
            if response.cancelled.wait(response.header_delay):
                return

            self.send_response(200)
            self.send_header("Cache-Control", "public, max-age=60")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            for index, chunk in enumerate(CHUNKS):
                if index > 0:
                    wait(response.release_body, "origin body release")
                    if response.cancelled.wait(response.chunk_delay):
                        return

                if index == len(CHUNKS) - 1:
                    with response.lock:
                        response.final_started.append(time.monotonic())

                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()

            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

            with response.lock:
                response.finished += 1
        except Exception as error:
            if not response.cancelled.is_set():
                with response.lock:
                    response.errors.append(repr(error))
        finally:
            self.close_connection = True


class Client:
    def __init__(self, port, path, barrier, disconnect=False):
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
        self.path = path
        self.barrier = barrier
        self.disconnect = disconnect
        self.request_sent = threading.Event()
        self.body_received = threading.Event()
        self.done = threading.Event()
        self.arrivals = []
        self.body = bytearray()
        self.status = None
        self.error = None
        self.thread = threading.Thread(target=self.run, name="http-client")

    def run(self):
        try:
            self.connection.connect()
            self.barrier.wait(TIMEOUT)

            self.connection.request("GET", self.path)
            self.request_sent.set()

            response = self.connection.getresponse()
            self.status = response.status

            while True:
                data = response.read1(4096)
                if not data:
                    break

                self.arrivals.append(time.monotonic())
                self.body.extend(data)
                self.body_received.set()

                if self.disconnect:
                    self.connection.sock.shutdown(socket.SHUT_RDWR)
                    break

            response.close()
        except Exception as error:
            self.error = repr(error)
        finally:
            self.connection.close()
            self.done.set()

    def stop(self):
        sock = self.connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.connection.close()


def check_clients(clients):
    for index, client in enumerate(clients):
        wait(client.done, f"client {index} completion")
        require(client.error is None, f"client {index}: {client.error}")
        require(client.status == 200, f"client {index}: HTTP {client.status}")

        if not client.disconnect:
            require(
                bytes(client.body) == BODY,
                f"client {index}: wrong body: {bytes(client.body)!r}",
            )


def check_streaming(clients, origin_result):
    require(origin_result.final_chunk_times, "No final chunk recorded at origin")
    first_final_chunk = min(origin_result.final_chunk_times)

    for index, client in enumerate(clients):
        margin = first_final_chunk - client.arrivals[0]
        require(
            margin >= 0.05,
            f"client {index}: first body only {margin:.3f}s before final chunk",
        )


def run_scenario(
    origin, port, name, client_count, streaming=False, disconnect=False, max_fetches=1
):
    response = OriginResponse(HEADER_DELAY, CHUNK_DELAY, streaming)
    path = f"/integration/{name}/{uuid.uuid4().hex}"
    origin.add(path, response)

    barrier = threading.Barrier(client_count + 1)
    clients = [
        Client(port, path, barrier, disconnect=disconnect and index == 0)
        for index in range(client_count)
    ]
    started = time.monotonic()

    try:
        for client in clients:
            client.thread.start()

        barrier.wait(TIMEOUT)

        for client in clients:
            wait(client.request_sent, "all client GETs sent")

        response.release_headers.set()

        if streaming:
            for index, client in enumerate(clients):
                require(
                    client.body_received.wait(TIMEOUT),
                    f"client {index}: no body bytes before origin release; "
                    f"status={client.status}, error={client.error}",
                )

            require(
                response.snapshot().completed_responses == 0,
                "Origin finished before streaming assertion",
            )

            if disconnect:
                wait(clients[0].done, "first client's disconnection")

            response.release_body.set()

        check_clients(clients)
        result = response.snapshot()
        require(not result.errors, f"Origin errors: {result.errors}")

        if streaming:
            check_streaming(clients, result)

        arrivals = [
            [round(arrival - started, 3) for arrival in client.arrivals]
            for client in clients
        ]
        avoided_fetches = client_count - result.request_count
        print(
            f"{name}: downstream_requests={client_count}, "
            f"origin_requests={result.request_count}, "
            f"avoided_fetches={avoided_fetches} ({avoided_fetches / client_count:.0%}), "
            f"body_arrivals_seconds={arrivals}",
            flush=True,
        )

        require(
            1 <= result.request_count <= max_fetches,
            f"{name}: expected 1..{max_fetches} upstream fetches for {client_count} clients, "
            f"observed {result.request_count}; "
            "check worker count, binary version, and joining delay",
        )
    finally:
        response.cancelled.set()
        response.release_headers.set()
        response.release_body.set()
        barrier.abort()

        for client in clients:
            client.stop()

        for client in clients:
            client.thread.join(TIMEOUT + 1)
            require(not client.thread.is_alive(), "Client thread failed to terminate")


def test_cache_hit(origin, port):
    response = OriginResponse(0, 0, streaming=False)
    response.release_headers.set()
    path = f"/integration/E/{uuid.uuid4().hex}"
    origin.add(path, response)

    try:
        for request_number in (1, 2):
            started = time.monotonic()
            arrivals = []
            body = bytearray()

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)

            with closing(connection):
                connection.request("GET", path)

                with connection.getresponse() as received:
                    require(received.status == 200, f"E request {request_number}: HTTP {received.status}")

                    while chunk := received.read1(4096):
                        arrivals.append(round(time.monotonic() - started, 3))
                        body.extend(chunk)

            require(bytes(body) == BODY, f"E request {request_number}: wrong body: {bytes(body)!r}")
            result = response.snapshot()
            require(not result.errors, f"E: origin errors: {result.errors}")

            print(
                f"E: request={request_number}, origin_requests={result.request_count}, "
                f"body_arrivals_seconds={arrivals}",
                flush=True,
            )

            require(
                result.request_count == 1,
                f"E request {request_number}: expected one total upstream fetch, "
                f"observed {result.request_count}; the second request must use the completed cache entry",
            )
    finally:
        response.cancelled.set()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def config(origin_port, listen_port):
    result = json.loads(Path(__file__).with_name("envoy.json").read_text())
    resources = result["static_resources"]
    resources["listeners"][0]["address"]["socket_address"]["port_value"] = listen_port

    cluster = resources["clusters"][0]
    endpoint = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]
    endpoint["address"]["socket_address"]["port_value"] = origin_port
    return result


def wait_for_envoy(process, port):
    deadline = time.monotonic() + TIMEOUT

    while time.monotonic() < deadline:
        require(process.poll() is None, f"Envoy exited with {process.returncode}")

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)

    raise AssertionError("Envoy listener did not start")


def run_envoy(binary, origin, name, workers, directory):
    port = free_port()
    config_path = Path(directory) / f"envoy-{name}.json"
    log_path = Path(directory) / f"envoy-{name}.log"
    config_path.write_text(json.dumps(config(origin.server_port, port), indent=2))

    command = [
        str(binary.resolve()),
        "-c", str(config_path),
        "--concurrency", str(workers),
        "--disable-hot-restart",
        "--log-level", "warn",
    ]

    with log_path.open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)

        try:
            wait_for_envoy(process, port)

            if name == "E":
                test_cache_hit(origin, port)
            else:
                run_scenario(
                    origin,
                    port,
                    name,
                    client_count=CONCURRENT_CLIENTS if name in ("A", "D") else 2,
                    streaming=name in ("B", "C", "D"),
                    disconnect=name == "C",
                    max_fetches=CONCURRENT_CLIENTS - 1 if workers > 1 else 1,
                )
        except Exception:
            log.flush()
            print(f"Test {name} Envoy log ({workers} workers):\n{log_path.read_text()}", flush=True)
            raise
        finally:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run_test(binary, name, directory):
    origin = Origin()

    try:
        workers = MULTI_WORKERS if name == "D" else 1
        run_envoy(binary, origin, name, workers, directory)
    finally:
        origin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envoy", required=True, type=Path, help="Existing executable; never rebuilt")
    args = parser.parse_args()

    if not args.envoy.is_file():
        parser.error(f"Envoy binary does not exist: {args.envoy}")

    passed = []
    failed = []

    with tempfile.TemporaryDirectory(prefix="simple-cache-integration-") as directory:
        for name in ("A", "B", "C", "D", "E"):
            print(f"\nRunning test {name}", flush=True)

            try:
                run_test(args.envoy, name, directory)
            except Exception as error:
                failed.append(name)
                print(f"FAIL {name}: {type(error).__name__}: {error}", flush=True)
            else:
                passed.append(name)
                print(f"PASS {name}", flush=True)

    print(f"\nPassed: {', '.join(passed) or 'none'}")
    print(f"Failed: {', '.join(failed) or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
