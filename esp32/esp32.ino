#include <ArduinoOTA.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <FS.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <SD.h>
#include <SPI.h>
#include <WebServer.h>
#include <WiFi.h>
#include <time.h>

// --- CONFIGURASCOES PADRAO (FALLBACK INICIAL CASO A FLASH ESTEJA VAZIA) ---
const char *DEFAULT_WIFI_SSID = "NOME_SUA_REDE";
const char *DEFAULT_WIFI_PASS = "SENHA_SUA_REDE";
const char *DEFAULT_MQTT_SERVER = "IP_DO_SEU_SERVIDOR_MQTT";
const int DEFAULT_MQTT_PORT = 1883;

// --- VARIAVEIS DE REDE DINAMICAS (CARREGADAS DA MEMORIA FLASH NVS) ---
String wifiSSID = DEFAULT_WIFI_SSID;
String wifiPassword = DEFAULT_WIFI_PASS;
String mqttServer = DEFAULT_MQTT_SERVER;
int mqttPort = DEFAULT_MQTT_PORT;

// --- CONFIGURACOES DO SENSOR E MEDIDOR ---
const int SENSOR_PIN =
    13; // Pino D13 (GPIO 13) ligado ao S0- com resistor de 10k ao GND
const float IMPULSOS_KWH = 800.0; // Medidor DDS238-4: 800 impulsos = 1 kWh
const int STATUS_LED = 23;        // LED de Status (GPIO 23 / D23)

// --- CONFIGURAÇÃO DO MÓDULO MICRO SD CARD ---
const int SD_CS_PIN = 15;   // Fio Marrom -> GPIO 15 (CS)
const int SD_MOSI_PIN = 17; // Fio Roxo   -> GPIO 17 (MOSI)
const int SD_SCK_PIN = 18;  // Fio Branco -> GPIO 18 (SCK)
const int SD_MISO_PIN = 19; // Fio Cinza  -> GPIO 19 (MISO)
// VCC no 5V, GND no GND

SPIClass spiSD(VSPI);
bool sdDisponivel = false;
unsigned long ultimoSalvamentoSD = 0;
unsigned long ultimoPulsosSalvos = 0;

// --- TOPICOS MQTT ---
const char *MQTT_TOPIC = "home/energia/dds238";
const char *MQTT_TOPIC_STATUS = "home/energia/status";
const char *MQTT_TOPIC_HEARTBEAT = "home/energia/heartbeat";
const char *MQTT_TOPIC_CONFIG_SET = "home/energia/config/set";
const char *MQTT_TOPIC_CONFIG_STATUS = "home/energia/config/status";

// --- VARIAVEIS DE CONTAGEM E SISTEMA ---
volatile unsigned long contadorPulsos = 0;
volatile bool novoPulso = false;
volatile unsigned long ultimoTempoPulso = 0;
volatile unsigned long ultimoDeltaTempoPulso = 0;
volatile float potenciaInstantaneaW = 0.0;
unsigned long ultimoHeartbeat = 0;

// --- MEMÓRIA FLASH NVS (APENAS CONFIGURAÇÕES DE REDE E FALLBACK) ---
Preferences preferences;

// --- CLIENTES DE REDE ---
WiFiClient espClient;
PubSubClient client(espClient);

// --- SERVIDORES DO PORTAL DE RESGATE (ACCESS POINT / CAPTIVE PORTAL) ---
WebServer server(80);
DNSServer dnsServer;
bool modoResgate = false;
bool jaConectouAlgumaVez = false;
bool redeConfiguradaNVS = false;
int tentativasFalhasReconexao = 0;
int ciclosRebootSemWiFi = 0;
const int MAX_REBOOTS_SEM_WIFI =
    3; // Após 3 reboots (~10 min totais), abre o portal de resgate
const unsigned long TEMPO_LIMITE_ROTEADOR_MS =
    600000; // 10 minutos (600s) de tolerância ativa pós-queda de energia para o roteador subir
const unsigned long TEMPO_LIMITE_SEM_CONFIG_MS =
    20000; // 20s se for primeira instalação de fábrica sem rede configurada
unsigned long inicioTentativaWiFi = 0;

// --- PROTÓTIPOS DE FUNÇÕES ---
bool redeEstaConfigurada();
void finalizarModoResgatePorAutoRecuperacao();
void salvarPulsosSD();
void verificarSalvamentoSD();
unsigned long carregarPulsosSD();
bool autoRecuperarBarramentoSD();
void carregarUltimaDataNVS();
void salvarUltimaDataNVS(String novaData);
void configurarNTP();
String obterDataAtual();
String obterHoraAtual();
void configurarRotasWebServer();
void tratarListarArquivosSD();
void tratarBaixarArquivoSD();
void tratarApagarArquivoSD();
void tratarRestaurarPulsosSD();

// --- INTERRUPCAO DE HARDWARE (Executada na RAM) ---
void IRAM_ATTR tratarPulso() {
  unsigned long agora = millis();
  // Debounce de 250ms: O pulso físico S0 do DDS238 dura de 80ms a 100ms.
  // Um debounce menor que 100ms causava leitura dupla no repique de descida do
  // pulso (a 90ms), gerando delta de 90ms e o falso pico de 50.000 Watts!
  // Como 15.000W residenciais geram pulsos a cada 300ms, 250ms é o ponto
  // perfeito.
  if (agora - ultimoTempoPulso > 250) {
    if (ultimoTempoPulso > 0) {
      ultimoDeltaTempoPulso = agora - ultimoTempoPulso;
    }
    contadorPulsos = contadorPulsos + 1;
    novoPulso = true;
    ultimoTempoPulso = agora;
  }
}

