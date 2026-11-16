#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p results
envoy_pid=

stop_envoy() {
  if [[ -n "$envoy_pid" ]]; then
    kill "$envoy_pid" 2>/dev/null || true
    wait "$envoy_pid" 2>/dev/null || true
    envoy_pid=
  fi
}
trap stop_envoy EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/random -o /dev/null
if curl --fail --silent --max-time 1 http://127.0.0.1:9901/ready -o /dev/null; then
  echo "Stop the existing Envoy before running this script." >&2
  exit 1
fi

for variant in no-cache simple-cache; do
  for concurrency in 1 2 4; do
    log="results/${variant}_concurrency-${concurrency}_envoy.log"
    ../../bin/envoy-coalescing -c "envoy-${variant}.yaml" \
      --concurrency "$concurrency" > "$log" 2>&1 &
    envoy_pid=$!

    ready=false
    for ((attempt = 0; attempt < 100; attempt++)); do
      if ! kill -0 "$envoy_pid" 2>/dev/null; then
        echo "Envoy exited; see $log" >&2
        exit 1
      fi
      if curl --fail --silent --max-time 1 http://127.0.0.1:9901/ready -o /dev/null; then
        ready=true
        break
      fi
      sleep 0.1
    done
    if [[ "$ready" != true ]]; then
      echo "Envoy did not become ready; see $log" >&2
      exit 1
    fi

    for rps in 5000 10000 50000; do
      if [[ "$variant" == simple-cache ]]; then
        curl --fail --silent --show-error --max-time 5 http://127.0.0.1:10000/random -o /dev/null
      fi
      result="results/${variant}_concurrency-${concurrency}_rps-${rps}.txt"
      echo "Running $result"
      docker run --rm --network host \
        --entrypoint /usr/local/bin/nighthawk_client \
        envoyproxy/nighthawk-dev:latest \
        --duration 20 --concurrency 2 --connections 256 \
        --rps "$((rps / 2))" --protocol http1 \
        http://127.0.0.1:10000/random > "$result" 2>&1
    done

    stop_envoy
  done
done
