## Benchmarking

#### Preparing the docker images 
docker pull nginx:alpine 
docker pull envoyproxy/nighthawk-dev:latest

#### Running the upstream server 
Run from `experiments/01/`
```bash
docker run -d --rm \
  --name bench-origin \
  -p 8080:80 \
  -v "$(pwd)/data:/usr/share/nginx/html:ro" \
  -v "$(pwd)/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine
```
### envoy w/o cache filter vs with coalescing cache filter (cache hits only)

The experiment compares envoy running with no cache filter to envoy serving cache hits from the simple_cache.
Experiment ran with envoy `--concurrency` set to 1, 2 and 4. For each concurrency setting of envoy, tested with nighthawk with total `--rps` 5000, 10000 and 50000, `--connections 256`. Nighthawk used 2 threads to generate load.
`run.sh` executes these and saves the results in `results/`. With nginx running and any existing Envoy stopped, run `bash experiments/01/run.sh` from the repository root.

#### How to run manually
To run without the cache: 
```bash
# Baseline, without a cache filter:
./bin/envoy-coalescing -c experiments/01/envoy-no-cache.yaml --concurrency 1
```

To run with my cache configured:
```bash
# Stop the baseline before starting the cache variant:
./bin/envoy-coalescing -c experiments/01/envoy-simple-cache.yaml --concurrency 1
```

If running with cache, make sure to warm up the cache first:
```bash
curl http://127.0.0.1:10000/data > /dev/null
```
(redirecting output since its binary, dont want to print it to terminal)

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

| Target RPS | Envoy workers | No cache RPS | Cache RPS | Speedup |
|---:|---:|---:|---:|---:|
| 5,000 | 1 | 1,088 | 2,655 | 2.44x |
| 5,000 | 2 | 2,177 | 4,984 | 2.29x |
| 5,000 | 4 | 4,033 | 4,999 | 1.24x |
| 10,000 | 1 | 1,101 | 2,761 | 2.51x |
| 10,000 | 2 | 2,166 | 5,476 | 2.53x |
| 10,000 | 4 | 4,035 | 9,970 | 2.47x |
| 50,000 | 1 | 1,068 | 2,882 | 2.70x |
| 50,000 | 2 | 2,191 | 5,492 | 2.51x |
| 50,000 | 4 | 3,966 | 10,153 | 2.56x |

Nighthawk did not achieve the target RPS in most runs, treat numbers as rough comparison.
