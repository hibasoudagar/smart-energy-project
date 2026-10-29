/*
  Smart Energy Monitor — ESP32 + PZEM-004T v3.0 + relay
  ------------------------------------------------------
  Every 10 seconds:
    1. Reads voltage, current, power and energy from the PZEM-004T
    2. Sends them to the laptop backend (app.py) over Wi-Fi
    3. Switches the relay ON or OFF as the backend replies

  Libraries (Arduino IDE -> Tools -> Manage Libraries):
    - "PZEM004Tv30" by Jakub Mandula
  Board: "ESP32 Dev Module" (install "esp32 by Espressif Systems" in Boards Manager)

  Wiring (low-voltage side):
    PZEM 5V  -> ESP32 VIN (5V)  (datasheet: TTL side needs an external 5 V supply)
    PZEM GND -> ESP32 GND
    PZEM TX  -> 1 kOhm -> ESP32 GPIO16 (RX2), and GPIO16 -> 2 kOhm -> GND
               (voltage divider: PZEM TX is 5 V, ESP32 pins are 3.3 V)
    PZEM RX  -> ESP32 GPIO17 (TX2)
    Relay VCC -> ESP32 VIN (5V), Relay GND -> GND, Relay IN -> GPIO26
    Alert LED: GPIO27 -> 220 Ohm resistor -> LED long leg (+); LED short leg (-) -> GND

  TEST MODE: set SIMULATE_READINGS = true to send sample readings (60 W when the
  relay is ON, ~0 W when OFF) without connecting mains. Use it to test Wi-Fi,
  backend, dashboard, relay and alert LED. Set it back to false for real readings.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <PZEM004Tv30.h>

// ---------- CHANGE THESE ----------
const char* WIFI_SSID     = "YOUR_WIFI_NAME";        // 2.4 GHz network (ESP32 can't use 5 GHz)
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL    = "http://192.168.1.5:5000/api/reading";  // your laptop's IPv4 address
const bool SIMULATE_READINGS = false;        // true = test without mains (sample readings)
// ----------------------------------

#define PZEM_RX_PIN   16     // ESP32 pin that receives from PZEM TX
#define PZEM_TX_PIN   17     // ESP32 pin that sends to PZEM RX
#define RELAY_PIN     26
#define ALERT_LED_PIN 27     // small indicator LED, lights up during an alert
const bool RELAY_ACTIVE_LOW = true;          // most relay modules switch ON when IN is LOW
const unsigned long SEND_INTERVAL_MS = 10000;

PZEM004Tv30 pzem(Serial2, PZEM_RX_PIN, PZEM_TX_PIN);
bool relayOn = true;
unsigned long lastSend = 0;

void setRelay(bool on) {
  relayOn = on;
  digitalWrite(RELAY_PIN, (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW);
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("Connecting to Wi-Fi \"%s\"", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nConnected. ESP32 IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\nWi-Fi failed. Will retry.");
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(ALERT_LED_PIN, OUTPUT);
  digitalWrite(ALERT_LED_PIN, LOW);     // alert LED off at start
  setRelay(true);                       // load ON at start
  if (SIMULATE_READINGS) Serial.println("*** TEST MODE: sending sample readings, PZEM not used ***");
  connectWiFi();
}

void loop() {
  if (millis() - lastSend < SEND_INTERVAL_MS) return;
  lastSend = millis();

  float voltage, current, power, energy;
  if (SIMULATE_READINGS) {
    power   = relayOn ? 58.0 + random(0, 41) / 10.0 : 0.2;   // 58-62 W when ON, ~0 W when OFF
    voltage = 228.0 + random(0, 81) / 10.0;                  // 228-236 V
    current = power / voltage;
    energy  = 0.0;
  } else {
    voltage = pzem.voltage();
    current = pzem.current();
    power   = pzem.power();             // watts
    energy  = pzem.energy();            // kWh since the PZEM was last reset
  }

  if (isnan(power)) {
    Serial.println("PZEM read error: check wiring and that the PZEM has mains voltage on L/N");
    return;
  }
  Serial.printf("V=%.1f V  I=%.3f A  P=%.1f W  E=%.3f kWh  relay=%s\n",
                voltage, current, power, energy, relayOn ? "ON" : "OFF");

  connectWiFi();
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(5000);
  String body = String("{\"power_w\":") + String(power, 2) +
                ",\"voltage\":" + String(voltage, 1) +
                ",\"current_a\":" + String(current, 3) +
                ",\"energy_kwh\":" + String(energy, 3) + "}";
  int code = http.POST(body);

  if (code == 200) {
    String reply = http.getString();    // e.g. {"alert":false,"predicted_w":12.9,"relay":"on"}
    reply.replace(" ", "");
    bool wantOn = reply.indexOf("\"relay\":\"off\"") < 0;
    if (wantOn != relayOn) {
      setRelay(wantOn);
      Serial.printf("Backend switched relay %s\n", wantOn ? "ON" : "OFF");
    }
    bool alert = reply.indexOf("\"alert\":true") >= 0;
    digitalWrite(ALERT_LED_PIN, alert ? HIGH : LOW);
    Serial.println("Backend: " + reply);
  } else {
    // Backend unreachable: keep the relay as it is
    Serial.printf("Send failed (HTTP %d). Is app.py running and SERVER_URL correct?\n", code);
  }
  http.end();
}
