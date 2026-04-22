import os
import time
import shutil
import re
import hashlib
import json
import logging
import concurrent.futures
from collections import deque, OrderedDict

# [Infra] PySide6 Imports
from PySide6.QtCore import QThread, Signal, QMutex, QWaitCondition, Qt, QBuffer, QByteArray, QFile, QIODevice
from PySide6.QtGui import QImage, QImageReader

# [Refactor] Services
from .services.api_service import ApiService
from .services.file_service import FileService

from .metadata import standardize_metadata
from .core import (
    QMutexWithLocker, 
    sanitize_filename, 
    calculate_structure_path,
    HAS_MARKDOWNIFY,
    HAS_PILLOW,
    SUPPORTED_EXTENSIONS,
    PREVIEW_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MAX_FILE_LOAD_BYTES,
    CACHE_DIR_NAME,
    BASE_DIR
)
from .utils.network import NetworkClient

# Optional dependencies
if HAS_MARKDOWNIFY:
    import markdownify

#Fn: Utility
def format_size(s):
    p=2**10; n=0; l={0:'', 1:'K', 2:'M', 3:'G'}
    while s > p: s/=p; n+=1
    return f"{s:.2f} {l.get(n,'T')}B"

# ==========================================
# Region: Media Workers (Image, Thumbnail)
# ==========================================
class ImageLoader(QThread):
    image_loaded = Signal(str, QImage) 

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ImageLoaderThread")
        self.queue = deque()
        self.mutex = QMutex()
        self.condition = QWaitCondition()
        self._is_running = True
        
        # [Cache] LRU Cache
        self.cache = OrderedDict()
        self.CACHE_SIZE = 100 # [Optimize] Increased cache size for tree thumbnails
    
    def __del__(self):
        try:
            if hasattr(self, 'wait'):
                self.wait()
        except RuntimeError as e:
            logging.debug(f"[ImageLoader] RuntimeError during cleanup: {e}")

    def load_image(self, path, target_width=None, clear_queue=True, resolve_preview=False):
        cache_key = f"{path}::{target_width}"
        with QMutexWithLocker(self.mutex):
             # Check cache first
             if cache_key in self.cache:
                 self.cache.move_to_end(cache_key) # Mark as recently used
                 self.image_loaded.emit(path, self.cache[cache_key])
                 return
        
             if os.path.isdir(path):
                 return # Skip directories

             if clear_queue:
                 self.queue.clear() # already locked
             self.queue.append((path, target_width, resolve_preview))
             self.condition.wakeOne()
             
    def clear_thumbnail_queue(self):
        with QMutexWithLocker(self.mutex):
             from .core import THUMBNAIL_SIZES
             self.queue = deque([item for item in self.queue if item[1] not in THUMBNAIL_SIZES])
            
    def clear_queue(self):
        with QMutexWithLocker(self.mutex):
            self.queue.clear()

    def remove_from_cache(self, path):
        with QMutexWithLocker(self.mutex):
             keys_to_remove = [k for k in list(self.cache.keys()) if str(k).startswith(f"{path}::")]
             for k in keys_to_remove:
                 del self.cache[k]

    def stop(self):
        logging.debug(f"[ImageLoader] Stop requested. is_running={self._is_running}")
        self._is_running = False
        with QMutexWithLocker(self.mutex):
            self.condition.wakeAll()
        logging.debug("[ImageLoader] WakeAll sent.")

    def run(self):
        logging.debug("[ImageLoader] Thread START")
        while self._is_running:
            self.mutex.lock()
            if not self._is_running:
                logging.debug("[ImageLoader] Loop exit check: Not running. Unlocking and breaking.")
                self.mutex.unlock()
                break
                
            if not self.queue:
                logging.debug("[ImageLoader] Queue empty. Waiting...")
                self.condition.wait(self.mutex, 500)
                logging.debug(f"[ImageLoader] Woke up. is_running={self._is_running}")
            
            if not self._is_running:
                logging.debug("[ImageLoader] Post-wait exit check. Unlocking and breaking.")
                self.mutex.unlock()
                break
            
            path = None
            target_width = None
            resolve_preview = False
            if self.queue:
                item_data = self.queue.popleft()
                path = item_data[0]
                target_width = item_data[1]
                resolve_preview = item_data[2] if len(item_data) > 2 else False
                logging.debug(f"[ImageLoader] Popped: {path}")
            
            self.mutex.unlock()

            if path:
                # [Optimize] Resolve preview path in background thread, not UI thread
                actual_path = path
                if resolve_preview:
                    from .core import PREVIEW_EXTENSIONS, VIDEO_EXTENSIONS
                    ext = os.path.splitext(path)[1].lower()
                    if ext in VIDEO_EXTENSIONS:
                        # Skip video files entirely
                        self.image_loaded.emit(path, QImage())
                        continue
                    elif ext in {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}:
                        actual_path = path  # Image file IS the thumbnail
                    else:
                        # Model file: search for associated preview
                        base = os.path.splitext(path)[0]
                        found = False
                        for p_ext in PREVIEW_EXTENSIONS:
                            if p_ext in VIDEO_EXTENSIONS:
                                continue
                            p = base + p_ext
                            if os.path.exists(p):
                                actual_path = p
                                found = True
                                break
                        if not found:
                            # No preview file exists, emit empty
                            self.image_loaded.emit(path, QImage())
                            continue
                try:
                    image = QImage()
                    if os.path.exists(actual_path):
                        ext = os.path.splitext(actual_path)[1].lower()
                        if ext in {'.mp4', '.webm', '.mkv', '.avi', '.mov', '.gif'}:
                            pass 
                        else:
                            f_size = os.path.getsize(actual_path)
                            if f_size > MAX_FILE_LOAD_BYTES:
                                 logging.warning(f"Skipping large file ({f_size} bytes): {actual_path}")
                            else:
                                # [Optimize] Prevent File Lock & Python overhead by using QFile
                                qfile = QFile(actual_path)
                                if qfile.open(QIODevice.ReadOnly):
                                    byte_array = qfile.readAll()
                                    qfile.close()

                                    buffer = QBuffer(byte_array)
                                    buffer.open(QIODevice.ReadOnly)

                                    reader = QImageReader(buffer)
                                    reader.setAutoTransform(True)
                                    
                                    if target_width:
                                        orig_size = reader.size()
                                        if orig_size.isValid() and (orig_size.width() > target_width or orig_size.height() > target_width):
                                             reader.setScaledSize(orig_size.scaled(target_width, target_width, Qt.KeepAspectRatio))
                                    
                                    loaded = reader.read()
                                    if not loaded.isNull():
                                        if not loaded.hasAlphaChannel():
                                             image = loaded.convertToFormat(QImage.Format_RGB888)
                                        else:
                                             image = loaded

                                    buffer.close()
                                    
                                else:
                                    logging.warning(f"Could not open file for reading: {actual_path}")
                                
                except Exception as e: 
                    logging.warning(f"Failed to load image {actual_path}: {e}")

                self.image_loaded.emit(path, image)
                
                with QMutexWithLocker(self.mutex):
                    if not image.isNull():
                        cache_key = f"{path}::{target_width}"
                        self.cache[cache_key] = image
                        self.cache.move_to_end(cache_key)
                        if len(self.cache) > self.CACHE_SIZE:
                            self.cache.popitem(last=False)

