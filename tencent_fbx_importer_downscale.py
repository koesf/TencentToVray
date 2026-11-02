# -*- coding: utf-8 -*-
"""3ds Max Run Script entry point. Keep the sibling tencent_*.py modules together."""
import os
import sys
import importlib
from pathlib import Path
# Max can reuse __file__ from a previously running Python script. The code
# object's filename identifies this entry point even for nested ExecuteFile.
_entry_filename = sys._getframe().f_code.co_filename
if _entry_filename.startswith('<'):
    raise RuntimeError('Bitte die Datei ueber Scripting > Run Script starten.')
_folder = str(Path(_entry_filename).resolve().parent)
if _folder not in sys.path:
    sys.path.insert(0, _folder)
import tencent_import_state as _import_state
import tencent_importer_core as _core
if _core.rt is not None and os.environ.get('TENCENT_FBX_IMPORTER_NO_UI') != '1':
    _old_dialog = _import_state.get_open_dialog(getattr(_core, '_DIALOG', None))
    if _old_dialog is not None:
        _import_state.focus_dialog(_old_dialog)
    else:
        try:
            # Load updates only after closing the previous window. This state
            # module deliberately survives reloads, including direct imports.
            with _import_state.reload_session():
                for _module in ('tencent_fbx_metadata', 'tencent_max_metadata', 'tencent_asset_store', 'tencent_layer_history', 'tencent_preflight', 'tencent_mesh_preview', 'tencent_preview_widget', 'tencent_batch_ui', 'tencent_batch_support', 'tencent_importer_core'):
                    if _module in sys.modules:
                        importlib.reload(sys.modules[_module])
        except _import_state.ImporterBusyError as _exc:
            print(str(_exc))
        else:
            _core.launch()

from tencent_importer_core import *
