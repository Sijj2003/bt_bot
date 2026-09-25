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
# FUNCIONES AUXILIARES DE LIMPIEZA Y PARSEO
# =========================================================
def parse_monto_safe(monto_val):
    """Convierte montos en string (ej: '115,51', '1.115,51', 115.51) a float estándar."""
    try:
        if isinstance(monto_val, (int, float)):
            return float(monto_val)
        m_str = str(monto_val).strip()
        if ',' in m_str:
            m_str = m_str.replace('.', '').replace(',', '.')
        return float(m_str)
    except Exception:
        return -1.0

def solo_numeros(cadena):
    """Filtra solo los dígitos numéricos de un texto."""
    return re.sub(r'\D', '', str(cadena or ''))

# =========================================================
# 4. LÓGICA DEL BOT BANCARIO CON SUPER-LOGGING
# =========================================================
def procesar_validacion_en_banco(order_id, datos_orden):
    print("\n" + "="*80)
    print(f"🚀 [INICIO DE PROCESAMIENTO] Orden ID: {order_id}")
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
    resultado_final = {"status": "ERROR", "mensaje": "Fallo desconocido durante la ejecución del bot."}

    try:
        # --- PASO 0: EXTRAER DATOS FIRESTORE CON LOG DETALLADO ---
        payment_details = datos_orden.get('payment_details', {})
        print(f"[FIRESTORE RAW payment_details]: {json.dumps(payment_details, ensure_ascii=False)}")
        
        referencia_raw = payment_details.get('referencia') or payment_details.get('reference') or ''
        banco_orden = str(payment_details.get('banco', '')).strip()
        telefono_orden = str(payment_details.get('telefono', '')).strip()
        monto_orden_raw = payment_details.get('monto_bot', '')
        
        # Limpieza de valores para comparación
        referencia_6_digitos = str(referencia_raw).strip().zfill(6)[-6:]
        monto_orden_float = parse_monto_safe(monto_orden_raw)
        tel_orden_num = solo_numeros(telefono_orden)
        banco_orden_num = solo_numeros(banco_orden)

        print("\n[DATOS EXTRAÍDOS Y NORMALIZADOS DE LA ORDEN]:")
        print(f"  • Referencia (últimos 6): '{referencia_6_digitos}' (Raw: '{referencia_raw}')")
        print(f"  • Monto: {monto_orden_float} VES (Raw: '{monto_orden_raw}')")
        print(f"  • Teléfono (dígitos): '{tel_orden_num}' (Raw: '{telefono_orden}')")
        print(f"  • Banco (dígitos): '{banco_orden_num}' (Raw: '{banco_orden}')")

        if not referencia_6_digitos or monto_orden_float <= 0:
            raise Exception("La orden carece de referencia o el monto es inválido en 'payment_details'.")

        session.cookies.clear()
        
        # --- PASO 1: LOGIN BANCO ---
        print("\n[PASO 1] Solicitando formulario de login...")
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
        
        print("[PASO 1] Enviando credenciales al banco...")
        session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/login'})
        res_login = session.post('https://tesoropagos.bt.com.ve/login', data=payload_login, allow_redirects=False, timeout=12)
        
        location = res_login.headers.get('Location', '')
        if res_login.status_code != 302 or 'login' in location:
            raise Exception("Credenciales del banco rechazadas o sesión bloqueada.")
            
        print("[PASO 1] Login exitoso.")
        
        # --- PASO 2: TOKEN CSRF ---
        print("\n[PASO 2] Extrayendo CSRF Token...")
        res_dash = session.get('https://tesoropagos.bt.com.ve/pago-movil', timeout=12)
        soup_dash = BeautifulSoup(res_dash.text, 'html.parser')
        meta_token = soup_dash.find('meta', {'name': 'csrf-token'})
        api_token = meta_token['content'] if meta_token else payload_login.get('_token')
        api_token_global = api_token
        print(f"[PASO 2] Token obtenido: {api_token[:10]}...")

        # --- PASO 3: CONSULTAR API DE MOVIMIENTOS ---
        print("\n[PASO 3] Consultando movimientos del día...")
        tz_ve = ZoneInfo("America/Caracas")
        hoy = datetime.now(tz_ve).strftime("%d/%m/%Y")
        
        payload_api = {"fechaDesde": hoy, "fechaHasta": hoy}
        
        session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'X-CSRF-TOKEN': api_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://tesoropagos.bt.com.ve/pago-movil'
        })
        
        res_api = session.post('https://tesoropagos.bt.com.ve/pago-movil/movimientos', json=payload_api, timeout=12)
        
        if res_api.status_code != 200:
            raise Exception(f"La API bancaria respondió con HTTP {res_api.status_code}.")
             
        movimientos_json = res_api.json()
        
        # Imprimir JSON completo recibido del banco para depuración visual
        print("\n" + "-"*60)
        print(" [RESPUESTA RAW DE LA API DEL BANCO]")
        print(json.dumps(movimientos_json, indent=2, ensure_ascii=False))
        print("-"*60 + "\n")
        
        # --- PASO 4: EVALUACIÓN Y MATCH DE MOVIMIENTOS ---
        print("[PASO 4] Analizando movimientos fila por fila...")
        pago_encontrado = None
        
        lista_movs = movimientos_json if isinstance(movimientos_json, list) else movimientos_json.get('data', []) if isinstance(movimientos_json, dict) else []
        
        print(f"[PASO 4] Cantidad de movimientos devueltos por el banco hoy: {len(lista_movs)}")

        for idx, mov in enumerate(lista_movs, 1):
            if not isinstance(mov, dict):
                continue

            print(f"\n--- Analizando Movimiento #{idx} ---")
            print(f"  Campos/Claves presentes en este registro: {list(mov.keys())}")

            # 1. Extraer Referencia del banco
            ref_banco_raw = str(
                mov.get('referencia') or 
                mov.get('numReferencia') or 
                mov.get('nroReferencia') or 
                mov.get('secuencia') or ''
            ).strip()
            ref_banco_6 = ref_banco_raw.zfill(6)[-6:] if ref_banco_raw else ''

            # 2. Extraer Monto del banco
            monto_banco_raw = mov.get('monto') or mov.get('montoTransaccion') or mov.get('monto_transaccion') or '0'
            monto_banco_float = parse_monto_safe(monto_banco_raw)

            # 3. Extraer Teléfono del banco
            tel_banco_raw = str(mov.get('telefono') or mov.get('telefonoOrigen') or mov.get('celular') or mov.get('origen') or '').strip()
            tel_banco_num = solo_numeros(tel_banco_raw)

            # 4. Extraer Banco del banco
            banco_api_raw = str(mov.get('banco') or mov.get('bancoOrigen') or mov.get('codBanco') or mov.get('codigoBanco') or '').strip()
            banco_api_num = solo_numeros(banco_api_raw)

            # --- VERIFICACIÓN DETALLADA ---
            match_ref = (ref_banco_6 == referencia_6_digitos)
            match_monto = abs(monto_orden_float - monto_banco_float) < 0.1
            match_tel = (tel_orden_num[-7:] == tel_banco_num[-7:]) if (tel_orden_num and tel_banco_num) else False
            match_banco = (banco_orden_num[-3:] in banco_api_num) if (banco_orden_num and banco_api_num) else False

            print(f"  • REF  -> Banco: '{ref_banco_6}' vs Orden: '{referencia_6_digitos}' -> Match: {'✅ OK' if match_ref else '❌ DIFERENTE'}")
            print(f"  • MONTO-> Banco: {monto_banco_float} vs Orden: {monto_orden_float} -> Match: {'✅ OK' if match_monto else '❌ DIFERENTE'}")
            print(f"  • TEL  -> Banco: '{tel_banco_num}' vs Orden: '{tel_orden_num}' -> Match: {'✅ OK' if match_tel else '❌ DIFERENTE'}")
            print(f"  • BANCO-> Banco: '{banco_api_num}' vs Orden: '{banco_orden_num}' -> Match: {'✅ OK' if match_banco else '❌ DIFERENTE'}")

            # Si coinciden TODOS los factores, aprobamos
            if match_ref and match_monto and match_tel and match_banco:
                pago_encontrado = mov
                print(f"\n🎉 ¡MATCH PERFECTO ENCONTRADO EN MOVIMIENTO #{idx}!")
                break
            elif match_ref:
                print(f"⚠️ La referencia coincidiïó pero ALGUNOS datos difieren (posible discrepancia de nombres de llaves o datos incorrectos ingresados por el cliente).")

        if pago_encontrado:
            resultado_final = {"status": "APROBADO", "mensaje": "Pago verificado exitosamente (Ref, Monto, Tlf y Banco coinciden)."}
        else:
            resultado_final = {"status": "NO_ENCONTRADO", "mensaje": "La referencia no figura en los movimientos de hoy o alguno de los datos (Monto/Teléfono/Banco) no coincidió."}
             
    except Exception as e:
        print(f"\n❌ [BOT EXCEPCIÓN] Error procesando la orden: {e}")
        resultado_final = {"status": "ERROR", "mensaje": str(e)}
        
    finally:
        # --- PASO 5: LOGOUT ---
        if api_token_global:
            try:
                print("\n[PASO 5] Cerrando sesión bancaria...")
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
            
        elif resultado_final['status'] == 'NO_ENCONTRADO':
            order_ref.update({
                'status': 'rejected',
                'bot_verification_msg': resultado_final['mensaje']
            })
            print(f"\n🚫 [FIRESTORE] Orden {order_id} actualizada a 'rejected'.")
            
        else:
            order_ref.update({
                'bot_verification_msg': f"Error en verificación: {resultado_final['mensaje']}"
            })
            print(f"\n⚠️ [FIRESTORE] Guardado mensaje de error técnico para orden {order_id}.")
            
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
            print(f"⚡ [FIRESTORE EVENT] Nueva orden detectada: {order_id}")
            
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
