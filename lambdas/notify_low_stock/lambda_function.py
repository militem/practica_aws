import json
import os
import boto3

sns = boto3.client('sns')
TOPIC_ARN = os.environ.get('TOPIC_ARN')
THRESHOLD = 5

def get_count(image):
    """Ayuda a extraer el entero de forma segura del formato DynamoDB JSON"""
    if not image:
        return 0
    # Busca 'count' (minúscula) como definimos en el loader
    try:
        val = image.get('count', {}).get('N')
        return int(val) if val else 0
    except:
        return 0

def lambda_handler(event, context):
    print(f"Procesando {len(event['Records'])} registros...")
    
    if not TOPIC_ARN:
        print("ERROR: La variable de entorno TOPIC_ARN no está definida.")
        return

    for record in event['Records']:
        event_name = record['eventName']
        
        # Solo nos interesan inserciones o modificaciones
        if event_name not in ['INSERT', 'MODIFY']:
            continue
            
        try:
            # 1. Obtener datos nuevos
            new_image = record['dynamodb'].get('NewImage', {})
            new_count = get_count(new_image)
            
            # Datos descriptivos (con seguridad .get)
            item_name = new_image.get('item', {}).get('S', 'Desconocido')
            store_name = new_image.get('store', {}).get('S', 'Desconocida')

            should_notify = False

            # 2. Lógica inteligente de notificación
            if new_count < THRESHOLD:
                if event_name == 'INSERT':
                    # Es nuevo y tiene poco stock -> AVISAR
                    print(f"[INSERT] Producto nuevo con stock bajo ({new_count})")
                    should_notify = True
                
                elif event_name == 'MODIFY':
                    # Ya existía. ¿Estaba bajo antes?
                    old_image = record['dynamodb'].get('OldImage', {})
                    old_count = get_count(old_image)
                    
                    if old_count >= THRESHOLD:
                        # CRÍTICO: Antes estaba bien, ahora bajó -> AVISAR
                        print(f"[MODIFY] El stock cayó por debajo del umbral (Antes: {old_count}, Ahora: {new_count})")
                        should_notify = True
                    else:
                        # Ya estaba bajo antes. No spammear.
                        print(f"[SKIP] El stock ya estaba bajo ({old_count} -> {new_count}). No se envía alerta.")

            # 3. Enviar Notificación
            if should_notify:
                msg = (f"ALERTA DE STOCK BAJO\n\n"
                       f"Tienda: {store_name}\n"
                       f"Producto: {item_name}\n"
                       f"Stock Actual: {new_count}\n"
                       f"Umbral Mínimo: {THRESHOLD}\n\n"
                       f"Por favor, proceda a reabastecer.")
                
                sns.publish(
                    TopicArn=TOPIC_ARN,
                    Message=msg,
                    Subject=f"Stock Bajo: {item_name} en {store_name}"
                )
                print(f"-> Correo enviado para {item_name}")

        except Exception as e:
            print(f"Error procesando registro {record['eventID']}: {e}")
            
    return {"status": "ok"}