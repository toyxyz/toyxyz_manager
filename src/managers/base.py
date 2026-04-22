import os
import time
import logging
from typing import Dict, Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTreeWidget, QTreeWidgetItem, 
    QLabel, QPushButton, QComboBox, QLineEdit, QMessageBox, QAbstractItemView,
    QFileDialog, QApplication, QFormLayout
)
from PySide6.QtCore import Qt, QThread, QSize, QTimer
from PySide6.QtGui import QIcon, QPixmap, QImage

from ..workers import FileScannerWorker, ThumbnailWorker, FileSearchWorker, ImageLoader
from ..ui_components import ZoomWindow, MarkdownNoteWidget
from .example import ExampleTabWidget
from ..core import VIDEO_EXTENSIONS, PREVIEW_EXTENSIONS, calculate_structure_path

class WrappingLabel(QLabel):
    """QLabel that wraps text without pushing parent layout wider."""
    def minimumSizeHint(self):
        sh = super().minimumSizeHint()
        return QSize(0, sh.height())

    def setText(self, text):
        # Insert zero-width space after path separators to allow wrapping
        if text:
            text = text.replace("\\", "\\\u200b").replace("/", "/\u200b").replace("_", "_\u200b")
        super().setText(text)

class SortableTreeItem(QTreeWidgetItem):
    def __lt__(self, other):
        # 1. Always prioritize Folders over Files
        # 'folder' < 'file' ?
        # We want folders to appear FIRST. 
        # In Ascending order, we want Small < Large. So Folder < File.
        # In Descending order, QTreeWidget reverses the result. So File < Folder?
        # This means in Descending, Files would come first.
        # To strictly keep folders on top is tricky without custom proxy model.
        # For now, let's just make sure "Folder" < "File" so in standard Ascending sort (default), it works.
        
        my_type = self.data(0, Qt.UserRole + 1)
        other_type = other.data(0, Qt.UserRole + 1)
        
        if my_type != other_type:
            # If I am folder (0) and other is file (1)
            # We want me < other -> True
            # But if the current SortOrder is Descending, Qt will reverse the return value.
            # To ALWAYS keep folders on top regardless of Ascending/Descending, we must adjust.
            tree = self.treeWidget()
            if tree and tree.header().sortIndicatorOrder() == Qt.DescendingOrder:
                # In descending, Qt reverses our result. To keep folder falling 'first', return False.
                return my_type != "folder"
            return my_type == "folder"
            
        tree = self.treeWidget()
        column = tree.sortColumn() if tree else 0

        # Sort by Size
        if column == 1:
            size1 = self.data(0, Qt.UserRole + 2) or 0
            size2 = other.data(0, Qt.UserRole + 2) or 0
            if size1 != size2:
                return size1 < size2
        # Sort by Date
        elif column == 2:
            time1 = self.data(0, Qt.UserRole + 3) or 0
            time2 = other.data(0, Qt.UserRole + 3) or 0
            if time1 != time2:
                return time1 < time2
            
        # Fallback to column text sorting (Format or Name)
        t1 = self.text(column).lower()
        t2 = other.text(column).lower()
        if t1 != t2:
            return t1 < t2
            
        # Fallback to Name sorting if current column values are equal
        return self.text(0).lower() < other.text(0).lower()

