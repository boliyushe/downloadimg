import os
import shutil
import tempfile
import pandas as pd
import requests
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TASKS = {}

def download_image(session, url, save_path):
    retries = 3
    for i in range(retries):
        try:
            if os.path.exists(save_path):
                return True
            resp = session.get(url, timeout=40)
            resp.raise_for_status()
            with open(save_path, 'wb') as f:
                f.write(resp.content)
            return True
        except Exception as e:
            if i < retries - 1:
                import time
                time.sleep(5 * (i + 1))
            else:
                return False

def get_filename_from_url(url):
    from urllib.parse import urlparse, unquote
    path = urlparse(url).path
    filename = os.path.basename(path)
    return unquote(filename) if filename else "file.jpg"

def download_task(task_id, xlsx_path, columns):
    TASKS[task_id]['status'] = 'downloading'
    TASKS[task_id]['progress'] = 0
    TASKS[task_id]['error'] = ''
    try:
        df = pd.read_excel(xlsx_path)
        total = 0
        tasks = []
        for col in columns:
            if col not in df.columns:
                TASKS[task_id]['error'] = f"列名不存在: {col}"
                TASKS[task_id]['status'] = 'error'
                return
            urls = df[col].dropna().tolist()
            total += len(urls)
            TASKS[task_id]['columns'][col] = len(urls)
            for url in urls:
                filename = get_filename_from_url(str(url))
                save_dir = os.path.join(TASKS[task_id]['workdir'], col)
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, filename)
                tasks.append((url, save_path, col))
        TASKS[task_id]['total'] = total
        TASKS[task_id]['done'] = 0

        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=16) as executor:
                futures = []
                for url, save_path, col in tasks:
                    futures.append(executor.submit(download_image, session, url, save_path))
                for fut in futures:
                    fut.result()
                    TASKS[task_id]['done'] += 1
                    TASKS[task_id]['progress'] = int(TASKS[task_id]['done'] / total * 100)
        # 打包结果
        zip_path = os.path.join(TASKS[task_id]['workdir'], f"{task_id}_result.zip")
        shutil.make_archive(zip_path.replace('.zip', ''), 'zip', TASKS[task_id]['workdir'])
        TASKS[task_id]['zip'] = zip_path
        TASKS[task_id]['status'] = 'done'
    except Exception as e:
        TASKS[task_id]['error'] = str(e)
        TASKS[task_id]['status'] = 'error'

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    columns: List[str] = Form(...)
):
    task_id = str(uuid.uuid4())
    workdir = tempfile.mkdtemp(prefix=f"dl_{task_id}_")
    xlsx_path = os.path.join(workdir, file.filename)
    with open(xlsx_path, "wb") as f:
        f.write(await file.read())
    TASKS[task_id] = {
        "status": "pending",
        "progress": 0,
        "error": "",
        "workdir": workdir,
        "columns": {col: 0 for col in columns},
        "total": 0,
        "done": 0,
        "zip": ""
    }
    threading.Thread(target=download_task, args=(task_id, xlsx_path, columns), daemon=True).start()
    return {"task_id": task_id}

@app.get("/progress/{task_id}")
def progress(task_id: str):
    if task_id not in TASKS:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    info = TASKS[task_id]
    return {
        "status": info["status"],
        "progress": info["progress"],
        "error": info["error"],
        "columns": info["columns"],
        "total": info["total"],
        "done": info["done"]
    }

@app.get("/download/{task_id}")
def download(task_id: str):
    if task_id not in TASKS or TASKS[task_id]["status"] != "done":
        return JSONResponse({"error": "任务未完成"}, status_code=400)
    return FileResponse(TASKS[task_id]["zip"], filename=f"{task_id}_result.zip", media_type="application/zip") 