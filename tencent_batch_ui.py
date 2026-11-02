"""Qt batch queue with independent import settings for every asset."""
from pathlib import Path


def make_dialog_class(core):
    Qt = core.QtWidgets
    if Qt is None:
        return object

    class CenteredCheckBox(Qt.QCheckBox):
        def __init__(self):
            super().__init__()
            self.setStyleSheet('QCheckBox { spacing: 0px; } QCheckBox::indicator { width: 18px; height: 18px; subcontrol-position: center; }')
            self.setCursor(core.QtCore.Qt.PointingHandCursor)

        def hitButton(self, position):
            return self.rect().contains(position)

    class AssetNameDelegate(Qt.QStyledItemDelegate):
        """Validate the text being edited, before a button can start an import."""
        def __init__(self, dialog):
            super().__init__(dialog.table)
            self.dialog = dialog
            self.editor = None
            self.index = None
            self.loading = False

        def createEditor(self, parent, option, index):
            editor = super().createEditor(parent, option, index)
            self.editor = editor
            self.index = core.QtCore.QPersistentModelIndex(index)
            editor.setToolTip(self.dialog._name_rules())
            editor.textChanged.connect(self._text_changed)
            return editor

        def paint(self, painter, option, index):
            # Keep invalid names red even when the entire row is selected.
            # The rest of the row retains the usual selection highlight.
            background = index.data(core.QtCore.Qt.BackgroundRole)
            if background is not None and background.style() != core.QtCore.Qt.NoBrush:
                option = Qt.QStyleOptionViewItem(option)
                option.state &= ~Qt.QStyle.State_Selected
            super().paint(painter, option, index)

        def setEditorData(self, editor, index):
            if self.dialog._validating_names:
                return
            self.loading = True
            try:
                super().setEditorData(editor, index)
            finally:
                self.loading = False

        def _text_changed(self, text):
            if not self.loading:
                self.dialog._validate_names()

        def pending_name(self):
            if self.editor is not None and self.index is not None and self.index.isValid():
                return self.index.row(), self.editor.text()
            return None

        def commit_active_editor(self):
            if self.editor is not None and self.index is not None and self.index.isValid():
                self.setModelData(self.editor, self.dialog.table.model(), core.QtCore.QModelIndex(self.index))

        def destroyEditor(self, editor, index):
            if editor is self.editor:
                self.editor = None
                self.index = None
            super().destroyEditor(editor, index)
            self.dialog._validate_names()

    class ImporterDialog(Qt.QDialog):
        COL_PREVIEW = 0
        COL_SOURCE = 1
        COL_NAME = 2
        COL_OPTIMIZE = 3
        COL_PERCENT = 4
        COL_TEXTURES = 5
        COL_RESOLUTION = 6
        COL_STATUS = 7

        def __init__(self, parent=None):
            super().__init__(parent)
            from tencent_preflight import PreflightRequired, PreflightSession
            self.setAttribute(core.QtCore.Qt.WA_AlwaysShowToolTips, True)
            self.setWindowTitle(f'{core.TOOL_NAME} {core.TOOL_VERSION}')
            self.resize(1420, 880)
            screen = Qt.QApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.resize(min(1420, available.width()-40), min(880, available.height()-60))
            self._running = False
            self._cancelled = False
            self._next_name_id = 0
            self._name_feedback = {}
            self._name_errors = {}
            self._validating_names = False
            self._preflight = PreflightSession(core)
            self._preflight_required = PreflightRequired
            self._preflight_records = {}
            layout = Qt.QVBoxLayout(self)
            self.controls = Qt.QWidget()
            controls = Qt.QVBoxLayout(self.controls)
            controls.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.controls)
            buttons = Qt.QHBoxLayout()
            self.add_button = Qt.QPushButton('FBX hinzufügen …')
            self.add_button.setToolTip('Eine oder mehrere FBX-Dateien zur Importliste hinzufügen. Danach für jede Datei einen einfachen Asset-Namen eingeben.')
            remove = Qt.QPushButton('Auswahl entfernen')
            clear = Qt.QPushButton('Alle entfernen')
            buttons.addWidget(self.add_button)
            buttons.addWidget(remove)
            buttons.addWidget(clear)
            buttons.addStretch()
            controls.addLayout(buttons)
            self.table = Qt.QTableWidget(0, 8)
            self.table.setHorizontalHeaderLabels(['Bild', 'FBX', 'Asset-Name', 'Optimizer', 'Rest %', 'Texturen', 'Auflösung', 'Status'])
            header_tips = [
                'Mesh-Vorschaubild dieser FBX, automatisch vor dem Benennen erzeugt. Die Geometrie wird vereinfacht und ohne Texturen dargestellt. Zeile auswählen zum Drehen; Doppelklick für eine große Ansicht. Die Szene bleibt unverändert.',
                'FBX-Quelldatei dieser Zeile. Der vollständige Pfad erscheint beim Überfahren der Datei.',
                'Pflichtfeld: einen eindeutigen Namen wie Stuhl_01 eingeben. Erlaubt sind Buchstaben, Ziffern, Leerzeichen, Bindestrich und Unterstrich; Anfang und Ende müssen Buchstaben oder Ziffern sein. Keine Satzzeichen oder Dateiendung. Für Dateinamen werden Umlaute ausgeschrieben und Leerzeichen durch _ ersetzt; höchstens 40 Zeichen. Gleichwertige Namen dürfen in der Liste nur einmal vorkommen, auch bei anderer Großschreibung. Uhrzeit (HHMM), achtstellige Kurzkennung und Datum werden automatisch ergänzt.',
                'Die Geometrie dieser FBX mit tyOptimize reduzieren. Ausgeschaltet werden auch die 80-%-Vorstufe und der abschließende Optimizer übersprungen.',
                'Anteil der Vertices, der beim letzten Optimizer erhalten bleibt. Bei aktiver 80-%-Vorstufe bezieht sich der Wert auf die bereits reduzierte Geometrie. Bei 100 % bleibt der letzte Modifier deaktiviert und editierbar.',
                'Texturen für dieses Asset verarbeiten und das V-Ray-Material zuweisen. Ausgeschaltet wird das Mesh ohne Texturen und ohne Material gespeichert.',
                'Maximale Kantenlänge der Maps in Pixeln. Kleinere Quellen werden nicht vergrößert. Original übernimmt die Quellauflösung. Ohne Texturen wird diese Auswahl nicht verwendet.',
                'Bearbeitungsstand dieser FBX. Fehlende, ungültige und doppelte Namen müssen vor dem Start korrigiert werden. Einzelheiten stehen im Protokoll.',
            ]
            for column, tooltip in enumerate(header_tips):
                self.table.horizontalHeaderItem(column).setToolTip(tooltip)
            self.table.setToolTip('Importliste: Asset-Namen per Doppelklick bearbeiten. Optimizer, Restanteil, Texturen und Auflösung gelten jeweils nur für die betreffende Zeile.')
            self.table.setSelectionBehavior(Qt.QAbstractItemView.SelectRows)
            self.table.setSelectionMode(Qt.QAbstractItemView.ExtendedSelection)
            self.table.horizontalHeader().setSectionResizeMode(self.COL_SOURCE, Qt.QHeaderView.Stretch)
            self.table.setColumnWidth(self.COL_PREVIEW, 104)
            self.table.setColumnWidth(self.COL_NAME, 140)
            self.table.setColumnWidth(self.COL_OPTIMIZE, 75)
            self.table.setColumnWidth(self.COL_PERCENT, 90)
            self.table.setColumnWidth(self.COL_TEXTURES, 70)
            self.table.setColumnWidth(self.COL_RESOLUTION, 90)
            self.table.setColumnWidth(self.COL_STATUS, 100)
            self.table.setIconSize(core.QtCore.QSize(96, 96))
            self.table.verticalHeader().setDefaultSectionSize(104)
            self.table.setMinimumSize(780, 330)
            self._name_delegate = AssetNameDelegate(self)
            self.table.setItemDelegateForColumn(self.COL_NAME, self._name_delegate)
            self.table.viewport().installEventFilter(self)
            self.table.horizontalHeader().viewport().installEventFilter(self)
            from tencent_preview_widget import make_preview_panel_class
            self.mesh_preview = make_preview_panel_class(core)(self)
            self.mesh_preview.name_edit.setToolTip(header_tips[self.COL_NAME])
            self.queue_splitter = Qt.QSplitter(core.QtCore.Qt.Horizontal)
            self.queue_splitter.addWidget(self.table)
            self.queue_splitter.addWidget(self.mesh_preview)
            self.queue_splitter.setChildrenCollapsible(False)
            self.queue_splitter.setStretchFactor(0, 3)
            self.queue_splitter.setStretchFactor(1, 1)
            self.queue_splitter.setSizes([1060, 300])
            controls.addWidget(self.queue_splitter, 1)
            form = Qt.QFormLayout()
            self.target = Qt.QLineEdit()
            self.target.setToolTip('Zielordner für die MAX-Dateien und die dazugehörigen Map-Ordner. Zusammengehörige Dateien behalten ihre eindeutige Asset-Kennung, damit Maps später wieder verknüpft werden können.')
            row = Qt.QWidget()
            row_layout = Qt.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(self.target)
            browse = Qt.QPushButton('Ordner …')
            browse.setToolTip('Den Zielordner für MAX-Dateien und Map-Ordner auswählen.')
            row_layout.addWidget(browse)
            target_label = Qt.QLabel('Zielordner')
            target_label.setToolTip(self.target.toolTip())
            form.addRow(target_label, row)
            controls.addLayout(form)
            self.pre80 = Qt.QCheckBox('80-%-Vorstufe')
            self.pre80.setChecked(True)
            self.pre80.setToolTip('Reduziert das Mesh vorab auf 80 % und kollabiert den Stack. Danach folgt „Rest %“. Nur bei aktivem Optimizer.')
            options = Qt.QGridLayout()
            self.save_max = Qt.QCheckBox('MAX speichern')
            self.save_max.setChecked(True)
            self.save_max.setEnabled(False)
            self.save_max.setToolTip('Jedes Asset wird als eigene MAX-Datei gespeichert. Zusätzliche Verwaltungsdaten werden weder in die MAX-Datei noch in ihre Objekte geschrieben. Die eindeutigen Datei- und Mapnamen dienen zur Zuordnung.')
            self.offer_overwrite = Qt.QCheckBox('Ersetzen anbieten')
            self.offer_overwrite.setChecked(True)
            self.offer_overwrite.setToolTip('Bei gleichem Asset-Namen im Zielordner nachfragen: ein vorhandenes Asset mit seinen Maps, seiner MAX-Datei und seinen bisherigen Objekten in der offenen Szene gemeinsam ersetzen oder eine neue Kopie mit eigener Kennung anlegen. Beim Ersetzen bleiben Kennung und Dateipfade für späteres Relinken erhalten. Ausgeschaltet wird immer eine neue Kopie angelegt.')
            self.group = Qt.QCheckBox('Gruppieren')
            self.group.setToolTip('Die Objekte jeder importierten FBX zu einer eigenen Gruppe zusammenfassen.')
            self.layer = Qt.QCheckBox('Layer')
            self.layer.setChecked(True)
            self.layer.setToolTip('Die importierten Objekte dem rechts ausgewählten Layer zuweisen. Ein neuer Layer wird bei Bedarf angelegt.')
            self.layer_name = Qt.QComboBox()
            self.layer_name.setEditable(True)
            self.layer_name.setCompleter(None)
            self.layer_name.setInsertPolicy(Qt.QComboBox.NoInsert)
            self.layer_name.addItems(list(dict.fromkeys(core.recent_layer_names() + ['00_PPL'] + core.scene_layer_names())))
            self.layer_name.setToolTip('Einen vorhandenen Layer auswählen oder einen eigenen Layer-Namen eingeben. Der eingegebene Text wird direkt übernommen, ohne automatische Vervollständigung. Die letzten zehn erfolgreich verwendeten Layer-Namen stehen beim nächsten Öffnen wieder zur Auswahl. Die Auswahl gilt, wenn Layer eingeschaltet ist.')
            options.addWidget(self.pre80, 0, 0)
            options.addWidget(self.group, 1, 0)
            options.addWidget(self.save_max, 0, 1)
            options.addWidget(self.offer_overwrite, 1, 1)
            options.addWidget(self.layer, 0, 2)
            options.addWidget(self.layer_name, 0, 3, 2, 1)
            options.setColumnStretch(3, 1)
            controls.addLayout(options)
            actions = Qt.QHBoxLayout()
            self.import_button = Qt.QPushButton('Importieren')
            self.import_button.setToolTip('Nach erfolgreicher Vorprüfung alle FBX-Dateien mit den geprüften Einstellungen importieren. Änderungen an der Liste oder den Einstellungen erfordern eine neue Vorprüfung.')
            self.scan_button = Qt.QPushButton('Vorprüfen')
            self.precheck_button = self.scan_button
            self.scan_button.setToolTip('Die gesamte Liste vor dem Import prüfen: Namen, FBX-Dateien, Maps, Zielordner und benötigte Plugins. Vorhandene Assets werden vorab geklärt. Fehler werden je Zeile angezeigt; beim erneuten Prüfen werden unveränderte erfolgreiche Ergebnisse wiederverwendet. Die Szene bleibt unverändert.')
            self._action_tooltips = {self.import_button: self.import_button.toolTip(), self.scan_button: self.scan_button.toolTip()}
            self.preview_button = Qt.QPushButton('Vorschau')
            self.preview_button.setToolTip('Die ausgewählte FBX als große, drehbare Mesh-Vorschau öffnen. Ohne Auswahl wird die erste Zeile verwendet. Die Vorschau funktioniert auch ohne Asset-Namen und verändert die Szene nicht.')
            for button in [self.scan_button, self.import_button, self.preview_button]:
                actions.addWidget(button)
            controls.addLayout(actions)
            self.progress = Qt.QProgressBar()
            self.progress.setToolTip('Anzahl der bereits bearbeiteten FBX-Dateien im aktuellen Durchlauf.')
            layout.addWidget(self.progress)
            self.cancel_button = Qt.QPushButton('Stoppen')
            self.cancel_button.setToolTip('Den Durchlauf nach der gerade bearbeiteten FBX stoppen. Bereits fertiggestellte Assets bleiben erhalten.')
            self.cancel_button.setEnabled(False)
            layout.addWidget(self.cancel_button)
            self.report_edit = Qt.QPlainTextEdit()
            self.report_edit.setReadOnly(True)
            layout.addWidget(self.report_edit, 1)
            self.add_button.clicked.connect(self._browse)
            remove.clicked.connect(self._remove)
            clear.clicked.connect(self._clear)
            browse.clicked.connect(self._target)
            self.import_button.clicked.connect(lambda: self._run(False))
            self.scan_button.clicked.connect(self._precheck)
            self.preview_button.clicked.connect(self._preview)
            self.cancel_button.clicked.connect(self._cancel)
            self.layer.toggled.connect(self.layer_name.setEnabled)
            self.table.itemChanged.connect(self._asset_name_changed)
            self.table.currentCellChanged.connect(self._preview_selection_changed)
            self.table.cellDoubleClicked.connect(self._preview_double_clicked)
            self.mesh_preview.name_edit.textChanged.connect(self._preview_name_changed)
            self.mesh_preview.thumbnail_ready.connect(self._thumbnail_ready)
            for checkbox in (self.pre80, self.save_max, self.offer_overwrite, self.group, self.layer):
                checkbox.toggled.connect(self._settings_changed)
            self.target.textChanged.connect(self._settings_changed)
            self.layer_name.currentTextChanged.connect(self._settings_changed)
            self._validate_names()

        def add_files(self, files):
            try:
                from PySide2.QtCore import Qt as Flags
            except ImportError:
                from PySide6.QtCore import Qt as Flags
            blocker = core.QtCore.QSignalBlocker(self.table)
            for file in files:
                path = Path(file).resolve()
                if path.suffix.lower() != '.fbx':
                    continue
                index = self.table.rowCount()
                self.table.insertRow(index)
                thumbnail = Qt.QTableWidgetItem('Laden …')
                thumbnail.setFlags(thumbnail.flags() & ~Flags.ItemIsEditable)
                thumbnail.setTextAlignment(core.QtCore.Qt.AlignCenter)
                thumbnail.setToolTip(self.table.horizontalHeaderItem(self.COL_PREVIEW).toolTip())
                thumbnail.setData(core.QtCore.Qt.UserRole, self.mesh_preview.source_key(path))
                self.table.setItem(index, self.COL_PREVIEW, thumbnail)
                cell = Qt.QTableWidgetItem(path.name)
                cell.setData(core.QtCore.Qt.UserRole, str(path))
                cell.setFlags(cell.flags() & ~Flags.ItemIsEditable)
                cell.setToolTip(f'FBX-Quelldatei:\n{path}')
                self.table.setItem(index, self.COL_SOURCE, cell)
                name = Qt.QTableWidgetItem('')
                name.setData(core.QtCore.Qt.UserRole, self._next_name_id)
                self._next_name_id += 1
                name.setToolTip(self.table.horizontalHeaderItem(self.COL_NAME).toolTip())
                self.table.setItem(index, self.COL_NAME, name)
                optimizer = CenteredCheckBox()
                optimizer.setChecked(True)
                optimizer.setToolTip(self.table.horizontalHeaderItem(self.COL_OPTIMIZE).toolTip())
                optimizer.installEventFilter(self)
                self.table.setCellWidget(index, self.COL_OPTIMIZE, optimizer)
                percent = Qt.QDoubleSpinBox()
                percent.setRange(.01, 100)
                percent.setDecimals(2)
                percent.setSuffix(' %')
                percent.setValue(55)
                percent.setToolTip(self.table.horizontalHeaderItem(self.COL_PERCENT).toolTip())
                percent.installEventFilter(self)
                percent.lineEdit().installEventFilter(self)
                self.table.setCellWidget(index, self.COL_PERCENT, percent)
                optimizer.toggled.connect(percent.setEnabled)
                optimizer.toggled.connect(self._settings_changed)
                percent.valueChanged.connect(self._settings_changed)
                textures = CenteredCheckBox()
                textures.setChecked(True)
                textures.setToolTip(self.table.horizontalHeaderItem(self.COL_TEXTURES).toolTip())
                textures.installEventFilter(self)
                self.table.setCellWidget(index, self.COL_TEXTURES, textures)
                resolution = Qt.QComboBox()
                for label, value in [('512', 512), ('1024', 1024), ('2048', 2048), ('Original', 0)]:
                    resolution.addItem(label, value)
                resolution.setCurrentIndex(resolution.findData(2048))
                resolution.setToolTip(self.table.horizontalHeaderItem(self.COL_RESOLUTION).toolTip())
                resolution.installEventFilter(self)
                self.table.setCellWidget(index, self.COL_RESOLUTION, resolution)
                textures.toggled.connect(resolution.setEnabled)
                textures.toggled.connect(self._settings_changed)
                resolution.currentIndexChanged.connect(self._settings_changed)
                status = Qt.QTableWidgetItem('Name fehlt')
                status.setFlags(status.flags() & ~Flags.ItemIsEditable)
                status.setToolTip('Vor dem Start einen einfachen Asset-Namen in dieser Zeile eingeben.')
                self.table.setItem(index, self.COL_STATUS, status)
                if not self.target.text():
                    self.target.setText(str(path.parent))
            del blocker
            self._validate_names()
            self._refresh_preview_sources()
            if self.table.currentRow() < 0 and self.table.rowCount():
                self.table.setCurrentCell(0, self.COL_SOURCE)
            self._preview_selection_changed()

        def eventFilter(self, watched, event):
            if event.type() == core.QtCore.QEvent.ToolTip:
                tooltip = ''
                if watched is self.table.viewport():
                    index = self.table.indexAt(event.pos())
                    if index.isValid():
                        widget = self.table.cellWidget(index.row(), index.column())
                        item = self.table.item(index.row(), index.column())
                        tooltip = widget.toolTip() if widget is not None else item.toolTip() if item is not None else ''
                elif watched is self.table.horizontalHeader().viewport():
                    column = self.table.horizontalHeader().logicalIndexAt(event.pos())
                    item = self.table.horizontalHeaderItem(column) if column >= 0 else None
                    tooltip = item.toolTip() if item is not None else ''
                elif isinstance(watched, Qt.QWidget) and self.table.isAncestorOf(watched):
                    tooltip = watched.toolTip()
                    if not tooltip and watched.parentWidget() is not None:
                        tooltip = watched.parentWidget().toolTip()
                if tooltip:
                    Qt.QToolTip.showText(event.globalPos(), tooltip, watched)
                    return True
            return super().eventFilter(watched, event)

        def showEvent(self, event):
            super().showEvent(event)
            # Max also filters application tooltip events. Handle our table
            # first, including its disabled controls, without touching other UI.
            Qt.QApplication.instance().installEventFilter(self)

        def _browse(self):
            files, _ = Qt.QFileDialog.getOpenFileNames(self, 'FBX-Dateien hinzufügen', '', 'FBX (*.fbx)')
            self.add_files(files)

        def _remove(self):
            self._name_delegate.commit_active_editor()
            blocker = core.QtCore.QSignalBlocker(self.table)
            for index in sorted({item.row() for item in self.table.selectedItems()}, reverse=True):
                self.table.removeRow(index)
            del blocker
            self._validate_names()
            self._refresh_preview_sources()
            self._preview_selection_changed()

        def _clear(self):
            self._name_delegate.commit_active_editor()
            blocker = core.QtCore.QSignalBlocker(self.table)
            self.table.setRowCount(0)
            self._name_feedback.clear()
            self._preflight_records.clear()
            del blocker
            self._validate_names()
            self.mesh_preview.clear()

        def _target(self):
            folder = Qt.QFileDialog.getExistingDirectory(self, 'Zielordner', self.target.text())
            if folder:
                self.target.setText(folder)

        def _asset_name_changed(self, item):
            if item.column() != self.COL_NAME or self._running:
                return
            self._validate_names(changed_row=item.row())

        def _name_rules(self):
            return self.table.horizontalHeaderItem(self.COL_NAME).toolTip()

        def _settings_changed(self, *unused):
            if not self._running:
                self._validate_names()

        def _validate_names(self, changed_row=None):
            if self._validating_names:
                return self._name_errors
            names = [self.table.item(row, self.COL_NAME).text()
                     if self.table.item(row, self.COL_NAME) is not None else ''
                     for row in range(self.table.rowCount())]
            pending = self._name_delegate.pending_name()
            if pending is not None and pending[0] < len(names):
                names[pending[0]] = pending[1]
            errors = core.asset_name_errors(names)
            self._name_errors = errors
            if pending is not None and self._name_delegate.editor is not None:
                self._name_delegate.editor.setStyleSheet(
                    'QLineEdit { background: #ffd8d8; color: #700000; }' if pending[0] in errors else '')
            self._validating_names = True
            blocker = core.QtCore.QSignalBlocker(self.table)
            active_ids = set()
            for row, name in enumerate(names):
                item = self.table.item(row, self.COL_NAME)
                status = self.table.item(row, self.COL_STATUS)
                if item is None or status is None:
                    continue
                token = item.data(core.QtCore.Qt.UserRole)
                active_ids.add(token)
                preview_changed = pending is not None and row == pending[0] and name != item.text()
                if row in errors or preview_changed:
                    if token not in self._name_feedback:
                        self._name_feedback[token] = (item.text(), status.text(), status.toolTip())
                if row == changed_row and token in self._name_feedback:
                    self._name_feedback[token] = (item.text(), 'Bereit', self.table.horizontalHeaderItem(self.COL_STATUS).toolTip())
                if row in errors:
                    error = errors[row]
                    status.setText('Name fehlt' if not name.strip() else 'Name doppelt' if 'doppelt' in error.casefold() else 'Name prüfen')
                    status.setToolTip(error)
                    item.setToolTip(f'{error}\n\n{self._name_rules()}')
                    for cell in (item, status):
                        cell.setBackground(core.QtGui.QColor('#ffd8d8'))
                        cell.setForeground(core.QtGui.QColor('#700000'))
                else:
                    feedback = self._name_feedback.get(token)
                    if feedback is not None:
                        same_name = feedback[0] == name and row != changed_row
                        restored = feedback[1] if same_name and feedback[1] not in ('Name fehlt', 'Name prüfen', 'Name doppelt') else 'Bereit'
                        status.setText(restored)
                        status.setToolTip(feedback[2] if same_name else self.table.horizontalHeaderItem(self.COL_STATUS).toolTip())
                        if not preview_changed:
                            self._name_feedback.pop(token, None)
                    elif row == changed_row:
                        status.setText('Bereit')
                        status.setToolTip(self.table.horizontalHeaderItem(self.COL_STATUS).toolTip())
                    item.setToolTip(self._name_rules())
                    for cell in (item, status):
                        cell.setBackground(core.QtGui.QBrush())
                        cell.setForeground(core.QtGui.QBrush())
            self._name_feedback = {token: value for token, value in self._name_feedback.items() if token in active_ids}
            del blocker
            self._validating_names = False
            self._sync_actions(names, errors)
            self._sync_preview_name()
            return errors

        def _sync_actions(self, names, errors):
            options = None
            try:
                options = self._options(validate=False, commit=False, names=names)
            except (ValueError, AttributeError):
                pass
            ready = options is not None and not errors and self._preflight.ready(options, self.offer_overwrite.isChecked())
            self.scan_button.setEnabled(bool(names) and not self._running)
            self.import_button.setEnabled(bool(names) and ready and not self._running)
            if errors:
                self.import_button.setToolTip('Start gesperrt: Alle markierten Asset-Namen korrigieren und Vorprüfen ausführen.\n' + '\n'.join(f'Zeile {row + 1}: {message}' for row, message in sorted(errors.items())))
            elif not ready:
                self.import_button.setToolTip('Start gesperrt: Zuerst Vorprüfen ausführen und alle angezeigten Fehler beheben. Geänderte Einstellungen müssen erneut geprüft werden.')
            else:
                self.import_button.setToolTip(self._action_tooltips[self.import_button])
            if options is not None:
                self._show_preflight_feedback(options, errors)

        def _option_signature(self, options):
            return repr(options), self.offer_overwrite.isChecked()

        def _show_preflight_feedback(self, options, name_errors):
            previous_validation = self._validating_names
            self._validating_names = True
            blocker = core.QtCore.QSignalBlocker(self.table)
            try:
                for row, option in enumerate(options):
                    item = self.table.item(row, self.COL_NAME)
                    status = self.table.item(row, self.COL_STATUS)
                    record = self._preflight_records.get(item.data(core.QtCore.Qt.UserRole))
                    if record is None or row in name_errors:
                        continue
                    signature, result = record
                    if result.get('name_error'):
                        # Duplicate-name errors depend on the entire queue,
                        # not just this row's settings. Live validation wins
                        # when editing or removing another row resolves them.
                        self._preflight_records.pop(item.data(core.QtCore.Qt.UserRole), None)
                        status.setText('Prüfen')
                        status.setToolTip('Namensfehler korrigiert. Vorprüfen erneut ausführen.')
                        continue
                    if signature != self._option_signature(option):
                        status.setText('Prüfen')
                        status.setToolTip('Die Einstellungen wurden geändert. Vorprüfen erneut ausführen.')
                    else:
                        status.setText(result['status'])
                        status.setToolTip(result['text'])
                        if not result['success']:
                            item.setToolTip(result['text'])
                            for cell in (item, status):
                                cell.setBackground(core.QtGui.QColor('#ffd8d8'))
                                cell.setForeground(core.QtGui.QColor('#700000'))
            finally:
                del blocker
                self._validating_names = previous_validation

        def _options(self, validate=True, commit=True, names=None):
            if commit:
                self._name_delegate.commit_active_editor()
            if not self.table.rowCount():
                raise ValueError('Bitte mindestens eine FBX hinzufügen.')
            if validate:
                errors = self._validate_names()
                if errors:
                    raise ValueError('Asset-Namen korrigieren:\n' + '\n'.join(f'Zeile {row + 1}: {message}' for row, message in sorted(errors.items())))
            if not self.target.text().strip():
                raise ValueError('Bitte einen Zielordner wählen.')
            result = []
            for row in range(self.table.rowCount()):
                name = names[row] if names is not None else self.table.item(row, self.COL_NAME).text()
                resolution = self.table.cellWidget(row, self.COL_RESOLUTION).currentData()
                result.append(core.ImportOptions(
                    fbx_file=self._source_path(row), asset_name=name,
                    prefix=core.make_default_prefix(), asset_folder=Path(self.target.text()),
                    texture_folder=core.maps_folder_for_target(Path(self.target.text()), name),
                    optimize_percent=self.table.cellWidget(row, self.COL_PERCENT).value(),
                    optimize_enabled=self.table.cellWidget(row, self.COL_OPTIMIZE).isChecked(),
                    import_textures=self.table.cellWidget(row, self.COL_TEXTURES).isChecked(),
                    pre_optimize_80=self.pre80.isChecked(),
                    save_max_file=self.save_max.isChecked(), group_imported_objects=self.group.isChecked(),
                    create_layer=self.layer.isChecked(), layer_mode='custom', layer_custom_name=self.layer_name.lineEdit().text(),
                    texture_downscale_sizes=(resolution,) if resolution else ()))
            if validate:
                core.validate_batch_names(result)
            return result

        def _precheck(self):
            if self._running:
                return
            try:
                options = self._options(validate=False)
            except Exception as exc:
                self.report_edit.setPlainText(str(exc))
                return
            self._running = True
            self._cancelled = False
            self.controls.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.report_edit.clear()
            self.progress.setRange(0, len(options))
            self.progress.setValue(0)

            def progress(index, total, record):
                token = self.table.item(index, self.COL_NAME).data(core.QtCore.Qt.UserRole)
                self._preflight_records[token] = (self._option_signature(options[index]), record)
                self.table.item(index, self.COL_STATUS).setText(record['status'])
                self.table.item(index, self.COL_STATUS).setToolTip(record['text'])
                self.report_edit.appendPlainText(f"Zeile {index + 1}: {options[index].fbx_file.name}\n{record['text']}\n")
                self.progress.setValue(index + 1)
                Qt.QApplication.processEvents()
                if self._cancelled:
                    raise core.ImportCancelled('Vorprüfung gestoppt. Vor dem Import erneut Vorprüfen ausführen.')

            try:
                records = self._preflight.check(options, resolver=self._resolve_overwrite,
                                                confirmer=self._confirm_overwrite, progress=progress,
                                                offer_overwrite=self.offer_overwrite.isChecked())
                if all(record['success'] for record in records):
                    self.report_edit.appendPlainText('Vorprüfung erfolgreich. Importieren ist freigegeben.')
                else:
                    self.report_edit.appendPlainText('Import gesperrt. Fehler korrigieren und Vorprüfen erneut ausführen.')
            except Exception as exc:
                self._preflight.invalidate()
                self.report_edit.appendPlainText(str(exc))
            finally:
                self._running = False
                self.controls.setEnabled(True)
                self.cancel_button.setEnabled(False)
                self._validate_names()

        def _cancel(self):
            self._cancelled = True
            self.cancel_button.setEnabled(False)

        def _refresh_layer_choices(self):
            typed_name = self.layer_name.lineEdit().text()
            names, seen = [], set()
            for name in core.recent_layer_names() + ['00_PPL'] + core.scene_layer_names():
                if name.casefold() not in seen:
                    names.append(name)
                    seen.add(name.casefold())
            previous_block = self.layer_name.blockSignals(True)
            try:
                self.layer_name.clear()
                self.layer_name.addItems(names)
                self.layer_name.lineEdit().setText(typed_name)
            finally:
                self.layer_name.blockSignals(previous_block)

        def _resolve_overwrite(self, options, candidates):
            if not candidates:
                return None
            chooser = Qt.QDialog(self)
            chooser.setWindowTitle('Asset vorhanden')
            chooser.resize(850, 360)
            layout = Qt.QVBoxLayout(chooser)
            selection = Qt.QComboBox()
            selection.setToolTip('Das vorhandene Asset auswählen, das ersetzt werden soll. Die vollständige Kennung und die betroffenen Dateipfade stehen darunter. Bei mehreren Treffern ist eine ausdrückliche Auswahl erforderlich.')
            for candidate in candidates:
                manifest = candidate['manifest']
                selection.addItem(f"{manifest.get('requested_asset_name') or options.asset_name} · {manifest.get('imported_at') or 'Datum unbekannt'} · {manifest.get('asset_short_code') or manifest.get('asset_id', '')[:8]}")
            selection.setCurrentIndex(0 if len(candidates) == 1 else -1)
            layout.addWidget(selection)
            details = Qt.QPlainTextEdit()
            details.setReadOnly(True)
            details.setToolTip('Metadaten des ausgewählten vorhandenen Assets. Quelle und Asset-ID helfen dabei, gleich benannte Assets sicher auseinanderzuhalten. Die Dateipfade können kopiert werden.')
            layout.addWidget(details, 1)
            actions = Qt.QHBoxLayout()
            fresh = Qt.QPushButton('Neu')
            fresh.setToolTip('Eine zusätzliche Kopie mit einer neuen eindeutigen Asset-ID und neuen Dateipfaden anlegen. Das vorhandene Asset bleibt erhalten.')
            replace = Qt.QPushButton('Ersetzen wählen')
            replace.setToolTip('Dieses Asset für den späteren Import zum Ersetzen auswählen. Danach die betroffenen Dateien und Szenenobjekte prüfen und ausdrücklich bestätigen. Kennung und Dateipfade bleiben erhalten; Maps, MAX-Datei und bisherige Objekte werden beim Import gemeinsam aktualisiert.')
            cancel = Qt.QPushButton('Abbrechen')
            cancel.setToolTip('Für diese FBX keine Auswahl bestätigen. Die Vorprüfung meldet die Zeile als ungeklärt und prüft die übrigen Zeilen weiter.')
            actions.addWidget(fresh)
            actions.addStretch()
            actions.addWidget(replace)
            actions.addWidget(cancel)
            layout.addLayout(actions)
            fresh.setDefault(True)
            fresh.clicked.connect(lambda: chooser.done(2))
            replace.clicked.connect(chooser.accept)
            cancel.clicked.connect(chooser.reject)

            def update_details(index):
                replace.setEnabled(index >= 0)
                if index < 0:
                    details.clear()
                    return
                candidate = candidates[index]
                manifest = candidate['manifest']
                details.setPlainText('\n'.join([
                    f"Asset: {manifest.get('asset_code') or manifest.get('asset', '')}",
                    f"Kennung: {manifest.get('asset_short_code') or manifest.get('asset_id', '')}",
                    f"Quelle: {manifest.get('source_fbx') or 'Nicht gespeichert'}",
                    f"MAX: {manifest.get('max_file') or 'Bisher nicht gespeichert'}",
                    f"Maps: {candidate['texture_folder']}",
                    f"Neue Quelle: {options.fbx_file}",
                ]))

            selection.currentIndexChanged.connect(update_details)
            update_details(selection.currentIndex())
            result = chooser.exec_() if hasattr(chooser, 'exec_') else chooser.exec()
            if result == 2:
                return None
            if result != Qt.QDialog.Accepted or selection.currentIndex() < 0:
                raise core.ImportCancelled('Asset-Auswahl nicht bestätigt. Vor dem Import erneut Vorprüfen ausführen.')
            return candidates[selection.currentIndex()]

        def _confirm_overwrite(self, options, candidate, plan):
            manifest = candidate['manifest']
            scene_nodes = plan.get('scene_nodes', [])
            confirmation = Qt.QMessageBox(self)
            confirmation.setWindowTitle('Ersetzen bestätigen')
            confirmation.setIcon(Qt.QMessageBox.Warning)
            readable_name = manifest.get('requested_asset_name') or options.requested_asset_name or options.asset_name
            confirmation.setText(f'Asset „{readable_name}“ ersetzen?')
            confirmation.setInformativeText(f'Beim anschließenden Import werden Maps und MAX-Datei gemeinsam ersetzt. {len(scene_nodes)} bisherige Objekte dieses Assets in der offenen Szene werden ersetzt. Andere Materialien mit denselben Map-Pfaden verwenden danach ebenfalls die neuen Maps.')
            confirmation.setDetailedText('\n'.join([
                f"Asset: {manifest.get('asset_code') or manifest.get('asset', '')}",
                f"Kennung: {manifest.get('asset_short_code') or manifest.get('asset_id', '')}",
                f"Bisherige Quelle: {manifest.get('source_fbx') or 'Nicht gespeichert'}",
                f"Neue Quelle: {options.fbx_file}",
                f"MAX: {plan.get('max_file', '')}",
                f"Maps: {plan.get('texture_folder', '')}",
                '', 'Betroffene Dateien:', *[str(path) for path in plan.get('files', [])],
                '', 'Bisherige Szenenobjekte:', *[str(name) for name in scene_nodes],
            ]))
            overwrite = confirmation.addButton('Ersetzen', Qt.QMessageBox.DestructiveRole)
            overwrite.setToolTip('Das Ersetzen für den anschließenden Import bestätigen. Die Vorprüfung ändert noch keine Dateien oder Objekte. Beim Import werden Maps, MAX-Datei und bisherige Szenenobjekte gemeinsam ersetzt; Kennung und Dateipfade bleiben erhalten.')
            stop = confirmation.addButton('Abbrechen', Qt.QMessageBox.RejectRole)
            stop.setToolTip('Das Ersetzen nicht bestätigen. Diese Zeile bleibt ungeklärt und der Import gesperrt; die übrigen Zeilen werden weiter geprüft.')
            for button in confirmation.buttons():
                if button is not overwrite and button is not stop:
                    button.setText('Details')
                    button.setToolTip('Die vollständige Asset-ID, Quellen, Dateipfade und bisherigen Szenenobjekte anzeigen oder ausblenden.')
                    button.clicked.connect(lambda checked=False, detail_button=button: detail_button.setText('Details'))
            confirmation.setDefaultButton(stop)
            confirmation.setEscapeButton(stop)
            confirmation.exec_() if hasattr(confirmation, 'exec_') else confirmation.exec()
            return confirmation.clickedButton() is overwrite

        def _run(self, scan_only):
            if scan_only:
                return self._precheck()
            if self._running:
                return
            try:
                options = self._options()
                if not self._preflight.ready(options, self.offer_overwrite.isChecked()):
                    raise ValueError('Import gesperrt. Zuerst Vorprüfen ausführen und alle angezeigten Fehler beheben.')
            except Exception as exc:
                self.report_edit.setPlainText(str(exc))
                return
            self._running = True
            self._cancelled = False
            self.controls.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.report_edit.clear()
            self.progress.setRange(0, len(options))
            self.progress.setValue(0)
            failure_feedback = {}

            def remember_failure(row, text, status='Prüfen'):
                if not isinstance(row, int) or not 0 <= row < len(options):
                    return
                token = self.table.item(row, self.COL_NAME).data(core.QtCore.Qt.UserRole)
                failure_feedback[token] = (self._option_signature(options[row]),
                                           {'success': False, 'status': status, 'text': str(text)})

            for row in range(len(options)):
                self.table.item(row, self.COL_STATUS).setText('Wartet')
                self.table.item(row, self.COL_STATUS).setToolTip(self.table.horizontalHeaderItem(self.COL_STATUS).toolTip())
            def progress(index, status, text):
                self.table.item(index, self.COL_STATUS).setText(status)
                if text:
                    self.report_edit.appendPlainText(f'FBX {index + 1}: {options[index].fbx_file.name}\n{text}\n')
                    self.progress.setValue(index + 1)
                Qt.QApplication.processEvents()
            try:
                results = core.run_batch(options, progress, lambda: self._cancelled, False,
                                         preflight=self._preflight)
                for position, result in enumerate(results):
                    if not result['success']:
                        remember_failure(result.get('index', position), result.get('text') or 'Import fehlgeschlagen. Bitte erneut vorprüfen.', 'Fehler')
                for row in range(len(results), len(options)):
                    self.table.item(row, self.COL_STATUS).setText('Gestoppt')
                success = sum(r['success'] for r in results)
                self.report_edit.appendPlainText(f'{success} erfolgreich, {len(results) - success} Fehler, {len(options) - len(results)} nicht ausgeführt.')
            except self._preflight_required as exc:
                self.report_edit.appendPlainText(str(exc))
                failed_row = getattr(exc, 'row_index', None)
                remember_failure(failed_row, exc)
                for row in range(len(options)):
                    status = self.table.item(row, self.COL_STATUS)
                    if status.text() not in ('Fertig', 'Fehler'):
                        status.setText('Prüfen' if row == failed_row else 'Gestoppt')
                        status.setToolTip(str(exc) if row == failed_row else 'Wegen einer notwendigen erneuten Vorprüfung nicht ausgeführt.')
            except core.ImportCancelled as exc:
                self.report_edit.appendPlainText(str(exc))
                for row in range(len(options)):
                    if self.table.item(row, self.COL_STATUS).text() not in ('Fertig', 'Fehler', 'Gestoppt'):
                        self.table.item(row, self.COL_STATUS).setText('Gestoppt')
            except Exception as exc:
                self.report_edit.appendPlainText(str(exc))
                for row in range(len(options)):
                    if self.table.item(row, self.COL_STATUS).text() not in ('Fertig', 'Fehler', 'Gestoppt'):
                        self.table.item(row, self.COL_STATUS).setText('Gestoppt')
            finally:
                self._preflight.invalidate()
                self._preflight_records.clear()
                self._preflight_records.update(failure_feedback)
                self._running = False
                self._refresh_layer_choices()
                self.controls.setEnabled(True)
                self.cancel_button.setEnabled(False)
                self._validate_names()

        def _preview(self):
            row = self.table.currentRow()
            if row < 0 and self.table.rowCount():
                row = 0
            if row >= 0:
                self.table.setCurrentCell(row, self.COL_SOURCE)
                self._preview_selection_changed()
                self.mesh_preview.show_large()

        def _preview_double_clicked(self, row, column):
            if column in (self.COL_PREVIEW, self.COL_SOURCE):
                self._preview()

        def _preview_selection_changed(self, *unused):
            row = self.table.currentRow()
            if row >= 0 and self.table.item(row, self.COL_SOURCE) is not None:
                path = self._source_path(row)
                if self.table.item(row, self.COL_PREVIEW).data(core.QtCore.Qt.UserRole) != self.mesh_preview.source_key(path):
                    self._refresh_preview_sources()
                self.mesh_preview.set_source(path)
            else:
                self.mesh_preview.set_source(None)
            self._sync_preview_name()

        def _sync_preview_name(self):
            row = self.table.currentRow()
            item = self.table.item(row, self.COL_NAME) if row >= 0 else None
            editor = self.mesh_preview.name_edit
            blocker = core.QtCore.QSignalBlocker(editor)
            text = item.text() if item is not None else ''
            pending = self._name_delegate.pending_name()
            if pending is not None and pending[0] == row:
                text = pending[1]
            if editor.text() != text:
                editor.setText(text)
            editor.setEnabled(item is not None and not self._running)
            invalid = item is not None and item.background().color().name() == '#ffd8d8'
            editor.setStyleSheet('QLineEdit { background: #ffd8d8; color: #700000; }' if invalid else '')
            editor.setToolTip(item.toolTip() if item is not None else self._name_rules())
            del blocker

        def _preview_name_changed(self, text):
            if self._running:
                return
            row = self.table.currentRow()
            item = self.table.item(row, self.COL_NAME) if row >= 0 else None
            if item is not None and item.text() != text:
                pending = self._name_delegate.pending_name()
                if pending is not None and pending[0] == row:
                    blocker = core.QtCore.QSignalBlocker(self._name_delegate.editor)
                    self._name_delegate.editor.setText(text)
                    del blocker
                item.setText(text)

        def _source_path(self, row):
            item = self.table.item(row, self.COL_SOURCE)
            return Path(item.data(core.QtCore.Qt.UserRole) or item.text())

        def _refresh_preview_sources(self):
            paths = []
            blocker = core.QtCore.QSignalBlocker(self.table)
            for row in range(self.table.rowCount()):
                path = self._source_path(row)
                paths.append(path)
                item = self.table.item(row, self.COL_PREVIEW)
                key = self.mesh_preview.source_key(path)
                if item.data(core.QtCore.Qt.UserRole) != key:
                    item.setData(core.QtCore.Qt.UserRole, key)
                    item.setIcon(core.QtGui.QIcon())
                    item.setText('Laden …')
            del blocker
            self.mesh_preview.set_sources(paths)

        def _thumbnail_ready(self, result):
            path, key, image, error = result
            blocker = core.QtCore.QSignalBlocker(self.table)
            for row in range(self.table.rowCount()):
                item = self.table.item(row, self.COL_PREVIEW)
                if item is None or item.data(core.QtCore.Qt.UserRole) != key:
                    continue
                if image is not None:
                    item.setIcon(core.QtGui.QIcon(core.QtGui.QPixmap.fromImage(image)))
                    item.setText('')
                    item.setToolTip(self.table.horizontalHeaderItem(self.COL_PREVIEW).toolTip())
                else:
                    item.setText('Kein Bild')
                    item.setToolTip(error or 'Für diese FBX ist keine Mesh-Vorschau verfügbar. Die Vorprüfung prüft separat, ob ein Import möglich ist.')
            del blocker

        def closeEvent(self, event):
            if self._running:
                self._cancel()
                event.ignore()
            else:
                self.mesh_preview.shutdown()
                Qt.QApplication.instance().removeEventFilter(self)
                event.accept()

    return ImporterDialog
