# Práctica 2025-26: Cloud Computing (AWS)
## Dashboard Serverless para Gestión de Inventario

Este proyecto implementa una arquitectura **Serverless completa en AWS** utilizando **Infrastructure as Code (IaC)** con Python y `boto3`. El sistema permite la ingesta de archivos CSV, su procesamiento automático, almacenamiento, visualización web y notificaciones automáticas por stock bajo.

---

### Arquitectura del Sistema

El despliegue crea y orquesta los siguientes recursos de forma automática sin intervención manual en la consola:

1.  **Capa de Ingesta (S3 + Lambda A):**
    * **S3 Bucket:** `inventory-uploads-<sufijo>` para la subida de archivos CSV.
    * **Lambda A (`LoadInventoryFunction`):** Se activa mediante un evento `s3:ObjectCreated`. Lee el archivo CSV, parsea el contenido y escribe los registros en DynamoDB.

2.  **Capa de Datos (DynamoDB):**
    * **Tabla:** `Inventory`.
    * **Esquema:** `store` (Partition Key) e `item` (Sort Key).
    * **Streams:** Habilitados con `NEW_AND_OLD_IMAGES` para permitir la lógica de notificación inteligente.

3.  **Capa de API y Visualización (Lambda B + API Gateway + S3 Web):**
    * **Lambda B (`GetInventoryApiFunction`):** Recupera los datos de DynamoDB. Soporta `Scan` (todas las tiendas) y `Query` (tienda específica).
    * **API Gateway (HTTP API):** Expone endpoints REST seguros (`GET /items` y `GET /items/{store}`) integrados con la Lambda B.
    * **S3 Web Bucket:** `inventory-web-<sufijo>` configurado como *Static Website Hosting* para servir el Dashboard HTML/JS.

4.  **Capa de Notificaciones (Lambda C + SNS):**
    * **Lambda C (`NotifyLowStockFunction`):** Se activa mediante DynamoDB Streams. Compara el stock anterior con el nuevo para detectar bajadas críticas (< 5 unidades).
    * **Amazon SNS:** Envía un correo electrónico de alerta al administrador suscrito.

---

###  Estructura del Proyecto

```text
.
├── deploy_script.py      # Script principal: Crea infraestructura, sube web y genera datos
├── teardown.py    # Script de limpieza: Elimina todos los recursos AWS creados
├── aws_resources.db      # Base de datos local (shelve) para rastrear recursos desplegados
├── .env.sample           # Variables de entorno (Región AWS)
├── lambdas/              # Código fuente de las funciones
│   ├── load_inventory/   # Lógica de carga CSV -> DynamoDB
│   ├── get_inventory_api/# Lógica de lectura DynamoDB -> JSON
│   └── notify_low_stock/ # Lógica de alertas DynamoDB Stream -> SNS
└── web/                  # Frontend estático
    └── index.html        # Dashboard con JS nativo

```

### Requisitos Previos
1. Python 3.x instalado.
2. AWS CLI configurado con credenciales válidas (aws configure).
3. Instalación de dependencias:

```bash
pip install boto3 python-dotenv
```
4. Archivo .env en la raíz:
```text
AWS_ACCESS_KEY_ID=YOUR_KEY
AWS_SECRET_ACCESS_KEY=YOUR_SECRET
AWS_SESSION_TOKEN=YOUR_SESSION_TOKEN
AWS_DEFAULT_REGION=us-east-1
EMAIL_NOTIFY=ejemplo@ejemplo.com
```

### Despliegue de los servicios en AWS
Para desplegar toda la infraestructura, configurar triggers y generar datos de prueba automáticamente:
```bash
python deploy_script.py
```
> Nota: Durante la ejecución, el script te suscribirá al topic SNS. Debes ir a tu correo electrónico y hacer clic en "Confirm subscription" para recibir las alertas.

Al finalizar, el script mostrará en la terminal la URL del Dashboard Web, la API, el bucket S3 de uploads.

### Borrado de todos los servicios desplegados
Para eliminar todos los recursos (Buckets, Lambdas, Tablas, APIs, etc.):
```bash
python teardown.py
```