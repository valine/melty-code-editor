# Real PyTorch repository review

Cloned into `/home/lukas/Desktop/pytorch-review` and marked as projects in the shared project store. No source files changed and no venvs installed. Scans use the exact dependency-manager assembler.

## Snapshot results

| Project | Table rows | Requirements entries | TOML entries | Red imported rows | Scan time |
|---|---:|---:|---:|---:|---:|
| [nanoGPT](https://github.com/karpathy/nanoGPT) | 18 | 0 | 0 | 8 | 0.02s |
| [ignite](https://github.com/pytorch/ignite) | 113 | 33 | 2 | 45 | 0.77s |
| [examples](https://github.com/pytorch/examples) | 73 | 0 | 0 | 21 | 0.13s |
| [timm](https://github.com/huggingface/pytorch-image-models) | 79 | 11 | 5 | 22 | 1.07s |

Counts describe fresh clones with no environment; they are not installation failures. The populated-environment and orange-cell paths are not exercised by this review.

## Findings

### nanoGPT

README-only installation instructions: both declaration columns are correctly empty. model is correctly local. wandb is feature-gated by wandb_log, but classified as required. No manifest constraints can be inferred from this snapshot.

### ignite

Root requirements-dev.txt is merged under the requirements.txt heading, losing its development scope. Runtime TOML dependencies are correctly discovered. The bare Git URL in requirements-dev.txt is Unparsed; pytorch_fid appears separately. scikit-image/skimage, scikit-learn/sklearn and neptune-client/neptune are split without installed metadata. Tests, docs and example integrations contribute many red cells.

### examples

No root requirements file; many examples have their own requirements.txt, which the root scan misses. Imports from those examples are included anyway. distributed/minGPT-ddp requires torch>=2.7, but the root Torch recommendation offers unconstrained torch. urllib2 is a Python 2 ImportError fallback incorrectly classified as required. Local imports such as model and src are correctly local.

### timm

Runtime requirements and PEP 621 dependencies are discovered and aligned, including combined torch>=1.7. requirements-dev.txt is merged into the requirements.txt column. The PDM test group is absent from TOML. pyyaml/yaml split without installed metadata; PIL has no distribution identity. Conversion scripts and optional readers contribute JAX, MXNet, Paddle and TensorFlow imports, making these appear project-wide requirements.

## Recommended order

1. No venv: show setup as the next action; suppress install actions until one is selected.
2. Preserve declaration file/group provenance. A requirements-dev file should not be labeled requirements.txt; installing runtime dependencies should not install every test integration.
3. Handle nested example manifests and their ownership before making root-level Torch recommendations. Do not combine incompatible examples into one forced environment.
4. Recognize legacy ImportError fallbacks as conditional, and distinguish optional features from unconditional runtime imports.
5. Preserve Git requirements as source dependencies; resolve package/import aliases using metadata rather than guessing PyPI names.

## Evidence

Full aligned tables and import locations: [tables.html](tables.html). JSON snapshots include commit hashes, suggested actions and source locations.

### Source examples

- [nanoGPT README dependencies](/home/lukas/Desktop/pytorch-review/nanoGPT/README.md:19) and [conditional wandb import](/home/lukas/Desktop/pytorch-review/nanoGPT/train.py:244).
- [Ignite development requirements and Git URL](/home/lukas/Desktop/pytorch-review/ignite/requirements-dev.txt:1), [runtime TOML](/home/lukas/Desktop/pytorch-review/ignite/pyproject.toml:19).
- [minGPT example's Torch constraint](/home/lukas/Desktop/pytorch-review/examples/distributed/minGPT-ddp/requirements.txt:1), [legacy urllib2 fallback](/home/lukas/Desktop/pytorch-review/examples/cpp/tools/download_mnist.py:10).
- [timm's PDM development group](/home/lukas/Desktop/pytorch-review/timm/pyproject.toml:52), [optional dataset reader](/home/lukas/Desktop/pytorch-review/timm/timm/data/readers/reader_hfds.py:13).

### UI screenshots

All four tables were also assembled in the actual dependency-manager window.

- [nanoGPT](../../screenshots/review-nanogpt.png)
- [Ignite](../../screenshots/review-ignite.png)
- [Examples](../../screenshots/review-examples.png)
- [timm](../../screenshots/review-timm.png)
