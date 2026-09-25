import os
import json
import time
import base64
import re
import threading
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from dotenv import load_dotenv

load_dotenv()

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
        print("[FIREBASE] Inicializado correctamente desde variable de entorno.")
    except Exception as e:
        print(f"[FIREBASE ERROR] Error parseando FIREBASE_CREDENTIALS: {e}")
        raise e
else:
    if os.path.exists('serviceAccountKey.json'):
        cred = credentials.Certificate('serviceAccountKey.json')
        firebase_admin.initialize_app(cred)
        print("[FIREBASE] Inicializado desde serviceAccountKey.json local.")
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
# FUNCIONES AUXILIARES DE FORMATEO (MUY IMPORTANTE)
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
        
        # Devuelve string con 2 decimales y coma
        return f"{f_val:.2f}".replace('.', ',')
    except Exception:
        return "0,00"

def solo_numeros(cadena):
    return re.sub(r'\D', '', str(cadena or ''))

# =========================================================
# 4. LÓGICA DEL BOT BANCARIO (ESTRATEGIA ZERO TRUST)
# =========================================================
def procesar_validacion_en_banco(order_id, datos_orden):
    print("\n" + "="*80)
    print(f"🚀 [INICIO DE PROCESAMIENTO ZERO TRUST] Orden ID: {order_id}")
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
        
        # Preparar para el formulario
        ref_6_digitos = str(referencia_raw).strip().zfill(6)[-6:]
        monto_form = formato_monto_formulario(monto_orden_raw)
        tel_form = solo_numeros(telefono_orden)
        banco_form = solo_numeros(banco_orden)

        print("\n[DATOS PREPARADOS PARA FORMULARIO DEL BANCO]:")
        print(f"  • Ref (6 dígitos): '{ref_6_digitos}'")
        print(f"  • Monto: '{monto_form}'")
        print(f"  • Teléfono: '{tel_form}'")
        print(f"  • Banco: '{banco_form}'")

        if not ref_6_digitos or monto_form == "0,00":
            raise Exception("Datos insuficientes en la orden (Falta referencia o monto).")

        session.cookies.clear()
        
        # --- PASO 1: LOGIN ---
        print("\n[PASO 1] Entrando a Login...")
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
        
        print("[PASO 1] Iniciando sesión...")
        session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/login'})
        res_login = session.post('https://tesoropagos.bt.com.ve/login', data=payload_login, allow_redirects=False, timeout=15)
        
        location = res_login.headers.get('Location', '')
        if res_login.status_code != 302 or 'login' in location:
            raise Exception("Credenciales del banco rechazadas o sesión bloqueada.")
            
        print("  ✅ Login exitoso.")
        
        # --- PASO 2: OBTENER TOKEN DE VALIDACIÓN ---
        print("\n[PASO 2] Extrayendo CSRF de Pago Móvil...")
        res_dash = session.get('https://tesoropagos.bt.com.ve/pago-movil', timeout=15)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        meta_token = soup_dash.find('meta', {'name': 'csrf-token'})
        api_token = meta_token['content'] if meta_token else payload_login.get('_token')
        api_token_global = api_token

        # --- PASO 3: ENVIAR FORMULARIO DE VALIDACIÓN ---
        print("\n[PASO 3] Enviando datos al motor de verificación del banco...")
        payload_validacion = {
            '_token': api_token,
            'monto': monto_form,
            'banco': banco_form,
            'telefono': tel_form,
            'referencia': ref_6_digitos
        }
        
        session.headers.update({
            'Referer': 'https://tesoropagos.bt.com.ve/pago-movil',
            'Content-Type': 'application/x-www-form-urlencoded'
        })
        
        res_val = session.post('https://tesoropagos.bt.com.ve/pago-movil', data=payload_validacion, allow_redirects=True, timeout=15)
        html_respuesta = res_val.text.lower()
        
        # --- PASO 4: INTERPRETACIÓN (ZERO TRUST) ---
        print("\n[PASO 4] Analizando dictamen del banco...")
        
        if any(w in html_respuesta for w in ["exitoso", "aprobado", "verificado"]):
            resultado_final = {"status": "APROBADO", "mensaje": "Pago validado y consumido exitosamente por el banco."}
            print("  🎉 DICTAMEN: Aprobado.")
            
        elif any(w in html_respuesta for w in ["ya utilizad", "ya procesad", "repetid", "ya ha sido procesada"]):
            resultado_final = {"status": "YA_UTILIZADO", "mensaje": "Fraude prevenido: Esta referencia ya fue cobrada y validada anteriormente."}
            print("  🚨 DICTAMEN: Rechazado (Referencia repetida / ya procesada).")
            
        elif any(w in html_respuesta for w in ["no encontrad", "inválid", "rechazad", "no coincide"]):
            resultado_final = {"status": "NO_ENCONTRADO", "mensaje": "El banco no encontró ningún pago que coincida con estos datos exactos."}
            print("  🚫 DICTAMEN: Rechazado (No encontrado o datos inválidos).")
            
        else:
            # Si el banco devuelve algo distinto, extraemos el texto limpio del HTML para verlo en los logs de Render
            soup_res = BeautifulSoup(res_val.text, 'html.parser')
            texto_limpio = soup_res.get_text(separator=' | ', strip=True)
            print("\n  ⚠️ [ALERTA] Respuesta desconocida del banco. Texto extraído del HTML:")
            print(f"  {texto_limpio[:500]}...") # Imprime los primeros 500 caracteres
            
            resultado_final = {"status": "DESCONOCIDO", "mensaje": "Respuesta no estándar del banco. Revisa los logs de Render."}
             
    except Exception as e:
        print(f"\n❌ [BOT EXCEPCIÓN] Error procesando la orden: {e}")
        resultado_final = {"status": "ERROR", "mensaje": str(e)}
        
    finally:
        # --- PASO 5: LOGOUT ---
        if api_token_global:
            try:
                print("\n[PASO 5] Cerrando sesión bancaria de forma segura...")
                session.headers.update({'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': None})
                session.post('https://tesoropagos.bt.com.ve/logout', data={'_token': api_token_global}, allow_redirects=False, timeout=5)
            except Exception as e:
                print(f"[PASO 5 ERROR] Error cerrando sesión: {e}")
                
    # --- PASO 6: ACTUALIZAR FIRESTORE ---
    try:
        order_ref = db.collection('store_orders').document(order_id)
        
        if resultado_final['status'] == 'APROBADO':
            order_ref.update({
                'status': 'approved',
                'bot_verification_msg': resultado_final['mensaje'],
                'verified_at': firestore.SERVER_TIMESTAMP
            })
            print(f"\n✅ [FIRESTORE] Orden {order_id} actualizada a 'approved'.")
            
        elif resultado_final['status'] in ['YA_UTILIZADO', 'NO_ENCONTRADO']:
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': resultado_final['mensaje']
            })
            print(f"\n🛡️ [FIRESTORE] Orden {order_id} rechazada por seguridad ({resultado_final['status']}).")
            
        else:
            order_ref.update({
                'bot_verification_msg': f"Error/Revisión Manual: {resultado_final['mensaje']}"
            })
            print(f"\n⚠️ [FIRESTORE] Mensaje técnico guardado. Requiere revisión manual para la orden {order_id}.")
            
    except Exception as e:
        print(f"❌ [FIRESTORE ERROR] Falló la actualización de la orden {order_id}: {e}")

    print("="*80 + "\n")

# =========================================================
# 5. LISTENER EN TIEMPO REAL (FIRESTORE)
# =========================================================
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            order_id = change.document.id
            order_data = change.document.to_dict()
            print(f"⚡ [FIRESTORE EVENT] Nueva orden pendiente detectada: {order_id}")
            
            threading.Thread(
                target=procesar_validacion_en_banco, 
                args=(order_id, order_data)
            ).start()

def start_firestore_listener():
    print("🚀 [BOT] Iniciando Listener en tiempo real para colección 'store_orders' (status == pending_verification)...")
    orders_ref = db.collection('store_orders').where(filter=FieldFilter('status', '==', 'pending_verification'))
    orders_ref.on_snapshot(on_snapshot)

start_firestore_listener()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
