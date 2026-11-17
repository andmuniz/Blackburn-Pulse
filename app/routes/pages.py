"""
app/routes/pages.py
Camada Controller — Rotas que renderizam os templates HTML Jinja2.
Inclui rotas do Progressive Web App (PWA): manifest.json e sw.js.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

from app.core.database import BASE_DIR

router = APIRouter()

templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    return FileResponse(BASE_DIR / "static" / "manifest.json", media_type="application/manifest+json")


@router.get("/sw.js")
def get_sw():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


@router.get("/manual", response_class=HTMLResponse)
def manual(request: Request):
    return render_template("manual.html", request)


@router.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request):
    return render_template("analytics.html", request)


@router.get("/auditoria", response_class=HTMLResponse)
def auditoria_page(request: Request):
    return render_template("auditoria.html", request)

