#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sincronizador de Backup e Retenção do Micro SD - Blackburn-Pulse
Engenharia de Firmware IoT & Backend Python
"""

import argparse
import json
import os
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Suporte dual: tenta requests, com fallback transparente para urllib padrão
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.error
    import urllib.parse
    import urllib.request
    HAS_REQUESTS = False

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "banco_energia.db"
DEFAULT_BACKUP_DIR = BASE_DIR / "historico_energia"
PADRAO_ARQUIVO_DIARIO = re.compile(r"^(\d{4}-\d{2}-\d{2})\.txt$")


def log(msg: str, nivel: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefixos = {
        "INFO": "[INFO]",
        "SUCESSO": "[✓ SUCESSO]",
        "AVISO": "[! AVISO]",
        "ERRO": "[✗ ERRO]"
    }
    pref = prefixos.get(nivel, f"[{nivel}]")
    print(f"{timestamp} {pref} {msg}")


def obter_ip_esp32_dinamico(db_path: Path = DB_PATH) -> str:
    """
    Descobre dinamicamente o IP do ESP32 por 3 métodos automáticos:
    1. Resolução mDNS pelo hostname 'esp32-energia.local'
    2. Leitura do último IP vivo reportado via Heartbeat MQTT no banco SQLite
    3. Fallback padrão configurado
    """
    # 1. Tenta resolver via mDNS
    try:
        ip_mdns = socket.gethostbyname("esp32-energia.local")
        if ip_mdns and ip_mdns != "127.0.0.1":
            log(f"ESP32 descoberto via mDNS (esp32-energia.local): {ip_mdns}")
            return ip_mdns
    except Exception:
        pass

    # 2. Tenta ler o último IP vivo registrado pelo Heartbeat MQTT no banco
    if db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT detalhes FROM auditoria_logs 
                WHERE categoria = 'ESP32_CONEXAO' AND detalhes LIKE '%IP:%'
                ORDER BY id DESC LIMIT 1
            """)
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                match = re.search(r"IP:\s*([\d\.]+)", row[0])
                if match:
                    ip_descoberto = match.group(1).strip()
                    log(f"ESP32 descoberto via Heartbeat recente no banco: {ip_descoberto}")
                    return ip_descoberto
        except Exception as e:
            log(f"Falha ao consultar IP do banco: {e}", "AVISO")

    # 3. Fallback de fábrica
    return "192.168.200.5"


def http_get_json(url: str, timeout: int = 10):
    """Executa requisição GET retornando JSON com suporte dual (requests ou urllib)."""
    if HAS_REQUESTS:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "Blackburn-Sync/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode("utf-8")
            return json.loads(data)


def http_download_file(url: str, destino_path: Path, timeout: int = 15) -> bool:
    """Faz o download em stream do arquivo remoto e salva localmente."""
    destino_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destino_path.with_suffix(".tmp_download")
    try:
        if HAS_REQUESTS:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(temp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Blackburn-Sync/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                with open(temp_path, "wb") as f:
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)

        # Validação de integridade do arquivo baixado
        if temp_path.exists() and temp_path.stat().st_size > 0:
            if destino_path.exists():
                destino_path.unlink()
            temp_path.rename(destino_path)
            return True
        else:
            if temp_path.exists():
                temp_path.unlink()
            return False
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise e


