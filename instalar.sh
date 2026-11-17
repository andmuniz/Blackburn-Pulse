#!/bin/bash
# ============================================================================
#  ⚡ Blackburn-Pulse — Script de Instalação Automatizada
#  Medição Residencial Inteligente de Alta Disponibilidade
# ============================================================================
#  Uso: sudo bash instalar.sh
#  O script deve ser executado COM SUDO a partir da raiz do projeto clonado.
# ============================================================================

set -e

# ── Cores e formatação ──────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # Reset

banner() {
    echo ""
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║          ⚡  BLACKBURN-PULSE  —  INSTALADOR             ║"
    echo "  ║     Medição Residencial Inteligente de Alta Dispo.      ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

info()    { echo -e "  ${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "  ${YELLOW}[!]${NC} $1"; }
erro()    { echo -e "  ${RED}[✗]${NC} $1"; }
etapa()   { echo ""; echo -e "  ${CYAN}${BOLD}── $1 ──${NC}"; }
separar() { echo -e "  ${CYAN}──────────────────────────────────────────────────────${NC}"; }

# ── Verificação de privilégios ──────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    erro "Este script precisa ser executado com privilégios de root (sudo)."
    echo "  Execute: sudo bash instalar.sh"
    exit 1
fi

# ── Detecta o usuário real (mesmo sob sudo) ─────────────────────────────────
REAL_USER="${SUDO_USER:-$USER}"
USER_HOME=$(eval echo "~$REAL_USER")

# ── Detecta o diretório fonte (onde o script está sendo executado) ──────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$SCRIPT_DIR/main.py" ]; then
    erro "Arquivo main.py não encontrado em '$SCRIPT_DIR'."
    erro "Execute o script a partir da raiz do repositório Blackburn-Pulse."
    exit 1
fi

banner

echo -e "  ${BOLD}Usuário de execução:${NC} $REAL_USER"
echo -e "  ${BOLD}Diretório home:${NC}      $USER_HOME"
echo -e "  ${BOLD}Diretório de origem:${NC} $SCRIPT_DIR"
separar

# ============================================================================
#  ETAPA 1 — Pré-checagem de Requisitos do Servidor (Hardware e SO)
# ============================================================================
etapa "ETAPA 1/8 — Verificação de Requisitos do Servidor"

FALHAS_PRECHECK=0

# 1.1 Gerenciador de Pacotes (Debian/Ubuntu/Raspberry Pi OS)
if command -v apt-get &> /dev/null; then
    info "Sistema operacional compatível (base Debian/Raspberry Pi OS detectada)."
else
    erro "Gerenciador apt-get não encontrado. Este instalador requer Debian, Ubuntu ou Raspberry Pi OS."
    FALHAS_PRECHECK=$((FALHAS_PRECHECK + 1))
fi

# 1.2 Sistema de Init (systemd)
if [ -d /run/systemd/system ] || command -v systemctl &> /dev/null; then
    info "Gerenciador de serviços systemd ativo e operacional."
else
    erro "systemd não detectado. O serviço blackburn-pulse.service necessita do systemd."
    FALHAS_PRECHECK=$((FALHAS_PRECHECK + 1))
fi

# 1.3 Conectividade com a Internet
info "Testando conexão com a internet..."
set +e
if curl -s --connect-timeout 4 https://deb.debian.org > /dev/null 2>&1 || \
   curl -s --connect-timeout 4 https://www.google.com > /dev/null 2>&1 || \
   ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1; then
    info "Conexão com a internet confirmada."
else
    erro "Sem conexão com a internet. O servidor precisa de acesso à rede para instalar pacotes."
    FALHAS_PRECHECK=$((FALHAS_PRECHECK + 1))
fi
set -e

# 1.4 Espaço em Disco Disponível (Mínimo 800 MB livre)
DISCO_LIVRE_MB=$(df -m "$USER_HOME" 2>/dev/null | awk 'NR==2 {print $4}')
DISCO_LIVRE_MB=${DISCO_LIVRE_MB:-0}
if [ "$DISCO_LIVRE_MB" -ge 800 ]; then
    info "Espaço em disco: ${DISCO_LIVRE_MB} MB livres (mínimo de 800 MB atendido)."
else
    warn "Espaço em disco baixo: apenas ${DISCO_LIVRE_MB} MB disponíveis. Recomendado liberar ao menos 1 GB."
fi

# 1.5 Memória RAM e Swap
RAM_TOTAL_MB=$(free -m 2>/dev/null | awk '/^Mem:/ {print $2}')
RAM_TOTAL_MB=${RAM_TOTAL_MB:-1024}
SWAP_TOTAL_MB=$(free -m 2>/dev/null | awk '/^Swap:/ {print $2}')
SWAP_TOTAL_MB=${SWAP_TOTAL_MB:-0}
if [ "$RAM_TOTAL_MB" -ge 500 ]; then
    info "Memória RAM disponível: ${RAM_TOTAL_MB} MB (Swap: ${SWAP_TOTAL_MB} MB)."
else
    warn "Memória RAM muito reduzida (${RAM_TOTAL_MB} MB). Se não houver swap, a instalação do pip pode ser lenta."
fi

# 1.6 Arquitetura de CPU
ARCH_DETECTADA=$(dpkg --print-architecture 2>/dev/null || uname -m)
info "Arquitetura do processador detectada: ${BOLD}$ARCH_DETECTADA${NC}"

if [ "$FALHAS_PRECHECK" -gt 0 ]; then
    echo ""
    erro "O servidor não atende aos requisitos mínimos fundamentais ($FALHAS_PRECHECK erro(s))."
    erro "Corrija os pontos listados acima antes de prosseguir com a instalação."
    exit 1
fi

info "Todos os pré-requisitos de hardware e sistema foram atendidos com sucesso!"

# ============================================================================
#  ETAPA 2 — Escolha do Diretório de Instalação (Menu Interativo)
# ============================================================================
etapa "ETAPA 2/8 — Diretório de Instalação do Projeto"

INSTALL_DIR_DEFAULT="$USER_HOME/Energia"

echo ""
echo -e "  ${BOLD}Onde deseja instalar o Blackburn-Pulse?${NC}"
echo ""
echo -e "    ${CYAN}[1]${NC} Padrão: $INSTALL_DIR_DEFAULT"
echo -e "    ${CYAN}[2]${NC} Escolher outro diretório personalizado"
echo ""
echo -e "  ${YELLOW}(Aguardando 30 segundos — sem resposta usará o diretório padrão)${NC}"
echo ""

INSTALL_DIR="$INSTALL_DIR_DEFAULT"
ESCOLHA=""
CUSTOM_DIR=""

if read -t 30 -p "  Sua escolha [1/2]: " ESCOLHA; then
    case "$ESCOLHA" in
        2)
            read -p "  Digite o caminho absoluto do diretório: " CUSTOM_DIR
            if [ -n "$CUSTOM_DIR" ]; then
                INSTALL_DIR="$CUSTOM_DIR"
            else
                warn "Entrada vazia. Usando diretório padrão: $INSTALL_DIR_DEFAULT"
                INSTALL_DIR="$INSTALL_DIR_DEFAULT"
            fi
            ;;
        *)
            INSTALL_DIR="$INSTALL_DIR_DEFAULT"
            ;;
    esac