// Retorna a potência ativa atual com decaimento suave se não houver pulsos
// recentes
float calcularPotenciaAtual() {
  if (ultimoTempoPulso == 0)
    return 0.0f;
  unsigned long tempoSemPulso = millis() - ultimoTempoPulso;

  // Decaimento imediato: se passou mais tempo que o último pulso, a potência já
  // caiu
  if (tempoSemPulso > ultimoDeltaTempoPulso && tempoSemPulso > 2000) {
    return 4500000.0f / (float)tempoSemPulso;
  }
  return potenciaInstantaneaW;
}

// --- FUNCAO PARA EXTRAIR VALORES DE UM JSON SIMPLES SEM BIBLIOTECAS EXTERNAS
// ---
String extrairValorJson(String json, String chave) {
  String busca = "\"" + chave + "\"";
  int idx = json.indexOf(busca);
  if (idx < 0)
    return "";
  int doisPontos = json.indexOf(':', idx + busca.length());
  if (doisPontos < 0)
    return "";
  int aspasInicio = json.indexOf('"', doisPontos);
  if (aspasInicio < 0)
    return "";
  int aspasFim = json.indexOf('"', aspasInicio + 1);
  if (aspasFim < 0)
    return "";
  return json.substring(aspasInicio + 1, aspasFim);
}

// --- VALIDAÇÃO SE O ESP32 JÁ POSSUI REDE CONFIGURADA PELO USUÁRIO ---
bool redeEstaConfigurada() {
  return (wifiSSID.length() > 0 &&
          wifiSSID != DEFAULT_WIFI_SSID &&
          wifiSSID != "NOME_SUA_REDE");
}

// --- LEITURA E GRAVACAO DE CONFIGURACOES NA MEMORIA NVS FLASH ---
void carregarConfiguracoesNVS() {
  Preferences p;
  p.begin("config", true);
  wifiSSID = p.getString("ssid", DEFAULT_WIFI_SSID);
  wifiPassword = p.getString("pass", DEFAULT_WIFI_PASS);
  mqttServer = p.getString("mqtt", DEFAULT_MQTT_SERVER);
  mqttPort = p.getInt("port", DEFAULT_MQTT_PORT);
  redeConfiguradaNVS = p.getBool("wifi_ok", false);
  p.end();

  if (redeEstaConfigurada()) {
    redeConfiguradaNVS = true;
    jaConectouAlgumaVez = true;
  }

  Serial.println("\n[CONFIG NVS] Configuracoes de rede carregadas:");
  Serial.println("  -> Wi-Fi SSID: " + wifiSSID);
  Serial.println("  -> MQTT Broker: " + mqttServer + ":" + String(mqttPort));
  Serial.println("  -> Rede Configurada Previamente: " + String(redeConfiguradaNVS ? "SIM (Residencial)" : "NAO (Primeiro Uso)"));
}

void salvarConfiguracoesNVS(String novoSsid, String novaPass, String novoMqtt) {
  Preferences p;
  p.begin("config", false);
  if (novoSsid.length() > 0) {
    p.putString("ssid", novoSsid);
    wifiSSID = novoSsid;
    p.putBool("wifi_ok", true);
    redeConfiguradaNVS = true;
    jaConectouAlgumaVez = true;
  }
  if (novaPass.length() > 0) {
    p.putString("pass", novaPass);
    wifiPassword = novaPass;
  }
  if (novoMqtt.length() > 0) {
    p.putString("mqtt", novoMqtt);
    mqttServer = novoMqtt;
  }
  p.end();
  Serial.println("[CONFIG NVS] Novas configurações salvas com sucesso!");
}

// --- INICIALIZACAO DA ATUALIZACAO SEM FIO (ArduinoOTA) ---
void iniciarOTA() {
  ArduinoOTA.setHostname("ESP32-Energia");
  ArduinoOTA.onStart([]() {
    Serial.println("\n[OTA] Iniciando atualizacao sem fio do firmware...");
  });
  ArduinoOTA.onEnd([]() {
    Serial.println("\n[OTA] Atualizacao concluida com sucesso! Reiniciando...");
  });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    Serial.printf("[OTA] Progresso: %u%%\r", (progress / (total / 100)));
  });
  ArduinoOTA.onError(
      [](ota_error_t error) { Serial.printf("[OTA] Erro [%u]\n", error); });
  ArduinoOTA.begin();
  Serial.println("[OTA] Pronto! Voce pode atualizar o codigo sem fio "
                 "diretamente pela rede.");
}

// --- PUBLICAO DE MEDICAO COM RETAIN = TRUE ---
void publicarMedicao() {
  float consumoKWh = contadorPulsos / IMPULSOS_KWH;
  float potW = calcularPotenciaAtual();
  String payload = "{\"pulsos\":" + String(contadorPulsos) +
                   ",\"kwh\":" + String(consumoKWh, 3) +
                   ",\"potencia_w\":" + String(potW, 1) + "}";

  Serial.print("[MQTT PUBLISH] -> ");
  Serial.println(payload);

  client.publish(MQTT_TOPIC, payload.c_str(), true);
}

// --- RECEBIMENTO DE COMANDOS MQTT REMOTOS DO APP ---
void callbackMQTT(char *topic, byte *payload, unsigned int length) {
  String topicoRecebido = String(topic);
  String mensagem = "";
  for (unsigned int i = 0; i < length; i++) {
    mensagem += (char)payload[i];
  }

  Serial.print("[MQTT RECEBIDO] Tópico: " + topicoRecebido + " | Conteúdo: ");
  Serial.println(mensagem);

  // Alteração remota de configurações de Wi-Fi ou Broker MQTT
  if (topicoRecebido == MQTT_TOPIC_CONFIG_SET) {
    String novoSsid = extrairValorJson(mensagem, "ssid");
    String novaPass = extrairValorJson(mensagem, "password");
    String novoMqtt = extrairValorJson(mensagem, "mqtt_server");

    salvarConfiguracoesNVS(novoSsid, novaPass, novoMqtt);

    String resp = "{\"status\":\"sucesso\",\"mensagem\":\"Configuracoes salvas "
                  "no ESP32! Reiniciando na nova rede...\"}";
    client.publish(MQTT_TOPIC_CONFIG_STATUS, resp.c_str(), true);

    Serial.println("[ESP32] Reiniciando em 2 segundos para aplicar as novas "
                   "configurações...");
    delay(2000);
    ESP.restart();
  }
}

