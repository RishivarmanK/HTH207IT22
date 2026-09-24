/*
  ==================================================================================
  ESP32 FIRMWARE: INTELLIGENT PEAK-SHAVING & ENERGY MANAGEMENT SYSTEM
  ==================================================================================
  Hardware:
    - ESP32 DevKit V1
    - TCA9548A 8-Channel I2C Multiplexer
    - INA219 Current/Voltage Sensors (Channels 0-4)
    - SSD1306 0.96" OLED Display (Channel 7 or directly on SDA/SCL)
    - 5V Relay Module (GPIO 18 - Flexible Load Fan 1)
    - IRFZ44N Gate Drivers (GPIO 19, 23 - Flexible Loads Fan 2, LED 1, LED 2)
    - Critical Pump (Monitored on INA219 Ch 4, direct power line - NO AUTO SHED GPIO)
    - Manual Push Buttons (GPIO 34, 35) & Buzzer (GPIO 13)
  ==================================================================================
*/

#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Adafruit_INA219.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>

// --- Wi-Fi Credentials ---
const char* ssid     = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// Backend Server API URL (Replace with your laptop's Local IP address)
const char* serverUrl = "http://192.168.1.100:5000/api/telemetry";

// --- TCA9548A I2C Multiplexer Address ---
#define TCAADDR 0x70

// --- GPIO Pin Definitions ---
#define RELAY_PIN_FAN1    18   // Relay control for Fan 1
#define MOSFET_PIN_FAN2   19   // Gate driver for Fan 2
#define MOSFET_PIN_LED1   23   // Gate driver for LED 1
#define MOSFET_PIN_LED2   5    // PWM Gate driver for LED 2 (Dimming)
#define BUZZER_PIN        13   // Active 5V Buzzer
#define BTN_OVERRIDE_PIN  34   // Button 1: Manual Override
#define BTN_SHED_PIN      35   // Button 2: Manual Shedding

// OLED Setup (128x64 I2C)
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// INA219 Sensor Array
Adafruit_INA219 ina219_ch0; // Fan 1
Adafruit_INA219 ina219_ch1; // Fan 2
Adafruit_INA219 ina219_ch2; // LED 1
Adafruit_INA219 ina219_ch3; // LED 2
Adafruit_INA219 ina219_ch4; // Pump (Critical)

// Function to select TCA9548A channel (0 to 7)
void tcaSelect(uint8_t i) {
  if (i > 7) return;
  Wire.beginTransmission(TCAADDR);
  Wire.write(1 << i);
  Wire.endTransmission();
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n--- ESP32 Peak Shaving System Initializing ---");

  // Wire I2C Initialization (SDA: GPIO 21, SCL: GPIO 22)
  Wire.begin(21, 22);

  // Pin Modes Setup
  pinMode(RELAY_PIN_FAN1, OUTPUT);
  pinMode(MOSFET_PIN_FAN2, OUTPUT);
  pinMode(MOSFET_PIN_LED1, OUTPUT);
  pinMode(MOSFET_PIN_LED2, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  
  pinMode(BTN_OVERRIDE_PIN, INPUT);
  pinMode(BTN_SHED_PIN, INPUT);

  // Initial Pin States (Defaults: Loads ON, Active LOW for Relays if applicable)
  digitalWrite(RELAY_PIN_FAN1, HIGH);
  digitalWrite(MOSFET_PIN_FAN2, HIGH);
  digitalWrite(MOSFET_PIN_LED1, HIGH);
  digitalWrite(MOSFET_PIN_LED2, HIGH);
  digitalWrite(BUZZER_PIN, LOW);

  // Initialize INA219 Sensors over TCA9548A
  tcaSelect(0); ina219_ch0.begin();
  tcaSelect(1); ina219_ch1.begin();
  tcaSelect(2); ina219_ch2.begin();
  tcaSelect(3); ina219_ch3.begin();
  tcaSelect(4); ina219_ch4.begin();

  // Initialize OLED Display
  if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("SSD1306 OLED allocation failed"));
  } else {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0,10);
    display.println("Peak-Shaving System");
    display.println("Connecting Wi-Fi...");
    display.display();
  }

  // Connect Wi-Fi
  WiFi.begin(ssid, password);
  Serial.print("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n[Wi-Fi] Connected! IP Address: " + WiFi.localIP().toString());
}

