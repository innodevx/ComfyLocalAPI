import json
import os
import random
import uuid
import websocket
import requests
from pathlib import Path
from flask import Flask, jsonify, abort, request, Response
from flask_cors import CORS
from dotenv import load_dotenv
import boto3
from botocore.client import Config as BotoConfig

load_dotenv()

app = Flask(__name__)
CORS(app)


class Config:
    COMFYUI_SERVER = os.getenv("COMFYUI_SERVER", "192.168.0.50:8188")
    CHARACTERS_API_URL = os.getenv("CHARACTERS_API_URL")
    WORKFLOW_FILE = Path(os.getenv("WORKFLOW_FILE", "workflow.json"))
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", 3000))
    CLIENT_ID = str(uuid.uuid4())
    NODE_IDS = {
        "text": "6",
        "latent_image": "5",
        "seed": "3"
    }

    MINIO_URL = os.getenv("MINIO_URL", "http://127.0.0.1:9000")
    MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "admin")
    MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "admin123")
    MINIO_BUCKET = os.getenv("MINIO_BUCKET", "meu-bucket")

s3_client = boto3.client(
    's3',
    endpoint_url=Config.MINIO_URL,
    aws_access_key_id=Config.MINIO_ACCESS_KEY,
    aws_secret_access_key=Config.MINIO_SECRET_KEY,
    config=BotoConfig(signature_version='s3v4')
)


def init_minio_bucket():
    try:
        s3_client.head_bucket(Bucket=Config.MINIO_BUCKET)
    except Exception as e:
        app.logger.info(f"Bucket não encontrado, criando o bucket {Config.MINIO_BUCKET}: {e}")
        s3_client.create_bucket(Bucket=Config.MINIO_BUCKET)


init_minio_bucket()


def load_workflow(prompt: str = None, width: int = None, height: int = None) -> dict:
    with open(Config.WORKFLOW_FILE, "r", encoding="utf-8") as f:
        workflow_template = f.read()
    workflow_str = workflow_template.replace("{{GOOD_PROMPT}}", os.getenv("GOOD_PROMPT", "")) \
        .replace("{{BAD_PROMPT}}", os.getenv("BAD_PROMPT", ""))
    workflow = json.loads(workflow_str)
    nodes = Config.NODE_IDS
    if prompt:
        workflow[nodes["text"]]["inputs"]["text"] = prompt
    if width:
        workflow[nodes["latent_image"]]["inputs"]["width"] = width
    if height:
        workflow[nodes["latent_image"]]["inputs"]["height"] = height
    workflow[nodes["seed"]]["inputs"]["seed"] = random.randint(1, 2 ** 64 - 1)
    return workflow


def comfyui_api_request(
        endpoint: str,
        method: str = "GET",
        params: dict = None,
        data: dict = None,
        return_json: bool = True
) -> requests.Response | dict:
    url = f"http://{Config.COMFYUI_SERVER}/{endpoint}"
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.request(
            method=method,
            url=url,
            params=params,
            json=data,
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        return response.json() if return_json else response
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Erro na requisição para {url}: {str(e)}")
        raise


def get_history(prompt_id: str) -> dict:
    url = f"http://{Config.COMFYUI_SERVER}/history/{prompt_id}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Erro ao obter histórico: {str(e)}")
        raise


def get_image(filename: str, subfolder: str, folder_type: str) -> bytes:
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url = f"http://{Config.COMFYUI_SERVER}/view"
    try:
        response = requests.get(url, params=data, timeout=30)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Erro ao obter imagem: {str(e)}")
        raise


@app.route('/generate', methods=['POST'])
def generate_image():
    data = request.get_json()
    prompt = data.get('prompt')
    width = data.get('width', 512)
    height = data.get('height', 768)

    try:
        workflow = load_workflow(prompt, width, height)

        response = comfyui_api_request(
            endpoint="prompt",
            method="POST",
            data={"prompt": workflow, "client_id": Config.CLIENT_ID}
        )
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise Exception("Prompt ID não retornado na resposta.")

        ws = websocket.create_connection(f"ws://{Config.COMFYUI_SERVER}/ws?clientId={Config.CLIENT_ID}")
        try:
            while True:
                out = ws.recv()
                message = json.loads(out)
                if message.get("type") == "executing":
                    data_msg = message.get("data", {})
                    if data_msg.get("node") is None and data_msg.get("prompt_id") == prompt_id:
                        break
        finally:
            ws.close()

        history = get_history(prompt_id)
        output_images = {}
        generated_files = []
        prompt_history = history.get(prompt_id, {})
        outputs = prompt_history.get("outputs", {})

        for node_id, node_output in outputs.items():
            if "images" in node_output:
                for image in node_output["images"]:
                    image_filename = f"{random.randint(1, 2 ** 64 - 1)}-{image['filename']}"
                    image_data = get_image(image['filename'], image['subfolder'], image['type'])

                    s3_client.put_object(
                        Bucket=Config.MINIO_BUCKET,
                        Key=image_filename,
                        Body=image_data,
                        ContentType='image/jpeg'
                    )

                    generated_files.append(image_filename)
                    output_images.setdefault(node_id, []).append(image_filename)

        return jsonify({
            "filenames": generated_files,
            "images": output_images
        })

    except Exception as e:
        app.logger.error(f"Erro na geração: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/images/<filename>', methods=['GET'])
def serve_image(filename):
    try:
        response = s3_client.get_object(Bucket=Config.MINIO_BUCKET, Key=filename)
        image_data = response['Body'].read()
        return Response(image_data, mimetype='image/jpeg')
    except Exception as e:
        app.logger.error(f"Erro ao servir a imagem {filename}: {str(e)}")
        abort(404)


@app.route('/', methods=['GET'])
def running():
    return "API is running!", 200

if __name__ == '__main__':
    app.run(host=Config.HOST, port=Config.PORT, threaded=True)