// --- PORTAL DE RESGATE: PONTO DE ACESSO WI-FI + PORTAL CATIVO AUTO-RECUPERÁVEL ---
void iniciarModoResgate() {
  if (modoResgate) return;
  modoResgate = true;
  Serial.println("\n========================================================");
  Serial.println("[PORTAL DE RESGATE] Roteador principal não respondeu.");
  Serial.println("[PORTAL DE RESGATE] Ativando modo dual (AP + Station)...");
  Serial.println("[PORTAL DE RESGATE] Criando rede Wi-Fi própria: Blackburn-Config");
  Serial.println("[PORTAL DE RESGATE] O ESP32 continuará sondando o Wi-Fi residencial em background a cada 30s!");
  Serial.println("========================================================");

  // Modo DUAL (WIFI_AP_STA): Mantém o AP aberto no celular e continua sondando a rede residencial
  WiFi.mode(WIFI_AP_STA);
  IPAddress apIP(192, 168, 4, 1);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255, 255, 255, 0));
  WiFi.softAP("Blackburn-Config"); // Rede aberta sem senha para facilitar

  dnsServer.start(53, "*", apIP); // Captive portal redirect

  configurarRotasWebServer();

  Serial.println("[PORTAL DE RESGATE] Pronto! Acesse no celular: Wi-Fi 'Blackburn-Config' -> http://192.168.4.1");
}

// --- FINALIZAÇÃO DO MODO RESGATE POR AUTO-RECUPERAÇÃO AUTOMÁTICA ---
void finalizarModoResgatePorAutoRecuperacao() {
  Serial.println("\n========================================================");
  Serial.println("[AUTO-RECUPERAÇÃO] ✓ Rede Wi-Fi residencial restabelecida e conectada!");
  Serial.println("[AUTO-RECUPERAÇÃO] Encerrando Portal de Resgate e desligando SoftAP...");
  Serial.println("========================================================");

  dnsServer.stop();
  WiFi.softAPdisconnect(true);
  delay(100);
  WiFi.mode(WIFI_STA);

  modoResgate = false;
  tentativasFalhasReconexao = 0;
  ciclosRebootSemWiFi = 0;
  preferences.begin("energia", false);
  preferences.putInt("reboots_wifi", 0);
  preferences.end();

  digitalWrite(STATUS_LED, HIGH);
  Serial.print("[Wi-Fi] IP local do ESP32: ");
  Serial.println(WiFi.localIP());

  iniciarOTA();
  if (MDNS.begin("esp32-energia")) {
    MDNS.addService("http", "tcp", 80);
    Serial.println("[mDNS] Ativo: http://esp32-energia.local");
  }
  configurarNTP();
  configurarRotasWebServer();

  Serial.println("[AUTO-RECUPERAÇÃO] Operação normal retomada com sucesso!");
}

// --- CONEXÃO E RECONEXÃO WI-FI ROBUSTA (SUPORTE A REDES OCULTAS E QUEDAS DE ENERGIA) ---
void conectarWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.print("[Wi-Fi] Conectando ativamente à rede: ");
  Serial.println(wifiSSID);

  if (!modoResgate) {
    WiFi.mode(WIFI_STA);
  }
  WiFi.disconnect();
  delay(100);

  // O 5º parâmetro (true) força o ESP32 a emitir Probe Requests ativos,
  // permitindo encontrar e reconectar instantaneamente em redes com SSID oculto.
  WiFi.begin(wifiSSID.c_str(), wifiPassword.c_str(), 0, NULL, true);

  unsigned long inicioTentativa = millis();
  while (WiFi.status() != WL_CONNECTED &&
         (millis() - inicioTentativa < 12000)) {
    digitalWrite(
        STATUS_LED,
        !digitalRead(STATUS_LED)); // Pisca rápido (200ms) procurando Wi-Fi
    delay(200);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    jaConectouAlgumaVez = true;
    redeConfiguradaNVS = true;
    modoResgate = false;
    tentativasFalhasReconexao = 0;

    // Salva na NVS que temos rede configurada com sucesso
    Preferences p;
    p.begin("config", false);
    p.putBool("wifi_ok", true);
    p.end();

    if (ciclosRebootSemWiFi > 0) {
      ciclosRebootSemWiFi = 0;
      preferences.begin("energia", false);
      preferences.putInt("reboots_wifi", 0);
      preferences.end();
    }
    digitalWrite(STATUS_LED, HIGH);
    Serial.println("\n[Wi-Fi] Conectado com sucesso!");
    Serial.print("[Wi-Fi] IP local do ESP32: ");
    Serial.println(WiFi.localIP());
    iniciarOTA();
    if (MDNS.begin("esp32-energia")) {
      MDNS.addService("http", "tcp", 80);
      Serial.println("[mDNS] Ativo: http://esp32-energia.local");
    }
    configurarNTP();
    configurarRotasWebServer();
    Serial.println("[WEBSERVER] Servidor HTTP de Backup e Sincronizacao ativo na porta 80!");
  } else {
    Serial.println("\n[Wi-Fi] Roteador ainda não respondeu neste ciclo.");
    tentativasFalhasReconexao++;

    unsigned long tempoDecorrido = millis() - inicioTentativaWiFi;
    unsigned long tempoLimite = redeEstaConfigurada() ? TEMPO_LIMITE_ROTEADOR_MS : TEMPO_LIMITE_SEM_CONFIG_MS;

    // Tolerância alta para queda de luz: enquanto estiver dentro do tempo limite (10 min),
    // o ESP32 continua tentando pacientemente no modo Station.
    // Após os 10 minutos, abre o Portal de Resgate, mas em modo DUAL (AP+STA) para continuar tentando em background!
    if (!modoResgate && (tempoDecorrido > tempoLimite)) {
      Serial.print("\n[Wi-Fi] Tempo de espera esgotado (");
      Serial.print(tempoDecorrido / 1000);
      Serial.println("s). Ativando Portal de Resgate com auto-recuperacao ativa...");
      iniciarModoResgate();
      return;
    }

    // Se já tentou muitas vezes no modo Station e o rádio travou, executa um ciclo de reboot limpo
    if (redeEstaConfigurada() && tentativasFalhasReconexao >= 12) {
      ciclosRebootSemWiFi++;
      tentativasFalhasReconexao = 0;

      // Se já tentou reiniciar 3 vezes completas (~10 minutos totais sem o roteador voltar),
      // ativa o Portal de Resgate com auto-recuperação contínua
      if (ciclosRebootSemWiFi >= MAX_REBOOTS_SEM_WIFI) {
        Serial.println("\n[Wi-Fi] 3 ciclos de reinicialização sem sucesso. Ativando Portal de Resgate com auto-recuperação...");
        ciclosRebootSemWiFi = 0;
        preferences.begin("energia", false);
        preferences.putInt("reboots_wifi", 0);
        preferences.end();
        if (!modoResgate) {
          iniciarModoResgate();
        }
        return;
      }

      Serial.print("\n[Wi-Fi] Roteador fora do ar (Ciclo ");
      Serial.print(ciclosRebootSemWiFi);
      Serial.print(" de ");
      Serial.print(MAX_REBOOTS_SEM_WIFI);
      Serial.println("). Salvando contagem e reiniciando o ESP32 para limpar o rádio...");

      salvarPulsosSD();
      preferences.begin("energia", false);
      preferences.putInt("reboots_wifi", ciclosRebootSemWiFi);
      preferences.end();
      delay(500);
      ESP.restart();
    }
  }
}

