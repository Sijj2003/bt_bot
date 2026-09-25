import os
import json
import time
import base64
import re
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

# Configuración de logs limpia para el Dashboard de Render
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
    """Convierte cualquier monto al formato estricto que exige el formulario del banco (ej. '115,51')"""
    try:
        if isinstance(monto_val, str):
            m_str = monto_val.strip()
            if ',' in m_str and '.' in m_str: # Ejemplo: 1.000,50
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
# 4. LÓGICA DEL BOT BANCARIO (ZERO TRUST CORREGIDO)
# =========================================================
def procesar_validacion_en_banco(order_id, datos_orden):
    print("\n" + "="*80)
    logger.info(f"🚀 [INICIO DE PROCESAMIENTO ZERO TRUST] Orden ID: {order_id}")
    print("="*80)

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Accept-Language': 'es-ES,es;q=0.9',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    })
    
    api_token_global = None
    resultado_final = {"status": "ERROR", "mensaje": "Fallo desconocido."}

    try:
        # --- PASO 0: EXTRAER DATOS FIRESTORE ---
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
            raise Exception("Datos insuficientes en la orden (Falta referencia o monto).")

        session.cookies.clear()
        
        # --- PASO 1: LOGIN ---
        logger.info("[PASO 1] Entrando a Login...")
        res_get = session.get('https://tesoropagos.bt.com.ve/login', timeout=15)
        soup = BeautifulSoup(res_get.text, 'html.parser')
        
        payload_login = {}
        for inp in soup.find_all('input', type='hidden'):
            if inp.get('name'):
                payload_login[inp.get('name')] = inp.get('value', '')
                
        payload_login['security_code'] = TESORO_SUCURSAL
        payload_login['box_number'] = TESORO_CAJA
        payload_login['password'] = TESORO_PASS
        
        if '_token' not in payload_login:
            raise Exception("Fallo obteniendo token CSRF inicial.")

        time.sleep(1)
        
        logger.info("[PASO 1] Enviando credenciales...")
        session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/login'})
        res_login = session.post('https://tesoropagos.bt.com.ve/login', data=payload_login, allow_redirects=False, timeout=15)
        
        location = res_login.headers.get('Location', '')
        if res_login.status_code != 302 or 'login' in location:
            raise Exception("Credenciales del banco rechazadas o sesión bloqueada.")
            
        logger.info("  ✅ Login exitoso.")
        
        # --- PASO 2: OBTENER TOKEN DE VALIDACIÓN ---
        logger.info("[PASO 2] Extrayendo CSRF de Pago Móvil...")
        res_dash = session.get('https://tesoropagos.bt.com.ve/pago-movil', timeout=15)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        meta_token = soup_dash.find('meta', {'name': 'csrf-token'})
        api_token = meta_token['content'] if meta_token else payload_login.get('_token')
        api_token_global = api_token

        # --- PASO 3: ENVIAR FORMULARIO DE VALIDACIÓN ---
        logger.info("[PASO 3] Enviando consulta al banco...")
        payload_validacion = {
            '_token': api_token,
            'monto': monto_form,
            'banco': banco_form,
            'telefono': tel_form,
            'referencia': ref_6_digitos
        }
        
        session.headers.update({
            'Referer': 'https://tesoropagos.bt.com.ve/pago-movil',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-CSRF-TOKEN': api_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json, text/javascript, */*; q=0.01'
        })
        
        res_val = session.post('https://tesoropagos.bt.com.ve/pago-movil', data=payload_validacion, allow_redirects=True, timeout=15)
        
        # --- PASO 4: INTERPRETACIÓN ESTRICTA (ZERO TRUST) ---
        logger.info("[PASO 4] Analizando dictamen del banco...")
        
        dictamen = "NO_ENCONTRADO"
        mensaje_banco = "El banco no encontró ningún pago que coincida con estos datos exactos."

        try:
            # CASO A: RESPUESTA EN FORMATO JSON
            data_json = res_val.json()
            logger.info(f"  [RESPUESTA JSON]: {json.dumps(data_json, ensure_ascii=False)}")
            
            is_success = data_json.get('success') in [True, 'true', 1, '1'] or data_json.get('status') in ['success', 'ok', 'approved']
            msg_json = str(data_json.get('message') or data_json.get('msg') or data_json.get('error') or data_json.get('leyenda') or '').lower()

            if any(w in msg_json for w in ["confirmado", "ya fue confirmad", "ya utilizad", "ya procesad", "repetid"]):
                dictamen = "YA_UTILIZADO"
                mensaje_banco = f"Fraude prevenido: {msg_json if msg_json else 'Esta referencia ya fue confirmada previamente.'}"

            elif not is_success or any(w in msg_json for w in ["no encontrad", "inválid", "rechazad", "no coincide", "no existe", "error", "fallo"]):
                dictamen = "NO_ENCONTRADO"
                mensaje_banco = f"El banco rechazó la validación: {msg_json if msg_json else 'Datos de pago incorrectos o inexistentes.'}"

            elif is_success or any(w in msg_json for w in ["exitoso", "aprobado", "verificado"]):
                dictamen = "APROBADO"
                mensaje_banco = "Pago validado y consumido exitosamente por el banco."

        except Exception:
            # CASO B: RESPUESTA EN FORMATO HTML
            soup_res = BeautifulSoup(res_val.text, 'html.parser')
            
            # Limpieza profunda: Eliminar scripts, CSS y tags no visibles que ensucian con 'true'/'success'
            for element in soup_res(["script", "style", "head", "title", "meta"]):
                element.extract()
            
            # Buscar texto dentro de avisos o alertas
            alertas = soup_res.find_all(class_=re.compile(r'alert|toast|swal|invalid-feedback|message|response|notification', re.I))
            if alertas:
                texto_visible = " ".join([a.get_text(strip=True) for a in alertas]).lower()
            else:
                texto_visible = soup_res.get_text(separator=' ', strip=True).lower()

            logger.info(f"  [TEXTO VISIBLE HTML EXTRAÍDO]: '{texto_visible[:250]}...'")

            # 1. Evaluar si la referencia ya fue utilizada
            if any(w in texto_visible for w in ["confirmado", "ya fue confirmad", "ya utilizad", "ya procesad", "repetid"]):
                dictamen = "YA_UTILIZADO"
                mensaje_banco = "Fraude prevenido: Esta referencia ya fue confirmada o procesada anteriormente."

            # 2. Exigir frases explícitas de éxito (NUNCA palabras sueltas como 'true' o 'success')
            elif any(phrase in texto_visible for phrase in [
                "pago exitoso", 
                "pago verificado", 
                "se validó el pago de forma exitosa", 
                "se valido el pago de forma exitosa",
                "operacion exitosa", 
                "operación exitosa",
                "pago procesado con exito",
                "pago procesado con éxito"
            ]):
                dictamen = "APROBADO"
                mensaje_banco = "Pago validado y consumido exitosamente por el banco."

            # 3. Respaldo Zero Trust: Si no contiene la frase completa de éxito, SE RECHAZA
            else:
                dictamen = "NO_ENCONTRADO"
                mensaje_banco = "El banco no confirmó el pago (datos inválidos, no coincidentes o inexistentes)."

        # ASIGNACIÓN FINAL
        if dictamen == "APROBADO":
            resultado_final = {"status": "APROBADO", "mensaje": mensaje_banco}
            logger.info("  🎉 DICTAMEN: Aprobado.")
        elif dictamen == "YA_UTILIZADO":
            resultado_final = {"status": "YA_UTILIZADO", "mensaje": mensaje_banco}
            logger.warning("  🚨 DICTAMEN: Rechazado (Referencia ya confirmada / repetida).")
        else:
            resultado_final = {"status": "NO_ENCONTRADO", "mensaje": mensaje_banco}
            logger.warning(f"  🚫 DICTAMEN: Rechazado ({mensaje_banco}).")

    except Exception as e:
        logger.error(f"❌ [BOT EXCEPCIÓN] Error procesando la orden: {e}")
        resultado_final = {"status": "ERROR", "mensaje": str(e)}
        
    finally:
        # --- PASO 5: LOGOUT SEGURO ---
        if api_token_global:
            try:
                logger.info("[PASO 5] Cerrando sesión bancaria...")
                session.headers.update({
                    'Accept': 'text/html', 
                    'Content-Type': 'application/x-www-form-urlencoded', 
                    'X-Requested-With': 'XMLHttpRequest'
                })
                session.post('https://tesoropagos.bt.com.ve/logout', data={'_token': api_token_global}, allow_redirects=False, timeout=5)
            except Exception as e:
                logger.error(f"[PASO 5 ERROR] Error cerrando sesión: {e}")
                
    # --- PASO 6: ACTUALIZAR FIRESTORE ---
    try:
        order_ref = db.collection('store_orders').document(order_id)
        
        if resultado_final['status'] == 'APROBADO':
            order_ref.update({
                'status': 'approved',
                'bot_verification_msg': resultado_final['mensaje'],
                'verified_at': firestore.SERVER_TIMESTAMP
            })
            logger.info(f"✅ [FIRESTORE] Orden {order_id} actualizada a 'approved'.")
            
        elif resultado_final['status'] in ['YA_UTILIZADO', 'NO_ENCONTRADO']:
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': resultado_final['mensaje']
            })
            logger.info(f"🛡️ [FIRESTORE] Orden {order_id} actualizada a 'rejected' ({resultado_final['status']}).")
            
        else:
            order_ref.update({
                'bot_verification_msg': f"Error/Revisión Manual: {resultado_final['mensaje']}"
            })
            logger.warning(f"⚠️ [FIRESTORE] Estado técnico guardado para revisión manual en la orden {order_id}.")
            
    except Exception as e:
        logger.error(f"❌ [FIRESTORE ERROR] Falló la actualización de la orden {order_id}: {e}")

    print("="*80 + "\n")

# =========================================================
# 5. LISTENER EN TIEMPO REAL (FIRESTORE)
# =========================================================
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            order_id = change.document.id
            order_data = change.document.to_dict()
            logger.info(f"⚡ [FIRESTORE EVENT] Nueva orden pendiente detectada: {order_id}")
            
            threading.Thread(
                target=procesar_validacion_en_banco, 
                args=(order_id, order_data)
            ).start()

def start_firestore_listener():
    try:
        logger.info("🚀 [BOT] Iniciando Listener en tiempo real para colección 'store_orders'...")
        orders_ref = db.collection('store_orders').where('status', '==', 'pending_verification')
        orders_ref.on_snapshot(on_snapshot)
        logger.info("✅ [BOT] Listener de Firestore activo y escuchando compras pendientes.")
    except Exception as e:
        logger.error(f"❌ [BOT ERROR] Falló al iniciar el Listener de Firestore: {e}")

# SE INICIA EL LISTENER AL CARGAR EL MÓDULO (Para que funcione con Gunicorn en Render)
start_firestore_listener()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