else
    echo ""
    warn "Tempo de espera esgotado. Adotando diretório padrão: $INSTALL_DIR_DEFAULT"
    INSTALL_DIR="$INSTALL_DIR_DEFAULT"
fi

info "Diretório de instalação definido: $INSTALL_DIR"

# ============================================================================
#  ETAPA 3 — Copiar Arquivos do Projeto & Preservar Base de Dados
# ============================================================================
etapa "ETAPA 3/8 — Transferência dos Arquivos do Projeto"

mkdir -p "$INSTALL_DIR"

if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    info "Copiando componentes de '$SCRIPT_DIR' para '$INSTALL_DIR'..."

    ITENS_COPIAR=(
        "main.py"
        "sincronizador_backup.py"
        "LICENSE"
        "README.md"
        "MANUAL_Blackburn-Pulse.md"
        "app"
        "static"
        "templates"
        "esp32"
    )

    for item in "${ITENS_COPIAR[@]}"; do
        if [ -e "$SCRIPT_DIR/$item" ]; then
            cp -r "$SCRIPT_DIR/$item" "$INSTALL_DIR/"
            info "  Copiado: $item"
        else
            warn "  Não encontrado (ignorado): $item"
        fi
    done

    # Preserva dados existentes no banco SQLite
    if [ ! -f "$INSTALL_DIR/banco_energia.db" ]; then
        if [ -f "$SCRIPT_DIR/banco_energia.db" ]; then
            cp "$SCRIPT_DIR/banco_energia.db" "$INSTALL_DIR/"
            info "  Copiado: banco_energia.db (nova base inicial)"
        fi
    else
        info "  banco_energia.db existente detectado em '$INSTALL_DIR' — PRESERVADO (dados protegidos)."
    fi