// --- RECONEXÃO MQTT AUTOMÁTICA ---
void reconectarMQTT() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  while (!client.connected()) {
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("[MQTT] Wi-Fi desconectado durante tentativa MQTT.");
      return;
    }

    digitalWrite(STATUS_LED, LOW);
    Serial.print("Conectando ao Broker MQTT (" + mqttServer + ")...");
    String clientId = "ESP32_Energia_";
    clientId += String(random(0xffff), HEX);

    client.setServer(mqttServer.c_str(), mqttPort);
    client.setCallback(callbackMQTT);
    client.setKeepAlive(45); // KeepAlive de 45 segundos para máxima
                             // estabilidade em Wi-Fi doméstico

    if (client.connect(clientId.c_str(), MQTT_TOPIC_STATUS, 1, true,
                       "offline")) {
      Serial.println(" Conectado!");
      digitalWrite(STATUS_LED, HIGH);

      // Status online
      client.publish(MQTT_TOPIC_STATUS, "online", true);

      // Inscreve-se no tópico de configuração remota
      client.subscribe(MQTT_TOPIC_CONFIG_SET);

      // Publica imediatamente medição acumulada
      publicarMedicao();

      // Envia heartbeat inicial com dados de telemetria
      float consumoKWh = contadorPulsos / IMPULSOS_KWH;
      float potW = calcularPotenciaAtual();
      String hb = "{\"status\":\"online\",\"ip\":\"" +
                  WiFi.localIP().toString() +
                  "\",\"rssi\":" + String(WiFi.RSSI()) +
                  ",\"uptime\":" + String(millis() / 1000) +
                  ",\"pulsos\":" + String(contadorPulsos) +
                  ",\"kwh\":" + String(consumoKWh, 3) +
                  ",\"potencia_w\":" + String(potW, 1) + "}";
      client.publish(MQTT_TOPIC_HEARTBEAT, hb.c_str());
    } else {
      Serial.print(" Falhou (rc=");
      Serial.print(client.state());
      Serial.println("). Tentando novamente em 5 segundos...");
      for (int i = 0; i < 10; i++) {
        if (WiFi.status() != WL_CONNECTED)
          break;
        digitalWrite(STATUS_LED,
                     !digitalRead(STATUS_LED)); // Pisca lento (500ms)
        delay(500);
      }
    }
  }
}

// --- RESILIÊNCIA DE DATA VIA NVS (APENAS 1 ESCRITA/DIA) ---
String ultimaDataValidaNVS = "";

void carregarUltimaDataNVS() {
  preferences.begin("energia", true); // somente-leitura
  ultimaDataValidaNVS = preferences.getString("ult_data", "2026-01-01");
  preferences.end();
  Serial.println("[NVS DATA] Ultima data valida recuperada da Flash: " +
                 ultimaDataValidaNVS);
}

void salvarUltimaDataNVS(String novaData) {
  if (novaData.length() == 10 && novaData != ultimaDataValidaNVS) {
    preferences.begin("energia", false);
    preferences.putString("ult_data", novaData);
    preferences.end();
    ultimaDataValidaNVS = novaData;
    Serial.println(
        "[NVS DATA] Nova data diaria persistida na Flash (1 escrita/dia): " +
        novaData);
  }
}

// --- SINCRONIZAÇÃO DE TEMPO E HORA (NTP - UTC-3) COM FALLBACK DE DATA ---
bool ntpSincronizado = false;

void configurarNTP() {
  configTime(-3 * 3600, 0, "pool.ntp.org", "a.st1.ntp.br", "time.google.com");
  Serial.println("[NTP] Sincronizacao de horario UTC-3 (Brasilia) iniciada.");
}

