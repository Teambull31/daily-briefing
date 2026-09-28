"""/etat : santé de la machine (GPU, RAM, disque) et de l'IA locale, vue depuis le téléphone."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx

GB = 1024**3


def amd_gpus(drm_root: Path = Path("/sys/class/drm")) -> list[str]:
    """Charge et VRAM des cartes AMD, lues dans sysfs (aucun outil à installer)."""
    lines = []
    for dev in sorted(drm_root.glob("card[0-9]*/device")):
        try:
            total = int((dev / "mem_info_vram_total").read_text())
            used = int((dev / "mem_info_vram_used").read_text())
            busy = (dev / "gpu_busy_percent").read_text().strip()
        except (OSError, ValueError):
            continue  # pas une carte AMD, ou pas de pilote amdgpu
        temp = _amd_temp(dev)
        lines.append(f"GPU {busy}% · VRAM {used / GB:.1f}/{total / GB:.0f} Go" + (f" · {temp}°C" if temp else ""))
    return lines


def _amd_temp(dev: Path) -> str:
    for sensor in dev.glob("hwmon/hwmon*/temp1_input"):
        try:
            return f"{int(sensor.read_text()) / 1000:.0f}"
        except (OSError, ValueError):
            pass
    return ""


def nvidia_gpus() -> list[str]:
    if not shutil.which("nvidia-smi"):
        return []
    query = "utilization.gpu,memory.used,memory.total,temperature.gpu"
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = []
    for row in out.strip().splitlines():
        busy, used, total, temp = (x.strip() for x in row.split(","))
        lines.append(f"GPU {busy}% · VRAM {int(used) / 1024:.1f}/{int(total) / 1024:.0f} Go · {temp}°C")
    return lines


def memory(meminfo: Path = Path("/proc/meminfo")) -> str:
    try:
        info = dict(line.split(":", 1) for line in meminfo.read_text().splitlines() if ":" in line)
        total = int(info["MemTotal"].split()[0]) * 1024
        available = int(info["MemAvailable"].split()[0]) * 1024
    except (OSError, KeyError, ValueError):
        return ""
    return f"RAM {(total - available) / GB:.1f}/{total / GB:.0f} Go"


def system_lines(workspace: Path) -> list[str]:
    lines = amd_gpus() + nvidia_gpus()
    if mem := memory():
        lines.append(mem)
    if hasattr(os, "getloadavg"):
        load = os.getloadavg()[0]
        lines.append(f"CPU charge {load:.1f} ({os.cpu_count()} threads)")
    disk = shutil.disk_usage(workspace)
    lines.append(f"Disque libre {disk.free / GB:.0f} Go")
    return lines


async def ollama_lines(base_url: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            version = (await client.get(f"{base_url}/api/version")).json().get("version", "?")
            models = (await client.get(f"{base_url}/api/ps")).json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return ["❌ Ollama ne répond pas"]
    lines = [f"✅ Ollama {version}"]
    for m in models:
        size = m.get("size") or 0
        on_gpu = 100 * (m.get("size_vram") or 0) / size if size else 0
        lines.append(f"  • {m.get('name')} chargé ({size / GB:.1f} Go, {on_gpu:.0f}% sur GPU)")
    if not models:
        lines.append("  • aucun modèle chargé (il se chargera au prochain message)")
    return lines
