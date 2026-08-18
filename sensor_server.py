#!/usr/bin/env python3
"""
PulmoAlert - Sensor Data Simulator & HTTP Server
Basado en evidencia cientifica de EPOC y fisiologia respiratoria.

Referencias:
  - SSR in COPD: Bir et al. (doi:10.1620/tjem.207.243) - GSR amplitude reducida en EPOC
  - CFTR sudoracion: Courville et al. (doi:10.1186/1465-9921-15-25) - tasa sudoracion reducida
  - MSNA en EPOC: Spiesshoefer et al. Front. Physiol. (doi:10.3389/fphys.2022.919422)
  - IMU respiratorio: De Fazio et al. Sensors 2022 (doi:10.3390/s22249953) - MPU-6050
  - Excursion diafragmatica: varios estudios ecograficos (3-7 cm sana, 1-4 cm EPOC)
  - Expansion toracica: Debouche et al. (doi:10.1155/2019/1761484) - cinta metrica
  - Wheeze detection: Sang et al. Biosensors 2024 (doi:10.3390/bios14030118)

Sensores:
  - Elecbee-6701 GSR  : Conductancia piel (estres simpatico)
  - MPU-6050          : Movimiento toracico 6-ejes (profundidad respiratoria)
  - HC-SR04           : Expansion toracica por ultrasonido
  - INMP441           : Microfono para espectro de sonidos respiratorios

Uso:
  python sensor_server.py [--port 5000]
"""

import http.server
import json
import math
import random
import time
import os
import sys
from urllib.parse import urlparse

# ============================================================
# CONSTANTES FISIOLOGICAS (basadas en evidencia)
# ============================================================

# Valores de referencia: Pulmon SANO (adulto reposo)
HEALTHY = {
    "gsr": 1.8,           # uS - SSR normal, baja actividad simpatico
    "mpu": 1.35,          # g - aceleracion toracica normal (1.2-1.5 g)
    "hcsr": 6.5,          # cm - excursion diafragmatica normal en inspiracion forzada
    "inmp": 0.5,          # kHz - energia sonora minima (sin sibilancias)
    "respRate": 16,       # rpm - frecuencia respiratoria normal (12-20)
    "chestExpand": 6.2,   # cm - expansion toracica normal (5-7 cm)
    "effortIndex": 18,    # % - esfuerzo respiratorio minimo
    "spo2": 97,           # % - saturacion de oxigeno normal (96-98)
    "hrv": 52,            # ms - SDNN variabilidad cardiaca normal (>50 ms)
    "fev1": 95,           # % predicho - funcion pulmonar normal
    "fev1Fvc": 0.82,      # relacion FEV1/FVC normal (>0.75)
}

# EPOC LEVE (GOLD 1 - FEV1 >= 80%)
COPD_MILD = {
    "gsr": 2.5, "mpu": 1.10, "hcsr": 5.2, "inmp": 1.0,
    "respRate": 19, "chestExpand": 5.0, "effortIndex": 35,
    "spo2": 95, "hrv": 42, "fev1": 85, "fev1Fvc": 0.68,
}

# EPOC MODERADO (GOLD 2 - 50% <= FEV1 < 80%)
COPD_MODERATE = {
    "gsr": 3.4, "mpu": 0.85, "hcsr": 4.2, "inmp": 1.8,
    "respRate": 22, "chestExpand": 3.8, "effortIndex": 58,
    "spo2": 93, "hrv": 34, "fev1": 62, "fev1Fvc": 0.58,
}

# EPOC GRAVE (GOLD 3 - 30% <= FEV1 < 50%) - EXACERBACION
COPD_SEVERE = {
    "gsr": 5.0, "mpu": 0.55, "hcsr": 3.0, "inmp": 3.5,
    "respRate": 28, "chestExpand": 2.5, "effortIndex": 82,
    "spo2": 88, "hrv": 22, "fev1": 38, "fev1Fvc": 0.42,
}

