import json
import os
import base64
from typing import Any, Dict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import urllib3
import boto3
from botocore.exceptions import ClientError

# Deshabilitar las advertencias de certificado SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def obtener_api_key_jenkins() -> str:
    """Recuperar la clave de la API de Jenkins desde AWS Secrets Manager"""
    try:
        sesion = boto3.session.Session()
        cliente = sesion.client(
            service_name='secretsmanager'
        )
        
        respuesta = cliente.get_secret_value(
            SecretId=os.environ['JENKINS_API_KEY_SECRET']
        )
        
        if 'SecretString' in respuesta:
            return respuesta['SecretString']
        else:
            raise ValueError("El secreto no es una cadena")

    except ClientError as e:
        if e.response['Error']['Code'] == 'DecryptionFailureException':
            raise Exception("Error: No se puede descifrar el secreto")
        elif e.response['Error']['Code'] == 'InternalServiceErrorException':
            raise Exception("Error: Error interno del servicio")
        elif e.response['Error']['Code'] == 'InvalidParameterException':
            raise Exception("Error: Parámetro no válido")
        elif e.response['Error']['Code'] == 'InvalidRequestException':
            raise Exception("Error: Solicitud no válida")
        elif e.response['Error']['Code'] == 'ResourceNotFoundException':
            raise Exception("Error: Secreto no encontrado")
        else:
            raise Exception(f"Error: {str(e)}")
    except Exception as e:
        raise Exception(f"Fallo al recuperar la cadena de la API key de Jenkins de AWS Secrets Manager: {str(e)}")
    
def obtener_fichero_config(bucket, clave) -> str:
    """Recuperar la configuración del proyecto Jenkins desde S3"""
    try:
        cliente = boto3.client('s3')
        respuesta = cliente.get_object(
            Bucket=bucket,
            Key=clave
        )
        return respuesta['Body'].read().decode('utf-8').strip()
    except Exception as e:
        raise Exception(f"Fallo al obtener la configuración del proyecto Jenkins desde Amazon S3: {str(e)}")

def enviar_respuesta_cloudformation(evento: Dict[str, Any], contexto: Any, 
                                estado: str, razon: str = None, 
                                dato: Dict[str, Any] = None, 
                                id_recurso_fisico: str = None) -> None:
    """Send response to CloudFormation"""
    cuerpo_respuesta = {
        'Status': estado,
        'Reason': razon or 'Ver los detalles en CloudWatch Log Stream'+ contexto.log_stream_name,
        'PhysicalResourceId': id_recurso_fisico or contexto.log_stream_name,
        'StackId': evento['StackId'],
        'RequestId': evento['RequestId'],
        'LogicalResourceId': evento['LogicalResourceId'],
        'NoEcho': False
    }
    
    if dato:
        cuerpo_respuesta['Data'] = dato
    
    respuesta_json = json.dumps(cuerpo_respuesta)
    print(f"Cuerpo de la respuesta: {respuesta_json}")
    
    try:
        cabeceras = {
            'Content-Type': 'application/json',
        }
        
        respuesta = requests.put(
            evento['ResponseURL'],
            data=respuesta_json,
            headers=cabeceras,
            verify=False
        )
        respuesta.raise_for_status()
        print("La respuesta de CloudFormation fue enviada con éxito")
        
    except Exception as e:
        print(f"Fallo al enviar la respuesta a AWS CloudFormation: {str(e)}")

def crear_sesion() -> requests.Session:
    sesion = requests.Session()
    est_reintento = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504]
    )
    adaptador_http = HTTPAdapter(max_retries=est_reintento)
    sesion.mount("http://", adaptador_http)
    sesion.mount("https://", adaptador_http)
    sesion.verify = False
    return sesion

def manejar_respuesta(respuesta: requests.Response, accion: str) -> None:
    """Validar la respuesta de la API de Jenkins"""
    try:
        respuesta.raise_for_status()
    except requests.exceptions.RequestException as e:
        if accion == "delete" and respuesta.status_code == 404:
            return
        raise Exception(f"Jenkins API {accion} failed: {str(e)}") from e

def manejador(event: Dict[str, Any], context: Any) -> None:
    print(f"Evento recibido: {json.dumps(event)}")
    
    try:
        # Se extraen las propiedades del evento que origina AWS CloudFormation
        propiedades = event['ResourceProperties']
        url_jenkins = propiedades['JenkinsUrl']
        ususario_jenkins = propiedades['JenkinsUsername']
        api_token_jenkins = obtener_api_key_jenkins()
        nombre_proyecto = propiedades['ProjectName']
        
        # Crear la cadena de autenticación a Jenkins
        autenticacion = base64.b64encode(
            f"{ususario_jenkins}:{api_token_jenkins}".encode()
        ).decode()
        
        cabeceras = {
            'Authorization': f'Basic {autenticacion}',
            'Content-Type': 'text/xml'
        }
        
        # Crear la sesión de requests
        sesion = crear_sesion()
        
        if event['RequestType'] in ['Create', 'Update']:
            config_xml = obtener_fichero_config(propiedades['JenkinsS3Bucket'], propiedades['JenkinsS3Key'])
            
            if event['RequestType'] == 'Create':

                # Crear el proyecto de Jenkins  
                respuesta = sesion.post(
                    f"{url_jenkins}/createItem",
                    params={'name': nombre_proyecto},
                    headers=cabeceras,
                    data=config_xml,
                    verify=False
                )
            else:
                # Actualizar el proyecto de Jenkins
                respuesta = sesion.post(
                    f"{url_jenkins}/job/{nombre_proyecto}/config.xml",
                    headers=cabeceras,
                    data=config_xml,
                    verify=False
                )

            manejar_respuesta(respuesta, "create/update")
            
        elif event['RequestType'] == 'Delete':
            # Eliminar el proyecto de Jenkins
            respuesta = sesion.post(
                f"{url_jenkins}/job/{nombre_proyecto}/doDelete",
                headers=cabeceras,
                verify=False
            )
            manejar_respuesta(respuesta, "delete")
        
        enviar_respuesta_cloudformation(
            event,
            context,
            'SUCCESS',
            id_recurso_fisico=nombre_proyecto
        )
        
    except Exception as e:
        print(f"Error: {str(e)}")
        enviar_respuesta_cloudformation(
            event,
            context,
            'FAILED',
            razon=str(e),
            id_recurso_fisico=nombre_proyecto if 'nombre_proyecto' in locals() else None
        )