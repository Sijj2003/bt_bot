import os
import json
import time
import base64
import re
import queue
import threading
import logging
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

# Configuración de logs limpia para Render
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =========================================================
# 1. SERVIDOR FLASK (Health Check para Render)
# =========================================================
app = Flask(__name__)

@app.route('/', methods=['GET'])
def health_check():
    return "Gymenez Bot Worker is Active and Listening!", 200

# =========================================================
# 2. INICIALIZACIÓN DE FIREBASE ADMIN
# =========================================================
firebase_credentials_raw = os.environ.get('FIREBASE_CREDENTIALS')

if firebase_credentials_raw:
    try:
        if not firebase_credentials_raw.strip().startswith('{'):
            decoded_bytes = base64.b64decode(firebase_credentials_raw)
            cred_dict = json.loads(decoded_bytes.decode('utf-8'))
        else:
            cred_dict = json.loads(firebase_credentials_raw)
            
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        logger.info("[FIREBASE] Inicializado correctamente desde variable de entorno.")
    except Exception as e:
        logger.error(f"[FIREBASE ERROR] Error parseando FIREBASE_CREDENTIALS: {e}")
        raise e
else:
    if os.path.exists('serviceAccountKey.json'):
        cred = credentials.Certificate('serviceAccountKey.json')
        firebase_admin.initialize_app(cred)
        logger.info("[FIREBASE] Inicializado desde serviceAccountKey.json local.")
    else:
        raise ValueError("Falta la variable FIREBASE_CREDENTIALS o el archivo local serviceAccountKey.json.")

db = firestore.client()

# =========================================================
# 3. CONFIGURACIÓN DEL BANCO
# =========================================================
TESORO_SUCURSAL = os.environ.get("TESORO_SUCURSAL")
TESORO_CAJA = os.environ.get("TESORO_CAJA")
TESORO_PASS = os.environ.get("TESORO_PASS")

if not all([TESORO_SUCURSAL, TESORO_CAJA, TESORO_PASS]):
    raise ValueError("Faltan variables de entorno del Banco del Tesoro (TESORO_SUCURSAL, TESORO_CAJA, TESORO_PASS).")

# =========================================================
# FUNCIONES AUXILIARES DE FORMATEO
# =========================================================
def formato_monto_formulario(monto_val):
    """Convierte cualquier monto al formato estricto del banco (ej. '115,51')"""
    try:
        if isinstance(monto_val, str):
            m_str = monto_val.strip()
            if ',' in m_str and '.' in m_str:
                m_str = m_str.replace('.', '').replace(',', '.')
            elif ',' in m_str:
                m_str = m_str.replace(',', '.')
            f_val = float(m_str)
        else:
            f_val = float(monto_val)
        
        return f"{f_val:.2f}".replace('.', ',')
    except Exception:
        return "0,00"

def solo_numeros(cadena):
    return re.sub(r'\D', '', str(cadena or ''))

