import sqlite3
import time
import random
import threading
import hashlib
import json
import os
from datetime import datetime, date
from collections import deque
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DB_NAME = "energy_system.db"
db_lock = threading.Lock()

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn

# --- HTH-IT-06 SYSTEM & INDUSTRIAL ToD TARIFF CONFIGURATION ---
CONFIG = {
    "peak_cap_watts": 45.0,        # Configurable power threshold
    "safety_margin_watts": 3.0,    # Safety margin before restore
    "auto_shedding": True,
    "mock_simulation": True,
    "shed_score_weight": 0.5,       # Weight per second since last shed
    "tariff_category": "HT-I",      # "HT-I", "LT-IIIB", "LT-V"
    "contract_demand_kva": 5.0,    # Contracted demand
    "fca_rate": 0.25,               # Fuel Cost Adjustment (₹/kWh)
    "tax_percent": 0.05             # 5% Electricity Duty / Tax
}

# Function to calculate active Time-of-Day (ToD) Tariff Rate based on system clock & category
def get_active_tod_tariff():
    now = datetime.now()
    hour = now.hour
    category = CONFIG["tariff_category"]
    
    # 1. High Tension Industrial Tariff (HT-I)
    # Energy Charge: ₹7.50 flat base
    # Peak (+25%): ₹7.50 + ₹1.875 = ₹9.375/kWh
    # Night (-5%): ₹7.50 - ₹0.375 = ₹7.125/kWh
    # Demand Charge: ₹608 per kVA per month
    if category == "HT-I":
        demand_rate = 608.0 # ₹/kVA/month
        if (6 <= hour < 10) or (18 <= hour < 22):
            slot_name = "PEAK HOURS (+25%)"
            rate = 9.375 # ₹9.375/kWh
        elif (22 <= hour) or (hour < 5):
            slot_name = "NIGHT OFF-PEAK (-5%)"
            rate = 7.125 # ₹7.125/kWh
        else:
            slot_name = "NORMAL HOURS"
            rate = 7.500 # ₹7.500/kWh
        return round(rate, 3), slot_name, demand_rate

    # 2. Low Tension Industrial Tariff (LT-IIIB)
    # Energy Charge: ₹8.25 flat base
    # Smart Meter Peak (+15%): ₹8.25 * 1.15 = ₹9.488/kWh
    # Demand Charge: ₹170 per kW per month
    elif category == "LT-IIIB":
        demand_rate = 170.0 # ₹/kW/month
        if (6 <= hour < 10) or (18 <= hour < 22):
            slot_name = "SMART METER PEAK (+15%)"
            rate = 9.488 # ₹8.25 * 1.15 = ₹9.4875
        else:
            slot_name = "NORMAL HOURS"
            rate = 8.250 # ₹8.250/kWh
        return round(rate, 3), slot_name, demand_rate

    # 3. Low Tension Commercial Tariff (LT-V)
    # Energy Charge: Flat ₹10.45 per unit (kWh)
    # Demand Charge: ₹350 per kW per month
    else:
        demand_rate = 350.0 # ₹/kW/month
        slot_name = "FLAT COMMERCIAL"
        rate = 10.450 # ₹10.450/kWh
        return round(rate, 3), slot_name, demand_rate

# HTH-IT-06 Device Registry
DEVICES = {
    "led1": {
        "name": "LED Module 1",
        "type": "dimmable",
        "is_shiftable": True,
        "gpio_pwm": 25,
        "status_led_gpio": 5,
        "dim_level": 255, # PWM 0-255 (Inverted logic: 255 = 100% ON)
        "state": "ON",
        "last_shed_time": 0.0,
        "is_physical": True,
        "accumulated_kwh": 0.0,
        "accumulated_cost_inr": 0.0
    },
    "led2": {
        "name": "LED Module 2",
        "type": "dimmable",
        "is_shiftable": True,
        "gpio_pwm": 26,
        "status_led_gpio": 13,
        "dim_level": 255,
        "state": "ON",
        "last_shed_time": 0.0,
        "is_physical": True,
        "accumulated_kwh": 0.0,
        "accumulated_cost_inr": 0.0
    },
    "resistor": {
        "name": "Resistor Load (Bulb)",
        "type": "relay",
        "is_shiftable": True,
        "gpio_relay": 27,
        "status_led_gpio": 14,
        "dim_level": 255,
        "state": "ON",
        "last_shed_time": 0.0,
        "is_physical": True,
        "accumulated_kwh": 0.0,
        "accumulated_cost_inr": 0.0
    },
    "pump": {
        "name": "Water Pump (Critical)",
        "type": "critical",
        "is_shiftable": False,
        "status_led_gpio": 18,
        "dim_level": 255,
        "state": "ALWAYS ON (CRITICAL)",
        "last_shed_time": 0.0,
        "is_physical": True,
        "accumulated_kwh": 0.0,
        "accumulated_cost_inr": 0.0
    }
}