String obterDataAtual() {
  struct tm timeinfo;
  if (getLocalTime(&timeinfo, 50)) {
    ntpSincronizado = true;
    char buf[16];
    strftime(buf, sizeof(buf), "%Y-%m-%d", &timeinfo);
    String dataStr = String(buf);
    if (dataStr != ultimaDataValidaNVS) {
      salvarUltimaDataNVS(dataStr);
    }
    return dataStr;
  }
  // Se estiver sem internet / NTP falhar, recorre à última data válida da NVS
  if (ultimaDataValidaNVS.length() == 10) {
    return ultimaDataValidaNVS;
  }
  return "";
}

String obterHoraAtual() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo, 50)) {
    return "00:00:00";
  }
  char buf[16];
  strftime(buf, sizeof(buf), "%H:%M:%S", &timeinfo);
  return String(buf);
}

// --- AUTOCORREÇÃO E AUTO CHECK DISK DO MICRO SD ---
unsigned long ultimoTentativaReconectarSD = 0;

bool autoRecuperarBarramentoSD() {
  Serial.println("\n[SD AUTO-CHECK] Executando rotina de auto-correcao e reset "
                 "do barramento SPI...");
  SD.end();
  delay(50);

  // Pulso de toggle no Chip Select (CS) para destravar controlador do SD
  pinMode(SD_CS_PIN, OUTPUT);
  digitalWrite(SD_CS_PIN, HIGH);
  delay(10);
  digitalWrite(SD_CS_PIN, LOW);
  delay(10);
  digitalWrite(SD_CS_PIN, HIGH);
  delay(50);

  // Reinicia o barramento SPI
  spiSD.begin(SD_SCK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);
  delay(80);

  if (SD.begin(SD_CS_PIN, spiSD)) {
    Serial.println("[SD AUTO-CHECK] ✓ Cartao Micro SD recuperado e remontado "
                   "com sucesso!");
    sdDisponivel = true;
    return true;
  } else {
    Serial.println("[SD AUTO-CHECK] ✗ Falha na remontagem do SD. Alternando "
                   "para buffer NVS de contingencia.");
    sdDisponivel = false;
    return false;
  }
}

// --- GRAVAÇÃO DA CONTAGEM DE PULSOS E ROTAÇÃO DIÁRIA NO CARTÃO MICRO SD ---
void salvarPulsosSD() {
  if (sdDisponivel) {
    // 1. Atualização atômica do totalizador rápido (/pulsos.txt) para boot
    // instantâneo
    File f = SD.open("/pulsos.tmp", FILE_WRITE);
    if (!f) {
      Serial.println("[SD CARD] Falha ao abrir /pulsos.tmp. Disparando Auto "
                     "Check Disk...");
      if (autoRecuperarBarramentoSD()) {
        f = SD.open("/pulsos.tmp", FILE_WRITE);
      }
    }

    if (f) {
      f.println(contadorPulsos);
      f.flush();
      f.close();

      if (SD.exists("/pulsos.txt")) {
        SD.remove("/pulsos.txt");
      }
      SD.rename("/pulsos.tmp", "/pulsos.txt");
      ultimoPulsosSalvos = contadorPulsos;

      Serial.print("[SD CARD] Gravacao atomica realizada! Total em disco: ");
      Serial.println(contadorPulsos);

      // 2. Registro no arquivo diário rotacionado por data (YYYY-MM-DD.txt)
      String dataHoje = obterDataAtual();
      if (dataHoje.length() == 10) {
        String caminhoDiario = "/" + dataHoje + ".txt";
        bool novoArquivo = !SD.exists(caminhoDiario);
        File fDiario = SD.open(caminhoDiario, FILE_APPEND);
        if (fDiario) {
          if (novoArquivo || fDiario.size() == 0) {
            fDiario.println("data_hora,pulsos,kwh,potencia_w");
          }
          String horaHoje = obterHoraAtual();
          float kwh = (float)contadorPulsos / IMPULSOS_KWH;
          float pot = calcularPotenciaAtual();
          fDiario.printf("%s %s,%lu,%.3f,%.1f\n", dataHoje.c_str(),
                         horaHoje.c_str(), contadorPulsos, kwh, pot);
          fDiario.flush();
          fDiario.close();
          Serial.println("[SD DIARIO] Registro adicionado em " + caminhoDiario);
        } else {
          Serial.println(
              "[SD DIARIO] Erro ao abrir arquivo diario para append: " +
              caminhoDiario);
        }
      }
      return;
    } else {
      Serial.println(
          "[SD CARD] Falha persistente ao abrir /pulsos.tmp para escrita!");
    }
  }

  // Fallback de segurança na Flash NVS se o SD falhar ou não estiver inserido
  preferences.begin("energia", false);
  preferences.putULong("pulsos", contadorPulsos);
  preferences.end();
  ultimoPulsosSalvos = contadorPulsos;
  Serial.print("[FALLBACK NVS] Gravacao de emergencia na Flash: ");
  Serial.println(contadorPulsos);
}

// --- ENDPOINTS HTTP DO WEBSERVER PARA GERENCIAMENTO E BACKUP DO SD ---

// GET /listar -> Retorna JSON com lista de arquivos .txt válidos no SD
void tratarListarArquivosSD() {
  if (!sdDisponivel) {
    server.send(503, "application/json",
                "{\"erro\":\"Cartao Micro SD indisponivel\"}");
    return;
  }

  File root = SD.open("/");
  if (!root || !root.isDirectory()) {
    server.send(500, "application/json",
                "{\"erro\":\"Falha ao abrir diretorio raiz do SD\"}");
    return;
  }

  String json = "[";
  bool primeiro = true;

  File file = root.openNextFile();
  while (file) {
    if (!file.isDirectory()) {
      String nome = String(file.name());
      if (nome.startsWith("/")) {
        nome = nome.substring(1);
      }
      // Lista arquivos .txt válidos (arquivos diários YYYY-MM-DD.txt e
      // pulsos.txt)
      if (nome.endsWith(".txt") && nome != "pulsos.tmp") {
        if (!primeiro)
          json += ",";
        json += "{\"arquivo\":\"" + nome +
                "\",\"tamanho\":" + String(file.size()) + "}";
        primeiro = false;
      }
    }
    file = root.openNextFile();
  }
  json += "]";
  server.send(200, "application/json", json);
}