# =========================================================
# 4. GESTOR DE SESIÓN PERSISTENTE Y REUTILIZABLE
# =========================================================
class BankSessionManager:
    """Mantiene una única sesión abierta en el banco y gestiona re-logins automáticos."""
    def __init__(self):
        self.session = None
        self.csrf_token = None
        self.is_logged_in = False
        self.last_activity = 0
        self.lock = threading.Lock()

    def _crear_session_http(self):
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'es-ES,es;q=0.9',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        })
        return s

    def login(self):
        with self.lock:
            logger.info("[BANCO] Iniciando sesión persistente en Banco del Tesoro...")
            self.session = self._crear_session_http()
            
            # Paso 1: GET Login
            res_get = self.session.get('https://tesoropagos.bt.com.ve/login', timeout=15)
            soup = BeautifulSoup(res_get.text, 'html.parser')
            
            payload_login = {}
            for inp in soup.find_all('input', type='hidden'):
                if inp.get('name'):
                    payload_login[inp.get('name')] = inp.get('value', '')
                    
            payload_login['security_code'] = TESORO_SUCURSAL
            payload_login['box_number'] = TESORO_CAJA
            payload_login['password'] = TESORO_PASS
            
            if '_token' not in payload_login:
                self.is_logged_in = False
                raise Exception("Fallo al obtener el token CSRF inicial del login.")

            time.sleep(0.5)
            self.session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/login'})
            res_login = self.session.post('https://tesoropagos.bt.com.ve/login', data=payload_login, allow_redirects=False, timeout=15)
            
            location = res_login.headers.get('Location', '')
            if res_login.status_code != 302 or 'login' in location:
                self.is_logged_in = False
                raise Exception("Credenciales del banco rechazadas o caja bloqueada.")
                
            # Paso 2: Extraer Token de Pago Móvil
            res_dash = self.session.get('https://tesoropagos.bt.com.ve/pago-movil', timeout=15)
            soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
            meta_token = soup_dash.find('meta', {'name': 'csrf-token'})
            
            self.csrf_token = meta_token['content'] if meta_token else payload_login.get('_token')
            self.is_logged_in = True
            self.last_activity = time.time()
            logger.info("✅ [BANCO] Sesión iniciada correctamente. Reutilizando para consultas.")

    def logout(self):
        with self.lock:
            if self.is_logged_in and self.session and self.csrf_token:
                try:
                    logger.info("[BANCO] Cerrando sesión por inactividad prolongada...")
                    self.session.headers.update({
                        'Accept': 'text/html', 
                        'Content-Type': 'application/x-www-form-urlencoded', 
                        'X-Requested-With': 'XMLHttpRequest'
                    })
                    self.session.post('https://tesoropagos.bt.com.ve/logout', data={'_token': self.csrf_token}, allow_redirects=False, timeout=5)
                except Exception as e:
                    logger.error(f"[BANCO LOGOUT ERROR] {e}")
                finally:
                    self.is_logged_in = False
                    self.csrf_token = None

    def consultar(self, payload_validacion, retry_on_419=True):
        if not self.is_logged_in:
            self.login()

        payload_validacion['_token'] = self.csrf_token
        headers = {
            'Referer': 'https://tesoropagos.bt.com.ve/pago-movil',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-CSRF-TOKEN': self.csrf_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json, text/javascript, */*; q=0.01'
        }

        res_val = self.session.post('https://tesoropagos.bt.com.ve/pago-movil', data=payload_validacion, headers=headers, allow_redirects=True, timeout=15)
        self.last_activity = time.time()

        # Detección de sesión caducada o CSRF 419
        if res_val.status_code in [419, 401] or 'login' in res_val.url.lower():
            logger.warning("⚠️ [BANCO] Sesión caducada o Token CSRF inválido. Re-autenticando en caliente...")
            self.is_logged_in = False
            if retry_on_419:
                self.login()
                payload_validacion['_token'] = self.csrf_token
                return self.consultar(payload_validacion, retry_on_419=False)
            else:
                raise Exception("La sesión expiró y no pudo restablecerse automáticamente.")

        return res_val

# Instancia global del gestor
bank_manager = BankSessionManager()

# =========================================================
# 5. INTELIGENCIA DE NEGOCIO Y PROTOCOLOS DE DECISIÓN
# =========================================================
def evaluar_respuesta_banco(res_val):
    """Analiza la respuesta HTTP/JSON/HTML bajo protocolo Zero Trust."""
    dictamen = "NO_ENCONTRADO"
    mensaje_banco = "El banco no encontró ningún pago que coincida con estos datos exactos."

    # Errores de servidor del banco (500, 502, 503, etc.)
    if res_val.status_code >= 500:
        return "BANCO_DOWN", f"Servidor del banco no disponible (HTTP {res_val.status_code})."

    try:
        # CASO A: FORMATO JSON
        data_json = res_val.json()
        logger.info(f"  [RESPUESTA JSON]: {json.dumps(data_json, ensure_ascii=False)}")
        
        is_success = data_json.get('success') in [True, 'true', 1, '1'] or data_json.get('status') in ['success', 'ok', 'approved']
        msg_json = str(data_json.get('message') or data_json.get('msg') or data_json.get('error') or data_json.get('leyenda') or '').lower()

        if any(w in msg_json for w in ["confirmado", "ya fue confirmad", "ya utilizad", "ya procesad", "repetid"]):
            dictamen = "YA_UTILIZADO"
            mensaje_banco = f"Esta referencia consta como confirmada o procesada anteriormente en el banco."

        elif not is_success or any(w in msg_json for w in ["no encontrad", "inválid", "rechazad", "no coincide", "no existe", "error", "fallo"]):
            dictamen = "NO_ENCONTRADO"
            mensaje_banco = f"El banco rechazó la validación: {msg_json if msg_json else 'Datos de pago incorrectos o inexistentes.'}"

        elif is_success or any(w in msg_json for w in ["exitoso", "aprobado", "verificado"]):
            dictamen = "APROBADO"
            mensaje_banco = "Pago validado y consumido exitosamente por el banco."

    except Exception:
        # CASO B: FORMATO HTML
        soup_res = BeautifulSoup(res_val.text, 'html.parser')
        
        # Limpieza profunda de HTML
        for element in soup_res(["script", "style", "head", "title", "meta"]):
            element.extract()
        
        alertas = soup_res.find_all(class_=re.compile(r'alert|toast|swal|invalid-feedback|message|response|notification', re.I))
        if alertas:
            texto_visible = " ".join([a.get_text(strip=True) for a in alertas]).lower()
        else:
            texto_visible = soup_res.get_text(separator=' ', strip=True).lower()

        logger.info(f"  [TEXTO HTML EXTRAÍDO]: '{texto_visible[:200]}...'")

        if any(w in texto_visible for w in ["mantenimiento", "fuera de servicio", "intente mas tarde", "intente más tarde"]):
            return "BANCO_DOWN", "Plataforma bancaria en mantenimiento temporal."

        if any(w in texto_visible for w in ["confirmado", "ya fue confirmad", "ya utilizad", "ya procesad", "repetid"]):
            dictamen = "YA_UTILIZADO"
            mensaje_banco = "Esta referencia consta como confirmada o procesada anteriormente en el banco."

        elif any(phrase in texto_visible for phrase in [
            "pago exitoso", "pago verificado", "se validó el pago de forma exitosa", 
            "se valido el pago de forma exitosa", "operacion exitosa", "operación exitosa",
            "pago procesado con exito", "pago procesado con éxito"
        ]):
            dictamen = "APROBADO"
            mensaje_banco = "Pago validado y consumido exitosamente por el banco."
        else:
            dictamen = "NO_ENCONTRADO"
            mensaje_banco = "El banco no confirmó el pago (datos inválidos o inexistentes)."

    return dictamen, mensaje_banco