else
    info "Projeto já está alocado no diretório de instalação."
fi

chown -R "$REAL_USER:$REAL_USER" "$INSTALL_DIR"
info "Permissões de pasta concedidas ao usuário '$REAL_USER'."

# ============================================================================
#  ETAPA 4 — Instalar Python 3 Completo e Bibliotecas do Projeto
# ============================================================================
etapa "ETAPA 4/8 — Instalação do Python 3 e Dependências do Backend"

info "Atualizando índices de repositórios do sistema operacional (apt update)..."
apt-get update -qq

info "Instalando Python 3, gerenciador pip, ferramentas venv e utilitários de compilação..."
apt-get install -y \
    curl \
    wget \
    ca-certificates \
    git \
    build-essential \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    libffi-dev \
    libssl-dev > /dev/null 2>&1

# Checagem da versão do Python instalada
PY_VERSAO=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
info "Versão do Python instalada no sistema: ${BOLD}Python $PY_VERSAO${NC}"

# Valida se é >= 3.10
PY_MAIOR=$(echo "$PY_VERSAO" | cut -d. -f1)
PY_MENOR=$(echo "$PY_VERSAO" | cut -d. -f2)
if [ "$PY_MAIOR" -lt 3 ] || { [ "$PY_MAIOR" -eq 3 ] && [ "$PY_MENOR" -lt 10 ]; }; then
    warn "Atenção: O projeto recomenda Python 3.10 ou superior (instalado: $PY_VERSAO)."
    warn "O sistema tentará instalar as bibliotecas normalmente."
fi

# Criação do Ambiente Virtual (.venv)
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    info "Criando ambiente virtual isolado (.venv)..."
    sudo -u "$REAL_USER" python3 -m venv "$INSTALL_DIR/.venv"
    info "Ambiente virtual criado em $INSTALL_DIR/.venv"
else
    info "Ambiente virtual existente reaproveitado em $INSTALL_DIR/.venv"
fi

# Instalação das bibliotecas no ambiente virtual isolado
info "Atualizando pip interno do virtualenv..."
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip

info "Instalando módulos Python (FastAPI, Uvicorn, Paho-MQTT, Jinja2, FPDF2, Requests)..."
"$INSTALL_DIR/.venv/bin/pip" install --quiet \
    "fastapi>=0.100.0" \
    "uvicorn[standard]>=0.23.0" \
    "paho-mqtt>=1.6.1" \
    "jinja2>=3.1.2" \
    "python-multipart>=0.0.6" \
    "requests>=2.31.0" \
    "fpdf2>=2.7.0"

info "Todas as dependências Python foram instaladas e validadas com sucesso."

# ============================================================================
#  ETAPA 5 — Configuração do Serviço systemd (Inicialização com Boot)
# ============================================================================
etapa "ETAPA 5/8 — Criação do Serviço de Inicialização Automática (systemd)"

VENV_PYTHON="$INSTALL_DIR/.venv/bin/python3"

tee /etc/systemd/system/blackburn-pulse.service > /dev/null <<EOF
[Unit]
Description=Blackburn-Pulse Energy Manager (FastAPI + MQTT)
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=$REAL_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_PYTHON -m uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips='*'
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable blackburn-pulse.service > /dev/null 2>&1
info "Serviço blackburn-pulse.service registrado e habilitado para iniciar no boot."

# ============================================================================
#  ETAPA 6 — Certificados SSL Locais (mkcert)
# ============================================================================
etapa "ETAPA 6/8 — Geração de Certificados SSL Criptografados (mkcert)"

