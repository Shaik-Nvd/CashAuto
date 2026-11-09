"""FastAPI Backend server providing Web UI, API, and Real-time WebSockets for CashAuto."""
import asyncio
import os
import sys
import json
import signal
import subprocess
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="CashAuto Control Panel", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import tempfile

BASE_DIR = Path(__file__).resolve().parent
# On serverless platforms like Vercel, root filesystem is read-only.
try:
    RUNS_DIR = BASE_DIR / "runs"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
except (OSError, PermissionError):
    RUNS_DIR = Path(tempfile.gettempdir()) / "cashauto_runs"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

WEB_DIR = BASE_DIR / "web"

# Active execution process tracker
class ExecutionManager:
    def __init__(self):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_running: bool = False
        self.active_batch: List[str] = []
        self.active_subscribers: List[WebSocket] = []
        self.log_history: List[str] = []

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active_subscribers:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active_subscribers:
                self.active_subscribers.remove(ws)

    def log(self, text: str):
        self.log_history.append(text)
        if len(self.log_history) > 2000:
            self.log_history.pop(0)

manager = ExecutionManager()

class RunRequest(BaseModel):
    urls: List[str]
    timeout: int = 60
    retries: int = 1
    profile: str = ".cashify-browser"
    rules_path: Optional[str] = None

class RulesUpdateRequest(BaseModel):
    content: dict

@app.get("/api/status")
async def get_status():
    return {
        "is_running": manager.is_running,
        "active_urls": manager.active_batch,
        "runs_count": len(list(RUNS_DIR.glob("*"))) if RUNS_DIR.exists() else 0
    }

@app.get("/api/logs")
async def get_logs():
    return {"logs": manager.log_history}

@app.post("/api/start")
async def start_run(req: RunRequest):
    if manager.is_running:
        raise HTTPException(status_code=400, detail="Automation run is already in progress.")
    
    clean_urls = [u.strip() for u in req.urls if u.strip()]
    if not clean_urls:
        raise HTTPException(status_code=400, detail="Please provide at least one valid Cashify URL.")

    # Validate URLs format
    for u in clean_urls:
        if not ("cashify.in" in u and "/sell-old-mobile-phone/used-" in u):
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid URL: {u}. URLs must be like 'https://www.cashify.in/sell-old-mobile-phone/used-...'"
            )

    manager.is_running = True
    manager.active_batch = clean_urls
    manager.log_history = []
    
    # Launch async task
    asyncio.create_task(run_subprocess(clean_urls, req.timeout, req.retries, req.profile, req.rules_path))
    
    return {"status": "started", "queued_count": len(clean_urls)}

async def run_subprocess(urls: List[str], timeout: int, retries: int, profile: str, rules_path: Optional[str]):
    cmd = [
        sys.executable,
        str(BASE_DIR / "cashify_runner.py"),
        *urls,
        "--timeout", str(timeout),
        "--retries", str(retries),
        "--profile", profile,
    ]
    if rules_path:
        cmd.extend(["--rules", rules_path])

    manager.log(f">>> [SYSTEM] Starting runner with {len(urls)} device URL(s)...")
    await manager.broadcast({"type": "status", "is_running": True, "event": "start", "urls": urls})
    await manager.broadcast({"type": "log", "text": f">>> Running: {' '.join(cmd)}\n"})

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        manager.process = proc

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode('utf-8', errors='replace')
            manager.log(decoded)
            await manager.broadcast({"type": "log", "text": decoded})

        await proc.wait()
        exit_code = proc.returncode
        msg = f">>> [SYSTEM] Runner completed with exit code {exit_code}."
        manager.log(msg)
        await manager.broadcast({"type": "log", "text": msg + "\n"})
        await manager.broadcast({"type": "status", "is_running": False, "event": "finish", "exit_code": exit_code})
    except Exception as e:
        err = f">>> [SYSTEM ERROR] {str(e)}"
        manager.log(err)
        await manager.broadcast({"type": "log", "text": err + "\n"})
        await manager.broadcast({"type": "status", "is_running": False, "event": "error", "error": str(e)})
    finally:
        manager.is_running = False
        manager.process = None

@app.post("/api/stop")
async def stop_run():
    if not manager.is_running or not manager.process:
        return {"status": "not_running"}
    
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(manager.process.pid)], capture_output=True)
        else:
            manager.process.send_signal(signal.SIGINT)
        msg = ">>> [SYSTEM] Stop signal sent to runner."
        manager.log(msg)
        await manager.broadcast({"type": "log", "text": f"\n{msg}\n"})
        manager.is_running = False
        return {"status": "stopped"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/runs")
async def list_runs():
    if not RUNS_DIR.exists():
        return []
    runs = []
    for item in sorted(RUNS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if item.is_dir():
            chk_file = item / "checkpoint.json"
            diag_file = item / "diagnostic.json"
            zip_file = item / "Cashify_Test_Results.zip"
            report_csv = item / "audit_report.csv"
            
            info = {
                "name": item.name,
                "modified": item.stat().st_mtime,
                "has_checkpoint": chk_file.exists(),
                "has_diagnostic": diag_file.exists(),
                "has_zip": zip_file.exists(),
                "has_csv": report_csv.exists(),
                "zip_size": zip_file.stat().st_size if zip_file.exists() else 0,
            }
            if chk_file.exists():
                try:
                    with chk_file.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                        info["device_url"] = data.get("url")
                        info["status"] = data.get("status")
                        info["variants_count"] = len(data.get("variants", []))
                        info["last_updated"] = data.get("last_updated")
                except Exception:
                    pass
            runs.append(info)
    return runs

@app.get("/api/runs/{run_name}")
async def get_run_detail(run_name: str):
    folder = RUNS_DIR / run_name
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    
    chk_file = folder / "checkpoint.json"
    diag_file = folder / "diagnostic.json"
    
    data = {"name": run_name}
    if chk_file.exists():
        try:
            with open(chk_file, "r", encoding="utf-8") as f:
                data["checkpoint"] = json.load(f)
        except Exception as e:
            data["checkpoint_error"] = str(e)
            
    if diag_file.exists():
        try:
            with open(diag_file, "r", encoding="utf-8") as f:
                data["diagnostic"] = json.load(f)
        except Exception as e:
            data["diagnostic_error"] = str(e)
            
    files = [f.name for f in folder.iterdir()]
    data["files"] = files
    return data

@app.get("/api/download/{run_name}/{filename}")
async def download_run_file(run_name: str, filename: str):
    file_path = RUNS_DIR / run_name / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, filename=filename)

@app.get("/api/rules")
async def get_rules():
    rules_file = BASE_DIR / "rules.json"
    example_file = BASE_DIR / "rules.example.json"
    if rules_file.exists():
        try:
            return json.loads(rules_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    elif example_file.exists():
        try:
            return json.loads(example_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

@app.post("/api/rules")
async def save_rules(req: RulesUpdateRequest):
    rules_file = BASE_DIR / "rules.json"
    try:
        rules_file.write_text(json.dumps(req.content, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    manager.active_subscribers.append(websocket)
    try:
        # Send initial status & last 100 log lines
        await websocket.send_json({
            "type": "init",
            "is_running": manager.is_running,
            "active_batch": manager.active_batch,
            "recent_logs": manager.log_history[-100:]
        })
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        if websocket in manager.active_subscribers:
            manager.active_subscribers.remove(websocket)

# Serve static web frontend
if (WEB_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

def run():
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)

if __name__ == "__main__":
    run()