import base64

# ==========================================
# JSON Async Loader
# ==========================================
class JsonLoadWorker(QThread):
    json_loaded = Signal(str, dict) # raw_text, parsed_dict
    json_error = Signal(str)
    clipboard_data = Signal(str, str, int, int) # b64_encoded, minified_json, node_count, link_count

    def __init__(self, filepath, load_graph=True, for_clipboard=False):
        super().__init__()
        self.filepath = filepath
        self.load_graph = load_graph
        self.for_clipboard = for_clipboard
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8', errors='replace') as f:
                raw_text = f.read()

            if not self._is_running: return

            try:
                # orjson is natively extremely fast compared to built-in json
                json_data = json.loads(raw_text)
            except Exception as e:
                self.json_error.emit(f"JSON Parse Error: {e}")
                return

            if not self._is_running: return

            if not self.for_clipboard:
                # Flow: Load into TextEdit & Graph Viewer
                if not self.load_graph: json_data = {}
                self.json_loaded.emit(raw_text, json_data)
            else:
                # Flow: Clipboard format processing
                graph_data = json_data
                if "nodes" not in json_data and "workflow" in json_data:
                     graph_data = json_data["workflow"]

                nodes = graph_data.get("nodes", [])
                links = graph_data.get("links", [])
                groups = graph_data.get("groups", [])
                
                # [Fix] Scrub node internal link references for ComfyUI Paste logic
                # ComfyUI's LiteGraph uses the 'links' list for creating new links on paste.
                # If nodes retain their old 'link' IDs, LiteGraph fails to wire them up properly.
                for node in nodes:
                    if "inputs" in node and isinstance(node["inputs"], list):
                        for inp in node["inputs"]:
                            if isinstance(inp, dict) and "link" in inp: inp["link"] = None
                    if "outputs" in node and isinstance(node["outputs"], list):
                        for out in node["outputs"]:
                            if isinstance(out, dict) and "links" in out: out["links"] = []
                
                # Link conversion
                def convert_links(links_list):
                    formatted = []
                    for link in links_list:
                        if isinstance(link, list):
                            if len(link) >= 5:
                                formatted.append({
                                    "id": link[0], "origin_id": link[1], "origin_slot": link[2],
                                    "target_id": link[3], "target_slot": link[4], "type": link[5] if len(link) > 5 else "*"
                                })
                            else: formatted.append(link)
                        else: formatted.append(link)
                    return formatted

                formatted_links = convert_links(links)

                subgraphs_data = graph_data.get("subgraphs", [])
                if not subgraphs_data:
                    definitions = graph_data.get("definitions", {})
                    if isinstance(definitions, dict):
                        subgraphs_data = definitions.get("subgraphs", [])
                if not isinstance(subgraphs_data, list): subgraphs_data = []

                formatted_subgraphs = []
                for sg in subgraphs_data:
                    if isinstance(sg, dict):
                        new_sg = sg.copy()
                        if "links" in new_sg: new_sg["links"] = convert_links(new_sg["links"])
                        formatted_subgraphs.append(new_sg)
                    else:
                        formatted_subgraphs.append(sg)

                payload = {
                    "nodes": nodes,
                    "links": formatted_links,
                    "groups": groups,
                    "reroutes": graph_data.get("reroutes", []),
                    "subgraphs": formatted_subgraphs,
                }

                if not self._is_running: return

                minified_json = json.dumps(payload).decode('utf-8') if hasattr(json.dumps(payload), 'decode') else json.dumps(payload)
                encoded_bytes = base64.b64encode(minified_json.encode('utf-8'))
                encoded_str = encoded_bytes.decode('utf-8')

                self.clipboard_data.emit(encoded_str, minified_json, len(nodes), len(links))

        except Exception as e:
            self.json_error.emit(str(e))

# ==========================================
# Thumbnail Worker
# ==========================================
class ThumbnailWorker(QThread):
    finished = Signal(bool, str) # success, message

    def __init__(self, source_path, dest_path, is_video):
        super().__init__()
        self.source_path = source_path
        self.dest_path = dest_path
        self.is_video = is_video

    def run(self):
        try:
            shutil.copy2(self.source_path, self.dest_path)
            if self.is_video:
                self.finished.emit(True, "Video set.")
                return
            self.finished.emit(True, "Thumbnail updated.")
        except Exception as e:
            logging.error(f"[ThumbnailWorker] Error setting thumbnail: {e}")
            self.finished.emit(False, str(e))