IP_LOCAL=$(hostname -I 2>/dev/null | awk '{print $1}')
IP_LOCAL=${IP_LOCAL:-"127.0.0.1"}
info "Endereço IP da rede local detectado: ${BOLD}$IP_LOCAL${NC}"

apt-get install -y libnss3-tools > /dev/null 2>&1

if ! command -v mkcert &> /dev/null; then
    info "Instalando utilitário mkcert..."
    if apt-get install -y mkcert > /dev/null 2>&1; then
        info "mkcert instalado via repositório de pacotes."
    else
        ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
        if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then
            MKCERT_URL="https://dl.filippo.io/mkcert/latest?for=linux/arm64"
        elif [ "$ARCH" = "armhf" ] || [ "$ARCH" = "armv7l" ]; then
            MKCERT_URL="https://dl.filippo.io/mkcert/latest?for=linux/arm"
        else
            MKCERT_URL="https://dl.filippo.io/mkcert/latest?for=linux/amd64"
        fi
        warn "Baixando binário oficial do mkcert para arquitetura $ARCH..."
        curl -sSL "$MKCERT_URL" -o /usr/local/bin/mkcert
        chmod +x /usr/local/bin/mkcert
        info "mkcert instalado com sucesso em /usr/local/bin/mkcert"
    fi
else
    info "mkcert já presente no sistema operacional."
fi

CERTS_DIR="$INSTALL_DIR/certs"
mkdir -p "$CERTS_DIR/ca"

# Centraliza a CA do mkcert na pasta do projeto para fácil exportação
export CAROOT="$CERTS_DIR/ca"

mkcert -install 2>/dev/null || true
info "Autoridade Certificadora local (Root CA) inicializada."

if [ ! -f "$CERTS_DIR/local-cert.pem" ] || [ ! -f "$CERTS_DIR/local-key.pem" ]; then
    info "Gerando certificados assinados para domínios locais e IP..."
    cd "$CERTS_DIR"
    mkcert \
        -cert-file local-cert.pem \
        -key-file local-key.pem \
        blackburn-pulse.lan \
        api.blackburn-pulse.lan \
        blackburn-pulse.local \
        api.blackburn-pulse.local \
        "$IP_LOCAL" \
        localhost \
        127.0.0.1
    cd - > /dev/null
    info "Certificados SSL gerados com sucesso em $CERTS_DIR"
else
    info "Certificados SSL existentes preservados em $CERTS_DIR."
fi

chown -R "$REAL_USER:$REAL_USER" "$CERTS_DIR"
chmod 644 "$CERTS_DIR/local-cert.pem" 2>/dev/null || true
chmod 600 "$CERTS_DIR/local-key.pem" 2>/dev/null || true

# ============================================================================
#  ETAPA 7 — Nginx (Proxy Reverso HTTPS) & Gerenciamento de Portas
# ============================================================================
etapa "ETAPA 7/8 — Instalação e Configuração do Nginx (HTTPS :443)"

apt-get install -y nginx > /dev/null 2>&1
info "Servidor Nginx instalado."

echo ""
info "Conferindo disponibilidade das portas 80 e 443..."

set +e
CONFLITO_80=$(ss -lntp '( sport = :80 )' 2>/dev/null | grep -v "^State" | grep -v "nginx" || true)
CONFLITO_443=$(ss -lntp '( sport = :443 )' 2>/dev/null | grep -v "^State" | grep -v "nginx" || true)
set -e

PIHOLE_DETECTADO=false

