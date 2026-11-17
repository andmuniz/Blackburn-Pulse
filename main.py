# main.py — Ponto de entrada da aplicação Blackburn-Pulse
# Arquitetura MVC em Camadas (Clean Code)
# Refatorado a partir do main.py monolítico (2474 linhas)
# Todos os comportamentos, rotas, SQL e regras de negócio preservados integralmente.

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.database import BASE_DIR, init_db
from app.services import mqtt_service  # inicializa MQTT e loop_start() ao importar

from app.routes.pages import router as pages_router
from app.routes.api.energia import router as energia_router
from app.routes.api.analytics import router as analytics_router
from app.routes.api.auditoria import router as auditoria_router

# Garante que todas as tabelas existam antes de servir requisições
init_db()

app = FastAPI(title="Blackburn-Pulse")

app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
    "blackburn-pulse.local", "api.blackburn-pulse.local", "*.blackburn-pulse.local",
    "blackburn-pulse.lan", "api.blackburn-pulse.lan", "*.blackburn-pulse.lan",
    "*.lan", "localhost", "127.0.0.1", "[IP_ADDRESS]", "meuapp.local",
])
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["127.0.0.1", "localhost"])

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(pages_router)
app.include_router(energia_router)
app.include_router(analytics_router)
app.include_router(auditoria_router)