# ==========================================
# Region: File System Workers
# ==========================================
class FileScannerWorker(QThread):
    batch_ready = Signal(str, list, list) 
    finished = Signal(dict)

    def __init__(self, base_path, extensions, recursive=True, max_depth=20, filter_mode=None):
        super().__init__()
        self.setObjectName("ScannerThread")
        self.base_path = base_path
        self.extensions = extensions
        self.recursive = recursive
        self.filter_mode = filter_mode
        self._is_running = True
        self.CHUNK_SIZE = 2000 # [Optimization] Increase batch size to reduce UI spam
        self.max_depth = max_depth # [Fix] Max depth to prevent deep tree freezing

    def stop(self):
        self._is_running = False

    def _is_comfyui_workflow(self, filepath):
        """Checks if a JSON file matches ComfyUI workflow structures without loading fully into memory."""
        try:
            if os.path.getsize(filepath) > 10 * 1024 * 1024:  # 10MB limit
                return False
                
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                head = f.read(256 * 1024)
                
                # 1. UI Workflow format
                if re.search(r'"nodes"\s*:\s*\[', head):
                    if '"last_node_id"' in head or '"last_link_id"' in head or '"version"' in head:
                        return True
                    
                # 2. API Workflow format
                if re.search(r'"class_type"\s*:\s*"[^"]+"', head) and re.search(r'"inputs"\s*:\s*\{', head):
                    return True
                    
            return False
        except Exception:
            return False

    def _has_workflow(self, dir_path):
        """Recursively checks if a directory contains any comfyui workflow file, up to a shallow depth."""
        try:
            max_depth = 5 
            base_depth = dir_path.count(os.sep)
            
            for root, dir_names, files in os.walk(dir_path, followlinks=True):
                current_depth = root.count(os.sep)
                if current_depth > base_depth + max_depth:
                    del dir_names[:]
                    continue
                    
                dir_names[:] = [
                    dn for dn in dir_names 
                    if not dn.startswith('.') and dn not in ('venv', 'node_modules', '__pycache__', '.git', 'models', 'custom_nodes')
                ]
                
                for f in files:
                    if f.lower().endswith('.json'):
                        json_path = os.path.join(root, f)
                        if self._is_comfyui_workflow(json_path):
                            return True
            return False
        except OSError:
            return False

    def run(self):
        if not os.path.exists(self.base_path):
            self.finished.emit({})
            return

        logging.debug(f"[FileScanner] Starting scan for: {self.base_path}")
        stack = [(self.base_path, 0)] # [Depth Limit] Tuple of (path, depth)
        visited = set()
        visited.add(os.path.realpath(self.base_path))
        
        while stack:
            if not self._is_running: return
            
            current_dir, current_depth = stack.pop()
            
            if current_depth > self.max_depth:
                logging.warning(f"[FileScanner] Max depth ({self.max_depth}) exceeded at {current_dir}. Skipping.")
                continue

            try:
                with os.scandir(current_dir) as it:
                    dirs_buffer = []
                    files_buffer = []
                    
                    for entry in it:
                        if not self._is_running: return
                        
                        if entry.is_dir():
                            # Remove symlink skipping to support symlinked models/workflows directory structures
                            real_path = os.path.realpath(entry.path)
                            if real_path in visited: continue
                            visited.add(real_path)
                            
                            # [Feature] Async filtering in background thread for empty folders
                            if self.filter_mode == "workflow_template":
                                if not self._has_workflow(entry.path):
                                    continue

                            dirs_buffer.append(entry.name)
                            if self.recursive:
                                stack.append((entry.path, current_depth + 1))
                        
                        elif entry.is_file():
                             if os.path.splitext(entry.name)[1].lower() in self.extensions:
                                 # [Feature] Async filtering in background thread
                                 if self.filter_mode == "workflow_template" and entry.name.lower().endswith('.json'):
                                     if not self._is_comfyui_workflow(entry.path):
                                         continue
                                 try:
                                     st = entry.stat()
                                     sz = format_size(st.st_size)
                                     dt = time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))
                                     files_buffer.append({
                                         "name": entry.name, 
                                         "path": entry.path, 
                                         "size": sz, 
                                         "date": dt,
                                         "raw_size": st.st_size,
                                         "raw_date": st.st_mtime
                                     })
                                     
                                     if len(files_buffer) >= self.CHUNK_SIZE:
                                         self.batch_ready.emit(current_dir, [], files_buffer)
                                         files_buffer = []

                                 except OSError as e:
                                     logging.debug(f"[FileScanner] OSError accessing {entry.path}: {e}")
                    
                    if dirs_buffer or files_buffer:
                         self.batch_ready.emit(current_dir, dirs_buffer, files_buffer)
            
            except OSError as e:
                logging.debug(f"[FileScanner] OSError accessing directory {current_dir}: {e}")
                continue
                
        if self._is_running:
            self.finished.emit({}) 

# ==========================================
# Search Worker
# ==========================================
class FileSearchWorker(QThread):
    finished = Signal(list) 
    
    def __init__(self, roots, query, extensions, max_depth=20):
        super().__init__()
        self.setObjectName("SearchThread")
        self.roots = roots if isinstance(roots, list) else [roots]
        self.query = query.lower()
        self.extensions = extensions
        self._is_running = True
        self.max_depth = max_depth # [Fix] Maintain depth limit

    def stop(self):
        self._is_running = False

    def run(self):
        results = []
        # [Refactor] Iterative Scan for safety
        stack = []
        visited = set() # [Fix] Added missing visited set
        
        for r in self.roots:
            if os.path.exists(r):
                stack.append((r, 0))
                visited.add(os.path.realpath(r))
        
        while stack:
            if not self._is_running: break
            current_path, current_depth = stack.pop()
            
            if current_depth > self.max_depth:
                logging.warning(f"[FileSearch] Max depth ({self.max_depth}) exceeded at {current_path}. Skipping.")
                continue
            
            try:
                with os.scandir(current_path) as it:
                    for entry in it:
                        if not self._is_running: break
                        
                        if entry.is_dir():
                            # Allow directory symlinks
                            real_path = os.path.realpath(entry.path)
                            if real_path in visited: continue
                            visited.add(real_path)
                            
                            stack.append((entry.path, current_depth + 1))
                        elif entry.is_file():
                             name_lower = entry.name.lower()
                             ext = os.path.splitext(name_lower)[1]
                             if ext in self.extensions:
                                 if self.query in name_lower:
                                     try:
                                         st = entry.stat()
                                         results.append((entry.path, "file", st.st_size, st.st_mtime))
                                     except OSError as e:
                                         logging.debug(f"[FileSearch] OSError stat {entry.path}: {e}")
                                         results.append((entry.path, "file", 0, 0))
            except OSError as e:
                logging.debug(f"[FileSearch] OSError accessing directory {current_path}: {e}")
        
        if self._is_running:
            self.finished.emit(results)

