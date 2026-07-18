# Ch. 3 notes — Hardware & Infrastructure Setup

## Test machine specs (captured 2026-07-18)
- Device name: GOSWAMI
- CPU: Intel Core i3-1115G4 @ 3.00GHz (2 cores / 4 threads)
- RAM: 8.00 GB installed, 7.74 GB usable
- GPU: Intel UHD Graphics (128 MB, not used for this work)
- OS: 64-bit Windows, x64-based processor
- Storage: 260 GB of 477 GB used

## Why this matters for the thesis
This is a genuinely resource-constrained machine relative to typical cloud/server
hardware, which is directly relevant to the thesis's framing ("...Object Storage
Systems in Resource-Constrained Environments"). It also forces methodological
choices documented in Ch. 3.2/3.4:
- Storage backends are run one at a time, never concurrently, both because 8GB RAM
  cannot comfortably host 5 storage systems + monitoring stack simultaneously, and
  because sequential isolation gives cleaner per-system resource measurements.
- Docker Desktop / WSL2 memory should be capped (recommended: .wslconfig with
  memory=4GB, processors=2) so the host OS and other backends (e.g. the MicroCeph
  Multipass VM) aren't starved. NOTE: this file was NOT auto-edited by the
  assistant — modifying system-wide WSL config is left to the user. Suggested
  content is:

  ```
  [wsl2]
  memory=4GB
  processors=2
  ```

  Save as `%USERPROFILE%\.wslconfig`, then run `wsl --shutdown` and restart
  Docker Desktop for it to take effect.

## Docker Desktop
- Docker CLI version: 28.3.3 (build 980b856)
- WSL2 backend, default distro: docker-desktop, version 2
