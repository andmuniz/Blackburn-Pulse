"""
app/services/auditoria_service.py
Serviço de Auditoria — Registro de eventos operacionais e gerenciais no SQLite.
"""

import sqlite3

from datetime import datetime

from app.core.database import DB_PATH


def registrar_auditoria(categoria: str, acao: str, detalhes: str, nivel: str = "INFO", origem: str = "Painel Web"):
    try:
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
        print(f"Erro ao registrar auditoria: {e}")

