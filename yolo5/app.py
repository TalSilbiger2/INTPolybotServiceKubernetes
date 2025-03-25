import time
import json
import yaml
import os
import boto3
import requests
from pathlib import Path
from pymongo import MongoClient
from detect import run
from loguru import logger

# Environment variables
images_bucket = os.environ['BUCKET_NAME']
queue_name = os.environ['SQS_QUEUE_URL']
mongodb_uri = os.environ['MONGODB_URI']
polybot_url = os.environ['POLYBOT_URL']
region_name = os.environ['AWS_REGION']

# AWS Clients
sqs_client = boto3.client('sqs', region_name=region_name)
s3_client = boto3.client('s3', region_name=region_name)

# Load class names
with open("data/coco128.yaml", "r") as stream:
    names = yaml.safe_load(stream)['names']

# MongoDB connection
mongo_client = MongoClient(mongodb_uri)
db = mongo_client['yolo5_db']
predictions_collection = db['predictions']

# Method to consume messages from SQS
def consume():
    while True:
        try:
            response = sqs_client.receive_message(
                QueueUrl=queue_name,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=5
            )

            # Log the response for debugging
            logger.info(f"SQS Response: {response}")

            if 'Messages' in response:
                message = response['Messages'][0]['Body']
                receipt_handle = response['Messages'][0]['ReceiptHandle']

                # Use the ReceiptHandle as a prediction UUID
                prediction_id = response['Messages'][0]['MessageId']

                logger.info(f'prediction: {prediction_id}. start processing')

                # Parse the job data from the message
                job_data = json.loads(message)
                img_name = job_data['s3_url'].split('/')[-1]
                chat_id = job_data['chat_id']
                original_img_path = img_name

                # Download image from S3
                s3_client.download_file(images_bucket, img_name, original_img_path)
                logger.info(f'prediction: {prediction_id}/{original_img_path}. Download img completed')

                # Perform inference with YOLOv5
                run(
                    weights='yolov5s.pt',
                    data='data/coco128.yaml',
                    source=original_img_path,
                    project='static/data',
                    name=prediction_id,
                    save_txt=True
                )

                logger.info(f'prediction: {prediction_id}/{original_img_path}. done')

                # Path for the predicted image with labels
                predicted_img_path = Path(f'static/data/{prediction_id}/{original_img_path}')
                predicted_s3_key = f"predictions/{prediction_id}_{img_name}"

                # Upload the predicted image back to S3
                s3_client.upload_file(str(predicted_img_path), images_bucket, predicted_s3_key)
                predicted_s3_url = f"https://{images_bucket}.s3.{region_name}.amazonaws.com/{predicted_s3_key}"

                # Parse prediction labels and create a summary
                pred_summary_path = Path(f'static/data/{prediction_id}/labels/{original_img_path.split(".")[0]}.txt')
                if pred_summary_path.exists():
                    with open(pred_summary_path) as f:
                        labels = f.read().splitlines()
                        labels = [line.split(' ') for line in labels]
                        labels = [{
                            'class': names[int(l[0])],
                            'cx': float(l[1]),
                            'cy': float(l[2]),
                            'width': float(l[3]),
                            'height': float(l[4]),
                        } for l in labels]

                    logger.info(f'prediction: {prediction_id}/{original_img_path}. prediction summary:\n\n{labels}')

                    prediction_summary = {
                        'prediction_id': prediction_id,
                        'original_img_path': original_img_path,
                        'predicted_img_path': str(predicted_img_path),
                        'labels': labels,
                        'time': time.time(),
                        'chat_id': chat_id
                    }

                    logger.info(f"Try to save to Mongo")

                    # Save the result to MongoDB
                    predictions_collection.insert_one(prediction_summary)
                    response = requests.post(
                        f"{polybot_url}/results",
                        json={"predictionId": prediction_id},
                        verify=False
                    )

                    logger.info(f"This is the results - TAL LOG :) - {polybot_url}/results")

                    logger.info(f"Polybot /results response: {response.status_code} - {response.text}")

                    logger.info(f"Response: {response.status_code} - {response.text}")
                    logger.info(f"saved id {prediction_id} to mongo")

                # Delete the message from the queue after processing
                sqs_client.delete_message(QueueUrl=queue_name, ReceiptHandle=receipt_handle)
                logger.info(f"Delete {prediction_id} from sqs queue")

            else:
                logger.info("No messages in the queue. Waiting...")

        except Exception as e:
            logger.error(f"Error processing message: {str(e)}")
            time.sleep(5)

if __name__ == "__main__":
    logger.info("Starting SQS Consumer Service...")
    consume()