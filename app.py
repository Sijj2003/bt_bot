import os
import json
import time
import base64
import threading
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

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
        # Intenta decodificar si viene codificado en Base64
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
# 4. LÓGICA DEL BOT BANCARIO (Scraping + Actualización Directa)
# =========================================================
def procesar_validacion_en_banco(order_id, datos_orden):
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
    resultado_final = {"status": "ERROR", "mensaje": "Fallo desconocido durante la ejecución del bot."}

    try:
        referencia_raw = datos_orden.get('referencia') or datos_orden.get('reference') or ''
        # Obtener los últimos 6 dígitos limpios
        referencia_6_digitos = str(referencia_raw).strip().zfill(6)[-6:]
        
        print(f"[BOT] Procesando orden {order_id}. Buscando últimos 6 dígitos: '{referencia_6_digitos}'...")
        session.cookies.clear()
        
        # --- PASO 1: LOGIN ---
        print("[BOT] Solicitando página de login...")
        res_get = session.get('https://tesoropagos.bt.com.ve/login', timeout=12)
        soup = BeautifulSoup(res_get.text, 'html.parser')
        
        payload_login = {}
        for inp in soup.find_all('input', type='hidden'):
            if inp.get('name'):
                payload_login[inp.get('name')] = inp.get('value', '')
                
        payload_login['security_code'] = TESORO_SUCURSAL
        payload_login['box_number'] = TESORO_CAJA
        payload_login['password'] = TESORO_PASS
        
        time.sleep(1)
        
        print("[BOT] Enviando credenciales...")
        session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/login'})
        res_login = session.post('https://tesoropagos.bt.com.ve/login', data=payload_login, allow_redirects=False, timeout=12)
        
        location = res_login.headers.get('Location', '')
        if res_login.status_code != 302 or 'login' in location:
            raise Exception("Credenciales inválidas, usuario bloqueado, o sesión atorada en el banco.")
            
        print("[BOT] Login exitoso. Preparando API...")
        
        # --- PASO 2: OBTENER TOKEN PARA API ---
        res_dash = session.get('https://tesoropagos.bt.com.ve/pago-movil', timeout=12)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        meta_token = soup_dash.find('meta', {'name': 'csrf-token'})
        api_token = meta_token['content'] if meta_token else payload_login.get('_token')
        api_token_global = api_token
        
        # --- PASO 3: CONSULTAR API DE MOVIMIENTOS (ZONA HORARIA CARACAS) ---
        print("[BOT] Consultando movimientos en el banco...")
        tz_ve = ZoneInfo("America/Caracas")
        hoy = datetime.now(tz_ve).strftime("%d/%m/%Y")
        
        payload_api = {
            "fechaDesde": hoy,
            "fechaHasta": hoy
        }
        
        session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'X-CSRF-TOKEN': api_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://tesoropagos.bt.com.ve/pago-movil'
        })
        
        res_api = session.post('https://tesoropagos.bt.com.ve/pago-movil/movimientos', json=payload_api, timeout=12)
        
        if res_api.status_code != 200:
             raise Exception(f"La API del banco devolvió error HTTP {res_api.status_code}.")
             
        movimientos = res_api.json()
        print(f"[BOT DEBUG] Respuesta raw del banco: {json.dumps(movimientos, ensure_ascii=False)}")
        
        # --- PASO 4: BUSCAR REFERENCIA ---
        print("[BOT] Analizando movimientos del día...")
        pago_encontrado = None
        
        lista_movs = movimientos if isinstance(movimientos, list) else movimientos.get('data', []) if isinstance(movimientos, dict) else []
        
        for mov in lista_movs:
            if isinstance(mov, dict):
                # Extraer referencia probando diferentes nombres de campos posibles
                ref_banco_raw = str(
                    mov.get('referencia') or 
                    mov.get('numReferencia') or 
                    mov.get('nroReferencia') or 
                    mov.get('secuencia') or ''
                ).strip()
                
                if ref_banco_raw:
                    # Extraer únicamente los últimos 6 dígitos de la referencia recibida del banco
                    ref_banco_6 = ref_banco_raw.zfill(6)[-6:]
                    print(f"[BOT DEBUG] Comparando -> Banco: '{ref_banco_raw}' (últimos 6: '{ref_banco_6}') vs Orden: '{referencia_6_digitos}'")
                    
                    if ref_banco_6 == referencia_6_digitos:
                        pago_encontrado = mov
                        break
        
        if pago_encontrado:
             resultado_final = {"status": "APROBADO", "mensaje": "Pago verificado exitosamente en los movimientos de hoy."}
             print(f"[BOT] ¡Referencia {referencia_6_digitos} encontrada para orden {order_id}!")
        else:
             resultado_final = {"status": "NO_ENCONTRADO", "mensaje": "La referencia no figura en los movimientos bancarios de hoy."}
             print(f"[BOT] Referencia {referencia_6_digitos} NO encontrada.")
             
    except Exception as e:
        print(f"[BOT ERROR] {e}")
        resultado_final = {"status": "ERROR", "mensaje": str(e)}
        
    finally:
        # --- PASO 5: LOGOUT SEGURO ---
        if api_token_global:
            try:
                print("[BOT] Cerrando sesión bancaria...")
                session.headers.update({'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': None})
                session.post('https://tesoropagos.bt.com.ve/logout', data={'_token': api_token_global}, allow_redirects=False, timeout=5)
            except Exception as e:
                print(f"[BOT ERROR LOGOUT] No se pudo cerrar la sesión: {e}")
                
    # --- PASO 6: ACTUALIZAR ESTADO DIRECTO EN FIRESTORE ---
    try:
        order_ref = db.collection('store_orders').document(order_id)
        
        if resultado_final['status'] == 'APROBADO':
            order_ref.update({
                'status': 'approved',
                'bot_verification_msg': resultado_final['mensaje'],
                'verified_at': firestore.SERVER_TIMESTAMP
            })
            print(f"[FIRESTORE] Orden {order_id} actualizada a 'approved'.")
            
        elif resultado_final['status'] == 'NO_ENCONTRADO':
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': resultado_final['mensaje']
            })
            print(f"[FIRESTORE] Orden {order_id} actualizada a 'rejected'.")
            
        else:
            order_ref.update({
                'bot_verification_msg': f"Error en verificación: {resultado_final['mensaje']}"
            })
            print(f"[FIRESTORE] Registrado mensaje de error en orden {order_id}.")
            
    except Exception as e:
        print(f"[FIRESTORE ERROR] Falló la actualización de la orden {order_id}: {e}")

# =========================================================
# 5. LISTENER EN TIEMPO REAL (FIRESTORE)
# =========================================================
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            order_id = change.document.id
            order_data = change.document.to_dict()
            print(f"⚡ [FIRESTORE EVENT] Nueva orden detectada: {order_id}")
            
            # Ejecutar en hilo secundario para no bloquear el listener de Firestore
            threading.Thread(
                target=procesar_validacion_en_banco, 
                args=(order_id, order_data)
            ).start()

def start_firestore_listener():
    print("🚀 [BOT] Iniciando Listener en tiempo real para colección 'store_orders' (status == pending_verification)...")
    orders_ref = db.collection('store_orders').where(filter=FieldFilter('status', '==', 'pending_verification'))
    orders_ref.on_snapshot(on_snapshot)

# Iniciar la escucha en segundo plano
start_firestore_listener()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