# ==========================================
# Async Metadata Search Worker (Progressive)
# ==========================================
def _mp_search_chunk(chunk_paths, query, search_field, has_prompt_only):
    from .metadata import validate_metadata_type, standardize_metadata
    from PIL import Image
    import os

    results = []
    for filepath in chunk_paths:
        try:
            with Image.open(filepath) as img:
                pass_chk = True
                if has_prompt_only and not validate_metadata_type(img):
                    pass_chk = False
                    
                pass_txt = True
                if query:
                    target_text = ""
                    if search_field == "all":
                        target_text = str(img.info).lower()
                    elif search_field == "workflow":
                        target_text = str(img.info.get("workflow", img.info.get("prompt", ""))).lower()
                    else:
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
                    results.append(filepath)
        except Exception:
            pass
            
    return results

class AsyncMetadataSearchWorker(QThread):
    batch_ready = Signal(list)  # Emits list of (file_path, size_bytes, mtime_tuple)
    progress_update = Signal(int, int) # (scanned_count, total_count)
    finished = Signal()
    
    def __init__(self, target_path, query, search_field, has_prompt_only, extensions, chunk_size=20):
        super().__init__()
        self.setObjectName("AsyncMetaSearchThread")
        self.target_path = target_path
        self.query = query.lower().strip()
        self.search_field = search_field.lower()
        self.has_prompt_only = has_prompt_only
        self.extensions = extensions
        self.chunk_size = chunk_size
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        import concurrent.futures
        
        # 1. Fast pre-scan to collect all matching candidate files
        target_files = []
        count_stack = [(self.target_path, 0)]
        count_visited = set()
        if os.path.exists(self.target_path):
            count_visited.add(os.path.realpath(self.target_path))
            
        while count_stack:
            if not self._is_running: return
            current_dir, depth = count_stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if not self._is_running: return
                        if entry.is_dir():
                            real_path = os.path.realpath(entry.path)
                            if real_path not in count_visited:
                                count_visited.add(real_path)
                                count_stack.append((entry.path, depth + 1))
                        elif entry.is_file():
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext in self.extensions and ext not in VIDEO_EXTENSIONS:
                                target_files.append(entry.path)
            except OSError:
                pass
                
        if not self._is_running: return
        
        total_files = len(target_files)
        if total_files == 0:
            if self._is_running: self.finished.emit()
            return
            
        # 2. Chunking
        # Dynamically calculate chunk size to balance between overhead and process count.
        cores = os.cpu_count() or 2
        chunk_size = max(50, total_files // (cores * 2))
        chunks = [target_files[i:i + chunk_size] for i in range(0, total_files, chunk_size)]
        
        # 3. Process Pool with Safety Core Limits (50% ~ 75%)
        # Base CPU count parsing
        max_workers = max(1, int(cores * 0.75))
        scanned_count = 0
        
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_mp_search_chunk, chunk, self.query, self.search_field, self.has_prompt_only): len(chunk)
                for chunk in chunks
            }
            
            for future in concurrent.futures.as_completed(futures):
                if not self._is_running:
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except Exception as e:
                        logging.debug(f"[AsyncMetadataSearchWorker] Force shutdown error: {e}")
                    return
                
                chunk_len = futures[future]
                scanned_count += chunk_len
                self.progress_update.emit(scanned_count, total_files)
                
                try:
                    results = future.result()
                    if results:
                        buffer = []
                        for filepath in results:
                            try:
                                st = os.stat(filepath)
                                buffer.append({
                                    "name": os.path.basename(filepath),
                                    "path": filepath,
                                    "size": format_size(st.st_size),
                                    "date": time.strftime('%Y-%m-%d', time.localtime(st.st_mtime)),
                                    "raw_size": st.st_size,
                                    "raw_date": st.st_mtime
                                })
                                
                                if len(buffer) >= self.chunk_size:
                                    if self._is_running: self.batch_ready.emit(buffer)
                                    buffer = []
                            except OSError:
                                pass
                        
                        if buffer and self._is_running:
                            self.batch_ready.emit(buffer)
                except Exception as e:
                    logging.debug(f"[AsyncMetadataSearchWorker] Process chunk error: {e}")
                    
        if self._is_running:
            self.finished.emit()

# ==========================================
# Region: Network & Metadata Workers
# ==========================================

