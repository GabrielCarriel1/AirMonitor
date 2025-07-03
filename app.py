import serial
import time
from flask import Flask, render_template, jsonify, request
import threading
from collections import deque
import sys
import requests

# --- Configuración del Puerto Serial de tu Arduino ---
# ¡¡¡IMPORTANTE!!! Cambia 'COM6' al puerto COM correcto de tu Arduino Nano.
# En Windows, suele ser 'COMx' (ej. COM3, COM4).
# En Linux/Mac, suele ser '/dev/ttyUSB0' o '/dev/ttyACM0'.
# Puedes verificarlo en el IDE de Arduino: Herramientas -> Puerto.
ARDUINO_PORT = 'COM6' 
BAUD_RATE = 9600 # Debe coincidir con Serial.begin() en Arduino

# --- Variables Globales para Almacenar Datos ---
last_sensor_data = {
    "temperatura": "N/A",
    "humedad": "N/A",
    "co_ppm": "N/A",
    "estado_aire": "N/A",
    "raw_mq7": "N/A",
    "timestamp": "N/A"
}

# Historial de datos (usaremos deque para mantener un tamaño fijo de las últimas 20 lecturas)
DATA_HISTORY_SIZE = 20
sensor_data_history = deque(maxlen=DATA_HISTORY_SIZE)

# Objeto Serial para la comunicación con Arduino
ser = None

# Un Lock para proteger el acceso al puerto serial en entornos multihilo
serial_lock = threading.Lock()

# --- Configuración de Telegram ---
TELEGRAM_TOKEN = '8158310204:AAGwoEiwqr157s7sOqLxIbsDsUHaHfQyYvw'
TELEGRAM_CHAT_ID = '8153505350'
CO_UMBRAL_ALERTA = 50  # ppm
alerta_telegram_activa = False  # Para evitar mensajes repetidos

# --- Configuración dinámica desde la web ---
co_umbral_config = CO_UMBRAL_ALERTA
update_interval_config = 5  # segundos

# Función para enviar mensaje a Telegram
def enviar_alerta_telegram(mensaje):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': mensaje
    }
    try:
        response = requests.post(url, data=data, timeout=5)
        if response.status_code == 200:
            print('Alerta enviada a Telegram')
        else:
            print(f'Error al enviar alerta a Telegram: {response.text}')
    except Exception as e:
        print(f'Error de conexión al enviar alerta a Telegram: {e}')

# --- Función para Conectar al Arduino ---
def connect_arduino():
    global ser
    with serial_lock: # Aseguramos que solo un hilo intente conectar a la vez
        if ser and ser.is_open:
            return True # Ya estamos conectados

        try:
            ser = serial.Serial(ARDUINO_PORT, BAUD_RATE, timeout=1)
            time.sleep(2) # Pequeña pausa para que el Arduino se inicialice después de la conexión serial
            print(f"Conectado a Arduino en {ARDUINO_PORT}")
            return True
        except serial.SerialException as e:
            print(f"Error al conectar con Arduino en {ARDUINO_PORT}: {e}")
            ser = None # Asegurarse de que ser sea None si la conexión falla
            return False
        except Exception as e:
            print(f"Error inesperado al intentar conectar: {e}")
            ser = None
            return False