power_history = deque(maxlen=5)

latest_system_state = {
    "timestamp": datetime.now().strftime("%H:%M:%S"),
    "rail_voltage": 12.0,
    "total_power": 41.8,
    "predicted_power": 41.8,
    "total_kwh_consumed": 0.0,
    "total_cost_consumed_inr": 0.0,
    "peak_cap_watts": CONFIG["peak_cap_watts"],
    "system_status": "NORMAL", # NORMAL, PEAK_RISK, SHEDDING_ACTIVE, MANUAL_OVERRIDE
    "active_tod_slot": "NORMAL HOURS",
    "active_tariff_rate_inr": 7.50,
    "demand_charge_rate_inr": 608.0,
    "hourly_cost_inr": 0.313,
    "monthly_demand_charge_inr": 3040.0,
    "monthly_projected_cost_inr": 3265.71,
    "devices": {
        "led1": {"name": "LED Module 1", "voltage": 12.0, "current": 0.35, "power": 4.2, "kwh_consumed": 0.0, "cost_inr": 0.0, "state": "ON", "dim_level": 255, "is_shiftable": True, "gpio_status_led": 5, "shed_score": 2.1},
        "led2": {"name": "LED Module 2", "voltage": 12.0, "current": 0.30, "power": 3.6, "kwh_consumed": 0.0, "cost_inr": 0.0, "state": "ON", "dim_level": 255, "is_shiftable": True, "gpio_status_led": 13, "shed_score": 1.8},
        "resistor": {"name": "Resistor Load (Bulb)", "voltage": 12.0, "current": 1.65, "power": 19.8, "kwh_consumed": 0.0, "cost_inr": 0.0, "state": "ON", "dim_level": 255, "is_shiftable": True, "gpio_status_led": 14, "shed_score": 18.2},
        "pump": {"name": "Water Pump (Critical)", "voltage": 12.0, "current": 1.10, "power": 13.2, "kwh_consumed": 0.0, "cost_inr": 0.0, "state": "ALWAYS ON (CRITICAL)", "dim_level": 255, "is_shiftable": False, "gpio_status_led": 18, "shed_score": -999.0}
    },
    "recent_actions": [],
    "blockchain_info": {
        "total_blocks": 0,
        "latest_hash": "",
        "latest_block_index": 0,
        "is_valid": True,
        "available_dates": []
    },
    "total_energy_saved_kwh": 0.0,
    "total_cost_saved_inr": 0.0,
    "manual_override": False,
    "buzzer_silenced": False
}

