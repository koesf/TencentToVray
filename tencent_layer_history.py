# -*- coding: utf-8 -*-
"""Small per-user history of custom layer names; independent of 3ds Max."""
import json
import os
from pathlib import Path
import tempfile
import time

from tencent_asset_store import AssetBusyError, AssetLock


def _history_path(path=None):
    if path is not None:
        return Path(path)
    appdata = os.environ.get('LOCALAPPDATA')
    if appdata:
        return Path(appdata) / 'TencentToVray' / 'layers.json'
    return Path.home() / '.TencentToVray' / 'layers.json'


def _unique_names(names):
    result, seen = [], set()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.casefold()
        if key not in seen:
            result.append(name)
            seen.add(key)
        if len(result) == 10:
            break
    return result


def recent_layer_names(path=None):
    """Read the newest ten names, tolerating missing or damaged preferences."""
    try:
        names = json.loads(_history_path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return []
    return _unique_names(names) if isinstance(names, list) else []


def remember_layer(name, path=None):
    """Move a custom name to the front without losing concurrent Max updates."""
    if not isinstance(name, str) or not name.strip():
        return
    destination = _history_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = AssetLock(destination.with_name('.' + destination.name + '.lock'))
    deadline = time.monotonic() + 0.25
    while True:
        try:
            lock.__enter__()
            break
        except AssetBusyError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Die Layer-Historie wird gerade von einer anderen Sitzung gespeichert.')
            time.sleep(0.01)
    temporary = None
    try:
        names = _unique_names([name] + recent_layer_names(destination))
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         prefix='.layers_', suffix='.json',
                                         dir=str(destination.parent), delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(names, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(destination))
    finally:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        finally:
            lock.__exit__()