# --- Función para Leer Datos del Arduino (se ejecutará en un hilo separado) ---
def read_arduino_data_thread():
    global last_sensor_data, ser, alerta_telegram_activa, co_umbral_config
    
    while True: # Bucle infinito para intentar leer y reconectar
        start_time = time.perf_counter()
        if not connect_arduino(): # Intentar conectar si no estamos conectados
            time.sleep(5) # Esperar antes de reintentar la conexión
            continue # Volver al inicio del bucle para reintentar la conexión

        # Si estamos aquí, la conexión con Arduino está activa
        try:
            # Verifica si el objeto ser está disponible y abierto
            if ser is not None and ser.is_open and ser.in_waiting > 0:
                line = ser.readline().decode('utf-8').strip() # Lee la línea, decodifica y quita espacios/saltos de línea
                if line: # Asegúrate de que la línea no esté vacía
                    if line == "DHT_ERROR":
                        print("Error del sensor DHT11 detectado por Arduino.")
                        # Si hay un error DHT, actualiza solo la temperatura y humedad a "Error"
                        last_sensor_data.update({
                            "temperatura": "Error",
                            "humedad": "Error",
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "tiempo_ciclo": "0",
                            "tiempo_dht": "0",
                            "tiempo_mq7": "0",
                            "tiempo_ciclo_arduino": "0"
                        })
                    else:
                        # Parsear la línea esperada: Temp,Humedad,PPM_CO,Estado_Aire,Raw_MQ7,Tiempo_DHT,Tiempo_MQ7,Tiempo_Ciclo_Arduino
                        parts = line.split(',')
                        if len(parts) == 8: # Verificamos que tengamos 8 partes
                            try:
                                temp = float(parts[0])
                                hum = float(parts[1])
                                co_ppm = float(parts[2])
                                estado_aire = parts[3]
                                raw_mq7 = int(parts[4])
                                tiempo_dht = int(parts[5])
                                tiempo_mq7 = int(parts[6])
                                tiempo_ciclo_arduino = int(parts[7])
                                end_time = time.perf_counter()
                                tiempo_ciclo = int((end_time - start_time) * 1000000)  # en μs
                                # Actualiza los últimos datos recibidos
                                last_sensor_data = {
                                    "temperatura": temp,
                                    "humedad": hum,
                                    "co_ppm": co_ppm,
                                    "estado_aire": estado_aire,
                                    "raw_mq7": raw_mq7,
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "tiempo_ciclo": tiempo_ciclo,
                                    "tiempo_dht": tiempo_dht,
                                    "tiempo_mq7": tiempo_mq7,
                                    "tiempo_ciclo_arduino": tiempo_ciclo_arduino
                                }
                                # Añade una copia de los datos actuales al historial
                                sensor_data_history.append(last_sensor_data.copy())
                                print(f"Datos recibidos y actualizados: {last_sensor_data}")
                                # --- ALERTA TELEGRAM ---
                                if co_ppm >= co_umbral_config:
                                    if not alerta_telegram_activa:
                                        mensaje = f"⚠️ ALERTA: El nivel de CO ha superado el umbral seguro ({co_ppm} ppm) a las {last_sensor_data['timestamp']}"
                                        enviar_alerta_telegram(mensaje)
                                        alerta_telegram_activa = True
                                else:
                                    alerta_telegram_activa = False
                            except ValueError as ve:
                                print(f"Error al parsear datos numéricos: {line} - {ve}")
                                print("Asegúrate de que los datos de Arduino sean números donde se esperan.")
                        elif len(parts) == 5:
                            # Soporte retrocompatible para el formato anterior
                            try:
                                temp = float(parts[0])
                                hum = float(parts[1])
                                co_ppm = float(parts[2])
                                estado_aire = parts[3]
                                raw_mq7 = int(parts[4])
                                end_time = time.perf_counter()
                                tiempo_ciclo = int((end_time - start_time) * 1000000)  # en μs
                                last_sensor_data = {
                                    "temperatura": temp,
                                    "humedad": hum,
                                    "co_ppm": co_ppm,
                                    "estado_aire": estado_aire,
                                    "raw_mq7": raw_mq7,
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "tiempo_ciclo": tiempo_ciclo
                                }
                                sensor_data_history.append(last_sensor_data.copy())
                                print(f"Datos recibidos y actualizados: {last_sensor_data}")
                                # --- ALERTA TELEGRAM ---
                                if co_ppm >= co_umbral_config:
                                    if not alerta_telegram_activa:
                                        mensaje = f"⚠️ ALERTA: El nivel de CO ha superado el umbral seguro ({co_ppm} ppm) a las {last_sensor_data['timestamp']}"
                                        enviar_alerta_telegram(mensaje)
                                        alerta_telegram_activa = True
                                else:
                                    alerta_telegram_activa = False
                            except ValueError as ve:
                                print(f"Error al parsear datos numéricos: {line} - {ve}")
                                print("Asegúrate de que los datos de Arduino sean números donde se esperan.")
                        else:
                            print(f"Formato de datos inesperado o incompleto: {line}")
            else:
                # Si no hay datos pendientes o el puerto no está disponible, espera un momento para no consumir CPU
                time.sleep(0.1)

        except serial.SerialException as e:
            print(f"Error de lectura/escritura serial: {e}. Desconectado. Intentando reconectar...")
            with serial_lock: # Usa el lock antes de cerrar y reestablecer 'ser'
                if ser:
                    ser.close()
                ser = None # Marca el puerto como desconectado para forzar la reconexión
            time.sleep(5) # Espera antes de que el bucle intente reconectar

        except Exception as e:
            print(f"Error inesperado en el hilo de lectura: {e}")
            time.sleep(1) # Pequeña espera para evitar un bucle rápido en caso de un error recurrente

