from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from yt_dlp import DownloadError, YoutubeDL

OUTPUT_DIR = Path(r"C:\Users\Lamine\Desktop\projetvideo\videos")


@dataclass(slots=True)
class FormatInfo:
    """Represent a download format returned by yt-dlp."""

    format_id: str
    ext: str
    resolution: str
    height: Optional[int]
    fps: Optional[float]
    vcodec: str
    acodec: str
    filesize: Optional[int]

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "FormatInfo":
        resolution = str(data.get("resolution") or "")
        if not resolution:
            height = data.get("height")
            width = data.get("width")
            if height and width:
                resolution = f"{width}x{height}"
        return cls(
            format_id=str(data.get("format_id")),
            ext=str(data.get("ext") or ""),
            resolution=resolution,
            height=(int(data["height"]) if data.get("height") is not None else None),
            fps=(float(data["fps"]) if data.get("fps") is not None else None),
            vcodec=str(data.get("vcodec") or ""),
            acodec=str(data.get("acodec") or ""),
            filesize=(int(data["filesize"])
                      if data.get("filesize") is not None
                      else None),
        )

    @property
    def preferred(self) -> tuple[int, int]:
        """Return preference weight for sorting purposes."""

        ext_score = 0 if self.ext.lower() == "mp4" else 1
        codec_lower = self.vcodec.lower()
        codec_score = 0 if ("avc" in codec_lower or "h264" in codec_lower) else 1
        return ext_score, codec_score


def ensure_output_dir(directory: Path | None = None) -> Path:
    """Ensure that the output directory exists."""

    target = directory or OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def _format_listing(formats: Iterable[Dict[str, object]]) -> List[FormatInfo]:
    formatted: List[FormatInfo] = []
    for fmt in formats:
        if not fmt.get("vcodec"):
            # Skip audio only formats for the UI list.
            continue
        formatted.append(FormatInfo.from_dict(fmt))
    formatted.sort(
        key=lambda info: (
            info.preferred,
            -(info.height or 0),
            -(info.fps or 0.0),
        ),
    )
    return formatted


def probe_formats(url: str) -> List[FormatInfo]:
    """Return available video formats for a YouTube URL."""

    ensure_output_dir()
    ydl_opts = {
        "noplaylist": True,
        "quiet": True,
        "skip_download": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("formats", [])
    return _format_listing(formats)


ProgressCallback = Callable[[Dict[str, object]], None]


def _build_format_selector(fmt_id: Optional[str]) -> str:
    if fmt_id:
        return f"{fmt_id}+bestaudio/bestvideo+bestaudio/best"
    return "bestvideo+bestaudio/best"


def download(
    url: str,
    fmt_id: Optional[str],
    out_dir: Path | None,
    progress_cb: ProgressCallback,
) -> Path:
    """Download the selected video format alongside best audio."""

    output_dir = ensure_output_dir(out_dir)
    outtmpl = str(output_dir / "%(title)s [%(id)s].%(ext)s")
    format_selector = _build_format_selector(fmt_id)
    ydl_opts = {
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [progress_cb],
        "concurrent_fragment_downloads": 3,
        "format": format_selector,
    }
    with YoutubeDL(ydl_opts) as ydl:
        try:
            result = ydl.extract_info(url, download=True)
        except DownloadError as exc:
            raise RuntimeError(str(exc)) from exc
        if isinstance(result, dict):
            filename = Path(ydl.prepare_filename(result))
        else:
            filename = Path(result[0])
    return filename