// GET /baixar?arq=<nome_do_arquivo> -> Faz stream direto do arquivo
void tratarBaixarArquivoSD() {
  if (!sdDisponivel) {
    server.send(503, "text/plain", "Cartao Micro SD indisponivel");
    return;
  }

  if (!server.hasArg("arq")) {
    server.send(400, "text/plain", "Parametro 'arq' ausente");
    return;
  }

  String arq = server.arg("arq");
  arq.trim();
  if (arq.length() == 0 || arq.indexOf("..") != -1) {
    server.send(400, "text/plain",
                "Nome de arquivo invalido ou tentativa de path traversal");
    return;
  }

  String caminho = arq.startsWith("/") ? arq : "/" + arq;

  if (!SD.exists(caminho)) {
    server.send(404, "text/plain", "Arquivo nao encontrado no SD");
    return;
  }

  File f = SD.open(caminho, FILE_READ);
  if (!f) {
    server.send(500, "text/plain", "Falha ao abrir o arquivo para leitura");
    return;
  }

  server.sendHeader("Content-Disposition",
                    "attachment; filename=\"" + arq + "\"");
  server.streamFile(f, "text/plain");
  f.close();
}

// POST /apagar?arq=<nome_do_arquivo> -> Exclui arquivo especificado com
// segurança
void tratarApagarArquivoSD() {
  if (!sdDisponivel) {
    server.send(503, "application/json",
                "{\"erro\":\"Cartao Micro SD indisponivel\"}");
    return;
  }

  if (!server.hasArg("arq")) {
    server.send(400, "application/json",
                "{\"erro\":\"Parametro 'arq' ausente\"}");
    return;
  }

  String arq = server.arg("arq");
  arq.trim();
  if (arq.length() == 0 || arq.indexOf("..") != -1) {
    server.send(400, "application/json",
                "{\"erro\":\"Nome de arquivo invalido\"}");
    return;
  }

  String caminho = arq.startsWith("/") ? arq : "/" + arq;

  // Proteção vital: nunca apagar os arquivos do totalizador
  if (caminho == "/pulsos.txt" || caminho == "/pulsos.tmp") {
    server.send(403, "application/json",
                "{\"erro\":\"Proibido apagar arquivo de contagem ativa\"}");
    return;
  }

  if (!SD.exists(caminho)) {
    server.send(404, "application/json",
                "{\"erro\":\"Arquivo nao encontrado\"}");
    return;
  }

  if (SD.remove(caminho)) {
    Serial.println("[SD BACKUP] Arquivo excluido com sucesso: " + caminho);
    server.send(200, "application/json",
                "{\"status\":\"sucesso\",\"arquivo\":\"" + arq + "\"}");
  } else {
    server.send(500, "application/json",
                "{\"erro\":\"Falha ao excluir arquivo do SD\"}");
  }
}

// POST /restaurar-pulsos?val=<novo_valor> -> Restaura a contagem no ESP32 e
// grava no SD
void tratarRestaurarPulsosSD() {
  if (!server.hasArg("val")) {
    server.send(400, "application/json",
                "{\"erro\":\"Parametro 'val' ausente\"}");
    return;
  }
  unsigned long novosPulsos = strtoul(server.arg("val").c_str(), NULL, 10);
  contadorPulsos = novosPulsos;
  salvarPulsosSD();
  Serial.print("[RESTAURAÇÃO MANUAL] Contagem restaurada via HTTP: ");
  Serial.println(novosPulsos);
  server.send(200, "application/json",
              "{\"status\":\"sucesso\",\"pulsos\":" + String(novosPulsos) +
                  "}");
}

