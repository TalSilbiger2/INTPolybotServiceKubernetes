import telebot
from loguru import logger
import os
from telebot.types import InputFile
import requests
import boto3
from botocore.exceptions import NoCredentialsError
import json
import time

class Bot:

    def __init__(self, token, telegram_chat_url):
        # create a new instance of the TeleBot class.
        # all communication with Telegram servers are done using self.telegram_bot_client
        self.telegram_bot_client = telebot.TeleBot(token)

        # remove any existing webhooks configured in Telegram servers
        self.telegram_bot_client.remove_webhook()
        time.sleep(0.5)

        # set the webhook URL
        self.telegram_bot_client.set_webhook(url=f'{telegram_chat_url}/{token}/', timeout=60)

        logger.info(f'Telegram Bot information\n\n{self.telegram_bot_client.get_me()}')

    def send_text(self, chat_id, text):
        self.telegram_bot_client.send_message(chat_id, text)

    def send_text_with_quote(self, chat_id, text, quoted_msg_id):
        self.telegram_bot_client.send_message(chat_id, text, reply_to_message_id=quoted_msg_id)

    def is_current_msg_photo(self, msg):
        return 'photo' in msg

    def download_user_photo(self, msg):
        """ Downloads the photos that sent to the Bot to `photos` directory (should be existed) """

        if not self.is_current_msg_photo(msg):
            raise RuntimeError(f'Message content of type \'photo\' expected')

        file_info = self.telegram_bot_client.get_file(msg['photo'][-1]['file_id'])
        data = self.telegram_bot_client.download_file(file_info.file_path)
        folder_name = file_info.file_path.split('/')[0]

        if not os.path.exists(folder_name):
            os.makedirs(folder_name)


        with open(file_info.file_path, 'wb') as photo:
            photo.write(data)

        return file_info.file_path

    def send_photo(self, chat_id, img_path):
        if not os.path.exists(img_path):
            raise RuntimeError("Image path doesn't exist")

        self.telegram_bot_client.send_photo(
            chat_id,
            InputFile(img_path)
        )

    def handle_message(self, msg):
        """Bot Main message handler"""

        logger.info(f'Incoming message: {msg}')
        self.send_text(msg['chat']['id'], f'Your original message: {msg["text"]}')

class ObjectDetectionBot(Bot):

    def __init__(self, token, telegram_chat_url, s3_bucket_name, sqs_queue_url, aws_region="eu-north-1"):
        super().__init__(token, telegram_chat_url)
        # Initialize AWS clients
        self.s3_bucket_name = s3_bucket_name
        self.sqs_queue_url = sqs_queue_url
        self.s3_client = boto3.client('s3', region_name=aws_region)
        self.sqs_client = boto3.client('sqs', region_name=aws_region)

    def upload_photo_to_s3(self, file_path):
        """ Uploads the image to AWS S3 bucket. """

        try:
            # Upload the file to S3
            file_name = os.path.basename(file_path)
            self.s3_client.upload_file(file_path, self.s3_bucket_name, file_name)
            logger.info(f"File {file_name} uploaded to S3 bucket {self.s3_bucket_name}.")

            # Return the S3 URL of the uploaded image
            return f"https://{self.s3_bucket_name}.s3.amazonaws.com/{file_name}"
        except FileNotFoundError:
            logger.error(f"File {file_path} not found.")
            raise RuntimeError(f"File {file_path} not found.")
        except NoCredentialsError:
            logger.error("AWS credentials not found.")
            raise RuntimeError("AWS credentials not found.")
        except Exception as e:
            logger.error(f"An error occurred while uploading to S3: {str(e)}")
            raise RuntimeError(f"An error occurred while uploading to S3: {str(e)}")

    def send_job_to_sqs(self, s3_url, chat_id):
        """ Sends a job to the SQS queue containing the S3 URL of the uploaded photo for processing."""

        try:
            # Prepare the job message
            message = {
                's3_url': s3_url,
                'chat_id': chat_id,
                'timestamp': int(time.time())
            }

            # Send the job to the SQS queue
            response = self.sqs_client.send_message(
                QueueUrl=self.sqs_queue_url,
                MessageBody=json.dumps(message)
            )

            logger.info(f"Job sent to SQS queue {self.sqs_queue_url}. Message ID: {response['MessageId']}")
        except Exception as e:
            logger.error(f"An error occurred while sending the job to SQS: {str(e)}")
            raise RuntimeError(f"An error occurred while sending the job to SQS: {str(e)}")

    def send_processing_message(self, chat_id):
        """ Sends a message to the Telegram user - image is being processed. """

        self.send_text(chat_id, "Your image is being processed. Please wait...")

    def handle_message(self, msg):
        logger.info(f'Incoming message: {msg}')

        if self.is_current_msg_photo(msg):
            photo_path = self.download_user_photo(msg)

            # Upload the photo to S3
            s3_url = self.upload_photo_to_s3(photo_path)

            # Send a job to the SQS queue for processing
            chat_id = msg['chat']['id']
            if not chat_id:
                logger.error("Chat ID not found in the message")
                return
            self.send_job_to_sqs(s3_url, chat_id)

            # Inform the user that the image is being processed
            self.send_processing_message(msg['chat']['id'])
        else:
            self.send_text(msg['chat']['id'], "Please send a photo to be processed.")