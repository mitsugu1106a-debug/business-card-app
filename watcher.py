import os
import time
import shutil
import uuid
import json
import threading
import io
import traceback
from dotenv import load_dotenv

# Note: Heavy imports like PIL, fitz, watchdog are moved inside functions
# to ensure the main application starts even if these libraries fail.

load_dotenv()

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_IMPORT_DIR = os.path.join(BASE_DIR, "auto_import")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(AUTO_IMPORT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Models list
MODELS_TO_TRY = [
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]

def perform_ocr(image_path: str):
    """Lazy-loading imports to prevent startup crashes."""
    import google.generativeai as genai
    import PIL.Image
    
    # Try enabling HEIC support
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        genai.configure(api_key=api_key)

    prompt = "名刺情報をJSON形式で抽出してください。" # Simplified for speed
    
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        
        if image_path.lower().endswith(".pdf"):
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            if len(doc) == 0: return None
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
            img = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
            doc.close()
        else:
            img = PIL.Image.open(io.BytesIO(data))
            if img.mode != "RGB": img = img.convert("RGB")
    except Exception as e:
        print(f"Image Load Error: {e}")
        return None

    config = {"temperature": 0.1, "response_mime_type": "application/json"}
    for mname in MODELS_TO_TRY:
        try:
            model = genai.GenerativeModel(model_name=mname, generation_config=config)
            resp = model.generate_content([prompt, img])
            text = resp.text.strip()
            if "```json" in text: text = text.split("```json")[1].split("```")[0]
            elif "```" in text: text = text.split("```")[1].split("```")[0]
            
            parsed = json.loads(text)
            return [parsed] if isinstance(parsed, dict) else parsed
        except Exception as e:
            print(f"Model {mname} failed: {e}")
            continue
    return None

def start_watching():
    """Watchdog is heavy, only import if needed."""
    if os.getenv("RENDER"):
        return

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("Watchdog not installed. Skipping folder monitoring.")
        return

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                # Actual processing logic would go here, simplified for robustness
                print(f"New file detected: {event.src_path}")

    observer = Observer()
    observer.schedule(Handler(), AUTO_IMPORT_DIR, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except:
        observer.stop()
    observer.join()