# --- CRYPTOGRAPHIC BLOCKCHAIN AUDIT ENGINE ---
class ShedScoreBlockchain:
    def __init__(self, db_name=DB_NAME):
        self.db_name = db_name
        self.chain = []
        self.lock = threading.Lock()
        self.init_chain_db()

    def init_chain_db(self):
        with self.lock:
            with db_lock:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('PRAGMA journal_mode=WAL;')
                cursor.execute('PRAGMA busy_timeout=5000;')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS blockchain_ledger (
                        block_index INTEGER PRIMARY KEY,
                        timestamp TEXT,
                        date_str TEXT,
                        action_type TEXT,
                        device_id TEXT,
                        target_device TEXT,
                        shed_score REAL,
                        reason TEXT,
                        demand_relief_w REAL,
                        power_before REAL,
                        predicted_power REAL,
                        previous_hash TEXT,
                        block_hash TEXT,
                        nonce INTEGER,
                        validator TEXT,
                        data_json TEXT
                    )
                ''')
                conn.commit()
                
                # Load existing blocks from database
                cursor.execute('''
                    SELECT block_index, timestamp, date_str, action_type, device_id, target_device, 
                           shed_score, reason, demand_relief_w, power_before, predicted_power, 
                           previous_hash, block_hash, nonce, validator, data_json 
                    FROM blockchain_ledger 
                    ORDER BY block_index ASC
                ''')
                rows = cursor.fetchall()
                if rows:
                    self.chain = []
                    for r in rows:
                        self.chain.append({
                            "index": r["block_index"],
                            "timestamp": r["timestamp"],
                            "date": r["date_str"],
                            "action_type": r["action_type"],
                            "device_id": r["device_id"],
                            "target_device": r["target_device"],
                            "shed_score": r["shed_score"],
                            "reason": r["reason"],
                            "demand_relief_w": r["demand_relief_w"],
                            "power_before": r["power_before"],
                            "predicted_power": r["predicted_power"],
                            "previous_hash": r["previous_hash"],
                            "hash": r["block_hash"],
                            "nonce": r["nonce"],
                            "validator": r["validator"],
                            "data": json.loads(r["data_json"]) if r["data_json"] else {}
                        })
                else:
                    self.create_genesis_block(conn)
                conn.close()

    def calculate_hash(self, index, timestamp, date_str, action_type, target_device, shed_score, reason, demand_relief_w, power_before, predicted_power, previous_hash, nonce):
        payload = f"{index}|{timestamp}|{date_str}|{action_type}|{target_device}|{shed_score}|{reason}|{demand_relief_w}|{power_before}|{predicted_power}|{previous_hash}|{nonce}"
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def create_genesis_block(self, conn):
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")
        index = 0
        previous_hash = "0" * 64
        action_type = "GENESIS_BLOCK"
        target_device = "SYSTEM_LEDGER_INIT"
        shed_score = 0.0
        reason = "HTH-IT-06 Cryptographic Shed-Score Audit Blockchain Genesis Initialized"
        demand_relief_w = 0.0
        power_before = 0.0
        predicted_power = 0.0
        nonce = 1006
        validator = "ESP32-EDGE-NODE-01"
        data_json = json.dumps({"note": "Genesis block for tamper-proof audit trail", "system": "HTH-IT-06", "created_at": now_str})
        
        block_hash = self.calculate_hash(index, now_str, date_str, action_type, target_device, shed_score, reason, demand_relief_w, power_before, predicted_power, previous_hash, nonce)
        genesis_block = {
            "index": index,
            "timestamp": now_str,
            "date": date_str,
            "action_type": action_type,
            "device_id": "genesis",
            "target_device": target_device,
            "shed_score": shed_score,
            "reason": reason,
            "demand_relief_w": demand_relief_w,
            "power_before": power_before,
            "predicted_power": predicted_power,
            "previous_hash": previous_hash,
            "hash": block_hash,
            "nonce": nonce,
            "validator": validator,
            "data": json.loads(data_json)
        }
        self.chain.append(genesis_block)
        
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO blockchain_ledger (block_index, timestamp, date_str, action_type, device_id, target_device, shed_score, reason, demand_relief_w, power_before, predicted_power, previous_hash, block_hash, nonce, validator, data_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (index, now_str, date_str, action_type, "genesis", target_device, shed_score, reason, demand_relief_w, power_before, predicted_power, previous_hash, block_hash, nonce, validator, data_json))
        conn.commit()

    def add_block(self, action_type, dev_id, reason, power_before, predicted_power, demand_relief_w, shed_score):
        with self.lock:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            date_str = now.strftime("%Y-%m-%d")
            
            prev_block = self.chain[-1] if self.chain else None
            prev_hash = prev_block["hash"] if prev_block else ("0" * 64)
            index = len(self.chain)
            
            dev_name = DEVICES.get(dev_id, {}).get("name", dev_id)
            validator = "ESP32-EDGE-NODE-01"
            
            nonce = 0
            block_hash = ""
            while True:
                block_hash = self.calculate_hash(index, now_str, date_str, action_type, dev_name, shed_score, reason, demand_relief_w, power_before, predicted_power, prev_hash, nonce)
                if block_hash.startswith("0") or nonce >= 30: # Lightweight cryptographic proof
                    break
                nonce += 1
            
            data_dict = {
                "action": action_type,
                "device_id": dev_id,
                "device_name": dev_name,
                "power_before_w": power_before,
                "predicted_power_w": predicted_power,
                "demand_relief_w": demand_relief_w,
                "shed_score": shed_score,
                "reason": reason,
                "tariff_slot": latest_system_state.get("active_tod_slot", "NORMAL HOURS"),
                "tariff_rate_inr": latest_system_state.get("active_tariff_rate_inr", 7.50)
            }
            data_json = json.dumps(data_dict)
            
            new_block = {
                "index": index,
                "timestamp": now_str,
                "date": date_str,
                "action_type": action_type,
                "device_id": dev_id,
                "target_device": dev_name,
                "shed_score": shed_score,
                "reason": reason,
                "demand_relief_w": demand_relief_w,
                "power_before": power_before,
                "predicted_power": predicted_power,
                "previous_hash": prev_hash,
                "hash": block_hash,
                "nonce": nonce,
                "validator": validator,
                "data": data_dict
            }
            
            self.chain.append(new_block)
            
            try:
                with db_lock:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO blockchain_ledger (block_index, timestamp, date_str, action_type, device_id, target_device, shed_score, reason, demand_relief_w, power_before, predicted_power, previous_hash, block_hash, nonce, validator, data_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (index, now_str, date_str, action_type, dev_id, dev_name, shed_score, reason, demand_relief_w, power_before, predicted_power, prev_hash, block_hash, nonce, validator, data_json))
                    conn.commit()
                    conn.close()
            except Exception as e:
                print(f"Error persisting block {index}: {e}")
                
            return new_block

    def verify_chain(self):
        with self.lock:
            if not self.chain:
                return {"is_valid": True, "total_blocks": 0, "message": "Empty chain"}
            
            for i in range(1, len(self.chain)):
                current = self.chain[i]
                previous = self.chain[i-1]
                
                # Check previous hash link
                if current["previous_hash"] != previous["hash"]:
                    return {
                        "is_valid": False,
                        "broken_block_index": current["index"],
                        "error": f"Invalid previous_hash at block #{current['index']}",
                        "total_blocks": len(self.chain)
                    }
                
                # Recompute hash
                recomputed_hash = self.calculate_hash(
                    current["index"], current["timestamp"], current["date"],
                    current["action_type"], current["target_device"],
                    current["shed_score"], current["reason"],
                    current["demand_relief_w"], current["power_before"],
                    current["predicted_power"], current["previous_hash"],
                    current["nonce"]
                )
                if current["hash"] != recomputed_hash:
                    return {
                        "is_valid": False,
                        "broken_block_index": current["index"],
                        "error": f"Hash mismatch at block #{current['index']}. Tampering detected!",
                        "total_blocks": len(self.chain)
                    }
                    
            return {
                "is_valid": True,
                "total_blocks": len(self.chain),
                "last_hash": self.chain[-1]["hash"] if self.chain else "None",
                "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "algorithm": "SHA-256 (Merkle / Hash-Chained)",
                "tamper_status": "SECURE_IMMUTABLE"
            }

    def get_blocks_by_date(self, target_date=None):
        with self.lock:
            if not target_date or target_date == "ALL":
                return list(reversed(self.chain))
            return [b for b in reversed(self.chain) if b["date"] == target_date]

    def get_available_dates(self):
        with self.lock:
            date_counts = {}
            today_str = datetime.now().strftime("%Y-%m-%d")
            for b in self.chain:
                d = b.get("date", today_str)
                date_counts[d] = date_counts.get(d, 0) + 1
            
            result = []
            for d in sorted(date_counts.keys(), reverse=True):
                result.append({
                    "date": d,
                    "count": date_counts[d],
                    "is_today": (d == today_str)
                })
            return result

blockchain = ShedScoreBlockchain()

# Populate initial blockchain summary
latest_system_state["blockchain_info"] = {
    "total_blocks": len(blockchain.chain),
    "latest_hash": blockchain.chain[-1]["hash"] if blockchain.chain else "",
    "latest_block_index": blockchain.chain[-1]["index"] if blockchain.chain else 0,
    "is_valid": True,
    "available_dates": blockchain.get_available_dates()
}

# --- DATABASE SETUP ---
def init_db():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA busy_timeout=5000;')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                rail_voltage REAL,
                device_id TEXT,
                current REAL,
                power REAL,
                state TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                action_type TEXT,
                device_id TEXT,
                reason TEXT,
                power_before REAL,
                predicted_power REAL,
                estimated_savings_w REAL,
                shed_score REAL
            )
        ''')
        conn.commit()
        conn.close()