// Registra rotas do WebServer para uso em modo Station e Portal de Resgate
void configurarRotasWebServer() {
  server.on("/", HTTP_GET, []() {
    if (modoResgate) {
      String html =
          "<!DOCTYPE html><html><head><meta charset='utf-8'><meta "
          "name='viewport' content='width=device-width, "
          "initial-scale=1'><title>Blackburn-Pulse Config</title>"
          "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe "
          "UI,Roboto,sans-serif;background:#0f172a;color:#f8fafc;padding:20px;"
          "margin:0;}.card{background:#1e293b;border-radius:12px;padding:24px;"
          "max-width:400px;margin:0 auto;border:1px solid "
          "#334155;}h2{color:#10b981;margin-top:0;}label{font-size:0.85rem;color:"
          "#94a3b8;display:block;margin-top:14px;margin-bottom:4px;}input{width:"
          "100%;box-sizing:border-box;padding:10px;border-radius:6px;border:1px "
          "solid "
          "#475569;background:#0f172a;color:#fff;font-size:1rem;}button{width:"
          "100%;margin-top:20px;padding:12px;background:#10b981;border:none;"
          "border-radius:6px;color:#fff;font-size:1rem;font-weight:bold;cursor:"
          "pointer;}.aviso{font-size:0.8rem;color:#38bdf8;margin-top:14px;text-align:center;line-height:1.4;}"
          "</style></head><body><div class='card'><h2>⚡ "
          "Blackburn-Pulse Configuração</h2><p "
          "style='font-size:0.85rem;color:#94a3b8;'>Rede de emergência do "
          "medidor. Digite os dados da sua rede Wi-Fi:</p><form action='/salvar' "
          "method='POST'><label>Nome do Wi-Fi (SSID):</label><input type='text' "
          "name='ssid' value='" +
          wifiSSID +
          "' required><label>Senha do Wi-Fi:</label><input type='password' "
          "name='pass' placeholder='Nova senha da rede'><label>IP do Broker MQTT "
          "(Raspberry Pi):</label><input type='text' name='mqtt' value='" +
          mqttServer +
          "' required><button type='submit'>Salvar e Reiniciar "
          "ESP32</button></form><div class='aviso'>🔄 O medidor está tentando reconectar ao seu Wi-Fi residencial automaticamente em segundo plano. Assim que o roteador responder, o portal fechará sozinho.</div></div></body></html>";
      server.send(200, "text/html", html);
    } else {
      String html = "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Blackburn-Pulse</title>"
                    "<style>body{font-family:sans-serif;background:#0f172a;color:#10b981;text-align:center;padding:50px;}</style></head>"
                    "<body><h2>⚡ Blackburn-Pulse Online</h2><p style='color:#94a3b8;'>ESP32 operando normalmente na rede local.</p>"
                    "<p><a style='color:#38bdf8;' href='/status-sd'>Ver Status do Cartão Micro SD</a> | <a style='color:#38bdf8;' href='/listar'>Listar Arquivos Diários</a></p></body></html>";
      server.send(200, "text/html", html);
    }
  });

  server.on("/salvar", HTTP_POST, []() {
    String novoS = server.arg("ssid");
    String novaP = server.arg("pass");
    String novoM = server.arg("mqtt");
    salvarConfiguracoesNVS(novoS, novaP, novoM);
    String resp =
        "<html><body "
        "style='background:#0f172a;color:#10b981;font-family:sans-serif;text-"
        "align:center;padding:40px;'><h2>Configurações Salvas!</h2><p "
        "style='color:#fff;'>O ESP32 está reiniciando para conectar na sua "
        "rede Wi-Fi...</p></body></html>";
    server.send(200, "text/html", resp);
    delay(1500);
    ESP.restart();
  });

  server.on("/listar", HTTP_GET, tratarListarArquivosSD);
  server.on("/baixar", HTTP_GET, tratarBaixarArquivoSD);
  server.on("/apagar", HTTP_POST, tratarApagarArquivoSD);
  server.on("/restaurar-pulsos", HTTP_POST, tratarRestaurarPulsosSD);

  server.on("/status-sd", HTTP_GET, []() {
    String dataHoje = obterDataAtual();
    String horaHoje = obterHoraAtual();
    uint64_t totalBytes = SD.totalBytes();
    uint64_t usedBytes = SD.usedBytes();
    String json =
        "{\"sd_disponivel\":" + String(sdDisponivel ? "true" : "false") +
        ",\"data_ntp\":\"" + dataHoje + " " + horaHoje + "\"" +
        ",\"total_mb\":" + String((float)totalBytes / (1024.0 * 1024.0), 2) +
        ",\"usado_mb\":" + String((float)usedBytes / (1024.0 * 1024.0), 2) +
        ",\"pulsos_ativos\":" + String(contadorPulsos) + "}";
    server.send(200, "application/json", json);
  });

  server.onNotFound([]() {
    if (modoResgate) {
      server.sendHeader("Location", "http://192.168.4.1/", true);
      server.send(302, "text/plain", "");
    } else {
      server.send(404, "text/plain", "Endpoint nao encontrado no Blackburn-Pulse");
    }
  });

  server.begin();
}

void verificarSalvamentoSD() {
  if (millis() - ultimoSalvamentoSD > 30000) { // A cada 30 segundos
    ultimoSalvamentoSD = millis();

    // Se o SD estava indisponivel, tenta restabelecer periodicamente a cada 60s
    // evitando travamentos
    if (!sdDisponivel && (millis() - ultimoTentativaReconectarSD > 60000)) {
      ultimoTentativaReconectarSD = millis();
      if (autoRecuperarBarramentoSD()) {
        salvarPulsosSD();
        return;
      }
    }

    if (contadorPulsos != ultimoPulsosSalvos) {
      salvarPulsosSD();
    }
  }
}

// Mantém compatibilidade com qualquer chamada existente
void verificarSalvamentoFlash() { verificarSalvamentoSD(); }

unsigned long carregarPulsosSD() {
  unsigned long pulsosLidos = 0;
  bool leuDoSD = false;

  if (sdDisponivel) {
    if (SD.exists("/pulsos.txt")) {
      File f = SD.open("/pulsos.txt", FILE_READ);
      if (f) {
        String linha = f.readStringUntil('\n');
        linha.trim();
        if (linha.length() > 0) {
          pulsosLidos = strtoul(linha.c_str(), NULL, 10);
          leuDoSD = true;
          Serial.print("[SD CARD] Contagem restaurada do /pulsos.txt: ");
          Serial.println(pulsosLidos);
        }
        f.close();
      }
    }
  }

  // Se o SD for novo (primeira vez) ou não tiver o arquivo,
  // migra automaticamente a contagem existente da NVS Flash para o SD!
  if (!leuDoSD) {
    preferences.begin("energia", true); // Modo somente-leitura
    pulsosLidos = preferences.getULong("pulsos", 0);
    preferences.end();
    Serial.print("[MIGRAÇÃO] Contagem migrada da NVS Flash: ");
    Serial.println(pulsosLidos);

    if (sdDisponivel) {
      contadorPulsos = pulsosLidos;
      salvarPulsosSD();
    }
  }

  return pulsosLidos;
}

