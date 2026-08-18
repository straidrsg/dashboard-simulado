"""
PulmoAlert - Firmware para ESP32-S3 Mini
Sensores reales: MPU-6050 (mov. toracico), HC-SR04 (expansion), KY-03 (sonido)
Comunicacion HTTP POST con servidor central (Marcos)

Conexiones:
  MPU-6050: SDA->GPIO1, SCL->GPIO2, VCC->3.3V, GND->GND
  HC-SR04:  TRIG->GPIO10, ECHO->GPIO11, VCC->5V, GND->GND
  KY-03:    A0->GPIO4, VCC->3.3V, GND->GND (salida analogica)

  LED indicador: GPIO15 (opcional)
"""

import network
import urequests
import ujson
import time
import math
from machine import Pin, I2C, ADC, Timer

# ============================================================
# CONFIGURACION
# ============================================================

# --- WiFi ---
WIFI_SSID = "MI_WIFI"
WIFI_PASS = "MI_CONTRASENA"

# --- Servidor (Marcos) ---
# IP del computador donde corre sensor_server.py
SERVER_HOST = "192.168.1.100"
SERVER_PORT = 5000
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/api/esp32"

# Intervalo de muestreo (ms)
SAMPLE_INTERVAL_MS = 200

# Limite de datos en buffer local antes de enviar
BUFFER_MAX = 25

# Sensibilidad KY-03 (ajustar segun entorno)
KY03_THRESHOLD = 0.15   # nivel minimo para considerar "sonido resp."
KY03_MAX_ADC = 4095     # ADC de 12 bits (0-4095)

# ============================================================
# INICIALIZACION DE SENSORES
# ============================================================

# --- LED indicador ---
led = Pin(15, Pin.OUT, value=0)

def blink(times=2, delay=0.1):
    for _ in range(times):
        led.value(1)
        time.sleep(delay)
        led.value(0)
        time.sleep(delay)

# --- I2C para MPU-6050 ---
i2c = I2C(0, scl=Pin(2), sda=Pin(1), freq=400000)
MPU_ADDR = 0x68

def mpu_init():
    """Inicializa MPU-6050: sale de sleep, configura rango."""
    try:
        i2c.writeto_mem(MPU_ADDR, 0x6B, b'\x00')  # PWR_MGMT: despertar
        i2c.writeto_mem(MPU_ADDR, 0x1C, b'\x00')  # ACCEL_CONFIG: +/-2g
        i2c.writeto_mem(MPU_ADDR, 0x1B, b'\x00')  # GYRO_CONFIG: +/-250deg/s
        return True
    except:
        return False

def mpu_read_raw(addr):
    """Lee 2 bytes de un registro MPU-6050 y convierte a int16."""
    try:
        data = i2c.readfrom_mem(MPU_ADDR, addr, 2)
        val = (data[0] << 8) | data[1]
        return val - 65536 if val >= 32768 else val
    except:
        return 0

def mpu_read():
    """Lee acelerometro (ax, ay, az) en g y giroscopio (gx, gy, gz) en deg/s."""
    ax = mpu_read_raw(0x3B) / 16384.0
    ay = mpu_read_raw(0x3D) / 16384.0
    az = mpu_read_raw(0x3F) / 16384.0
    gx = mpu_read_raw(0x43) / 131.0
    gy = mpu_read_raw(0x45) / 131.0
    gz = mpu_read_raw(0x47) / 131.0
    # Magnitud de aceleracion total (movimiento toracico)
    acc_mag = math.sqrt(ax*ax + ay*ay + az*az)
    return {
        "ax": round(ax, 3), "ay": round(ay, 3), "az": round(az, 3),
        "gx": round(gx, 1), "gy": round(gy, 1), "gz": round(gz, 1),
        "accel": round(acc_mag, 3),
    }

# --- HC-SR04 ---
TRIG = Pin(10, Pin.OUT, value=0)
ECHO = Pin(11, Pin.IN)
SOUND_SPEED = 0.0343  # cm/us

def hcsr04_read():
    """Mide distancia en cm con HC-SR04. Retorna -1 si timeout."""
    TRIG.value(0)
    time.sleep_us(2)
    TRIG.value(1)
    time.sleep_us(10)
    TRIG.value(0)

    timeout = time.ticks_us() + 30000  # 30ms timeout (~5m)
    while ECHO.value() == 0:
        if time.ticks_diff(time.ticks_us(), timeout) > 0:
            return -1
    start = time.ticks_us()

    timeout = time.ticks_us() + 30000
    while ECHO.value() == 1:
        if time.ticks_diff(time.ticks_us(), timeout) > 0:
            return -1
    end = time.ticks_us()

    duration = time.ticks_diff(end, start)
    distance = (duration * SOUND_SPEED) / 2
    return round(distance, 1)

