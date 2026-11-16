# In-memory HTTP cache for Envoy

## 1. Overview and scope

The cache is registered as an Envoy filter under `envoy.filters.http.simple_cache`, and its implementation is in `envoy/source/extensions/filters/http/simple_cache`

In addition to the assignment's requirements:
- Implement a simple RAM-only cache of HTTP responses
- Optimize your cache to solve the thundering herd problem by request coalescing.
- The coalesced requests must be receiving data as soon as it is available in order to not trigger envoy's idle timers.
- Disconnecting clients shouldn't affect anyone else

I also set the following requirement:
- A cached response that is being served to a client must remain alive until the client streamed all of it/stops observing it, regardless if it is evicted from the cache in the meantime

These are the main properties and scope limitations:
- Request eligibility: caches GET requests without a request body. Other requests bypass the cache.
    - Supporting bodies would also require deciding how they affect request identity and forwarding them through a shared fetch, which would require buffering and waiting for the arrival of the body before deciding on cachability 
- Cache key: scheme + authority + full path, including query parameters.
- TTL policy: TTL derived from HTTP headers. Prefers s-maxage, then max-age, otherwise Expires. Accounts for Date, Age, and response delay. Responses with no usable TTL aren't stored.
- Expiration: TTL deadlines with lazy expiry on lookup. Expired entries remain allocated until replaced or evicted.
- Cacheability policy: rejects responses containing Cache-Control: no-store, no-cache, or private.
- Eviction policy: FIFO, does not consider TTL
- Capacity: configurable per-entry and total cache byte limits, including headers, body, and trailers. Defaults: 1 KiB per entry, 1 MiB total.
    - Due to lifetime requirement of evicted responses which are being read, this doesn't strictly bound the memory usage, rather it is a soft limit for insertion
- Sharing: one synchronized cache per cache filter configuration, shared across workers.
- Request coalescing: worker-local. Identical misses share a fetch only before response headers arrive.
    - This means coalescing happens for requests handled by the same worker, but not globally across workers. This still significantly reduces the number of upstream requests.
- Streaming and cancellation: subscribers receive chunks as they arrive. Disconnecting a subscriber does not cancel the shared fetch.
- Insertion: inserts only completed responses. Oversized or uncacheable responses still stream downstream without being cached.
- Reader lifetime: shared ownership keeps acquired responses alive after eviction.

The reasoning behind these choices is explained further down in the writeup.

## 2. Architecture and design decisions

### Shared storage and ownership

I considered separate worker caches to avoid synchronization, but workers could store the same response multiple times. I chose one shared cache per filter configuration to avoid duplication and give its capacity setting one meaning across workers. Different configurations can still have different capacities. The factory callback retains a shared `CacheFilterContext`, which stream filters share. The context holds the cache and policies through shared resources, plus a thread-local slot for fetch managers.

The cache uses `absl::flat_hash_map` for average constant-time lookup, insertion, and removal. Envoy already uses it, and I expected better locality and less pointer chasing than a node-based map. I did not benchmark the alternatives or investigate open-addressing/tombstone costs in detail.

Sharing storage requires locking: lookups take a reader lock; insertion, replacement, and eviction take a writer lock. Callers receive `shared_ptr<const CachedResponse>` on cahce hit rather than a reference into the map. This also separates reader lifetime from cache membership: eviction removes the cache's reference, while existing readers keep the immutable response alive while it is needed.

I chose FIFO as a simple example implementation of the eviction policy interface. It tracks insertion order with a list and an additional map, adding allocations and key copies. Hits do not change the order, and expired entries get no priority, so a popular response can still be evicted. In a production environment, I'd implement a few smarter policies such as LRU and add the ability to configure which policy is used via the Envoy's yaml configuration system.

### Response representation and insertion

A response arrives in pieces: headers, body chunks, optional trailers. `CachedResponseBuilder` assembles it without exposing a partial response as a cache entry.  The final `CachedResponse` representation uses unique pointers to Envoy header/trailer maps and `Buffer::OwnedImpl` for the body. 