def http_post_apagar(url: str, timeout: int = 10) -> bool:
    """Envia requisição POST para exclusão segura de arquivo no ESP32."""
    if HAS_REQUESTS:
        resp = requests.post(url, timeout=timeout)
        return resp.status_code == 200
    else:
        req = urllib.request.Request(url, data=b"", headers={"User-Agent": "Blackburn-Sync/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status == 200


def registrar_log_auditoria(categoria: str, acao: str, detalhes: str, nivel: str = "INFO", origem: str = "Sincronizador SD"):
    """Insere evento de auditoria no SQLite do sistema."""
    try:
        if not DB_PATH.exists():
            return
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO auditoria_logs (timestamp, categoria, acao, detalhes, nivel, usuario_origem)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (agora_str, categoria, acao, detalhes, nivel, origem))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Falha ao registrar auditoria: {e}", "AVISO")


def sincronizar_backup_sd(esp32_ip: str = None, destino_dir: Path = None, registrar_auditoria: bool = True) -> dict:
    """
    Executa a rotina completa de backup, verificação de integridade e retenção segura.
    Regra de Exclusão: NUNCA apaga os 2 arquivos mais recentes do cartão SD do ESP32.
    """
    if not esp32_ip:
        esp32_ip = os.getenv("ESP32_IP") or obter_ip_esp32_dinamico()

    if not destino_dir:
        destino_dir = DEFAULT_BACKUP_DIR

    destino_dir = Path(destino_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)

    base_url = f"http://{esp32_ip}"
    url_listar = f"{base_url}/listar"

    log(f"Iniciando rotina de sincronização de backup do Micro SD...")
    log(f"Alvo ESP32: {base_url} | Diretório Local: {destino_dir}")

    # 1. Obter lista de arquivos no SD
    try:
        arquivos_remotos = http_get_json(url_listar, timeout=10)
    except Exception as e:
        msg_erro = f"Não foi possível conectar ao ESP32 ({base_url}): {e}"
        log(msg_erro, "ERRO")
        if registrar_auditoria:
            registrar_log_auditoria("BACKUP_SD", "Falha na Conexão SD", msg_erro, "AVISO")
        return {
            "status": "erro",
            "mensagem": msg_erro,
            "arquivos_baixados": [],
            "arquivos_retidos_sd": [],
            "arquivos_apagados_sd": []
        }

    if not isinstance(arquivos_remotos, list):
        msg_erro = f"Resposta inesperada do endpoint /listar: {arquivos_remotos}"
        log(msg_erro, "ERRO")
        return {"status": "erro", "mensagem": msg_erro}

    # 2. Filtrar e classificar arquivos diários (YYYY-MM-DD.txt) e pulsos.txt
    arquivos_diarios = []
    tem_pulsos_txt = False

    for item in arquivos_remotos:
        nome = item.get("arquivo", "")
        if nome == "pulsos.txt":
            tem_pulsos_txt = True
        elif PADRAO_ARQUIVO_DIARIO.match(nome):
            arquivos_diarios.append(nome)

    # Ordenação cronológica (pelo nome YYYY-MM-DD.txt a ordenação alfabética é idêntica à cronológica)
    arquivos_diarios.sort()

    log(f"Arquivos diários históricos encontrados no SD ({len(arquivos_diarios)}): {arquivos_diarios}")

    # 3. Aplicar a Regra de Retenção e Segurança dos 2 Mais Recentes
    # Os 2 mais recentes NUNCA podem ser apagados do SD
    if len(arquivos_diarios) <= 2:
        arquivos_protegidos = set(arquivos_diarios)
        arquivos_candidatos_expurgo = []
    else:
        # Os últimos 2 elementos da lista cronológica são protegidos
        arquivos_protegidos = set(arquivos_diarios[-2:])
        # Todos os anteriores são candidatos à liberação de espaço após download
        arquivos_candidatos_expurgo = arquivos_diarios[:-2]

    log(f"Arquivos protegidos no SD (Regra dos 2 Mais Recentes): {list(arquivos_protegidos)}")
    log(f"Arquivos candidatos a expurgo após validação local ({len(arquivos_candidatos_expurgo)}): {arquivos_candidatos_expurgo}")

    arquivos_baixados = []
    arquivos_apagados_sd = []

    # 4. Backup do totalizador rápido (pulsos.txt), se presente
    if tem_pulsos_txt:
        try:
            url_baixar_pulsos = f"{base_url}/baixar?arq=pulsos.txt"
            destino_pulsos = destino_dir / "pulsos_acumulados.txt"
            if http_download_file(url_baixar_pulsos, destino_pulsos):
                log("Snapshot do totalizador /pulsos.txt sincronizado com sucesso.")
        except Exception as e:
            log(f"Aviso ao sincronizar /pulsos.txt: {e}", "AVISO")

    # 5. Download de todos os arquivos diários encontrados
    for arq in arquivos_diarios:
        url_download = f"{base_url}/baixar?arq={arq}"
        destino_arquivo = destino_dir / arq

        sucesso_download = False
        try:
            log(f"Baixando '{arq}' do ESP32...")
            sucesso_download = http_download_file(url_download, destino_arquivo)
            if sucesso_download:
                tamanho_kb = destino_arquivo.stat().st_size / 1024.0
                log(f"Arquivo '{arq}' baixado e verificado com sucesso ({tamanho_kb:.2f} KB).", "SUCESSO")
                arquivos_baixados.append(arq)
            else:
                log(f"Download de '{arq}' falhou: arquivo vazio.", "ERRO")
        except Exception as e:
            log(f"Erro ao baixar '{arq}': {e}", "ERRO")
            sucesso_download = False

        # 6. Exclusão no SD apenas para arquivos antigos confirmados e fora da zona de proteção
        if arq in arquivos_candidatos_expurgo and sucesso_download:
            # Dupla checagem de integridade local antes de autorizar remoção remota
            if destino_arquivo.exists() and destino_arquivo.stat().st_size > 0:
                try:
                    url_apagar = f"{base_url}/apagar?arq={arq}"
                    log(f"Expurgando arquivo histórico '{arq}' do cartão SD (retenção segura)...")
                    if http_post_apagar(url_apagar):
                        log(f"Arquivo '{arq}' removido do cartão SD com sucesso.", "SUCESSO")
                        arquivos_apagados_sd.append(arq)
                    else:
                        log(f"ESP32 recusou exclusão de '{arq}'.", "AVISO")
                except Exception as e:
                    log(f"Erro ao solicitar exclusão de '{arq}' no ESP32: {e}", "AVISO")
            else:
                log(f"Segurança ativa: Exclusão de '{arq}' abortada por falha na integridade local.", "AVISO")

    # 7. Resumo e registro de auditoria
    total_baixados = len(arquivos_baixados)
    total_retidos = len(arquivos_diarios) - len(arquivos_apagados_sd)
    total_expurgados = len(arquivos_apagados_sd)

    detalhes_log = (
        f"Alvo ESP32 ({esp32_ip}): {total_baixados} baixados, "
        f"{total_retidos} mantidos no SD (proteção 2 dias), "
        f"{total_expurgados} antigos expurgados."
    )
    log(detalhes_log, "SUCESSO")

    if registrar_auditoria:
        registrar_log_auditoria(
            categoria="BACKUP_SD",
            acao="Sincronização Micro SD",
            detalhes=detalhes_log,
            nivel="INFO",
            origem="Sincronizador SD"
        )

    return {
        "status": "sucesso",
        "esp32_ip": esp32_ip,
        "diretorio_local": str(destino_dir),
        "total_encontrados": len(arquivos_diarios),
        "arquivos_baixados": arquivos_baixados,
        "arquivos_retidos_sd": list(arquivos_protegidos),
        "arquivos_apagados_sd": arquivos_apagados_sd,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def main():
    parser = argparse.ArgumentParser(description="Sincronizador de Backup e Retenção do Micro SD - Blackburn-Pulse")
    parser.add_argument("--ip", type=str, default=None, help="Endereço IP do ESP32 (ex: 192.168.200.5)")
    parser.add_argument("--dir", type=str, default=None, help="Diretório de destino dos backups locais")
    parser.add_argument("--no-audit", action="store_true", help="Desativa gravação no banco de auditoria")

    args = parser.parse_args()
    resultado = sincronizar_backup_sd(
        esp32_ip=args.ip,
        destino_dir=args.dir,
        registrar_auditoria=not args.no_audit
    )

    if resultado.get("status") == "sucesso":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()