# --- Configuración del Servidor Flask ---
app = Flask(__name__)

# Ruta principal que carga la página HTML
@app.route('/')
def index():
    # Renderiza la plantilla HTML y le pasa solo los datos actuales
    return render_template('index.html', current_data=last_sensor_data)

@app.route('/historial')
def historial():
    # Renderiza la plantilla de historial y le pasa el historial
    return render_template('historial.html', history=list(sensor_data_history))

@app.route('/graficas')
def graficas():
    # Renderiza la plantilla de gráficas y le pasa el historial
    return render_template('graficas.html', history=list(sensor_data_history))

# Ruta API para obtener los datos más recientes (usada por JavaScript para actualizar)
@app.route('/data')
def get_data():
    return jsonify(last_sensor_data) # Devuelve los datos actuales en formato JSON

# Ruta API para obtener el historial de datos con paginación
@app.route('/history')
def get_history():
    # Obtener parámetros de paginación de la query string
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
    except Exception:
        page = 1
        size = 20
    
    # Asegurar valores válidos
    page = max(page, 1)
    size = max(size, 1)
    
    history_list = list(sensor_data_history)
    total = len(history_list)
    total_pages = (total + size - 1) // size
    
    # Calcular el rango de datos a devolver
    start = (page - 1) * size
    end = start + size
    paginated = history_list[start:end]
    
    return jsonify({
        'data': paginated,
        'page': page,
        'size': size,
        'total': total,
        'total_pages': total_pages
    })

@app.route('/config', methods=['GET', 'POST'])
def config():
    global co_umbral_config, update_interval_config
    if request.method == 'POST':
        data = request.get_json()
        if 'co_umbral' in data:
            co_umbral_config = int(data['co_umbral'])
        if 'update_interval' in data:
            update_interval_config = int(data['update_interval'])
            # Enviar el nuevo intervalo al Arduino por serial
            try:
                if ser and ser.is_open:
                    ser.write(f"INTERVALO:{update_interval_config}\n".encode())
            except Exception as e:
                print(f"[ADVERTENCIA] No se pudo enviar el intervalo al Arduino: {e}")
        return jsonify({
            'co_umbral': co_umbral_config,
            'update_interval': update_interval_config
        })
    else:
        return jsonify({
            'co_umbral': co_umbral_config,
            'update_interval': update_interval_config
        })

# --- Inicio de la Aplicación Principal ---
if __name__ == '__main__':
    # Enviar mensaje de prueba a Telegram al iniciar la app
    enviar_alerta_telegram('El proyecto del monitoreo de la calidad del aire está encendido.')
    # Iniciar el hilo para leer datos del Arduino.
    arduino_thread = threading.Thread(target=read_arduino_data_thread, daemon=True)
    arduino_thread.start()

    try:
        # Iniciar el servidor Flask.
        # host='0.0.0.0' permite que la página sea accesible desde otros dispositivos en tu red local.
        # port=5000 es el puerto estándar de Flask.
        # debug=False y use_reloader=False son cruciales cuando usas hilos para evitar duplicaciones y errores.
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        # Maneja la interrupción del teclado (Ctrl+C) para un cierre limpio
        print("\nServidor Flask detenido por el usuario.")
    finally:
        # Asegurarse de que el puerto serial se cierre correctamente al finalizar la aplicación
        if ser and ser.is_open:
            print("Cerrando puerto serial.")
            ser.close()
        print("Aplicación terminada.")
        sys.exit(0) # Salir limpiamente del programa