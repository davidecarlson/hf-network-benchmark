# Measurements that motivated this tool

The measurements below were taken on 2026-10-01 before this portable script was created. They are historical results, not validation runs of the new script.

| Host | Internet interface | Link speed | Model bytes | Download seconds | Effective MB/s |
|---|---|---:|---:|---:|---:|
| Compute `b40x4-07` | `eth0` | 1 Gb/s | 55,586,114,863 | 408.6649 | 136.0188 |
| Login `login1` | `eth5` | 10 Gb/s | 55,586,114,863 | 67.1367 | 827.9545 |

Both used `Qwen/Qwen3.8-27B` revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`, Hugging Face Hub 1.31.0, and hf-xet 1.6.0. The successful login run used two file workers and four range requests per file; the compute run used eight workers. The login node's initial eight-worker attempt was killed after encountering its memory limit, and the successful run started from a new directory and cache.

The login-node completed run was 6.0871 times faster. An independent 1 GiB identity-encoded transfer on the compute node measured 109.7606 MB/s overall and 115.6561 MB/s during streaming. Model throughput counts reconstructed file bytes, so compression can account for a rate above the 1 Gb/s raw ceiling.

[Full evidence and network layout](https://github.com/davidecarlson/codex-conversations/blob/main/docs/network-layout.md)
