import json
import urllib.parse
import boto3
import csv
import os

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# 1. Usar variable de entorno para el nombre de la tabla
table_name = os.environ.get('TABLE_NAME', 'Inventory')
inventoryTable = dynamodb.Table(table_name)

def lambda_handler(event, context):
    print("Event received:", json.dumps(event, indent=2))

    # Obtener bucket y key
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'])
    localFilename = '/tmp/inventory.txt'

    try:
        s3.download_file(bucket, key, localFilename)
    except Exception as e:
        print(f"Error descargando {key} de {bucket}: {e}")
        raise e

    with open(localFilename, 'r', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile, delimiter=',')
        rowCount = 0

        # Usar batch_writer es mucho más eficiente y rápido para Lambdas
        with inventoryTable.batch_writer() as batch:
            for row in reader:
                rowCount += 1
                
                # Convertir nombres de columnas si el CSV trae mayúsculas, 
                # asegurando que coincidan con row['...']
                # Asumimos que el CSV tiene cabeceras: store,item,count
                
                try:
                    store_val = row['store']
                    item_val = row['item']
                    count_val = int(row['count'])
                    
                    print(f"Insertando: {store_val}, {item_val}, {count_val}")

                    # 2. CORRECCIÓN CRÍTICA: Las claves del diccionario deben 
                    # coincidir EXACTAMENTE con el KeySchema de DynamoDB (minúsculas)
                    batch.put_item(
                        Item={
                            'store': store_val,  # <--- Minuscula (Partition Key)
                            'item':  item_val,   # <--- Minuscula (Sort Key)
                            'count': count_val,  # <--- Atributo normal (puede ser lo que quieras)
                            'Count': count_val   # (Opcional) Guardamos ambos si el frontend espera Mayuscula
                        }
                    )
                except KeyError as e:
                    print(f"Error de formato en CSV (columna faltante): {e}")
                except Exception as e:
                    print(f"Error insertando fila: {e}")

    return f"{rowCount} counts inserted"