I considered storing the response in one flat structure to reduce indirection and allocations. However, my reasoning was that while this would improve memory locality, serving the response still requires copying the headers, body, and trailers, which would be an expensive operation as well in this case, my hypothesis is that it would be more expensive than the indirection, but I did not explicitly benchmark it. Envoy's APIs take ownership of the maps or accept mutable buffers, with no guarantee that the data will remain intact. We therefore cannot pass the shared cached response directly. I kept Envoy's native objects and decided to change the representation later if profiling shows it is worth it.

I separated cache insertion from response delivery because not every fetched response can be cached, but it still needs to reach the clients. Each `FetchOperation` owns a `CacheInsertion`, which checks cacheability, freshness, and size limits while accumulating the response. The fetch can continue streaming data to subscribers even if insertion is rejected, for example when the response exceeds the entry size limit.

I also didn't want partially downloaded responses to be visible in the cache. `CacheInsertion` builds the response separately and only inserts it when the upstream response completes. `Cache` then handles the total capacity and eviction, independently of how the response was fetched.

### Coalescing and fetch ownership

My first idea was to let one filter forward the request upstream and have the others wait for its response. This would let the request continue through the normal filter chain, but it would also tie the shared fetch to the initiating client. If that client disconnected, the other clients could lose the upstream request.

I considered handing off the fetch to another filter when the initiating client disconnected. However, this would require coordinating ownership and state between different request streams, especially if clients disconnect during the handoff. I decided to avoid this complexity by making the upstream fetch independent of any downstream client.

I moved the request into `Http::AsyncClient` and made `FetchManager` own active fetches. Each filter has a cancellable subscription. Disconnecting only removes that subscriber; even if all subscribers leave, the operation can finish and populate the cache. The trade-off is that the async request does not automatically pass through the normal downstream HTTP filter chain.

I also considered sharing active fetches across workers. However, filters and async callbacks belong to a worker dispatcher, so this would require cross-thread notification, cancellation, and buffer ownership rules. I chose one manager per worker through Envoy's thread-local infrastructure, while keeping completed cache entries shared. This avoids cross-worker coordination during fetching, but simultaneous misses on different workers can still issue duplicate requests.

Joining an in-progress request closes when response headers arrive. A later subscriber would need the response prefix as well as future chunks. I did not implement prefix replay or per-subscriber read positions, so an arrival during body download starts another fetch until a completed entry is available. Streaming to existing subscribers and accepting late subscribers are separate problems.

On a miss, the filter uses the selected route's cluster and timeout for its async request. Missing route, route entry, or cluster means continuing the normal chain so the router handles the error. This way, I reuse Envoy's routing infrastructure.

### Asynchronous lifetimes and callback safety

The main lifetime questions were borrowed objects and callbacks that can complete or destroy objects before the current call returns. I reviewed the subscriber, request headers, and options separately:

- **Subscribers:** `FetchOperation` stores non-owning pointers to subscribers, so `onDestroy()` must cancel the filter's subscription. Notification snapshots subscriber IDs and checks whether each subscriber is still registered before calling it. This allows subscribers to be removed during notification without accessing a destroyed object. The fetch operation also keeps itself alive during notification, so a reentrant reset callback cannot destroy it mid-callback.
- **Request headers:** copied for the async fetch and retained by a stream destructor callback until stream destruction, which is an "ugly" solution to the ownership problem, as the operation only receives an observer pointer to the stream created by the `AsyncClient`, so it can't destroy the stream but rather must ensure the headers remain available as long as the stream is, in case the stream tries accessing them.
- **Options:** the current timeout is consumed during stream construction. Future options containing borrowed pointers need their own lifetime review.

Cancellation also has to handle the special case of instant callbacks: `subscribe()` may complete or destroy the filter before returning. In that case, the returned subscription must be cancelled rather than stored. Separate fetch/subscriber IDs prevent an old subscription affecting a replacement fetch for the same key.

## 3. Testing

Used AI assistance to generate tests from described expected behavior and class APIs, then reviewed them for weak assertions, ambiguity, duplication and mixing of responsibilities. I wanted to make sure the tests will catch implementation errors rather than conform to the implementation. For example, one issue recorded in my working notes was a TTL test whose inputs and expected values did not distinguish the intended Date/Expires behavior. 