def resolver_falso_positivo_ya_utilizado(order_id, ref_6_digitos, datos_orden):
    """
    Diferencia si un YA_UTILIZADO es un fraude real (usado en otra compra) 
    o un reintento exitoso de la misma orden tras una interrupción de red.
    """
    logger.info(f"🔍 [INTELLIGENCE] Analizando trazabilidad para YA_UTILIZADO en orden: {order_id}")
    
    # 1. Buscar si esta misma referencia YA fue aprobada en OTRA orden distinta
    query_otras_ordenes = db.collection('store_orders') \
        .where('status', '==', 'approved') \
        .get()
    
    for doc in query_otras_ordenes:
        if doc.id != order_id:
            p_details = doc.to_dict().get('payment_details', {})
            ref_otra = str(p_details.get('referencia') or p_details.get('reference') or '').strip().zfill(6)[-6:]
            if ref_otra == ref_6_digitos:
                logger.warning(f"🚨 [FRAUDE DETECTADO] La referencia {ref_6_digitos} ya fue utilizada en la orden {doc.id}.")
                return False, "Fraude prevenido: Esta referencia ya fue utilizada en otra compra anterior."

    # 2. Si NO existe en otra orden aprobada, verificar si esta misma orden inició verificación previamente
    started_at = datos_orden.get('verification_started_at')
    if started_at:
        logger.info(f"✅ [RECUPERACIÓN EXITOSA] La orden {order_id} ya fue procesada por el banco en un intento previo interrumpido. Se procede a APROBAR.")
        return True, "Pago verificado exitosamente (Recuperación por reintento de conexión)."

    return False, "Esta referencia ya fue confirmada o procesada anteriormente en el banco."

