# Dataset locks

Commit one `cvbench.dataset-lock/v1` JSON document for each external dataset
release consumed by a benchmark manifest. Locks are release pointers, not
annotation manifests.

`real-video-v2.compatibility.json` is the sole exception: it pins the frozen
inline fixture that existed before the benchmark and dataset repositories were
separated.
