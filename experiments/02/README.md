## Benchmarking

#### Preparing the docker images 
docker pull nginx:alpine 
docker pull envoyproxy/nighthawk-dev:latest

#### Running the upstream server 
Run from `experiments/02/` (stop the previous `bench-origin` container first if running).
```bash
docker run -d --rm \
  --name bench-origin \
  -p 8080:80 \
  -v "$(pwd)/data:/usr/share/nginx/html:ro" \
  -v "$(pwd)/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine
```
### envoy w/o cache filter vs with simple_cache (cache misses only, no coalescing)

The experiment compares envoy running with no cache filter to envoy handling cache misses through simple_cache.
Experiment ran with envoy `--concurrency` set to 1, 2 and 4. For each concurrency setting of envoy, tested with nighthawk with total `--rps` 5000, 10000 and 50000, `--connections 256`. Nighthawk used 2 threads to generate load.
`run.sh` executes these and saves the results in `results/`. With nginx running and any existing Envoy stopped, run `bash experiments/02/run.sh` from the repository root.

#### How to run manually
To run without the cache: 
```bash
# Baseline, without a cache filter:
./bin/envoy-coalescing -c experiments/02/envoy-no-cache.yaml --concurrency 1
```

To run with my cache configured:
```bash
# Stop the baseline before starting the cache variant:
./bin/envoy-coalescing -c experiments/02/envoy-simple-cache.yaml --concurrency 1
```

Now, run nighthawk to generate load:
```bash
docker run --rm --network host \
  --entrypoint /usr/local/bin/nighthawk_client \
  envoyproxy/nighthawk-dev:latest \
  --duration 20 \
  --concurrency 2 \
  --connections 256 \
  --rps "$((rps / 2))" \
  --protocol http1 \
  http://127.0.0.1:10000/random
```

### Results

Successful RPS from Nighthawk's `benchmark.http_2xx`, rounded to whole requests/s.

| Target RPS | Envoy workers | No cache RPS | Cache RPS | Throughput change |
|---:|---:|---:|---:|---:|
| 5,000 | 1 | 1,044 | 778 | -25.4% |
| 5,000 | 2 | 2,006 | 1,530 | -23.7% |
| 5,000 | 4 | 3,717 | 2,780 | -25.2% |
| 10,000 | 1 | 1,001 | 782 | -21.8% |
| 10,000 | 2 | 2,017 | 1,509 | -25.2% |
| 10,000 | 4 | 3,637 | 2,726 | -25.0% |
| 50,000 | 1 | 1,013 | 778 | -23.3% |
| 50,000 | 2 | 2,015 | 1,525 | -24.3% |
| 50,000 | 4 | 3,679 | 2,737 | -25.6% |

Nighthawk did not achieve the target RPS in most runs, treat numbers as rough comparison.
