#!/usr/bin/env python3
import os
import io
import json
import time
import uuid
import shelve
import zipfile
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# ---------- Config ----------
load_dotenv()
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN")
EMAIL_NOTIFY = os.getenv("EMAIL_NOTIFY", "ale_dario_04@hotmail.es")
DB_PATH = "aws_resources.db"
ROLE_NAME = "LabRole"

# Nombres base de funciones
LOADER_FUNC_NAME = "LoadInventoryFunction"
API_FUNC_NAME    = "GetInventoryApiFunction"
NOTIFY_FUNC_NAME = "NotifyLowStockFunction"
TABLE_NAME       = "Inventory"

session = boto3.Session(region_name=AWS_DEFAULT_REGION)
s3 = session.client("s3")
dynamodb = session.client("dynamodb")
lambda_client = session.client("lambda")
sts = session.client("sts")
apigateway = session.client("apigatewayv2")

# ---------- Helpers ----------
def unique_suffix():
    return f"{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

def bucket_exists(name):
    try:
        s3.head_bucket(Bucket=name)
        return True
    except ClientError:
        return False

def create_bucket(name):
    if AWS_DEFAULT_REGION == "us-east-1":
        s3.create_bucket(Bucket=name)
    else:
        s3.create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": AWS_DEFAULT_REGION}
        )

def ensure_bucket(name):
    if bucket_exists(name):
        print(f"[S3] Bucket ya existe: {name}")
    else:
        create_bucket(name)
        print(f"[S3] Bucket creado: {name}")

def disable_bucket_bpa(bucket):
    try:
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
    except ClientError as e:
        print(f"[WARN] No se pudo desactivar BPA en {bucket}: {e}")

def apply_web_public_policy(bucket):
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadObjects",
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))

def labrole_arn():
    ident = sts.get_caller_identity()
    account = ident["Account"]
    partition = ident["Arn"].split(":")[1]
    return f"arn:{partition}:iam::{account}:role/{ROLE_NAME}"

