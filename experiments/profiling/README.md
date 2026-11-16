## Profiling

Ran the same `nginx` setup as in the benchmarking experiments. Used `nighthawk` to generate load:

```bash
docker run --rm --network host \
  --entrypoint /usr/local/bin/nighthawk_client \
  envoyproxy/nighthawk-dev:latest \
  --duration 60 \
  --concurrency 2 \
  --connections 256 \
  --rps 10000 \
  --protocol http1 \
  http://127.0.0.1:10000/random
```

Envoy was ran with `--concurrency 4` and the provided configurations.
Profiled with all 3 configurations, where:
- `envoy-simple-cache.yaml` - every request is a cache hit
- `envoy-90-10.yaml` - 90% of requests are cache hits, 10% are unique misses, that are not coalesced (each one makes upstream request, but this is handled by the FetchManager, just each request has 1 subscriber)
- `envoy-50-50.yaml` - 50% hit, 50% miss, same as previous case

## Results
Descibed in `WRITEUP.md` in repository root.