class MetadataWorker(QThread):
    batch_started = Signal(list) 
    task_progress = Signal(str, str, int) 
    status_update = Signal(str) 
    model_processed = Signal(bool, str, dict, str) 
    ask_overwrite = Signal(str)

    def __init__(self, mode="auto", targets=None, manual_url=None, civitai_key="", hf_key="", cache_root=None, directories=None, overwrite_behavior='ask', cache_mode="model"):
        super().__init__()
        self.mode = mode 
        self.cache_mode = cache_mode
        self.targets = targets if targets else []
        self.manual_url = manual_url
        self.overwrite_behavior = overwrite_behavior
        self.directories = directories.copy() if directories else {} 
        self._is_running = True 
        
        self._overwrite_decision = None
        self._wait_mutex = QMutex()
        self._wait_condition = QWaitCondition()
        
        # [Refactor] Using Services
        self.api_service = ApiService(civitai_key, hf_key)
        self.file_service = FileService(cache_root if cache_root else CACHE_DIR_NAME)

    def stop(self):
        self._is_running = False
        self._resume() 

    def set_overwrite_response(self, response):
        self._overwrite_decision = response
        self._resume()

    def _resume(self):
        self._wait_mutex.lock()
        self._wait_condition.wakeAll()
        self._wait_mutex.unlock()

    def _wait_for_user(self):
        self._wait_mutex.lock()
        self._wait_condition.wait(self._wait_mutex)
        self._wait_mutex.unlock()

    def run(self):
        total_files = len(self.targets)
        success_count = 0
        global_overwrite = None
        if self.overwrite_behavior in ['yes_all', 'no_all']:
            global_overwrite = self.overwrite_behavior

        self.batch_started.emit(self.targets)

        for idx, model_path in enumerate(self.targets):
            if not self._is_running: break

            try:
                if not model_path or not os.path.exists(model_path): continue

                filename = os.path.basename(model_path)
                
                # [Refactor] Use FileService
                if self.file_service.check_metadata_exists(model_path, self.directories, self.cache_mode):
                    should_skip = False
                    if global_overwrite == 'no_all': should_skip = True
                    elif global_overwrite == 'yes_all': should_skip = False
                    else:
                        self._overwrite_decision = None
                        self.ask_overwrite.emit(filename)
                        self._wait_for_user() 
                        if not self._is_running:
                            break
                        resp = self._overwrite_decision
                        if resp in ('no', None): should_skip = True
                        elif resp == 'no_all':
                            global_overwrite = 'no_all'; should_skip = True
                        elif resp == 'yes_all':
                            global_overwrite = 'yes_all'; should_skip = False
                        elif resp == 'cancel': 
                            self.stop(); break
                        
                    if should_skip:
                        self.task_progress.emit(model_path, "Skipped (Exists)", 100)
                        self.status_update.emit(f"Skipped: {filename}")
                        continue

                self.task_progress.emit(model_path, "Starting...", 0)
                self.status_update.emit(f"Processing ({idx+1}/{total_files}): {filename}")

                if self.mode == "manual" and self.manual_url and "huggingface.co" in self.manual_url:
                    self._process_huggingface(model_path, self.manual_url)
                    success_count += 1
                    continue

                # Civitai Processing
                model_id = None
                version_id = None

                if self.mode == "auto":
                    self.task_progress.emit(model_path, "Checking Hash...", 10)
                    file_hash, is_cached = self.file_service.get_cached_hash(
                        model_path, self.directories, self.cache_mode, self.status_update
                    )
                    
                    if not self._is_running: break
                    if not file_hash: raise Exception("Failed to calculate hash.")

                    if is_cached: self.task_progress.emit(model_path, "Hash Cached", 30)
                    else: self.task_progress.emit(model_path, "Hashing Done", 30)

                    self.task_progress.emit(model_path, "Searching Civitai...", 40)
                    version_data = self.api_service.fetch_civitai_version(file_hash)
                    model_id = version_data.get("modelId")
                    version_id = version_data.get("id")
                else:
                    if not self.manual_url: raise Exception("No URL.")
                    match_m = re.search(r'models/(\d+)', self.manual_url)
                    match_v = re.search(r'modelVersionId=(\d+)', self.manual_url)
                    if match_m: model_id = match_m.group(1)
                    if match_v: version_id = match_v.group(1)
                
                if not model_id: 
                    self.task_progress.emit(model_path, "Not Found", 0)
                    continue
                
                self.task_progress.emit(model_path, "Fetching Details...", 50)
                model_data = self.api_service.fetch_civitai_model(model_id)
                if not self._is_running: break

                all_versions = model_data.get("modelVersions", [])
                target_version = None
                if version_id:
                     for v in all_versions:
                         if str(v.get("id")) == str(version_id):
                             target_version = v; break
                if not target_version and all_versions: target_version = all_versions[0]

                name = model_data.get("name", "Unknown")
                creator = model_data.get("creator", {}).get("username", "Unknown")
                model_url = f"https://civitai.com/models/{model_id}"
                trained_words = target_version.get("trainedWords", []) if target_version else []
                trigger_str = ", ".join(trained_words) if trained_words else "None"
                base_model = target_version.get("baseModel", "Unknown") if target_version else "Unknown"
                
                model_desc_html = model_data.get("description", "") or ""
                ver_desc_html = target_version.get("description", "") or "" if target_version else ""

                if HAS_MARKDOWNIFY:
                    model_desc_md = markdownify.markdownify(model_desc_html, heading_style="ATX")
                    ver_desc_md = markdownify.markdownify(ver_desc_html, heading_style="ATX")
                else:
                    model_desc_md = model_desc_html
                    ver_desc_md = ver_desc_html

                note_content = [f"# {name}", f"**Link:**\n[{model_url}]({model_url})", f"**Creator:**\n{creator}", f"**Base Model:**\n{base_model}", f"**Trigger Words:**\n`{trigger_str}`", "\n---"]
                if ver_desc_md:
                    note_content.append("## Version Info")
                    note_content.append(ver_desc_md)
                    note_content.append("\n---")
                note_content.append("## Model Description")
                note_content.append(model_desc_md)

                full_desc = "\n\n".join(note_content)
                self.task_progress.emit(model_path, "Downloading...", 70)
                full_desc = self._process_embedded_images(full_desc, model_path)

                preview_urls = []
                if target_version:
                    preview_urls = [img.get("url") for img in target_version.get("images", []) if img.get("url")]

                if preview_urls:
                    self._download_preview_images(preview_urls, model_path)
                    self.file_service.try_set_thumbnail_from_cache(model_path, self.directories, self.cache_mode)
                    self.status_update.emit(f"Auto-set thumbnail checked for {filename}")

                self.task_progress.emit(model_path, "Done", 100)
                self.model_processed.emit(True, "Processed", {"description": full_desc}, model_path)
                success_count += 1
                
            except Exception as e:
                logging.error(f"Error processing {model_path}: {e}")
                self.task_progress.emit(model_path, "Error", 0)
                self.model_processed.emit(False, str(e), {}, model_path)
            
            time.sleep(0.5)
            
        if self._is_running:
            self.status_update.emit(f"Batch Done. ({success_count}/{total_files} succeeded)")
        else:
            self.status_update.emit("Batch Cancelled.")


    def _process_huggingface(self, model_path, url):
        self.task_progress.emit(model_path, "Fetching HF Info...", 20)
        match = re.search(r'huggingface\.co/([^/]+)/([^/?#]+)', url)
        if not match: raise Exception("Invalid Hugging Face URL format.")
        repo_id = f"{match.group(1)}/{match.group(2)}"
        
        model_data = self.api_service.fetch_hf_model(repo_id)
        author = model_data.get("author", "Unknown")
        tags = model_data.get("tags", [])
        last_modified = model_data.get("lastModified", "Unknown")
        readme_content = self.api_service.fetch_hf_readme(repo_id)
        
        self.task_progress.emit(model_path, "Downloading...", 50)
        
        note_content = [f"# {repo_id}", f"**Link:**\n[{url}]({url})", f"**Author:** {author}", f"**Last Modified:** {last_modified}", f"**Tags:** `{', '.join(tags)}`", "\n---", "## Model Card (README.md)", readme_content]
        full_desc = "\n\n".join(note_content)
        
        siblings = model_data.get("siblings", [])
        image_urls = []
        for sibling in siblings:
            fname = sibling.get("rfilename", "")
            if fname.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                img_url = f"https://huggingface.co/{repo_id}/resolve/main/{fname}"
                image_urls.append(img_url)
        if image_urls:
            self._download_preview_images(image_urls, model_path)
            self.file_service.try_set_thumbnail_from_cache(model_path, self.directories, self.cache_mode)
        
        self.task_progress.emit(model_path, "Done", 100)
        self.model_processed.emit(True, "Hugging Face Data Processed", {"description": full_desc}, model_path)


    def _process_embedded_images(self, text, model_path):
        paths = self.file_service.get_cache_paths(model_path, self.directories, self.cache_mode)
        embed_dir = paths["embedded"]
        if not os.path.exists(embed_dir): os.makedirs(embed_dir)

        def replace_md(match):
            alt = match.group(1); url = match.group(2)
            local_path = self.api_service.download_file(url, embed_dir)
            # [Fix] Use relative path (embedded/filename) for Markdown
            if local_path: 
                rel_path = f"embedded/{os.path.basename(local_path)}"
                return f"![{alt}]({rel_path})"
            return match.group(0)

        def replace_html(match):
            pre = match.group(1); url = match.group(2); post = match.group(3)
            local_path = self.api_service.download_file(url, embed_dir)
            # [Fix] Use relative path (embedded/filename) for HTML/Markdown
            if local_path: 
                rel_path = f"embedded/{os.path.basename(local_path)}"
                return f'{pre}{rel_path}{post}'
            return match.group(0)
            
        try:
             text = re.sub(r'!\[(.*?)\]\((.*?)\)', replace_md, text)
             text = re.sub(r'(<img[^>]+src=["\'])(.*?)(["\'][^>]*>)', replace_html, text)
        except Exception as e:
             logging.warning(f"Error processing embedded images: {e}")
        return text

    def _download_preview_images(self, urls, model_path):
        paths = self.file_service.get_cache_paths(model_path, self.directories, self.cache_mode)
        preview_dir = paths["preview"]
        if not os.path.exists(preview_dir): os.makedirs(preview_dir)
        
        def _download_single(url):
            if not self._is_running: return
            try:
                fpath = self.api_service.download_file(url, preview_dir)
                if fpath and HAS_PILLOW:
                    base, ext = os.path.splitext(fpath)
                    if ext.lower() not in VIDEO_EXTENSIONS and ext.lower() != ".png":
                        from PIL import Image
                        from PIL.PngImagePlugin import PngInfo
                        try:
                            img = Image.open(fpath)
                            img.load()
                            metadata = PngInfo()
                            for k, v in img.info.items():
                                if k in ["exif", "icc_profile"]: continue
                                if isinstance(v, str): metadata.add_text(k, v)
                            save_kwargs = {"pnginfo": metadata}
                            if "exif" in img.info: save_kwargs["exif"] = img.info["exif"]
                            if "icc_profile" in img.info: save_kwargs["icc_profile"] = img.info["icc_profile"]
                            new_path = base + ".png"
                            img.save(new_path, "PNG", **save_kwargs)
                            img.close()
                            if os.path.exists(new_path): os.remove(fpath)
                        except Exception as e:
                            logging.warning(f"[AutoConvert] Failed to convert {os.path.basename(fpath)}: {e}")
            except Exception as e: logging.error(f"Preview download error: {e}")
            
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        try:
            futures = [executor.submit(_download_single, url) for url in urls]
            while futures and self._is_running:
                done, not_done = concurrent.futures.wait(futures, timeout=0.2)
                if not self._is_running:
                    for f in not_done: f.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                futures = list(not_done)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

