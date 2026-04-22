import os
import shutil
import json
import re
import time
import gc
import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QTextEdit, 
    QFormLayout, QGridLayout, QTabWidget, QStackedWidget, QMessageBox, QGroupBox, QLineEdit, QFileDialog, QInputDialog,
    QSplitter, QApplication
)
from PySide6.QtCore import Qt, QTimer, QMimeData
from PySide6.QtGui import QFont

from .base import BaseManagerWidget
from ..core import (
    calculate_structure_path, HAS_PILLOW, HAS_MARKDOWN,
    SUPPORTED_EXTENSIONS, PREVIEW_EXTENSIONS, VIDEO_EXTENSIONS, IMAGE_EXTENSIONS
)
from ..ui_components import (
    SmartMediaWidget, TaskMonitorWidget, DownloadDialog, 
    FileCollisionDialog, OverwriteConfirmDialog, ZoomWindow
)
from .example import ExampleTabWidget
from ..workers import CacheMatchWorker, ImageLoader
from .download import DownloadController
from ..controllers.metadata_controller import MetadataController
from ..utils.comfy_node_builder import ComfyNodeBuilder

try:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
except ImportError:
    pass
try:
    import markdown
except ImportError:
    pass

class ModelManagerWidget(BaseManagerWidget):
    def __init__(self, directories, app_settings, task_monitor, parent_window=None):
        self.task_monitor = task_monitor
        self.parent_window = parent_window
        self.last_download_dir = None

        # Filter directories for 'model' mode
        model_dirs = {k: v for k, v in directories.items() if v.get("mode", "model") == "model"}
        super().__init__(model_dirs, SUPPORTED_EXTENSIONS["model"], app_settings)
        
        self.metadata_queue = []
        self.selected_model_paths = []
        self._gc_counter = 0 # [Memory] Counter for periodic GC
        
        # Download Controller
        self.downl_controller = DownloadController(self, task_monitor, app_settings)
        self.downl_controller.download_finished.connect(self._on_download_finished_controller)
        self.downl_controller.download_error.connect(self._on_download_error_controller)
        self.downl_controller.progress_updated.connect(lambda k, s, p: self.show_status_message(f"{s}: {p}%", 0))
        
        # Metadata Controller
        self.metadata_controller = MetadataController(app_settings, directories, self)
        self.metadata_controller.status_message.connect(lambda msg, dur: self.show_status_message(msg, dur))
        self.metadata_controller.task_progress.connect(self.task_monitor.update_task)
        self.metadata_controller.batch_started.connect(lambda paths: self.task_monitor.add_tasks(paths, task_type="Auto Match"))
        self.metadata_controller.model_processed.connect(self._on_model_processed)
        self.metadata_controller.batch_processed.connect(self._on_batch_processed)
        
    def set_directories(self, directories):
        # Filter directories for 'model' mode
        model_dirs = {k: v for k, v in directories.items() if v.get("mode", "model") == "model"}
        super().set_directories(model_dirs)
        if self.directories:
            self.metadata_controller.directories = directories
        if hasattr(self, 'tab_example'):
            self.tab_example.directories = directories

    def stop_all_workers(self):
        # Stop Controllers first
        if hasattr(self, 'downl_controller'):
             self.downl_controller.stop()
        if hasattr(self, 'metadata_controller'):
             self.metadata_controller.stop()
        
        # Stop Base workers
        super().stop_all_workers()

    def collect_active_workers(self):
        workers, thumb_workers, heavy_workers = super().collect_active_workers()
        try:
            if hasattr(self, 'cache_match_worker') and self.cache_match_worker and self.cache_match_worker.isRunning():
                heavy_workers.append(self.cache_match_worker)
        except RuntimeError:
            pass
        return workers, thumb_workers, heavy_workers

    def get_mode(self): return "model"

    def get_debug_info(self):
        info = super().get_debug_info()
        
        # Player Stats
        player_state = "Stopped"
        if self.preview_lbl and self.preview_lbl.media_player:
            state = self.preview_lbl.media_player.playbackState()
            if state == 1: player_state = "Playing" 
            elif state == 2: player_state = "Paused"
            
        info.update({
            "download_queue_size": len(self.downl_controller.download_queue),
            "metadata_queue_size": len(self.metadata_controller.queue),
            "video_player_active": (self.preview_lbl.media_player is not None),
            "video_player_state": player_state,
            "gc_counter": self._gc_counter,
            "example_tab_stats": self.tab_example.get_debug_info() if hasattr(self, 'tab_example') else {}
        })
        return info

    def init_center_panel(self):

        # [Refactor] Use shared setup
        self._setup_info_panel(["Ext"])
        
        self.preview_lbl = SmartMediaWidget(loader=self.image_loader_thread, player_type="preview")
        self.preview_lbl.setMinimumSize(100, 100) 
        self.preview_lbl.clicked.connect(self.on_preview_click)
        self.center_layout.addWidget(self.preview_lbl, 1)
        
        # [Layout] 2x2 Grid for buttons
        center_btn_layout = QGridLayout()
        center_btn_layout.setSpacing(5)
        
        # [Feature] Copy ComfyUI Node
        self.btn_copy_node = QPushButton("📋 Copy Node")
        self.btn_copy_node.setToolTip("Copy as ComfyUI Node JSON (Ctrl+V in ComfyUI)")
        self.btn_copy_node.clicked.connect(self.copy_comfy_node)
        
        self.btn_replace = QPushButton("🖼️ Change Thumb")
        self.btn_replace.setToolTip("Change the thumbnail image for the selected model")
        self.btn_replace.clicked.connect(self.replace_thumbnail)
        
        btn_open = QPushButton("📂 Open Folder")
        btn_open.setToolTip("Open the containing folder in File Explorer")
        btn_open.clicked.connect(self.open_current_folder)
        
        # [New Feature] Copy Relative Path
        self.btn_copy_path = QPushButton("📋 Copy Path")
        self.btn_copy_path.setToolTip("Copy file path relative to the registered root folder")
        self.btn_copy_path.clicked.connect(self.copy_model_relative_path)
        
        # Row 0
        center_btn_layout.addWidget(self.btn_copy_node, 0, 0)
        center_btn_layout.addWidget(self.btn_replace, 0, 1)
        
        # Row 1
        center_btn_layout.addWidget(btn_open, 1, 0)
        center_btn_layout.addWidget(self.btn_copy_path, 1, 1)
        
        self.center_layout.addLayout(center_btn_layout)

    def init_right_panel(self):
        meta_btns = QGridLayout()
        btn_auto = QPushButton("⚡ Auto Match")
        btn_auto.setToolTip("Automatically search Civitai for metadata by file hash")
        btn_manual = QPushButton("🔗 Manual URL")
        btn_manual.setToolTip("Manually enter a Civitai/HuggingFace URL to fetch metadata")
        btn_download = QPushButton("⬇️ Download Model")
        btn_download.setToolTip("Download a new model from a URL")
        btn_cache_match = QPushButton("Cache match")
        btn_cache_match.setToolTip("Find and attach an existing cache folder with the same model hash")
        
        btn_auto.clicked.connect(lambda: self.run_civitai("auto"))
        btn_manual.clicked.connect(lambda: self.run_civitai("manual"))
        btn_download.clicked.connect(self.download_model_dialog)
        btn_cache_match.clicked.connect(self.cache_match_selected_models)

        meta_btns.addWidget(btn_auto, 0, 0)
        meta_btns.addWidget(btn_manual, 0, 1)
        meta_btns.addWidget(btn_download, 1, 0)
        meta_btns.addWidget(btn_cache_match, 1, 1)
        self.right_layout.addLayout(meta_btns)
        
        
        
        # Tabs (from Base)
        self.tabs = self.setup_content_tabs()
        
        # Download Tab Removed (User Request: Redundant with Task Monitor)
        
        self.right_layout.addWidget(self.tabs)



    # === Interaction Logic ===

    def copy_comfy_node(self):
        """
        [Role]
        Handles the 'Copy Node' button click event.
        It retrieves the current file path, determines the model type from the folder configuration,
        and uses ComfyNodeBuilder to creating the clipboard content.

        [Flow]
        1. Validate selection.
        2. Get 'model_type' (e.g. checkpoints) from the current folder's config.
        3. Generate HTML clipboard data.
        4. Set to System Clipboard.
        """
        if not self.current_path or not os.path.exists(self.current_path):
            self.show_status_message("No model selected or file not found.", 3000)
            return

        # Get Model Type from current folder config
        current_root_alias = self.folder_combo.currentText()
        folder_config = self.directories.get(current_root_alias, {})
        model_type = folder_config.get("model_type", "")
        
        if not model_type:
             QMessageBox.warning(self, "Configuration Required", 
                                 f"Model Type is not configured for '{current_root_alias}'.\nPlease set it in Settings -> Registered Folders.")
             return
            
        # [Feature] Support ComfyUI Root Override
        root_path = folder_config.get("comfy_root", "")
        if not root_path:
            root_path = folder_config.get("path", "")
            
        data, mime_type = ComfyNodeBuilder.create_html_clipboard(self.current_path, model_type, root_path)
        print(f"[DEBUG] Copy Node Payload ({mime_type}): {data}") 
        
        clipboard = QApplication.clipboard()
        mime_data = QMimeData()
        
        if mime_type == "text/html":
            mime_data.setHtml(data)
            mime_data.setText("ComfyUI Node") # Fallback text
        else:
            mime_data.setText(data)
            
        clipboard.setMimeData(mime_data)
        
        msg = "Embedding copied!" if model_type == "embeddings" else "ComfyUI Node copied to clipboard!"
        self.show_status_message(msg, 3000)
        # Optional: Toast notification if available, but status bar is fine.

    def copy_model_relative_path(self):
        """
        Copies the relative path of the selected model to the clipboard.
        The path is relative to the currently selected root folder.
        """
        if not self.current_path or not os.path.exists(self.current_path):
            self.show_status_message("No model selected or file not found.", 3000)
            return

        # Get Current Root Path
        current_root_alias = self.folder_combo.currentText()
        folder_config = self.directories.get(current_root_alias, {})
        
        # Determine actual root path
        if isinstance(folder_config, dict):
            # [Feature] First try to use the configured ComfyUI Root path
            root_path = folder_config.get("comfy_root")
            # Fallback to physical path if not configured
            if not root_path:
                root_path = folder_config.get("path")
        else:
            root_path = str(folder_config)
            
        if not root_path or not os.path.exists(root_path):
             self.show_status_message(f"Error: Invalid root path for '{current_root_alias}'", 3000)
             return

        try:
            # Calculate Relative Path
            # commonpath might be safer? relpath is fine if under root.
            # If cross-drive, relpath might fail or return absolute on some py versions/OS, 
            # but usually safely returns abspath if no common root on Windows.
            # However, user wants "ILXL\oneObsession..." if under "F:\SD_Model\Ckpt"
            
            rel_path = os.path.relpath(self.current_path, root_path)
            
            # Copy to Clipboard
            clipboard = QApplication.clipboard()
            clipboard.setText(rel_path)
            
            self.show_status_message(f"Relative path copied: {rel_path}", 3000)
            
        except ValueError:
            # Can happen on Windows if paths are on different drives
            self.show_status_message("Error: Paths are on different drives", 3000)
        except Exception as e:
            self.show_status_message(f"Error calculating path: {e}", 3000)
    
    def on_tree_select(self):
        items = self.tree.selectedItems()
        if not items: return
        selected_paths = []
        for item in items:
            path = item.data(0, Qt.UserRole)
            type_ = item.data(0, Qt.UserRole + 1)
            if type_ == "file" and path: 
                selected_paths.append(path)
        self.selected_model_paths = selected_paths
        current_item = self.tree.currentItem()
        if current_item:
            path = current_item.data(0, Qt.UserRole)
            type_ = current_item.data(0, Qt.UserRole + 1)
            
            # [Memory] Fast cleanup of previous view
            self.image_loader_thread.clear_queue() # Cancel pending loads
            self.preview_lbl.clear_memory()
            self.tab_example.unload_current_examples()
            
            if type_ == "file" and path:
                 self.current_path = path # [Fix] Update current path tracker
                 self._load_details(path)
                 

                 
            elif type_ == "dict":
                 # Assuming self.lbl_info is a QLabel to display messages
                 # If not, this line might need adjustment based on actual UI
                 self.info_labels["Name"].setText("Select a model file to see details.")
                 self.info_labels["Ext"].setText("-")
                 self.info_labels["Size"].setText("-")
                 self.info_labels["Path"].setText("-")
                 self.info_labels["Date"].setText("-")
                 self.preview_lbl.set_media(None)
                 self.tab_note.set_text("")
            else:
                 self.info_labels["Name"].setText("Select a model file to see details.")
                 self.info_labels["Ext"].setText("-")
                 self.info_labels["Size"].setText("-")
                 self.info_labels["Path"].setText("-")
                 self.info_labels["Date"].setText("-")
                 self.preview_lbl.set_media(None)
                 self.tab_note.set_text("")

    def _load_details(self, path):
        # [Refactor] Use shared logic from BaseManagerWidget
        filename, size_str, date_str, preview_path = self._load_common_file_details(path)
        
        # Update Info Labels
        ext = os.path.splitext(filename)[1]
        self.info_labels["Name"].setText(filename)
        self.info_labels["Ext"].setText(ext)
        self.info_labels["Size"].setText(size_str)
        self.info_labels["Date"].setText(date_str)
        self.info_labels["Path"].setText(path)
        
        self.preview_lbl.set_media(preview_path)
        
        # [Memory] Periodic GC for model browsing
        self._gc_counter += 1
        if self._gc_counter >= 20: 
            gc.collect()
            self._gc_counter = 0
        
        # Note Loading (Standardized)
        self.load_content_data(path)



    




    def _save_json_direct(self, model_path, content):
        # [Fix] Added mode argument
        cache_dir = calculate_structure_path(model_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
        if not os.path.exists(cache_dir): os.makedirs(cache_dir)
        model_name = os.path.splitext(os.path.basename(model_path))[0]
        json_path = os.path.join(cache_dir, model_name + ".json")
        try:
            data = {}
            if os.path.exists(json_path):
                with open(json_path, 'r', encoding='utf-8') as f: data = json.load(f)
            data["user_note"] = content
            with open(json_path, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e: logging.error(f"Save Error: {e}")



    # === Civitai / Download Logic ===
    def run_civitai(self, mode, targets=None, manual_url_override=None, overwrite_behavior_override=None):
        if targets is None:
            targets = self.selected_model_paths
        
        # Delegate to Controller
        self.metadata_controller.run_civitai(mode, targets, manual_url_override, overwrite_behavior_override)

    def _on_model_processed(self, success, msg, data, model_path):
        if success:
            desc = data.get("description", "")
            self.save_note_for_path(model_path, desc, silent=True)
            if self.current_path == model_path:
                self.tab_note.set_text(desc)
                self.tab_example.load_examples(model_path)
                QTimer.singleShot(200, lambda: self._load_details(model_path))

    def _on_batch_processed(self):
        self.show_status_message("Batch Processed.")
        # Resume download queue if we were in a chain
        self.downl_controller.resume()

    def download_model_dialog(self):
        default_dir = None
        if self.last_download_dir and os.path.exists(self.last_download_dir):
            default_dir = self.last_download_dir
        if not default_dir:
            current_item = self.tree.currentItem()
            if current_item:
                path = current_item.data(0, Qt.UserRole)
                type_ = current_item.data(0, Qt.UserRole + 1)
                if path:
                    default_dir = os.path.dirname(path) if type_ == "file" else path
        if not default_dir:
            root_name = self.folder_combo.currentText()
            default_dir = self.directories.get(root_name, {}).get("path") if isinstance(self.directories.get(root_name), dict) else self.directories.get(root_name)
        if not default_dir:
            default_dir = os.getcwd() 

        dlg = DownloadDialog(default_dir, self)
        if dlg.exec():
            url, target_dir = dlg.get_data()
            if not url: return
            if not os.path.exists(target_dir):
                QMessageBox.warning(self, "Error", "Selected directory does not exist.")
                return

            self.last_download_dir = target_dir
            self.downl_controller.add_download(url, target_dir)
            self.show_status_message(f"Added to queue: {os.path.basename(target_dir)}")

    def _on_download_finished_controller(self, msg, file_path):
        self.show_status_message(msg)
        self.refresh_list()
        
        # Auto-match Logic
        chain_started = False
        if file_path and os.path.exists(file_path):
             self.show_status_message(f"Auto-matching for: {os.path.basename(file_path)}...")
             self.run_civitai("auto", targets=[file_path])
             # Check if controller accepted the task (worker running or queue not empty)
             if self.metadata_controller.worker is not None or self.metadata_controller.queue:
                 chain_started = True
                 
        if not chain_started:
             self.downl_controller.resume() # Process next immediately

    def _on_download_error_controller(self, err_msg):
        self.show_status_message(f"Download Error: {err_msg}")
        QMessageBox.critical(self, "Download Failed", err_msg)
        self.downl_controller.resume()

    def cache_match_selected_models(self):
        targets = list(self.selected_model_paths)
        if not targets and self.current_path and os.path.isfile(self.current_path):
            targets = [self.current_path]

        if not targets:
            QMessageBox.warning(self, "Warning", "No model selected.")
            return

        if hasattr(self, 'cache_match_worker') and self.cache_match_worker:
            try:
                if self.cache_match_worker.isRunning():
                    QMessageBox.information(self, "Cache Match", "Cache match is already running.")
                    return
            except RuntimeError:
                self.cache_match_worker = None

        self.task_monitor.add_tasks(targets, task_type="Cache Match")
        self.cache_match_worker = CacheMatchWorker(
            targets=targets,
            cache_root=self.get_cache_dir(),
            directories=self.directories,
            mode=self.get_mode()
        )
        self.cache_match_worker.progress.connect(self._on_cache_match_progress)
        self.cache_match_worker.completed.connect(self._on_cache_match_completed)
        self.cache_match_worker.finished.connect(self.cache_match_worker.deleteLater)
        self.cache_match_worker.finished.connect(lambda: setattr(self, "cache_match_worker", None))
        self.cache_match_worker.start()
        self.show_status_message(f"Cache match started for {len(targets)} model(s).")

    def _on_cache_match_progress(self, path, status, percent):
        self.task_monitor.update_task(path, status, percent)
        self.show_status_message(f"Cache Match: {os.path.basename(path)} - {status}", 0)

    def _on_cache_match_completed(self, summary):
        matched = summary.get("matched", 0)
        skipped = summary.get("skipped", 0)
        not_found = summary.get("not_found", 0)
        errors = summary.get("errors", [])

        if self.current_path and os.path.exists(self.current_path):
            QTimer.singleShot(0, lambda p=self.current_path: self._load_details(p))

        msg = f"Cache Match done. Matched: {matched}, Skipped: {skipped}, Not found: {not_found}"
        if errors:
            QMessageBox.warning(self, "Cache Match Completed with Errors", msg + "\n\n" + "\n".join(errors[:10]))
        self.show_status_message(msg, 5000)











    # === Remove / Rename Feature ===
    
    def init_left_bottom(self, layout):
        """Override to add Remove/Rename buttons to the bottom of the left panel."""
        btn_layout = QHBoxLayout()
        
        btn_remove = QPushButton("🗑️ Remove")
        btn_remove.setToolTip("Permanently delete the selected model and its resources")
        btn_remove.clicked.connect(self.remove_model)
        
        btn_rename = QPushButton("✏️ Rename")
        btn_rename.setToolTip("Rename the selected model and its resources")
        btn_rename.clicked.connect(self.rename_model)
        
        btn_move = QPushButton("📦 Move")
        btn_move.setToolTip("Move selected model(s) to another folder")
        btn_move.clicked.connect(self.move_models)
        
        btn_layout.addWidget(btn_remove)
        btn_layout.addWidget(btn_rename)
        btn_layout.addWidget(btn_move)
        layout.addLayout(btn_layout)

    def remove_model(self):
        """
        Permanently deletes the selected model and its associated resources.
        Resources include:
        - The model file itself
        - Thumbnail/Preview files (same name, different extension)
        - Cache directory (metadata, notes, examples)
        """
        if not self.current_path or not os.path.exists(self.current_path):
            QMessageBox.warning(self, "Warning", "No model selected.")
            return

        filename = os.path.basename(self.current_path)
        
        # Confirm Delete
        reply = QMessageBox.question(
            self, "Confirm Delete", 
            f"Are you sure you want to PERMANENTLY delete:\n{filename}\n\nThis will also delete:\n- Thumbnails/Previews\n- Metadata & Notes\n- Example Images",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply != QMessageBox.Yes: return

        # 1. Unload resources
        if hasattr(self, 'preview_lbl'): 
            self.preview_lbl.clear_memory()
            self.preview_lbl.set_media(None)
        if hasattr(self, 'tab_example'): 
            self.tab_example.unload_current_examples()
        
        # Ensure image loader isn't holding it (if applicable)
        if hasattr(self, 'image_loader_thread'):
            self.image_loader_thread.remove_from_cache(self.current_path)
            
        QApplication.processEvents() # Allow UI to release

        # [Refactor] Delegate to BaseManagerWidget's centralized method
        success, deleted_count, errors = self.remove_associated_files(self.current_path)
        
        if errors:
            msg = "Completed with errors:\n" + "\n".join(errors)
            QMessageBox.warning(self, "Delete Incomplete", msg)
        else:
            self.show_status_message(f"Deleted {deleted_count} items.")
            
        # Refresh List
        self.current_path = None # Reset selection
        self.refresh_list()

    def rename_model(self):
        """
        Renames the selected model and its associated resources.
        Resources include:
        - The model file itself
        - Thumbnail/Preview files
        - Cache directory
        """
        if not self.current_path or not os.path.exists(self.current_path):
            QMessageBox.warning(self, "Warning", "No model selected.")
            return

        old_filename = os.path.basename(self.current_path)
        old_base = os.path.splitext(old_filename)[0]
        ext = os.path.splitext(old_filename)[1]
        
        # 1. Get New Name
        new_base, ok = QInputDialog.getText(self, "Rename Model", "New Name:", text=old_base)
        if not ok or not new_base: return
        
        # 2. Unload resources
        if hasattr(self, 'preview_lbl'): 
            self.preview_lbl.clear_memory()
            self.preview_lbl.set_media(None)
        if hasattr(self, 'tab_example'): 
            self.tab_example.unload_current_examples()
            
        QApplication.processEvents()

        # [Refactor] Delegate to BaseManagerWidget's centralized method
        success, renamed_count, errors = self.rename_associated_files(self.current_path, new_base)

        if not success and errors and "A file with that name" in errors[0]:
            QMessageBox.warning(self, "Error", errors[0])
            return
        elif not success and errors and "Invalid characters" in errors[0]:
            QMessageBox.warning(self, "Invalid Name", errors[0])
            return

        if errors:
            msg = "Completed with errors:\n" + "\n".join(errors)
            QMessageBox.warning(self, "Rename Incomplete", msg)
        else:
            self.show_status_message(f"Renamed {renamed_count} files/dirs.")
        
        self.current_path = None
        self.refresh_list()

    def move_models(self):
        """
        Moves the selected model(s) to a new target directory within the current root.
        """
        selected_items = self.tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Warning", "No item selected to move.")
            return

        name = self.folder_combo.currentText()
        if not name: return
        data = self.directories.get(name)
        raw_path = data.get("path") if isinstance(data, dict) else data
        root_path = os.path.normpath(raw_path)

        target_dir = QFileDialog.getExistingDirectory(self, "Select Target Directory", root_path)
        if not target_dir: return
        
        target_dir = os.path.normpath(target_dir)

        # Ensure target is inside root
        if not self._is_path_within(root_path, target_dir):
            QMessageBox.critical(self, "Error", "Cannot move files outside the selected root directory.")
            return

        # Unload resources
        if hasattr(self, 'preview_lbl'): 
            self.preview_lbl.clear_memory()
            self.preview_lbl.set_media(None)
        if hasattr(self, 'tab_example'): 
            self.tab_example.unload_current_examples()
            
        QApplication.processEvents()

        total_moved = 0
        all_errors = []

        for item in selected_items:
            # We don't move "DUMMY" items or loading indicators
            if item.data(0, Qt.UserRole) == "DUMMY": continue
            item_path = item.data(0, Qt.UserRole)
            if not item_path or not os.path.exists(item_path): continue
            
            # Ensure image loader isn't holding it
            if hasattr(self, 'image_loader_thread'):
                self.image_loader_thread.remove_from_cache(item_path)

            success, moved_count, errors = self.move_associated_files(item_path, target_dir)
            total_moved += moved_count
            if errors:
                all_errors.extend(errors)

        if all_errors:
            msg = "Completed with errors:\n" + "\n".join(all_errors)
            QMessageBox.warning(self, "Move Incomplete", msg)
        else:
            self.show_status_message(f"Moved {total_moved} files/dirs successfully.")
            
        self.current_path = None
        self.refresh_list()


    def closeEvent(self, event):
        self.metadata_controller.stop()

        self.downl_controller.stop()
        
        # [Memory] Explicit cleanup of media widgets
        if hasattr(self, 'preview_lbl'):
            self.preview_lbl.clear_memory()
            
        if hasattr(self, 'tab_example'):
            self.tab_example.unload_current_examples()
            
        super().closeEvent(event)