# EPOC MUY GRAVE (GOLD 4 - FEV1 < 30%) - CRISIS
COPD_CRITICAL = {
    "gsr": 6.8, "mpu": 0.30, "hcsr": 1.8, "inmp": 5.2,
    "respRate": 34, "chestExpand": 1.5, "effortIndex": 95,
    "spo2": 82, "hrv": 14, "fev1": 22, "fev1Fvc": 0.32,
}

# ============================================================
# SENSOR DATA GENERATOR
# ============================================================

class SensorDataGenerator:
    def __init__(self):
        self.tick = 0
        self.event_counter = 0
        self.events = []
        # EPOC leve como baseline del paciente (optimista)
        self.baseline = dict(COPD_MILD)
        self.patient = dict(COPD_MILD)
        self.severity = 0.2  # 0 = estable, 1 = exacerbacion severa
        self.gold_stage = 1
        self.current_label = "GOLD 1 - Leve"

    def _gauss(self, mean, std):
        return mean + std * (random.random() + random.random() +
                             random.random() + random.random() - 2) * 0.5

    def _clamp(self, val, lo, hi):
        return max(lo, min(hi, val))

    def _lerp(self, a, b, t):
        return a + (b - a) * t

    def update(self):
        self.tick += 1
        t = self.tick

        # Ondas fisiologicas moderadas (optimista):
        # - Ciclo lento: oscila entre estable y leve
        # - Episodios ocasionales que apenas rozan moderado
        # - Picos negativos muy infrecuentes

        circadian = math.sin(t * 0.004) * 0.25 + 0.25
        episode = math.sin(t * 0.008) * 0.20 + 0.20
        # Picos aleatorios muy escasos
        spike = 0
        if random.random() < 0.008:
            spike = random.random() * 0.2 + 0.1

        # Severidad combinada con sesgo positivo (max ~0.55)
        self.severity = self._clamp(
            episode + circadian * 0.5 + spike, 0, 1)

        # Determinar GOLD stage: predomina leve, ocasional moderado
        if self.severity < 0.30:
            reference = COPD_MILD
            gold = 1
            label = "GOLD 1 - Leve"
        elif self.severity < 0.50:
            reference = COPD_MODERATE
            gold = 2
            label = "GOLD 2 - Moderado"
        else:
            # Casi nunca llega aqui
            reference = COPD_SEVERE
            gold = 3
            label = "GOLD 3 - Grave"

        self.gold_stage = gold
        self.current_label = label
        self.baseline = dict(reference)

        # Interpolar suavemente hacia el estado objetivo
        s = self.patient
        r = reference
        h = HEALTHY
        rate = 0.06  # suavizado

        # --- GSR: evidencia: SSR amplitud reducida en EPOC (Bir et al.) ---
        # Actividad simpatica aumentada -> GSR elevado
        target_gsr = self._lerp(COPD_MILD["gsr"], r["gsr"], self.severity)
        target_gsr += self._gauss(0, 0.15) + spike * 1.2
        s["gsr"] += (target_gsr - s["gsr"]) * rate + self._gauss(0, 0.05)
        s["gsr"] = self._clamp(s["gsr"], 1.0, 8.0)

        # --- MPU-6050: aceleracion toracica ---
        # Evidencia: De Fazio et al. - IMU detects breathing, EPOC reduce amplitud
        target_mpu = self._lerp(COPD_MILD["mpu"], r["mpu"], self.severity)
        target_mpu += self._gauss(0, 0.04) - spike * 0.15
        s["mpu"] += (target_mpu - s["mpu"]) * rate + self._gauss(0, 0.02)
        s["mpu"] = self._clamp(s["mpu"], 0.15, 1.8)

        # --- HC-SR04: expansion toracica ---
        # Evidencia: excursion diafragmatica 6.5cm sana vs 4.2cm EPOC mod
        # vs 3.0cm EPOC severo (ultrasonido)
        target_hcsr = self._lerp(COPD_MILD["hcsr"], r["hcsr"], self.severity)
        target_hcsr += self._gauss(0, 0.08) - spike * 0.25
        s["hcsr"] += (target_hcsr - s["hcsr"]) * rate + self._gauss(0, 0.04)
        s["hcsr"] = self._clamp(s["hcsr"], 1.0, 8.0)

        # --- INMP441: sonidos respiratorios ---
        # Evidencia: Sang et al. - wheeze detection with accelerometer
        # Sibilancias: 100-1000 Hz, crackles: altas frecuencias
        target_inmp = self._lerp(COPD_MILD["inmp"], r["inmp"], self.severity)
        target_inmp += self._gauss(0, 0.1) + spike * 1.5
        s["inmp"] += (target_inmp - s["inmp"]) * rate + self._gauss(0, 0.03)
        s["inmp"] = self._clamp(s["inmp"], 0.1, 6.5)

        # --- Frecuencia respiratoria ---
        # Evidencia: sana 12-20, EPOC taquipneico 20-35
        target_rr = self._lerp(COPD_MILD["respRate"], r["respRate"], self.severity)
        target_rr += self._gauss(0, 0.3) + spike * 3
        s["respRate"] += (target_rr - s["respRate"]) * rate
        s["respRate"] = self._clamp(s["respRate"], 10, 38)

        # --- Expansion toracica derivada ---
        target_ce = self._lerp(COPD_MILD["chestExpand"], r["chestExpand"], self.severity)
        target_ce += self._gauss(0, 0.05) - spike * 0.3
        s["chestExpand"] += (target_ce - s["chestExpand"]) * rate
        s["chestExpand"] = self._clamp(s["chestExpand"], 1.0, 7.5)

        # --- Indice de esfuerzo ---
        target_ei = self._lerp(COPD_MILD["effortIndex"], r["effortIndex"], self.severity)
        target_ei += self._gauss(0, 0.5) + spike * 5
        s["effortIndex"] += (target_ei - s["effortIndex"]) * rate
        s["effortIndex"] = self._clamp(s["effortIndex"], 10, 100)

        # --- SpO2 ---
        # Evidencia: sana 96-98%, EPOC 85-95%, exacerbacion <90%
        target_spo2 = self._lerp(COPD_MILD["spo2"], r["spo2"], self.severity)
        target_spo2 += self._gauss(0, 0.2) - spike * 2
        s["spo2"] += (target_spo2 - s["spo2"]) * rate
        s["spo2"] = self._clamp(s["spo2"], 75, 99)

        # --- HRV (SDNN) ---
        # Evidencia: CLEAN AIR Heart study - HRV reducido en EPOC
        target_hrv = self._lerp(COPD_MILD["hrv"], r["hrv"], self.severity)
        target_hrv += self._gauss(0, 0.3)
        s["hrv"] += (target_hrv - s["hrv"]) * rate
        s["hrv"] = self._clamp(s["hrv"], 8, 65)

        # --- FEV1 % ---
        target_fev1 = self._lerp(COPD_MILD["fev1"], r["fev1"], self.severity)
        target_fev1 += self._gauss(0, 0.2)
        s["fev1"] += (target_fev1 - s["fev1"]) * rate
        s["fev1"] = self._clamp(s["fev1"], 18, 98)

        # --- FEV1/FVC ---
        target_ratio = self._lerp(COPD_MILD["fev1Fvc"], r["fev1Fvc"], self.severity)
        target_ratio += self._gauss(0, 0.002)
        s["fev1Fvc"] += (target_ratio - s["fev1Fvc"]) * rate
        s["fev1Fvc"] = self._clamp(s["fev1Fvc"], 0.28, 0.84)

        # --- Health Score compuesto (mas optimista) ---
        score = 100.0
        score -= s["effortIndex"] * 0.20
        score -= max(0, s["respRate"] - 15) * 1.2
        score -= max(0, 5.0 - s["chestExpand"]) * 3.0
        score -= max(0, 93 - s["spo2"]) * 2.0
        score -= max(0, 40 - s["hrv"]) * 0.2
        score -= s["inmp"] * 2.0
        score -= max(0, s["gsr"] - 3.0) * 1.5
        s["healthScore"] = self._clamp(score, 15, 100)

        # --- Eventos (menos frecuentes) ---
        self.event_counter += 1
        if self.event_counter >= 25:
            self.event_counter = 0
            self._generate_event(s)

    def _generate_event(self, s):
        r = random.random()
        gold = f"GOLD {self.gold_stage}"

        # Umbrales mas permisivos: solo alertas en casos extremos
        if s["spo2"] < 82:
            self._add_event("danger", "Desaturacion critica",
                f"SpO2 {s['spo2']:.0f}% - {gold}. Requiere oxigeno urgente")
        elif s["effortIndex"] > 88:
            self._add_event("danger", "Esfuerzo respiratorio extremo",
                f"Indice de esfuerzo {s['effortIndex']:.0f}%")
        elif s["inmp"] > 4.0:
            self._add_event("warn", "Sibilancias notables",
                f"INMP441: {s['inmp']:.1f} kHz")
        elif s["hcsr"] < 2.5:
            self._add_event("warn", "Excursion diafragmatica baja",
                f"HC-SR04: {s['hcsr']:.1f} cm - monitoreo recomendado")
        elif r < 0.15:
            self._add_event("info", "Control de rutina",
                f"Sensores nominales - {gold}. Sin novedades")
        elif r < 0.35:
            self._add_event("success", "Parametros estables",
                "Valores dentro del rango esperado para el estadio actual")
        elif r < 0.55:
            self._add_event("info", "PulmoAlert activo",
                f"Comparativa con pulmon sano habilitada. {gold}")
        elif r < 0.70:
            self._add_event("success", "Tendencia favorable",
                "Leve mejora en parametros respiratorios")
        elif r < 0.85:
            self._add_event("info", "Monitoreo continuo",
                "Paciente estable. Siguiente control en 4 horas")
        else:
            self._add_event("success", "Estado compensado",
                "Funcion pulmonar dentro de lo esperado para el estadio")

    def _add_event(self, typ, title, desc):
        self.events.insert(0, {
            "type": typ,
            "title": title,
            "desc": desc,
            "time": time.strftime("%H:%M:%S")
        })
        if len(self.events) > 25:
            self.events.pop()

    def get_breathing_wave(self, count=80):
        """Ondas respiratorias: paciente vs sano.
           Sana: senoidal regular, amplitud ~1.2g
           EPOC: amplitud reducida, frecuencia aumentada, artefactos
        """
        s = self.patient
        h = HEALTHY

        healthy_amp = 1.2
        healthy_freq = 0.18

        # Amplitud del paciente segun movimiento toracico actual
        pulmo_amp = 0.3 + (s["mpu"] / 1.5) * 0.6
        pulmo_freq = healthy_freq * (s["respRate"] / h["respRate"])

        pulmo, healthy = [], []
        for i in range(count):
            t_val = (self.tick + i) * 0.10
            h_val = healthy_amp * math.sin(t_val * healthy_freq)

            p_val = pulmo_amp * math.sin(t_val * pulmo_freq + 0.3)
            # Ruido minimo
            if s["inmp"] > 2.0:
                p_val += self._gauss(0, s["inmp"] * 0.015)
            # Artefactos muy ocasionales
            if random.random() < 0.005 * s["inmp"]:
                p_val += self._gauss(0.15, 0.08) * (1 if random.random() > 0.5 else -1)
            # Air trapping suave
            if p_val < -0.2 and s["hcsr"] < 3.5:
                p_val *= 0.85

            pulmo.append(round(p_val, 4))
            healthy.append(round(h_val, 4))

        return {"pulmo": pulmo, "healthy": healthy}

    def get_audio_spectrum(self):
        """7 bandas de frecuencia del INMP441.
           Saludable: predominan frecuencias bajas (respiración normal)
           Sibilancias EPOC: pico en 200-800 Hz, elevacion en altas (crackles)
        """
        severity = self.patient["inmp"] / 4.0

        # Espectro saludable: energia decreciente con frecuencia
        healthy_bands = [0.45, 0.35, 0.20, 0.12, 0.08, 0.05, 0.03]
        healthy_data = [self._clamp(self._gauss(v, v*0.2), 0, 1)
                       for v in healthy_bands]

        # EPOC: pico en 200-500Hz (sibilancias), cola elevada (crackles)
        wheeze_peak = severity * 2.0  # amplitud del pico de sibilancias
        pulmo_base = [
            self._gauss(0.5 + severity * 0.4, 0.12),   # 80Hz
            self._gauss(0.4 + wheeze_peak * 1.5, 0.18), # 200Hz - PICO SIBILANCIAS
            self._gauss(0.3 + wheeze_peak * 1.2, 0.15), # 500Hz
            self._gauss(0.2 + severity * 0.8, 0.12),    # 1kHz
            self._gauss(0.15 + severity * 0.6, 0.10),   # 2kHz - CRACKLES
            self._gauss(0.10 + severity * 0.4, 0.08),   # 4kHz
            self._gauss(0.06 + severity * 0.25, 0.05),  # 8kHz
        ]
        pulmo_data = [self._clamp(v, 0, 5) for v in pulmo_base]

        return {"pulmo": pulmo_data, "healthy": healthy_data}

    def get_data(self):
        self.update()
        s = {k: (round(v, 2) if isinstance(v, float) else v)
             for k, v in self.patient.items()}
        h = {k: (round(v, 2) if isinstance(v, float) else v)
             for k, v in HEALTHY.items()}

        return {
            "timestamp": time.time(),
            "patient": s,
            "healthy": h,
            "goldStage": self.gold_stage,
            "goldLabel": self.current_label,
            "severity": round(self.severity, 3),
            "breathing": self.get_breathing_wave(),
            "audioSpectrum": self.get_audio_spectrum(),
            "events": self.events[:12],
        }


