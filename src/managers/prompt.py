import os
import json
import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, 
    QAbstractItemView, QSplitter, QPushButton, QInputDialog, QMessageBox, QTextEdit, QDialog, QDialogButtonBox, QFileDialog, QApplication
)
from PySide6.QtGui import QClipboard, QTextOption
from PySide6.QtCore import Qt, QSize, QTimer

from .base import BaseManagerWidget
from .example import ExampleTabWidget
from ..ui_components import MarkdownNoteWidget
from ..core import SUPPORTED_EXTENSIONS, CACHE_DIR_NAME, calculate_structure_path
import uuid
import shutil

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QSizePolicy

# [Helper Class for Event Propagation & Advanced Wrapping]
class PromptTextEdit(QTextEdit):
    clicked = Signal()
    
    def __init__(self, text, bg_color="#f9f9f9", border_color="#ddd", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setText(text)
        
        # Appearance
        self.setFrameStyle(0) # No frame
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded) # Enable Scroll
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        # Wrapping Logic
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapAnywhere) 
        self.document().setDefaultTextOption(option)
        self.document().setDocumentMargin(0) # Remove internal document margin
        
        # Style
        # Style -> Moved to QSS
        # Removed inline stylesheet
        
        # Size Policy
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        
    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)
        
    def sizeHint(self):
        # Calculate height based on document
        doc_height = self.document().size().height()
        
        # Calculate Max Height (approx 10 lines) - Increased x2
        fm = self.fontMetrics()
        line_height = fm.lineSpacing()
        max_h = (line_height * 10) + 12 
        
        # Add minimal buffer for border/padding (4px padding * 2 = 8px + borders)
        # Since document margin is 0, doc_height is just text.
        # We need to add the CSS padding we set above (4px).
        # Actually let's set CSS padding to 4px, so total extra is ~10px.
        # But user said still too much.
        # Let's try matching exactly: doc_height + 10 (padding 4+4 + border 1+1).
        # If doc_height includes line spacing, it should be fine.
        
        final_height = min(int(doc_height + 10), max_h)
        return QSize(self.viewport().width(), final_height)
        
    
    def get_height_for_width(self, width):
        # Calculate height for a specific width without resizing
        # We clone the document to test layout
        doc = self.document().clone()
        doc.setTextWidth(width)
        
        doc_height = doc.size().height()
        
        fm = self.fontMetrics()
        line_height = fm.lineSpacing()
        max_h = (line_height * 10) + 12
        
        final_height = min(int(doc_height + 10), max_h)
        return final_height

