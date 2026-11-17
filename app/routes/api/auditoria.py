# app/routes/api/auditoria.py
# Rotas: GET /api/auditoria, POST /api/backup-sd/sincronizar,
#         GET /api/backup-sd/status, POST /api/backup-sd/restaurar-esp32
# Transferido integralmente de main.py (linhas 2259–2473)

import math
import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query

from app.core.database import DB_PATH
from app.services.auditoria_service import registrar_auditoria
from app.services.mqtt_service import esp_telemetria

router = APIRouter()


@router.get("/api/auditoria")
def get_auditoria(
    categoria: Optional[str] = Query(None),
    nivel: Optional[str] = Query(None),
    busca: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    pagina: int = Query(1),
    limite: int = Query(30),
    exportar: int = Query(0)
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cat_str = str(categoria).strip().upper() if categoria and isinstance(categoria, str) else None
    niv_str = str(nivel).strip().upper() if nivel and isinstance(nivel, str) else None
    bus_str = str(busca).strip() if busca and isinstance(busca, str) else None
    dt_ini = str(data_inicio).strip() if data_inicio and isinstance(data_inicio, str) else None
    dt_fim = str(data_fim).strip() if data_fim and isinstance(data_fim, str) else None

    # Sanitização de paginação
    pag_val = int(pagina) if isinstance(pagina, (int, float)) and int(pagina) >= 1 else 1
    lim_val = int(limite) if isinstance(limite, (int, float)) and int(limite) > 0 else 30

    condicoes = []
    params = []

    if cat_str and cat_str != "TODAS":
        condicoes.append("categoria = ?")
        params.append(cat_str)

    if niv_str and niv_str != "TODOS":
        condicoes.append("nivel = ?")
        params.append(niv_str)

    if bus_str:
        condicoes.append("(acao LIKE ? OR detalhes LIKE ? OR usuario_origem LIKE ?)")
        termo = f"%{bus_str}%"
        params.extend([termo, termo, termo])

    if dt_ini:
        condicoes.append("date(timestamp) >= date(?)")
        params.append(dt_ini)

    if dt_fim:
        condicoes.append("date(timestamp) <= date(?)")
        params.append(dt_fim)

    where_sql = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""

    # 1. Total de registros correspondentes ao filtro
    count_query = f"SELECT COUNT(*) FROM auditoria_logs {where_sql}"
    cursor.execute(count_query, params)
    total_filtrados = cursor.fetchone()[0] or 0

    # 2. Cálculo de páginas e fatiamento
    if exportar == 1:
        # Quando solicitado para exportação CSV, busca todos os registros filtrados (limite de segurança 10.000)
        total_paginas = 1
        pag_val = 1
        query = f"""
            SELECT id, timestamp, categoria, acao, detalhes, nivel, usuario_origem
            FROM auditoria_logs
            {where_sql}
            ORDER BY id DESC
            LIMIT 10000
        """
        cursor.execute(query, params)
    else:
        total_paginas = max(1, math.ceil(total_filtrados / lim_val)) if total_filtrados > 0 else 1
        if pag_val > total_paginas:
            pag_val = total_paginas
        offset = (pag_val - 1) * lim_val

        query = f"""
            SELECT id, timestamp, categoria, acao, detalhes, nivel, usuario_origem
            FROM auditoria_logs
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """
        params_paginados = list(params) + [lim_val, offset]
        cursor.execute(query, params_paginados)

    rows = cursor.fetchall()

    logs = []
    for r in rows:
        logs.append({
            "id": r[0],
            "timestamp": r[1],
            "categoria": r[2],
            "acao": r[3],
            "detalhes": r[4],
            "nivel": r[5],
            "origem": r[6]
        })

    # Total geral cadastrado na base
    cursor.execute("SELECT COUNT(*) FROM auditoria_logs")
    total_geral = cursor.fetchone()[0] or 0

    # Estatísticas e KPIs rápidos de Auditoria
    cursor.execute("SELECT COUNT(*) FROM auditoria_logs WHERE categoria = 'ESP32_CONEXAO' AND acao LIKE '%Desconectado%'")
    total_quedas = cursor.fetchone()[0] or 0

    cursor.execute("SELECT COUNT(*) FROM auditoria_logs WHERE categoria IN ('TARIFA', 'BANDEIRA', 'ICMS', 'CONFIG')")
    total_alteracoes_tarifas = cursor.fetchone()[0] or 0

    cursor.execute("SELECT timestamp FROM auditoria_logs WHERE categoria = 'ESP32_CONEXAO' AND acao LIKE '%Desconectado%' ORDER BY id DESC LIMIT 1")
    row_ultima_queda = cursor.fetchone()
    ultima_queda = row_ultima_queda[0] if row_ultima_queda else "Nenhuma queda registrada"

    cursor.execute("SELECT timestamp, acao, detalhes FROM auditoria_logs WHERE categoria IN ('TARIFA', 'BANDEIRA', 'ICMS', 'CONFIG') ORDER BY id DESC LIMIT 1")
    row_ult_tarifa = cursor.fetchone()
    ultima_alteracao_tarifa = f"{row_ult_tarifa[0]} - {row_ult_tarifa[1]}" if row_ult_tarifa else "Nenhuma alteração registrada"

    conn.close()

    return {
        "status": "sucesso",
        "pagina_atual": pag_val,
        "limite": lim_val,
        "total_filtrados": total_filtrados,
        "total_paginas": total_paginas,
        "total_registros": total_geral,
        "kpis": {
            "total_quedas": total_quedas,
            "ultima_queda": ultima_queda,
            "total_alteracoes_tarifas": total_alteracoes_tarifas,
            "ultima_alteracao_tarifa": ultima_alteracao_tarifa,
            "esp32_status_atual": esp_telemetria.get("status", "desconhecido"),
            "esp32_ip_atual": esp_telemetria.get("ip", "--"),
            "esp32_uptime": esp_telemetria.get("uptime", 0)
        },
        "logs": logs
    }


# --- ROTAS DE BACKUP E SINCRONIZAÇÃO DO CARTÃO MICRO SD ---

@router.post("/api/backup-sd/sincronizar")
def api_sincronizar_backup_sd(ip: Optional[str] = Query(None)):
    """Executa a sincronização remota do cartão SD do ESP32 com política de retenção dos 2 mais recentes."""
    from sincronizador_backup import sincronizar_backup_sd, DEFAULT_BACKUP_DIR
    alvo_ip = ip or esp_telemetria.get("ip") or "IP_DO_ESP32"
    resultado = sincronizar_backup_sd(esp32_ip=alvo_ip, destino_dir=DEFAULT_BACKUP_DIR, registrar_auditoria=True)
    return resultado


@router.get("/api/backup-sd/status")
def api_status_backup_sd():
    """Retorna o inventário dos arquivos de backup diário armazenados localmente no servidor."""
    from sincronizador_backup import DEFAULT_BACKUP_DIR
    pasta = DEFAULT_BACKUP_DIR
    arquivos = []
    total_bytes = 0
    if pasta.exists():
        for p in sorted(pasta.glob("*.txt")):
            st = p.stat()
            total_bytes += st.st_size
            arquivos.append({
                "arquivo": p.name,
                "tamanho_bytes": st.st_size,
                "tamanho_kb": round(st.st_size / 1024.0, 2),
                "modificado": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })
    return {
        "status": "sucesso",
        "total_arquivos_locais": len(arquivos),
        "total_armazenado_kb": round(total_bytes / 1024.0, 2),
        "pasta_destino": str(pasta),
        "esp32_ip_detectado": esp_telemetria.get("ip", "--"),
        "arquivos": arquivos
    }


@router.post("/api/backup-sd/restaurar-esp32")
def api_restaurar_esp32_sd(pulsos: Optional[int] = Query(None)):
    """Restaura a contagem de pulsos consolidada no banco de dados SQLite de volta para o ESP32 e cartão SD."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if pulsos is None:
        cursor.execute("SELECT valor FROM configuracoes WHERE chave='ultimo_pulsos_esp'")
        row = cursor.fetchone()
        pulsos_alvo = int(row[0]) if row and row[0] else 0
    else:
        pulsos_alvo = int(pulsos)
    conn.close()

    alvo_ip = esp_telemetria.get("ip") or "IP_DO_ESP32"
    url = f"http://{alvo_ip}/restaurar-pulsos?val={pulsos_alvo}"
    try:
        from sincronizador_backup import HAS_REQUESTS
        if HAS_REQUESTS:
            import requests
            r = requests.post(url, timeout=6)
            sucesso = r.status_code == 200
        else:
            import urllib.request
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=6) as resp:
                sucesso = resp.status == 200

        if sucesso:
            registrar_auditoria("BACKUP_SD", "Restauração de Contagem no SD", f"Contagem de {pulsos_alvo} pulsos enviada com sucesso para o ESP32 ({alvo_ip}) e gravada no Micro SD", "INFO", "Painel Web")
            return {
                "status": "sucesso",
                "mensagem": f"Contagem de {pulsos_alvo} pulsos ({pulsos_alvo/800.0:.3f} kWh) restaurada com sucesso no ESP32 e gravada no Cartão SD!",
                "pulsos": pulsos_alvo
            }
        else:
            return {"status": "erro", "mensagem": "ESP32 recusou ou falhou ao restaurar a contagem."}
    except Exception as e:
        return {"status": "erro", "mensagem": f"Falha de comunicacao com o ESP32 ({alvo_ip}): {e}"}