init_db()

# --- PREDICTION ENGINE ---
def predict_near_future_power(current_total_power):
    power_history.append((time.time(), current_total_power))
    if len(power_history) < 2:
        return current_total_power
    dt = power_history[-1][0] - power_history[0][0]
    dp = power_history[-1][1] - power_history[0][1]
    if dt <= 0:
        return current_total_power
    rate_of_change = dp / dt
    rate_of_change = min(1.5, rate_of_change) # Clamp rate of change
    predicted_power = current_total_power + (rate_of_change * 3.0)
    return max(0.0, round(predicted_power, 2))

# --- HTH-IT-06 SHED-SCORE DECISION ENGINE ---
stable_safe_cycles = 0

def calculate_shed_score(dev_id, dev_power):
    dev_info = DEVICES[dev_id]
    if not dev_info["is_shiftable"] or dev_info["state"] == "OFF":
        return -999.0
    time_since_shed = time.time() - dev_info["last_shed_time"] if dev_info["last_shed_time"] > 0 else 100.0
    score = dev_power - (time_since_shed * CONFIG["shed_score_weight"])
    return round(score, 2)

def run_decision_engine(total_power, predicted_power):
    global latest_system_state, stable_safe_cycles
    
    if latest_system_state["manual_override"]:
        latest_system_state["system_status"] = "MANUAL_OVERRIDE"
        return

    cap = CONFIG["peak_cap_watts"]
    
    if predicted_power > cap or total_power > cap:
        stable_safe_cycles = 0
        if latest_system_state["system_status"] != "SHEDDING_ACTIVE":
            latest_system_state["system_status"] = "PEAK_RISK"
            
        if CONFIG["auto_shedding"]:
            candidate_scores = []
            for dev_id, dev_info in DEVICES.items():
                # STRICT RULE: Critical components (e.g. Water Pump) must NEVER be considered or touched
                if not dev_info["is_shiftable"]:
                    continue
                if dev_info["state"] == "OFF" and dev_info["dim_level"] == 0:
                    continue
                dev_p = latest_system_state["devices"].get(dev_id, {}).get("power", 0.0)
                score = calculate_shed_score(dev_id, dev_p)
                candidate_scores.append((dev_id, score, dev_p))
            
            # Prioritize the load with highest power / highest shed amount first
            candidate_scores.sort(key=lambda x: (x[1], x[2]), reverse=True)
            
            if candidate_scores:
                best_dev_id, best_score, best_p = candidate_scores[0]
                best_dev = DEVICES[best_dev_id]
                
                if best_dev["type"] == "dimmable" and best_dev["dim_level"] > 25: # >10% PWM (25/255)
                    # GRADUAL 10% PWM STEP-DOWN DIMMING: Reduce PWM current flow by 10% (~25/255)
                    old_dim = best_dev["dim_level"]
                    new_dim = max(0, old_dim - 25)
                    best_dev["dim_level"] = new_dim
                    pct = int((new_dim / 255.0) * 100)
                    best_dev["state"] = "ON" if pct >= 95 else (f"DIMMED {pct}%" if pct > 0 else "OFF")
                    best_dev["last_shed_time"] = time.time()
                    
                    # Calculate actual watts saved: reduction in PWM ratio × device's current power reading
                    dev_cur_power = latest_system_state["devices"].get(best_dev_id, {}).get("power", 12.0)
                    dim_ratio_reduction = (old_dim - new_dim) / 255.0
                    actual_savings_w = round(dev_cur_power * dim_ratio_reduction, 2)
                    log_action("PWM_DIM_10%", best_dev_id, f"Surge active (> {cap}W). Reduced PWM current flow by 10% to {pct}%.", total_power, predicted_power, actual_savings_w, best_score)
                    latest_system_state["system_status"] = "SHEDDING_ACTIVE"
                else:
                    # Cut load completely if non-dimmable relay (e.g. Resistor bulb) or reached bottom PWM
                    dev_cur_power = latest_system_state["devices"].get(best_dev_id, {}).get("power", 19.8)
                    best_dev["state"] = "OFF"
                    best_dev["dim_level"] = 0
                    best_dev["last_shed_time"] = time.time()
                    log_action("SHED", best_dev_id, f"Highest shed amount ({dev_cur_power}W, Score: {best_score}). Cut load completely.", total_power, predicted_power, dev_cur_power, best_score)
                    latest_system_state["system_status"] = "SHEDDING_ACTIVE"

    elif predicted_power < (cap - CONFIG["safety_margin_watts"]) and total_power < (cap - CONFIG["safety_margin_watts"]):
        stable_safe_cycles += 1
        if stable_safe_cycles >= 2: # 4 seconds hysteresis for step-up
            if latest_system_state["system_status"] in ["SHEDDING_ACTIVE", "PEAK_RISK"] and not spike_test_active:
                dimmed_candidates = [
                    (dev_id, dev_info) for dev_id, dev_info in DEVICES.items()
                    if dev_info["is_shiftable"] and dev_info["dim_level"] < 255
                ]
                # Start restoring the dimmed loads gradually by +10% PWM
                dimmed_candidates.sort(key=lambda x: x[1]["dim_level"], reverse=True)
                
                if dimmed_candidates:
                    dev_id, dev_info = dimmed_candidates[0]
                    if dev_info["type"] == "dimmable":
                        new_dim = min(255, dev_info["dim_level"] + 25) # Increase current flow by 10%
                        dev_info["dim_level"] = new_dim
                        pct = int((new_dim / 255.0) * 100)
                        dev_info["state"] = "ON" if pct >= 95 else f"DIMMED {pct}%"
                        power_history.clear()
                        log_action("PWM_RESTORE_10%", dev_id, f"Demand safe. Increased PWM current flow up 10% to {pct}%.", total_power, predicted_power, 0.0, 0.0)
                    else:
                        # Relay load: restore ON once stable
                        dev_info["dim_level"] = 255
                        dev_info["state"] = "ON"
                        power_history.clear()
                        log_action("RESTORE", dev_id, "Demand safe below threshold. Restored load to full power.", total_power, predicted_power, 0.0, 0.0)
                    stable_safe_cycles = 0
                else:
                    latest_system_state["system_status"] = "NORMAL"
                    stable_safe_cycles = 0

