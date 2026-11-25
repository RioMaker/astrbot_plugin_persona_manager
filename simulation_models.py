"""Verified, on-demand CPU assets for the Dashboard simulator."""

import asyncio
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from ctypes.util import find_library
from dataclasses import dataclass
from pathlib import Path

import aiohttp


@dataclass(frozen=True)
class Asset:
    filename: str
    size: int
    sha256: str
    urls: tuple[str, ...]


def _model(repo, revision, filename, size, sha256):
    return Asset(
        filename,
        size,
        sha256,
        (
            f"https://modelscope.cn/models/{repo}/resolve/master/{filename}",
            f"https://huggingface.co/{repo}/resolve/{revision}/{filename}",
        ),
    )


MODELS = {
    "Qwen3.5-2B": _model(
        "unsloth/Qwen3.5-2B-GGUF",
        "f6d5376be1edb4d416d56da11e5397a961aca8ae",
        "Qwen3.5-2B-Q4_K_M.gguf",
        1280835840,
        "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223",
    ),
    "Qwen3-8B": _model(
        "Qwen/Qwen3-8B-GGUF",
        "7c41481f57cb95916b40956ab2f0b139b296d974",
        "Qwen3-8B-Q4_K_M.gguf",
        5027783488,
        "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
    ),
}
ENGINE = Asset(
    "llama-b12679-bin-ubuntu-x64.tar.gz",
    23378151,
    "6a614893470238e246b1b26dd36f27b402b96415833594fba217dcd061e01cc1",
    (
        "https://github.com/ggml-org/llama.cpp/releases/download/b12679/llama-b12679-bin-ubuntu-x64.tar.gz",
    ),
)
OPENMP = Asset(
    "libgomp1.deb",
    115772,
    "48fec46bda7f5b1638b9e959889bfbc20491247d402d120bb152687eb48143d7",
    (
        "https://deb.debian.org/debian/pool/main/g/gcc-12/libgomp1_12.2.0-14+deb12u1_amd64.deb",
    ),
)


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelFiles:
    def __init__(self, directory: Path):
        self.directory = directory
        self.checked = {}

    async def fetch(self, session, asset, progress):
        target = self.directory / asset.filename
        part = target.with_name(target.name + ".part")
        if target.is_file() and target.stat().st_size == asset.size:
            stamp = target.stat().st_mtime_ns
            if self.checked.get(target) == stamp:
                return target
            progress(f"校验 {asset.filename}")
            if await asyncio.to_thread(_digest, target) == asset.sha256:
                self.checked[target] = stamp
                return target
        last_error = ""
        for url in asset.urls:
            try:
                offset = part.stat().st_size if part.exists() else 0
                if offset == asset.size:
                    progress(f"校验 {asset.filename}")
                    if await asyncio.to_thread(_digest, part) == asset.sha256:
                        part.replace(target)
                        self.checked[target] = target.stat().st_mtime_ns
                        return target
                    part.unlink()
                    offset = 0
                elif offset > asset.size:
                    part.unlink()
                    offset = 0
                if shutil.disk_usage(self.directory).free < asset.size - offset:
                    raise ValueError(
                        f"磁盘空间不足，{asset.filename} 还需下载 {(asset.size - offset) / 1e9:.2f} GB"
                    )
                progress(f"连接下载源，准备 {asset.filename}")
                await self._download(session, url, asset, part, offset, progress)
                progress(f"校验 {asset.filename}")
                if await asyncio.to_thread(_digest, part) != asset.sha256:
                    part.unlink()
                    raise RuntimeError("SHA256 校验失败")
                part.replace(target)
                self.checked[target] = target.stat().st_mtime_ns
                return target
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_error = (
                    str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                )
        raise ValueError(
            f"{asset.filename} 下载失败（{last_error}），请检查下载源连接后重试。"
        )

    @staticmethod
    async def _download(session, url, asset, part, offset, progress):
        # Bounded ranges also work behind proxies that buffer an entire response.
        while offset < asset.size:
            end = min(offset + 32 * 1024**2, asset.size) - 1
            headers = (
                {"Range": f"bytes={offset}-{end}"}
                if offset or end + 1 < asset.size
                else {}
            )
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    offset, end = 0, asset.size - 1
                elif response.status == 206:
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)",
                        response.headers.get("Content-Range", ""),
                    )
                    if (
                        not match
                        or int(match[1]) != offset
                        or not offset <= int(match[2]) <= end
                        or int(match[3]) != asset.size
                    ):
                        raise RuntimeError("下载源返回了无效的续传范围")
                    end = int(match[2])
                else:
                    raise RuntimeError(f"下载源返回 HTTP {response.status}")
                with part.open("ab" if offset else "wb") as output:
                    async for chunk in response.content.iter_chunked(1024**2):
                        offset += len(chunk)
                        if offset > end + 1:
                            raise RuntimeError("下载内容超过预期大小")
                        await asyncio.to_thread(output.write, chunk)
                        progress(
                            f"下载 {asset.filename} · {offset / asset.size:.0%}（{offset / 1e6:.0f} / {asset.size / 1e6:.0f} MB）"
                        )
                if offset != end + 1:
                    raise RuntimeError("下载未完整，可重试续传")

    async def prepare(self, model, progress):
        if sys.platform != "linux" or platform.machine().lower() not in {
            "amd64",
            "x86_64",
        }:
            raise ValueError(
                "内置模型适用于 Linux x86_64。当前系统可在「模拟模型」选择阿里云百炼或已配置的自定义模型。"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        import fcntl

        with (self.directory / ".prepare.lock").open("a+b") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    progress("等待正在运行的模型下载")
                    await asyncio.sleep(0.5)
            return await self._prepare_files(model, progress)

    async def _prepare_files(self, model, progress):
        engine_dir = self.directory / "llama-b12679"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            if not (engine_dir / "llama-server").is_file():
                archive = await self.fetch(session, ENGINE, progress)
                openmp = None
                if not await asyncio.to_thread(find_library, "gomp"):
                    openmp = await self.fetch(session, OPENMP, progress)
                progress("准备 llama.cpp CPU 引擎")
                install = asyncio.create_task(
                    asyncio.to_thread(_install_engine, archive, openmp, engine_dir)
                )
                try:
                    await asyncio.shield(install)
                except asyncio.CancelledError:
                    await install
                    raise
            weights = await self.fetch(session, MODELS[model], progress)
        return engine_dir / "llama-server", weights


def _install_engine(archive, openmp, destination):
    # Install the verified release into a private directory, then publish it atomically.
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        work = Path(staging) / "engine"
        work.mkdir()
        with tarfile.open(archive) as tar:
            for member in tar:
                name = Path(member.name).name
                if name != "llama-server" and not (
                    name.startswith("lib") and ".so" in name
                ):
                    continue
                path = work / name
                if member.isfile():
                    with path.open("wb") as output:
                        shutil.copyfileobj(tar.extractfile(member), output)
                    path.chmod(0o755)
                elif member.issym() and Path(member.linkname).name == member.linkname:
                    path.symlink_to(member.linkname)
        if openmp is not None:
            _unpack_openmp(openmp, work)
        os.rename(work, destination)


def _unpack_openmp(package, directory):
    # Slim Debian/AstrBot images can omit libgomp. Unpack just the library locally.
    try:
        data = subprocess.check_output(
            ["dpkg-deb", "--fsys-tarfile", str(package)], timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            "无法解包 CPU 运行库，需要 Ubuntu / Debian 自带的 dpkg-deb。"
        ) from exc
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = next(
            m for m in tar if m.isfile() and Path(m.name).name == "libgomp.so.1.0.0"
        )
        with (directory / "libgomp.so.1").open("wb") as output:
            shutil.copyfileobj(tar.extractfile(member), output)