# --- KY-03 (Microfono analogico) ---
mic_adc = ADC(Pin(4), atten=ADC.ATTN_11DB)  # 0-3.3V -> 0-4095

def ky03_read():
    """Lee nivel de sonido ambiental normalizado 0.0-1.0."""
    raw = mic_adc.read()
    # Offset DC: el ADC en silencio tipicamente da ~2048 (mitad de 3.3V/4095)
    # Tomamos la desviacion como indicador de sonido
    dc_offset = 2048
    deviation = abs(raw - dc_offset) / KY03_MAX_ADC
    # Normalizar y escalar
    level = min(1.0, deviation * 4.0)
    return {
        "raw": raw,
        "level": round(level, 4),
        "active": 1 if level > KY03_THRESHOLD else 0,
    }

# ============================================================
# RSUN
# ============================================================

def rsun(inicio, final):
    """Calcula frecuencia respiratoria desde picos de aceleracion."""
    picos = 0
    umbral = 0.15
    prev = inicio
    for i in range(1, len(final)):
        if final[i] > umbral and final[i-1] <= umbral:
            picos += 1
    duracion_seg = (len(final) * SAMPLE_INTERVAL_MS) / 1000.0
    if duracion_seg < 5:
        return None
    rpm = (picos / duracion_seg) * 60
    return round(rpm / 2, 1)  # /2 porque inspiracion+expiracion = 1 ciclo

# ============================================================
# BUFFER DE DATOS
# ============================================================

buffer = []
mpu_history = []  # para calcular RR

def add_reading(mpu, hcsr, ky3):
    """Agrega una lectura al buffer."""
    global buffer, mpu_history
    entry = {
        "t": time.time(),
        "mpu": mpu,
        "hcsr": hcsr,
        "ky03": ky3,
    }
    buffer.append(entry)
    mpu_history.append(mpu["accel"])

def build_payload():
    """Construye JSON con datos acumulados y metricas derivadas."""
    global buffer, mpu_history
    if not buffer:
        return None

    # Promedios
    acc_total = sum(e["mpu"]["accel"] for e in buffer) / len(buffer)
    hcsr_total = sum(e["hcsr"] for e in buffer if e["hcsr"] > 0) 
    hcsr_count = sum(1 for e in buffer if e["hcsr"] > 0)
    hcsr_avg = round(hcsr_total / hcsr_count, 1) if hcsr_count > 0 else -1
    ky03_avg = round(sum(e["ky03"]["level"] for e in buffer) / len(buffer), 3)
    ky03_active = sum(e["ky03"]["active"] for e in buffer)

    # Frecuencia respiratoria desde MPU
    resp_rate = rsun(0, mpu_history)

    # Indice de esfuerzo (proxy: que tan erratico es el movimiento)
    if len(mpu_history) > 5:
        diffs = [abs(mpu_history[i] - mpu_history[i-1]) for i in range(1, len(mpu_history))]
        effort = round(min(100, sum(diffs) / len(diffs) * 50), 1)
    else:
        effort = 0

    # Nivel de sonido (0-5 escala para comparar con INMP441 simulado)
    inmp_scaled = round(ky03_avg * 5.0, 2)

    ultimo = buffer[-1]

    payload = {
        "timestamp": ultimo["t"],
        "samples": len(buffer),
        "mpu": {
            "accel_mag": round(acc_total, 3),
            "ax_avg": round(sum(e["mpu"]["ax"] for e in buffer) / len(buffer), 3),
            "ay_avg": round(sum(e["mpu"]["ay"] for e in buffer) / len(buffer), 3),
            "az_avg": round(sum(e["mpu"]["az"] for e in buffer) / len(buffer), 3),
            "last_ax": ultimo["mpu"]["ax"],
            "last_ay": ultimo["mpu"]["ay"],
            "last_az": ultimo["mpu"]["az"],
            "last_gx": ultimo["mpu"]["gx"],
            "last_gy": ultimo["mpu"]["gy"],
            "last_gz": ultimo["mpu"]["gz"],
        },
        "ultrasonic": {
            "avg_distance": hcsr_avg,
            "last_distance": ultimo["hcsr"],
        },
        "sound": {
            "avg_level": ky03_avg,
            "active_samples": ky03_active,
            "inmp_scaled": inmp_scaled,
            "last_raw": ultimo["ky03"]["raw"],
            "last_level": ultimo["ky03"]["level"],
        },
        "derived": {
            "respiratory_rate": resp_rate if resp_rate else -1,
            "effort_index": effort,
            "chest_expansion_proxy": hcsr_avg if hcsr_avg > 0 else -1,
        }
    }
    return payload

# ============================================================
# WIFI
# ============================================================

wifi = network.WLAN(network.STA_IF)