void loop() {
  // Read Sensor Telemetry across multiplexer channels
  tcaSelect(0); float v_fan1 = ina219_ch0.getBusVoltage_V(); float i_fan1 = ina219_ch0.getCurrent_mA() / 1000.0;
  tcaSelect(1); float v_fan2 = ina219_ch1.getBusVoltage_V(); float i_fan2 = ina219_ch1.getCurrent_mA() / 1000.0;
  tcaSelect(2); float v_led1 = ina219_ch2.getBusVoltage_V(); float i_led1 = ina219_ch2.getCurrent_mA() / 1000.0;
  tcaSelect(3); float v_led2 = ina219_ch3.getBusVoltage_V(); float i_led2 = ina219_ch3.getCurrent_mA() / 1000.0;
  tcaSelect(4); float v_pump = ina219_ch4.getBusVoltage_V(); float i_pump = ina219_ch4.getCurrent_mA() / 1000.0;

  float total_p = (v_fan1 * i_fan1) + (v_fan2 * i_fan2) + (v_led1 * i_led1) + (v_led2 * i_led2) + (v_pump * i_pump);

  // Update OLED Display
  display.clearDisplay();
  display.setCursor(0, 0);
  display.print("TOTAL PWR: "); display.print(total_p, 1); display.println(" W");
  display.print("PUMP: "); display.print(v_pump * i_pump, 1); display.println(" W (CRIT)");
  display.print("FAN1: "); display.print(v_fan1 * i_fan1, 1); display.print("W | FAN2: "); display.println(v_fan2 * i_fan2, 1);
  display.print("LED1: "); display.print(v_led1 * i_led1, 1); display.print("W | LED2: "); display.println(v_led2 * i_led2, 1);
  display.display();

  // Send Telemetry to Laptop Backend over HTTP POST
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");

    StaticJsonDocument<512> doc;
    JsonObject devices = doc.createNestedObject("devices");

    JsonObject fan1 = devices.createNestedObject("fan1"); fan1["voltage"] = v_fan1; fan1["current"] = i_fan1;
    JsonObject fan2 = devices.createNestedObject("fan2"); fan2["voltage"] = v_fan2; fan2["current"] = i_fan2;
    JsonObject led1 = devices.createNestedObject("led1"); led1["voltage"] = v_led1; led1["current"] = i_led1;
    JsonObject led2 = devices.createNestedObject("led2"); led2["voltage"] = v_led2; led2["current"] = i_led2;
    JsonObject pump = devices.createNestedObject("pump"); pump["voltage"] = v_pump; pump["current"] = i_pump;

    String jsonPayload;
    serializeJson(doc, jsonPayload);

    int httpCode = http.POST(jsonPayload);

    if (httpCode > 0) {
      String response = http.getString();
      // Parse backend control commands to adjust local relays & MOSFETs
      StaticJsonDocument<256> resDoc;
      deserializeJson(resDoc, response);
      JsonObject states = resDoc["active_states"];
      
      if (states.containsKey("fan1")) digitalWrite(RELAY_PIN_FAN1, String(states["fan1"]).equals("OFF") ? LOW : HIGH);
      if (states.containsKey("fan2")) digitalWrite(MOSFET_PIN_FAN2, String(states["fan2"]).equals("OFF") ? LOW : HIGH);
      if (states.containsKey("led1")) digitalWrite(MOSFET_PIN_LED1, String(states["led1"]).equals("OFF") ? LOW : HIGH);
      if (states.containsKey("led2")) {
        String s = states["led2"];
        if (s == "OFF") digitalWrite(MOSFET_PIN_LED2, LOW);
        else if (s.startsWith("DIMMED")) analogWrite(MOSFET_PIN_LED2, 128); // 50% PWM
        else digitalWrite(MOSFET_PIN_LED2, HIGH);
      }
    }
    http.end();
  }

  delay(2000); // 2 second update loop
}
