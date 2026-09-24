/*
  ==================================================================================
  HTH-IT-06 · PEAK-SHAVING SYSTEM FIRMWARE (peak_shaving_firmware.ino)
  ==================================================================================
  Hardware Map:
    - ESP32 DevKit V1
    - ACS712-5A Current Sensors:
        GPIO 32: LED Module 1 (ADC1)
        GPIO 33: LED Module 2 (ADC1)
        GPIO 34: Resistor Load (ADC1)
        GPIO 35: Water Pump (ADC1 - Critical / Always Monitored)
    - Voltage Divider (10k / 3.3k Rail Sense): GPIO 36 (VP)
    - MOSFET PWM Drivers (NPN + IRFZ44N, Inverted Logic):
        GPIO 25: LED Module 1 PWM (Channel 0)
        GPIO 26: LED Module 2 PWM (Channel 1)
    - 5V Relay Module (Active-LOW): GPIO 27 (Resistor Load)
    - SSD1306 0.96" OLED (I2C): SDA GPIO 21, SCL GPIO 22
    - Status LEDs: GPIO 5 (LED1), 13 (LED2), 14 (Resistor), 18 (Pump)
    - Buttons: GPIO 16 (Manual Override), 17 (Ack/Silence)
    - Active 5V Buzzer: GPIO 4
    - WiFi Mode: Standalone Access Point ("PeakShaver" @ 192.168.4.1)
  ==================================================================================
*/

#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// --- USER CONFIGURATION BLOCK ---
const char* AP_SSID = "PeakShaver";
const char* AP_PASS = "hackathon123";
float PEAK_CAP_WATTS = 45.0;
float RATE_PER_KWH = 8.0;

// --- HARDWARE PIN DEFINITIONS ---
#define PIN_ACS_LED1     32
#define PIN_ACS_LED2     33
#define PIN_ACS_RESISTOR 34
#define PIN_ACS_PUMP     35
#define PIN_RAIL_VOLT    36

#define PIN_PWM_LED1     25
#define PIN_PWM_LED2     26
#define PIN_RELAY_RES    27

#define PIN_LED_STAT1    5
#define PIN_LED_STAT2    13
#define PIN_LED_STAT_RES 14
#define PIN_LED_STAT_PMP 18

#define PIN_BTN_OVERRIDE 16
#define PIN_BTN_SILENCE  17
#define PIN_BUZZER       4

// OLED Setup (128x64 I2C)
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// Web Server on Port 80
WebServer server(80);

// ACS712 Zero Calibration Voltage Offsets (Initial default ~2.5V)
float acsZero[4] = {2.50, 2.50, 2.50, 2.50}; // [LED1, LED2, Resistor, Pump]
const float ACS_SENSITIVITY = 0.185; // 185mV/A for ACS712-5A

// System State Variables
float railVoltage = 12.0;
float currentDraw[4] = {0.0, 0.0, 0.0, 0.0};
float powerDraw[4]   = {0.0, 0.0, 0.0, 0.0};
float totalPower = 0.0;
float predictedPower = 0.0;

uint8_t pwm_led1 = 255; // 0-255 (Inverted logic: 255 = 100% ON)
uint8_t pwm_led2 = 255;
bool state_resistor = true; // True = ON (Relay LOW)
bool isManualOverride = false;
bool isBuzzerSilenced = false;
unsigned long lastShedTime[3] = {0, 0, 0}; // [LED1, LED2, Resistor]

// Inverted PWM Writer Helper Function
void pwmWriteInverted(uint8_t pin, uint8_t brightness) {
  // NPN Driver logic: GPIO HIGH turns NPN ON -> pulls MOSFET Gate LOW (OFF)
  // brightness = 255 -> full ON -> GPIO LOW (0)
  // brightness = 0   -> full OFF -> GPIO HIGH (255)
  uint8_t inverted = 255 - brightness;
  analogWrite(pin, inverted);
}