# ==========================================
# Cache Match Worker
# ==========================================
class CacheMatchWorker(QThread):
    progress = Signal(str, str, int)  # path, status, percent
    completed = Signal(dict)

    def __init__(self, targets, cache_root, directories, mode="model"):
        super().__init__()
        self.setObjectName("CacheMatchWorker")
        self.targets = targets or []
        self.cache_root = cache_root or CACHE_DIR_NAME
        self.directories = directories.copy() if directories else {}
        self.mode = mode
        self._is_running = True

    def stop(self):
        self._is_running = False

    def _safe_inside(self, parent_path, child_path):
        try:
            parent = os.path.normcase(os.path.abspath(os.path.normpath(parent_path)))
            child = os.path.normcase(os.path.abspath(os.path.normpath(child_path)))
            return os.path.commonpath([parent, child]) == parent
        except (OSError, ValueError):
            return False

    def _calculate_sha256(self, path):
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                if not self._is_running:
                    return ""
                sha256.update(chunk)
        return sha256.hexdigest().upper()

    def _cache_score(self, cache_dir):
        if not os.path.isdir(cache_dir):
            return 0

        score = 0
        try:
            root_files = os.listdir(cache_dir)
        except OSError:
            return 0

        if any(name.lower().endswith(".md") for name in root_files):
            score += 5

        preview_dir = os.path.join(cache_dir, "preview")
        if os.path.isdir(preview_dir):
            try:
                if any(os.path.isfile(os.path.join(preview_dir, name)) for name in os.listdir(preview_dir)):
                    score += 4
            except OSError:
                pass

        embedded_dir = os.path.join(cache_dir, "embedded")
        if os.path.isdir(embedded_dir):
            score += 1

        for name in root_files:
            if not name.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as f:
                    data = json.load(f)
                meaningful_keys = set(data.keys()) - {"sha256", "mtime_check"}
                if meaningful_keys:
                    score += 2
                    break
            except Exception:
                continue

        return score

    def _read_json_hash(self, json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            value = data.get("sha256")
            if isinstance(value, str) and value:
                return value.upper(), data
        except Exception:
            pass
        return "", {}

    def _find_cache_by_hash(self, file_hash, target_cache_dir):
        search_root = os.path.join(self.cache_root, self.mode)
        if not os.path.isdir(search_root):
            search_root = self.cache_root

        best = None
        best_score = 0
        best_mtime = 0
        best_json_data = {}
        best_json_name = ""

        for root, dirs, files in os.walk(search_root):
            if not self._is_running:
                return None

            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]

            if os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(target_cache_dir)):
                continue

            for name in files:
                if not name.lower().endswith(".json"):
                    continue

                json_path = os.path.join(root, name)
                found_hash, data = self._read_json_hash(json_path)
                if found_hash != file_hash:
                    continue

                score = self._cache_score(root)
                if score <= 0:
                    continue

                try:
                    mtime = os.path.getmtime(root)
                except OSError:
                    mtime = 0

                if score > best_score or (score == best_score and mtime > best_mtime):
                    best = root
                    best_score = score
                    best_mtime = mtime
                    best_json_data = data
                    best_json_name = name

        if not best:
            return None

        return {
            "cache_dir": best,
            "json_data": best_json_data,
            "json_name": best_json_name,
            "score": best_score,
        }

    def _normalize_cache_files(self, cache_dir, model_path, json_data):
        model_name = os.path.splitext(os.path.basename(model_path))[0]
        model_mtime = os.path.getmtime(model_path)

        json_data = dict(json_data or {})
        json_data["sha256"] = json_data.get("sha256", "").upper()
        json_data["mtime_check"] = model_mtime

        json_path = os.path.join(cache_dir, model_name + ".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=4, ensure_ascii=False)

        md_target = os.path.join(cache_dir, model_name + ".md")
        if not os.path.exists(md_target):
            md_candidates = [
                os.path.join(cache_dir, name)
                for name in os.listdir(cache_dir)
                if name.lower().endswith(".md") and os.path.isfile(os.path.join(cache_dir, name))
            ]
            if md_candidates:
                shutil.move(md_candidates[0], md_target)

        for name in list(os.listdir(cache_dir)):
            path = os.path.join(cache_dir, name)
            if not os.path.isfile(path):
                continue
            if name.lower().endswith(".json") and name != os.path.basename(json_path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _relocate_cache(self, source_dir, target_dir, model_path, json_data):
        if not self._safe_inside(self.cache_root, source_dir):
            raise RuntimeError("Matched cache is outside the configured cache root.")
        if not self._safe_inside(self.cache_root, target_dir):
            raise RuntimeError("Target cache is outside the configured cache root.")

        if os.path.exists(target_dir):
            if self._cache_score(target_dir) > 0:
                raise RuntimeError("Target cache already has metadata.")
            shutil.rmtree(target_dir)

        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        shutil.copytree(source_dir, target_dir)
        self._normalize_cache_files(target_dir, model_path, json_data)
        shutil.rmtree(source_dir)

    def run(self):
        summary = {"matched": 0, "skipped": 0, "not_found": 0, "errors": []}
        total = len(self.targets)

        for idx, model_path in enumerate(self.targets):
            if not self._is_running:
                break

            try:
                if not model_path or not os.path.isfile(model_path):
                    summary["skipped"] += 1
                    continue

                target_cache_dir = calculate_structure_path(model_path, self.cache_root, self.directories, mode=self.mode)
                if self._cache_score(target_cache_dir) > 0:
                    self.progress.emit(model_path, "Skipped (Cache exists)", 100)
                    summary["skipped"] += 1
                    continue

                self.progress.emit(model_path, "Hashing", 10)
                file_hash = self._calculate_sha256(model_path)
                if not self._is_running:
                    break
                if not file_hash:
                    summary["errors"].append(f"{os.path.basename(model_path)}: failed to calculate hash")
                    self.progress.emit(model_path, "Error", 0)
                    continue

                self.progress.emit(model_path, "Searching cache", 50)
                match = self._find_cache_by_hash(file_hash, target_cache_dir)
                if not self._is_running:
                    break
                if not match:
                    self.progress.emit(model_path, "No match", 100)
                    summary["not_found"] += 1
                    continue

                json_data = match["json_data"]
                json_data["sha256"] = file_hash
                self.progress.emit(model_path, "Moving cache", 80)
                self._relocate_cache(match["cache_dir"], target_cache_dir, model_path, json_data)
                self.progress.emit(model_path, "Matched", 100)
                summary["matched"] += 1

            except Exception as e:
                logging.error(f"[CacheMatchWorker] Failed for {model_path}: {e}")
                summary["errors"].append(f"{os.path.basename(model_path)}: {e}")
                self.progress.emit(model_path, "Error", 0)

            if total:
                logging.debug(f"[CacheMatchWorker] Progress {idx + 1}/{total}")

        self.completed.emit(summary)

# ==========================================
# Model Download Worker (Restored)
# ==========================================
class ModelDownloadWorker(QThread):
    progress = Signal(str, str, int)
    finished = Signal(str, str)
    error = Signal(str)
    name_found = Signal(str, str)
    ask_collision = Signal(str)

    def __init__(self, url, target_dir, api_key="", task_key=""):
        super().__init__()
        self.url = url
        self.target_dir = target_dir
        self.api_key = api_key
        self.task_key = task_key if task_key else url
        self._is_running = True
        self._decision = None
        self._wait_mutex = QMutex()
        self._wait_condition = QWaitCondition()
        self.net_client = NetworkClient(civitai_key=api_key)

    def stop(self):
        self._is_running = False
        self._resume()

    def set_collision_decision(self, decision):
        self._decision = decision
        self._resume()

    def _resume(self):
        self._wait_mutex.lock()
        self._wait_condition.wakeAll()
        self._wait_mutex.unlock()

    def run(self):
        try:
            self.progress.emit(self.task_key, "Resolving...", 0)
            
            # 1. Resolve Info (Name, etc.)
            model_id = None
            version_id = None
            match_m = re.search(r'models/(\d+)', self.url)
            match_v = re.search(r'modelVersionId=(\d+)', self.url)
            if match_m: model_id = match_m.group(1)
            if match_v: version_id = match_v.group(1)

            download_url = self.url
            if model_id and "civitai.com" in self.url:
                if version_id:
                     download_url = f"https://civitai.com/api/download/models/{version_id}"
                     api_url = f"https://civitai.com/api/v1/model-versions/{version_id}"
                else:
                     api_url = f"https://civitai.com/api/v1/models/{model_id}"
                     try:
                         data = self.net_client.get(api_url).json()
                         if "modelVersions" in data and data["modelVersions"]:
                             latest_ver = data["modelVersions"][0]
                             vid = latest_ver["id"]
                             download_url = f"https://civitai.com/api/download/models/{vid}"
                             version_id = vid
                     except Exception as e:
                         logging.debug(f"[ModelDownloadWorker] Failed to resolve version URL from API: {e}")
            
            if model_id:
                try:
                    target_api = f"https://civitai.com/api/v1/model-versions/{version_id}" if version_id else f"https://civitai.com/api/v1/models/{model_id}"
                    data = self.net_client.get(target_api).json()
                    name = data.get("name", "Unknown")
                    if "model" in data: name = f"{data['model'].get('name')} - {name}"
                    
                    self.name_found.emit(self.task_key, f"{name} / {os.path.basename(self.target_dir)}")
                except Exception as e:
                    logging.debug(f"[ModelDownloadWorker] Failed to resolve model name from API: {e}")
                
            # 2. Collision Check (Pre-download)
            try:
                head = self.net_client.get(download_url, stream=True)
                from email.message import EmailMessage
                fname = None
                if "Content-Disposition" in head.headers:
                     msg = EmailMessage()
                     msg['content-disposition'] = head.headers["Content-Disposition"]
                     fname = msg['content-disposition'].params.get('filename')
                
                if not fname:
                     fname = os.path.basename(head.url.split('?')[0])
                
                head.close()
                
                if fname:
                    target_path = os.path.join(self.target_dir, fname)
                    if os.path.exists(target_path):
                         self.ask_collision.emit(fname)
                         self._wait_mutex.lock()
                         self._wait_condition.wait(self._wait_mutex)
                         self._wait_mutex.unlock()
                         
                         if self._decision == 'cancel':
                             self.finished.emit("Cancelled", "")
                             return
                         elif self._decision == 'rename':
                             name, ext = os.path.splitext(fname)
                             fname = f"{name}_{int(time.time())}{ext}"

            except Exception as e:
                logging.warning(f"Collision check failed: {e}")
                fname = None

            # 3. Download
            self.progress.emit(self.task_key, "Downloading...", 0)
            
            def progress_cb(dl, total):
                if total > 0:
                    pct = int((dl / total) * 100)
                    self.progress.emit(self.task_key, "Downloading", pct)
            
            final_path = self.net_client.download_file(
                download_url, self.target_dir, filename=fname, progress_callback=progress_cb,
                stop_callback=lambda: not self._is_running
            )
            
            if final_path:
                self.finished.emit("Download Complete", final_path)
            else:
                self.error.emit("Download failed (No path returned)")

        except InterruptedError:
            self.finished.emit("Cancelled", "")
            return
        except Exception as e:
            self.error.emit(str(e))

# ==========================================
# Local Metadata Worker (Restored)
# ==========================================
class LocalMetadataWorker(QThread):
    finished = Signal(str, dict) # path, metadata
    
    def __init__(self):
        super().__init__()
        self.setObjectName("LocalMetadataWorker")
        self.mutex = QMutex()
        self.condition = QWaitCondition()
        self._is_running = True
        self.queue = deque()
        self.CACHE_SIZE = 50
        self.cache = OrderedDict()  # {(path, mtime): metadata_dict}

    def __del__(self):
        try:
            if hasattr(self, 'wait'):
                self.wait()
        except RuntimeError as e:
            logging.debug(f"[LocalMetadataWorker] RuntimeError during cleanup: {e}")
        
    def extract(self, path):
        with QMutexWithLocker(self.mutex):
             self.queue.clear() 
             self.queue.append(path)
             self.condition.wakeOne()
             
    def stop(self):
        self._is_running = False
        with QMutexWithLocker(self.mutex):
            self.condition.wakeAll()
    
    def cancel_path(self, path):
        with QMutexWithLocker(self.mutex):
            self.queue = deque([p for p in self.queue if os.path.normpath(p) != os.path.normpath(path)])
    
    def clear_queue(self):
        with QMutexWithLocker(self.mutex):
            self.queue.clear()
    
    def invalidate_cache(self, path):
        with QMutexWithLocker(self.mutex):
            keys_to_remove = [k for k in self.cache.keys() if k[0] == path]
            for k in keys_to_remove:
                del self.cache[k]
            
    def run(self):
        from PIL import Image
        from io import BytesIO
        
        while self._is_running:
            self.mutex.lock()
            if not self.queue:
                self.condition.wait(self.mutex, 500)
                
            if not self._is_running:
                self.mutex.unlock()
                break
            
            try:
                path = self.queue.popleft()
            except IndexError:
                path = None
            self.mutex.unlock()
            
            if path and os.path.exists(path):
                try:
                    mtime = None
                    cache_key = None
                    try:
                        mtime = os.path.getmtime(path)
                        cache_key = (path, mtime)
                        
                        cached_meta = None
                        with QMutexWithLocker(self.mutex):
                            if cache_key in self.cache:
                                self.cache.move_to_end(cache_key)
                                cached_meta = self.cache[cache_key]
                        
                        if cached_meta is not None:
                            if self._is_running:
                                self.finished.emit(path, cached_meta)
                            continue
                    except OSError as e:
                        logging.debug(f"[LocalMetadataWorker] Cannot get mtime or cache key for {path}: {e}")
                    
                    # [Fix] Check if video before attempting Image.open
                    ext = os.path.splitext(path)[1].lower()
                    if ext in VIDEO_EXTENSIONS:
                        # Return empty/default metadata for videos
                        meta = {
                            "type": "video",
                            "main": {},
                            "model": {"checkpoint": "", "loras": [], "resources": []},
                            "prompts": {"positive": "", "negative": ""},
                            "etc": {},
                            "raw_text": "Video File (Metadata extraction not supported)"
                        }
                    else:
                        with Image.open(path) as img:
                            img.load() 
                            meta = standardize_metadata(img)
                    
                    if cache_key:
                        try:
                            with QMutexWithLocker(self.mutex):
                                self.cache[cache_key] = meta
                                self.cache.move_to_end(cache_key)
                                if len(self.cache) > self.CACHE_SIZE:
                                    self.cache.popitem(last=False)
                        except Exception as e:
                            logging.debug(f"[LocalMetadataWorker] Cache update failed for {path}: {e}")
                        
                    if self._is_running:
                        self.finished.emit(path, meta)
                        
                except Exception as e:
                    logging.error(f"Metadata extraction failed for {path}: {e}")