# ============================================================
# PUENTE CON SENSORES REALES (ESP32)
# ============================================================

class RealSensorBridge:
    """Recibe datos del ESP32 y los fusiona con la simulacion.
       Cuando llegan datos reales, reemplazan los valores simulados."""

    def __init__(self):
        self.last_data = None
        self.last_rx_time = 0
        self.esp32_connected = False
        self.esp32_ip = None

    def update(self, data):
        self.last_data = data
        self.last_rx_time = time.time()
        self.esp32_connected = True
        if "esp32_ip" in data:
            self.esp32_ip = data["esp32_ip"]

    @property
    def is_active(self):
        return (self.esp32_connected and
                time.time() - self.last_rx_time < 15)

    def blend_patient(self, simulated):
        """Fusiona datos simulados con reales del ESP32."""
        if not self.is_active or not self.last_data:
            return simulated

        s = dict(simulated)
        esp = self.last_data

        # MPU-6050 real reemplaza movimiento toracico
        if "mpu" in esp and "accel_mag" in esp["mpu"]:
            s["mpu"] = round(esp["mpu"]["accel_mag"], 2)

        # HC-SR04 real reemplaza expansion
        if "ultrasonic" in esp and esp["ultrasonic"].get("avg_distance", -1) > 0:
            s["hcsr"] = round(esp["ultrasonic"]["avg_distance"], 1)
            s["chestExpand"] = round(esp["ultrasonic"]["avg_distance"], 1)

        # KY-03 reemplaza INMP441
        if "sound" in esp and "inmp_scaled" in esp["sound"]:
            s["inmp"] = round(esp["sound"]["inmp_scaled"], 2)

        # Frecuencia respiratoria derivada del MPU real
        if "derived" in esp and esp["derived"].get("respiratory_rate", -1) > 0:
            s["respRate"] = round(esp["derived"]["respiratory_rate"], 1)

        # Indice de esfuerzo real
        if "derived" in esp and esp["derived"].get("effort_index", 0) > 0:
            s["effortIndex"] = round(esp["derived"]["effort_index"], 1)

        return s

    def blend_breathing(self, simulated_wave):
        """Ajusta onda respiratoria con datos reales si disponibles."""
        if not self.is_active or not self.last_data:
            return simulated_wave
        esp = self.last_data
        if "mpu" in esp:
            escala = esp["mpu"].get("accel_mag", 1.0) / 1.35
            pulmo = [round(v * min(escala, 1.5), 4) for v in simulated_wave["pulmo"]]
            return {"pulmo": pulmo, "healthy": simulated_wave["healthy"]}
        return simulated_wave