// Read ACS712 Current Sensor with 32-sample averaging
float readACS712Current(int pin, float zeroVolt) {
  long rawSum = 0;
  for(int i=0; i<32; i++) {
    rawSum += analogRead(pin);
    delayMicroseconds(100);
  }
  float avgRaw = rawSum / 32.0;
  float voltage = (avgRaw / 4095.0) * 3.3;
  float current = (voltage - zeroVolt) / ACS_SENSITIVITY;
  return max(0.0f, fabsf(current));
}

// Read 12V Rail Voltage via 10k / 3.3k Voltage Divider
float readRailVoltage() {
  long rawSum = 0;
  for(int i=0; i<16; i++) {
    rawSum += analogRead(PIN_RAIL_VOLT);
  }
  float avgRaw = rawSum / 16.0;
  float vAdc = (avgRaw / 4095.0) * 3.3;
  float vRail = vAdc * (13.3 / 3.3); // Divider ratio
  return (vRail < 5.0) ? 12.0 : vRail; // Default fallback to 12V if unhooked
}

// HTH-IT-06 Shed-Score Calculation: score = power - (time_since_last_shed * weight)
float calculateShedScore(int index, float power) {
  if (index >= 3) return -999.0; // Pump is index 3 (Critical -> Excluded)
  float timeSec = (millis() - lastShedTime[index]) / 1000.0;
  return power - (timeSec * 0.5);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n--- HTH-IT-06 PeakShaver System Initializing ---");

  // Wire I2C Setup
  Wire.begin(21, 22);

  // Pin Modes
  pinMode(PIN_PWM_LED1, OUTPUT);
  pinMode(PIN_PWM_LED2, OUTPUT);
  pinMode(PIN_RELAY_RES, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);

  pinMode(PIN_LED_STAT1, OUTPUT);
  pinMode(PIN_LED_STAT2, OUTPUT);
  pinMode(PIN_LED_STAT_RES, OUTPUT);
  pinMode(PIN_LED_STAT_PMP, OUTPUT);

  pinMode(PIN_BTN_OVERRIDE, INPUT_PULLUP);
  pinMode(PIN_BTN_SILENCE, INPUT_PULLUP);

  // Initial States
  pwmWriteInverted(PIN_PWM_LED1, 255);
  pwmWriteInverted(PIN_PWM_LED2, 255);
  digitalWrite(PIN_RELAY_RES, LOW); // Active-LOW: LOW = ON
  digitalWrite(PIN_BUZZER, LOW);

  digitalWrite(PIN_LED_STAT1, HIGH);
  digitalWrite(PIN_LED_STAT2, HIGH);
  digitalWrite(PIN_LED_STAT_RES, HIGH);
  digitalWrite(PIN_LED_STAT_PMP, HIGH);

  // OLED Init
  if(display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0,10);
    display.println("PeakShaver AP Mode");
    display.println("Starting WiFi...");
    display.display();
  }

  // Self-Zero ACS712 Channels (Shiftable loads off/idle at boot)
  Serial.println("[ACS712] Calibrating zero offsets...");
  for(int ch=0; ch<3; ch++) {
    int pin = (ch==0)?PIN_ACS_LED1:((ch==1)?PIN_ACS_LED2:PIN_ACS_RESISTOR);
    long sum = 0;
    for(int i=0; i<64; i++) { sum += analogRead(pin); delay(2); }
    acsZero[ch] = ((sum / 64.0) / 4095.0) * 3.3;
    Serial.printf("  Ch %d Zero: %.3f V\n", ch, acsZero[ch]);
  }

  // WiFi Access Point Setup
  WiFi.softAP(AP_SSID, AP_PASS);
  IPAddress apIP = WiFi.softAPIP();
  Serial.print("[WiFi AP] Started! SSID: "); Serial.println(AP_SSID);
  Serial.print("[WiFi AP] Dashboard URL: http://"); Serial.println(apIP);

  // Setup Web Server Routes
  server.on("/data", HTTP_GET, []() {
    String json = "{\"rail_voltage\":" + String(railVoltage, 2) + ",\"total_power\":" + String(totalPower, 2) +
                  ",\"predicted_power\":" + String(predictedPower, 2) + ",\"devices\":{" +
                  "\"led1\":{\"current\":" + String(currentDraw[0], 2) + ",\"power\":" + String(powerDraw[0], 2) + "}," +
                  "\"led2\":{\"current\":" + String(currentDraw[1], 2) + ",\"power\":" + String(powerDraw[1], 2) + "}," +
                  "\"resistor\":{\"current\":" + String(currentDraw[2], 2) + ",\"power\":" + String(powerDraw[2], 2) + "}," +
                  "\"pump\":{\"current\":" + String(currentDraw[3], 2) + ",\"power\":" + String(powerDraw[3], 2) + "}}}";
    server.send(200, "application/json", json);
  });

  server.begin();
}

