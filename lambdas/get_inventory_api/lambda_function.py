import json
import os
import boto3
import urllib.parse
from boto3.dynamodb.conditions import Key
from decimal import Decimal

# Configuración
TABLE_NAME = os.environ.get("TABLE_NAME", "Inventory")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

# Clase auxiliar para convertir objetos Decimal de DynamoDB a int/float estándar
# Sin esto, json.dumps fallará con "Object of type Decimal is not JSON serializable"
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super(DecimalEncoder, self).default(obj)

def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*", # Importante para CORS
            "Access-Control-Allow-Methods": "GET, OPTIONS"
        },
        # Usamos la clase DecimalEncoder aquí
        "body": json.dumps(body, cls=DecimalEncoder)
    }

def lambda_handler(event, context):
    print("Event:", json.dumps(event))
    
    # Obtener parámetros del path (inyectados por API Gateway)
    path_params = event.get("pathParameters") or {}
    store_param = path_params.get("store")

    try:
        items = []
        
        if store_param:
            # CASO 1: Filtrar por tienda (/items/{store})
            # Usamos QUERY que es eficiente porque 'store' es la Partition Key
            print(f"Consultando tienda: {store_param}")
            
            # Decodificar URL (ej: "New%20York" -> "New York")
            store_name = urllib.parse.unquote_plus(store_param)
            
            response = table.query(
                KeyConditionExpression=Key('store').eq(store_name)
            )
            items = response.get('Items', [])
            
        else:
            # CASO 2: Obtener todo (/items)
            # Usamos SCAN (menos eficiente, pero necesario para listar todo)
            print("Escaneando toda la tabla...")
            response = table.scan()
            items = response.get('Items', [])
            
            # Nota: table.scan tiene límite de 1MB. Si tienes miles de items,
            # deberías manejar paginación con 'LastEvaluatedKey', pero para
            # esta práctica el scan simple es suficiente.

        print(f"Encontrados {len(items)} registros.")
        return _resp(200, items)

    except Exception as e:
        print(f"Error: {e}")
        return _resp(500, {"error": str(e)})