def ensure_inventory_table(name):
    try:
        dynamodb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": "store", "KeyType": "HASH"},  # Partition Key
                {"AttributeName": "item", "KeyType": "RANGE"}   # Sort Key
            ],
            AttributeDefinitions=[
                {"AttributeName": "store", "AttributeType": "S"},
                {"AttributeName": "item", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
            StreamSpecification={
                'StreamEnabled': True,
                'StreamViewType': 'NEW_AND_OLD_IMAGES'
            }
        )
        print(f"[DDB] Creando tabla: {name}...")
        waiter = session.resource("dynamodb").meta.client.get_waiter("table_exists")
        waiter.wait(TableName=name)
    except dynamodb.exceptions.ResourceInUseException:
        print(f"[DDB] Tabla ya existe: {name}")

    return dynamodb.describe_table(TableName=name)["Table"]["TableArn"]

def ensure_http_api(api_name, lambda_arn):
    # 1. Crear API
    apis = apigateway.get_apis()["Items"]
    api_id = next((a["ApiId"] for a in apis if a["Name"] == api_name), None)
    
    if not api_id:
        resp = apigateway.create_api(
            Name=api_name,
            ProtocolType="HTTP",
            CorsConfiguration={
                "AllowOrigins": ["*"],
                "AllowMethods": ["GET", "OPTIONS"],
                "AllowHeaders": ["Content-Type"]
            }
        )
        api_id = resp["ApiId"]
        print(f"[API GW] API creada: {api_name} (ID: {api_id})")
    else:
        print(f"[API GW] API ya existe: {api_name} (ID: {api_id})")

    # 2. Permiso para API Gateway -> Lambda
    try:
        lambda_client.add_permission(
            FunctionName=lambda_arn,
            StatementId=f"ApiGatewayInvoke-{api_id}", # ID consistente
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{AWS_DEFAULT_REGION}:{sts.get_caller_identity()['Account']}:{api_id}/*/*"
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass 

    integration_resp = apigateway.create_integration(
        ApiId=api_id,
        IntegrationType="AWS_PROXY",
        IntegrationUri=lambda_arn,
        PayloadFormatVersion="2.0"
    )
    integration_id = integration_resp["IntegrationId"]

    # 3. Rutas
    for route_key in ["GET /items", "GET /items/{store}"]:
        try:
            apigateway.create_route(
                ApiId=api_id,
                RouteKey=route_key,
                Target=f"integrations/{integration_id}"
            )
        except apigateway.exceptions.ConflictException:
            pass 

    # 4. Stage
    try:
        apigateway.create_stage(
            ApiId=api_id,
            StageName="$default",
            AutoDeploy=True
        )
    except apigateway.exceptions.ConflictException:
        pass 

    endpoint = f"https://{api_id}.execute-api.{AWS_DEFAULT_REGION}.amazonaws.com"
    return endpoint

def build_lambda_zip_bytes(source_path: str = None) -> bytes:
    if source_path is None:
        source_path = os.path.join("lambda_function", "lambda_function.py")

    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"No se encontró el archivo: {source_path}")

    with open(source_path, "rb") as f:
        code_bytes = f.read()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("lambda_function.py", code_bytes)
    buf.seek(0)
    return buf.read()

def ensure_lambda_generic(function_name, role_arn, zip_bytes, env_vars=None, handler="lambda_function.lambda_handler"):
    if env_vars is None: env_vars = {}
    
    # ARN Base limpio (sin versiones)
    # Lo construimos manualmente o lo pedimos, pero esta es la forma más segura para Boto3
    account_id = sts.get_caller_identity()["Account"]
    base_arn = f"arn:aws:lambda:{AWS_DEFAULT_REGION}:{account_id}:function:{function_name}"

    try:
        lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.11",
            Role=role_arn,
            Handler=handler,
            Code={"ZipFile": zip_bytes},
            Timeout=30,
            MemorySize=256,
            Environment={"Variables": env_vars},
            Publish=True, # Publicamos versión, pero usaremos el ARN base
        )
        print(f"[Lambda] Función creada: {function_name}")
    except lambda_client.exceptions.ResourceConflictException:
        print(f"[Lambda] Función {function_name} ya existe. Actualizando código...")
        lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes, Publish=True)
        waiter = lambda_client.get_waiter("function_updated")
        waiter.wait(FunctionName=function_name)
        
        print(f"[Lambda] Actualizando configuración de {function_name}...")
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Role=role_arn,
            Environment={"Variables": env_vars},
        )
    
    # IMPORTANTE: Retornamos siempre el ARN base para evitar errores de validación con S3
    return base_arn

def configure_s3_lambda_trigger(bucket_name, lambda_arn, function_name):
    # ID ÚNICO que incluye el nombre del bucket para evitar conflictos si cambias de bucket
    statement_id = f"S3Invoke-{bucket_name}"

    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal="s3.amazonaws.com",
            SourceArn=f"arn:aws:s3:::{bucket_name}"
        )
    except lambda_client.exceptions.ResourceConflictException:
        # El permiso ya existe, perfecto.
        pass

    # Espera breve para propagación de IAM
    time.sleep(2)

    s3.put_bucket_notification_configuration(
        Bucket=bucket_name,
        NotificationConfiguration={
            'LambdaFunctionConfigurations': [{
                'LambdaFunctionArn': lambda_arn,
                'Events': ['s3:ObjectCreated:*'],
                'Filter': {'Key': {'FilterRules': [{'Name': 'suffix', 'Value': '.csv'}]}}
            }]
        }
    )
    print(f"[S3] Trigger configurado: {bucket_name} -> {function_name}")

def ensure_sns_topic(topic_name):
    resp = session.client("sns").create_topic(Name=topic_name)
    topic_arn = resp["TopicArn"]
    print(f"[SNS] Topic: {topic_arn}")
    return topic_arn

def subscribe_email(topic_arn, email):
    session.client("sns").subscribe(TopicArn=topic_arn, Protocol='email', Endpoint=email)
    print(f"[SNS] Suscripción enviada a {email}")

def create_dynamodb_trigger(lambda_name, stream_arn):
    try:
        lambda_client.create_event_source_mapping(
            EventSourceArn=stream_arn,
            FunctionName=lambda_name,
            StartingPosition='LATEST',
            BatchSize=1,
            Enabled=True
        )
        print(f"[Lambda] Stream conectado a {lambda_name}")
    except lambda_client.exceptions.ResourceConflictException:
        print(f"[Lambda] Stream ya conectado a {lambda_name}")

def deploy_static_site(web_bucket, api_url, index_path="web/index.html"):
    """
    Lee el index.html local, reemplaza la URL de la API y lo sube.
    """
    if not os.path.exists(index_path):
        print(f"[WARN] No se encontró {index_path}, saltando subida web.")
        return

    disable_bucket_bpa(web_bucket)
    apply_web_public_policy(web_bucket)

    # Inyección dinámica de la URL de la API
    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Reemplazar el placeholder que viene en el PDF o ponerlo fijo
    # Asegúrate que en tu HTML pongas: const API = "REPLACE_ME_API_URL";
    content_mod = content.replace("REPLACE_ME_WITH_YOUR_INVOKE_URL", api_url)

    s3.put_object(
        Bucket=web_bucket,
        Key="index.html",
        Body=content_mod.encode("utf-8"),
        ContentType="text/html; charset=utf-8"
    )
    
    s3.put_bucket_website(Bucket=web_bucket, WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}})
    
    website_host = "s3-website-us-east-1.amazonaws.com" if AWS_DEFAULT_REGION == "us-east-1" else f"s3-website-{AWS_DEFAULT_REGION}.amazonaws.com"
    return f"http://{web_bucket}.{website_host}/"

def upload_initial_data(bucket_name, local_folder="data"):
    """
    Lee todos los archivos .csv de la carpeta local 'data'
    y los sube al bucket de S3 para disparar la ingesta.
    """
    import os
    
    # Verificar si la carpeta existe
    if not os.path.exists(local_folder):
        print(f"[WARN] No se encontró la carpeta local './{local_folder}'. Salta la subida de datos.")
        print(f"       -> Crea la carpeta '{local_folder}' y pon tus .csv dentro.")
        return

    print(f"\n[Data] Buscando archivos CSV en './{local_folder}' para subir a {bucket_name}...")
    
    files_found = False
    
    # Recorrer archivos en el directorio
    for filename in os.listdir(local_folder):
        if filename.lower().endswith(".csv"):
            files_found = True
            local_path = os.path.join(local_folder, filename)
            
            try:
                print(f"   -> Subiendo: {filename} ...")
                # upload_file maneja automáticamente la lectura del archivo
                s3.upload_file(local_path, bucket_name, filename)
            except Exception as e:
                print(f"   [ERROR] Falló al subir {filename}: {e}")
    
    if not files_found:
        print(f"   [INFO] La carpeta '{local_folder}' existe pero no contiene archivos .csv.")
    else:
        print("[Data] Subida completada. Las Lambdas deberían activarse en breve.")

# ---------- Main ----------
if __name__ == "__main__":
    
    # 1. GESTIÓN DE ESTADO (SHELVE)
    # Leemos la configuración previa para mantener los mismos recursos
    with shelve.open(DB_PATH, writeback=True) as db:
        if "suffix" in db:
            suffix = db["suffix"]
            print(f"=== RESUMIENDO DESPLIEGUE (Sufijo: {suffix}) ===")
        else:
            suffix = unique_suffix()
            db["suffix"] = suffix
            print(f"=== INICIANDO NUEVO DESPLIEGUE (Sufijo: {suffix}) ===")

        # Definimos nombres basados en el sufijo persistente
        uploads_bucket = f"inventory-uploads-{suffix}"
        web_bucket     = f"inventory-web-{suffix}"
        sns_topic_name = f"NoStock-{suffix}"

        # Guardamos nombres en shelve de una vez
        db["uploads-bucket"] = uploads_bucket
        db["web-bucket"] = web_bucket

        # 2. Infraestructura Base
        ensure_bucket(uploads_bucket)
        ensure_bucket(web_bucket)
        table_arn = ensure_inventory_table(TABLE_NAME)
        role = labrole_arn()

        # 3. Lambda A (Loader)
        print("\n--- Desplegando Lambda A (Loader) ---")
        loader_zip = build_lambda_zip_bytes("lambdas/load_inventory/lambda_function.py")
        loader_arn = ensure_lambda_generic(
            LOADER_FUNC_NAME, role, loader_zip, 
            env_vars={"TABLE_NAME": TABLE_NAME}
        )

        # 4. Lambda B (API)
        print("\n--- Desplegando Lambda B (API) ---")
        api_zip = build_lambda_zip_bytes("lambdas/get_inventory_api/lambda_function.py")
        api_lambda_arn = ensure_lambda_generic(
            API_FUNC_NAME, role, api_zip,
            env_vars={"TABLE_NAME": TABLE_NAME}
        )
        api_url = ensure_http_api("InventoryAPI", api_lambda_arn)

        # 5. Lambda C (Notificaciones)
        print("\n--- Desplegando Lambda C (Notify) ---")
        sns_topic_arn = ensure_sns_topic(sns_topic_name)
        # Email harcodeado para evitar inputs interactivos en re-runs
        subscribe_email(sns_topic_arn, EMAIL_NOTIFY)
        
        notify_zip = build_lambda_zip_bytes("lambdas/notify_low_stock/lambda_function.py")
        notify_arn = ensure_lambda_generic(
            NOTIFY_FUNC_NAME, role, notify_zip, 
            env_vars={"TOPIC_ARN": sns_topic_arn}
        )

        time.sleep(5) # Esperamos unos segundos para asegurar que el Trigger S3 esté listo
        configure_s3_lambda_trigger(uploads_bucket, loader_arn, LOADER_FUNC_NAME)
        
        # Conectar Stream DynamoDB
        ddb_desc = dynamodb.describe_table(TableName=TABLE_NAME)
        if 'LatestStreamArn' in ddb_desc['Table']:
            create_dynamodb_trigger(NOTIFY_FUNC_NAME, ddb_desc['Table']['LatestStreamArn'])
        
        # 6. Frontend
        print("\n--- Desplegando Frontend ---")
        web_url = deploy_static_site(web_bucket, api_url, "web/index.html")

        # 7. Subida de datos desde carpeta local
        print("\n--- Ingestando Datos desde carpeta 'data' ---")
        # Esperamos unos segundos para asegurar que el Trigger S3 esté listo
        time.sleep(5) 
        
        # Llama a la nueva función
        upload_initial_data(uploads_bucket, "data")

        # 7. Guardar resultados finales
        db["api-url"] = api_url
        db["web-url"] = web_url
        db["table-arn"] = table_arn
        db["sns-arn"] = sns_topic_arn

        print("\n=== DESPLIEGUE FINALIZADO ===")
        print(f"Bucket CSV      : s3://{uploads_bucket}")
        print(f"API Endpoint    : {api_url}")
        print(f"Web Dashboard   : {web_url}")