void loop() {
  server.handleClient();

  // 1. Read Sensors
  railVoltage = readRailVoltage();
  currentDraw[0] = readACS712Current(PIN_ACS_LED1, acsZero[0]);
  currentDraw[1] = readACS712Current(PIN_ACS_LED2, acsZero[1]);
  currentDraw[2] = readACS712Current(PIN_ACS_RESISTOR, acsZero[2]);
  currentDraw[3] = readACS712Current(PIN_ACS_PUMP, acsZero[3]);

  totalPower = 0.0;
  for(int i=0; i<4; i++) {
    powerDraw[i] = railVoltage * currentDraw[i];
    totalPower += powerDraw[i];
  }
  predictedPower = totalPower; // Baseline

  // 2. Check Physical Buttons
  if (digitalRead(PIN_BTN_OVERRIDE) == LOW) {
    isManualOverride = !isManualOverride;
    delay(300);
  }
  if (digitalRead(PIN_BTN_SILENCE) == LOW) {
    isBuzzerSilenced = true;
    digitalWrite(PIN_BUZZER, LOW);
    delay(300);
  }

  // 3. Peak-Shaving Shed-Score Logic (If not in Manual Override)
  if (!isManualOverride && totalPower > PEAK_CAP_WATTS) {
    float score0 = calculateShedScore(0, powerDraw[0]);
    float score1 = calculateShedScore(1, powerDraw[1]);
    float score2 = calculateShedScore(2, powerDraw[2]);

    if (score0 >= score1 && score0 >= score2 && pwm_led1 > 25) {
      pwm_led1 = (pwm_led1 >= 25) ? (pwm_led1 - 25) : 0; // Reduce 10%
      pwmWriteInverted(PIN_PWM_LED1, pwm_led1);
      lastShedTime[0] = millis();
    } else if (score1 >= score2 && pwm_led2 > 25) {
      pwm_led2 = (pwm_led2 >= 25) ? (pwm_led2 - 25) : 0; // Reduce 10%
      pwmWriteInverted(PIN_PWM_LED2, pwm_led2);
      lastShedTime[1] = millis();
    } else if (state_resistor) {
      state_resistor = false;
      digitalWrite(PIN_RELAY_RES, HIGH); // OFF
      lastShedTime[2] = millis();
    }

    if (!isBuzzerSilenced) digitalWrite(PIN_BUZZER, HIGH);
  } else {
    digitalWrite(PIN_BUZZER, LOW);
  }

  // 4. Update Status LEDs
  digitalWrite(PIN_LED_STAT1, (pwm_led1 > 0) ? HIGH : LOW);
  digitalWrite(PIN_LED_STAT2, (pwm_led2 > 0) ? HIGH : LOW);
  digitalWrite(PIN_LED_STAT_RES, state_resistor ? HIGH : LOW);
  digitalWrite(PIN_LED_STAT_PMP, HIGH); // Pump status LED ALWAYS ON!

  // 5. Update OLED Display
  display.clearDisplay();
  display.setCursor(0, 0);
  display.print("PWR: "); display.print(totalPower, 1); display.print("/"); display.print(PEAK_CAP_WATTS,0); display.println("W");
  display.print("PMP: "); display.print(powerDraw[3], 1); display.println("W (CRIT)");
  display.print("L1:"); display.print(powerDraw[0], 1); display.print("W L2:"); display.println(powerDraw[1], 1);
  display.print("RES:"); display.print(powerDraw[2], 1); display.print("W ");
  display.println(isManualOverride ? "[OVERRIDE]" : "[AUTO]");
  display.display();

  delay(200);
}
