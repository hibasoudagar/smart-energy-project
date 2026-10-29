/*
  Smart Energy Monitor — DC version: ESP32 + ACS712 + relay + battery/DC load
  ---------------------------------------------------------------------------
  Every 10 seconds:
    1. Measures the load current with the ACS712 (averaged over many samples)
    2. Calculates power = battery voltage x current, and accumulates energy
    3. Sends the reading to the laptop backend (app.py) over Wi-Fi
    4. Switches the relay and the alert LED as the backend replies

  No extra libraries needed. Board: "ESP32 Dev Module".

  LOW-VOLTAGE WIRING (jumper wires)
    ACS712 VCC  -> ESP32 VIN (5V)
    ACS712 GND  -> ESP32 GND
    ACS712 OUT  -> 1 kOhm -> ESP32 GPIO34, and GPIO34 -> 2 kOhm -> GND
                   (voltage divider: ACS712 output can reach 5 V, ESP32 pins are 3.3 V)
    Relay VCC   -> ESP32 VIN (5V),  Relay GND -> GND,  Relay IN -> GPIO26
    Alert LED   -> GPIO27 -> 220 Ohm -> LED long leg (+); LED short leg (-) -> GND

  LOAD CIRCUIT (battery side)
    Battery (+) -> Relay COM
    Relay NO    -> ACS712 IP+   (big screw terminal)
    ACS712 IP-  -> Load (+)     (DC fan / LED strip / small motor)
    Load (-)    -> Battery (-)

  IMPORTANT: the relay must be OFF for 1 second at start-up so the ACS712 zero
  point can be measured. Don't touch the load circuit during start-up.
*/

#include <WiFi.h>
#include <HTTPClient.h>

// ---------- CHANGE THESE ----------
const char* WIFI_SSID     = "YOUR_WIFI_NAME";        // 2.4 GHz network (ESP32 can't use 5 GHz)
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL    = "http://192.168.1.5:5000/api/reading";  // your laptop's IPv4 address

const float BATTERY_VOLTAGE   = 9.0;    // your battery/adapter voltage, e.g. 9.0 or 12.0
const float ACS712_MV_PER_AMP = 185.0;  // 5A module: 185 | 20A module: 100 | 30A module: 66
                                        // (the model is printed on the chip: ACS712ELCTR-05B / -20A / -30A)
const bool SIMULATE_READINGS  = false;  // true = test without the load circuit (sample readings)
// ----------------------------------

#define ACS712_PIN    34     // analog input (ADC1, works with Wi-Fi on)
#define RELAY_PIN     26
#define ALERT_LED_PIN 27
const bool  RELAY_ACTIVE_LOW = true;        // most relay modules switch ON when IN is LOW
const float DIVIDER_RATIO    = 2.0 / 3.0;   // 1k / 2k divider: pin voltage = sensor voltage x 2/3
                                            // No resistors (direct OUT -> GPIO34, small loads under 1 A only)? Use 1.0
const float NOISE_FLOOR_A    = 0.03;        // currents below this are treated as 0 A (sensor noise)
const int   SAMPLES          = 1000;        // ADC samples averaged per reading
const unsigned long SEND_INTERVAL_MS = 10000;

bool relayOn = true;
unsigned long lastSend = 0;
float zeroMv = 0;            // sensor output (at the pin) when no current flows
double energyKwh = 0;

void setRelay(bool on) {
  relayOn = on;
  digitalWrite(RELAY_PIN, (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW);
}

float readPinMv() {                       // average many samples to reduce noise
  uint32_t sum = 0;
  for (int i = 0; i < SAMPLES; i++) {
    sum += analogReadMilliVolts(ACS712_PIN);
    delayMicroseconds(100);
  }
  return (float)sum / SAMPLES;
}

float readCurrentA() {
  float sensorMv = (readPinMv() - zeroMv) / DIVIDER_RATIO;   // change at the sensor output
  float amps = sensorMv / ACS712_MV_PER_AMP;
  if (fabs(amps) < NOISE_FLOOR_A) amps = 0;
  return fabs(amps);                     // direction doesn't matter for consumption
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
  digitalWrite(ALERT_LED_PIN, LOW);

  if (SIMULATE_READINGS) {
    Serial.println("*** TEST MODE: sending sample readings, ACS712 not used ***");
  } else {
    setRelay(false);                     // no current -> measure the zero point
    delay(1000);
    zeroMv = readPinMv();
    float expectedMv = 2500.0 * DIVIDER_RATIO;   // ACS712 outputs ~2.5 V at 0 A
    Serial.printf("ACS712 zero point: %.0f mV at the pin (expect about %.0f mV)\n", zeroMv, expectedMv);
    if (fabs(zeroMv - expectedMv) > 400) {
      Serial.println("WARNING: zero point looks wrong. Check ACS712 VCC=5V, GND, OUT and the 1k/2k divider.");
    }
  }
  setRelay(true);                        // load ON
  connectWiFi();
}

void loop() {
  if (millis() - lastSend < SEND_INTERVAL_MS) return;
  unsigned long now = millis();
  float hours = (lastSend == 0) ? 0 : (now - lastSend) / 3600000.0;
  lastSend = now;

  float current, voltage, power;
  if (SIMULATE_READINGS) {
    voltage = BATTERY_VOLTAGE;
    current = relayOn ? 0.18 + random(0, 5) / 100.0 : 0.0;   // ~0.2 A when ON
  } else {
    voltage = BATTERY_VOLTAGE;
    current = relayOn ? readCurrentA() : 0.0;
  }
  power = voltage * current;               // watts
  energyKwh += power * hours / 1000.0;

  Serial.printf("V=%.1f V  I=%.3f A  P=%.2f W  E=%.6f kWh  relay=%s\n",
                voltage, current, power, energyKwh, relayOn ? "ON" : "OFF");

  connectWiFi();
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(5000);
  String body = String("{\"power_w\":") + String(power, 3) +
                ",\"voltage\":" + String(voltage, 2) +
                ",\"current_a\":" + String(current, 3) +
                ",\"energy_kwh\":" + String(energyKwh, 6) + "}";
  int code = http.POST(body);

  if (code == 200) {
    String reply = http.getString();    // e.g. {"alert":false,"predicted_w":1.8,"relay":"on"}
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
    Serial.printf("Send failed (HTTP %d). Is app.py running and SERVER_URL correct?\n", code);
  }
  http.end();
}