The unit tests cover cache policies, size accounting, insertion, and request coalescing and test for issues with object lifetimes and callback ordering, such as disconnecting subscribers, callbacks that complete synchronously, and responses being evicted while a client is still reading them.

The [Python integration tests](integration/harness.py) use real connections and a controlled origin for concurrent requests, delayed chunks, disconnection, multiple workers, and reuse without another origin request. Its streaming check establishes body delivery before completion, not prompt delivery of every chunk or idle-timeout behavior. The multi-worker case checks that some origin requests are avoided, not one global fetch. The unit test explicitly covers disconnecting either initiator or follower; concurrent client launch order in the harness does not establish which client initiated the fetch.

## 4. Benchmarks and profiling

### Benchmarks

I wanted to measure both the benefit of serving cached responses and the overhead introduced when requests cannot be cached. I ran two experiments using Nighthawk:

- [Experiment 01](experiments/01/README.md) compares warm cache hits against Envoy without the cache filter.
- [Experiment 02](experiments/02/README.md) uses a unique query parameter for every request, preventing cache hits and request coalescing. Both configurations include the same Lua rewrite.

I used the existing `fastbuild` Envoy binary on my laptop, since rebuilding an optimized version would be expensive. The origin served a 1 KiB file. Each run lasted 20 seconds, with 1, 2, or 4 Envoy workers. Nighthawk used two workers and 256 connections per worker, with requested total rates of 5,000, 10,000, and 50,000 RPS.

At the 50,000 RPS target, I measured the following differences in successful throughput (`benchmark.http_2xx` per second):

| Envoy workers | Warm-cache throughput / baseline | Unique-miss throughput change |
|---:|---:|---:|
| 1 | 2.70x | -23.3% |
| 2 | 2.51x | -24.3% |
| 4 | 2.56x | -25.6% |

The warm-cache setup achieved roughly 2.5-2.7 times the baseline throughput, while the unique-miss setup performed about 23-26% worse. The same trend appeared at lower requested rates. This shows the trade-off of the current implementation: cache hits avoid upstream work, but requests that cannot be reused still pay for lookup, fetch management, and insertion checks.

These results are only rough comparisons. 

### Profiling observations

I used `perf` to investigate where the cache spends CPU time, profiling warm cache hits and workloads with 90/10 and 50/50 hit/miss mixes. For the misses, I used unique keys to prevent both cache hits and coalescing.  The [profiling setup](experiments/profiling/README.md) uses four Envoy workers and a requested load of 20,000 RPS.

**Cache hits**

I first looked at `CacheFilter::decodeHeaders` to see how expensive serving a cached response was. Expanding its call tree showed that a considerable part of the work was actually downstream response processing. In one worker, `encodeData` accounted for about 3.10% of the displayed samples, `encodeHeaders` for 2.98%, and `CachedResponse::copyHeaders` for 1.37%. 

Header reconstruction was visible, but it wasn't the largest cost in that path. I considered whether reusing header maps or their backing storage could avoid allocations. However, Envoy's downstream APIs take ownership of header maps, while the cached response must remain reusable. 

**Cache misses**

With more misses, the profile shifted toward `FetchManager::subscribe`, upstream request creation, and response callbacks. I expanded the call graph and got the following information:

For example, one worker showed `FetchOperation::start` at about 3.47%, with `AsyncStreamImpl::sendHeaders` accounting for about 3.04% along that call path. This suggested that most of the sampled work attributed to starting a fetch was in Envoy's `AsyncClient` work, rather than the fetch manager's bookkeeping.

I then looked at the response callbacks. `FetchOperation::onHeaders` spent much of its inclusive cost notifying subscribers and preparing the cached headers. Expanding subscriber notification led into Envoy's downstream `encodeHeaders`. Similarly, `FetchOperation::onData` showed approximately 1.02% under subscriber notification and 0.84% under `CacheInsertion::onData`.

This made me inspect how the response body is handled. `CacheInsertion::onData` copies incoming chunks into the builder using `body_.add(data)`. When the response completes, the builder moves the accumulated buffer into `CachedResponse`. I therefore didn't find an obvious redundant copy in the insertion path.