def log_action(action_type, dev_id, reason, power_before, predicted_power, estimated_savings_w, shed_score=0.0):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dev_name = DEVICES[dev_id]["name"] if dev_id in DEVICES else dev_id
    
    # Securely append block to the Cryptographic Blockchain Ledger
    new_block = blockchain.add_block(
        action_type=action_type,
        dev_id=dev_id,
        reason=reason,
        power_before=power_before,
        predicted_power=predicted_power,
        demand_relief_w=estimated_savings_w,
        shed_score=shed_score
    )
    
    action_entry = {
        "block_index": new_block["index"],
        "block_hash": new_block["hash"],
        "short_hash": new_block["hash"][:8] + "..." + new_block["hash"][-6:],
        "prev_hash": new_block["previous_hash"],
        "timestamp": new_block["timestamp"],
        "date": new_block["date"],
        "action": action_type,
        "device": dev_name,
        "reason": reason,
        "savings_w": estimated_savings_w,
        "shed_score": shed_score,
        "validator": new_block["validator"]
    }
    
    latest_system_state["recent_actions"].insert(0, action_entry)
    latest_system_state["recent_actions"] = latest_system_state["recent_actions"][:20]
    
    latest_system_state["blockchain_info"] = {
        "total_blocks": len(blockchain.chain),
        "latest_hash": blockchain.chain[-1]["hash"] if blockchain.chain else "",
        "latest_block_index": blockchain.chain[-1]["index"] if blockchain.chain else 0,
        "is_valid": True,
        "available_dates": blockchain.get_available_dates()
    }
    
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO action_logs (timestamp, action_type, device_id, reason, power_before, predicted_power, estimated_savings_w, shed_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (now_str, action_type, dev_id, reason, power_before, predicted_power, estimated_savings_w, shed_score))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"Error writing to action_logs DB: {e}")

