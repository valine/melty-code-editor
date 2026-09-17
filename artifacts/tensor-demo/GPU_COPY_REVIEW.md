# GPU tensor capture and display review

Reviewed on 2026-09-15 after the initial CPU-snapshot implementation was rejected.

## Findings and fixes

| Path | Finding | Result |
| --- | --- | --- |
| Project capture | `.cpu()` and tensor serialization copied CUDA values to the host. | CUDA values now retain their original allocation and export CUDA IPC handles. No tensor bytes are written to disk or copied to CPU. |
| Producer lifetime | An ordinary subprocess exits after a function returns, invalidating shared GPU storage. | A separate control connection keeps the producer alive until all imported C++ storages expire. Views can outlive the first imported tensor. Editor exit closes the connection. |
| Volume rendering | Missing CUDA support silently selected a whole-tensor upload path. | CUDA inputs require the direct sampler. Missing support reports an error without falling back to a copy. |
| Line rendering | Dtype coercion, contiguous packing, and texture upload copied the source. | A CUDA rasterizer samples the original dtype and strides directly. Per-line range metadata and output pixels are separate derived allocations. |
| Unsupported source types | Sparse/complex conversion could silently allocate dense/magnitude tensors. | Direct CUDA rendering reports unsupported input and requires an explicit conversion in user code. |
| Loop history | Accumulation intentionally copies values into a history buffer. | Unchanged, as requested. Explicit means, sorts, and history stacking remain computations that allocate results. |
| Display output | Rendered RGBA pixels are staged to the display GL texture. | Retained. This is the finished 2-D image, not the source tensor. Small range/normalization metadata also reaches CPU. |

## Verification

- Real GPU IPC test across the editor and project Python environments: a producer-side mutation becomes visible without resending tensor data.
- Imported aliases retain identical storage, dtype, strides, and offsets, including bfloat16 and noncontiguous views.
- Producer survives while a sliced view remains, then exits after all captured storage is released.
- Copy-sensitive tests forbid `cpu`, `numpy`, `clone`, `contiguous`, and `to` during direct view construction/rendering. The producer test forbids serialization too.
- Real CUDA kernel tests cover float32, bfloat16, and int64 line rendering and volume view construction.
- 74 targeted tests passed. One GL-equivalence test was deselected for the headless test run; the actual editor UI was separately checked on a reserved Wayland desktop.
- `tensor_gpu_example.py` renders both a cuda:0 torus and a CUDA line graph. Screenshots: `tensor-cuda-shared.png`, `tensor-cuda-lines.png`.

The verified no-source-copy guarantee applies to dense, real-valued CUDA tensors in the default capture → IPC → direct rendering path. It does not claim that history accumulation, explicit transforms, CPU inputs, rendering outputs, or user-written tensor operations allocate nothing.

PyTorch's [CUDA sharing lifetime requirements](https://docs.pytorch.org/docs/stable/multiprocessing.html#sharing-cuda-tensors) require the producer to retain the allocation while consumers use it; the storage-lifetime test covers that requirement.