class BaseManagerWidget(QWidget):
    def __init__(self, directories: Dict[str, Any], extensions, app_settings: Dict[str, Any] = None):
        super().__init__()
        self.directories = directories
        self.extensions = extensions
        self.app_settings = app_settings or {}
        self.current_path = None
        self.active_scanners = []
        self._cancelled_workers = set() # [Fix] Safely hold references to stopping workers
        self._thumb_pending = {} # [Optimize] Track pending thumbnails
        self.image_loader_thread = ImageLoader()
        self.image_loader_thread.image_loaded.connect(self._on_thumbnail_loaded)
        self.image_loader_thread.start()
        
        # [Optimize] Thumbnail lazy loading debounce timer
        self.thumb_scroll_timer = QTimer(self)
        self.thumb_scroll_timer.setSingleShot(True)
        self.thumb_scroll_timer.setInterval(100) # [Optimize] Reduced debounce for snappier response
        self.thumb_scroll_timer.timeout.connect(self._load_visible_thumbnails)
        
        # [Feature] Thumbnail Size Setting
        self.thumb_size = int(self.app_settings.get("thumbnail_size", 64))
        
        self._init_base_ui()
        self.update_combo_list()
        
    # ... (rest of class)

    def _cancel_worker(self, worker, disconnect_signals=False):
        if not worker: return
        try:
            if worker.isRunning():
                worker.stop()
                if disconnect_signals:
                    try: worker.batch_ready.disconnect()
                    except Exception: pass
                    try: worker.finished.disconnect()
                    except Exception: pass
                self._cancelled_workers.add(worker)
                worker.finished.connect(lambda w=worker: self._cancelled_workers.discard(w))
        except RuntimeError: pass

    def get_debug_info(self) -> Dict[str, Any]:
        """Returns debug statistics for the manager."""
        info = {
            "scanners_active": len(self.active_scanners),
            "search_active": hasattr(self, 'search_worker') and self.search_worker and self.search_worker.isRunning(),
            "loader_queue": len(self.image_loader_thread.queue),
            "tree_items": self.tree.topLevelItemCount()
        }
        return info

    @staticmethod
    def format_size(size_bytes):
        if size_bytes >= 1073741824: return f"{size_bytes / 1073741824:.2f} GB"
        elif size_bytes >= 1048576: return f"{size_bytes / 1048576:.2f} MB"
        elif size_bytes >= 1024: return f"{size_bytes / 1024:.2f} KB"
        return f"{size_bytes} B"

    @staticmethod
    def format_date(mtime, seconds=False):
        if mtime <= 0: return "-"
        fmt = '%Y-%m-%d %H:%M:%S' if seconds else '%Y-%m-%d %H:%M'
        return time.strftime(fmt, time.localtime(mtime))

    @staticmethod
    def _is_path_within(parent_path, child_path):
        """Return True when child_path is inside parent_path, handling Windows drive mismatches."""
        try:
            parent = os.path.normcase(os.path.abspath(os.path.normpath(parent_path)))
            child = os.path.normcase(os.path.abspath(os.path.normpath(child_path)))
            return os.path.commonpath([parent, child]) == parent
        except (OSError, ValueError):
            return False

    @staticmethod
    def _is_associated_sibling(filename, base_name):
        """Match normal sidecars and compound preview names like '<base>.preview.png'."""
        if os.path.splitext(filename)[0] == base_name:
            return True

        filename_lower = filename.lower()
        base_lower = base_name.lower()
        return any(filename_lower == f"{base_lower}{ext.lower()}" for ext in PREVIEW_EXTENSIONS)

    def save_note_for_path(self, path, text, silent=False):
        if not path: return
        try:
            filename = os.path.basename(path)
            model_name = os.path.splitext(filename)[0]
            # [Fix] Added mode argument
            cache_dir = calculate_structure_path(path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            md_path = os.path.join(cache_dir, model_name + ".md")
            
            # [FIX] Create directory if it doesn't exist
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir, exist_ok=True)
            
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(text)
                
            if not silent:
                self.show_status_message("Note saved (.md).")
        except Exception as e: 
            logging.error(f"Save Error: {e}")
            self.show_status_message(f"Save Failed: {e}")

    def _init_base_ui(self):
        main_layout = QVBoxLayout(self)
        self.splitter = QSplitter(Qt.Horizontal)
        # self.splitter.setStyleSheet(...) -> Moved to QSS
        
        # [Left Panel] 
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0,0,0,0)
        
        combo_box = QHBoxLayout()
        self.folder_combo = QComboBox()
        self.folder_combo.currentIndexChanged.connect(self.refresh_list)
        btn_refresh = QPushButton("🔄")
        btn_refresh.setToolTip("Refresh file list")
        btn_refresh.clicked.connect(self.refresh_list)
        combo_box.addWidget(self.folder_combo, 1)
        combo_box.addWidget(btn_refresh)
        
        # [Search UI]
        search_layout = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("🔍 Search... (Enter)")
        self.filter_edit.returnPressed.connect(self.search_files)
        
        self.btn_search = QPushButton("Search")
        self.btn_search.setToolTip("Search files in the current directory (Recursive)")
        self.btn_search.clicked.connect(self.search_files)
        
        self.btn_search_back = QPushButton("⬅️ Back")
        self.btn_search_back.setToolTip("Return to full list (Clear search)")
        self.btn_search_back.setEnabled(False) # Default hidden/disabled
        self.btn_search_back.clicked.connect(self.cancel_search)
        
        search_layout.addWidget(self.filter_edit)
        search_layout.addWidget(self.btn_search)
        search_layout.addWidget(self.btn_search_back)
        
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size", "Date", "Format"])
        if self.thumb_size > 0:
            self.tree.setIconSize(QSize(self.thumb_size, self.thumb_size)) # [Optimize] Set dynamic thumbnail size
        self.tree.setColumnWidth(0, 200) 
        self.tree.setColumnWidth(1, 70)  
        self.tree.setColumnWidth(2, 110) 
        self.tree.setColumnWidth(3, 70)
        # self.tree.setStyleSheet(...) -> Moved to QSS
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSortingEnabled(True) # [Fix] Enable Sorting
        self.tree.sortByColumn(0, Qt.AscendingOrder) # Default sort by Name
        self.tree.itemSelectionChanged.connect(self.on_tree_select)
        self.tree.itemExpanded.connect(self.on_tree_expand)
        self.tree.verticalScrollBar().valueChanged.connect(self._on_tree_scrolled) # [Optimize] Lazy load hook
        
        left_layout.addLayout(combo_box)
        left_layout.addLayout(search_layout)
        left_layout.addWidget(self.tree)
        
        # Hook for additional left-side widgets (e.g. New File button)
        self.init_left_bottom(left_layout)
        
        self.splitter.addWidget(left_panel)
        
        # [Center Panel] - To be filled by subclasses
        self.center_panel = QWidget()
        self.center_layout = QVBoxLayout(self.center_panel)
        self.init_center_panel()
        self.splitter.addWidget(self.center_panel)
        
        # [Right Panel] - To be filled by subclasses
        self.right_panel = QWidget()
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(0,0,0,0)
        self.init_right_panel()
        self.splitter.addWidget(self.right_panel)
        
        self.splitter.setSizes([450, 500, 400])
        main_layout.addWidget(self.splitter)

    # Hooks for subclasses
    def init_center_panel(self): pass
    def init_right_panel(self): pass
    def init_left_bottom(self, layout): pass
    def on_tree_select(self): pass
    
    def _setup_info_panel(self, extra_fields: list = None):
        """Helper to create standard info panel (Name, Size, Path, Date + Extras)."""
        extra_fields = extra_fields or []
        # Standard fields: Name is always first. Size, Path, Date are always last.
        # Extras inserted in between.
        target_fields = ["Name"] + extra_fields + ["Size", "Path", "Date"]
        
        self.info_labels = {}
        form_layout = QFormLayout()
        
        for k in target_fields:
            l = WrappingLabel("-")
            l.setWordWrap(True)
            self.info_labels[k] = l
            form_layout.addRow(f"{k}:", l)
            
        # Duplicate Warning
        self.lbl_duplicate_warning = QLabel("")
        self.lbl_duplicate_warning.setObjectName("DuplicateWarning")
        self.lbl_duplicate_warning.setWordWrap(True)
        self.lbl_duplicate_warning.hide()
        form_layout.addRow(self.lbl_duplicate_warning)
        
        self.center_layout.addLayout(form_layout)

    # Hook for getting current mode, defaulted to 'model' if not overridden
    def get_mode(self): return "model"

    def set_directories(self, directories):
        """Updates the directories and refreshes the combo box."""
        self.directories = directories
        self.update_combo_list()

    def update_combo_list(self):
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        # Subclasses should filter directories by mode if needed, 
        # but here we might just show all or let subclass handle it.
        # Actually, let's make it data-driven. The passed `directories` 
        # should only contain the relevant ones for this mode.
        self.folder_combo.addItems(list(self.directories.keys()))
        self.folder_combo.blockSignals(False)
        if self.directories: self.refresh_list()

    def get_scanner_filter_mode(self):
        """Hook for subclasses to provide a specific filter mode to the background scanner."""
        return None

    def refresh_list(self):
        if self.folder_combo.count() == 0: return
        name = self.folder_combo.currentText()
        data = self.directories.get(name)
        if not data: return
        
        raw_path = data.get("path") if isinstance(data, dict) else data
        # [Fix] Normalize path here to ensure consistency with worker and popup logic
        path = os.path.normpath(raw_path)
        
        if hasattr(self, 'indexing_scanner'):
            self._cancel_worker(self.indexing_scanner)

        # [Fix] Stop all active partial scanners to prevent zombie signals
        if hasattr(self, 'active_scanners'):
            for scanner in list(self.active_scanners):
                self._cancel_worker(scanner)
            self.active_scanners.clear()

        # [Fix] Stop main UI scanner if running
        if hasattr(self, 'scanner'):
            self._cancel_worker(self.scanner, disconnect_signals=True)

        self._thumb_pending.clear()
        self.tree.clear()
        self.filter_edit.clear()
        
        # [Duplicate Check] Initialize File Map
        # Key: filename (lowercase), Value: list of full paths
        self.file_map = {} 
        
        # [Thread Safety] Track active thumbnail workers
        self.active_thumb_workers = set()
        
        filter_mode = self.get_scanner_filter_mode()
        
        # 1. UI Scanner (Fast, Non-Recursive)
        self.tree.setSortingEnabled(False) # [Optimization] Disable sorting for entire scan
        self.scanner = FileScannerWorker(path, self.extensions, recursive=False, filter_mode=filter_mode)
        self.scanner.batch_ready.connect(self._on_batch_ready)
        self.scanner.finished.connect(self._on_scan_finished) # [Optimization] New slot
        self.scanner.finished.connect(self.scanner.deleteLater) 
        self.scanner.start()
        
        # 2. Indexing Scanner (Background, Recursive for full duplicate check)
        # [Feature] Only start if "show_duplicates" setting is enabled
        if self.app_settings.get("show_duplicates", False):
            self.indexing_scanner = FileScannerWorker(path, self.extensions, recursive=True, filter_mode=filter_mode)
            self.indexing_scanner.setObjectName("IndexingScannerThread")
            self.indexing_scanner.batch_ready.connect(self._on_indexing_batch_ready)
            self.indexing_scanner.finished.connect(self.indexing_scanner.deleteLater)
            self.indexing_scanner.start()
            # [Optimization] Low priority for background indexing to prevent UI jank
            self.indexing_scanner.setPriority(QThread.LowPriority)
        else:
            # [Fix] Explicitly hide stale duplicate warning when setting is off
            if hasattr(self, 'lbl_duplicate_warning'):
                self.lbl_duplicate_warning.hide()
        
        # Disable Back button when in normal list view
        if hasattr(self, 'btn_search_back'):
            self.btn_search_back.setEnabled(False)

    def _on_batch_ready(self, current_dir, dirs, files):
        self.tree.setUpdatesEnabled(False)
        
        # Find the parent item for 'current_dir'
        # Since this is the initial scan (non-recursive), current_dir SHOULD be the root path
        # But if we change it to recursive later, we need to find the item.
        
        # Check if current_dir matches the root
        name = self.folder_combo.currentText()
        data = self.directories.get(name)
        raw_path = data.get("path") if isinstance(data, dict) else data
        base_path = os.path.normpath(raw_path)
        
        parent_item = self.tree.invisibleRootItem()
        
        # If the batch is for a subdirectory (not currently supported in initial refresh but good for safety)
        if os.path.normpath(current_dir) != base_path:
             # Find item by path... (Optimization: Too slow for large trees?)
             # Since initial scan is recursive=False, we always populate root.
             pass
        
        # [Optimization] Sorting is disabled globally during scan
        # self.tree.setSortingEnabled(False) 
        
        # Construct data dict as expected by _populate_item
        root_data = {"dirs": dirs, "files": files}
        self._populate_item(parent_item, current_dir, root_data)

        self.tree.setUpdatesEnabled(True)
        # [Optimization] Sorting re-enabled only at end of scan
        # self.tree.setSortingEnabled(True)

    def _on_scan_finished(self):
        """Called when INITIAL UI scan is complete."""
        self.tree.setSortingEnabled(True)
        self._load_visible_thumbnails() # [Optimize] Initial load
        # self.show_status_message(f"Scan complete. {self.tree.topLevelItemCount()} items.")

    def _filter_directories_by_extension(self, parent_path, dirs):
        """Hook for subclasses to filter the list of directories before they are added to the tree."""
        return dirs

    def _filter_files_by_extension(self, parent_path, files):
        """Hook for subclasses to filter the list of files before they are added to the tree."""
        return files

    def _populate_item(self, parent_item, current_path, data):
        # ... (Unchanged logic, just ensure no sorting calls here)
        # 1. Add Folders
        dirs = data.get("dirs", [])
        
        # Apply subclass-specific filtering
        dirs = self._filter_directories_by_extension(current_path, dirs)
        
        # Sort folders by name
        dirs.sort(key=lambda s: s.lower())
        
        for d_name in dirs:
            d_path = os.path.join(current_path, d_name)
            d_item = SortableTreeItem(parent_item) # [Fix] Use SortableItem
            d_item.setText(0, f"📁 {d_name}")
            d_item.setData(0, Qt.UserRole, d_path)
            d_item.setData(0, Qt.UserRole + 1, "folder")
            
            # Add Dummy Item to enable expansion
            dummy = QTreeWidgetItem(d_item) # Dummy doesn't need to be sortable, or maybe yes?
            dummy.setText(0, "Loading...")
            dummy.setData(0, Qt.UserRole, "DUMMY")

        # 2. Add Files
        files = data.get("files", [])
        
        # Apply subclass-specific filtering
        files = self._filter_files_by_extension(current_path, files)
        
        # Files are already sorted or we can sort here
        files.sort(key=lambda x: x['name'].lower())
        
        for f in files:
            f_item = SortableTreeItem(parent_item) # [Fix] Use SortableItem
            f_item.setText(0, f['name'])
            f_item.setText(1, f['size'])
            f_item.setText(2, f['date'])
            ext = os.path.splitext(f['name'])[1].lower()
            f_item.setText(3, ext)
            f_item.setData(0, Qt.UserRole, f['path'])
            f_item.setData(0, Qt.UserRole + 1, "file")
            f_item.setData(0, Qt.UserRole + 2, f.get('raw_size', 0))
            f_item.setData(0, Qt.UserRole + 3, f.get('raw_date', 0))
            
            # [Duplicate Check] Update Global File Map (Initial visible items)
            f_name_lower = f['name'].lower()
            if f_name_lower not in self.file_map:
                self.file_map[f_name_lower] = []
            if f['path'] not in self.file_map[f_name_lower]:
                self.file_map[f_name_lower].append(f['path'])

    def _on_indexing_batch_ready(self, root, dirs, files):
        """Background worker updates the file map for full duplicate detection."""
        for f in files:
            f_name_lower = f['name'].lower()
            f_path = f['path']
            
            if f_name_lower not in self.file_map:
                self.file_map[f_name_lower] = []
            
            if f_path not in self.file_map[f_name_lower]:
                self.file_map[f_name_lower].append(f_path)
        
        # If currently selected item has duplicates, update warning immediately
        if self.current_path:
            cur_name = os.path.basename(self.current_path).lower()
            if cur_name in self.file_map and len(self.file_map[cur_name]) > 1:
                # Trigger re-selection logic to refresh warning
                # We can call on_tree_select manually or just update warning if we refactor warning logic.
                # For now, let's just re-simulate selection if it's the current item
                # But on_tree_select expects an item.
                # Simpler: Update the warning label directly if method exists (it's in subclass)
                # Or verify if we can call something generic.
                # Let's check subclasses... or just rely on user re-clicking? 
                # Better: Emit a signal or call a refresh method.
                self._refresh_duplicate_warning()

    def _refresh_duplicate_warning(self):
        if self.get_mode() == "gallery": return
        if not self.app_settings.get("show_duplicates", False): return

        # Subclasses can override or we implement generic if label is standard
        # ModelManagerWidget has lbl_duplicate_warning
        if hasattr(self, 'lbl_duplicate_warning') and self.current_path:
             f_name = os.path.basename(self.current_path).lower()
             duplicates = self.file_map.get(f_name, [])
             if len(duplicates) > 1:
                 # Exclude current path (More robust comparison)
                 curr_norm = os.path.normcase(os.path.abspath(self.current_path))
                 other_paths = [p for p in duplicates if os.path.normcase(os.path.abspath(p)) != curr_norm]
                 
                 msg = f"⚠️ Duplicate Found ({len(duplicates)})"
                 if other_paths:
                     msg += "\n" + "\n".join(other_paths)
                 
                 tooltip = "Same filename detected in:\n" + "\n".join(duplicates)
                 self.lbl_duplicate_warning.setText(msg)
                 self.lbl_duplicate_warning.setToolTip(tooltip)
                 self.lbl_duplicate_warning.show()
             else:
                 self.lbl_duplicate_warning.hide()

    def on_tree_expand(self, item):
        # Check if it has a dummy child
        if item.childCount() == 1 and item.child(0).data(0, Qt.UserRole) == "DUMMY":
            # Remove dummy
            item.takeChild(0)
            
            path = item.data(0, Qt.UserRole)
            if not path or not os.path.isdir(path): return
            
            self.tree.setSortingEnabled(False) # [Optimization] Disable sort for lazy load
            
            worker = FileScannerWorker(path, self.extensions, recursive=False, filter_mode=self.get_scanner_filter_mode())
            # Connect to batch signal, reusing the logic to populate THIS item
            worker.batch_ready.connect(lambda p, d, f: self._on_partial_batch_ready(item, p, d, f))
            worker.finished.connect(lambda: self.tree.setSortingEnabled(True)) # [Optimization] Re-enable
            worker.finished.connect(worker.deleteLater) # Cleanup thread
            
            # [Fix] Remove from active list when done to prevent accessing deleted objects
            worker.finished.connect(lambda: self.active_scanners.remove(worker) if worker in self.active_scanners else None)
            
            self.active_scanners.append(worker)
            worker.start()

    def _on_partial_batch_ready(self, parent_item, current_path, dirs, files):
        # [Fix] Critical Crash Prevention:
        # If the parent_item (QTreeWidgetItem) has been deleted by a refresh/clear operation
        # while this signal was in flight, accessing it will raise RuntimeError.
        try:
             # Just checking if 'parent_item' is valid.
             # Accessing any method on a deleted C++ object raises RuntimeError.
             if not parent_item or parent_item.childCount() < 0: 
                 return
                 
             self.tree.setUpdatesEnabled(False)
             # self.tree.setSortingEnabled(False) # [Optimization] Handled in on_tree_expand
            
             root_data = {"dirs": dirs, "files": files}
             self._populate_item(parent_item, current_path, root_data)
            
             self.tree.setUpdatesEnabled(True)
             self.thumb_scroll_timer.start() # [Optimize] trigger thumbnail load on expand
             # self.tree.setSortingEnabled(True) # [Optimization] Handled in on_tree_expand finished
        except RuntimeError:
             # "wrapped C/C++ object of type SortableTreeItem has been deleted"
             # This is expected during rapid refreshes. Ignore.
             pass


    # _on_partial_scan_finished REMOVED (Replaced by _on_partial_batch_ready)

    def search_files(self):
        query = self.filter_edit.text().strip()
        if not query:
            self.refresh_list()
            return

        name = self.folder_combo.currentText()
        if not name: return
        data = self.directories.get(name)
        
        raw_path = data.get("path") if isinstance(data, dict) else data
        root_path = os.path.normpath(raw_path)

        if hasattr(self, 'scanner'):
            self._cancel_worker(self.scanner, disconnect_signals=True)

        if hasattr(self, 'search_worker'):
            self._cancel_worker(self.search_worker, disconnect_signals=True)

        self._thumb_pending.clear()
        self.tree.clear()
        
        # Loading Indicator
        loading = QTreeWidgetItem(self.tree)
        loading.setText(0, "Searching...")
        
        self.filter_edit.setEnabled(False)
        self.btn_search.setEnabled(False)
        if hasattr(self, 'btn_search_back'): self.btn_search_back.setEnabled(False)
        
        self.search_worker = FileSearchWorker(root_path, query, self.extensions)
        self.search_worker.finished.connect(self._on_search_finished)
        self.search_worker.finished.connect(self.search_worker.deleteLater) # Cleanup thread
        self.search_worker.start()

    def _on_search_finished(self, results):
        self.filter_edit.setEnabled(True)
        self.btn_search.setEnabled(True)
        if hasattr(self, 'btn_search_back'):
            self.btn_search_back.setEnabled(True)
        self.tree.clear()
        
        if not results:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, "No results found.")
            return
            
        # [Safety] Cap results to prevent UI freeze
        total_found = len(results)
        if total_found > 2000:
            results = results[:2000]
            self.show_status_message(f"Search results capped to 2000 items (found {total_found})")

        # Sort by name
        results.sort(key=lambda x: os.path.basename(x[0]).lower())
        
        for item_data in results:
            # Handle both old (2 items) and new (4 items) formats for safety, though only new will be emitted
            path = item_data[0]
            
            # Unpack stats if available
            size_bytes = 0
            mtime = 0
            if len(item_data) >= 4:
                size_bytes = item_data[2]
                mtime = item_data[3]
            
            name = os.path.basename(path)
            item = SortableTreeItem(self.tree)
            item.setText(0, name)
            item.setToolTip(0, path) 
            
            # Format Size
            size_str = self.format_size(size_bytes)
            
            # Format Date
            date_str = self.format_date(mtime)

            item.setText(1, size_str) 
            item.setText(2, date_str)
            
            ext = os.path.splitext(name)[1].lower()
            item.setText(3, ext)
            
            item.setData(0, Qt.UserRole, path)
            item.setData(0, Qt.UserRole + 1, "file")
            item.setData(0, Qt.UserRole + 2, size_bytes)
            item.setData(0, Qt.UserRole + 3, mtime)
            
        self._load_visible_thumbnails() # [Optimize] Load search results

    def cancel_search(self):
        self.filter_edit.clear()
        self.refresh_list()

    def _on_tree_scrolled(self, value):
        self.thumb_scroll_timer.start()

    def _load_visible_thumbnails(self):
        """Lazy load thumbnails for items currently visible in the tree."""
        if self.thumb_size <= 0: return # [Optimize] Early exit if thumbnails are off
        if not self.tree.topLevelItemCount(): return
        
        # [Optimize] Drop previous trailing thumbnail requests on scroll
        self.image_loader_thread.clear_thumbnail_queue()
        self._thumb_pending.clear() # [Fix] Reset pending trackers so dropped items can be re-queued
        
        viewport = self.tree.viewport()
        rect = viewport.rect()
        
        visible_items = []
        item = self.tree.itemAt(rect.topLeft())
        if not item:
            for y in range(rect.top(), rect.bottom(), 20):
                item = self.tree.itemAt(2, y)
                if item: break
                
        while item:
            rect_item = self.tree.visualItemRect(item)
            if not rect_item.intersects(rect):
                if rect_item.top() > rect.bottom():
                    break
            else:
                visible_items.append(item)
            item = self.tree.itemBelow(item)

        from ..core import VIDEO_EXTENSIONS
        for item in visible_items:
            try:
                item_type = item.data(0, Qt.UserRole + 1)
                if item_type != "file": continue
                
                if not item.icon(0).isNull(): continue
                
                path = item.data(0, Qt.UserRole)
                if not path: continue
                
                ext = os.path.splitext(path)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    continue # Skip video thumbnails for performance
                
                # [Optimize] Skip if already pending
                if path not in self._thumb_pending:
                    self._thumb_pending[path] = []
                if item not in self._thumb_pending[path]:
                    self._thumb_pending[path].append(item)
                    # [Optimize] Delegate file existence check to background thread
                    self.image_loader_thread.load_image(path, target_width=self.thumb_size, 
                                                        clear_queue=False, resolve_preview=True)
            except RuntimeError:
                pass # Item deleted

    def _on_thumbnail_loaded(self, path, image):
        if image.isNull(): return
        items = self._thumb_pending.pop(path, [])
        if not items: return
        
        icon = QIcon(QPixmap.fromImage(image))
        for item in items:
            try:
                if item.treeWidget():
                    item.setIcon(0, icon)
                    
                    # [Fix] Dynamically adjust row height ONLY for items with a thumbnail
                    fm = self.tree.fontMetrics()
                    text_w = fm.horizontalAdvance(item.text(0)) if hasattr(fm, 'horizontalAdvance') else fm.boundingRect(item.text(0)).width()
                    # Add breathing room for text and prevent vertical cropping
                    item.setSizeHint(0, QSize(text_w + self.thumb_size + 30, self.thumb_size + 6))
            except RuntimeError:
                pass

    def apply_thumbnail_size(self):
        """Apply thumbnail size change from settings without restart."""
        new_size = int(self.app_settings.get("thumbnail_size", 64))
        self.thumb_size = new_size
        
        if new_size <= 0:
            self.tree.setIconSize(QSize(0, 0))
        else:
            self.tree.setIconSize(QSize(new_size, new_size))
        
        # Recursively clear all icons and row heights
        self._clear_all_icons()
        self._thumb_pending.clear()
        self.image_loader_thread.clear_thumbnail_queue()
        
        if new_size > 0:
            self._load_visible_thumbnails()

    def _clear_all_icons(self, parent_item=None):
        """Recursively clear icons and size hints from all tree items."""
        count = parent_item.childCount() if parent_item else self.tree.topLevelItemCount()
        for i in range(count):
            item = parent_item.child(i) if parent_item else self.tree.topLevelItem(i)
            if item:
                try:
                    item.setIcon(0, QIcon())
                    item.setSizeHint(0, QSize(-1, -1))
                    if item.childCount() > 0:
                        self._clear_all_icons(item)
                except RuntimeError:
                    pass

    def show_status_message(self, msg, duration=3000):
        if hasattr(self, 'parent_window') and self.parent_window:
            self.parent_window.statusBar().showMessage(msg, duration)
        else:
            logging.info(f"[Status] {msg}")

    def get_cache_dir(self):
        # Allow app_settings to define cache path, or fallback to default
        custom_path = ""
        if hasattr(self, 'app_settings'):
            custom_path = self.app_settings.get("cache_path", "").strip()
        
        if custom_path and os.path.isdir(custom_path):
            return custom_path
            
        from ..core import CACHE_DIR_NAME
        if not os.path.exists(CACHE_DIR_NAME):
            try: os.makedirs(CACHE_DIR_NAME)
            except OSError: pass
        return CACHE_DIR_NAME

    def replace_thumbnail(self):
        if not self.current_path: return
        
        filters = "Media (*.png *.jpg *.jpeg *.webp *.mp4 *.webm *.gif)"
        file_path, _ = QFileDialog.getOpenFileName(self, "Select New Thumbnail/Preview", "", filters)
        if not file_path: return
        
        base = os.path.splitext(self.current_path)[0]
        ext = os.path.splitext(file_path)[1].lower()
        target_path = base + ext
        
        if hasattr(self, 'btn_replace'): self.btn_replace.setEnabled(False)
        
        # Unload image to be safe against file locks
        if hasattr(self, 'preview_lbl'): self.preview_lbl.set_media(None)
        QApplication.processEvents()

        self.show_status_message("Processing thumbnail...")

        # [Fix] Remove existing preview files to ensure the new one takes precedence
        # (e.g., .mp4 takes priority over .jpg, so we must remove .mp4 if replacing with .jpg)
        from ..core import PREVIEW_EXTENSIONS
        try:
            for p_ext in PREVIEW_EXTENSIONS:
                p_path = base + p_ext
                if os.path.exists(p_path) and os.path.abspath(p_path) != os.path.abspath(target_path):
                    try: os.remove(p_path)
                    except OSError: pass
        except Exception as e:
            logging.warning(f"Cleanup error: {e}")
        
        is_video = (ext in VIDEO_EXTENSIONS)
        
        # [Fix] Invalidate cache for the target path to ensure UI updates
        if hasattr(self, 'image_loader_thread'):
            self.image_loader_thread.remove_from_cache(target_path)
            
        self.thumb_worker = ThumbnailWorker(file_path, target_path, is_video)
        self.thumb_worker.finished.connect(self._on_thumb_worker_finished)
        
        # [Thread Safety] Track worker
        if not hasattr(self, 'active_thumb_workers'): self.active_thumb_workers = set()
        self.active_thumb_workers.add(self.thumb_worker)
        self.thumb_worker.finished.connect(lambda: self._cleanup_thumb_worker(self.thumb_worker))
        
        self.thumb_worker.start()

    def _cleanup_thumb_worker(self, worker):
        if hasattr(self, 'active_thumb_workers') and worker in self.active_thumb_workers:
            self.active_thumb_workers.discard(worker)
        worker.deleteLater()

    def _on_thumb_worker_finished(self, success, msg):
        if hasattr(self, 'btn_replace'): self.btn_replace.setEnabled(True)
        self.show_status_message(msg)
        if success:
             # Refresh details - assumes subclasses implement _load_details
             if hasattr(self, '_load_details'): self._load_details(self.current_path)
        else:
             QMessageBox.warning(self, "Error", f"Failed: {msg}")

    def on_preview_click(self):
        if not hasattr(self, 'preview_lbl'): return
        path = self.preview_lbl.get_current_path()
        if path and os.path.exists(path) and os.path.splitext(path)[1].lower() not in VIDEO_EXTENSIONS:
            ZoomWindow(path, self).show()

    # === Shared Content Logic (Note/Example) ===
    
    def setup_content_tabs(self):
        """Initializes the standard Note and Example tabs."""
        from PySide6.QtWidgets import QTabWidget
        self.tabs = QTabWidget()
        
        # Tab 1: Note
        self.tab_note = MarkdownNoteWidget()
        self.tab_note.save_requested.connect(self.save_note)
        self.tab_note.set_media_handler(self.handle_media_insert)
        self.tabs.addTab(self.tab_note, "Note")
        
        # Tab 2: Example
        # [Refactor] Pass cache_root explicitely
        self.tab_example = ExampleTabWidget(self.directories, self.app_settings, self, self.image_loader_thread, cache_root=self.get_cache_dir(), mode=self.get_mode())
        self.tab_example.status_message.connect(self.show_status_message)
        self.tabs.addTab(self.tab_example, "Example")
        
        return self.tabs

    def load_content_data(self, path):
        """Loads note content from .md file and initializes examples."""
        if not path: return

        filename = os.path.basename(path)
        # [Fix] Added mode argument
        cache_dir = calculate_structure_path(path, self.get_cache_dir(), self.directories, mode=self.get_mode())
        model_name = os.path.splitext(filename)[0]
        md_path = os.path.join(cache_dir, model_name + ".md")
        
        note_content = ""
        if os.path.exists(md_path):
            try:
                with open(md_path, 'r', encoding='utf-8') as f:
                    note_content = f.read()
            except OSError: pass
            
        if hasattr(self, 'tab_note'):
            # [Fix] Set base path for relative image resolution
            self.tab_note.set_base_path(cache_dir)
            self.tab_note.set_text(note_content)

        if hasattr(self, 'tab_example'): self.tab_example.load_examples(path)

    def save_note(self, text):
        if not self.current_path: return
        self.save_note_for_path(self.current_path, text)

    def handle_media_insert(self, mtype):
        if not self.current_path: 
            QMessageBox.warning(self, "Error", "No item selected.")
            return None
            
        if mtype not in ["image", "video"]: return None
        
        filters = "Media (*.png *.jpg *.jpeg *.webp *.mp4 *.webm *.gif)"
        file_path, _ = QFileDialog.getOpenFileName(self, f"Select {mtype.title()}", "", filters)
        if not file_path: return None
        
        return self.copy_media_to_cache(file_path, self.current_path)

    def open_current_folder(self):
        if self.current_path:
            f = os.path.dirname(self.current_path)
            try: os.startfile(f)
            except OSError: pass

    # === Shared File Operations (Delete / Rename) ===
    
    def remove_associated_files(self, file_path):
        """
        [Refactor] Centralized logic to permanently delete a main file, its cache directory, 
        and any associated sibling files (like thumbnails or previews) that share the same base name.
        Returns: (success_bool, deleted_count, error_messages_list)
        """
        import shutil
        from ..core import calculate_structure_path
        
        if not file_path or not os.path.exists(file_path):
            return False, 0, ["Selection does not exist."]
            
        filename = os.path.basename(file_path)
        base_name = os.path.splitext(filename)[0]
        dir_path = os.path.dirname(file_path)
        
        deleted_items = []
        errors = []

        try:
            # 1. Delete Cache Directory
            cache_dir = calculate_structure_path(file_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            if os.path.exists(cache_dir):
                try:
                    shutil.rmtree(cache_dir)
                    deleted_items.append(f"Cache: {os.path.basename(cache_dir)}")
                except Exception as e:
                    errors.append(f"Failed to delete cache: {e}")

            # 2. Delete Sibling Files (including main file, preview, thumbnail)
            for f in os.listdir(dir_path):
                f_path = os.path.join(dir_path, f)
                if not os.path.isfile(f_path): continue
                
                if self._is_associated_sibling(f, base_name):
                    try:
                        os.remove(f_path)
                        deleted_items.append(f"File: {f}")
                        # Also remove from loader cache just in case
                        if hasattr(self, 'image_loader_thread'):
                            self.image_loader_thread.remove_from_cache(f_path)
                    except Exception as e:
                        errors.append(f"Failed to delete {f}: {e}")

            return len(errors) == 0, len(deleted_items), errors
            
        except Exception as e:
             return False, 0, [f"Critical error during delete: {e}"]

    def rename_associated_files(self, file_path, new_base_name):
        """
        [Refactor] Centralized logic to rename a main file, its cache directory, 
        and associated sibling files (like thumbnails or previews).
        Returns: (success_bool, renamed_count, error_messages_list)
        """
        import shutil
        from ..core import calculate_structure_path
        import re
        
        if not file_path or not os.path.exists(file_path):
            return False, 0, ["Selection does not exist."]

        old_filename = os.path.basename(file_path)
        old_base = os.path.splitext(old_filename)[0]
        ext = os.path.splitext(old_filename)[1]
        
        new_base_name = new_base_name.strip()
        if not new_base_name or new_base_name == old_base:
            return False, 0, ["Invalid or identical name."]
            
        # Validate Filename characters
        if re.search(r'[<>:\"/\\|?*]', new_base_name):
             return False, 0, ["Filename contains invalid characters."]

        dir_path = os.path.dirname(file_path)
        new_filename = new_base_name + ext
        new_path = os.path.join(dir_path, new_filename)
        
        if os.path.exists(new_path):
            return False, 0, ["A file with that name already exists."]

        renamed_count = 0
        errors = []

        try:
            # 1. Rename Cache Directory
            old_cache_dir = calculate_structure_path(file_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            new_fake_path = os.path.join(dir_path, new_filename)
            new_cache_dir = calculate_structure_path(new_fake_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            
            if os.path.exists(old_cache_dir):
                if os.path.exists(new_cache_dir):
                     errors.append(f"Target cache directory already exists: {os.path.basename(new_cache_dir)}")
                else:
                    try:
                        os.rename(old_cache_dir, new_cache_dir)
                        renamed_count += 1
                        
                        # Rename files INSIDE the cache directory
                        if os.path.exists(new_cache_dir):
                            for inner_f in os.listdir(new_cache_dir):
                                inner_path = os.path.join(new_cache_dir, inner_f)
                                if not os.path.isfile(inner_path): continue
                                
                                if inner_f.startswith(old_base):
                                    inner_suffix = inner_f[len(old_base):]
                                    if inner_suffix.startswith(".") or inner_suffix == "":
                                        new_inner_name = new_base_name + inner_suffix
                                        new_inner_path = os.path.join(new_cache_dir, new_inner_name)
                                        try:
                                            os.rename(inner_path, new_inner_path)
                                        except OSError as e:
                                            errors.append(f"Failed to rename cache file {inner_f}: {e}")
                    except Exception as e:
                        errors.append(f"Failed to rename cache: {e}")

            # 2. Rename Sibling Files
            for f in os.listdir(dir_path):
                f_path = os.path.join(dir_path, f)
                if not os.path.isfile(f_path): continue
                
                if f.startswith(old_base):
                    suffix = f[len(old_base):]
                    if suffix.startswith("."):
                        new_f_name = new_base_name + suffix
                        new_f_path = os.path.join(dir_path, new_f_name)
                        
                        try:
                            os.rename(f_path, new_f_path)
                            renamed_count += 1
                            if hasattr(self, 'image_loader_thread'):
                                self.image_loader_thread.remove_from_cache(f_path)
                        except Exception as e:
                            errors.append(f"Failed to rename {f}: {e}")

            return len(errors) == 0, renamed_count, errors
            
        except Exception as e:
            return False, 0, [f"Critical error during rename: {e}"]

    def move_associated_files(self, file_path, target_dir):
        """
        [New Feature] Moves a main file/folder, its cache directory, 
        and associated sibling files (like thumbnails or previews) to a new target directory.
        Returns: (success_bool, moved_count, error_messages_list)
        """
        import shutil
        from ..core import calculate_structure_path
        
        if not file_path or not os.path.exists(file_path):
            return False, 0, ["Selection does not exist."]

        if not target_dir or not os.path.exists(target_dir):
            return False, 0, ["Target directory does not exist."]

        # Ensure we are not moving a folder into itself
        if os.path.isdir(file_path):
            if self._is_path_within(file_path, target_dir):
                return False, 0, ["Cannot move a folder into itself."]

        filename = os.path.basename(file_path)
        new_path = os.path.join(target_dir, filename)
        
        if os.path.exists(new_path):
            return False, 0, [f"'{filename}' already exists in target directory."]

        moved_count = 0
        errors = []
        is_folder = os.path.isdir(file_path)

        try:
            # 1. Move Cache Directory
            old_cache_dir = calculate_structure_path(file_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            new_cache_dir = calculate_structure_path(new_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            
            if os.path.exists(old_cache_dir):
                if os.path.exists(new_cache_dir):
                     errors.append(f"Target cache directory already exists: {os.path.basename(new_cache_dir)}")
                else:
                    try:
                        os.makedirs(os.path.dirname(new_cache_dir), exist_ok=True)
                        shutil.move(old_cache_dir, new_cache_dir)
                    except Exception as e:
                        errors.append(f"Failed to move cache: {e}")

            # 2. Move Main Item & Sibling Files
            if is_folder:
                try:
                    shutil.move(file_path, target_dir)
                    moved_count += 1
                except Exception as e:
                    errors.append(f"Failed to move folder {filename}: {e}")
            else:
                old_base = os.path.splitext(filename)[0]
                dir_path = os.path.dirname(file_path)
                
                for f in os.listdir(dir_path):
                    f_path = os.path.join(dir_path, f)
                    if not os.path.isfile(f_path): continue
                    
                    if f.startswith(old_base):
                        suffix = f[len(old_base):]
                        if suffix.startswith("."):
                            new_f_path = os.path.join(target_dir, f)
                            try:
                                shutil.move(f_path, new_f_path)
                                moved_count += 1
                                if hasattr(self, 'image_loader_thread'):
                                    self.image_loader_thread.remove_from_cache(f_path)
                            except Exception as e:
                                errors.append(f"Failed to move {f}: {e}")

            return len(errors) == 0, moved_count, errors
            
        except Exception as e:
            return False, 0, [f"Critical error during move: {e}"]

    # Re-implementing helper methods to be used by subclasses
    
    def copy_media_to_cache(self, file_path, target_relative_path):
        import shutil
        from ..core import calculate_structure_path
        
        if not target_relative_path: return None
        
        # [Fix] Added mode argument
        cache_dir = calculate_structure_path(target_relative_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
        if not os.path.exists(cache_dir): os.makedirs(cache_dir)
        
        name = os.path.basename(file_path)
        dest_path = os.path.join(cache_dir, name)
        
        try:
            shutil.copy2(file_path, dest_path)
            # [Fix] Return Markdown/HTML tag with relative path
            # Since we are using relative paths (filenames), we just use 'name'
            ext = os.path.splitext(name)[1].lower()
            if ext in ['.mp4', '.webm', '.mkv']:
                return f'<video src="{name}" controls width="100%"></video>'
            else:
                return f"![{name}]({name})"
        except Exception as e:
            self.show_status_message(f"Failed to copy media: {e}")
            return None

    def closeEvent(self, event):
        self.stop_all_workers()
        super().closeEvent(event)


    def stop_all_workers(self):
        """
        [Optimization] Parallel shutdown sequence.
        Phase 1: Signal all threads to stop.
        Phase 2: Wait for threads with a global timeout.
        """
        workers, thumb_workers, heavy_workers = self.collect_active_workers()
        self.signal_workers_stop(workers, heavy_workers)
        self.wait_workers_stop(workers, thumb_workers, heavy_workers)

    def collect_active_workers(self):
        workers = [] # Fast workers (Scanners, Search)
        heavy_workers = [] # Slow IO workers (ImageLoader, Metadata)

        # Collect all active workers
        if hasattr(self, 'active_scanners'):
            workers.extend([w for w in self.active_scanners if w.isRunning()])
            self.active_scanners.clear()
            
        try:
            if hasattr(self, 'scanner') and self.scanner and self.scanner.isRunning():
                workers.append(self.scanner)
        except RuntimeError: pass

        try:
            if hasattr(self, 'indexing_scanner') and self.indexing_scanner and self.indexing_scanner.isRunning():
                workers.append(self.indexing_scanner)
        except RuntimeError: pass

        try:
            if hasattr(self, 'search_worker') and self.search_worker and self.search_worker.isRunning():
                workers.append(self.search_worker)
        except RuntimeError: pass

        try:
            if hasattr(self, 'image_loader_thread') and self.image_loader_thread and self.image_loader_thread.isRunning():
                 heavy_workers.append(self.image_loader_thread)
        except RuntimeError: pass

        # Collect thumbnail workers
        thumb_workers = []
        if hasattr(self, 'active_thumb_workers'):
            thumb_workers = list(self.active_thumb_workers)
            self.active_thumb_workers.clear()
        
        # [NEW] Collect LocalMetadataWorker from ExampleTabWidget
        if hasattr(self, 'tab_example') and hasattr(self.tab_example, 'metadata_worker'):
            try:
                if self.tab_example.metadata_worker and self.tab_example.metadata_worker.isRunning():
                    heavy_workers.append(self.tab_example.metadata_worker)
            except RuntimeError:
                pass
        
        return workers, thumb_workers, heavy_workers

    def signal_workers_stop(self, workers=None, heavy_workers=None):
        if workers is None:
             workers, _, heavy_workers = self.collect_active_workers() # Re-collect if needed
        
        if heavy_workers is None: heavy_workers = []

        all_stop_workers = workers + heavy_workers
        logging.debug(f"[StopAllWorkers] Stopping {len(all_stop_workers)} workers...")

        # Phase 1: Send Stop Signal (for those that support it)
        for w in all_stop_workers:
            try:
                if hasattr(w, 'stop'):
                    w.stop()
            except RuntimeError: pass
            # ThumbnailWorker doesn't have stop(), it just runs until completion (copy is blocking usually)

    def wait_workers_stop(self, workers=None, thumb_workers=None, heavy_workers=None):
        if workers is None:
             workers, thumb_workers, heavy_workers = self.collect_active_workers()
        
        if thumb_workers is None: thumb_workers = []
        if heavy_workers is None: heavy_workers = []

        # Phase 2: Wait for workers
        
        # 1. Wait for Scanners & Searchers (Fast)
        for w in workers:
            try:
                if w.isRunning():
                    logging.debug(f"[StopAllWorkers] Waiting for {w.objectName() if w.objectName() else 'Worker'}...")
                    w.wait(1000) # 1 sec each
                    logging.debug(f"[StopAllWorkers] {w.objectName() if w.objectName() else 'Worker'} finished.")
            except RuntimeError: pass

        # 2. Wait for Thumbnail workers
        for w in thumb_workers:
            try:
                if w.isRunning():
                    logging.debug(f"[StopAllWorkers] Waiting for ThumbnailWorker...")
                    w.wait(500)
            except RuntimeError: pass
            
        # 3. Wait for Heavy Workers (ImageLoader, Metadata)
        for w in heavy_workers:
            try:
                if w.isRunning():
                    name = w.objectName() if w.objectName() else str(w)
                    logging.debug(f"[StopAllWorkers] Waiting for {name} (3s timeout)...")
                    # Give it ample time (e.g. 3s)
                    if not w.wait(3000):
                        logging.warning(f"[StopAllWorkers] {name} stuck. Forcing termination.")
                        # Terminate is dangerous but prevents the "Destroyed while running" error on exit
                        w.terminate()
                        w.wait()
                        logging.warning(f"[StopAllWorkers] {name} terminated.")
                    else:
                        logging.debug(f"[StopAllWorkers] {name} exited gracefully.")
            except RuntimeError: pass
            
        logging.debug("[StopAllWorkers] Cleanup complete.")

    # [Memory Optimization] Tab Visibility Hooks
    def on_tab_hidden(self):
        """Called when this manager tab is hidden (user switched to another tab)."""
        import logging
        logger = logging.getLogger("managers.base")
        logger.debug(f"[BaseManager] Tab hidden: {self.__class__.__name__}")
        
        # Release preview player resources
        if hasattr(self, 'preview_lbl') and hasattr(self.preview_lbl, 'release_resources'):
            self.preview_lbl.release_resources()
            
        # Stop example videos
        if hasattr(self, 'tab_example') and hasattr(self.tab_example, 'stop_videos'):
            self.tab_example.stop_videos()

    def on_tab_shown(self):
        """Called when this manager tab is shown."""
        # Optional: Restore resources if needed, but lazy loading usually handles it
        pass

    def cleanup(self):
        """Called on app exit"""
        self.stop_all_workers()
        # Ensure we also release video resources on exit
        self.on_tab_hidden()



    def _load_common_file_details(self, path):
        """
        Refactored common logic for loading file details.
        Returns: (filename, size_str, date_str, preview_path)
        """
        filename = os.path.basename(path)
        
        # [Log] Debug
        logging.debug(f"[_load_common_file_details] Loading details for: {path}")

        try:
            st = os.stat(path)
            size_str = self.format_size(st.st_size)
            date_str = self.format_date(st.st_mtime, seconds=True)
        except (OSError, ValueError) as e:
            logging.error(f"Failed to stat file {path}: {e}")
            size_str = "Error"
            date_str = "Error"
            
        # Duplicate Check
        if self.app_settings.get("show_duplicates", False) and self.get_mode() != "gallery" and hasattr(self, 'file_map') and self.lbl_duplicate_warning:
            f_name_lower = filename.lower()
            duplicates = self.file_map.get(f_name_lower, [])
            if len(duplicates) > 1:
                 logging.debug(f"[Duplicate] Found {len(duplicates)} duplicates for {filename}")
                 # Exclude current path from display
                 curr_norm = os.path.normcase(os.path.abspath(path))
                 other_paths = [p for p in duplicates if os.path.normcase(os.path.abspath(p)) != curr_norm]
                 
                 msg = f"⚠️ Duplicate Files Found ({len(duplicates)})"
                 if other_paths:
                     msg += "\n" + "\n".join(other_paths)

                 tooltip = "Same filename detected in:\n" + "\n".join(duplicates)
                 self.lbl_duplicate_warning.setText(msg)
                 self.lbl_duplicate_warning.setToolTip(tooltip)
                 self.lbl_duplicate_warning.show()
            else:
                 self.lbl_duplicate_warning.hide()

        # Find Thumbnail Common Logic
        base = os.path.splitext(path)[0]
        preview_path = None
        for ext in PREVIEW_EXTENSIONS:
            if os.path.exists(base + ext):
                preview_path = base + ext
                break
        
        return filename, size_str, date_str, preview_path