if [ -n "$CONFLITO_80" ]; then
    warn "PORTA 80 já em uso por outro serviço no sistema!"
    echo -e "  ${YELLOW}$CONFLITO_80${NC}"

    if echo "$CONFLITO_80" | grep -qi "pihole\|lighttpd\|pihole-FTL" || command -v pihole &> /dev/null; then
        PIHOLE_DETECTADO=true
    fi

    if [ "$PIHOLE_DETECTADO" = true ]; then
        echo ""
        echo -e "  ${YELLOW}${BOLD}⚠  O Pi-hole está utilizando a porta 80!${NC}"
        echo -e "  O Nginx necessita da porta 80 para redirecionamento transparente HTTP -> HTTPS."
        echo -e "  Podemos migrar o painel web do Pi-hole para a porta 8080/8443 agora."
        echo ""
        echo -e "    ${CYAN}[1]${NC} Mover o Pi-hole para porta 8080 (Recomendado)"
        echo -e "    ${CYAN}[2]${NC} Não alterar, resolver manualmente mais tarde"
        echo ""
        ESCOLHA_PIHOLE=""
        if read -t 45 -p "  Sua escolha [1/2]: " ESCOLHA_PIHOLE; then
            :
        else
            ESCOLHA_PIHOLE="1"
        fi

        case "$ESCOLHA_PIHOLE" in
            1)
                info "Alterando porta do Pi-hole para 8080..."
                pihole-FTL --config webserver.port '8080o,8443os,[::]:8080o,[::]:8443os' 2>/dev/null || \
                    sed -i 's/server.port\s*=\s*80/server.port = 8080/' /etc/lighttpd/lighttpd.conf 2>/dev/null || true

                systemctl restart pihole-FTL 2>/dev/null || \
                    systemctl restart lighttpd 2>/dev/null || true

                info "Painel do Pi-hole realocado para: http://$IP_LOCAL:8080/admin"
                ;;
            *)
                warn "Porta 80 mantida. Certifique-se de liberar antes de iniciar o Nginx."
                ;;
        esac
    fi
elif [ -n "$CONFLITO_443" ]; then
    warn "PORTA 443 em uso por outro processo:"
    echo -e "  ${YELLOW}$CONFLITO_443${NC}"
else
    info "Portas 80 e 443 livres para uso do Nginx."
fi

# Criação do arquivo de configuração do Nginx
info "Gerando configuração de proxy reverso..."

tee /etc/nginx/sites-available/blackburn-pulse > /dev/null <<NGINXEOF
# ── Blackburn-Pulse — Proxy Reverso HTTPS ──