def wifi_connect():
    """Conecta a WiFi. Bloquea hasta conexion exitosa."""
    if wifi.isconnected():
        return True
    wifi.active(True)
    wifi.connect(WIFI_SSID, WIFI_PASS)
    intentos = 0
    while not wifi.isconnected() and intentos < 40:
        time.sleep(0.5)
        intentos += 1
    if wifi.isconnected():
        print(f"WiFi Conectado. IP: {wifi.ifconfig()[0]}")
        blink(3, 0.05)
        return True
    else:
        print("Error: No se pudo conectar a WiFi")
        return False

def wifi_status():
    """Indica estado de conexion."""
    if wifi.isconnected():
        led.value(1)
    else:
        led.value(not led.value())

# ============================================================
# ENVIO DE DATOS (HTTP POST a Marcos)
# ============================================================

def enviar_datos(payload):
    """Envia JSON por HTTP POST al servidor."""
    try:
        headers = {"Content-Type": "application/json"}
        data = ujson.dumps(payload)
        resp = urequests.post(SERVER_URL, data=data, headers=headers, timeout=3)
        resp.close()
        return True
    except Exception as e:
        print(f"Error envio: {e}")
        return False

def enviar_buffer():
    """Envia buffer acumulado y lo limpia."""
    global buffer, mpu_history
    payload = build_payload()
    if payload is None:
        return
    if enviar_datos(payload):
        buffer = []
        mpu_history = []
        blink(1, 0.03)
        print(f"Enviados {payload['samples']} samples")
    else:
        # Si falla, mantener buffer hasta BUFFER_MAX
        if len(buffer) > BUFFER_MAX * 2:
            buffer = buffer[-BUFFER_MAX:]
            mpu_history = mpu_history[-100:]

# ============================================================
# MODO SERVIDOR LOCAL (fallback: ver datos desde el navegador)
# ============================================================

def iniciar_servidor_local():
    """Inicia servidor HTTP basico en el ESP32 para ver datos en vivo."""
    import socket
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    print(f"Servidor local en http://{wifi.ifconfig()[0]}:80")

    def handle_client(cl, _):
        payload = build_payload()
        if payload:
            body = ujson.dumps(payload)
        else:
            body = '{"status":"no data"}'
        cl.send(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n{body}")
        cl.close()

    import _thread
    def server_loop():
        while True:
            try:
                cl, addr = s.accept()
                cl.settimeout(1)
                req = cl.recv(1024)
                handle_client(cl, req)
            except:
                pass
            time.sleep(0.05)
    _thread.start_new_thread(server_loop, ())

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("\n=== PulmoAlert - ESP32-S3 Mini ===")
    print("Inicializando sensores...")

    # Inicializar MPU-6050
    if not mpu_init():
        print("ERROR: MPU-6050 no detectado en I2C")
    else:
        print(f"MPU-6050 OK (addr: 0x{MPU_ADDR:02X})")

    # Conectar WiFi
    if not wifi_connect():
        print("Continuando sin WiFi (modo servidor local)")
    else:
        print(f"Enviando datos a: {SERVER_URL}")

    # Servidor local como respaldo
    if wifi.isconnected():
        try:
            iniciar_servidor_local()
        except:
            pass

    blink(4, 0.08)
    print("\nLeyendo sensores cada {}ms...\n".format(SAMPLE_INTERVAL_MS))

    sample_count = 0
    last_send = time.ticks_ms()

    while True:
        try:
            # --- LECTURA DE SENSORES ---
            mpu_data = mpu_read()
            dist = hcsr04_read()
            ky3_data = ky03_read()

            add_reading(mpu_data, dist, ky3_data)
            sample_count += 1

            # LED parpadea con cada lectura
            led.value(not led.value())

            # --- ENVIO PERIODICO ---
            now = time.ticks_ms()
            elapsed = time.ticks_diff(now, last_send)
            if elapsed >= 5000 and wifi.isconnected():  # cada 5s
                if len(buffer) >= 3:
                    enviar_buffer()
                last_send = now

            # Control de buffer en modo offline
            if len(buffer) > BUFFER_MAX and not wifi.isconnected():
                buffer.pop(0)
                if mpu_history:
                    mpu_history.pop(0)

            # Mostrar resumen cada 10 lecturas
            if sample_count % 10 == 0:
                rr = rsun(0, mpu_history[-30:]) if len(mpu_history) >= 10 else -1
                print(f"[{sample_count}] MPU:{mpu_data['accel']:.2f}g "
                      f"HCSR:{dist}cm KY03:{ky3_data['level']:.2f} "
                      f"RR:{rr}/min" if rr else
                      f"[{sample_count}] MPU:{mpu_data['accel']:.2f}g "
                      f"HCSR:{dist}cm KY03:{ky3_data['level']:.2f}")

        except Exception as e:
            print(f"Error en loop: {e}")

        time.sleep_ms(SAMPLE_INTERVAL_MS)


if __name__ == "__main__":
    main()
