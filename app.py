import os
import time
import requests
from bs4 import BeautifulSoup
import threading
import json
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask

# ==========================================
# 1. INICIALIZAR FIREBASE EN EL BOT
# ==========================================
# Leemos el JSON de Firebase desde una variable de entorno segura en Render
firebase_creds_json = os.environ.get("FIREBASE_JSON")
cred = credentials.Certificate(json.loads(firebase_creds_json))
firebase_admin.initialize_app(cred)
db = firestore.client()

SUCURSAL = os.environ.get("TESORO_SUCURSAL", "01334301")
CAJA = os.environ.get("TESORO_CAJA", "03")
PASSWORD = os.environ.get("TESORO_PASS", "31103356")

# ==========================================
# 2. FUNCIÓN DE VERIFICACIÓN DEL BANCO
# ==========================================
def verificar_pago_tesoro(sucursal, caja, password, datos_pago):
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'es-ES,es;q=0.9',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    })

    api_token_global = None

    try:
        session.cookies.clear()
        
        # 1. LOGIN
        response_get = session.get('https://tesoropagos.bt.com.ve/login')
        soup = BeautifulSoup(response_get.text, 'html.parser')
        
        payload_login = {}
        for hidden_input in soup.find_all('input', type='hidden'):
            if hidden_input.get('name'):
                payload_login[hidden_input.get('name')] = hidden_input.get('value', '')
        
        if '_token' not in payload_login:
            return {"status": "ERROR", "mensaje": "Fallo obteniendo token CSRF."}

        payload_login['security_code'] = sucursal
        payload_login['box_number'] = caja
        payload_login['password'] = password
        
        session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/login'})
        response_login = session.post('https://tesoropagos.bt.com.ve/login', data=payload_login, allow_redirects=False)
        
        location = response_login.headers.get('Location', '')
        if response_login.status_code != 302 or 'login' in location:
            return {"status": "ERROR", "mensaje": "Credenciales inválidas."}
        
        # 2. PREPARAR VALIDACIÓN
        response_dash = session.get('https://tesoropagos.bt.com.ve/pago-movil')
        soup_dash = BeautifulSoup(response_dash.text, 'html.parser')
        meta_token = soup_dash.find('meta', {'name': 'csrf-token'})
        api_token = meta_token['content'] if meta_token else payload_login['_token']
        api_token_global = api_token
        
        # 3. ENVIAR FORMULARIO AL BANCO (Usamos la estructura de tu nuevo backend)
        detalles = datos_pago.get('payment_details', {})
        payload_validacion = {
            '_token': api_token,
            'monto': detalles.get('monto_bot', ''),
            'banco': detalles.get('banco', ''),
            'telefono': detalles.get('telefono', ''),
            'referencia': str(detalles.get('referencia', ''))[-6:]
        }
        
        session.headers.update({'Referer': 'https://tesoropagos.bt.com.ve/pago-movil', 'Content-Type': 'application/x-www-form-urlencoded'})
        response_validacion = session.post('https://tesoropagos.bt.com.ve/pago-movil', data=payload_validacion, allow_redirects=True)
        html_respuesta = response_validacion.text.lower()
        
        # 4. LOGOUT SEGURO
        session.headers.update({'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': None})
        session.post('https://tesoropagos.bt.com.ve/logout', data={'_token': api_token}, allow_redirects=False)
        
        # 5. PARSEO
        if "se validó el pago de forma exitosa" in html_respuesta or "se valido el pago de forma exitosa" in html_respuesta:
             return {"status": "APROBADO", "mensaje": "Pago verificado exitosamente."}
        elif "este pago ya fue confirmado anteriormente" in html_respuesta or "ya utilizad" in html_respuesta:
             return {"status": "YA_UTILIZADO", "mensaje": "La referencia ya fue usada."}
        elif "no encontrad" in html_respuesta or "inválid" in html_respuesta or "rechazad" in html_respuesta:
             return {"status": "NO_ENCONTRADO", "mensaje": "Los datos no coinciden."}
        else:
             return {"status": "NO_ENCONTRADO", "mensaje": "Respuesta desconocida del banco."}

    except Exception as e:
        if api_token_global:
            session.headers.update({'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': None})
            session.post('https://tesoropagos.bt.com.ve/logout', data={'_token': api_token_global}, allow_redirects=False)
        return {"status": "ERROR", "mensaje": str(e)}

# ==========================================
# 3. EL LISTENER (EL VIGILANTE DE FIREBASE)
# ==========================================
def on_snapshot(doc_snapshot, changes, read_time):
    for change in changes:
        if change.type.name in ['ADDED', 'MODIFIED']:
            doc = change.document
            pago = doc.to_dict()
            
            # Buscamos el estado exacto que configuraste en tu backend de PythonAnywhere
            if pago.get('status') == 'pending_verification' and pago.get('payment_method') == 'pago_movil':
                print(f"🔔 ¡Nuevo pago detectado! Pedido: {doc.id}")
                
                resultado_banco = verificar_pago_tesoro(SUCURSAL, CAJA, PASSWORD, pago)
                print(f"🏦 Resultado del banco para {doc.id}: {resultado_banco['status']}")
                
                # Mapeamos la respuesta al formato de tu base de datos
                status_final = "approved" if resultado_banco['status'] == "APROBADO" else "rejected"
                
                doc.reference.update({
                    'status': status_final,
                    'bot_verification_msg': resultado_banco.get('mensaje', ''),
                    'verified_at': firestore.SERVER_TIMESTAMP
                })
                print(f"✅ Pedido {doc.id} actualizado en Firebase a {status_final}.")
                time.sleep(3)

def iniciar_vigilancia():
    print("🚀 Bot Vigilante de Firebase INICIADO.")
    query = db.collection('store_orders').where('status', '==', 'pending_verification')
    query_watch = query.on_snapshot(on_snapshot)
    while True:
        time.sleep(3600)

# ==========================================
# 4. SERVIDOR FLASK (PARA MANTENER RENDER ACTIVO)
# ==========================================
app = Flask(__name__)

@app.route('/')
def keep_alive():
    return "✅ El Bot de Gymenez está vivo y vigilando Firebase en las sombras."

if __name__ == '__main__':
    # Arrancamos el vigilante en segundo plano
    hilo_bot = threading.Thread(target=iniciar_vigilancia, daemon=True)
    hilo_bot.start()
    
    # Arrancamos Flask en el puerto que exige Render
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)