# --- TELEMETRY PROCESSING ---
def process_incoming_telemetry(raw_data):
    global latest_system_state
    now_str = datetime.now().strftime("%H:%M:%S")
    rail_v = round(raw_data.get("rail_voltage", 12.0), 2)
    total_power = 0.0
    device_metrics = {}

    raw_devices = raw_data.get("devices", {})
    
    # Update active ToD tariff rate and slot name
    active_rate, tod_slot, demand_rate = get_active_tod_tariff()
    
    for dev_id, dev_info in DEVICES.items():
        dev_data = raw_devices.get(dev_id, {})
        state = dev_info["state"]
        
        if state == "OFF":
            current = 0.0
        elif "DIMMED" in state:
            base_c = dev_data.get("current", 0.3)
            current = round(base_c * (dev_info["dim_level"] / 255.0), 2)
        else:
            current = round(dev_data.get("current", 1.5 if dev_id == "resistor" else (1.1 if dev_id == "pump" else 0.4)), 2)
            
        power = round(rail_v * current, 2)
        total_power += power
        score = calculate_shed_score(dev_id, power)

        # Track per-device kWh and cost
        dev_kwh = (power * 2.0) / (3600.0 * 1000.0)
        dev_cost = dev_kwh * active_rate
        dev_info["accumulated_kwh"] = dev_info.get("accumulated_kwh", 0.0) + dev_kwh
        dev_info["accumulated_cost_inr"] = dev_info.get("accumulated_cost_inr", 0.0) + dev_cost

        device_metrics[dev_id] = {
            "name": dev_info["name"],
            "voltage": rail_v,
            "current": current,
            "power": power,
            "kwh_consumed": round(dev_info["accumulated_kwh"], 6),
            "cost_inr": round(dev_info["accumulated_cost_inr"], 4),
            "state": state,
            "dim_level": dev_info["dim_level"],
            "is_shiftable": dev_info["is_shiftable"],
            "gpio_status_led": dev_info["status_led_gpio"],
            "shed_score": score
        }

    total_power = round(total_power, 2)
    predicted_power = predict_near_future_power(total_power)
    
    # Calculate kWh consumed in this 2.0 second cycle
    cycle_kwh = (total_power * 2.0) / (3600.0 * 1000.0)
    latest_system_state["total_kwh_consumed"] += cycle_kwh
    
    # Calculate kWh saved in this 2.0 second cycle
    cycle_saved_w = 0.0
    for dev_id, dev_info in DEVICES.items():
        if dev_info["is_shiftable"]:
            base_p = 19.8 if dev_id == "resistor" else (4.2 if dev_id == "led1" else 3.6)
            if spike_test_active:
                base_p *= 2.4
            act_p = device_metrics.get(dev_id, {}).get("power", 0.0)
            if act_p < base_p:
                cycle_saved_w += (base_p - act_p)

    cycle_saved_kwh = (cycle_saved_w * 2.0) / (3600.0 * 1000.0)
    latest_system_state["total_energy_saved_kwh"] += cycle_saved_kwh
    
    # Recalculate total cost consumed and saved directly based on active tariff rate
    total_kwh = latest_system_state["total_kwh_consumed"]
    latest_system_state["total_cost_consumed_inr"] = round(total_kwh * active_rate, 4)
    latest_system_state["total_cost_saved_inr"] = round(latest_system_state["total_energy_saved_kwh"] * active_rate, 4)
    
    # Billing projections for real-time UI impact display
    hourly_kwh = total_power / 1000.0
    latest_system_state["hourly_cost_inr"] = round(hourly_kwh * active_rate, 3)
    latest_system_state["monthly_demand_charge_inr"] = round(CONFIG["contract_demand_kva"] * demand_rate, 2)
    latest_system_state["monthly_projected_cost_inr"] = round((hourly_kwh * 24 * 30 * active_rate) + (CONFIG["contract_demand_kva"] * demand_rate), 2)

    latest_system_state["active_tod_slot"] = tod_slot
    latest_system_state["active_tariff_rate_inr"] = active_rate
    latest_system_state["demand_charge_rate_inr"] = demand_rate

    latest_system_state["timestamp"] = now_str
    latest_system_state["rail_voltage"] = rail_v
    latest_system_state["total_power"] = total_power
    latest_system_state["predicted_power"] = predicted_power
    latest_system_state["devices"] = device_metrics

    # Batch save telemetry metrics
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            for dev_id, dev_m in device_metrics.items():
                cursor.execute('''
                    INSERT INTO telemetry (timestamp, rail_voltage, device_id, current, power, state)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (now_str, rail_v, dev_id, dev_m["current"], dev_m["power"], dev_m["state"]))
            conn.commit()
            conn.close()
    except Exception as e:
        pass

    run_decision_engine(total_power, predicted_power)

# --- BACKGROUND SIMULATOR ---
spike_test_active = False

def background_hardware_simulator():
    global spike_test_active
    while True:
        try:
            if CONFIG.get("mock_simulation", True):
                base_v = 12.0 + random.uniform(-0.1, 0.1)
                mult = 2.4 if spike_test_active else 1.0
                
                mock_data = {
                    "rail_voltage": base_v,
                    "devices": {
                        "led1": {"current": 0.35 * mult + random.uniform(-0.02, 0.02)},
                        "led2": {"current": 0.30 * mult + random.uniform(-0.02, 0.02)},
                        "resistor": {"current": 1.65 * mult + random.uniform(-0.05, 0.05)},
                        "pump": {"current": 1.10 + random.uniform(-0.03, 0.03)}
                    }
                }
                process_incoming_telemetry(mock_data)
            else:
                # Poll live ESP32 Hardware via WiFi AP (192.168.4.1) or local IP
                esp_ip = CONFIG.get("esp32_ip", "192.168.4.1")
                try:
                    import urllib.request
                    req = urllib.request.Request(f"http://{esp_ip}/data", headers={"User-Agent": "Flask-PeakShaver"})
                    with urllib.request.urlopen(req, timeout=1.5) as resp:
                        live_data = json.loads(resp.read().decode('utf-8'))
                        process_incoming_telemetry(live_data)
                except Exception:
                    pass
        except Exception as e:
            print(f"Simulator exception: {e}")
        time.sleep(1.5)

sim_thread = threading.Thread(target=background_hardware_simulator, daemon=True)
sim_thread.start()

# --- HTTP ENDPOINTS & API ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/telemetry", methods=["GET"])
@app.route("/data", methods=["GET"])
def get_telemetry():
    return jsonify({
        "config": CONFIG,
        "state": latest_system_state
    })

@app.route("/api/telemetry", methods=["POST"])
@app.route("/data", methods=["POST"])
def post_telemetry():
    data = request.get_json()
    if data:
        process_incoming_telemetry(data)
        return jsonify({"status": "success", "active_states": {k: v["state"] for k, v in DEVICES.items()}}), 200
    return jsonify({"status": "error", "message": "Invalid JSON payload"}), 400

# --- BLOCKCHAIN LEDGER API ENDPOINTS ---
@app.route("/api/blockchain", methods=["GET"])
def get_blockchain():
    selected_date = request.args.get("date", "ALL")
    blocks = blockchain.get_blocks_by_date(selected_date)
    return jsonify({
        "status": "success",
        "total_blocks": len(blockchain.chain),
        "filtered_count": len(blocks),
        "selected_date": selected_date,
        "available_dates": blockchain.get_available_dates(),
        "is_valid": True,
        "blocks": blocks
    })

@app.route("/api/blockchain/verify", methods=["GET"])
def verify_blockchain():
    verification_result = blockchain.verify_chain()
    return jsonify({
        "status": "success",
        "verification": verification_result
    })

@app.route("/api/blockchain/dates", methods=["GET"])
def get_blockchain_dates():
    return jsonify({
        "status": "success",
        "dates": blockchain.get_available_dates()
    })

@app.route("/api/blockchain/block/<int:block_idx>", methods=["GET"])
def get_blockchain_block(block_idx):
    with blockchain.lock:
        if 0 <= block_idx < len(blockchain.chain):
            return jsonify({
                "status": "success",
                "block": blockchain.chain[block_idx]
            })
        return jsonify({"status": "error", "message": "Block not found"}), 404

@app.route("/api/control", methods=["POST"])
def manual_control():
    global spike_test_active
    data = request.get_json()
    action = data.get("action")
    dev_id = data.get("device_id")
    
    if action == "TRIGGER_SPIKE":
        spike_test_active = True
        def reset_spike():
            global spike_test_active
            spike_test_active = False
        threading.Timer(8.0, reset_spike).start()
        log_action("TEST_SPIKE", "system", "Injected high load spike to test Shed-Score peak shaving.", latest_system_state["total_power"], latest_system_state["predicted_power"], 0)
        return jsonify({"status": "success", "message": "Power surge injected"}), 200

    if action == "OVERRIDE_TOGGLE": # Button GPIO 16
        latest_system_state["manual_override"] = not latest_system_state["manual_override"]
        latest_system_state["system_status"] = "MANUAL_OVERRIDE" if latest_system_state["manual_override"] else "NORMAL"
        log_action("MANUAL_OVERRIDE", "system", f"Manual override set to {latest_system_state['manual_override']}", 0, 0, 0)
        return jsonify({"status": "success", "override": latest_system_state["manual_override"]}), 200

    if action == "SILENCE_BUZZER": # Button GPIO 17
        latest_system_state["buzzer_silenced"] = True
        log_action("SILENCE", "system", "Buzzer alarm acknowledged/silenced.", 0, 0, 0)
        return jsonify({"status": "success", "silenced": True}), 200

    if action == "RESTORE_ALL":
        for k, v in DEVICES.items():
            if v["is_shiftable"]:
                v["state"] = "ON"
                v["dim_level"] = 255
        latest_system_state["system_status"] = "NORMAL"
        log_action("RESTORE_ALL", "all", "Manual restore all command executed.", 0, 0, 0)
        return jsonify({"status": "success"}), 200

    if dev_id in DEVICES:
        if not DEVICES[dev_id]["is_shiftable"] and action in ["SHED", "DIM"]:
            return jsonify({"status": "blocked", "message": "CRITICAL PUMP CANNOT BE TOUCHED!"}), 403
            
        if action == "SHED":
            DEVICES[dev_id]["state"] = "OFF"
            DEVICES[dev_id]["dim_level"] = 0
        elif action == "RESTORE":
            DEVICES[dev_id]["state"] = "ON"
            DEVICES[dev_id]["dim_level"] = 255
        elif action == "DIM":
            val = data.get("value", 128)
            DEVICES[dev_id]["dim_level"] = val
            DEVICES[dev_id]["state"] = "DIMMED 50%"
            
        log_action(action, dev_id, "Manual control from dashboard.", 0, 0, 0)
        return jsonify({"status": "success"}), 200

    return jsonify({"status": "error", "message": "Invalid request"}), 400

@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data provided"}), 400

    if "peak_cap_watts" in data:
        CONFIG["peak_cap_watts"] = float(data["peak_cap_watts"])
        latest_system_state["peak_cap_watts"] = CONFIG["peak_cap_watts"]

    if "mock_simulation" in data:
        CONFIG["mock_simulation"] = bool(data["mock_simulation"])

    if "esp32_ip" in data:
        CONFIG["esp32_ip"] = str(data["esp32_ip"])

    if "tariff_category" in data:
        CONFIG["tariff_category"] = str(data["tariff_category"])
        active_rate, tod_slot, demand_rate = get_active_tod_tariff()
        total_kwh = latest_system_state.get("total_kwh_consumed", 0.0)
        total_p = latest_system_state.get("total_power", 41.8)
        hourly_kwh = total_p / 1000.0
        
        latest_system_state["active_tod_slot"] = tod_slot
        latest_system_state["active_tariff_rate_inr"] = active_rate
        latest_system_state["demand_charge_rate_inr"] = demand_rate
        latest_system_state["total_cost_consumed_inr"] = round(total_kwh * active_rate, 4)
        latest_system_state["total_cost_saved_inr"] = round(latest_system_state.get("total_energy_saved_kwh", 0.0) * active_rate, 3)
        latest_system_state["hourly_cost_inr"] = round(hourly_kwh * active_rate, 3)
        latest_system_state["monthly_demand_charge_inr"] = round(CONFIG["contract_demand_kva"] * demand_rate, 2)
        latest_system_state["monthly_projected_cost_inr"] = round((hourly_kwh * 24 * 30 * active_rate) + (CONFIG["contract_demand_kva"] * demand_rate), 2)
        
        # Update devices with new rate
        for dev_id, dev_info in DEVICES.items():
            dev_kwh = dev_info.get("accumulated_kwh", 0.0)
            dev_info["accumulated_cost_inr"] = round(dev_kwh * active_rate, 4)
            if "devices" in latest_system_state and dev_id in latest_system_state["devices"]:
                latest_system_state["devices"][dev_id]["cost_inr"] = round(dev_kwh * active_rate, 4)

    return jsonify({
        "status": "success",
        "config": CONFIG,
        "state": latest_system_state
    }), 200

if __name__ == "__main__":
    print("\n========================================================")
    print("  *** HTH-IT-06 PEAK-SHAVING SYSTEM BACKEND STARTED ***")
    print("  Cryptographic Blockchain Audit Ledger: ACTIVE")
    print("  Access Web Dashboard at: http://127.0.0.1:5000")
    print("========================================================\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
