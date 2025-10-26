from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from moteurs.youtube import FormatInfo, download, ensure_output_dir, probe_formats


@dataclass(slots=True)
class DisplayFormat:
    format_info: FormatInfo

    def to_row(self) -> List[str]:
        size = (
            f"{self.format_info.filesize / (1024 ** 2):.1f} MB"
            if self.format_info.filesize
            else "?"
        )
        fps = f"{int(self.format_info.fps)} fps" if self.format_info.fps else ""
        return [
            self.format_info.format_id,
            f"{self.format_info.resolution} ({self.format_info.ext})".strip(),
            fps,
            self.format_info.vcodec,
            self.format_info.acodec,
            size,
        ]


class AnalysisWorker(QObject):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    @Slot()
    def run(self) -> None:
        try:
            formats = probe_formats(self._url)
        except Exception as exc:  # pragma: no cover - handled via UI
            self.error.emit(str(exc))
            return
        self.finished.emit(formats)


class DownloadWorker(QObject):
    progress = Signal(int)
    status = Signal(str)
    speed = Signal(str)
    eta = Signal(str)
    error = Signal(str)
    done = Signal(Path)

    def __init__(self, url: str, format_id: Optional[str]) -> None:
        super().__init__()
        self._url = url
        self._format_id = format_id

    @Slot()
    def run(self) -> None:
        def progress_hook(data: dict) -> None:
            status = data.get("status", "")
            if status:
                self.status.emit(str(status))
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            if total:
                percent = int(downloaded * 100 / total)
                self.progress.emit(percent)
            speed = data.get("speed")
            if speed:
                self.speed.emit(self._human_readable_rate(float(speed)))
            eta = data.get("eta")
            if eta is not None:
                self.eta.emit(self._format_eta(int(eta)))

        try:
            file_path = download(self._url, self._format_id, None, progress_hook)
        except Exception as exc:  # pragma: no cover - handled via UI
            self.error.emit(str(exc))
            return
        self.status.emit("finished")
        self.progress.emit(100)
        self.speed.emit("")
        self.eta.emit("")
        self.done.emit(file_path)

    @staticmethod
    def _human_readable_rate(rate: float) -> str:
        units = ["B/s", "KB/s", "MB/s", "GB/s"]
        index = 0
        while rate >= 1024 and index < len(units) - 1:
            rate /= 1024
            index += 1
        return f"{rate:.1f} {units[index]}"

    @staticmethod
    def _format_eta(seconds: int) -> str:
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}h {minutes:02d}m {secs:02d}s"
        if minutes:
            return f"{minutes:d}m {secs:02d}s"
        return f"{secs:d}s"


class YouTubeTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._analysis_thread: Optional[QThread] = None
        self._download_thread: Optional[QThread] = None
        self._formats: List[DisplayFormat] = []

        self._build_ui()
        ensure_output_dir()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        input_layout = QHBoxLayout()
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText("https://www.youtube.com/watch?v=...")
        input_layout.addWidget(QLabel("URL:"))
        input_layout.addWidget(self.url_edit)

        self.analyse_button = QPushButton("Analyser", self)
        self.analyse_button.clicked.connect(self._trigger_analysis)
        input_layout.addWidget(self.analyse_button)

        layout.addLayout(input_layout)

        self.table = QTableWidget(self)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Format", "Résolution", "FPS", "VCodec", "ACodec", "Taille"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemSelectionChanged.connect(self._handle_selection_change)
        layout.addWidget(self.table)

        controls_layout = QHBoxLayout()
        self.download_button = QPushButton("Télécharger", self)
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._trigger_download)
        controls_layout.addWidget(self.download_button)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        controls_layout.addWidget(self.progress_bar)

        layout.addLayout(controls_layout)

        status_layout = QHBoxLayout()
        self.status_label = QLabel("En attente", self)
        self.speed_label = QLabel("", self)
        self.eta_label = QLabel("", self)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.speed_label)
        status_layout.addStretch()
        status_layout.addWidget(self.eta_label)
        layout.addLayout(status_layout)

        self.open_button = QPushButton("Ouvrir dossier", self)
        self.open_button.clicked.connect(self._open_output_folder)
        layout.addWidget(self.open_button, alignment=Qt.AlignmentFlag.AlignRight)

    @Slot()
    def _trigger_analysis(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "URL manquante", "Veuillez saisir une URL YouTube.")
            return
        self.analyse_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.status_label.setText("Analyse en cours...")
        self.progress_bar.setValue(0)
        self.speed_label.setText("")
        self.eta_label.setText("")
        self.table.setRowCount(0)
        self._formats = []

        self._analysis_thread = QThread(self)
        worker = AnalysisWorker(url)
        worker.moveToThread(self._analysis_thread)
        self._analysis_thread.started.connect(worker.run)
        worker.finished.connect(self._analysis_finished)
        worker.error.connect(self._analysis_error)
        worker.finished.connect(self._analysis_thread.quit)
        worker.error.connect(self._analysis_thread.quit)
        self._analysis_thread.finished.connect(worker.deleteLater)
        self._analysis_thread.finished.connect(self._cleanup_analysis_thread)
        self._analysis_thread.start()

    @Slot(list)
    def _analysis_finished(self, formats: List[FormatInfo]) -> None:
        self.analyse_button.setEnabled(True)
        if not formats:
            self.status_label.setText("Aucun format disponible")
            QMessageBox.information(self, "Formats", "Aucun format vidéo disponible.")
            return
        self.status_label.setText("Analyse terminée")
        self._formats = [DisplayFormat(fmt) for fmt in formats]
        self.table.setRowCount(len(self._formats))
        for row, display_format in enumerate(self._formats):
            for column, value in enumerate(display_format.to_row()):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, display_format.format_info.format_id)
                self.table.setItem(row, column, item)
        self._select_default_format()
        self._handle_selection_change()

    @Slot(str)
    def _analysis_error(self, message: str) -> None:
        self.analyse_button.setEnabled(True)
        self.status_label.setText("Erreur d'analyse")
        QMessageBox.critical(self, "Erreur", message)

    def _select_default_format(self) -> None:
        preferred_index: Optional[int] = None
        for index, display_format in enumerate(self._formats):
            info = display_format.format_info
            if info.ext.lower() == "mp4" and (
                "avc" in info.vcodec.lower() or "h264" in info.vcodec.lower()
            ):
                preferred_index = index
                break
        if preferred_index is None and self._formats:
            preferred_index = 0
        if preferred_index is not None:
            self.table.selectRow(preferred_index)

    @Slot()
    def _trigger_download(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "URL manquante", "Veuillez saisir une URL YouTube.")
            return
        selected_format = self._selected_format_id()
        self.download_button.setEnabled(False)
        self.status_label.setText("Téléchargement en cours...")
        self.progress_bar.setValue(0)
        self.speed_label.setText("")
        self.eta_label.setText("")

        self._download_thread = QThread(self)
        worker = DownloadWorker(url, selected_format)
        worker.moveToThread(self._download_thread)
        self._download_thread.started.connect(worker.run)
        worker.progress.connect(self.progress_bar.setValue)
        worker.status.connect(self._update_status)
        worker.speed.connect(self.speed_label.setText)
        worker.eta.connect(self.eta_label.setText)
        worker.error.connect(self._download_error)
        worker.done.connect(self._download_finished)
        worker.done.connect(self._download_thread.quit)
        worker.error.connect(self._download_thread.quit)
        self._download_thread.finished.connect(worker.deleteLater)
        self._download_thread.finished.connect(self._cleanup_download_thread)
        self._download_thread.start()

    @Slot(str)
    def _update_status(self, status: str) -> None:
        if status == "downloading":
            self.status_label.setText("Téléchargement...")
        elif status == "finished":
            self.status_label.setText("Terminé")
        elif status:
            self.status_label.setText(status)

    @Slot(Path)
    def _download_finished(self, path: Path) -> None:
        self.download_button.setEnabled(bool(self.table.selectedItems()))
        QMessageBox.information(
            self,
            "Téléchargement terminé",
            f"Fichier enregistré dans :\n{path}",
        )

    @Slot(str)
    def _download_error(self, message: str) -> None:
        self.download_button.setEnabled(bool(self.table.selectedItems()))
        self.status_label.setText("Erreur de téléchargement")
        QMessageBox.critical(self, "Erreur", message)

    def _cleanup_analysis_thread(self) -> None:
        if self._analysis_thread is not None:
            self._analysis_thread.deleteLater()
            self._analysis_thread = None

    def _cleanup_download_thread(self) -> None:
        if self._download_thread is not None:
            self._download_thread.deleteLater()
            self._download_thread = None

    def _selected_format_id(self) -> Optional[str]:
        selected_items = self.table.selectedItems()
        if not selected_items:
            return None
        return selected_items[0].data(Qt.ItemDataRole.UserRole)

    @Slot()
    def _handle_selection_change(self) -> None:
        self.download_button.setEnabled(bool(self.table.selectedItems()))

    @Slot()
    def _open_output_folder(self) -> None:
        folder = ensure_output_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Téléchargements vidéo")
        layout = QVBoxLayout(self)

        tabs = QTabWidget(self)
        tabs.addTab(YouTubeTab(), "YouTube")
        tabs.addTab(QWidget(), "1")
        tabs.addTab(QWidget(), "2")
        tabs.addTab(QWidget(), "3")

        layout.addWidget(tabs)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(900, 600)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