class PromptListItemWidget(QWidget):
    copy_requested = Signal(str, str) # text, type
    clicked = Signal() # New signal for selection
    
    def __init__(self, positive, negative, tags, parent=None):
        super().__init__(parent)
        self.positive = positive
        self.negative = negative
        self.tags = tags
        self._is_selected = False
        
        # ... (Layout setup is done in UI, but we need to know structure for calc)
        # Main margins: 4
        # Spacing: 6
        # Row Spacing: 10
        # Button: 28 
        
        # We keep references to text widgets if needed, but better to recalc.
        # Actually to be efficient, let's just use the widget instances if they exist?
        # But we need to call get_height_for_width on them.
        self.txt_pos = None
        self.txt_neg = None
        self.lbl_tags = None

        # Main layout: Vertical stack of rows
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4) 
        main_layout.setSpacing(6)
        main_layout.setAlignment(Qt.AlignTop)
        
        # Positive Row (Always Show)
        pos_row = QHBoxLayout()
        pos_row.setSpacing(10)
        pos_row.setContentsMargins(0, 0, 0, 0)
        
        btn_copy_pos = QPushButton("📋")
        btn_copy_pos.setFixedWidth(28)
        btn_copy_pos.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        btn_copy_pos.setToolTip("Copy Positive")
        btn_copy_pos.setCursor(Qt.PointingHandCursor)
        btn_copy_pos.clicked.connect(lambda: self.copy_requested.emit(self.positive, "Positive"))
        pos_row.addWidget(btn_copy_pos)
        
        self.txt_pos = PromptTextEdit(positive, bg_color="#f9f9f9", border_color="#ddd")
        self.txt_pos.setPlaceholderText("Positive Prompt...") # Placeholder for empty
        self.txt_pos.setObjectName("PromptItemPositive")
        self.txt_pos.clicked.connect(self._propagate_click)
        pos_row.addWidget(self.txt_pos, 1) 
        
        main_layout.addLayout(pos_row)

        # Negative Row (Always Show)
        neg_row = QHBoxLayout()
        neg_row.setSpacing(10)
        neg_row.setContentsMargins(0, 0, 0, 0)
        
        btn_copy_neg = QPushButton("📋")
        btn_copy_neg.setFixedWidth(28)
        btn_copy_neg.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        btn_copy_neg.setToolTip("Copy Negative")
        btn_copy_neg.setCursor(Qt.PointingHandCursor)
        btn_copy_neg.clicked.connect(lambda: self.copy_requested.emit(self.negative, "Negative"))
        neg_row.addWidget(btn_copy_neg)
        
        self.txt_neg = PromptTextEdit(negative, bg_color="#fff5f5", border_color="#eec")
        self.txt_neg.setPlaceholderText("Negative Prompt...") # Placeholder for empty
        self.txt_neg.setObjectName("PromptItemNegative")
        self.txt_neg.clicked.connect(self._propagate_click)
        neg_row.addWidget(self.txt_neg, 1) 
        
        main_layout.addLayout(neg_row)

        # Tags (Show if exists, or maybe just hidden if empty?) 
        # Requirement is about 'Empty Prompt' label. 
        # If tags exist, show them.
        if tags:
            tag_str = ", ".join(tags)
            self.lbl_tags = QLabel(f"Tags: {tag_str}")
            self.lbl_tags.setWordWrap(True)
            self.lbl_tags.setObjectName("PromptTags")
            main_layout.addWidget(self.lbl_tags)

            
        # Size Policy
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        
        # [Enhancement] Enforce minimum height for the whole widget
        self.setMinimumHeight(120) # Approx 2x the original visual feel for empty/small items

    # ... (rest of methods)

    def calculate_height(self, width):
        # Calculate full height for a given width
        # Width available for text:
        # parent_width - margins_left_right (4+4=8) - button (28) - spacing (10)
        text_avail_width = width - 8 - 28 - 10
        if text_avail_width <= 0: text_avail_width = 100 # Fallback
        
        total_h = 4 # Top margin
        
        if True: # Always show Positive now
            t_h = self.txt_pos.get_height_for_width(text_avail_width)
            row_h = max(28, t_h)
            total_h += row_h
        
        if True: # Always show Negative now
            total_h += 6 # Spacing
            t_h = self.txt_neg.get_height_for_width(text_avail_width)
            row_h = max(28, t_h)
            total_h += row_h
            
        if self.tags:
            # Tags label height
            # Label wordwrap calculation is tricky without instance.
            # Assuming simple height: heightForWidth.
            # Label width = width - 8
            if self.positive or self.negative: total_h += 6
            lbl_w = width - 8
            if self.lbl_tags:
                total_h += self.lbl_tags.heightForWidth(lbl_w)
                
        total_h += 4 # Bottom margin
        
        # [Enhancement] Enforce minimum height here too
        return max(total_h, 120)

    def _propagate_click(self):
        # Emit clicked signal so parent can handle selection
        self.clicked.emit()

    def mousePressEvent(self, event):
        # If user clicks background, select this row.
        self.clicked.emit()
        super().mousePressEvent(event)
        
        # Find parent QListWidget
        parent = self.parent()
        while parent and not isinstance(parent, QListWidget):
            parent = parent.parent()
            
        if isinstance(parent, QListWidget):
             # Find item for this widget
             # This is O(N) but reliable. Or use itemAt?
             # Actually, since this widget IS the item widget:
             # We can iterate to find which item has this widget.
             # Or more robust:
             # Just emit a signal that Manager catches? No, Manager connects to ItemSelectionChanged.
             # We need to trigger ItemSelectionChanged.
             
             # Efficient way:
             for i in range(parent.count()):
                 it = parent.item(i)
                 if parent.itemWidget(it) == self:
                     parent.setCurrentItem(it)
                     break
        
        super().mousePressEvent(event)

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QColor, QPen
        super().paintEvent(event)
        
        if self._is_selected:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            
            # Draw Selection Border
            rect = self.rect()
            rect.adjust(1, 1, -1, -1) # adjust slightly
            pen = QPen(QColor("dodgerblue"), 3) # Thick Blue Border
            painter.setPen(pen)
            painter.drawRoundedRect(rect, 4, 4)

    def set_selected(self, selected):
        self._is_selected = selected
        self.update() # Trigger repaint

