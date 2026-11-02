# -*- coding: utf-8 -*-
"""Core for the Tencent FBX batch importer with per-asset map resolution.

Dieses Skript wird in 3ds Max 2024 über Scripting > Run Script ausgeführt.

Das Werkzeug extrahiert ausschließlich eingebettete Tencent-/Hunyuan-FBX-Texturen,
kopiert diese Texturen in einen abgeleiteten Maps-Unterordner, importiert die FBX-Datei,
erstellt ein neues VRayMtl mit VRayBitmap-Nodes über pymxs und speichert eine
MAX-Datei zum Merge im angegebenen Max-Zielordner. Externe Texturordner werden
nicht gescannt. BaseColor dient als Mapping-Source-Master für die übrigen PBR-Maps.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import sys
import tempfile
import traceback
import zlib
import json
import hashlib
import time
from dataclasses import dataclass, field, replace
from tencent_fbx_metadata import inspect as inspect_fbx, object_name
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import pymxs  # type: ignore

    rt = pymxs.runtime
except Exception:  # pragma: no cover - expected outside 3ds Max.
    pymxs = None
    rt = None

try:
    from PySide2 import QtCore, QtGui, QtWidgets  # type: ignore

    QT_BINDING = "PySide2"
except Exception:  # pragma: no cover - PySide is not available in local CI.
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

        QT_BINDING = "PySide6"
    except Exception:
        QtCore = None
        QtGui = None
        QtWidgets = None
        QT_BINDING = None


TOOL_NAME = "Tencent FBX Batch Importer"
TOOL_VERSION = "2.06"
LAST_UPDATE = "24.09.2026"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TEXTURE_SIZE_OPTIONS = (2048, 1024, 512)
IMAGE_EXTENSIONS = {
    ".bmp",
    ".dib",
    ".exr",
    ".gif",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".pic",
    ".png",
    ".psd",
    ".sgi",
    ".tga",
    ".tif",
    ".tiff",
}

MAP_SUFFIX = {
    "basecolor": "basecolor",
    "roughness": "roughness",
    "metalness": "metalness",
    "normal": "normal",
    "ao": "ao",
    "emission": "emission",
    "height": "height",
    "packed_metal_roughness": "metallicRoughness",
}

MAP_PRIORITY = [
    "normal",
    "packed_metal_roughness",
    "roughness",
    "metalness",
    "ao",
    "emission",
    "height",
    "basecolor",
]

MAP_DISPLAY_NAMES = {
    "basecolor": "BaseColor / Albedo",
    "roughness": "Roughness",
    "metalness": "Metalness",
    "normal": "Normalmap",
    "ao": "Ambient Occlusion",
    "emission": "Emission",
    "height": "Height / Displacement",
    "packed_metal_roughness": "gepackte Metallic/Roughness-Map",
}


def map_label(map_type: str) -> str:
    return MAP_DISPLAY_NAMES.get(map_type, map_type)

MAP_KEYWORDS = {
    "normal": ["normal", "nrm", "norm"],
    "packed_metal_roughness": [
        "metallicroughness",
        "metalroughness",
        "metalrough",
        "metal_rough",
        "orm",
        "arm",
        "occlusionroughnessmetallic",
    ],
    "roughness": ["roughness", "rough", "rgh"],
    "metalness": ["metallic", "metalness", "metal"],
    "ao": ["ao", "ambientocclusion", "occlusion"],
    "emission": ["emission", "emissive", "emit"],
    "height": ["height", "displacement", "disp", "bump"],
    "basecolor": [
        "basecolor",
        "base_color",
        "albedo",
        "diffuse",
        "color",
        "tex_color",
        "basecolour",
        "texture_pbr",
    ],
}

COLOR_MAP_TYPES = {"basecolor", "emission"}
RAW_MAP_TYPES = {
    "roughness",
    "metalness",
    "normal",
    "ao",
    "height",
    "packed_metal_roughness",
}

VRAY_BITMAP_TRANSFER_PROPS = [
    "color_space",
    "colorSpace",
    "transfer_function",
    "transferFunction",
    "color_space_transfer_function",
    "colorSpaceTransferFunction",
    "rgb_transfer_function",
    "rgbTransferFunction",
]

VRAY_BITMAP_PRIMARIES_PROPS = [
    "rgbColorSpace",
    "rgb_color_space",
    "rgb_primaries",
    "rgbPrimaries",
    "color_space_rgb_primaries",
    "colorSpaceRgbPrimaries",
    "primaries",
]

VRAY_BITMAP_MAPPING_SOURCE_PROPS = [
    "mapping_source",
    "mappingSource",
    "mappingSourceMap",
    "mapping_source_map",
    "mapping_source_texmap",
    "mappingSourceTexmap",
    "map_source",
    "mapSource",
    "source_map",
    "sourceMap",
    "uvwgen",
    "uvwGen",
    "uvw_generator",
    "uvwGenerator",
    "texmap_uvwgen",
    "texmapUVWGen",
    "mapping",
    "mapping_map",
    "mappingMap",
]


def expected_bitmap_color_settings(map_type: str) -> Tuple[int, List[str], int, List[str], str]:
    """Zielwerte für VRayBitmap-Farbinterpretation.

    transfer_value: Color space transfer function.
    primaries_value: RGB color space parameters / RGB primaries.
    """
    if map_type in COLOR_MAP_TYPES:
        return 2, ["sRGB", "srgb"], 1, ["sRGB", "sRGB primaries", "srgb"], "Farbdaten / sRGB"
    return 0, ["None", "Raw", "Linear", "linear"], 3, ["Raw", "Raw primaries", "raw"], "Technische Daten / Raw-Linear"


@dataclass
class MapHit:
    map_type: str
    source_path: Path
    score: int


@dataclass
class PngInfo:
    name: str
    offset: int
    end: int
    width: Optional[int] = None
    height: Optional[int] = None
    bit_depth: Optional[int] = None
    color_type: Optional[int] = None
    srgb: Optional[int] = None
    gamma: Optional[float] = None
    icc_profile: Optional[str] = None


@dataclass
class ScanResult:
    by_type: Dict[str, List[MapHit]] = field(default_factory=dict)
    ignored: List[Path] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def selected(self) -> Dict[str, MapHit]:
        result: Dict[str, MapHit] = {}
        for map_type, hits in self.by_type.items():
            if not hits:
                continue
            ordered = sorted(
                hits,
                key=lambda hit: (-hit.score, len(hit.source_path.name), str(hit.source_path).lower()),
            )
            result[map_type] = ordered[0]
        return result


@dataclass
class CopiedMap:
    map_type: str
    source_path: Path
    target_path: Path


@dataclass
class ImportOptions:
    fbx_file: Path
    asset_name: str
    prefix: str
    asset_folder: Path
    texture_folder: Path
    save_max_file: bool = True
    rename_texture_files: bool = True
    texture_downscale_sizes: Tuple[int, ...] = ()
    create_vray_bitmap_nodes: bool = True
    replace_imported_material: bool = True
    group_imported_objects: bool = False
    create_layer: bool = True
    layer_mode: str = "preset"
    layer_existing_name: str = "00_PPL"
    layer_custom_name: str = ""
    optimize_percent: float = 55.0
    pre_optimize_80: bool = True
    optimize_enabled: bool = True
    import_textures: bool = True
    requested_asset_name: str = ""
    asset_id: str = ""
    asset_short_code: str = ""
    import_time_code: str = ""
    imported_at: str = ""


class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def add(self, line: str = "") -> None:
        self.lines.append(line)

    def section(self, title: str) -> None:
        if self.lines and self.lines[-1]:
            self.lines.append("")
        self.lines.append(f"[{title}]")

    def warning(self, line: str) -> None:
        self.lines.append(f"WARNUNG: {line}")

    def error(self, line: str) -> None:
        self.lines.append(f"FEHLER: {line}")

    def text(self) -> str:
        return "\n".join(self.lines)


class ImportCancelled(RuntimeError):
    """The user cancelled before replacing any existing asset files or nodes."""


def safe_filename(value: str, default: str = "asset") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned or default


def validate_asset_name(value: str) -> str:
    """Require a user-supplied label; the import adds its own permanent identity."""
    if not isinstance(value, str) or not value.strip(' '):
        raise ValueError('Bitte einen Asset-Namen eingeben, z. B. Stuhl.')
    value = value.strip(' ')
    if re.search(r'[^A-Za-z0-9ÄÖÜäöüßẞ _-]', value):
        raise ValueError('Ungültiges Zeichen im Asset-Namen. Erlaubt sind Buchstaben, Ziffern, Leerzeichen, _ und -; keine Kommas, Punkte oder Ordnerpfade.')
    if not re.fullmatch(r'[A-Za-z0-9ÄÖÜäöüßẞ](?:[A-Za-z0-9ÄÖÜäöüßẞ _-]*[A-Za-z0-9ÄÖÜäöüßẞ])?', value):
        raise ValueError('Der Asset-Name muss mit einem Buchstaben oder einer Ziffer beginnen und enden.')
    for source, replacement in [('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'),
                                ('Ä', 'Ae'), ('Ö', 'Oe'), ('Ü', 'Ue'), ('ß', 'ss'), ('ẞ', 'SS')]:
        value = value.replace(source, replacement)
    cleaned = safe_filename(value, default='')
    if not cleaned:
        raise ValueError('Der Asset-Name muss Buchstaben oder Ziffern enthalten.')
    if len(cleaned) > 40:
        raise ValueError('Bitte einen kürzeren Asset-Namen verwenden (höchstens 40 Zeichen).')
    return cleaned


def asset_name_errors(names: Sequence[str]) -> Dict[int, str]:
    """Report every bad row, including aliases that produce the same file base."""
    errors: Dict[int, str] = {}
    groups: Dict[str, List[int]] = {}
    for row, name in enumerate(names):
        try:
            normalized = validate_asset_name(name)
        except ValueError as exc:
            errors[row] = str(exc)
        else:
            key = prefixed_asset_name(normalized).casefold()
            groups.setdefault(key, []).append(row)
    for rows in groups.values():
        if len(rows) > 1:
            message = ('Asset-Name doppelt in den Zeilen ' + ', '.join(str(row + 1) for row in rows)
                       + '. Jede Zeile dieser Importliste benötigt einen eigenen Namen.')
            for row in rows:
                errors[row] = message
    return dict(sorted(errors.items()))


def validate_batch_names(options_list) -> None:
    names = [getattr(options, 'requested_asset_name', '') or getattr(options, 'asset_name', '')
             for options in options_list]
    errors = asset_name_errors(names)
    if errors:
        details = '\n'.join(f'Zeile {row + 1}: {message}' for row, message in errors.items())
        raise ValueError('Import nicht gestartet. Bitte alle Namensfehler in der Liste korrigieren:\n' + details)


def make_default_prefix() -> str:
    return datetime.now().strftime("%y%m%d")


def safe_date_code(value: str, default: Optional[str] = None) -> str:
    fallback = default or make_default_prefix()
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", value.strip())
    return cleaned or fallback


ASSET_FILE_PREFIX = "ai_"


def prefixed_asset_name(asset_name: str) -> str:
    """Return a stable asset base name with the configured file prefix."""
    cleaned = safe_filename(asset_name, default="asset")
    if cleaned.lower().startswith(ASSET_FILE_PREFIX):
        return cleaned
    return f"{ASSET_FILE_PREFIX}{cleaned}"


def asset_code(prefix: str, asset_name: str) -> str:
    return f"{prefixed_asset_name(asset_name)}_{safe_date_code(prefix)}"


def maps_folder_for_target(max_target_folder: Path, asset_name: str) -> Path:
    """Der Maps-Unterordner liegt direkt im Max-Zielordner und trägt den Asset-Namen."""
    return max_target_folder / f"{prefixed_asset_name(asset_name)}_maps"


def max_file_path_for_target(max_target_folder: Path, prefix: str, asset_name: str) -> Path:
    return max_target_folder / f"{asset_code(prefix, asset_name)}.max"


def path_is_empty(path: Path) -> bool:
    return str(path).strip() in {"", "."}


def normalized_compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def tokens_from_stem(path: Path) -> List[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", path.stem.lower()) if token]


def keyword_score(path: Path, map_type: str) -> int:
    tokens = set(tokens_from_stem(path))
    compact = normalized_compact(path.stem)
    score = 0
    for keyword in MAP_KEYWORDS[map_type]:
        key_compact = normalized_compact(keyword)
        if not key_compact:
            continue
        if key_compact in tokens:
            score = max(score, 100)
        elif key_compact == compact:
            score = max(score, 95)
        elif len(key_compact) > 3 and key_compact in compact:
            score = max(score, 75)
        elif len(key_compact) <= 3 and key_compact in tokens:
            score = max(score, 60)
    return score


def classify_texture(path: Path) -> Optional[Tuple[str, int]]:
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None

    for priority_index, map_type in enumerate(MAP_PRIORITY):
        score = keyword_score(path, map_type)
        if score:
            return map_type, score + (len(MAP_PRIORITY) - priority_index)
    return None


def parse_png_info(data: bytes, offset: int = 0, name: str = "image.png") -> Optional[PngInfo]:
    if data[offset : offset + len(PNG_SIGNATURE)] != PNG_SIGNATURE:
        return None

    pos = offset + len(PNG_SIGNATURE)
    info = PngInfo(name=name, offset=offset, end=offset)
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8].decode("ascii", "replace")
        chunk_data_start = pos + 8
        chunk_data_end = chunk_data_start + length
        chunk_end = chunk_data_end + 4
        if chunk_end > len(data):
            return None
        chunk_data = data[chunk_data_start:chunk_data_end]

        if chunk_type == "IHDR" and length >= 13:
            info.width, info.height, info.bit_depth, info.color_type = struct.unpack(">IIBB", chunk_data[:10])
        elif chunk_type == "sRGB" and length == 1:
            info.srgb = chunk_data[0]
        elif chunk_type == "gAMA" and length == 4:
            info.gamma = struct.unpack(">I", chunk_data)[0] / 100000.0
        elif chunk_type == "iCCP":
            info.icc_profile = chunk_data.split(b"\x00", 1)[0].decode("latin1", "replace")

        pos = chunk_end
        if chunk_type == "IEND":
            info.end = pos
            return info
    return None


def read_png_info(path: Path) -> Optional[PngInfo]:
    if path.suffix.lower() != ".png" or not path.exists():
        return None
    try:
        data = path.read_bytes()
    except Exception:
        return None
    return parse_png_info(data, 0, path.name)


def embedded_texture_names(data: bytes) -> List[Tuple[int, str]]:
    pattern = rb"([A-Za-z0-9_./\\: -]+\.(?:png|jpg|jpeg|tif|tiff|tga|exr|bmp))"
    names: List[Tuple[int, str]] = []
    for match in re.finditer(pattern, data, re.IGNORECASE):
        raw_name = match.group(1).decode("latin1", "replace").strip("\x00 ")
        if raw_name:
            names.append((match.start(), raw_name))
    return names


def clean_embedded_texture_name(name: str, fallback: str) -> str:
    base = name.replace("\\", "/").split("/")[-1].strip()
    base = safe_filename(base, default=fallback)
    if not Path(base).suffix:
        base = f"{base}.png"
    return base


def inspect_embedded_pngs(fbx_path: Path) -> List[PngInfo]:
    if not fbx_path.exists() or fbx_path.suffix.lower() != ".fbx":
        return []
    objects, _ = inspect_fbx(fbx_path)
    infos: List[PngInfo] = []
    for key, video in objects.items():
        if video.name != 'Video':
            continue
        data = video.value('Content', b'')
        raw_name = str(video.value('RelativeFilename', video.value('Filename', object_name(video.props[1]))))
        name = clean_embedded_texture_name(raw_name, f'video_{key}.png')
        info = parse_png_info(data, 0, name)
        if info is not None:
            infos.append(info)
    return infos


def extract_embedded_pngs(fbx_path: Path, target_folder: Path, report: Report) -> List[Path]:
    if not fbx_path.exists() or fbx_path.suffix.lower() != ".fbx":
        return []

    objects, _ = inspect_fbx(fbx_path)
    paths: List[Path] = []
    target_folder.mkdir(parents=True, exist_ok=True)

    for key, video in objects.items():
        if video.name != 'Video':
            continue
        data = video.value('Content', b'')
        raw_name = str(video.value('RelativeFilename', video.value('Filename', object_name(video.props[1]))))
        name = clean_embedded_texture_name(raw_name, f'video_{key}.png')
        info = parse_png_info(data, 0, name)
        if info is None:
            continue
        target = ensure_unique_path(target_folder / info.name)
        target.write_bytes(data[info.offset : info.end])
        paths.append(target)

    if paths:
        report.section("Eingebettete FBX-Texturen")
        report.add(f"{len(paths)} eingebettete PNG-Textur(en) für Scan/Kopie extrahiert.")
        for path in paths:
            report.add(str(path.name))
    return paths


def scan_specific_paths(paths: Iterable[Path]) -> ScanResult:
    result = ScanResult()
    for path in sorted({p.resolve() for p in paths if p.exists()}, key=lambda p: str(p).lower()):
        classified = classify_texture(path)
        if classified is None:
            result.ignored.append(path)
            continue
        map_type, score = classified
        result.by_type.setdefault(map_type, []).append(MapHit(map_type, path, score))
    for map_type, hits in sorted(result.by_type.items()):
        if len(hits) > 1:
            chosen = result.selected().get(map_type)
            chosen_name = chosen.source_path.name if chosen else "keine"
            result.warnings.append(
                f"Mehrere Maps für {map_label(map_type)} gefunden ({len(hits)}). Verwendet wird: {chosen_name}"
            )
    return result


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = parent / f"{stem}_{index:02d}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Es konnte kein eindeutiger Dateipfad erzeugt werden: {path}")


def normalized_texture_sizes(values: Sequence[int]) -> Tuple[int, ...]:
    sizes = sorted({int(value) for value in values if int(value) in TEXTURE_SIZE_OPTIONS}, reverse=True)
    return tuple(sizes)


def texture_pixel_size(path: Path) -> Optional[Tuple[int, int]]:
    info = read_png_info(path)
    if info and info.width and info.height:
        return info.width, info.height

    try:
        from PIL import Image  # type: ignore

        with Image.open(str(path)) as image:
            return image.size
    except Exception:
        pass

    if QtGui is not None:
        image = QtGui.QImage(str(path))
        if not image.isNull():
            return int(image.width()), int(image.height())
    return None


def _pillow_resize_texture(source: Path, target: Path, size: int, map_type: str) -> Optional[str]:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None

    with Image.open(str(source)) as image:
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
        resized = image.resize((size, size), resampling)
        normal_renormalized = False

        if map_type == "normal":
            try:
                import numpy as np  # type: ignore

                rgb = resized.convert("RGB")
                arr = np.asarray(rgb).astype("float32") / 255.0
                normal = arr * 2.0 - 1.0
                length = np.linalg.norm(normal, axis=2, keepdims=True)
                normal = normal / np.maximum(length, 1e-6)
                arr = ((normal * 0.5 + 0.5) * 255.0).clip(0, 255).astype("uint8")
                resized = Image.fromarray(arr, "RGB")
                normal_renormalized = True
            except Exception:
                resized = resized.convert("RGB")

        save_kwargs: Dict[str, Any] = {}
        if target.suffix.lower() in {".jpg", ".jpeg"}:
            save_kwargs.update({"quality": 95, "subsampling": 0})
        elif target.suffix.lower() == ".png":
            save_kwargs.update({"compress_level": 6})
        resized.save(str(target), **save_kwargs)
    if map_type == "normal" and normal_renormalized:
        return "Pillow/Lanczos + Normal-Renorm"
    return "Pillow/Lanczos"


def _qt_image_byte_count(image: Any) -> int:
    if hasattr(image, "sizeInBytes"):
        return int(image.sizeInBytes())
    return int(image.byteCount())


def _qt_as_rgb_image(image: Any) -> Any:
    if QtGui is None:
        return image
    rgb_format = getattr(QtGui.QImage, "Format_RGB888", None)
    if rgb_format is None:
        return image
    try:
        return image.convertToFormat(rgb_format)
    except Exception:
        return image


def _renormalize_qt_normal_image(image: Any) -> Tuple[Any, bool]:
    if QtGui is None:
        return image, False

    rgba_format = getattr(QtGui.QImage, "Format_RGBA8888", None)
    if rgba_format is not None:
        rgba = image.convertToFormat(rgba_format)
        try:
            byte_count = _qt_image_byte_count(rgba)
            ptr = rgba.bits()
            if hasattr(ptr, "setsize"):
                ptr.setsize(byte_count)
            buffer = memoryview(ptr)
            if len(buffer) >= byte_count:
                for offset in range(0, byte_count, 4):
                    nx = (buffer[offset] / 255.0) * 2.0 - 1.0
                    ny = (buffer[offset + 1] / 255.0) * 2.0 - 1.0
                    nz = (buffer[offset + 2] / 255.0) * 2.0 - 1.0
                    length = max((nx * nx + ny * ny + nz * nz) ** 0.5, 1e-6)
                    buffer[offset] = int(max(0, min(255, round(((nx / length) * 0.5 + 0.5) * 255.0))))
                    buffer[offset + 1] = int(max(0, min(255, round(((ny / length) * 0.5 + 0.5) * 255.0))))
                    buffer[offset + 2] = int(max(0, min(255, round(((nz / length) * 0.5 + 0.5) * 255.0))))
                return _qt_as_rgb_image(rgba), True
        except Exception:
            pass

    try:
        rgba = image.convertToFormat(QtGui.QImage.Format_ARGB32)
        for y in range(rgba.height()):
            for x in range(rgba.width()):
                color = rgba.pixelColor(x, y)
                nx = (color.red() / 255.0) * 2.0 - 1.0
                ny = (color.green() / 255.0) * 2.0 - 1.0
                nz = (color.blue() / 255.0) * 2.0 - 1.0
                length = max((nx * nx + ny * ny + nz * nz) ** 0.5, 1e-6)
                color.setRed(int(max(0, min(255, round(((nx / length) * 0.5 + 0.5) * 255.0)))))
                color.setGreen(int(max(0, min(255, round(((ny / length) * 0.5 + 0.5) * 255.0)))))
                color.setBlue(int(max(0, min(255, round(((nz / length) * 0.5 + 0.5) * 255.0)))))
                rgba.setPixelColor(x, y, color)
        return _qt_as_rgb_image(rgba), True
    except Exception:
        return image, False


def _qt_resize_texture(source: Path, target: Path, size: int, map_type: str) -> Optional[str]:
    if QtCore is None or QtGui is None:
        return None
    image = QtGui.QImage(str(source))
    if image.isNull():
        return None
    scaled = image.scaled(size, size, QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
    backend = "Qt Smooth"
    if map_type == "normal":
        scaled, normal_renormalized = _renormalize_qt_normal_image(scaled)
        if normal_renormalized:
            backend += " + Normal-Renorm"
    if not scaled.save(str(target)):
        return None
    return backend


def resize_texture_file(source: Path, target: Path, size: int, map_type: str, report: Report) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backend = _pillow_resize_texture(source, target, size, map_type)
    if backend:
        report.add(f"{map_label(map_type)}: {source.name} -> {target.name} ({backend})")
        if map_type == "normal" and "Normal-Renorm" not in backend:
            report.warning("Normalmap wurde skaliert, aber ohne Normalvektor-Renormalisierung.")
        return
    backend = _qt_resize_texture(source, target, size, map_type)
    if backend:
        report.add(f"{map_label(map_type)}: {source.name} -> {target.name} ({backend})")
        if map_type == "normal":
            if "Normal-Renorm" not in backend:
                report.warning("Normalmap wurde skaliert, aber ohne Normalvektor-Renormalisierung.")
        return
    raise RuntimeError(f"Textur konnte nicht skaliert werden: {source} -> {target}")


def usable_downscale_sizes(source: Path, requested_sizes: Sequence[int], map_type: str, report: Report) -> Tuple[int, ...]:
    sizes = normalized_texture_sizes(requested_sizes)
    if not sizes:
        return ()
    pixel_size = texture_pixel_size(source)
    if pixel_size is None:
        report.warning(f"{map_label(map_type)}: Quellgröße konnte nicht gelesen werden; gewählte Größen werden ohne Upscale-Prüfung erzeugt.")
        return sizes
    width, height = pixel_size
    source_limit = max(width, height)
    usable = tuple(size for size in sizes if size <= source_limit)
    skipped = [size for size in sizes if size > source_limit]
    if skipped:
        report.warning(
            f"{map_label(map_type)}: {source.name} ist {width}x{height}; "
            f"überspringe {', '.join(f'{size}px' for size in skipped)}, damit nicht hochskaliert wird."
        )
    return usable


def copy_maps(
    selected_maps: Dict[str, MapHit],
    texture_folder: Path,
    prefix: str,
    asset_name: str,
    rename_texture_files: bool,
    texture_sizes: Sequence[int],
    report: Report,
) -> Dict[str, CopiedMap]:
    texture_folder.mkdir(parents=True, exist_ok=True)
    copied: Dict[str, CopiedMap] = {}
    code = asset_code(prefix, asset_name)
    sizes = normalized_texture_sizes(texture_sizes)

    report.section("Maps kopieren / umbenennen")
    if sizes:
        report.add("Downscale-Varianten: " + ", ".join(f"{size}px" for size in sizes))
        report.add("Material verwendet pro Map die größte tatsächlich erzeugte Variante.")
    for map_type in MAP_PRIORITY:
        hit = selected_maps.get(map_type)
        if hit is None:
            continue

        suffix = hit.source_path.suffix.lower()
        map_suffix = MAP_SUFFIX.get(map_type, map_type)
        if sizes:
            map_sizes = usable_downscale_sizes(hit.source_path, sizes, map_type, report)
            material_size = max(map_sizes) if map_sizes else None
            for size in map_sizes:
                if rename_texture_files:
                    target = texture_folder / f"{code}_{size}px_{map_suffix}{suffix}"
                else:
                    target = texture_folder / f"{hit.source_path.stem}_{size}px{suffix}"
                target = ensure_unique_path(target)
                resize_texture_file(hit.source_path, target, size, map_type, report)
                if size == material_size:
                    copied[map_type] = CopiedMap(map_type, hit.source_path, target)
            if map_sizes:
                continue
            report.warning(f"{map_label(map_type)}: keine Downscale-Variante erzeugt; Originalgröße wird kopiert.")
            if rename_texture_files:
                target = texture_folder / f"{code}_{map_suffix}{suffix}"
            else:
                target = texture_folder / hit.source_path.name
            target = ensure_unique_path(target)
            shutil.copy2(str(hit.source_path), str(target))
            if not target.exists():
                raise RuntimeError(f"Kopiervorgang fehlgeschlagen: {hit.source_path} -> {target}")
            copied[map_type] = CopiedMap(map_type, hit.source_path, target)
            report.add(f"{map_label(map_type)}: {hit.source_path.name} -> {target.name} (Originalgröße)")
            continue

        if rename_texture_files:
            target = texture_folder / f"{code}_{map_suffix}{suffix}"
        else:
            target = texture_folder / hit.source_path.name
        target = ensure_unique_path(target)

        if hit.source_path.resolve() == target.resolve():
            copied[map_type] = CopiedMap(map_type, hit.source_path, target)
            report.add(f"{map_label(map_type)}: bereits im Zielordner -> {target}")
            continue

        shutil.copy2(str(hit.source_path), str(target))
        if not target.exists():
            raise RuntimeError(f"Kopiervorgang fehlgeschlagen: {hit.source_path} -> {target}")
        copied[map_type] = CopiedMap(map_type, hit.source_path, target)
        report.add(f"{map_label(map_type)}: {hit.source_path.name} -> {target.name}")

    if "packed_metal_roughness" in copied:
        raise RuntimeError('Gepackte Map muss vor dem Materialaufbau getrennt werden.')

    return copied

def save_max_file_to_target_folder(options, report, nodes=None):
    if not options.save_max_file:
        return None
    require_3dsmax()
    if not nodes:
        raise RuntimeError('Keine Asset-Nodes zum Speichern vorhanden.')
    target = ensure_unique_path(max_file_path_for_target(options.asset_folder, options.prefix, options.asset_name))
    result = rt.saveNodes(rt.Array(*nodes), str(target), quiet=True)
    if result is False or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError('saveNodes hat keine gültige MAX-Datei erzeugt: ' + str(target))
    report.add('Nur Asset-Nodes gespeichert: ' + str(target))
    return target


def require_3dsmax() -> None:
    if rt is None:
        raise RuntimeError("Diese Funktion muss innerhalb von 3ds Max mit verfügbarem pymxs ausgeführt werden.")


def mx_name(value: str) -> Any:
    require_3dsmax()
    for attr in ("Name", "name"):
        try:
            factory = getattr(rt, attr)
            return factory(value)
        except Exception:
            pass
    return value


def strip_prop_name(value: Any) -> str:
    text = str(value)
    if text.startswith("#"):
        text = text[1:]
    return text.strip("'\"")


def prop_names(obj: Any) -> List[str]:
    require_3dsmax()
    try:
        return [strip_prop_name(name) for name in list(rt.getPropNames(obj))]
    except Exception:
        return []


def normalize_prop(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower().lstrip("#"))


def find_property(obj: Any, candidates: Sequence[str]) -> Optional[str]:
    names = prop_names(obj)
    if not names:
        return None
    index = {normalize_prop(name): name for name in names}

    for candidate in candidates:
        match = index.get(normalize_prop(candidate))
        if match:
            return match

    # Slot names must match exactly; e.g. "roughness" must not select diffuse_roughness.
    return None


def set_property(obj: Any, prop: str, value: Any) -> bool:
    require_3dsmax()
    errors: List[Exception] = []
    for prop_value in (mx_name(prop), prop):
        try:
            rt.setProperty(obj, prop_value, value)
            return True
        except Exception as exc:
            errors.append(exc)
    try:
        setattr(obj, prop, value)
        return True
    except Exception as exc:
        errors.append(exc)
    return False


def get_property(obj: Any, prop: str) -> Any:
    require_3dsmax()
    for prop_value in (mx_name(prop), prop):
        try:
            return rt.getProperty(obj, prop_value)
        except Exception:
            pass
    try:
        return getattr(obj, prop)
    except Exception:
        return None


def try_set(
    obj: Any,
    candidates: Sequence[str],
    value: Any,
    report: Report,
    label: str,
    warn: bool = True,
) -> Optional[str]:
    prop = find_property(obj, candidates)
    if prop is None:
        if warn:
            report.warning(f"Keine passende Eigenschaft für {label} gefunden. Kandidaten: {', '.join(candidates)}")
        return None
    if set_property(obj, prop, value):
        report.add(f"{label}: {prop} gesetzt")
        return prop
    if warn:
        report.warning(f"Eigenschaft {prop} für {label} wurde gefunden, konnte aber nicht gesetzt werden.")
    return None


def try_set_any_value(
    obj: Any,
    candidates: Sequence[str],
    values: Sequence[Any],
    report: Report,
    label: str,
    warn: bool = False,
) -> Optional[str]:
    prop = find_property(obj, candidates)
    if prop is None:
        if warn:
            report.warning(f"Keine passende Eigenschaft für {label} gefunden. Kandidaten: {', '.join(candidates)}")
        return None
    for value in values:
        try:
            if set_property(obj, prop, value):
                report.add(f"{label}: {prop} = {value} gesetzt")
                return prop
        except Exception:
            pass
    if warn:
        report.warning(f"Eigenschaft {prop} für {label} wurde gefunden, jedoch konnte kein Wert gesetzt werden.")
    return None


def set_node_name(obj: Any, name: str) -> None:
    try:
        obj.name = name
    except Exception:
        try:
            set_property(obj, "name", name)
        except Exception:
            pass


def create_max_object(class_names: Sequence[str], report: Report, label: str) -> Optional[Any]:
    require_3dsmax()
    for class_name in class_names:
        try:
            cls = getattr(rt, class_name)
        except Exception:
            continue
        try:
            obj = cls()
            report.add(f"{label}: {class_name} erstellt")
            return obj
        except Exception as exc:
            report.warning(f"{label}: {class_name} ist vorhanden, konnte aber nicht erstellt werden: {exc}")
    report.warning(f"{label}: Keine dieser Klassen konnte erstellt werden: {', '.join(class_names)}")
    return None


def make_bitmap_texture(copied_map: CopiedMap, options: ImportOptions, report: Report) -> Optional[Any]:
    class_names = ["VRayBitmap"] if options.create_vray_bitmap_nodes else []
    class_names.extend(["Bitmaptexture", "BitmapTexture"])
    bitmap = create_max_object(class_names, report, f"Bitmap {map_label(copied_map.map_type)}")
    if bitmap is None:
        return None

    tex_name = f"TEX_{asset_code(options.prefix, options.asset_name)}_{MAP_SUFFIX.get(copied_map.map_type, copied_map.map_type)}"
    set_node_name(bitmap, tex_name)

    file_candidates = [
        "filename",
        "fileName",
        "file",
        "mapFile",
        "bitmap",
        "bitmapFile",
        "BitmapFile",
        "HDRIMapName",
        "hdrimapname",
        "imageFile",
        "textureFile",
    ]
    try_set(bitmap, file_candidates, str(copied_map.target_path), report, f"{map_label(copied_map.map_type)} Dateipfad")
    set_bitmap_color_space(bitmap, copied_map.map_type, report)
    return bitmap




def bitmap_mapping_source_property(bitmap: Any) -> Optional[str]:
    return find_property(bitmap, VRAY_BITMAP_MAPPING_SOURCE_PROPS)


def link_vraybitmap_mapping_sources(bitmaps: Dict[str, Any], report: Report) -> None:
    """BaseColor ist der Mapping-Source-Master; technische PBR-Maps folgen ihm."""
    base = bitmaps.get("basecolor")
    if base is None:
        report.section("VRayBitmap Mapping Source")
        report.warning("Keine BaseColor-Bitmap vorhanden. Mapping-Source-Verknüpfung wurde übersprungen.")
        return

    report.section("VRayBitmap Mapping Source")
    report.add("BaseColor dient als Mapping-Source-Master. Normal, Roughness und Metalness werden als Slaves verbunden.")
    for map_type in ["normal", "roughness", "metalness"]:
        slave = bitmaps.get(map_type)
        if slave is None:
            report.add(f"{map_label(map_type)}: nicht vorhanden, keine Mapping-Source-Verknüpfung nötig.")
            continue
        prop = bitmap_mapping_source_property(slave)
        if prop is None:
            likely = [name for name in prop_names(slave) if "map" in normalize_prop(name) or "uvw" in normalize_prop(name) or "source" in normalize_prop(name)]
            report.warning(
                f"{map_label(map_type)}: Mapping-Source-Eingang wurde nicht gefunden. "
                f"Kandidaten im Node: {', '.join(likely[:20]) if likely else 'keine passenden Property-Namen gefunden'}"
            )
            continue
        if set_property(slave, prop, base):
            try_set(slave, ['mapSource_on'], True, report, 'Mapping Source aktivieren', warn=False)
            report.add(f"{map_label(map_type)}: {prop} -> BaseColor-Bitmap")
        else:
            report.warning(f"{map_label(map_type)}: Property {prop} gefunden, Verbindung zur BaseColor-Bitmap ist aber fehlgeschlagen.")



def try_set_bitmap_enum_verified(
    obj: Any,
    candidates: Sequence[str],
    values: Sequence[Any],
    expected_numbers: Sequence[int],
    expected_words: Sequence[str],
    report: Report,
    label: str,
) -> Optional[str]:
    prop = find_property(obj, candidates)
    if prop is None:
        report.warning(f"Keine passende Eigenschaft für {label} gefunden. Kandidaten: {', '.join(candidates)}")
        return None

    last_value: Any = None
    for value in values:
        if not set_property(obj, prop, value):
            continue
        last_value = get_property(obj, prop)
        if bitmap_value_matches(last_value, expected_numbers, expected_words):
            report.add(f"{label}: {prop} = {last_value} bestätigt; gesetzt über {value}")
            return prop

    if last_value is None:
        last_value = get_property(obj, prop)
    report.warning(
        f"{label}: {prop} konnte nach dem Setzen nicht bestätigt werden. "
        f"Soll: {list(expected_numbers)} / {', '.join(expected_words)}; Ist: {last_value}."
    )
    return prop

def set_bitmap_color_space(bitmap: Any, map_type: str, report: Report) -> None:
    transfer_number, transfer_words, primaries_number, primaries_words, policy_label = expected_bitmap_color_settings(map_type)
    is_color = map_type in COLOR_MAP_TYPES

    gamma_values = [2.2] if is_color else [1.0]
    gamma_candidates = ["gamma", "bitmapGamma", "inputGamma", "fileGamma"]
    try_set_any_value(
        bitmap,
        gamma_candidates,
        gamma_values,
        report,
        f"{map_label(map_type)} Gamma",
        warn=False,
    )

    # Wichtig: V-Ray-Enums numerisch setzen. Stringwerte können in einigen
    # Versionen MAXScript-Konvertierungsfehler auslösen.
    transfer_values: List[Any] = [transfer_number]
    try_set_bitmap_enum_verified(
        bitmap,
        VRAY_BITMAP_TRANSFER_PROPS,
        transfer_values,
        [transfer_number],
        transfer_words,
        report,
        f"{map_label(map_type)} Transferfunktion ({policy_label})",
    )

    primaries_values: List[Any] = [primaries_number]
    try_set_bitmap_enum_verified(
        bitmap,
        VRAY_BITMAP_PRIMARIES_PROPS,
        primaries_values,
        [primaries_number],
        primaries_words,
        report,
        f"{map_label(map_type)} RGB-Primärfarben ({policy_label})",
    )
    report_bitmap_color_space_actual(bitmap, map_type, report)


def compact_value(value: Any) -> str:
    return normalize_prop(str(value))


def bitmap_value_matches(value: Any, expected_numbers: Sequence[int], expected_words: Sequence[str]) -> bool:
    if value is None:
        return False
    try:
        if int(value) in expected_numbers:
            return True
    except Exception:
        pass
    normalized_value = compact_value(value)
    return any(normalize_prop(word) in normalized_value for word in expected_words)


def report_bitmap_color_space_actual(bitmap: Any, map_type: str, report: Report) -> None:
    transfer_number, transfer_words, primaries_number, primaries_words, policy_label = expected_bitmap_color_settings(map_type)
    transfer_prop = find_property(bitmap, VRAY_BITMAP_TRANSFER_PROPS)
    primaries_prop = find_property(bitmap, VRAY_BITMAP_PRIMARIES_PROPS)
    transfer_value = get_property(bitmap, transfer_prop) if transfer_prop else None
    primaries_value = get_property(bitmap, primaries_prop) if primaries_prop else None

    report.add(
        f"{map_label(map_type)} aktuelle VRayBitmap-Farbeinstellungen: "
        f"{transfer_prop or 'Transfer?'}={transfer_value} (Soll: {transfer_number}/{transfer_words[0]}), "
        f"{primaries_prop or 'Primärfarben?'}={primaries_value} (Soll: {primaries_number}/{primaries_words[0]}); "
        f"Interpretation: {policy_label}"
    )

    if transfer_prop and not bitmap_value_matches(transfer_value, [transfer_number], transfer_words):
        report.warning(
            f"Die Transferfunktion für {map_label(map_type)} ist nicht eindeutig bestätigt. "
            f"Sollwert ist {transfer_number}/{transfer_words[0]}; Istwert ist {transfer_value}. "
            "Wenn hier Auto/Default stehen bleibt, muss die Property-Belegung über die Diagnose geprüft werden."
        )
    if primaries_prop and not bitmap_value_matches(primaries_value, [primaries_number], primaries_words):
        report.warning(
            f"Die RGB-Primärfarben für {map_label(map_type)} sind nicht eindeutig bestätigt. "
            f"Sollwert ist {primaries_number}/{primaries_words[0]}; Istwert ist {primaries_value}. "
            "Default kann das Farbmanagement der Szene übernehmen und ist für technische Maps nicht eindeutig reproduzierbar."
        )


def make_normal_node(bitmap: Any, options: ImportOptions, report: Report) -> Optional[Any]:
    normal_node = create_max_object(
        ["VRayNormalMap", "VRayNormalMapTex", "Normal_Bump", "NormalBump"],
        report,
        "Normalmap-Node",
    )
    if normal_node is None:
        report.warning("Kein V-Ray-/Max-Normalmap-Node gefunden. Die Normalmap wird nicht direkt verbunden.")
        return None

    set_node_name(normal_node, f"TEX_{asset_code(options.prefix, options.asset_name)}_normal_NODE")
    try_set(
        normal_node,
        [
            "texmap",
            "texture",
            "normal_map",
            "normalMap",
            "map",
            "bitmap",
            "bump_texmap",
            "normal_texmap",
        ],
        bitmap,
        report,
        "Normalmap-Bitmap in Normalmap-Node",
    )

    try_set_any_value(
        normal_node,
        [
            "flip_green",
            "flipGreen",
            "green_flip",
            "flip_g",
            "flipG",
            "invert_green",
            "invertGreen",
            "normal_y_flip",
        ],
        [False, 0],
        report,
        "Tencent-Normalmap-Konvention: OpenGL/Y+, grüner Kanal bleibt unverändert",
        warn=False,
    )
    try_set(normal_node, ['apply_gamma'], False, report, 'Normalmap: zusätzliche Gamma-Korrektur aus', warn=False)
    try_set(normal_node, ['normal_map_multiplier'], 1.0, report, 'Normalmap-Stärke', warn=False)
    return normal_node


def enable_map_slot(obj: Any, candidates: Sequence[str], report: Report, label: str) -> None:
    try_set_any_value(obj, candidates, [True, 1], report, label, warn=False)


def configure_tencent_vray_mtl_defaults(material: Any, report: Report) -> None:
    """Best-Effort-Standardwerte für Tencent-PBR-Materialien im Metallic/Roughness-Workflow."""
    try_set_any_value(
        material,
        ["diffuse", "diffuse_color", "diffuseColor", "base_color", "baseColor"],
        [rt.Color(255, 255, 255)],
        report,
        "Standardwert Diffuse/BaseColor",
        warn=False,
    )
    try_set_any_value(
        material,
        ["reflect", "reflection", "reflection_color", "reflectionColor", "refl_color"],
        [rt.Color(255, 255, 255), 1.0],
        report,
        "Reflexionsfarbe",
        warn=False,
    )
    try_set_any_value(
        material,
        ["use_roughness", "useRoughness", "brdf_useRoughness", "option_use_roughness", "reflection_use_roughness"],
        [True, 1],
        report,
        "Roughness-Workflow",
        warn=False,
    )
    try_set_any_value(
        material,
        ["reflection_fresnel", "fresnel", "use_fresnel", "fresnel_reflections", "fresnelReflections", "option_fresnel"],
        [True, 1],
        report,
        "Fresnel-Reflexionen",
        warn=False,
    )
    try_set_any_value(
        material,
        ["fresnel_ior", "fresnelIOR", "fresnelIor", "reflection_ior", "reflectionIOR"],
        [1.6],
        report,
        "Fresnel-IOR",
        warn=False,
    )
    try_set_any_value(
        material,
        ["brdf_type", "brdfType", "brdf", "reflection_brdf", "reflectionBRDF"],
        [4, 3, 2],
        report,
        "BRDF-Typ",
        warn=False,
    )
    try_set_any_value(
        material,
        ["preserve_energy", "preserveEnergy", "option_preserve_energy", "energy_preservation"],
        [1],
        report,
        "Energieerhaltung",
        warn=False,
    )
    try_set_any_value(
        material,
        ["double_sided", "doubleSided", "option_double_sided"],
        [True, 1],
        report,
        "Doppelseitig",
        warn=False,
    )
    try_set_any_value(
        material,
        ["trace_reflections", "traceReflections", "option_trace_reflections"],
        [True, 1],
        report,
        "Reflexionen berechnen",
        warn=False,
    )
    try_set_any_value(
        material,
        ["trace_refractions", "traceRefractions", "option_trace_refractions"],
        [True, 1],
        report,
        "Refraktionen berechnen",
        warn=False,
    )


def build_vray_material(copied_maps: Dict[str, CopiedMap], options: ImportOptions, report: Report) -> Any:
    require_3dsmax()
    report.section("VRayMtl erstellen")
    material = create_max_object(["VRayMtl", "VRayMtl2"], report, "Material")
    if material is None:
        raise RuntimeError("VRayMtl konnte nicht erstellt werden. Ist V-Ray installiert und geladen?")

    material_name = f"MTL_{asset_code(options.prefix, options.asset_name)}_VRay"
    set_node_name(material, material_name)
    configure_tencent_vray_mtl_defaults(material, report)

    bitmaps: Dict[str, Any] = {}
    for map_type, copied_map in copied_maps.items():
        bitmap = make_bitmap_texture(copied_map, options, report)
        if bitmap is not None:
            bitmaps[map_type] = bitmap

    link_vraybitmap_mapping_sources(bitmaps, report)

    base = bitmaps.get("basecolor")
    if base is not None:
        try_set(
            material,
            [
                "texmap_diffuse",
                "diffuse_texmap",
                "diffuseMap",
                "base_color_map",
                "baseColorMap",
                "texmap_base_color",
                "map_diffuse",
            ],
            base,
            report,
            "BaseColor-Map",
        )
        enable_map_slot(
            material,
            [
                "texmap_diffuse_on",
                "diffuse_texmap_on",
                "diffuseMap_on",
                "base_color_map_on",
                "baseColorMap_on",
                "map_diffuse_on",
            ],
            report,
            "BaseColor-Map aktivieren",
        )
    else:
        report.warning("Keine BaseColor-/Albedo-Map gefunden.")

    roughness = bitmaps.get("roughness")
    if roughness is not None:
        roughness_prop = try_set(
            material,
            [
                "texmap_reflectionRoughness",
                "texmap_reflection_roughness",
                "reflection_roughness_texmap",
                "reflectionRoughnessMap",
                "texmap_reflectionGlossiness",
                "reflectionGlossinessMap",
                "texmap_reflGlossiness",
            ],
            roughness,
            report,
            "Roughness-Map",
        )
        if roughness_prop:
            try_set_any_value(
                material,
                ["reflection_glossiness", "reflection_roughness", "reflectionRoughness"],
                [1.0],
                report,
                "Roughness-Skalarwert für Map",
                warn=False,
            )
        enable_map_slot(
            material,
            [
                "texmap_reflectionRoughness_on",
                "texmap_reflection_roughness_on",
                "reflection_roughness_texmap_on",
                "reflectionRoughnessMap_on",
                "texmap_reflectionGlossiness_on",
                "reflectionGlossinessMap_on",
                "texmap_reflGlossiness_on",
            ],
            report,
            "Roughness-Map aktivieren",
        )
    else:
        try_set_any_value(
            material,
            ["reflection_glossiness", "reflection_roughness", "reflectionRoughness"],
            [0.55],
            report,
            "Roughness-Fallback",
            warn=False,
        )
        report.warning("Keine Roughness-Map gefunden. Es wurde versucht, den Fallback-Wert 0.55 zu setzen.")

    metalness = bitmaps.get("metalness")
    if metalness is not None:
        metalness_prop = try_set(
            material,
            [
                "texmap_metalness",
                "metalness_texmap",
                "metalnessMap",
                "texmap_metallic",
                "metallic_texmap",
                "metallicMap",
            ],
            metalness,
            report,
            "Metalness-Map",
        )
        if metalness_prop:
            try_set_any_value(
                material,
                ["reflection_metalness", "metalness", "metallic"],
                [1.0],
                report,
                "Metalness-Skalarwert für Map",
                warn=False,
            )
        enable_map_slot(
            material,
            [
                "texmap_metalness_on",
                "metalness_texmap_on",
                "metalnessMap_on",
                "texmap_metallic_on",
                "metallic_texmap_on",
                "metallicMap_on",
            ],
            report,
            "Metalness-Map aktivieren",
        )
    else:
        try_set_any_value(
            material,
            ["reflection_metalness", "metalness", "metallic"],
            [0.0],
            report,
            "Metalness-Fallback",
            warn=False,
        )
        report.warning("Keine Metalness-Map gefunden. Es wurde versucht, den Fallback-Wert 0.0 zu setzen.")

    normal_bitmap = bitmaps.get("normal")
    if normal_bitmap is not None:
        try_set_any_value(
            material,
            ["texmap_bump_multiplier", "bump_amount", "bumpAmount", "bump_multiplier", "bumpMultiplier", "bump_mult", "bumpMult"],
            [100.0],
            report,
            "Normal-/Bump-Stärke",
            warn=False,
        )
        normal_node = make_normal_node(normal_bitmap, options, report)
        if normal_node is not None:
            try_set(
                material,
                ["texmap_bump", "bump_texmap", "bumpMap", "normal_texmap", "normalMap", "texmap_normal"],
                normal_node,
                report,
                "Normal-/Bump-Map",
            )
            enable_map_slot(
                material,
                [
                    "texmap_bump_on",
                    "bump_texmap_on",
                    "bumpMap_on",
                    "normal_texmap_on",
                    "normalMap_on",
                    "texmap_normal_on",
                ],
                report,
                "Normal-/Bump-Map aktivieren",
            )
    else:
        report.warning("Keine Normalmap gefunden.")

    emission = bitmaps.get("emission")
    if emission is not None:
        try_set(
            material,
            [
                "texmap_self_illumination",
                "self_illumination_texmap",
                "selfIllumMap",
                "texmap_selfIllumination",
                "texmap_emission",
                "emission_texmap",
            ],
            emission,
            report,
            "Emission-Map",
            warn=False,
        )

    if "ao" in copied_maps:
        report.warning("AO wurde erkannt und kopiert. Die automatische Multiplikation in BaseColor ist in dieser Version nicht implementiert.")
    if "height" in copied_maps:
        report.warning("Height wurde kopiert; Displacement muss wegen der modellabhängigen Stärke manuell eingerichtet werden.")

    return material


def get_node_key(node: Any) -> str:
    if rt is not None:
        try:
            return str(int(rt.getHandleByAnim(node)))
        except Exception:
            pass
    return str(id(node))


def current_scene_objects() -> List[Any]:
    require_3dsmax()
    try:
        return list(rt.objects)
    except Exception:
        return []


def import_fbx_and_get_nodes(fbx_path: Path, report: Report) -> List[Any]:
    require_3dsmax()
    before = {get_node_key(node) for node in current_scene_objects()}
    report.section("FBX importieren")
    report.add(f"Importiere: {fbx_path}")

    rt.pluginManager.loadClass(rt.FBXIMP)
    rt.FBXImporterSetParam("PushSettings")
    try:
        rt.FBXImporterSetParam("Mode", mx_name("create"))
        if str(rt.FBXImporterGetParam("Mode")).lower() != 'create':
            if str(rt.FBXImporterGetParam("Mode")).lower() != '#create':
                raise RuntimeError('FBX-Modus Hinzufügen konnte nicht bestätigt werden.')
        for param, value in [('Animation', False), ('Cameras', False), ('Lights', False),
                             ('FillTimeline', False), ('SmoothingGroups', True)]:
            rt.FBXImporterSetParam(param, value)
        result = rt.importFile(str(fbx_path), mx_name("noPrompt"), using=rt.FBXIMP)
        if result is False:
            raise RuntimeError('FBX-Import wurde abgebrochen.')
    finally:
        rt.FBXImporterSetParam("PopSettings")

    nodes = [node for node in current_scene_objects() if get_node_key(node) not in before]
    if not nodes:
        raise RuntimeError('FBX-Import hat keine neuen Objekte erzeugt.')
    report.add(f"Neue Szenen-Nodes: {len(nodes)}")
    return nodes


def has_material_slot(node: Any) -> bool:
    try:
        getattr(node, "material")
        return True
    except Exception:
        return find_property(node, ["material"]) is not None


def assign_material(nodes: Sequence[Any], material: Any, report: Report) -> List[Any]:
    assigned: List[Any] = []
    report.section("Material zuweisen")
    for node in nodes:
        if not has_material_slot(node):
            continue
        try:
            node.material = material
            assigned.append(node)
        except Exception:
            if try_set(node, ["material"], material, report, f"Material auf {node}", warn=False):
                assigned.append(node)
    report.add(f"Material wurde {len(assigned)} Node(s) zugewiesen.")
    return assigned


def rename_nodes(nodes: Sequence[Any], options: ImportOptions, report: Report) -> None:
    report.section("Nodes umbenennen")
    base = asset_code(options.prefix, options.asset_name)
    index = 1
    for node in nodes:
        if not has_material_slot(node):
            continue
        name = f"{base}_GEO_{index:03d}"
        set_node_name(node, name)
        report.add(name)
        index += 1


def scene_layer_names() -> List[str]:
    """Liest die Layernamen aus der aktuellen 3ds-Max-Szene bestmöglich aus."""
    if rt is None:
        return []
    names: List[str] = []
    try:
        count = int(rt.LayerManager.count)
        for index in range(count):
            layer = None
            for getter in ("getLayer", "getLayerObject"):
                try:
                    layer = getattr(rt.LayerManager, getter)(index)
                    break
                except Exception:
                    layer = None
            if layer is None:
                continue
            try:
                name = str(layer.name)
            except Exception:
                name = ""
            if name and name not in names:
                names.append(name)
    except Exception:
        pass
    if "0" not in names:
        names.insert(0, "0")
    return sorted(names, key=lambda value: value.lower())


def safe_layer_name(value: str, default: str = "00_PPL") -> str:
    """Layernamen dürfen Leerzeichen enthalten; nur Steuerzeichen werden bereinigt."""
    cleaned = re.sub(r"[\r\n\t]+", " ", str(value)).strip()
    return cleaned or default


def recent_layer_names() -> List[str]:
    from tencent_layer_history import recent_layer_names as load_layers
    return load_layers()


def resolve_layer_name(options: ImportOptions) -> str:
    mode = (options.layer_mode or "preset").lower().strip()
    if mode == "existing":
        return safe_layer_name(options.layer_existing_name, default="00_PPL")
    if mode == "custom":
        if not options.layer_custom_name.strip():
            raise ValueError('Bitte einen Layer-Namen eingeben oder Layer ausschalten.')
        return safe_layer_name(options.layer_custom_name)
    return "00_PPL"


def get_or_create_layer(layer_name: str, report: Report) -> Any:
    require_3dsmax()
    layer = None
    try:
        layer = rt.LayerManager.getLayerFromName(layer_name)
    except Exception:
        layer = None
    if layer is None:
        layer = rt.LayerManager.newLayerFromName(layer_name)
        if layer is None:
            raise RuntimeError('Der Layer konnte nicht erstellt werden: ' + layer_name)
        report.add(f"Layer neu erstellt: {layer_name}")
    else:
        report.add(f"Layer verwendet: {layer_name}")
    return layer


def create_layer_for_nodes(nodes: Sequence[Any], options: ImportOptions, report: Report) -> None:
    if not options.create_layer:
        return
    require_3dsmax()
    report.section("Layer")
    layer_name = resolve_layer_name(options)
    layer = get_or_create_layer(layer_name, report)
    if str(layer.name).casefold() != layer_name.casefold():
        raise RuntimeError('Der eingegebene Layer-Name wurde nicht übernommen: ' + layer_name)
    for node in nodes:
        layer.addNode(node)
        if str(node.layer.name).casefold() != layer_name.casefold():
            raise RuntimeError('Layer-Zuweisung fehlgeschlagen: ' + str(node.name))
    report.add(f'Zugewiesene Nodes: {len(nodes)}; Layer: {layer.name}')


def group_nodes(nodes: Sequence[Any], options: ImportOptions, report: Report) -> None:
    if not options.group_imported_objects or not nodes:
        return
    require_3dsmax()
    report.section("Gruppe")
    group_name = f"GRP_{asset_code(options.prefix, options.asset_name)}"
    groupable = [node for node in nodes if has_material_slot(node)]
    if not groupable:
        report.warning("Keine gruppierbaren Geometrie-Nodes gefunden.")
        return
    try:
        rt.group(groupable, name=group_name)
        report.add(group_name)
    except Exception as exc:
        report.warning(f"Importierte Objekte konnten nicht gruppiert werden: {exc}")


def validate_options(options: ImportOptions, require_fbx: bool = True) -> None:
    if not options.save_max_file:
        raise ValueError('MAX speichern ist erforderlich: Jedes Asset wird als eigene MAX-Datei ausgegeben.')
    if require_fbx and (not options.fbx_file or not options.fbx_file.exists()):
        raise RuntimeError(f"FBX-Datei existiert nicht: {options.fbx_file}")
    if require_fbx and options.fbx_file.suffix.lower() != ".fbx":
        raise RuntimeError(f"Die ausgewählte Datei ist keine FBX-Datei: {options.fbx_file}")
    validate_asset_name(options.requested_asset_name or options.asset_name)
    if options.create_layer:
        resolve_layer_name(options)
    if not safe_date_code(options.prefix):
        raise RuntimeError("Der Datumscode ist leer oder ungültig.")
    if path_is_empty(options.asset_folder):
        raise RuntimeError("Der Max-Zielordner ist leer.")
    if path_is_empty(options.texture_folder):
        raise RuntimeError("Der abgeleitete Maps-Unterordner ist leer.")
    invalid_sizes = [size for size in options.texture_downscale_sizes if size not in TEXTURE_SIZE_OPTIONS] if options.import_textures else []
    if invalid_sizes:
        raise RuntimeError(f"Ungültige Downscale-Größe(n): {invalid_sizes}")


def report_scan_result(scan: ScanResult, report: Report) -> None:
    report.section("Scan-Ergebnis")
    selected = scan.selected()
    for map_type in MAP_PRIORITY:
        hits = scan.by_type.get(map_type, [])
        if not hits:
            report.add(f"{map_label(map_type)}: nicht gefunden")
            continue
        chosen = selected.get(map_type)
        marker = f"ausgewählt: {chosen.source_path.name}" if chosen else "keine Auswahl"
        report.add(f"{map_label(map_type)}: {len(hits)} Treffer, {marker}")
        for hit in sorted(hits, key=lambda item: str(item.source_path).lower()):
            report.add(f"  - {hit.source_path}")
    if scan.ignored:
        report.add(f"Ignorierte Bilddateien: {len(scan.ignored)}")
    for warning in scan.warnings:
        report.warning(warning)


def png_profile_summary(path: Path) -> str:
    info = read_png_info(path)
    if info is None:
        return "PNG-Profil: nicht verfügbar"
    parts = []
    if info.width and info.height:
        parts.append(f"{info.width}x{info.height}")
    if info.bit_depth is not None:
        parts.append(f"{info.bit_depth}-bit")
    if info.color_type is not None:
        parts.append(f"Farbtyp={info.color_type}")
    profile_parts = []
    if info.srgb is not None:
        profile_parts.append(f"sRGB-Chunk={info.srgb}")
    if info.gamma is not None:
        profile_parts.append(f"gAMA={info.gamma:g}")
    if info.icc_profile:
        profile_parts.append(f"iCCP={info.icc_profile}")
    if not profile_parts:
        profile_parts.append("keine eingebetteten sRGB-/gAMA-/iCCP-Daten")
    parts.append("; ".join(profile_parts))
    return ", ".join(parts)


def map_color_policy(map_type: str) -> str:
    if map_type in COLOR_MAP_TYPES:
        return "sRGB-Farbdaten; VRayBitmap Type=sRGB, RGB primaries=sRGB primaries"
    if map_type in RAW_MAP_TYPES:
        return "Raw/Linear-Technikdaten; VRayBitmap Type=None, RGB primaries=Raw"
    return "Standardmäßig Raw/Linear; VRayBitmap Type=None, RGB primaries=Raw"


def report_color_space_plan(selected_maps: Dict[str, MapHit], report: Report) -> None:
    report.section("Farbmanagement-Plan")
    if not selected_maps:
        report.warning("Keine klassifizierten Maps für die Farbmanagement-Planung verfügbar.")
        return
    for map_type in MAP_PRIORITY:
        hit = selected_maps.get(map_type)
        if hit is None:
            continue
        report.add(f"{map_label(map_type)}: {map_color_policy(map_type)}")
        report.add(f"  Datei: {hit.source_path.name}")
        if hit.source_path.suffix.lower() == ".png":
            report.add(f"  PNG: {png_profile_summary(hit.source_path)}")
        if map_type in RAW_MAP_TYPES:
            info = read_png_info(hit.source_path)
            if info and (info.srgb is not None or (info.gamma is not None and abs(info.gamma - 1.0) > 0.001)):
                report.warning(
                    f"{hit.source_path.name} enthält PNG-Farbmetadaten, {map_label(map_type)} wird jedoch auf Raw/Linear gesetzt."
                )


def report_embedded_png_probe(fbx_path: Path, report: Report) -> None:
    infos = inspect_embedded_pngs(fbx_path)
    if not infos:
        return
    report.section("Prüfung eingebetteter PNG-Texturen")
    report.add(f"{len(infos)} eingebettete PNG-Textur(en) in der FBX gefunden.")
    for info in infos:
        profile_parts = []
        if info.srgb is not None:
            profile_parts.append(f"sRGB-Chunk={info.srgb}")
        if info.gamma is not None:
            profile_parts.append(f"gAMA={info.gamma:g}")
        if info.icc_profile:
            profile_parts.append(f"iCCP={info.icc_profile}")
        profile = ", ".join(profile_parts) if profile_parts else "keine eingebetteten sRGB-/gAMA-/iCCP-Daten"
        size = f"{info.width}x{info.height}" if info.width and info.height else "unbekannte Größe"
        report.add(f"{info.name}: {size}, {profile}")


def scan_embedded_fbx_textures(fbx_path: Path, report: Report, extract_folder: Path) -> ScanResult:
    """Extract and classify only PNG textures embedded in the selected FBX."""
    report_embedded_png_probe(fbx_path, report)
    embedded_paths = extract_embedded_pngs(fbx_path, extract_folder, report)
    if not embedded_paths:
        report.warning("In der FBX wurden keine eingebetteten PNG-Texturen gefunden. Der Import wird ohne PBR-Maps fortgesetzt.")
        return ScanResult()
    return scan_specific_paths(embedded_paths)


def run_scan_only(options):
    import tencent_batch_support
    return tencent_batch_support.scan(sys.modules[__name__], options)


def run_import_and_convert(options, overwrite_resolver=None, overwrite_confirm=None, _batch_token=None,
                           preflight=None):
    import tencent_batch_support
    return tencent_batch_support.run_import(sys.modules[__name__], options,
        overwrite_resolver=overwrite_resolver, overwrite_confirm=overwrite_confirm, _batch_token=_batch_token,
        preflight=preflight)


def run_batch(options_list, progress=None, cancelled=None, scan_only=False,
              overwrite_resolver=None, overwrite_confirm=None, preflight=None):
    import tencent_import_state as state
    from tencent_preflight import PreflightRequired
    results = []
    with state.batch_session() as token:
        options_list = list(options_list)
        validate_batch_names(options_list)
        if preflight is not None and not scan_only:
            # Recheck the complete approved list before the first scene change.
            preflight.assert_current(options_list)
            overwrite_resolver = preflight.resolve
            overwrite_confirm = preflight.confirm
        for index, options in enumerate(options_list):
            if cancelled and cancelled():
                break
            if progress:
                progress(index, 'Läuft', '')
            if cancelled and cancelled():
                break
            try:
                import_kwargs = dict(overwrite_resolver=overwrite_resolver,
                                     overwrite_confirm=overwrite_confirm, _batch_token=token)
                if preflight is not None:
                    import_kwargs['preflight'] = preflight
                text = run_scan_only(options) if scan_only else run_import_and_convert(options, **import_kwargs)
                result = {'index': index, 'success': True, 'text': text}
            except PreflightRequired as exc:
                # A changed source, destination or approval must return to the
                # preflight screen, never continue with the next queued asset.
                exc.row_index = index
                raise
            except ImportCancelled as exc:
                if progress:
                    progress(index, 'Gestoppt', str(exc))
                break
            except Exception as exc:
                result = {'index': index, 'success': False, 'text': str(exc)}
            results.append(result)
            if progress:
                progress(index, 'Fertig' if result['success'] else 'Fehler', result['text'])
            if preflight is not None and not result['success']:
                preflight.invalidate()
                break
    return results


def dump_vray_properties() -> str:
    report = Report()
    require_3dsmax()
    report.section("V-Ray-Diagnose")
    for class_name in ["VRayMtl", "VRayBitmap", "VRayNormalMap", "VRayNormalMapTex", "Normal_Bump"]:
        try:
            cls = getattr(rt, class_name)
        except Exception:
            report.warning(f"{class_name}: Klasse nicht gefunden")
            continue
        try:
            obj = cls()
        except Exception as exc:
            report.warning(f"{class_name}: konnte nicht instanziiert werden: {exc}")
            continue
        props = prop_names(obj)
        report.add(f"{class_name}: {len(props)} Eigenschaften")
        if class_name.lower() == "vraybitmap":
            for label, candidates in [
                ("Dateipfad", ["filename", "fileName", "bitmapFile", "HDRIMapName", "imageFile"]),
                ("Transfer Function", VRAY_BITMAP_TRANSFER_PROPS),
                ("RGB Primaries", VRAY_BITMAP_PRIMARIES_PROPS),
                ("Mapping Source", VRAY_BITMAP_MAPPING_SOURCE_PROPS),
            ]:
                found = find_property(obj, candidates)
                report.add(f"  {label}: {found or 'nicht gefunden'}")
        elif class_name.lower() in {"vraynormalmap", "vraynormalmaptex", "normal_bump"}:
            for label, candidates in [
                ("Normal-Bitmap", ["texmap", "texture", "normal_map", "normalMap", "map", "bitmap"]),
                ("Green Flip", ["flip_green", "flipGreen", "green_flip", "invertGreen", "normal_y_flip"]),
            ]:
                found = find_property(obj, candidates)
                report.add(f"  {label}: {found or 'nicht gefunden'}")
        elif class_name.lower() == "vraymtl":
            for label, candidates in [
                ("Diffuse/BaseColor", ["texmap_diffuse", "diffuse_texmap", "base_color_map", "baseColorMap"]),
                ("Reflection Roughness", ["texmap_reflectionRoughness", "reflection_roughness_texmap", "reflectionRoughnessMap"]),
                ("Metalness", ["texmap_metalness", "metalness_texmap", "metalnessMap"]),
                ("Bump/Normal", ["texmap_bump", "bump_texmap", "bumpMap", "normal_texmap"]),
                ("Use Roughness", ["use_roughness", "useRoughness", "brdf_useRoughness"]),
            ]:
                found = find_property(obj, candidates)
                report.add(f"  {label}: {found or 'nicht gefunden'}")
    text = report.text()
    try:
        print(text)
    except Exception:
        pass
    return text


def get_max_parent_window() -> Optional[Any]:
    if QtWidgets is None:
        return None
    try:
        import qtmax  # type: ignore

        return qtmax.GetQMaxMainWindow()
    except Exception:
        return None




@dataclass
class FbxPreviewData:
    """Technische FBX-Auswertung für das UI."""
    info_lines: List[str] = field(default_factory=list)


def _unpack_numeric_array(type_code: bytes, payload: bytes) -> List[Any]:
    import struct as _struct
    if type_code == b"f":
        fmt = "<" + "f" * (len(payload) // 4)
    elif type_code == b"d":
        fmt = "<" + "d" * (len(payload) // 8)
    elif type_code == b"i":
        fmt = "<" + "i" * (len(payload) // 4)
    elif type_code == b"l":
        fmt = "<" + "q" * (len(payload) // 8)
    elif type_code == b"b":
        return list(payload)
    else:
        return []
    if not payload:
        return []
    try:
        return list(_struct.unpack(fmt, payload))
    except Exception:
        return []


def _read_fbx_property(data: bytes, pos: int) -> Tuple[Any, int]:
    import struct as _struct
    if pos >= len(data):
        return None, pos
    type_code = data[pos : pos + 1]
    pos += 1

    if type_code in {b"f", b"d", b"i", b"l", b"b"}:
        if pos + 12 > len(data):
            return None, pos
        count, encoding, comp_len = _struct.unpack_from("<III", data, pos)
        pos += 12
        raw = data[pos : pos + comp_len]
        pos += comp_len
        if encoding == 1:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                return [], pos
        return _unpack_numeric_array(type_code, raw), pos

    if type_code == b"Y":
        if pos + 2 > len(data):
            return None, pos
        value = _struct.unpack_from("<h", data, pos)[0]
        return value, pos + 2
    if type_code == b"C":
        if pos + 1 > len(data):
            return None, pos
        return bool(data[pos]), pos + 1
    if type_code == b"I":
        if pos + 4 > len(data):
            return None, pos
        value = _struct.unpack_from("<i", data, pos)[0]
        return value, pos + 4
    if type_code == b"F":
        if pos + 4 > len(data):
            return None, pos
        value = _struct.unpack_from("<f", data, pos)[0]
        return value, pos + 4
    if type_code == b"D":
        if pos + 8 > len(data):
            return None, pos
        value = _struct.unpack_from("<d", data, pos)[0]
        return value, pos + 8
    if type_code == b"L":
        if pos + 8 > len(data):
            return None, pos
        value = _struct.unpack_from("<q", data, pos)[0]
        return value, pos + 8
    if type_code in {b"S", b"R"}:
        if pos + 4 > len(data):
            return None, pos
        length = _struct.unpack_from("<I", data, pos)[0]
        pos += 4
        value = data[pos : pos + length]
        pos += length
        if type_code == b"S":
            return value.decode("utf-8", "replace"), pos
        return value, pos
    return None, pos


def _extract_binary_fbx_arrays(data: bytes) -> Tuple[List[float], List[int], List[str]]:
    import struct as _struct
    messages: List[str] = []
    vertices: List[float] = []
    indices: List[int] = []
    if not data.startswith(b"Kaydara FBX Binary"):
        return vertices, indices, ["FBX-Format: ASCII oder unbekannt. Binäre Meshdaten können nur eingeschränkt ausgewertet werden."]

    if len(data) < 27:
        return vertices, indices, ["FBX-Datei ist zu kurz oder beschädigt."]
    version = _struct.unpack_from("<I", data, 23)[0]
    use_64 = version >= 7500
    pos = 27
    messages.append(f"FBX-Binary-Version: {version}")

    def read_record_header(offset: int) -> Optional[Tuple[int, int, int, int, int]]:
        if use_64:
            size = 25
            if offset + size > len(data):
                return None
            end_offset, prop_count, prop_len = _struct.unpack_from("<QQQ", data, offset)
            name_len = data[offset + 24]
        else:
            size = 13
            if offset + size > len(data):
                return None
            end_offset, prop_count, prop_len = _struct.unpack_from("<III", data, offset)
            name_len = data[offset + 12]
        return int(end_offset), int(prop_count), int(prop_len), int(name_len), size

    def walk(offset: int, boundary: int, depth: int = 0) -> int:
        nonlocal vertices, indices
        pos_local = offset
        safety = 0
        while pos_local < boundary and pos_local < len(data) and safety < 100000:
            safety += 1
            header = read_record_header(pos_local)
            if header is None:
                break
            end_offset, prop_count, prop_len, name_len, header_size = header
            if end_offset == 0 and prop_count == 0 and prop_len == 0 and name_len == 0:
                pos_local += header_size
                break
            if end_offset <= pos_local or end_offset > len(data):
                break
            pos_local += header_size
            name_bytes = data[pos_local : pos_local + name_len]
            pos_local += name_len
            name = name_bytes.decode("ascii", "replace")
            props_start = pos_local
            props: List[Any] = []
            for _ in range(prop_count):
                value, pos_local = _read_fbx_property(data, pos_local)
                props.append(value)
                if pos_local > end_offset:
                    break
            # Schutz gegen fehlerhafte Property-Längen.
            expected_children_start = props_start + prop_len
            if expected_children_start > pos_local and expected_children_start <= end_offset:
                pos_local = expected_children_start

            if name == "Vertices" and props and isinstance(props[0], list) and not vertices:
                vertices = [float(v) for v in props[0]]
            elif name == "PolygonVertexIndex" and props and isinstance(props[0], list) and not indices:
                indices = [int(v) for v in props[0]]

            if pos_local < end_offset:
                pos_local = walk(pos_local, end_offset, depth + 1)
            pos_local = end_offset
            if vertices and indices:
                # Erstes Mesh genügt für eine schnelle technische Auswertung.
                return pos_local
        return pos_local

    walk(pos, len(data))
    return vertices, indices, messages


def _extract_ascii_fbx_arrays(text: str) -> Tuple[List[float], List[int], List[str]]:
    messages = ["FBX-Format: ASCII. Meshdaten werden aus den Textdaten gelesen."]
    vertices: List[float] = []
    indices: List[int] = []
    try:
        vertex_match = re.search(r"Vertices:\s*\*\d+\s*\{\s*a:\s*([^}]*)", text, re.S)
        if vertex_match:
            vertices = [float(v) for v in re.findall(r"[-+]?\d*\.\d+(?:[eE][-+]?\d+)?|[-+]?\d+", vertex_match.group(1))]
        index_match = re.search(r"PolygonVertexIndex:\s*\*\d+\s*\{\s*a:\s*([^}]*)", text, re.S)
        if index_match:
            indices = [int(v) for v in re.findall(r"-?\d+", index_match.group(1))]
    except Exception as exc:
        messages.append(f"ASCII-Auswertung konnte nicht vollständig gelesen werden: {exc}")
    return vertices, indices, messages


def build_fbx_preview(path: Path) -> FbxPreviewData:
    info: List[str] = []
    preview = FbxPreviewData(info_lines=info)
    if not path.exists():
        info.append(f"Datei nicht gefunden: {path}")
        return preview
    if path.suffix.lower() != ".fbx":
        info.append(f"Keine FBX-Datei: {path}")
        return preview

    info.append(f"Datei: {path.name}")
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
        info.append(f"Größe: {size_mb:.2f} MB")
        data = path.read_bytes()
    except Exception as exc:
        info.append(f"Datei konnte nicht gelesen werden: {exc}")
        return preview

    embedded = inspect_embedded_pngs(path)
    info.append(f"Eingebettete PNG-Texturen: {len(embedded)}")
    for item in embedded[:12]:
        size = f"{item.width}x{item.height}" if item.width and item.height else "unbekannte Größe"
        classified = classify_texture(Path(item.name))
        role = map_label(classified[0]) if classified else "nicht klassifiziert"
        depth = f", {item.bit_depth}-bit" if item.bit_depth is not None else ""
        color_type = f", Farbtyp {item.color_type}" if item.color_type is not None else ""
        info.append(f"  - {item.name} ({size}{depth}{color_type}) -> {role}")
    if len(embedded) > 12:
        info.append(f"  ... weitere {len(embedded) - 12}")
    if not embedded:
        info.append("Eingebettete PNG-Texturen: keine gefunden.")

    if data.startswith(b"Kaydara FBX Binary"):
        vertices, indices, messages = _extract_binary_fbx_arrays(data)
    else:
        try:
            text = data.decode("utf-8", "ignore")
        except Exception:
            text = ""
        vertices, indices, messages = _extract_ascii_fbx_arrays(text)
    info.extend(messages)

    vertex_count = len(vertices) // 3
    poly_count = sum(1 for idx in indices if int(idx) < 0)
    info.append(f"Mesh-Vertices gelesen: {vertex_count}")
    info.append(f"Polygone geschätzt: {poly_count}")

    if not vertices:
        info.append("Meshdaten: Keine auswertbaren Vertexdaten gefunden. Der eigentliche 3ds-Max-Import kann trotzdem funktionieren.")
        return preview

    triples = [(float(vertices[i]), float(vertices[i + 1]), float(vertices[i + 2])) for i in range(0, len(vertices) - 2, 3)]
    if triples:
        xs = [p[0] for p in triples]
        ys = [p[1] for p in triples]
        zs = [p[2] for p in triples]
        info.append(
            "Bounding Box aus FBX-Daten, ungeprüfte Quelleinheiten: "
            f"X {min(xs):.4g} bis {max(xs):.4g}, "
            f"Y {min(ys):.4g} bis {max(ys):.4g}, "
            f"Z {min(zs):.4g} bis {max(zs):.4g}"
        )
        info.append(
            "Abmessungen aus FBX-Daten, ungeprüfte Quelleinheiten: "
            f"X {max(xs) - min(xs):.4g}, Y {max(ys) - min(ys):.4g}, Z {max(zs) - min(zs):.4g}"
        )

    if indices:
        polygon_sizes: List[int] = []
        current = 0
        for raw in indices:
            current += 1
            if int(raw) < 0:
                polygon_sizes.append(current)
                current = 0
        tri_count = sum(1 for size in polygon_sizes if size == 3)
        quad_count = sum(1 for size in polygon_sizes if size == 4)
        ngon_count = sum(1 for size in polygon_sizes if size > 4)
        info.append(f"Polygonverteilung geschätzt: Dreiecke {tri_count}, Quads {quad_count}, N-Gons {ngon_count}")
    else:
        info.append("Polygonindizes: nicht auswertbar.")

    return preview


DialogBase = QtWidgets.QDialog if QtWidgets is not None else object


from tencent_batch_ui import make_dialog_class
ImporterDialog = make_dialog_class(sys.modules[__name__])


_DIALOG: Optional[Any] = None


def _listener_print(text: str) -> None:
    try:
        print(text)
    except Exception:
        pass


def show() -> Any:
    """Importer-Dialog in 3ds Max anzeigen."""
    if QtWidgets is None:
        raise RuntimeError("PySide2/PySide6 ist nicht verfügbar. Der Dialog kann nicht geöffnet werden.")
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)

    import tencent_import_state as state
    global _DIALOG
    existing = state.get_open_dialog(_DIALOG)
    if existing is not None:
        _DIALOG = existing
        return state.focus_dialog(existing)
    if state.is_busy():
        raise state.ImporterBusyError(state.BUSY_MESSAGE)
    _DIALOG = ImporterDialog(parent=get_max_parent_window())
    state.set_dialog(_DIALOG)
    _DIALOG.show()
    try:
        _DIALOG.raise_()
        _DIALOG.activateWindow()
    except Exception:
        pass
    return _DIALOG


def launch() -> Optional[Any]:
    """Robuster Starter für 3ds Max Run Script.

    Einige 3ds-Max-Python-Startwege führen den __main__-Block nicht zuverlässig aus.
    Deshalb wird dieser Starter am Dateiende direkt aufgerufen und schreibt immer
    eine Rückmeldung in den Listener.
    """
    _listener_print(f"{TOOL_NAME}: starte Dialog ...")
    try:
        dialog = show()
        _listener_print(f"{TOOL_NAME}: Dialog geöffnet.")
        return dialog
    except Exception:
        text = traceback.format_exc()
        _listener_print(text)
        try:
            if rt is not None:
                rt.messageBox(text, title=TOOL_NAME)
        except Exception:
            pass
        return None


# Entry points launch the dialog; importing the shared core never changes a scene.
