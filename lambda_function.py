
import boto3
import os
import json
import base64
import urllib.parse
import uuid
from io import BytesIO
from email import policy
from email.parser import BytesParser
from PIL import Image, ImageOps

s3 = boto3.client("s3")

OUTPUT_BUCKET = "serverless-image-output-bhimashankar"

ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

SIZES = {
    "thumbnail": (100, 100),
    "medium": (500, 500),
    "large": (1200, 1200),
}


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body),
    }


def resize_and_upload(image, original_name, requested_sizes=None):
    base_name = (
        os.path.splitext(os.path.basename(original_name))[0] or "image"
    )

    sizes_to_make = requested_sizes or list(SIZES.keys())
    results = {}

    if image.mode != "RGB":
        image = image.convert("RGB")

    for size_name in sizes_to_make:
        if size_name not in SIZES:
            continue

        dimensions = SIZES[size_name]

        resized = ImageOps.fit(
            image,
            dimensions,
            method=Image.Resampling.LANCZOS,
        )

        output_path = f"/tmp/{uuid.uuid4().hex}_{size_name}.jpg"
        output_key = f"resized/{size_name}/{base_name}.jpg"

        try:
            resized.save(output_path, "JPEG", quality=85)

            s3.upload_file(
                output_path,
                OUTPUT_BUCKET,
                output_key,
                ExtraArgs={"ContentType": "image/jpeg"},
            )

            results[size_name] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": OUTPUT_BUCKET,
                    "Key": output_key,
                },
                ExpiresIn=3600,
            )

        finally:
            if os.path.exists(output_path):
                os.remove(output_path)

    return results


def parse_multipart(raw_body, content_type):
    header = (
        "Content-Type: " + content_type + "\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8")

    message = BytesParser(policy=policy.default).parsebytes(
        header + raw_body
    )

    file_bytes = None
    filename = "image.jpg"
    requested_sizes = None

    if not message.is_multipart():
        print("DEBUG: Request is not multipart.")
        print("DEBUG: Content-Type:", content_type)
        print("DEBUG: Body length:", len(raw_body))
        raise ValueError(
            "Could not read the image upload. Check the website upload form."
        )

    for part in message.iter_parts():
        field_name = part.get_param(
            "name",
            header="content-disposition",
        )
        part_filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""

        # Log field details only. Never log the actual image contents.
        print(
            "DEBUG multipart part:",
            {
                "field": field_name,
                "filename": part_filename,
                "content_type": part.get_content_type(),
                "size": len(payload),
            },
        )

        if part_filename or field_name == "file":
            if payload:
                file_bytes = payload
                filename = part_filename or filename

        elif field_name == "sizes":
            try:
                requested_sizes = json.loads(
                    payload.decode("utf-8")
                )
            except (ValueError, UnicodeDecodeError):
                requested_sizes = None

    if not file_bytes:
        raise ValueError(
            "No image file was received. Check the DEBUG multipart logs."
        )

    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise ValueError(
            "Only JPG, JPEG, PNG and WEBP images are supported."
        )

    if not isinstance(requested_sizes, list):
        requested_sizes = list(SIZES.keys())

    requested_sizes = [
        name for name in requested_sizes if name in SIZES
    ]

    if not requested_sizes:
        raise ValueError("Select at least one image size.")

    return file_bytes, filename, requested_sizes


def lambda_handler(event, context):
    try:
        print("Incoming event keys:", list(event.keys()))

        # Handle browser preflight requests.
        if event.get("requestContext", {}).get("http", {}).get(
            "method"
        ) == "OPTIONS":
            return response(200, {"message": "CORS preflight OK"})

        # Handle S3-triggered image resizing.
        if "Records" in event:
            record = event["Records"][0]

            input_bucket = record["s3"]["bucket"]["name"]
            input_key = urllib.parse.unquote_plus(
                record["s3"]["object"]["key"]
            )

            if not input_key.lower().endswith(ALLOWED_EXTENSIONS):
                print("Skipping unsupported file:", input_key)
                return {
                    "statusCode": 400,
                    "body": "Unsupported image type",
                }

            input_path = "/tmp/input-image"

            try:
                s3.download_file(
                    input_bucket,
                    input_key,
                    input_path,
                )

                with Image.open(input_path) as source:
                    image = source.copy()

                results = resize_and_upload(image, input_key)

                print(
                    "S3 resize completed:",
                    list(results.keys()),
                )

                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "message": "Images resized successfully"
                    }),
                }

            finally:
                if os.path.exists(input_path):
                    os.remove(input_path)

        # Handle API Gateway / website requests.
        headers = event.get("headers") or {}

        normalized_headers = {
            str(key).lower(): value
            for key, value in headers.items()
        }

        content_type = normalized_headers.get(
            "content-type", ""
        ) or ""

        body = event.get("body")

        print("DEBUG content-type:", content_type)
        print(
            "DEBUG isBase64Encoded:",
            event.get("isBase64Encoded"),
        )
        print(
            "DEBUG body type:",
            type(body).__name__,
        )
        print(
            "DEBUG body length:",
            len(body) if isinstance(body, str) else 0,
        )

        if body is None:
            return response(
                400,
                {"error": "Request body is missing."},
            )

        if event.get("isBase64Encoded"):
            try:
                raw_body = base64.b64decode(body, validate=True)
            except Exception:
                return response(
                    400,
                    {"error": "Could not decode the request body."},
                )
        else:
            raw_body = body.encode("utf-8")

        if "multipart/form-data" in content_type.lower():
            image_bytes, filename, requested_sizes = parse_multipart(
                raw_body,
                content_type,
            )

        else:
            print(
                "DEBUG: Expected multipart/form-data but received:",
                content_type,
            )

            try:
                data = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return response(
                    400,
                    {
                        "error": (
                            "Unsupported request format. "
                            "Expected multipart/form-data."
                        )
                    },
                )

            image_data = (
                data.get("image")
                or data.get("file")
                or data.get("imageData")
            )

            filename = (
                data.get("filename")
                or data.get("fileName")
                or "image.jpg"
            )

            if not image_data:
                return response(
                    400,
                    {"error": "Image data is missing."},
                )

            if image_data.startswith("data:") and "," in image_data:
                image_data = image_data.split(",", 1)[1]

            try:
                image_bytes = base64.b64decode(image_data)
            except Exception:
                return response(
                    400,
                    {"error": "Invalid base64 image data."},
                )

            requested_sizes = data.get(
                "sizes",
                list(SIZES.keys()),
            )

            if not isinstance(requested_sizes, list):
                requested_sizes = list(SIZES.keys())

            requested_sizes = [
                name for name in requested_sizes if name in SIZES
            ]

            if not requested_sizes:
                requested_sizes = list(SIZES.keys())

        if not image_bytes:
            return response(
                400,
                {"error": "The uploaded image is empty."},
            )

        try:
            with Image.open(BytesIO(image_bytes)) as source:
                source.verify()

            with Image.open(BytesIO(image_bytes)) as source:
                image = source.copy()

        except Exception as exc:
            print("DEBUG image validation error:", repr(exc))
            return response(
                400,
                {
                    "error": (
                        "The uploaded file is not a valid supported image."
                    )
                },
            )

        images = resize_and_upload(
            image,
            filename,
            requested_sizes,
        )

        if not images:
            return response(
                400,
                {
                    "error": (
                        "No image sizes were created. "
                        "Select at least one size."
                    )
                },
            )

        return response(
            200,
            {
                "message": "Images resized successfully.",
                "images": images,
            },
        )

    except Exception as exc:
        print("ERROR:", repr(exc))
        return response(500, {"error": str(exc)})
