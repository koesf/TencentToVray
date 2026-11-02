"""Read-only FBX preview widgets; Qt is supplied by the running Max host."""
from collections import OrderedDict
import math
from pathlib import Path
import threading


def _normals_for(mesh):
    normals = []
    for a, b, c in mesh.triangles:
        va, vb, vc = mesh.vertices[a], mesh.vertices[b], mesh.vertices[c]
        u = tuple(vb[k] - va[k] for k in range(3))
        v = tuple(vc[k] - va[k] for k in range(3))
        nx, ny, nz = u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]
        length = math.sqrt(nx*nx + ny*ny + nz*nz) or 1.0
        normals.append((nx/length, ny/length, nz/length))
    return tuple(normals)


def make_preview_panel_class(core):
    Qt = core.QtWidgets
    QC = core.QtCore
    QG = core.QtGui

    def paint_mesh(painter, width, height, mesh, normals, yaw=-35.0, pitch=22.0, zoom=1.0, dragging=False, axes=True):
        gradient = QG.QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0, QG.QColor('#343b42'))
        gradient.setColorAt(1, QG.QColor('#20252a'))
        painter.fillRect(QC.QRect(0, 0, width, height), gradient)
        if mesh is None:
            return
        yaw, pitch = math.radians(yaw), math.radians(pitch)
        cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)

        def rotate(v):
            x, y, z = v
            depth = sy*x + cy*y
            return cy*x-sy*y, cp*z-sp*depth, sp*z+cp*depth

        rotated = [rotate(v) for v in mesh.vertices]
        scale = min(width, height) * .34 * zoom
        points = [QC.QPointF(width*.5 + p[0]*scale, height*.49 - p[1]*scale) for p in rotated]
        step = max(1, len(mesh.triangles)//4000) if dragging else 1
        faces = [(sum(rotated[index][2] for index in triangle), i, triangle)
                 for i, triangle in enumerate(mesh.triangles) if i % step == 0]
        faces.sort(key=lambda item: item[0])
        painter.setPen(QC.Qt.NoPen)
        for _, index, triangle in faces:
            nx, ny, nz = rotate(normals[index])
            illumination = .36 + .64 * abs(nx*-.35 + ny*.55 + nz*.758)
            color = QG.QColor(int(192*illumination), int(206*illumination), int(216*illumination))
            painter.setBrush(color)
            painter.drawPolygon(QG.QPolygonF([points[i] for i in triangle]))
        if axes:
            origin = QC.QPointF(30, height-30)
            for label, axis, color in [('X', (1, 0, 0), '#e19d95'), ('Y', (0, 1, 0), '#9fc59a'), ('Z', (0, 0, 1), '#93bbdf')]:
                v = rotate(axis)
                tip = QC.QPointF(origin.x()+v[0]*19, origin.y()-v[1]*19)
                painter.setPen(QG.QPen(QG.QColor(color), 1.5))
                painter.drawLine(origin, tip)
                painter.drawText(QC.QPointF(tip.x()+3, tip.y()-3), label)

    def thumbnail_image(mesh):
        image = QG.QImage(96, 96, QG.QImage.Format_ARGB32)
        painter = QG.QPainter(image)
        try:
            painter.setRenderHint(QG.QPainter.Antialiasing, True)
            paint_mesh(painter, 96, 96, mesh, _normals_for(mesh), axes=False)
        finally:
            painter.end()
        return image

    class MeshCanvas(Qt.QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.mesh = None
            self.yaw = -35.0
            self.pitch = 22.0
            self.zoom = 1.0
            self._message = 'FBX auswählen'
            self._drag_position = None
            self._normals = ()
            self.setMinimumSize(240, 190)
            self.setSizePolicy(Qt.QSizePolicy.Expanding, Qt.QSizePolicy.Expanding)
            self.setCursor(QC.Qt.OpenHandCursor)
            self.setToolTip('Linke Maustaste ziehen: Mesh drehen. Mausrad: vergrößern oder verkleinern. Doppelklick: Ansicht zurücksetzen. Die Vorschau zeigt vereinfachte Geometrie ohne Texturen und verändert die Szene nicht.')

        def reset_view(self):
            self.yaw, self.pitch, self.zoom = -35.0, 22.0, 1.0
            self.update()

        def set_mesh(self, mesh, message=''):
            self.mesh = mesh
            self._message = message or 'Keine Vorschau'
            self._normals = _normals_for(mesh) if mesh is not None else ()
            self.reset_view()

        def paintEvent(self, event):
            painter = QG.QPainter(self)
            try:
                painter.setRenderHint(QG.QPainter.Antialiasing, True)
                paint_mesh(painter, self.width(), self.height(), self.mesh, self._normals, self.yaw, self.pitch, self.zoom, self._drag_position is not None)
                if self.mesh is None:
                    painter.setPen(QG.QColor('#cbd1d7'))
                    painter.drawText(self.rect().adjusted(20, 20, -20, -20), QC.Qt.AlignCenter | QC.Qt.TextWordWrap, self._message)
                    return
            finally:
                painter.end()

        def mousePressEvent(self, event):
            if event.button() == QC.Qt.LeftButton and self.mesh is not None:
                self._drag_position = event.pos()
                self.setCursor(QC.Qt.ClosedHandCursor)
                event.accept()
            else:
                super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._drag_position is not None:
                delta = event.pos() - self._drag_position
                self._drag_position = event.pos()
                self.yaw += delta.x()*.7
                self.pitch = max(-89.0, min(89.0, self.pitch+delta.y()*.7))
                self.update()
                event.accept()
            else:
                super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            self._drag_position = None
            self.setCursor(QC.Qt.OpenHandCursor)
            self.update()
            super().mouseReleaseEvent(event)

        def mouseDoubleClickEvent(self, event):
            self._drag_position = None
            self.reset_view()
            event.accept()

        def wheelEvent(self, event):
            if self.mesh is not None:
                self.zoom = max(.2, min(6.0, self.zoom * math.pow(1.15, event.angleDelta().y()/120.0)))
                self.update()
                event.accept()
            else:
                event.ignore()

    class WorkerSignals(QC.QObject):
        completed = QC.Signal(object)

    class PreviewWorker(QC.QRunnable):
        def __init__(self, token, path, key, cancellation):
            super().__init__()
            self.token, self.path, self.key, self.cancellation = token, path, key, cancellation
            self.signals = WorkerSignals()

        def run(self):
            mesh, image, error = None, None, ''
            try:
                from tencent_mesh_preview import load_mesh_preview
                mesh = load_mesh_preview(self.path, cancelled=self.cancellation.is_set)
                if not self.cancellation.is_set():
                    image = thumbnail_image(mesh)
            except Exception as exc:
                error = str(exc) or 'Die FBX-Vorschau konnte nicht gelesen werden.'
            # The signal object has no QWidget parent and remains owned by this
            # runnable until emission. Queued receiver connections disappear
            # automatically when the preview panel is destroyed.
            self.signals.completed.emit((self.token, self.key, mesh, image, error))

    class MeshPreviewPanel(Qt.QWidget):
        thumbnail_ready = QC.Signal(object)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setMinimumWidth(260)
            self.current_path = None
            self.mesh = None
            self.loading = False
            self._token = 0
            self._closed = False
            self._cache = OrderedDict()
            self._thumbnails = OrderedDict()
            self._wanted = OrderedDict()
            self._pending = OrderedDict()
            self._workers = {}
            self._large = None
            self._large_canvas = None
            self._timer = QC.QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(120)
            self._timer.timeout.connect(self._start_load)
            layout = Qt.QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            top = Qt.QHBoxLayout()
            self.source_label = Qt.QLabel('Vorschau')
            self.source_label.setMinimumWidth(0)
            self.source_label.setSizePolicy(Qt.QSizePolicy.Ignored, Qt.QSizePolicy.Preferred)
            top.addWidget(self.source_label, 1)
            self.reset_button = Qt.QPushButton('Ansicht')
            self.reset_button.setToolTip('Drehung und Vergrößerung der Mesh-Vorschau zurücksetzen.')
            self.reset_button.clicked.connect(self._reset_views)
            top.addWidget(self.reset_button)
            layout.addLayout(top)
            self.canvas = MeshCanvas(self)
            layout.addWidget(self.canvas, 1)
            self.details_label = Qt.QLabel('')
            self.details_label.setWordWrap(True)
            self.details_label.setMaximumHeight(38)
            layout.addWidget(self.details_label)
            form = Qt.QFormLayout()
            self.name_edit = Qt.QLineEdit()
            self.name_edit.setPlaceholderText('Asset-Name')
            self.name_edit.setEnabled(False)
            form.addRow('Name', self.name_edit)
            layout.addLayout(form)

        def _reset_views(self):
            self.canvas.reset_view()
            if self._large_canvas is not None:
                self._large_canvas.reset_view()

        def _cancel_workers(self):
            for worker in self._workers.values():
                worker.cancellation.set()

        @staticmethod
        def source_key(path):
            path = Path(path)
            try:
                stat = path.stat()
                return str(path.resolve()).casefold(), stat.st_size, stat.st_mtime_ns
            except OSError:
                return str(path.resolve()).casefold(), None, None

        def set_sources(self, paths):
            """Queue one read per distinct source; all row thumbnails load in order."""
            if self._closed:
                return
            self._wanted = OrderedDict((self.source_key(path), Path(path)) for path in paths)
            self._pending = OrderedDict((key, path) for key, path in self._pending.items() if key in self._wanted)
            active_keys = {worker.key for worker in self._workers.values()
                           if not worker.cancellation.is_set()}
            for worker in self._workers.values():
                if worker.key not in self._wanted:
                    worker.cancellation.set()
            for key, path in self._wanted.items():
                if key in self._thumbnails:
                    self.thumbnail_ready.emit((path, key, self._thumbnails[key], ''))
                elif key not in active_keys:
                    self._pending[key] = path
            self._schedule()

        def set_source(self, path):
            if self._closed:
                return
            path = Path(path) if path is not None else None
            key = self.source_key(path) if path is not None else None
            if path == self.current_path and key == getattr(self, '_key', None):
                return
            self.current_path, self._key = path, key
            self.mesh = None
            self.loading = path is not None
            self.source_label.setText(path.name if path is not None else 'Vorschau')
            self.source_label.setToolTip(str(path) if path is not None else 'Eine FBX-Zeile auswählen, um die Geometrie vor dem Benennen anzusehen.')
            self.details_label.setText('FBX lesen …' if path is not None else '')
            self.canvas.set_mesh(None, 'Vorschau wird geladen …' if path is not None else 'FBX auswählen')
            if path is not None:
                if key in self._cache:
                    mesh = self._cache.pop(key)
                    self._cache[key] = mesh
                    self._present(mesh, '')
                else:
                    if not any(worker.key == key and not worker.cancellation.is_set() for worker in self._workers.values()):
                        self._pending[key] = path
                        self._pending.move_to_end(key, last=False)
                    self._schedule()
            self._refresh_large()

        def _schedule(self):
            if not self._closed and not self._workers and self._pending and not self._timer.isActive():
                self._timer.start()

        def _start_load(self):
            if self._closed or self._workers or not self._pending:
                return
            key, path = self._pending.popitem(last=False)
            self._token += 1
            worker = PreviewWorker(self._token, path, key, threading.Event())
            worker.signals.completed.connect(self._loaded, QC.Qt.QueuedConnection)
            self._workers[self._token] = worker
            QC.QThreadPool.globalInstance().start(worker)

        @QC.Slot(object)
        def _loaded(self, result):
            token, key, mesh, image, error = result
            worker = self._workers.pop(token, None)
            if self._closed:
                return
            wanted = key in self._wanted or key == getattr(self, '_key', None)
            if worker is None or worker.cancellation.is_set() or not wanted:
                self._schedule()
                return
            if self.source_key(worker.path) != key:
                mesh, image = None, None
                error = 'Die FBX-Datei wurde während des Ladens geändert. Die Zeile erneut auswählen, um die Vorschau neu zu laden.'
            if mesh is not None:
                self._cache[key] = mesh
                while len(self._cache) > 8:
                    self._cache.popitem(last=False)
                if image is not None:
                    self._thumbnails[key] = image
                    while len(self._thumbnails) > 256:
                        self._thumbnails.popitem(last=False)
            self.thumbnail_ready.emit((worker.path, key, image, error))
            if key == getattr(self, '_key', None):
                self._present(mesh, error)
            self._schedule()

        def _present(self, mesh, error):
            self.mesh, self.loading = mesh, False
            self.canvas.set_mesh(mesh, 'Keine Vorschau verfügbar' if error else 'Kein Mesh gefunden')
            if mesh is None:
                self.details_label.setText('Vorschau nicht verfügbar')
                self.details_label.setToolTip(error)
                self.canvas.setToolTip(error)
            else:
                amount = f'{mesh.source_triangle_count:,}'.replace(',', '.')
                self.details_label.setText(f'{mesh.mesh_count} Mesh · {amount} Dreiecke')
                self.details_label.setToolTip('\n'.join(mesh.model_names + mesh.warnings) or 'Vereinfachte Geometrievorschau ohne Material und Texturen.')
                self.canvas.setToolTip('Linke Maustaste ziehen: Mesh drehen. Mausrad: vergrößern oder verkleinern. Doppelklick: Ansicht zurücksetzen. Die Vorschau zeigt vereinfachte Geometrie ohne Texturen und verändert die Szene nicht.')
            self._refresh_large()

        def clear(self):
            self.set_sources([])
            self.set_source(None)
            self.name_edit.clear()
            self.name_edit.setEnabled(False)

        def show_large(self):
            if self._closed or self.current_path is None:
                return
            if self._large is None:
                self._large = Qt.QDialog(self)
                self._large.resize(760, 700)
                box = Qt.QVBoxLayout(self._large)
                self._large_canvas = MeshCanvas(self._large)
                box.addWidget(self._large_canvas, 1)
                reset = Qt.QPushButton('Ansicht')
                reset.setToolTip(self.reset_button.toolTip())
                reset.clicked.connect(self._large_canvas.reset_view)
                box.addWidget(reset)
            self._refresh_large()
            self._large.show()
            self._large.raise_()
            self._large.activateWindow()

        def _refresh_large(self):
            if self._large is not None:
                self._large.setWindowTitle('Vorschau – ' + (self.current_path.name if self.current_path is not None else 'FBX'))
                self._large_canvas.set_mesh(self.mesh, self.canvas._message)
                self._large_canvas.setToolTip(self.canvas.toolTip())

        def shutdown(self):
            self._closed = True
            self._token += 1
            self._timer.stop()
            self._pending.clear()
            self._cancel_workers()
            for worker in self._workers.values():
                try:
                    worker.signals.completed.disconnect(self._loaded)
                except (RuntimeError, TypeError):
                    pass
            if self._large is not None:
                self._large.close()

    return MeshPreviewPanel
