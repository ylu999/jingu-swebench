# Docker Setup for SWE-bench

Source: https://www.swebench.com/SWE-bench/guides/docker_setup/

## Prerequisites

- Docker installed and running
- 120GB+ free disk space
- 16GB+ RAM
- 8+ CPU cores (x86_64)

## Platform Setup

### macOS / Windows
Install Docker Desktop. Configure resource allocation:
- CPUs: 8+
- RAM: 16GB+
- Disk: 120GB+

### Linux (Amazon Linux, Ubuntu, etc.)
```bash
# Install Docker
sudo yum install docker -y   # Amazon Linux
sudo systemctl start docker
sudo usermod -aG docker $USER
newgrp docker

# Verify
docker --version
docker run hello-world
```

## Image Architecture (3-layer)

1. **Base images** — common Python/OS dependencies
2. **Environment images** (~60 configs) — per-repo Python environment
3. **Instance images** — per-task checkout at `base_commit`

## Cache Level Selection

| Level | Disk Usage | Rebuild Time | When to Use |
|-------|-----------|--------------|-------------|
| `none` | Minimal | Every run | Testing only |
| `base` | Minimal | Most of run | Low disk |
| `env` | ~100GB | First run only | **Default recommendation** |
| `instance` | ~2TB | Never (after first) | Production speed |

ECS EC2 instance → use `env` cache level (image pull cached by vfs storage driver).

## Disk Management

```bash
docker system df           # View Docker disk usage
docker system prune        # Remove all unused objects
docker image ls            # List images
docker container ls -a     # List containers
```

## Performance Tuning

```
workers = min(0.75 * cpu_count(), 24)
```
For 8 CPUs → 6 workers max.

## Troubleshooting

### "No space left on device"
```bash
docker system prune -a --volumes
```

### Permission denied
```bash
sudo usermod -aG docker $USER && newgrp docker
```

### Image build network errors
Check DNS: `docker run --rm alpine nslookup pypi.org`