server {
    listen 80;
    server_name blackburn-pulse.local blackburn-pulse.lan $IP_LOCAL localhost;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name blackburn-pulse.local blackburn-pulse.lan $IP_LOCAL localhost;

    ssl_certificate     $CERTS_DIR/local-cert.pem;
    ssl_certificate_key $CERTS_DIR/local-key.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINXEOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/blackburn-pulse /etc/nginx/sites-enabled/blackburn-pulse

if nginx -t 2>/dev/null; then
    info "Configuração do Nginx validada com sucesso."
else
    warn "Aviso na sintaxe do Nginx. Verifique detalhes com: sudo nginx -t"
fi

# ============================================================================
#  ETAPA 8 — Mosquitto MQTT, Agendamento Cron e Inicialização Geral
# ============================================================================
etapa "ETAPA 8/8 — Broker MQTT, Backup Automático e Inicialização"

# Instala e configura Mosquitto
if ! command -v mosquitto &> /dev/null; then
    info "Instalando Broker Mosquitto MQTT..."
    apt-get install -y mosquitto mosquitto-clients > /dev/null 2>&1
    systemctl enable mosquitto > /dev/null 2>&1
fi

mkdir -p /etc/mosquitto/conf.d
tee /etc/mosquitto/conf.d/blackburn-pulse.conf > /dev/null <<MQTTEOF
# Configuração Blackburn-Pulse — Permite telemetria remota do ESP32 via Wi-Fi
listener 1883
allow_anonymous true
MQTTEOF

systemctl restart mosquitto 2>/dev/null || true
info "Broker Mosquitto operacional na porta 1883 (acessível pelo ESP32)."

# Configuração do Cron diário para o sincronizador de backup
CRON_LINE="0 1 * * * $VENV_PYTHON $INSTALL_DIR/sincronizador_backup.py > $INSTALL_DIR/backup.log 2>&1"

set +e
(crontab -u "$REAL_USER" -l 2>/dev/null | grep -v "sincronizador_backup" ; echo "$CRON_LINE") | crontab -u "$REAL_USER" -
set -e
info "Backup automático do MicroSD agendado no crontab (diariamente à 01:00)."

mkdir -p "$INSTALL_DIR/historico_energia"
chown "$REAL_USER:$REAL_USER" "$INSTALL_DIR/historico_energia"

# Inicialização e reinício dos serviços
info "Iniciando e sincronizando serviços do sistema..."
systemctl restart blackburn-pulse.service 2>/dev/null || true
systemctl restart nginx 2>/dev/null || true

sleep 2

# ============================================================================
#  RESUMO FINAL & ORIENTAÇÕES DE USO
# ============================================================================
echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║         ⚡  INSTALAÇÃO CONCLUÍDA COM SUCESSO!           ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
separar
echo ""
echo -e "  ${BOLD}📂 Diretório do Projeto:${NC}      $INSTALL_DIR"
echo -e "  ${BOLD}🐍 Ambiente Python:${NC}           Python $PY_VERSAO (.venv ativo)"
echo -e "  ${BOLD}🌐 Painel Web Principal:${NC}      https://blackburn-pulse.lan"
echo -e "  ${BOLD}🌐 Acesso por IP Direto:${NC}      https://$IP_LOCAL"
echo -e "  ${BOLD}🔧 Serviço de Sistema:${NC}        blackburn-pulse.service"
echo -e "  ${BOLD}📡 Broker MQTT Mosquitto:${NC}     $IP_LOCAL:1883"
echo -e "  ${BOLD}💾 Diretório de Backups:${NC}      $INSTALL_DIR/historico_energia/"
echo -e "  ${BOLD}⏰ Sincronização Automática:${NC}  Diária às 01:00 (via Cron)"
echo ""
separar
echo ""
echo -e "  ${BOLD}📋 Status dos Serviços em Execução:${NC}"
echo ""
systemctl is-active --quiet blackburn-pulse.service && \
    info "blackburn-pulse.service : ${GREEN}ONLINE (ativo)${NC}" || \
    warn "blackburn-pulse.service : inicializando (verifique: sudo systemctl status blackburn-pulse)"
systemctl is-active --quiet nginx && \
    info "nginx (proxy HTTPS)     : ${GREEN}ONLINE (ativo)${NC}" || \
    warn "nginx                   : offline (verifique portas 80/443)"
systemctl is-active --quiet mosquitto && \
    info "mosquitto (MQTT broker) : ${GREEN}ONLINE (ativo)${NC}" || \
    warn "mosquitto               : offline"
echo ""
separar
echo ""
echo -e "  ${YELLOW}${BOLD}🔐 COMO INSTALAR O CERTIFICADO SSL NO SEU COMPUTADOR OU CELULAR${NC}"
echo ""
echo -e "  Para acessar ${BOLD}https://blackburn-pulse.lan${NC} com cadeado verde e sem avisos de segurança:"
echo ""
echo -e "  ${CYAN}1. Copie o certificado da Autoridade Raiz para o seu computador:${NC}"
echo -e "     ${BOLD}scp $REAL_USER@$IP_LOCAL:$CERTS_DIR/ca/rootCA.pem .${NC}"
echo ""
echo -e "  ${CYAN}2. Instale o certificado no seu dispositivo:${NC}"
echo -e "     • ${BOLD}Windows:${NC} Dois cliques em rootCA.pem → 'Instalar Certificado' → 'Máquina Local'"
echo -e "                  → 'Colocar todos os certificados no repositório a seguir'"
echo -e "                  → Selecione: ${BOLD}Autoridades de Certificação Raiz Confiáveis${NC}"
echo -e "     • ${BOLD}Android:${NC} Configurações → Segurança → Instalar do armazenamento → Certificado CA"
echo -e "     • ${BOLD}iOS / Mac:${NC} Abra o rootCA.pem → Ajustes → Perfil Baixado → Instalar e Confiar"
echo -e "     • ${BOLD}Linux:${NC}   sudo cp rootCA.pem /usr/local/share/ca-certificates/ && sudo update-ca-certificates"
echo ""
separar
echo ""
echo -e "  ${BOLD}📡 Configuração DNS Local (no Pi-hole ou arquivo hosts):${NC}"
echo -e "     $IP_LOCAL    blackburn-pulse.lan"
echo -e "     $IP_LOCAL    blackburn-pulse.local"
echo ""
separar
echo ""
echo -e "  ${GREEN}${BOLD}⚡ Blackburn-Pulse instalado e pronto para monitoramento!${NC}"
echo ""
