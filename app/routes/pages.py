"""
app/routes/pages.py
Camada Controller — Rotas que renderizam os templates HTML Jinja2.
Inclui rotas do Progressive Web App (PWA): manifest.json e sw.js.
"""

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates

from app.core.database import BASE_DIR

router = APIRouter()

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_asset_version() -> int:
    """Calcula timestamp dinâmico baseado na modificação dos ícones para cache-busting 100% automático."""
    t = 1
    for name in ("icon-192.svg", "icon-512.svg", "manifest.json", "sw.js"):
        p = BASE_DIR / "static" / name
        if p.exists():
            try:
                t = max(t, int(p.stat().st_mtime))
            except Exception:
                pass
    return t


templates.env.globals["asset_v"] = get_asset_version


def render_template(name: str, request: Request, context: dict = None):
    """Função universal de renderização compatível com qualquer versão do Starlette/FastAPI."""
    ctx = context or {}
    ctx["request"] = request
    try:
        return templates.TemplateResponse(request=request, name=name, context=ctx)
    except TypeError:
        return templates.TemplateResponse(name, ctx)


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render_template("index.html", request)


# --- ROTAS DO PROGRESSIVE WEB APP (PWA) ---
@router.get("/manifest.json")
def get_manifest():
    manifest_path = BASE_DIR / "static" / "manifest.json"
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        v = get_asset_version()
        for icon in data.get("icons", []):
            src = icon.get("src", "").split("?")[0]
            icon["src"] = f"{src}?v={v}"
        content = json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        content = manifest_path.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/sw.js")
def get_sw():
    sw_path = BASE_DIR / "static" / "sw.js"
    try:
        content = sw_path.read_text(encoding="utf-8")
        v = get_asset_version()
        content = re.sub(
            r"const CACHE_NAME = ['\"][^'\"]+['\"];",
            f"const CACHE_NAME = 'blackburn-pulse-v{v}';",
            content,
        )
    except Exception:
        content = sw_path.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/favicon.ico")
def get_favicon():
    return FileResponse(
        BASE_DIR / "static" / "icon-192.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/manual", response_class=HTMLResponse)
def manual(request: Request):
    return render_template("manual.html", request)


@router.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request):
    return render_template("analytics.html", request)


@router.get("/auditoria", response_class=HTMLResponse)
def auditoria_page(request: Request):
    return render_template("auditoria.html", request)