# =========================================================
# 6. PROCESAMIENTO PRINCIPAL DE UNA ORDEN
# =========================================================
def procesar_validacion_en_banco(order_id, datos_orden):
    print("\n" + "="*80)
    logger.info(f"🚀 [INICIO PROCESAMIENTO] Orden ID: {order_id}")
    print("="*80)

    order_ref = db.collection('store_orders').document(order_id)
    
    # Marcar sello de tiempo de inicio para trazabilidad
    try:
        order_ref.update({'verification_started_at': firestore.SERVER_TIMESTAMP})
    except Exception as e:
        logger.error(f"[FIRESTORE WARNING] No se pudo marcar timestamp inicial: {e}")

    try:
        payment_details = datos_orden.get('payment_details', {})
        referencia_raw = payment_details.get('referencia') or payment_details.get('reference') or ''
        banco_orden = str(payment_details.get('banco', '')).strip()
        telefono_orden = str(payment_details.get('telefono', '')).strip()
        monto_orden_raw = payment_details.get('monto_bot', '')

        ref_6_digitos = str(referencia_raw).strip().zfill(6)[-6:]
        monto_form = formato_monto_formulario(monto_orden_raw)
        tel_form = solo_numeros(telefono_orden)
        banco_form = solo_numeros(banco_orden)

        logger.info(f"[DATOS FORMULARIO]: Ref='{ref_6_digitos}' | Monto='{monto_form}' | Tel='{tel_form}' | Banco='{banco_form}'")

        if not ref_6_digitos or monto_form == "0,00":
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': 'Datos insuficientes en la orden (Falta referencia o monto).'
            })
            return

        payload_val = {
            'monto': monto_form,
            'banco': banco_form,
            'telefono': tel_form,
            'referencia': ref_6_digitos
        }

        # Ejecutar consulta a través del gestor persistente
        res_val = bank_manager.consultar(payload_val)
        dictamen, mensaje_banco = evaluar_respuesta_banco(res_val)

        # --- APLICACIÓN DE RESULTADOS ---
        if dictamen == "APROBADO":
            order_ref.update({
                'status': 'approved',
                'bot_verification_msg': mensaje_banco,
                'verified_at': firestore.SERVER_TIMESTAMP
            })
            logger.info(f"🎉 [FIRESTORE] Orden {order_id} APROBADA.")

        elif dictamen == "YA_UTILIZADO":
            es_aprobado_recuperado, msg_resolucion = resolver_falso_positivo_ya_utilizado(order_id, ref_6_digitos, datos_orden)
            
            if es_aprobado_recuperado:
                order_ref.update({
                    'status': 'approved',
                    'bot_verification_msg': msg_resolucion,
                    'verified_at': firestore.SERVER_TIMESTAMP
                })
                logger.info(f"🎉 [FIRESTORE] Orden {order_id} APROBADA tras resolución de reintento.")
            else:
                order_ref.update({
                    'status': 'rejected',
                    'bot_verification_msg': msg_resolucion
                })
                logger.warning(f"🚨 [FIRESTORE] Orden {order_id} RECHAZADA (Doble uso/Fraude).")

        elif dictamen == "NO_ENCONTRADO":
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': mensaje_banco
            })
            logger.warning(f"🚫 [FIRESTORE] Orden {order_id} RECHAZADA (No encontrado en el banco).")

        elif dictamen == "BANCO_DOWN":
            order_ref.update({
                'bot_verification_msg': f"Revisión en pausa: {mensaje_banco}"
            })
            logger.warning(f"⚠️ [FIRESTORE] Orden {order_id} mantenida en espera por problemas de conectividad con el banco.")

    except Exception as e:
        logger.error(f"❌ [BOT EXCEPCIÓN] Error crítico procesando la orden {order_id}: {e}")
        order_ref.update({
            'bot_verification_msg': f"Error técnico / Revisión manual: {str(e)}"
        })
    finally:
        print("="*80 + "\n")

# =========================================================
# 7. WORKER Y COLA DE PROCESAMIENTO SECUENCIAL (QUEUE)
# =========================================================
order_queue = queue.Queue()
processed_in_flight = set()
lock_in_flight = threading.Lock()

def worker_loop():
    """Hilo único de fondo que procesa los pagos en orden exacto de llegada."""
    logger.info("👷 [WORKER] Hilo de procesamiento continuo iniciado correctamente.")
    
    while True:
        try:
            # Espera por una orden con tiempo límite de 30s
            try:
                order_id, order_data = order_queue.get(timeout=30)
            except queue.Empty:
                # Si transcurren más de 120s de inactividad, cierra sesión en el banco para liberar la caja
                if bank_manager.is_logged_in and (time.time() - bank_manager.last_activity > 120):
                    bank_manager.logout()
                continue

            logger.info(f"📥 [WORKER] Extrayendo orden de la cola: {order_id}")
            procesar_validacion_en_banco(order_id, order_data)
            
            order_queue.task_done()
            with lock_in_flight:
                processed_in_flight.discard(order_id)

        except Exception as e:
            logger.error(f"❌ [WORKER ERROR] Excepción no controlada en el worker loop: {e}")
            time.sleep(2)

# =========================================================
# 8. LISTENER EN TIEMPO REAL (FIRESTORE)
# =========================================================
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            order_id = change.document.id
            order_data = change.document.to_dict()
            
            with lock_in_flight:
                if order_id in processed_in_flight:
                    continue
                processed_in_flight.add(order_id)

            logger.info(f"⚡ [FIRESTORE EVENT] Nueva orden encolada: {order_id}")
            order_queue.put((order_id, order_data))

def start_bot_services():
    # Iniciar Hilo Worker
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    # Iniciar Listener Firestore
    try:
        logger.info("🚀 [BOT] Iniciando Listener de Firestore para 'store_orders'...")
        orders_ref = db.collection('store_orders').where('status', '==', 'pending_verification')
        orders_ref.on_snapshot(on_snapshot)
        logger.info("✅ [BOT] Listener activo y escuchando compras pendientes.")
    except Exception as e:
        logger.error(f"❌ [BOT ERROR] Falló al iniciar el Listener de Firestore: {e}")

# Iniciar servicios al cargar el script
start_bot_services()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
