# -*- coding: utf-8 -*-
"""Process-wide importer state. Never reload this module in the script starter.

Qt can process events while a batch runs, including another script launch or a
direct import. A batch token authorizes its own rows without permitting those
callbacks to enter the same scene operation again.
"""
from contextlib import contextmanager
import threading


BUSY_MESSAGE = (
    "Der Importer arbeitet bereits in dieser 3ds-Max-Sitzung. "
    "Bitte den laufenden Vorgang beenden lassen."
)

_state_lock = threading.Lock()
_batch_token = None
_batch_owner = None
_import_active = False
_reload_active = False
_dialog = None


class ImporterBusyError(RuntimeError):
    """A nested or concurrent operation would modify the same Max scene."""


def is_busy():
    with _state_lock:
        return _batch_token is not None or _import_active or _reload_active


@contextmanager
def batch_session():
    """Reserve the entire batch, including gaps between its individual rows."""
    global _batch_token, _batch_owner
    token = object()
    with _state_lock:
        if _batch_token is not None or _import_active or _reload_active:
            raise ImporterBusyError(BUSY_MESSAGE)
        _batch_token = token
        _batch_owner = threading.get_ident()
    try:
        yield token
    finally:
        with _state_lock:
            _batch_token = None
            _batch_owner = None


@contextmanager
def import_session(batch_token=None):
    """Reserve one import; only the owning batch may pass its private token."""
    global _import_active
    with _state_lock:
        if _import_active or _reload_active:
            raise ImporterBusyError(BUSY_MESSAGE)
        if _batch_token is not None:
            if batch_token is not _batch_token or _batch_owner != threading.get_ident():
                raise ImporterBusyError(BUSY_MESSAGE)
        elif batch_token is not None:
            raise ImporterBusyError("Der Batch-Auftrag ist nicht mehr aktiv. Bitte erneut starten.")
        _import_active = True
    try:
        yield
    finally:
        with _state_lock:
            _import_active = False


@contextmanager
def reload_session():
    """Prevent module reloads from overlapping an import or batch start."""
    global _reload_active
    with _state_lock:
        if _batch_token is not None or _import_active or _reload_active:
            raise ImporterBusyError(BUSY_MESSAGE)
        _reload_active = True
    try:
        yield
    finally:
        with _state_lock:
            _reload_active = False


def set_dialog(dialog):
    global _dialog
    with _state_lock:
        _dialog = dialog


def get_dialog():
    """Return the remembered dialog, including a closed instance if present."""
    with _state_lock:
        return _dialog


def get_open_dialog(fallback=None):
    """Find a live visible/running dialog without depending on a Qt version."""
    dialog = get_dialog()
    for candidate in (dialog, fallback):
        if candidate is None:
            continue
        try:
            # Calling into Qt also detects wrappers whose C++ object was deleted.
            visible = candidate.isVisible()
            if visible or getattr(candidate, '_running', False):
                set_dialog(candidate)
                return candidate
        except (RuntimeError, AttributeError):
            continue
    set_dialog(None)
    return None


def focus_dialog(dialog):
    """Bring the existing window forward while keeping its queue intact."""
    try:
        if dialog.isMinimized():
            dialog.showNormal()
        elif not dialog.isVisible():
            dialog.show()
        dialog.raise_()
        dialog.activateWindow()
    except (RuntimeError, AttributeError):
        pass
    return dialog