The profiles showed where the work happens, but they didn't establish a specific optimization worth implementing. The percentages are sampled, inclusive call-tree costs, not CPU time per request, and I used a non-optimized `fastbuild` binary. I didn't identify an obvious redundant operation or a sufficiently clear bottleneck to justify redesigning a part of the system. Potentially this decision could be revisited using an optimized build.

## 5. Limitations and production considerations

### Workload fit and scaling

Repeated cacheable GETs benefit most. Unique requests pay lookup, fetch management, buffering, and insertion costs without reuse. FIFO can also evict a popular entry when mixed with one-off responses.

Cache locking and hot-response reference counts remain shared across workers. Insertions, eviction, and possible rehashing happen under the writer lock. I would measure lock contention as well, to see how it scales with the number of workers and if it would introduce a meaningful bottleneck at a reasonable worker count.

### HTTP correctness and security

Authorization, Range, and conditional requests are not excluded from lookup/coalescing; response admission does not require status 200 or implement `Vary`. Requests that should remain separate can therefore reuse or share content.

Request identity and eligibility need checking **before lookup and coalescing**, with authentication/authorization preceding the cache. Response admission is too late to prevent incorrect sharing of an active fetch. Request cache directives, updated `Age`, revalidation, and invalidation after modifying requests also need handling.

### Maintainability and next steps

Separating policies, insertion, and fetch ownership makes changes easier to isolate and test. Callback lifetime/order remains the fragile part. Late joining to fetch operations could be implemented, however I decided to keep it out of scope for the sake of the assignment.

If this was a real production cache, implementing proper policies for safe HTTP behavior, including more detailed cache control behavior would be my priority. Then replacing the eviction policy and adding the ability to configure policies from the Envoy config yaml.

### Watermarking
Watermark buffers are used to protect a slow consumer (in this case the client receiving the response) from "being overwhelmed" by a fast producer (upstream origin) ie. from unbounded growth of the response buffer. When the client's buffer reaches the high watermark size, it signals the upstream to slow down with delivery. Analogously the low watermark signals the upstream to continue delivery.

The complicating circumstance here is the coalescing, because different clients subscribed to the same upstream can have different consumption speeds. My solution is as follows:
- Currently, the incoming response is being buffered in the `CacheInsertion`'s `CachedResponseBuilder` by the `FetchOperation` managing the upstream fetch, but this buffer is private
- I would create a shared response buffer visible to the subscribed clients, and track each subscriber's read position (position in buffer)
  - As long as there is at least one client who hasn't signaled the high watermark, do not signal the upstream, let the stream continue as usual
  - If all subscribers signal their high watermark, signal high watermark to the upstream
- Advance each subscriber's read position as data is delivered to the subscriber (response chunks are handed to a subscriber if their downstream buffer didn't signal high watermark)
- Chunks can be released from the shared buffer once every remaining subscriber advances past them

Now, a problem with this approach is that if there is a very slow subscriber, the shared response buffer's size is not bounded and it can grow indefinitely. I propose two ways to deal with this:
1) If the buffer hits a fixed memory limit, signal high watermark to upstream
    - This way the slow subscriber can stay subscribed, but impedes progress of the faster subscribers
2) If a subscriber's backlog ie. distance from newest response chunk exceeds a configured limit, disconnect them
    - Now the subscriber doesn't impede the progress of the rest, but their request can fail repeatedly if they are coalesced with faster subscribers, which would require additional handling

The algorithm could remain independent of HTTP version, as Envoy exposes downstream callbacks for watermarking.

## 6. Time spent

Tracked approximately 40 hours of work sessions through the beginning of Day 8, with additional time spent on the writeup afterwards.

| Period | Recorded time | Main activities |
|---|---:|---|
| Days 1-2 | 10h | Setting up and building Envoy, exploring codebase, exploring HTTP filters API, Envoy threading model |
| Days 3-5 | 17h 50m | Further research into Envoy's architecture,designing the cache and coalescing mechanism, implementation and debugging |
| Days 6-7 | 10h 25m | Refactoring, reviewing ownership and lifetimes, tests, integration testing, benchmarking |
| Day 8 | 1h 30m recorded | Profiling and performance investigation |
| **Recorded subtotal** | **39h 45m** | |

Research, implementation, and debugging often overlapped, and some sessions included build waiting or potentially breaks (but I tried to log those accurately).