class PromptEditDialog(QDialog):
    def __init__(self, positive, negative, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Prompt")
        self.resize(600, 400)
        layout = QVBoxLayout(self)
        
        # Positive
        layout.addWidget(QLabel("Positive Prompt:"))
        self.txt_positive = QTextEdit()
        self.txt_positive.setPlainText(positive)
        self.txt_positive.setStyleSheet("background-color: #f0fff0;") # Keeping inline for dynamic hint
        layout.addWidget(self.txt_positive)
        
        # Negative
        layout.addWidget(QLabel("Negative Prompt:"))
        self.txt_negative = QTextEdit()
        self.txt_negative.setPlainText(negative)
        self.txt_negative.setStyleSheet("background-color: #fff0f0;") # Keeping inline for dynamic hint
        layout.addWidget(self.txt_negative)
        
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_data(self):
        return self.txt_positive.toPlainText(), self.txt_negative.toPlainText()

class PromptManagerWidget(BaseManagerWidget):
    def __init__(self, directories, app_settings, parent_window=None):
        self.parent_window = parent_window
        
        # Filter directories for 'prompt' mode
        prompt_dirs = {k: v for k, v in directories.items() if v.get("mode") == "prompt"}
        super().__init__(prompt_dirs, SUPPORTED_EXTENSIONS["prompt"], app_settings)
        
        self.current_prompt_data = [] # List of dicts
        self.current_json_path = None
        self.current_prompt_index = -1

    def set_directories(self, directories):
        # Filter directories for 'prompt' mode
        prompt_dirs = {k: v for k, v in directories.items() if v.get("mode") == "prompt"}
        super().set_directories(prompt_dirs)
        # Update ExampleTab directories too
        if hasattr(self, 'tab_example'):
            self.tab_example.directories = directories

    # [Fix] Override mode
    def get_mode(self): return "prompt"

    def init_center_panel(self):
        # List widget for displaying prompts chunks
        self.prompt_list = QListWidget()
        self.prompt_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.prompt_list.setSpacing(5) # Add spacing between items
        self.prompt_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel) # Smooth scrolling
        
        # [Enhancement] Use ItemWidget instead of Delegate
        # self.prompt_list.setItemDelegate(PromptDelegate(self.prompt_list))
        self.prompt_list.setResizeMode(QListWidget.Adjust) 
        
        # [UI] Light gray selection color
        # [UI] Light gray selection color
        self.prompt_list.setObjectName("PromptList")
        
        self.prompt_list.itemSelectionChanged.connect(self.on_prompt_selected)
        
        self.center_layout.addWidget(self.prompt_list)
        
        # [Fix] Install Event Filter for Resizing
        self.prompt_list.installEventFilter(self)
        
        # [Fix] Disable Horizontal Scrollbar to force word wrap vertical expansion
        self.prompt_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Buttons Control
        btn_layout = QHBoxLayout()
        self.btn_add_item = QPushButton("➕ New")
        self.btn_add_item.setToolTip("Add a new prompt item")
        self.btn_add_item.clicked.connect(self.add_prompt_item)
        
        self.btn_edit_item = QPushButton("✏️ Edit")
        self.btn_edit_item.setToolTip("Edit selected prompt text")
        self.btn_edit_item.clicked.connect(self.edit_prompt_item)

        self.btn_del_item = QPushButton("➖ Remove")
        self.btn_del_item.setToolTip("Remove selected prompt item")
        self.btn_del_item.clicked.connect(self.remove_prompt_item)
        
        # Move Buttons
        self.btn_up_item = QPushButton("⬆️")
        self.btn_up_item.setToolTip("Move selected item up")
        self.btn_up_item.setFixedWidth(40)
        self.btn_up_item.clicked.connect(self.move_item_up)
        
        self.btn_down_item = QPushButton("⬇️")
        self.btn_down_item.setToolTip("Move selected item down")
        self.btn_down_item.setFixedWidth(40)
        self.btn_down_item.clicked.connect(self.move_item_down)
        
        btn_layout.addWidget(self.btn_add_item)
        btn_layout.addWidget(self.btn_edit_item)
        btn_layout.addWidget(self.btn_del_item)
        btn_layout.addWidget(self.btn_up_item)
        btn_layout.addWidget(self.btn_down_item)
        self.center_layout.addLayout(btn_layout)

    def eventFilter(self, obj, event):
        if obj == self.prompt_list and event.type() == event.Type.Resize:
            self._adjust_list_items()
        
        return super().eventFilter(obj, event)

    def _adjust_list_items(self):
        """Force update item sizes based on current viewport width to fix word wrap resizing."""
        # Calculate available width
        width = self.prompt_list.viewport().width()
        # Enforce minimum width to prevent collapse
        if width < 100: width = 100
        
        for i in range(self.prompt_list.count()):
            item = self.prompt_list.item(i)
            widget = self.prompt_list.itemWidget(item)
            if widget and hasattr(widget, 'calculate_height'):
                # 1. Calculate EXACT height needed for this width
                new_height = widget.calculate_height(width)
                
                # 2. Update item size hint
                current_hint = item.sizeHint()
                if current_hint.height() != new_height or current_hint.width() != width:
                    item.setSizeHint(QSize(width, new_height))

        
    def init_left_bottom(self, layout):
        # Container for buttons
        btn_container = QWidget()
        hbox = QHBoxLayout(btn_container)
        hbox.setContentsMargins(0, 0, 0, 0)
        
        self.btn_new_file = QPushButton("➕ New File")
        self.btn_new_file.setToolTip("Create a new JSON prompt file in the selected directory")
        self.btn_new_file.clicked.connect(self.create_new_file)
        
        self.btn_open_folder = QPushButton("📂")
        self.btn_open_folder.setToolTip("Open folder of the currently selected file")
        self.btn_open_folder.clicked.connect(self.open_current_folder)
        
        self.btn_rename_file = QPushButton("✏️ Rename")
        self.btn_rename_file.setToolTip("Rename the selected prompt file")
        self.btn_rename_file.clicked.connect(self.rename_prompt_file)
        
        self.btn_remove_file = QPushButton("🗑️ Remove")
        self.btn_remove_file.setToolTip("Permanently delete the selected prompt file")
        self.btn_remove_file.clicked.connect(self.remove_prompt_file)

        self.btn_move_file = QPushButton("📦 Move")
        self.btn_move_file.setToolTip("Move selected prompt file(s) to another folder")
        self.btn_move_file.clicked.connect(self.move_prompt_files)
        
        hbox.addWidget(self.btn_new_file, 1) # Expand
        hbox.addWidget(self.btn_open_folder, 0) # Fixed size
        hbox.addWidget(self.btn_remove_file, 1) # Expand
        hbox.addWidget(self.btn_rename_file, 1) # Expand
        hbox.addWidget(self.btn_move_file, 1) # Expand
        
        layout.addWidget(btn_container)

    def rename_prompt_file(self):
        """Renames the selected prompt JSON file and its cache directory."""
        if not self.current_json_path or not os.path.exists(self.current_json_path):
            QMessageBox.warning(self, "Warning", "No prompt file selected.")
            return

        old_filename = os.path.basename(self.current_json_path)
        old_base = os.path.splitext(old_filename)[0]
        
        # 1. Get New Name
        new_base, ok = QInputDialog.getText(self, "Rename File", "New Name:", text=old_base)
        if not ok or not new_base: return
        
        new_base = new_base.strip()
        if new_base == old_base: return
        
        # Validation
        if any(c in new_base for c in '<>:"/\\|?*'):
             QMessageBox.warning(self, "Invalid Name", "Filename contains invalid characters.")
             return

        dir_path = os.path.dirname(self.current_json_path)
        new_filename = new_base + ".json"
        new_path = os.path.join(dir_path, new_filename)
        
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Error", "A file with that name already exists.")
            return

        # 2. Unload resources
        self.tab_note.set_text("")
        self.tab_example.unload_current_examples()
        QApplication.processEvents()

        try:
            # 3. Rename Cache Directory
            # Old cache path calculation
            old_cache_dir = calculate_structure_path(self.current_json_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            
            # New cache path calculation (using fake new path)
            new_cache_dir = calculate_structure_path(new_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            
            if os.path.exists(old_cache_dir):
                if os.path.exists(new_cache_dir):
                     QMessageBox.warning(self, "Error", f"Target cache directory already exists: {os.path.basename(new_cache_dir)}")
                     return
                try:
                    os.rename(old_cache_dir, new_cache_dir)
                except OSError as e:
                    QMessageBox.critical(self, "Error", f"Failed to rename cache directory: {e}")
                    return

            # 4. Rename Main JSON File
            os.rename(self.current_json_path, new_path)
            
            # [Fix] Update current path immediately to prevent loading old path
            self.current_json_path = new_path
            self.current_path = new_path 
            
            self.show_status_message(f"Renamed to {new_filename}")
            self.refresh_list()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Critical error during rename: {e}")

    def remove_prompt_file(self):
        """Permanently deletes the selected prompt JSON file and its cache directory."""
        if not self.current_json_path or not os.path.exists(self.current_json_path):
            QMessageBox.warning(self, "Warning", "No prompt file selected.")
            return

        filename = os.path.basename(self.current_json_path)
        
        # Confirm Delete
        reply = QMessageBox.question(
            self, "Confirm Delete", 
            f"Are you sure you want to PERMANENTLY delete:\n{filename}\n\nThis will also delete all associated notes and example images.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply != QMessageBox.Yes: return

        # [Fix] Unload resources (images/videos) to release file handles before deletion
        self.tab_example.unload_current_examples()

        try:
            # 2. Delete Cache Directory
            cache_dir = calculate_structure_path(self.current_json_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)

            # 3. Delete Main JSON File
            os.remove(self.current_json_path)
            
            self.current_json_path = None
            self.current_prompt_data = []
            self.prompt_list.clear()
            
            self.show_status_message(f"Deleted {filename}")
            self.refresh_list()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error during delete: {e}")

    def move_prompt_files(self):
        """
        Moves the selected prompt file(s) to a new target directory within the current root.
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
        self.tab_example.unload_current_examples()
        self.tab_note.set_text("")
        QApplication.processEvents()

        total_moved = 0
        all_errors = []

        for item in selected_items:
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
            
        self.current_json_path = None
        self.current_prompt_data = []
        self.prompt_list.clear()
        self.refresh_list()


    def open_current_folder(self):
        item = self.tree.currentItem()
        target_path = None
        
        if item:
            path = item.data(0, Qt.UserRole)
            if path and os.path.exists(path):
                if os.path.isfile(path):
                    target_path = os.path.dirname(path)
                else:
                    target_path = path
        
        if target_path and os.path.exists(target_path):
            os.startfile(target_path)
        else:
            QMessageBox.warning(self, "Error", "No valid folder or file selected.")

    def init_right_panel(self):
        # Tabs for Detail/Note and Example
        from PySide6.QtWidgets import QTabWidget
        self.right_tabs = QTabWidget()
        
        # Tab 1: Note (JSON Detail)
        self.tab_note = MarkdownNoteWidget()
        self.tab_note.save_requested.connect(self.save_prompt_note)
        self.tab_note.set_media_handler(self.handle_media_insert) # Base class method? No, need to implement or remove
        self.right_tabs.addTab(self.tab_note, "Note")
        
        # Tab 2: Example
        # We pass self.directories. If they are updated, set_directories handles it.
        self.tab_example = ExampleTabWidget(self.directories, self.app_settings, self, self.image_loader_thread, cache_root=self.get_cache_dir(), mode="prompt")
        self.tab_example.status_message.connect(self.show_status_message)
        self.right_tabs.addTab(self.tab_example, "Example")
        
        self.right_layout.addWidget(self.right_tabs)
        
    def handle_media_insert(self, mtype):
        if self.current_prompt_index < 0 or not self.current_json_path:
            QMessageBox.warning(self, "Error", "No prompt item selected.")
            return None
            
        if mtype not in ["image", "video"]: return None
        
        # Select File
        filters = "Media (*.png *.jpg *.jpeg *.webp *.mp4 *.webm *.gif)"
        file_path, _ = QFileDialog.getOpenFileName(self, f"Select {mtype.title()}", "", filters)
        if not file_path: return None
        
        # Calculate target relative path: <json_stem>/<UUID>/assets
        json_stem = os.path.splitext(os.path.basename(self.current_json_path))[0]
        
        # Get Item Data
        item_data = self.current_prompt_data[self.current_prompt_index]
        if "id" not in item_data:
             QMessageBox.warning(self, "Error", "Prompt item has no ID. Please select it again to generate one.")
             return None
             
        # Using UUID as folder name
        # [Fix] Manually handle path to avoid flattening by calculate_structure_path
        base_cache = calculate_structure_path(self.current_json_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
        assets_dir = os.path.join(base_cache, item_data["id"], "assets")
        
        if not os.path.exists(assets_dir): os.makedirs(assets_dir)
        
        # Copy File
        name = os.path.basename(file_path)
        dest_path = os.path.join(assets_dir, name)
        
        try:
            shutil.copy2(file_path, dest_path)
            
            # Return Markdown tag (Relative filename)
            # Since base_path will be set to assets_dir, just filename is enough
            ext = os.path.splitext(name)[1].lower()
            if ext in ['.mp4', '.webm', '.mkv']:
                return f'<video src="{name}" controls width="100%"></video>'
            else:
                return f"![{name}]({name})"
        except Exception as e:
            self.show_status_message(f"Failed to copy media: {e}")
            return None 
    
    def on_tree_select(self):
        item = self.tree.currentItem()
        if not item: return
        
        path = item.data(0, Qt.UserRole)
        type_ = item.data(0, Qt.UserRole + 1)
        
        if type_ == "file" and path:
            # [Fix] Validate file existence to prevent ghost errors during rename/delete
            if not os.path.exists(path):
                return
                
            self.current_path = path # BaseManager tracks this
            self._load_prompt_content(path)
            
    def _load_prompt_content(self, path):
        self.current_json_path = path
        self.current_prompt_data = []
        self.prompt_list.clear()
        self.tab_note.set_text("")
        self.tab_example.unload_current_examples()
        self.current_prompt_index = -1
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            if isinstance(data, list):
                self.current_prompt_data = data
            elif isinstance(data, dict):
                 # Handle single object or wrap it? Let's assume list structure as per req.
                 # If it's a dict but has "prompts" key? 
                 # Let's support list of objects.
                 self.current_prompt_data = [data]
            
            # [Migration] Content -> Positive
            migrated_count = 0
            for idx, entry in enumerate(self.current_prompt_data):
                dirty_entry = False
                if "content" in entry and "positive" not in entry:
                    entry["positive"] = entry["content"]
                    entry["negative"] = ""
                    del entry["content"]
                    dirty_entry = True
                
                # Ensure keys exist
                if "positive" not in entry: entry["positive"] = ""
                if "negative" not in entry: entry["negative"] = ""
                
                if dirty_entry: migrated_count += 1

            # [Migration] Check for missing IDs and Migrate Folders
            dirty = False
            base_cache_path = calculate_structure_path(path, self.get_cache_dir(), self.directories, mode=self.get_mode())
            # base_cache_path is roughly: cache/prompt/<json_filename>
            
            migrated_count = 0
            
            for idx, entry in enumerate(self.current_prompt_data):
                if "id" not in entry:
                    uid = str(uuid.uuid4())
                    entry["id"] = uid
                    dirty = True
                    
                    # Migration Logic: Check if legacy folder exists for this index
                    legacy_path = os.path.join(base_cache_path, str(idx))
                    new_path = os.path.join(base_cache_path, uid)
                    
                    if os.path.exists(legacy_path) and not os.path.exists(new_path):
                        try:
                            # Ensure parent existence not strictly needed as legacy exists
                            os.rename(legacy_path, new_path)
                            logging.info(f"Migrated item {idx} folder to UUID {uid}")
                            migrated_count += 1
                        except OSError as e:
                            logging.error(f"Migration failed for item {idx}: {e}")
            
            if dirty:
                self._save_current_data()
                if migrated_count > 0:
                    self.show_status_message(f"Migrated {migrated_count} item folders to new ID format.")
            
            # Populate List
            self.refresh_current_file()

            
            # Select first if available
            if self.prompt_list.count() > 0:
                self.prompt_list.setCurrentRow(0)
            
            size = os.path.getsize(path)
            self.show_status_message(f"Loaded: {os.path.basename(path)} ({self.format_size(size)})")
            
        except Exception as e:
            logging.error(f"Error loading prompt JSON: {e}")
            self.prompt_list.addItem(f"Error loading file: {e}")
            self.show_status_message(f"Error loading file: {e}")

    def on_prompt_selected(self):
        selected_items = self.prompt_list.selectedItems()
        if not selected_items:
            self.current_prompt_index = -1
            self.tab_note.set_text("")
            self.tab_example.unload_current_examples()
            
            # Clear all highlights
            for i in range(self.prompt_list.count()):
                widget = self.prompt_list.itemWidget(self.prompt_list.item(i))
                if widget and isinstance(widget, PromptListItemWidget):
                    widget.set_selected(False)
            return

        item = selected_items[0]
        idx = item.data(Qt.UserRole)
        self.current_prompt_index = idx
        
        # [Visual Fix] Update Highlight State
        # Loop all items to clear others and enable current
        # This is okay for small lists, but if slow, track previous selected widget.
        for i in range(self.prompt_list.count()):
            it = self.prompt_list.item(i)
            widget = self.prompt_list.itemWidget(it)
            if widget and isinstance(widget, PromptListItemWidget):
                widget.set_selected(it == item)

        if 0 <= idx < len(self.current_prompt_data):
            entry = self.current_prompt_data[idx]
            
            # Load Note
            self.tab_note.set_text(entry.get("note", ""))
            
            # Load Example
            item_id = entry.get("id")
            if item_id:
                # Calculate resource path
                # cache/prompt/<stem>/<UUID>
                base_cache = calculate_structure_path(self.current_json_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
                custom_path = os.path.join(base_cache, item_id)
                
                # [Fix] Set base path for relative image resolution (assets folder)
                assets_path = os.path.join(custom_path, "assets")
                if hasattr(self, 'tab_note'):
                     self.tab_note.set_base_path(assets_path)
                
                self.tab_example.load_examples(self.current_json_path, custom_cache_path=custom_path)
        
    def create_new_file(self):
        # Determine target directory
        item = self.tree.currentItem()
        target_dir = None
        
        if item:
            path = item.data(0, Qt.UserRole)
            type_ = item.data(0, Qt.UserRole + 1)
            
            if type_ == "folder":
                target_dir = path
            elif type_ == "file":
                target_dir = os.path.dirname(path)
        
        # Fallback to first available root directory if nothing selected
        if not target_dir:
            if self.directories:
                # Use the path of the first registered directory
                first_key = next(iter(self.directories))
                target_dir = self.directories[first_key].get("path")
            else:
                QMessageBox.warning(self, "Error", "No directories registered.")
                return

        if not target_dir or not os.path.exists(target_dir):
            QMessageBox.warning(self, "Error", f"Invalid target directory: {target_dir}")
            return

        # Get Filename
        name, ok = QInputDialog.getText(self, "New File", "Enter file name (without .json):")
        if not ok or not name.strip(): return
        
        filename = name.strip()
        if not filename.lower().endswith(".json"):
            filename += ".json"
            
        full_path = os.path.join(target_dir, filename)
        
        if os.path.exists(full_path):
            QMessageBox.warning(self, "Error", "File already exists.")
            return
            
        # Create empty JSON list
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                json.dump([], f, indent=2)
            
            self.show_status_message(f"Created: {filename}")
            
            # Refresh Tree and Select
            self.refresh_list()
            # Note: Selecting the new item programmatically is complex because of async tree/workers. 
            # Ideally we just refresh.
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create file: {e}")

    def add_prompt_item(self):
        if not self.current_json_path:
            QMessageBox.warning(self, "Error", "No prompt file selected.")
            return

        # Default new item with UUID
        new_item = {
            "id": str(uuid.uuid4()),
            "positive": "",
            "negative": "",
            "note": "",
            "tags": []
        }
        
        self.current_prompt_data.append(new_item)
        self._save_current_data()
        self.refresh_current_file()
        
        # Select the new item
        count = self.prompt_list.count()
        if count > 0:
            self.prompt_list.setCurrentRow(count - 1)

    def edit_prompt_item(self):
        if self.current_prompt_index < 0: return
        item_data = self.current_prompt_data[self.current_prompt_index]
        
        # Open Custom Dialog
        dlg = PromptEditDialog(item_data.get("positive", ""), item_data.get("negative", ""), self)
        
        if dlg.exec():
            pos, neg = dlg.get_data()
            item_data["positive"] = pos
            item_data["negative"] = neg
            self._save_current_data()
            self.refresh_current_file()
            # Restore selection
            self.prompt_list.setCurrentRow(self.current_prompt_index)

    def move_item_up(self):
        idx = self.current_prompt_index
        if idx <= 0: return
        
        # Swap
        self.current_prompt_data[idx], self.current_prompt_data[idx-1] = self.current_prompt_data[idx-1], self.current_prompt_data[idx]
        
        self._save_current_data()
        self.refresh_current_file()
        self.prompt_list.setCurrentRow(idx-1)

    def move_item_down(self):
        idx = self.current_prompt_index
        if idx < 0 or idx >= len(self.current_prompt_data) - 1: return
        
        # Swap
        self.current_prompt_data[idx], self.current_prompt_data[idx+1] = self.current_prompt_data[idx+1], self.current_prompt_data[idx]
        
        self._save_current_data()
        self.refresh_current_file()
        self.prompt_list.setCurrentRow(idx+1)

    def remove_prompt_item(self):
        if self.current_prompt_index < 0: return
        
        item_data = self.current_prompt_data[self.current_prompt_index]
        
        msg = "Permanently delete this prompt?\n\nAll associated images and data will be lost forever."
        if QMessageBox.question(self, "Delete Prompt", msg, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        
        # [Fix] Unload resources (images/videos) to release file handles before deletion
        self.tab_example.unload_current_examples()
        
        # [Cleanup] Delete Resource Folder

        try:
            item_id = item_data.get("id")
            if item_id and self.current_json_path:
                 base_cache = calculate_structure_path(self.current_json_path, self.get_cache_dir(), self.directories, mode=self.get_mode())
                 # Folder to delete: cache/prompt/<json>/<UUID>
                 target_dir = os.path.join(base_cache, item_id)
                 
                 if os.path.exists(target_dir):
                     shutil.rmtree(target_dir)
                     logging.info(f"Deleted resource folder for prompt {item_id}")
            
        except OSError as e:
            logging.error(f"Failed to cleanup folder: {e}")
            QMessageBox.warning(self, "Warning", f"Failed to delete resource folder:\\n{e}")

        # Delete from list
        del self.current_prompt_data[self.current_prompt_index]
        self._save_current_data()
        self.refresh_current_file()

    def _save_current_data(self):
        if not self.current_json_path: return
        try:
            with open(self.current_json_path, 'w', encoding='utf-8') as f:
                json.dump(self.current_prompt_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save JSON: {e}")

    def refresh_current_file(self):
        if self.current_json_path:
            # Reload from list memory
            self.prompt_list.clear() # Single clear is enough
            
            for idx, entry in enumerate(self.current_prompt_data):
                pos = entry.get("positive", "")
                neg = entry.get("negative", "")
                tags = entry.get("tags", [])
                
                item = QListWidgetItem()
                item.setData(Qt.UserRole, idx)
                self.prompt_list.addItem(item)
                
                # Create and set custom widget
                widget = PromptListItemWidget(pos, neg, tags)
                widget.copy_requested.connect(self._on_copy_requested)
                
                # Connect clicked signal to select this item
                widget.clicked.connect(lambda item=item: self.prompt_list.setCurrentItem(item))
                
                # Assign widget to item
                item.setSizeHint(widget.sizeHint())
                self.prompt_list.setItemWidget(item, widget)

            QTimer.singleShot(0, self._adjust_list_items)



    def _on_copy_requested(self, text, ptype):
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            self.show_status_message(f"Copied {ptype} prompt to clipboard")
        else:
            self.show_status_message(f"{ptype} prompt is empty")

    def save_prompt_note(self, text):
        if self.current_prompt_index < 0 or not self.current_json_path:
            return
            
        # Update memory
        self.current_prompt_data[self.current_prompt_index]["note"] = text
        
        # Save to file
        try:
            with open(self.current_json_path, 'w', encoding='utf-8') as f:
                json.dump(self.current_prompt_data, f, indent=2, ensure_ascii=False)
            self.show_status_message("Prompt note saved.")
        except Exception as e:
            logging.error(f"Failed to save prompt json: {e}")
            self.show_status_message(f"Save failed: {e}")


