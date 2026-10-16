/*
 * HealthBand ESP32 Multi-Modal Sensor Streamer
 * --------------------------------------------
 * Integrates with the HealthBand WESAD web dashboard via:
 * 1. USB Serial (Direct JSON streaming to Web Serial in Chrome/Edge)
 * 2. Bluetooth Low Energy (BLE - Direct connection in browser)
 * 3. WiFi HTTP POST (Streaming to http://<YOUR_PC_IP>:8000/api/sensor-data)
 *
 * Supported Sensor Pinouts (Configurable):
 * - MAX30100/MAX30102 (Pulse & SpO2): I2C (SDA: GPIO 21, SCL: GPIO 22)
 * - AD8232 (ECG): Analog A0 (GPIO 36 / VP), LO+ (GPIO 2), LO- (GPIO 4)
 * - EDA / GSR (Galvanic Skin Response): Analog ADC1 (GPIO 34)
 * - Skin Temp (MLX90614 or Thermistor / DS18B20): I2C or GPIO 15
 * - MPU6050 (IMU Accelerometer): I2C
 * - DHT22 (Ambient Temp & Humidity): GPIO 23
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ====== CONFIGURATION ======
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
// Set to your computer's local IP address where server.py is running
const char* SERVER_URL    = "http://192.168.1.100:8000/api/sensor-data";

// BLE UUIDs (Nordic UART Service standard)
#define SERVICE_UUID           "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define CHARACTERISTIC_UUID_TX "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

BLEServer *pServer = NULL;
BLECharacteristic *pTxCharacteristic = NULL;
bool deviceConnected = false;
bool oldDeviceConnected = false;

// Hardware Pins
const int PIN_EDA_GSR   = 34; // Analog ADC input for Skin Conductance
const int PIN_ECG_ADC   = 36; // Analog ADC input for AD8232 ECG
const int PIN_ECG_LO_P  = 2;  // Leads-off positive
const int PIN_ECG_LO_M  = 4;  // Leads-off negative

class MyServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) {
      deviceConnected = true;
    };
    void onDisconnect(BLEServer* pServer) {
      deviceConnected = false;
    }
};

void setupBLE() {
  BLEDevice::init("HealthBand-ESP32");
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new MyServerCallbacks());

  BLEService *pService = pServer->createService(SERVICE_UUID);
  pTxCharacteristic = pService->createCharacteristic(
                        CHARACTERISTIC_UUID_TX,
                        BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ
                      );
  pTxCharacteristic->addDescriptor(new BLE2902());
  pService->start();

  BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  pAdvertising->setMinPreferred(0x06);
  BLEDevice::startAdvertising();
  Serial.println("[BLE] Advertising started as 'HealthBand-ESP32'");
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n--- HealthBand ESP32 Initializing ---");

  pinMode(PIN_EDA_GSR, INPUT);
  pinMode(PIN_ECG_ADC, INPUT);
  pinMode(PIN_ECG_LO_P, INPUT);
  pinMode(PIN_ECG_LO_M, INPUT);

  // Initialize BLE
  setupBLE();

  // Connect WiFi if credentials provided
  if (String(WIFI_SSID) != "YOUR_WIFI_SSID") {
    Serial.printf("[WiFi] Connecting to %s...", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 20) {
      delay(500);
      Serial.print(".");
      attempts++;
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.println("\n[WiFi] Connected! IP: " + WiFi.localIP().toString());
    } else {
      Serial.println("\n[WiFi] Connection timed out. Running in BLE/Serial mode.");
    }
  } else {
    Serial.println("[WiFi] Credentials not set. Running in BLE / Web Serial mode.");
  }
}

// Convert ADC reading to microsiemens (uS) for EDA
float readEDA_uS() {
  int raw = analogRead(PIN_EDA_GSR);
  // Standard conversion for GSR sensor voltage divider
  float voltage = (raw / 4095.0) * 3.3;
  if (voltage <= 0.05) return 0.1;
  float resistance_kohm = ((3.3 - voltage) / voltage) * 100.0;
  float eda_uS = 1000.0 / max(10.0f, resistance_kohm);
  return constrain(eda_uS, 0.05f, 25.0f);
}

void loop() {
  // Read sensor values (replace with your sensor library reads, e.g. pox.getHeartRate())
  float eda = readEDA_uS();
  
  // Simulated / Sample readings for demonstration (substitute with live hardware readings):
  float hr = 76.0 + (random(-20, 20) / 10.0);
  float hrv = 54.0 + (random(-30, 30) / 10.0);
  float skin_temp = 33.2 + (random(-10, 10) / 50.0);
  int spo2 = 98;
  const char* act = "Resting";
  const char* mv = "Low";

  // Build JSON Payload
  String json = "{";
  json += "\"source\":\"ESP32-Hardware\",";
  json += "\"hr\":" + String(hr, 1) + ",";
  json += "\"hrv\":" + String(hrv, 1) + ",";
  json += "\"eda\":" + String(eda, 3) + ",";
  json += "\"st\":" + String(skin_temp, 2) + ",";
  json += "\"sp\":" + String(spo2) + ",";
  json += "\"act\":\"" + String(act) + "\",";
  json += "\"mv\":\"" + String(mv) + "\",";
  json += "\"amb\":26.5,";
  json += "\"hum\":56";
  json += "}";

  // 1. Output to USB Serial (Direct Web Serial in Chrome/Edge)
  Serial.println(json);

  // 2. Transmit via BLE if connected
  if (deviceConnected) {
    pTxCharacteristic->setValue((uint8_t*)json.c_str(), json.length());
    pTxCharacteristic->notify();
  }

  // 3. Send via WiFi HTTP POST if connected
  if (WiFi.status() == WL_CONNECTED && String(SERVER_URL).indexOf("192.168") != -1) {
    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "application/json");
    int httpResponseCode = http.POST(json);
    http.end();
  }

  // Advertising management for BLE
  if (!deviceConnected && oldDeviceConnected) {
    delay(500);
    pServer->startAdvertising();
    oldDeviceConnected = deviceConnected;
  }
  if (deviceConnected && !oldDeviceConnected) {
    oldDeviceConnected = deviceConnected;
  }

  delay(1000); // 1 Hz sensor update
}
