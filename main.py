import base64
import secrets
from fastapi import FastAPI, Depends, HTTPException, Form, File, UploadFile, Body, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List, Optional
import os
import uuid
import json
import traceback
import threading
from contextlib import asynccontextmanager

# Delay heavy imports until needed or inside lifespan
# import models, database -> Moved inside or used safely
import database
import models
import watcher

from dotenv import load_dotenv
load_dotenv()

# --- DB Initialization ---
try:
    models.Base.metadata.create_all(bind=database.engine)
except Exception as e:
    print(f"Startup Warning: Database initialization delayed or failed: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Render環境ではフォルダ監視（watchdog）を完全に無効化して起動を最優先する
    if not os.getenv("RENDER"):
        try:
            print("Starting directory watcher...")
            watch_thread = threading.Thread(target=watcher.start_watching, daemon=True)
            watch_thread.start()
        except Exception as e:
            print(f"Watcher could not start: {e}")
    else:
        print("Running on Render: Directory watcher is disabled for stability.")
    yield

app = FastAPI(title="Business Card API", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth
VALID_USERS = {"admin": "admin123", "member": "member123", "test": "test123"}

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in ["/", "/docs", "/openapi.json"]:
        return await call_next(request)
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return Response(content="Unauthorized", status_code=401, headers={"WWW-Authenticate": 'Basic realm="login"'})
    try:
        decoded = base64.b64decode(auth_header.split(" ")[1]).decode("utf-8")
        user, _, pwd = decoded.partition(":")
        if user in VALID_USERS and secrets.compare_digest(pwd, VALID_USERS[user]):
            return await call_next(request)
    except: pass
    return Response(content="Unauthorized", status_code=401)

@app.get("/")
async def root():
    return {"status": "online", "render": os.getenv("RENDER") is not None}

# Uploads
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- OCR Route ---
@app.post("/cards/ocr")
async def upload_card(file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    try:
        content = await file.read()
        ext = os.path.splitext(file.filename)[1].lower()
        filename = f"{uuid.uuid4()}{ext}"
        save_path = os.path.join(UPLOAD_DIR, filename)
        with open(save_path, "wb") as f:
            f.write(content)
        
        # Perform OCR
        results = watcher.perform_ocr(save_path)
        if not results: results = [{}]

        registered = []
        for idx, res in enumerate(results):
            db_card = models.DBBusinessCard(
                name=res.get("name") or "OCR解析失敗 (要確認)",
                company_name=res.get("company_name") or "",
                department=res.get("department"),
                title=res.get("title"),
                phone_number=res.get("phone_number"),
                email=res.get("email"),
                address=res.get("address"),
                memo=res.get("memo") or "自動アップロード",
                image_path=f"/uploads/{filename}" if idx == 0 else None
            )
            db.add(db_card)
            db.commit()
            db.refresh(db_card)
            registered.append(db_card)
        return {"status": "success", "cards": registered}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cards/")
def get_cards(skip: int = 0, limit: int = 100, db: Session = Depends(database.get_db)):
    cards = db.query(models.DBBusinessCard).order_by(models.DBBusinessCard.created_at.desc()).offset(skip).limit(limit).all()
    return {"cards": cards, "total": db.query(models.DBBusinessCard).count()}

@app.delete("/cards/{card_id}")
def delete_card(card_id: str, db: Session = Depends(database.get_db)):
    card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if not card: raise HTTPException(status_code=404)
    db.delete(card)
    db.commit()
    return {"status": "deleted"}
