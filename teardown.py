#!/usr/bin/env python3
import os
import shelve
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
DB_PATH = "aws_resources.db"

# Nombres de recursos (Deben coincidir con deploy_script.py)
LOADER_FUNC = "LoadInventoryFunction"
API_FUNC    = "GetInventoryApiFunction"
NOTIFY_FUNC = "NotifyLowStockFunction"
TABLE_NAME  = "Inventory"

session = boto3.Session(region_name=REGION)
s3 = session.client("s3")
dynamodb = session.client("dynamodb")
lambda_client = session.client("lambda")
sns = session.client("sns")
apigateway = session.client("apigatewayv2")

def empty_bucket(bucket, region=REGION):
    """Vacía un bucket completamente (versiones y marcadores de borrado incluidos)."""
    if not bucket: return
    s3r = boto3.client("s3", region_name=region)
    
    print(f"[S3] Vaciando bucket: {bucket}...")
    try:
        # 1. Versiones y DeleteMarkers
        paginator = s3r.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            objects_to_delete = []
            for v in page.get("Versions", []):
                objects_to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
            for m in page.get("DeleteMarkers", []):
                objects_to_delete.append({"Key": m["Key"], "VersionId": m["VersionId"]})
            
            if objects_to_delete:
                # Borrar en lotes de 1000
                for i in range(0, len(objects_to_delete), 1000):
                    batch = objects_to_delete[i:i+1000]
                    s3r.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})

        # 2. Objetos normales (si no hay versiones)
        paginator2 = s3r.get_paginator("list_objects_v2")
        for page in paginator2.paginate(Bucket=bucket):
            objects_to_delete = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects_to_delete:
                for i in range(0, len(objects_to_delete), 1000):
                    batch = objects_to_delete[i:i+1000]
                    s3r.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})

    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchBucket':
            print(f"[S3] El bucket {bucket} ya no existe.")
        else:
            print(f"[ERROR] Falló vaciado de {bucket}: {e}")

def delete_bucket(bucket_name):
    if not bucket_name: return
    try:
        # S3 es global, pero necesitamos saber dónde está para borrarlo correctamente
        loc = s3.get_bucket_location(Bucket=bucket_name).get("LocationConstraint")
        region = "us-east-1" if loc in (None, "", "US") else loc
        
        empty_bucket(bucket_name, region)
        
        boto3.client("s3", region_name=region).delete_bucket(Bucket=bucket_name)
        print(f"[S3] Bucket eliminado: {bucket_name}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'NoSuchBucket':
            print(f"[ERROR] No se pudo borrar bucket {bucket_name}: {e}")

def delete_lambda(func_name):
    try:
        lambda_client.delete_function(FunctionName=func_name)
        print(f"[Lambda] Función eliminada: {func_name}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            print(f"[Lambda] Error eliminando {func_name}: {e}")

def delete_dynamo_table(table_name):
    try:
        dynamodb.delete_table(TableName=table_name)
        print(f"[DDB] Tabla eliminada: {table_name}")
        # Opcional: Esperar a que se borre
        waiter = dynamodb.get_waiter('table_not_exists')
        waiter.wait(TableName=table_name)
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            print(f"[DDB] Error eliminando tabla {table_name}: {e}")

def delete_api_gateway(api_url):
    if not api_url: return
    try:
        # Extraer API ID de la URL: https://{api_id}.execute-api...
        api_id = api_url.split("https://")[1].split(".")[0]
        apigateway.delete_api(ApiId=api_id)
        print(f"[API GW] API eliminada (ID: {api_id})")
    except (IndexError, ClientError) as e:
        print(f"[API GW] No se pudo eliminar API desde URL {api_url}: {e}")

def delete_sns_topic(topic_arn):
    if not topic_arn: return
    try:
        sns.delete_topic(TopicArn=topic_arn)
        print(f"[SNS] Topic eliminado: {topic_arn}")
    except ClientError as e:
        print(f"[SNS] Error eliminando topic: {e}")

def delete_triggers(func_name):
    """Borra los Event Source Mappings (ej: DynamoDB Streams)"""
    try:
        mappings = lambda_client.list_event_source_mappings(FunctionName=func_name)
        for m in mappings['EventSourceMappings']:
            lambda_client.delete_event_source_mapping(UUID=m['UUID'])
            print(f"[Lambda] Trigger eliminado para {func_name} (UUID: {m['UUID']})")
    except ClientError:
        pass

if __name__ == "__main__":
    print("=== INICIANDO TEARDOWN (Borrado de recursos) ===")
    
    # 1. Leer recursos del archivo local
    with shelve.open(DB_PATH) as db:
        uploads_bucket = db.get("uploads-bucket")
        web_bucket = db.get("web-bucket")
        api_url = db.get("api-url")
        sns_arn = db.get("sns-arn")
        
        # Limpiamos la DB para que el próximo deploy sea limpio
        db.clear()
        print("[Config] Base de datos local eliminada.")

    # 2. Borrar API Gateway (Lo primero para cortar tráfico)
    delete_api_gateway(api_url)

    # 3. Borrar Triggers y Lambdas
    for func in [LOADER_FUNC, API_FUNC, NOTIFY_FUNC]:
        delete_triggers(func)
        delete_lambda(func)

    # 4. Borrar DynamoDB
    delete_dynamo_table(TABLE_NAME)

    # 5. Borrar SNS
    delete_sns_topic(sns_arn)

    # 6. Borrar S3 Buckets
    delete_bucket(uploads_bucket)
    delete_bucket(web_bucket)

    print("\n=== TEARDOWN COMPLETO ===")