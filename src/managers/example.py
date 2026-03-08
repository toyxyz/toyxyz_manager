import os
import shutil
import json
import time
import gc
import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit, 
    QGridLayout, QGroupBox, QLineEdit, QSplitter, QFileDialog, QMessageBox, QApplication, QTabWidget, QCheckBox, QComboBox
)
from PySide6.QtCore import Qt, Signal
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from ..core import calculate_structure_path, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, CACHE_DIR_NAME
from ..ui_components import SmartMediaWidget, ZoomWindow
from ..ui.metadata_widget import MetadataViewerWidget
from ..ui_components import SmartMediaWidget, ZoomWindow
from ..ui.metadata_widget import MetadataViewerWidget
from ..workers import LocalMetadataWorker
from ..metadata import validate_metadata_type, standardize_metadata

class ExampleTabWidget(QWidget):
    status_message = Signal(str)

    def __init__(self, directories, app_settings, parent=None, image_loader=None, cache_root=None, mode="model"):
        super().__init__(parent)
        self.directories = directories
        self.app_settings = app_settings
        self.image_loader = image_loader
        self.cache_root = cache_root or CACHE_DIR_NAME
        self.mode = mode
        self.mode = mode
        self.current_item_path = None
        self.current_cache_dir = None
        self.using_custom_path = False
        self.example_images = []
        self.current_example_idx = 0
        self._gc_counter = 0 # [Memory] Counter for periodic GC
        
        self.init_ui()
        
        # [Optimization] Async Metadata Worker
        self.metadata_worker = LocalMetadataWorker()
        self.metadata_worker.finished.connect(self._on_metadata_ready)
        self.metadata_worker.start()
    
    def closeEvent(self, event):
        """Ensure metadata worker is stopped on widget close."""
        if self.metadata_worker and self.metadata_worker.isRunning():
            self.metadata_worker.stop()
            self.metadata_worker.wait(1000)  # Wait up to 1 second
        super().closeEvent(event)

    def get_debug_info(self):
        mem_bytes = self.lbl_img.get_memory_usage()
        return {
            "file_list_count": len(self.example_images),
            "est_memory_mb": mem_bytes / 1024 / 1024,
            "gc_counter": self._gc_counter,
            "current_index": self.current_example_idx
        }

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5,5,5,5)
        
        self.splitter = QSplitter(Qt.Vertical)
        
        # [Top] Image Area
        img_widget = QWidget()
        img_layout = QVBoxLayout(img_widget)
        img_layout.setContentsMargins(0,0,0,0)
        
        # [New] Metadata Filter Text Box at the very top
        top_layout = QHBoxLayout()
        
        self.cmb_search_field = QComboBox()
        self.cmb_search_field.addItems(["All", "Positive", "Negative", "Settings", "Resources", "Workflow"])
        self.cmb_search_field.setToolTip("Select metadata field to search")
        self.cmb_search_field.currentTextChanged.connect(lambda: self.load_examples(self.current_item_path))
        
        self.txt_search_meta = QLineEdit()
        self.txt_search_meta.setPlaceholderText("Search metadata...")
        self.txt_search_meta.returnPressed.connect(lambda: self.load_examples(self.current_item_path))
        
        self.btn_clear_search = QPushButton("🔙")
        self.btn_clear_search.setToolTip("Clear search filter (Back to all)")
        self.btn_clear_search.setFixedWidth(30)
        self.btn_clear_search.clicked.connect(self.clear_search_filter)
        
        top_layout.addWidget(self.cmb_search_field)
        top_layout.addWidget(self.txt_search_meta)
        top_layout.addWidget(self.btn_clear_search)
        img_layout.addLayout(top_layout)
        
        self.lbl_img = SmartMediaWidget(loader=self.image_loader, player_type="example")
        self.lbl_img.setMinimumSize(100, 100)
        self.lbl_img.clicked.connect(self.on_example_click)
        
        img_layout.addWidget(self.lbl_img)
        
        # Navigation
        nav_layout = QHBoxLayout()
        self.btn_prev = QPushButton("◀")
        self.btn_next = QPushButton("▶")
        self.lbl_count = QLabel("0/0")
        self.lbl_wf_status = QLabel("No Workflow")
        
        self.btn_prev.clicked.connect(lambda: self.change_example(-1))
        self.btn_next.clicked.connect(lambda: self.change_example(1))
        
        nav_layout.addWidget(self.btn_prev)
        nav_layout.addWidget(self.lbl_count)
        nav_layout.addWidget(self.lbl_wf_status)
        nav_layout.addStretch()
        nav_layout.addWidget(self.btn_next)
        img_layout.addLayout(nav_layout)
        
        # Tools
        tools_layout = QHBoxLayout()
        btn_add = QPushButton("➕")
        btn_add.setToolTip("Add Image")
        btn_add.clicked.connect(self.add_example_image)
        btn_del = QPushButton("➖")
        btn_del.setToolTip("Delete Image")
        btn_del.clicked.connect(self.delete_example_image)
        btn_open = QPushButton("📂")
        btn_open.setToolTip("Open Folder")
        btn_open.clicked.connect(self.open_example_folder)
        btn_save_meta = QPushButton("💾")
        btn_save_meta.setToolTip("Save Metadata")
        btn_save_meta.clicked.connect(self.save_example_metadata)
        
        for b in [btn_add, btn_del, btn_open, btn_save_meta]:
            b.setFixedWidth(40)
            tools_layout.addWidget(b)
            
        self.chk_has_prompt = QCheckBox("Has Prompt/Workflow")
        self.chk_has_prompt.setChecked(False)
        self.chk_has_prompt.toggled.connect(lambda: self.load_examples(self.current_item_path))
        tools_layout.addWidget(self.chk_has_prompt)
        tools_layout.addStretch()
        img_layout.addLayout(tools_layout)
        self.splitter.addWidget(img_widget)
        
        # [Bottom] Metadata Area
        self.meta_viewer = MetadataViewerWidget()
        self.splitter.addWidget(self.meta_viewer)
        
        main_layout.addWidget(self.splitter)
        self.splitter.setSizes([500, 300])

    def unload_current_examples(self):
        """Force cleanup of current examples to release memory."""
        self.lbl_img.clear_memory()
        self.example_images = []
        self.current_example_idx = 0
        self._clear_meta()
        self.lbl_count.setText("0/0")
        self.lbl_wf_status.setText("")
        
    def clear_search_filter(self):
        """Clear search query and refresh."""
        if hasattr(self, 'txt_search_meta'):
            self.txt_search_meta.clear()
        self.load_examples(self.current_item_path)
        
    def load_examples(self, path, target_filename=None, custom_cache_path=None):
        # Detect if this is a "reload" or "switch"
        is_reload = (path == self.current_item_path)
        self.current_item_path = path
        self.example_images = []
        self.current_example_idx = 0
        self._clear_meta()
        
        if not path:
            self._update_ui()
            return

        # Determine Cache Directory
        if custom_cache_path:
            self.current_cache_dir = custom_cache_path
            self.using_custom_path = True
        elif is_reload and getattr(self, 'using_custom_path', False):
            # Keep existing current_cache_dir
            pass
        else:
            self.using_custom_path = False
            self.current_cache_dir = calculate_structure_path(path, self.cache_root, self.directories, mode=self.mode)

        cache_dir = self.current_cache_dir
        preview_dir = os.path.join(cache_dir, "preview")
        
        if os.path.exists(preview_dir):
            valid_exts = tuple(list(IMAGE_EXTENSIONS) + list(VIDEO_EXTENSIONS))
            all_files = [os.path.join(preview_dir, f) for f in os.listdir(preview_dir) if f.lower().endswith(valid_exts)]
            
            # [Filter Logic] Check for Query & Checkbox
            query = ""
            if hasattr(self, 'txt_search_meta'):
                query = self.txt_search_meta.text().strip().lower()
                
            has_prompt_only = False
            if hasattr(self, 'chk_has_prompt'):
                has_prompt_only = self.chk_has_prompt.isChecked()
                
            search_field = "all"
            if hasattr(self, 'cmb_search_field'):
                search_field = self.cmb_search_field.currentText().lower()
            
            if has_prompt_only or query:
                filtered = []
                for f in all_files:
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                        continue # Cannot parse meta from video headers directly here yet
                    
                    try:
                        with Image.open(f) as img:
                            # Condition 1: Checkbox
                            pass_chk = True
                            if has_prompt_only and not validate_metadata_type(img):
                                pass_chk = False
                                
                            # Condition 2: Text Search
                            pass_txt = True
                            if query:
                                target_text = ""
                                if search_field == "all":
                                    target_text = str(img.info).lower()
                                elif search_field == "workflow":
                                    target_text = str(img.info.get("workflow", img.info.get("prompt", ""))).lower()
                                else:
                                    # Use standardize_metadata for specific fields
                                    meta = standardize_metadata(img)
                                    if search_field == "positive":
                                        target_text = str(meta.get("prompts", {}).get("positive", "")).lower()
                                    elif search_field == "negative":
                                        target_text = str(meta.get("prompts", {}).get("negative", "")).lower()
                                    elif search_field == "settings":
                                        target_text = str(meta.get("main", {})).lower()
                                    elif search_field == "resources":
                                        target_text = str(meta.get("model", {})).lower()
                                
                                if query not in target_text:
                                    pass_txt = False
                                    
                            if pass_chk and pass_txt:
                                filtered.append(f)
                    except Exception as e:
                        logging.debug(f"[Example Filter] Error filtering image {f}: {e}")
                self.example_images = filtered
            else:
                self.example_images = all_files
                
            self.example_images.sort()
            
            # Attempt to restore selection
            if target_filename:
                for i, full_path in enumerate(self.example_images):
                    if os.path.basename(full_path).lower() == target_filename.lower():
                        self.current_example_idx = i
                        break
            
        self._update_ui()

    def _update_ui(self):
        total = len(self.example_images)
        if total == 0:
            self.lbl_img.set_media(None)
            self.lbl_count.setText("0/0")
            self.lbl_wf_status.setText("")
            self._clear_meta()
        else:
            self.current_example_idx = max(0, min(self.current_example_idx, total - 1))
            self.lbl_count.setText(f"{self.current_example_idx + 1}/{total}")
            path = self.example_images[self.current_example_idx]
            self.lbl_img.set_media(path)
            
            if os.path.splitext(path)[1].lower() not in VIDEO_EXTENSIONS:
                self._parse_and_display_meta(path)
            else:
                self._clear_meta()
                self.lbl_wf_status.setText("Video")

    def hideEvent(self, event):
        # [Memory] Stop playback when tab is hidden
        if self.lbl_img:
            self.lbl_img._stop_video_playback()
        super().hideEvent(event)

    def showEvent(self, event):
        # [Fix] Restore media playback/display when tab becomes visible again
        if self.example_images and 0 <= self.current_example_idx < len(self.example_images):
            path = self.example_images[self.current_example_idx]
            self.lbl_img.set_media(path)
        super().showEvent(event)

    def change_example(self, delta):
        if not self.example_images: return
        
        # [Memory] Pre-cleanup before switching
        # If we were playing video, force full cleanup to release MediaPlayer
        if self.lbl_img.is_video:
             self.lbl_img.clear_memory()
             
        self.current_example_idx = (self.current_example_idx + delta) % len(self.example_images)
        self._update_ui()
        
        # [Memory] Periodic GC - removed to prevent stuttering

    def add_example_image(self):
        if not self.current_item_path: return
        

        filters = "Media (*.png *.jpg *.jpeg *.webp *.mp4 *.webm *.gif)"
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files", "", filters)
        if not files: return
        
        cache_dir = self.current_cache_dir
        if not cache_dir: return
        preview_dir = os.path.join(cache_dir, "preview")
        if not os.path.exists(preview_dir): os.makedirs(preview_dir)
        
        last_added_name = None
        for f in files:
            try: 
                shutil.copy2(f, preview_dir)
                last_added_name = os.path.basename(f)
            except OSError: pass
            
        self.load_examples(self.current_item_path)
        
        # [UX Fix] Auto-select the last added file
        if last_added_name and self.example_images:
            for idx, path in enumerate(self.example_images):
                if os.path.basename(path) == last_added_name:
                    self.current_example_idx = idx
                    self._update_ui()
                    break

    def delete_example_image(self):
        if not self.example_images: return
        path = self.example_images[self.current_example_idx]
        
        # Safety Check
        msg = "Delete this file?"
        if QMessageBox.question(self, "Delete File", msg, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        # [Fix] Release file handle & Retry logic via QTimer
        try:
            # 0. Cancel any pending metadata extraction for this file
            if self.metadata_worker:
                self.metadata_worker.cancel_path(path)
            
            # 1. Unload image from UI (CLEANUP)
            self.lbl_img.clear_memory()
            
            # 2. Clear from ImageLoader cache (important!)
            if self.image_loader:
                self.image_loader.remove_from_cache(path)
            
            # 3. Async delete with retry using QTimer to avoid locking UI
            def try_delete(attempt=0):
                if not os.path.exists(path):
                    self.load_examples(self.current_item_path)
                    self.status_message.emit("File permanently deleted.")
                    return
                try:
                    os.remove(path)
                    self.load_examples(self.current_item_path)
                    self.status_message.emit("File permanently deleted.")
                except PermissionError as pe:
                    if attempt < 4:
                        from PySide6.QtCore import QTimer
                        QTimer.singleShot(100, lambda: try_delete(attempt + 1))
                    else:
                        logging.warning(f"Delete failed after retries: {pe}")
                        QMessageBox.warning(self, "Error", f"Failed to delete file after retries:\n{pe}")
                        if os.path.exists(path):
                            self.lbl_img.set_media(path)
                except Exception as e:
                    logging.warning(f"Delete failed: {e}")
                    QMessageBox.warning(self, "Error", f"Failed to delete file:\n{e}")
                    if os.path.exists(path):
                        self.lbl_img.set_media(path)

            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, try_delete)
            
        except Exception as e:
             logging.warning(f"Delete preparation failed: {e}")
             QMessageBox.warning(self, "Error", f"Failed to prepare deletion:\n{e}")

    def open_example_folder(self):
        if not self.example_images: return
        f = os.path.dirname(self.example_images[0])
        try: os.startfile(f)
        except Exception as e: self.status_message.emit(f"Failed to open folder: {e}")

    def on_example_click(self):
        path = self.lbl_img.get_current_path()
        if not path: return
        if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS:
            return
        if os.path.exists(path):
            ZoomWindow(path, self).show()



    def save_example_metadata(self):
        if not self.example_images: return
        path = self.example_images[self.current_example_idx]
        
        ext = os.path.splitext(path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            return

        try:
            # Reconstruct parameters from UI
            full_text = self.meta_viewer.get_formatted_parameters()
             
            # Open Image and Update Metadata
            img = Image.open(path)
            img.load()
            
            metadata = PngInfo()
            
            # Preserve existing metadata except 'parameters'
            for k, v in img.info.items():
                if k == "parameters": continue
                if k in ["exif", "icc_profile"]: continue 
                if isinstance(v, str):
                    metadata.add_text(k, v)
            
            metadata.add_text("parameters", full_text)
            
            save_kwargs = {"pnginfo": metadata}
            if "exif" in img.info: save_kwargs["exif"] = img.info["exif"]
            if "icc_profile" in img.info: save_kwargs["icc_profile"] = img.info["icc_profile"]
            
            if ext == ".png":
                tmp_path = path + ".tmp.png"
                img.save(tmp_path, **save_kwargs)
                img.close()
                shutil.move(tmp_path, path)
                
                # [CACHE] Invalidate metadata cache since file was modified
                if self.metadata_worker:
                    self.metadata_worker.invalidate_cache(path)
                
                self._parse_and_display_meta(path)
                self.status_message.emit("Image metadata updated.")
            else:
                # Convert to PNG
                base = os.path.splitext(path)[0]
                new_path = base + ".png"
                
                img.save(new_path, format="PNG", **save_kwargs)
                img.close()
                
                # Delete original file safely
                try: 
                    os.remove(path)
                except Exception as e:
                    logging.warning(f"Failed to remove original file: {e}")
                
                self.status_message.emit("Converted to PNG and saved metadata.")
                # Reload list because filename changed, but try to keep selection on the new file
                # [CACHE] Invalidate old path cache (new file has different path anyway)
                if self.metadata_worker:
                    self.metadata_worker.invalidate_cache(path)
                
                self.load_examples(self.current_item_path, target_filename=os.path.basename(new_path))
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save metadata: {e}")

    def _parse_and_display_meta(self, path):
        self._clear_meta()
        self.lbl_wf_status.setText("Loading...")
        # [Optimization] Offload to worker
        if self.metadata_worker:
            self.metadata_worker.extract(path)
            
    def _on_metadata_ready(self, path, meta):
        # Verify if this is still the current item
        # If user clicked multiple times, path might differ from current_item_path
        # But for 'example' logic, self.current_item_path tracks the MAIN file (example list parent?).
        # Wait, load_examples sets self.current_item_path to the FOLDER or FILE?
        # self.example_images[self.current_example_idx] is the actual image being shown.
        
        current_img_path = None
        if self.example_images and 0 <= self.current_example_idx < len(self.example_images):
            current_img_path = self.example_images[self.current_example_idx]
            
        if not current_img_path or os.path.normpath(path) != os.path.normpath(current_img_path):
            return # Stale result
            
        try:
            # Update Status Icon based on standardized type
            if meta["type"] == "comfy":
                self.lbl_wf_status.setText("Workflow")
                self.lbl_wf_status.setToolTip("Contains ComfyUI Workflow (JSON)")
                self.lbl_wf_status.setObjectName("WorkflowStatus_Success")
            else:
                self.lbl_wf_status.setText("no workflow")
                self.lbl_wf_status.setToolTip("No ComfyUI workflow metadata found")
                self.lbl_wf_status.setObjectName("WorkflowStatus_Normal")
            
            # Force style reload
            self.lbl_wf_status.style().unpolish(self.lbl_wf_status)
            self.lbl_wf_status.style().polish(self.lbl_wf_status)
            
            # Populate UI
            self.meta_viewer.set_metadata(meta)
            
        except Exception as e: 
            logging.warning(f"Meta parse error: {e}")
            self.meta_viewer.txt_etc.setText(f"Error: {e}")

    def _clear_meta(self):
        self.meta_viewer.clear()
        self.lbl_wf_status.setText("No Workflow")
        self.lbl_wf_status.setObjectName("WorkflowStatus_Neutral")
        self.lbl_wf_status.style().unpolish(self.lbl_wf_status)
        self.lbl_wf_status.style().polish(self.lbl_wf_status)
        self._raw_civitai_resources = None # Legacy, maybe unused now? keeping safe.

    # _display_parameters, _parse_parameters_robust, _copy_to_clipboard REMOVED




    # [Memory Optimization]
    def stop_videos(self):
        """Stops and releases video resources in the example tab."""
        if hasattr(self, 'lbl_img'):
            if hasattr(self.lbl_img, 'release_resources'):
                self.lbl_img.release_resources()
            elif hasattr(self.lbl_img, '_stop_video_playback'):
                self.lbl_img._stop_video_playback()