void setup() {
  Serial.begin(115200);

  // Configuração do LED de Status
  pinMode(STATUS_LED, OUTPUT);
  digitalWrite(STATUS_LED, LOW);

  // Pino do sensor com PULL-DOWN e interrupção
  pinMode(SENSOR_PIN, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(SENSOR_PIN), tratarPulso, RISING);

  // Restaura ultima data valida da NVS para resiliencia offline caso inicie sem
  // rede
  carregarUltimaDataNVS();

  // --- INICIALIZAÇÃO DO CARTÃO MICRO SD COM AUTO CHECK DISK ---
  pinMode(SD_CS_PIN, OUTPUT);
  digitalWrite(SD_CS_PIN, HIGH);
  spiSD.begin(SD_SCK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);

  if (!SD.begin(SD_CS_PIN, spiSD)) {
    Serial.println("[SD CARD] Falha inicial ao montar SD. Iniciando Auto Check "
                   "Disk e recuperacao...");
    autoRecuperarBarramentoSD();
  } else {
    Serial.println("[SD CARD] Cartão Micro SD montado com sucesso!");
    sdDisponivel = true;
  }

  // --- RESTAURAÇÃO DA CONTAGEM DE ENERGIA (SD com Migração/Fallback NVS) ---
  contadorPulsos = carregarPulsosSD();
  ultimoPulsosSalvos = contadorPulsos;

  preferences.begin("energia", true); // Modo somente-leitura
  ciclosRebootSemWiFi = preferences.getInt("reboots_wifi", 0);
  preferences.end();

  // Se já tinha reboots acumulados ou se o SSID é válido, já temos rede configurada
  if (ciclosRebootSemWiFi > 0 || redeEstaConfigurada()) {
    jaConectouAlgumaVez = true;
    redeConfiguradaNVS = true;
  }

  Serial.println("\n==========================================");
  Serial.print("[SISTEMA] Contagem ativa restaurada: ");
  Serial.print(contadorPulsos);
  Serial.println(" pulsos");
  Serial.println("==========================================");

  // Carrega configurações de Wi-Fi e MQTT salvas
  carregarConfiguracoesNVS();

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  WiFi.setSleep(false); // Desativa Modem Sleep (evita desconexões por Group
                        // Rekeying do roteador a cada 30min)

  inicioTentativaWiFi = millis();
  conectarWiFi();
}

void loop() {
  // 1. SE ESTIVER NO PORTAL DE RESGATE (Blackburn-Config)
  if (modoResgate) {
    dnsServer.processNextRequest();
    server.handleClient();

    // Pisca LED indicando modo de configuração (duas piscadas rápidas)
    static unsigned long ultimoBlinkAP = 0;
    if (millis() - ultimoBlinkAP > 1000) {
      ultimoBlinkAP = millis();
      digitalWrite(STATUS_LED, HIGH);
      delay(40);
      digitalWrite(STATUS_LED, LOW);
      delay(80);
      digitalWrite(STATUS_LED, HIGH);
      delay(40);
      digitalWrite(STATUS_LED, LOW);
    }

    // AUTO-RECUPERAÇÃO ATIVA: Tenta reconectar ao Wi-Fi residencial em background a cada 30 segundos!
    static unsigned long ultimoTentativaReconexaoResgate = 0;
    if (redeEstaConfigurada() && (millis() - ultimoTentativaReconexaoResgate > 30000)) {
      ultimoTentativaReconexaoResgate = millis();
      Serial.println("\n[PORTAL RESGATE] Sondando retorno do roteador residencial em background...");
      WiFi.begin(wifiSSID.c_str(), wifiPassword.c_str(), 0, NULL, true);

      unsigned long iniWait = millis();
      while (WiFi.status() != WL_CONNECTED && (millis() - iniWait < 6000)) {
        delay(100);
        dnsServer.processNextRequest();
        server.handleClient();
      }

      if (WiFi.status() == WL_CONNECTED) {
        finalizarModoResgatePorAutoRecuperacao();
      }
    }

    verificarSalvamentoSD();
    return;
  }

  // 2. MODO NORMAL: Garante Wi-Fi
  if (WiFi.status() != WL_CONNECTED) {
    conectarWiFi();
    if (WiFi.status() != WL_CONNECTED) {
      delay(500);
      verificarSalvamentoSD();
      return;
    }
  }

  // 3. Garante MQTT
  if (!client.connected()) {
    reconectarMQTT();
  }
  client.loop();

  // 4. Trata atualizações de código sem fio (ArduinoOTA)
  ArduinoOTA.handle();

  // 5. Atende requisições do WebServer HTTP (Backup, Listar, Baixar, Apagar no
  // SD)
  server.handleClient();

  // 6. Heartbeat periódico a cada 20 segundos
  if (millis() - ultimoHeartbeat > 20000) {
    ultimoHeartbeat = millis();
    float consumoKWh = contadorPulsos / IMPULSOS_KWH;
    float potW = calcularPotenciaAtual();
    String sdStatusStr = sdDisponivel ? "OK" : "FALHA";
    String hb = "{\"status\":\"online\",\"ip\":\"" + WiFi.localIP().toString() +
                "\",\"rssi\":" + String(WiFi.RSSI()) +
                ",\"uptime\":" + String(millis() / 1000) +
                ",\"pulsos\":" + String(contadorPulsos) +
                ",\"kwh\":" + String(consumoKWh, 3) +
                ",\"potencia_w\":" + String(potW, 1) + ",\"sd_status\":\"" +
                sdStatusStr + "\"}";
    client.publish(MQTT_TOPIC_HEARTBEAT, hb.c_str());
  }

  // 6. Envio em tempo real a cada novo pulso
  if (novoPulso) {
    novoPulso = false;

    // Calcula potência instantânea fora da interrupção com trava de segurança
    // residencial
    if (ultimoDeltaTempoPulso >= 250) {
      float w = 4500000.0f / (float)ultimoDeltaTempoPulso;
      if (w <= 15000.0f) {
        potenciaInstantaneaW = w;
      }
    }

    // Pisca brevemente (60ms) confirmando o pulso lido
    digitalWrite(STATUS_LED, LOW);
    delay(60);
    digitalWrite(STATUS_LED, HIGH);

    publicarMedicao();
  }

  // 7. Gravação no cartão Micro SD a cada 30 segundos
  verificarSalvamentoSD();
}