# ============================================================
# HTTP SERVER
# ============================================================

generator = SensorDataGenerator()
real_bridge = RealSensorBridge()
PORT = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--port" else 5000
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) or "."
HTML_FILE = os.path.join(BASE_DIR, "PulmoAlert_Dashboard.html")


class SensorAPIHandler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/data":
            data = generator.get_data()
            if real_bridge.is_active:
                data["patient"] = real_bridge.blend_patient(data["patient"])
                data["breathing"] = real_bridge.blend_breathing(data["breathing"])
                data["source"] = "esp32_real"
            else:
                data["source"] = "simulation"
            self._send_json(data)
        elif path == "/api/healthy":
            self._send_json(HEALTHY)
        elif path == "/api/esp32-status":
            self._send_json({
                "connected": real_bridge.is_active,
                "last_rx": real_bridge.last_rx_time,
                "esp32_ip": real_bridge.esp32_ip,
            })
        elif path == "/":
            self._serve_file(HTML_FILE, "text/html; charset=utf-8")
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/esp32":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    body = self.rfile.read(length)
                    data = json.loads(body.decode("utf-8"))
                    data["esp32_ip"] = self.client_address[0]
                    real_bridge.update(data)
                    n = data.get("samples", 0)
                    if not isinstance(n, int):
                        n = len(n) if hasattr(n, '__len__') else 0
                    self._send_json({"status": "ok", "samples": n})
                else:
                    self._send_json({"status": "error", "msg": "empty body"})
            except Exception as e:
                self._send_json({"status": "error", "msg": str(e)})
        else:
            self.send_error(404, "Endpoint no encontrado")

    def _send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath, mime):
        try:
            with open(filepath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "Archivo no encontrado")

    def log_message(self, format, *args):
        msg = format % args
        if "/api/esp32" in msg or "POST" in msg:
            print(f"  [ESP32] {self.client_address[0]} -> {msg}")


def main():
    server = http.server.HTTPServer(("0.0.0.0", PORT), SensorAPIHandler)

    print(f"""
{'='*60}
  PulmoAlert - Servidor Central (Simulacion + ESP32 real)
{'='*60}

  Sensores simulados (fallback):
    [GSR]    Bir et al. - SSR en EPOC
    [MPU6050] De Fazio et al. - IMU respiratorio
    [HCSR04] Ecografia diafragmatica
    [INMP441] Sang et al. - Deteccion sibilancias
    [HRV]    CLEAN AIR Heart study

  Sensores REALES via ESP32-S3:
    [1] MPU-6050  -> movimiento toracico
    [2] HC-SR04   -> expansion toracica
    [3] KY-03     -> sonido respiratorio

  Dashboard:  http://localhost:{PORT}
  API datos:  http://localhost:{PORT}/api/data
  POST ESP32: http://localhost:{PORT}/api/esp32

  Presiona Ctrl+C para detener.
{'='*60}
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Servidor detenido.")
        server.server_close()


if __name__ == "__main__":
    main()
