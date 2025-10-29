"""Utility functions for all connectors"""

import base64
import contextvars
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Generator, Mapping
from datetime import datetime, timezone, timedelta
from functools import lru_cache, wraps
from io import BytesIO
from itertools import islice
from numbers import Integral
from pathlib import Path
from typing import Any, Optional, IO, TypeVar, cast, Iterable, Generic
from urllib.parse import quote, urlparse, urljoin, parse_qs

import boto3
import chardet
import requests
from botocore.client import Config
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.errors import HttpError
from mypy_boto3_s3 import S3Client
from retry import retry
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from common.data_source.config import (
    BlobType,
    DB_CREDENTIALS_PRIMARY_ADMIN_KEY,
    DOWNLOAD_CHUNK_SIZE,
    SIZE_THRESHOLD_BUFFER, _NOTION_CALL_TIMEOUT, _ITERATION_LIMIT, CONFLUENCE_OAUTH_TOKEN_URL,
    RATE_LIMIT_MESSAGE_LOWERCASE, _SLACK_LIMIT, CONFLUENCE_CONNECTOR_ATTACHMENT_SIZE_THRESHOLD,
    CONFLUENCE_CONNECTOR_ATTACHMENT_CHAR_COUNT_THRESHOLD, FileOrigin
)
from common.data_source.confluence_connector import OnyxConfluence
from common.data_source.exceptions import RateLimitTriedTooManyTimesError
from common.data_source.interfaces import SecondsSinceUnixEpoch, CT, LoadFunction, \
    CheckpointedConnector, CheckpointOutputWrapper, ConfluenceUser, TokenResponse, OnyxExtensionType, \
    AttachmentProcessingResult
from common.data_source.models import BasicExpertInfo, Document, ExternalAccess


def datetime_from_string(datetime_string: str) -> datetime:
    datetime_object = datetime.fromisoformat(datetime_string)

    if datetime_object.tzinfo is None:
        # If no timezone info, assume it is UTC
        datetime_object = datetime_object.replace(tzinfo=timezone.utc)
    else:
        # If not in UTC, translate it
        datetime_object = datetime_object.astimezone(timezone.utc)

    return datetime_object


def get_current_tz_offset() -> int:
    # datetime now() gets local time, datetime.now(timezone.utc) gets UTC time.
    # remove tzinfo to compare non-timezone-aware objects.
    time_diff = datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)
    return round(time_diff.total_seconds() / 3600)


def is_valid_image_type(mime_type: str) -> bool:
    """
    Check if mime_type is a valid image type.

    Args:
        mime_type: The MIME type to check

    Returns:
        True if the MIME type is a valid image type, False otherwise
    """
    return (
        bool(mime_type)
        and mime_type.startswith("image/")
        and mime_type not in EXCLUDED_IMAGE_TYPES
    )


"""If you want to allow the external service to tell you when you've hit the rate limit,
use the following instead"""

R = TypeVar("R", bound=Callable[..., requests.Response])


def _handle_http_error(e: requests.HTTPError, attempt: int) -> int:
    MIN_DELAY = 2
    MAX_DELAY = 60
    STARTING_DELAY = 5
    BACKOFF = 2

    # Check if the response or headers are None to avoid potential AttributeError
    if e.response is None or e.response.headers is None:
        logging.warning("HTTPError with `None` as response or as headers")
        raise e

    # Confluence Server returns 403 when rate limited
    if e.response.status_code == 403:
        FORBIDDEN_MAX_RETRY_ATTEMPTS = 7
        FORBIDDEN_RETRY_DELAY = 10
        if attempt < FORBIDDEN_MAX_RETRY_ATTEMPTS:
            logging.warning(
                "403 error. This sometimes happens when we hit "
                f"Confluence rate limits. Retrying in {FORBIDDEN_RETRY_DELAY} seconds..."
            )
            return FORBIDDEN_RETRY_DELAY

        raise e

    if (
        e.response.status_code != 429
        and RATE_LIMIT_MESSAGE_LOWERCASE not in e.response.text.lower()
    ):
        raise e

    retry_after = None

    retry_after_header = e.response.headers.get("Retry-After")
    if retry_after_header is not None:
        try:
            retry_after = int(retry_after_header)
            if retry_after > MAX_DELAY:
                logging.warning(
                    f"Clamping retry_after from {retry_after} to {MAX_DELAY} seconds..."
                )
                retry_after = MAX_DELAY
            if retry_after < MIN_DELAY:
                retry_after = MIN_DELAY
        except ValueError:
            pass

    if retry_after is not None:
        logging.warning(
            f"Rate limiting with retry header. Retrying after {retry_after} seconds..."
        )
        delay = retry_after
    else:
        logging.warning(
            "Rate limiting without retry header. Retrying with exponential backoff..."
        )
        delay = min(STARTING_DELAY * (BACKOFF**attempt), MAX_DELAY)

    delay_until = math.ceil(time.monotonic() + delay)
    return delay_until


def update_param_in_path(path: str, param: str, value: str) -> str:
    """Update a parameter in a path. Path should look something like:

    /api/rest/users?start=0&limit=10
    """
    parsed_url = urlparse(path)
    query_params = parse_qs(parsed_url.query)
    query_params[param] = [value]
    return (
        path.split("?")[0]
        + "?"
        + "&".join(f"{k}={quote(v[0])}" for k, v in query_params.items())
    )


def build_confluence_document_id(
    base_url: str, content_url: str, is_cloud: bool
) -> str:
    """For confluence, the document id is the page url for a page based document
        or the attachment download url for an attachment based document

    Args:
        base_url (str): The base url of the Confluence instance
        content_url (str): The url of the page or attachment download url

    Returns:
        str: The document id
    """

    # NOTE: urljoin is tricky and will drop the last segment of the base if it doesn't
    # end with "/" because it believes that makes it a file.
    final_url = base_url.rstrip("/") + "/"
    if is_cloud and not final_url.endswith("/wiki/"):
        final_url = urljoin(final_url, "wiki") + "/"
    final_url = urljoin(final_url, content_url.lstrip("/"))
    return final_url


def get_single_param_from_url(url: str, param: str) -> str | None:
    """Get a parameter from a url"""
    parsed_url = urlparse(url)
    return parse_qs(parsed_url.query).get(param, [None])[0]


def get_start_param_from_url(url: str) -> int:
    """Get the start parameter from a url"""
    start_str = get_single_param_from_url(url, "start")
    return int(start_str) if start_str else 0


def wrap_request_to_handle_ratelimiting(
    request_fn: R, default_wait_time_sec: int = 30, max_waits: int = 30
) -> R:
    def wrapped_request(*args: list, **kwargs: dict[str, Any]) -> requests.Response:
        for _ in range(max_waits):
            response = request_fn(*args, **kwargs)
            if response.status_code == 429:
                try:
                    wait_time = int(
                        response.headers.get("Retry-After", default_wait_time_sec)
                    )
                except ValueError:
                    wait_time = default_wait_time_sec

                time.sleep(wait_time)
                continue

            return response

        raise RateLimitTriedTooManyTimesError(f"Exceeded '{max_waits}' retries")

    return cast(R, wrapped_request)


_rate_limited_get = wrap_request_to_handle_ratelimiting(requests.get)
_rate_limited_post = wrap_request_to_handle_ratelimiting(requests.post)


class _RateLimitedRequest:
    get = _rate_limited_get
    post = _rate_limited_post


rl_requests = _RateLimitedRequest

# Blob Storage Utilities

def create_s3_client(bucket_type: BlobType, credentials: dict[str, Any], european_residency: bool = False) -> S3Client:
    """Create S3 client for different blob storage types"""
    if bucket_type == BlobType.R2:
        subdomain = "eu." if european_residency else ""
        endpoint_url = f"https://{credentials['account_id']}.{subdomain}r2.cloudflarestorage.com"

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=credentials["r2_access_key_id"],
            aws_secret_access_key=credentials["r2_secret_access_key"],
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )

    elif bucket_type == BlobType.S3:
        authentication_method = credentials.get("authentication_method", "access_key")
        
        if authentication_method == "access_key":
            session = boto3.Session(
                aws_access_key_id=credentials["aws_access_key_id"],
                aws_secret_access_key=credentials["aws_secret_access_key"],
            )
            return session.client("s3")
        
        elif authentication_method == "iam_role":
            role_arn = credentials["aws_role_arn"]
            
            def _refresh_credentials() -> dict[str, str]:
                sts_client = boto3.client("sts")
                assumed_role_object = sts_client.assume_role(
                    RoleArn=role_arn,
                    RoleSessionName=f"onyx_blob_storage_{int(datetime.now().timestamp())}",
                )
                creds = assumed_role_object["Credentials"]
                return {
                    "access_key": creds["AccessKeyId"],
                    "secret_key": creds["SecretAccessKey"],
                    "token": creds["SessionToken"],
                    "expiry_time": creds["Expiration"].isoformat(),
                }

            refreshable = RefreshableCredentials.create_from_metadata(
                metadata=_refresh_credentials(),
                refresh_using=_refresh_credentials,
                method="sts-assume-role",
            )
            botocore_session = get_session()
            botocore_session._credentials = refreshable
            session = boto3.Session(botocore_session=botocore_session)
            return session.client("s3")
        
        elif authentication_method == "assume_role":
            return boto3.client("s3")
        
        else:
            raise ValueError("Invalid authentication method for S3.")

    elif bucket_type == BlobType.GOOGLE_CLOUD_STORAGE:
        return boto3.client(
            "s3",
            endpoint_url="https://storage.googleapis.com",
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
            region_name="auto",
        )

    elif bucket_type == BlobType.OCI_STORAGE:
        return boto3.client(
            "s3",
            endpoint_url=f"https://{credentials['namespace']}.compat.objectstorage.{credentials['region']}.oraclecloud.com",
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
            region_name=credentials["region"],
        )

    else:
        raise ValueError(f"Unsupported bucket type: {bucket_type}")


def detect_bucket_region(s3_client: S3Client, bucket_name: str) -> str | None:
    """Detect bucket region"""
    try:
        response = s3_client.head_bucket(Bucket=bucket_name)
        bucket_region = response.get("BucketRegion") or response.get(
            "ResponseMetadata", {}
        ).get("HTTPHeaders", {}).get("x-amz-bucket-region")
        
        if bucket_region:
            logging.debug(f"Detected bucket region: {bucket_region}")
        else:
            logging.warning("Bucket region not found in head_bucket response")
        
        return bucket_region
    except Exception as e:
        logging.warning(f"Failed to detect bucket region via head_bucket: {e}")
        return None


def download_object(s3_client: S3Client, bucket_name: str, key: str, size_threshold: int | None = None) -> bytes | None:
    """Download object from blob storage"""
    response = s3_client.get_object(Bucket=bucket_name, Key=key)
    body = response["Body"]

    try:
        if size_threshold is None:
            return body.read()

        return read_stream_with_limit(body, key, size_threshold)
    finally:
        body.close()


def read_stream_with_limit(body: Any, key: str, size_threshold: int) -> bytes | None:
    """Read stream with size limit"""
    bytes_read = 0
    chunks: list[bytes] = []
    chunk_size = min(DOWNLOAD_CHUNK_SIZE, size_threshold + SIZE_THRESHOLD_BUFFER)

    for chunk in body.iter_chunks(chunk_size=chunk_size):
        if not chunk:
            continue
        chunks.append(chunk)
        bytes_read += len(chunk)

        if bytes_read > size_threshold + SIZE_THRESHOLD_BUFFER:
            logging.warning(
                f"{key} exceeds size threshold of {size_threshold}. Skipping."
            )
            return None

    return b"".join(chunks)


def _extract_onyx_metadata(line: str) -> dict | None:
    """
    Example: first line has:
        <!-- ONYX_METADATA={"title": "..."} -->
      or
        #ONYX_METADATA={"title":"..."}
    """
    html_comment_pattern = r"<!--\s*ONYX_METADATA=\{(.*?)\}\s*-->"
    hashtag_pattern = r"#ONYX_METADATA=\{(.*?)\}"

    html_comment_match = re.search(html_comment_pattern, line)
    hashtag_match = re.search(hashtag_pattern, line)

    if html_comment_match:
        json_str = html_comment_match.group(1)
    elif hashtag_match:
        json_str = hashtag_match.group(1)
    else:
        return None

    try:
        return json.loads("{" + json_str + "}")
    except json.JSONDecodeError:
        return None


def read_text_file(
    file: IO,
    encoding: str = "utf-8",
    errors: str = "replace",
    ignore_onyx_metadata: bool = True,
) -> tuple[str, dict]:
    """
    For plain text files. Optionally extracts Onyx metadata from the first line.
    """
    metadata = {}
    file_content_raw = ""
    for ind, line in enumerate(file):
        # decode
        try:
            line = line.decode(encoding) if isinstance(line, bytes) else line
        except UnicodeDecodeError:
            line = (
                line.decode(encoding, errors=errors)
                if isinstance(line, bytes)
                else line
            )

        # optionally parse metadata in the first line
        if ind == 0 and not ignore_onyx_metadata:
            potential_meta = _extract_onyx_metadata(line)
            if potential_meta is not None:
                metadata = potential_meta
                continue

        file_content_raw += line

    return file_content_raw, metadata


def get_blob_link(bucket_type: BlobType, s3_client: S3Client, bucket_name: str, key: str, bucket_region: str | None = None) -> str:
    """Get object link for different blob storage types"""
    encoded_key = quote(key, safe="/")

    if bucket_type == BlobType.R2:
        account_id = s3_client.meta.endpoint_url.split("//")[1].split(".")[0]
        subdomain = "eu/" if "eu." in s3_client.meta.endpoint_url else "default/"

        return f"https://dash.cloudflare.com/{account_id}/r2/{subdomain}buckets/{bucket_name}/objects/{encoded_key}/details"

    elif bucket_type == BlobType.S3:
        region = bucket_region or s3_client.meta.region_name
        return f"https://s3.console.aws.amazon.com/s3/object/{bucket_name}?region={region}&prefix={encoded_key}"

    elif bucket_type == BlobType.GOOGLE_CLOUD_STORAGE:
        return f"https://console.cloud.google.com/storage/browser/_details/{bucket_name}/{encoded_key}"

    elif bucket_type == BlobType.OCI_STORAGE:
        namespace = s3_client.meta.endpoint_url.split("//")[1].split(".")[0]
        region = s3_client.meta.region_name
        return f"https://objectstorage.{region}.oraclecloud.com/n/{namespace}/b/{bucket_name}/o/{encoded_key}"

    else:
        raise ValueError(f"Unsupported bucket type: {bucket_type}")


def extract_size_bytes(obj: Mapping[str, Any]) -> int | None:
    """Extract size bytes from object metadata"""
    candidate_keys = (
        "Size",
        "size",
        "ContentLength",
        "content_length",
        "Content-Length",
        "contentLength",
        "bytes",
        "Bytes",
    )

    def _normalize(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, Integral):
            return int(value)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric >= 0 and numeric.is_integer():
            return int(numeric)
        return None

    for key in candidate_keys:
        if key in obj:
            normalized = _normalize(obj.get(key))
            if normalized is not None:
                return normalized

    for key, value in obj.items():
        if not isinstance(key, str):
            continue
        lowered_key = key.lower()
        if "size" in lowered_key or "length" in lowered_key:
            normalized = _normalize(value)
            if normalized is not None:
                return normalized

    return None


def get_file_ext(file_name: str) -> str:
    """Get file extension"""
    return os.path.splitext(file_name)[1].lower()


def is_accepted_file_ext(file_ext: str, extension_type: str) -> bool:
    """Check if file extension is accepted"""
    # Simplified file extension check
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}
    text_extensions = {".txt", ".md", ".mdx", ".conf", ".log", ".json", ".csv", ".tsv", ".xml", ".yml", ".yaml", ".sql"}
    document_extensions = {".pdf", ".docx", ".pptx", ".xlsx", ".eml", ".epub", ".html"}
    
    if extension_type == "multimedia":
        return file_ext in image_extensions
    elif extension_type == "text":
        return file_ext in text_extensions
    elif extension_type == "document":
        return file_ext in document_extensions
    
    return False


def detect_encoding(file: IO[bytes]) -> str:
    raw_data = file.read(50000)
    file.seek(0)
    encoding = chardet.detect(raw_data)["encoding"] or "utf-8"
    return encoding


def get_markitdown_converter() -> "MarkItDown":
    global _MARKITDOWN_CONVERTER
    from markitdown import MarkItDown

    if _MARKITDOWN_CONVERTER is None:
        _MARKITDOWN_CONVERTER = MarkItDown(enable_plugins=False)
    return _MARKITDOWN_CONVERTER


def to_bytesio(stream: IO[bytes]) -> BytesIO:
    if isinstance(stream, BytesIO):
        return stream
    data = stream.read()  # consumes the stream!
    return BytesIO(data)



# Slack Utilities

@lru_cache()
def get_base_url(token: str) -> str:
    """Get and cache Slack workspace base URL"""
    client = WebClient(token=token)
    return client.auth_test()["url"]


def get_message_link(event: dict, client: WebClient, channel_id: str) -> str:
    """Get message link"""
    message_ts = event["ts"]
    message_ts_without_dot = message_ts.replace(".", "")
    thread_ts = event.get("thread_ts")
    base_url = get_base_url(client.token)

    link = f"{base_url.rstrip('/')}/archives/{channel_id}/p{message_ts_without_dot}" + (
        f"?thread_ts={thread_ts}" if thread_ts else ""
    )
    return link


def make_slack_api_call(call: Callable[..., SlackResponse], **kwargs: Any) -> SlackResponse:
    """Make Slack API call"""
    return call(**kwargs)


def make_paginated_slack_api_call(
    call: Callable[..., SlackResponse], **kwargs: Any
) -> Generator[dict[str, Any], None, None]:
    """Make paginated Slack API call"""
    return _make_slack_api_call_paginated(call)(**kwargs)


def _make_slack_api_call_paginated(
    call: Callable[..., SlackResponse],
) -> Callable[..., Generator[dict[str, Any], None, None]]:
    """Wrap Slack API call to automatically handle pagination"""

    @wraps(call)
    def paginated_call(**kwargs: Any) -> Generator[dict[str, Any], None, None]:
        cursor: str | None = None
        has_more = True
        while has_more:
            response = call(cursor=cursor, limit=_SLACK_LIMIT, **kwargs)
            yield response.validate()
            cursor = response.get("response_metadata", {}).get("next_cursor", "")
            has_more = bool(cursor)

    return paginated_call


def is_atlassian_date_error(e: Exception) -> bool:
    return "field 'updated' is invalid" in str(e)


def expert_info_from_slack_id(
    user_id: str | None,
    client: WebClient,
    user_cache: dict[str, BasicExpertInfo | None],
) -> BasicExpertInfo | None:
    """Get expert information from Slack user ID"""
    if not user_id:
        return None

    if user_id in user_cache:
        return user_cache[user_id]

    response = client.users_info(user=user_id)

    if not response["ok"]:
        user_cache[user_id] = None
        return None

    user: dict = response.data.get("user", {})
    profile = user.get("profile", {})

    expert = BasicExpertInfo(
        display_name=user.get("real_name") or profile.get("display_name"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
        email=profile.get("email"),
    )

    user_cache[user_id] = expert

    return expert


class SlackTextCleaner:
    """Slack text cleaning utility class"""

    def __init__(self, client: WebClient) -> None:
        self._client = client
        self._id_to_name_map: dict[str, str] = {}

    def _get_slack_name(self, user_id: str) -> str:
        """Get Slack username"""
        if user_id not in self._id_to_name_map:
            try:
                response = self._client.users_info(user=user_id)
                self._id_to_name_map[user_id] = (
                    response["user"]["profile"]["display_name"]
                    or response["user"]["profile"]["real_name"]
                )
            except SlackApiError as e:
                logging.exception(
                    f"Error fetching data for user {user_id}: {e.response['error']}"
                )
                raise

        return self._id_to_name_map[user_id]

    def _replace_user_ids_with_names(self, message: str) -> str:
        """Replace user IDs with usernames"""
        user_ids = re.findall("<@(.*?)>", message)

        for user_id in user_ids:
            try:
                if user_id in self._id_to_name_map:
                    user_name = self._id_to_name_map[user_id]
                else:
                    user_name = self._get_slack_name(user_id)

                message = message.replace(f"<@{user_id}>", f"@{user_name}")
            except Exception:
                logging.exception(
                    f"Unable to replace user ID with username for user_id '{user_id}'"
                )

        return message

    def index_clean(self, message: str) -> str:
        """Index cleaning"""
        message = self._replace_user_ids_with_names(message)
        message = self.replace_tags_basic(message)
        message = self.replace_channels_basic(message)
        message = self.replace_special_mentions(message)
        message = self.replace_special_catchall(message)
        return message

    @staticmethod
    def replace_tags_basic(message: str) -> str:
        """Basic tag replacement"""
        user_ids = re.findall("<@(.*?)>", message)
        for user_id in user_ids:
            message = message.replace(f"<@{user_id}>", f"@{user_id}")
        return message

    @staticmethod
    def replace_channels_basic(message: str) -> str:
        """Basic channel replacement"""
        channel_matches = re.findall(r"<#(.*?)\|(.*?)>", message)
        for channel_id, channel_name in channel_matches:
            message = message.replace(
                f"<#{channel_id}|{channel_name}>", f"#{channel_name}"
            )
        return message

    @staticmethod
    def replace_special_mentions(message: str) -> str:
        """Special mention replacement"""
        message = message.replace("<!channel>", "@channel")
        message = message.replace("<!here>", "@here")
        message = message.replace("<!everyone>", "@everyone")
        return message

    @staticmethod
    def replace_special_catchall(message: str) -> str:
        """Special catchall replacement"""
        pattern = r"<!([^|]+)\|([^>]+)>"
        return re.sub(pattern, r"\2", message)

    @staticmethod
    def add_zero_width_whitespace_after_tag(message: str) -> str:
        """Add zero-width whitespace after tag"""
        return message.replace("@", "@\u200b")


# Gmail Utilities

def is_mail_service_disabled_error(error: HttpError) -> bool:
    """Detect if the Gmail API is telling us the mailbox is not provisioned."""
    if error.resp.status != 400:
        return False
    
    error_message = str(error)
    return (
        "Mail service not enabled" in error_message
        or "failedPrecondition" in error_message
    )


def build_time_range_query(
    time_range_start: SecondsSinceUnixEpoch | None = None,
    time_range_end: SecondsSinceUnixEpoch | None = None,
) -> str | None:
    """Build time range query for Gmail API"""
    query = ""
    if time_range_start is not None and time_range_start != 0:
        query += f"after:{int(time_range_start)}"
    if time_range_end is not None and time_range_end != 0:
        query += f" before:{int(time_range_end)}"
    query = query.strip()
    
    if len(query) == 0:
        return None
    
    return query


def clean_email_and_extract_name(email: str) -> tuple[str, str | None]:
    """Extract email address and display name from email string."""
    email = email.strip()
    if "<" in email and ">" in email:
        # Handle format: "Display Name <email@domain.com>"
        display_name = email[: email.find("<")].strip()
        email_address = email[email.find("<") + 1 : email.find(">")].strip()
        return email_address, display_name if display_name else None
    else:
        # Handle plain email address
        return email.strip(), None


def get_message_body(payload: dict[str, Any]) -> str:
    """Extract message body text from Gmail message payload."""
    parts = payload.get("parts", [])
    message_body = ""
    for part in parts:
        mime_type = part.get("mimeType")
        body = part.get("body")
        if mime_type == "text/plain" and body:
            data = body.get("data", "")
            text = base64.urlsafe_b64decode(data).decode()
            message_body += text
    return message_body


def get_google_creds(
    credentials: dict[str, Any], 
    source: str
) -> tuple[OAuthCredentials | ServiceAccountCredentials | None, dict[str, str] | None]:
    """Get Google credentials based on authentication type."""
    # Simplified credential loading - in production this would handle OAuth and service accounts
    primary_admin_email = credentials.get(DB_CREDENTIALS_PRIMARY_ADMIN_KEY)
    
    if not primary_admin_email:
        raise ValueError("Primary admin email is required")
    
    # Return None for credentials and empty dict for new creds
    # In a real implementation, this would handle actual credential loading
    return None, {}


def get_admin_service(creds: OAuthCredentials | ServiceAccountCredentials, admin_email: str):
    """Get Google Admin service instance."""
    # Simplified implementation
    return None


def get_gmail_service(creds: OAuthCredentials | ServiceAccountCredentials, user_email: str):
    """Get Gmail service instance."""
    # Simplified implementation
    return None


def execute_paginated_retrieval(
    retrieval_function, 
    list_key: str, 
    fields: str, 
    **kwargs
):
    """Execute paginated retrieval from Google APIs."""
    # Simplified pagination implementation
    return []


def execute_single_retrieval(
    retrieval_function, 
    list_key: Optional[str], 
    **kwargs
):
    """Execute single retrieval from Google APIs."""
    # Simplified single retrieval implementation
    return []


def time_str_to_utc(time_str: str):
    """Convert time string to UTC datetime."""
    from datetime import datetime
    return datetime.fromisoformat(time_str.replace('Z', '+00:00'))


# Notion Utilities
T = TypeVar("T")


def batch_generator(
    items: Iterable[T],
    batch_size: int,
    pre_batch_yield: Callable[[list[T]], None] | None = None,
) -> Generator[list[T], None, None]:
    iterable = iter(items)
    while True:
        batch = list(islice(iterable, batch_size))
        if not batch:
            return

        if pre_batch_yield:
            pre_batch_yield(batch)
        yield batch


@retry(tries=3, delay=1, backoff=2)
def fetch_notion_data(
    url: str, 
    headers: dict[str, str], 
    method: str = "GET", 
    json_data: Optional[dict] = None
) -> dict[str, Any]:
    """Fetch data from Notion API with retry logic."""
    try:
        if method == "GET":
            response = rl_requests.get(url, headers=headers, timeout=_NOTION_CALL_TIMEOUT)
        elif method == "POST":
            response = rl_requests.post(url, headers=headers, json=json_data, timeout=_NOTION_CALL_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching data from Notion API: {e}")
        raise


def properties_to_str(properties: dict[str, Any]) -> str:
    """Convert Notion properties to a string representation."""
    
    def _recurse_list_properties(inner_list: list[Any]) -> str | None:
        list_properties: list[str | None] = []
        for item in inner_list:
            if item and isinstance(item, dict):
                list_properties.append(_recurse_properties(item))
            elif item and isinstance(item, list):
                list_properties.append(_recurse_list_properties(item))
            else:
                list_properties.append(str(item))
        return (
            ", ".join([list_property for list_property in list_properties if list_property])
            or None
        )

    def _recurse_properties(inner_dict: dict[str, Any]) -> str | None:
        sub_inner_dict: dict[str, Any] | list[Any] | str = inner_dict
        while isinstance(sub_inner_dict, dict) and "type" in sub_inner_dict:
            type_name = sub_inner_dict["type"]
            sub_inner_dict = sub_inner_dict[type_name]
            
            if not sub_inner_dict:
                return None

        if isinstance(sub_inner_dict, list):
            return _recurse_list_properties(sub_inner_dict)
        elif isinstance(sub_inner_dict, str):
            return sub_inner_dict
        elif isinstance(sub_inner_dict, dict):
            if "name" in sub_inner_dict:
                return sub_inner_dict["name"]
            if "content" in sub_inner_dict:
                return sub_inner_dict["content"]
            start = sub_inner_dict.get("start")
            end = sub_inner_dict.get("end")
            if start is not None:
                if end is not None:
                    return f"{start} - {end}"
                return start
            elif end is not None:
                return f"Until {end}"
            
            if "id" in sub_inner_dict:
                logging.debug("Skipping Notion object id field property")
                return None

        logging.debug(f"Unreadable property from innermost prop: {sub_inner_dict}")
        return None

    result = ""
    for prop_name, prop in properties.items():
        if not prop or not isinstance(prop, dict):
            continue
        
        try:
            inner_value = _recurse_properties(prop)
        except Exception as e:
            logging.warning(f"Error recursing properties for {prop_name}: {e}")
            continue
        
        if inner_value:
            result += f"{prop_name}: {inner_value}\t"

    return result


def filter_pages_by_time(
    pages: list[dict[str, Any]],
    start: float,
    end: float,
    filter_field: str = "last_edited_time"
) -> list[dict[str, Any]]:
    """Filter pages by time range."""
    from datetime import datetime
    
    filtered_pages: list[dict[str, Any]] = []
    for page in pages:
        timestamp = page[filter_field].replace(".000Z", "+00:00")
        compare_time = datetime.fromisoformat(timestamp).timestamp()
        if compare_time > start and compare_time <= end:
            filtered_pages.append(page)
    return filtered_pages


def _load_all_docs(
    connector: CheckpointedConnector[CT],
    load: LoadFunction,
) -> list[Document]:
    num_iterations = 0

    checkpoint = cast(CT, connector.build_dummy_checkpoint())
    documents: list[Document] = []
    while checkpoint.has_more:
        doc_batch_generator = CheckpointOutputWrapper[CT]()(load(checkpoint))
        for document, failure, next_checkpoint in doc_batch_generator:
            if failure is not None:
                raise RuntimeError(f"Failed to load documents: {failure}")
            if document is not None and isinstance(document, Document):
                documents.append(document)
            if next_checkpoint is not None:
                checkpoint = next_checkpoint

        num_iterations += 1
        if num_iterations > _ITERATION_LIMIT:
            raise RuntimeError("Too many iterations. Infinite loop?")

    return documents


def load_all_docs_from_checkpoint_connector(
    connector: CheckpointedConnector[CT],
    start: SecondsSinceUnixEpoch,
    end: SecondsSinceUnixEpoch,
) -> list[Document]:
    return _load_all_docs(
        connector=connector,
        load=lambda checkpoint: connector.load_from_checkpoint(
            start=start, end=end, checkpoint=checkpoint
        ),
    )


def get_cloudId(base_url: str) -> str:
    tenant_info_url = urljoin(base_url, "/_edge/tenant_info")
    response = requests.get(tenant_info_url, timeout=10)
    response.raise_for_status()
    return response.json()["cloudId"]


def scoped_url(url: str, product: str) -> str:
    parsed = urlparse(url)
    base_url = parsed.scheme + "://" + parsed.netloc
    cloud_id = get_cloudId(base_url)
    return f"https://api.atlassian.com/ex/{product}/{cloud_id}{parsed.path}"


def process_confluence_user_profiles_override(
    confluence_user_email_override: list[dict[str, str]],
) -> list[ConfluenceUser]:
    return [
        ConfluenceUser(
            user_id=override["user_id"],
            # username is not returned by the Confluence Server API anyways
            username=override["username"],
            display_name=override["display_name"],
            email=override["email"],
            type=override["type"],
        )
        for override in confluence_user_email_override
        if override is not None
    ]


def confluence_refresh_tokens(
    client_id: str, client_secret: str, cloud_id: str, refresh_token: str
) -> dict[str, Any]:
    # rotate the refresh and access token
    # Note that access tokens are only good for an hour in confluence cloud,
    # so we're going to have problems if the connector runs for longer
    # https://developer.atlassian.com/cloud/confluence/oauth-2-3lo-apps/#use-a-refresh-token-to-get-another-access-token-and-refresh-token-pair
    response = requests.post(
        CONFLUENCE_OAUTH_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )

    try:
        token_response = TokenResponse.model_validate_json(response.text)
    except Exception:
        raise RuntimeError("Confluence Cloud token refresh failed.")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=token_response.expires_in)

    new_credentials: dict[str, Any] = {}
    new_credentials["confluence_access_token"] = token_response.access_token
    new_credentials["confluence_refresh_token"] = token_response.refresh_token
    new_credentials["created_at"] = now.isoformat()
    new_credentials["expires_at"] = expires_at.isoformat()
    new_credentials["expires_in"] = token_response.expires_in
    new_credentials["scope"] = token_response.scope
    new_credentials["cloud_id"] = cloud_id
    return new_credentials


class TimeoutThread(threading.Thread, Generic[R]):
    def __init__(
        self, timeout: float, func: Callable[..., R], *args: Any, **kwargs: Any
    ):
        super().__init__()
        self.timeout = timeout
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.exception: Exception | None = None

    def run(self) -> None:
        try:
            self.result = self.func(*self.args, **self.kwargs)
        except Exception as e:
            self.exception = e

    def end(self) -> None:
        raise TimeoutError(
            f"Function {self.func.__name__} timed out after {self.timeout} seconds"
        )


def run_with_timeout(
    timeout: float, func: Callable[..., R], *args: Any, **kwargs: Any
) -> R:
    """
    Executes a function with a timeout. If the function doesn't complete within the specified
    timeout, raises TimeoutError.
    """
    context = contextvars.copy_context()
    task = TimeoutThread(timeout, context.run, func, *args, **kwargs)
    task.start()
    task.join(timeout)

    if task.exception is not None:
        raise task.exception
    if task.is_alive():
        task.end()

    return task.result  # type: ignore


def validate_attachment_filetype(
    attachment: dict[str, Any],
) -> bool:
    """
    Validates if the attachment is a supported file type.
    """
    media_type = attachment.get("metadata", {}).get("mediaType", "")
    if media_type.startswith("image/"):
        return is_valid_image_type(media_type)

    # For non-image files, check if we support the extension
    title = attachment.get("title", "")
    extension = Path(title).suffix.lstrip(".").lower() if "." in title else ""

    return is_accepted_file_ext(
        "." + extension, OnyxExtensionType.Plain | OnyxExtensionType.Document
    )


def _make_attachment_link(
    confluence_client: "OnyxConfluence",
    attachment: dict[str, Any],
    parent_content_id: str | None = None,
) -> str | None:
    download_link = ""

    if "api.atlassian.com" in confluence_client.url:
        # https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content---attachments/#api-wiki-rest-api-content-id-child-attachment-attachmentid-download-get
        if not parent_content_id:
            logging.warning(
                "parent_content_id is required to download attachments from Confluence Cloud!"
            )
            return None

        download_link = (
            confluence_client.url
            + f"/rest/api/content/{parent_content_id}/child/attachment/{attachment['id']}/download"
        )
    else:
        download_link = confluence_client.url + attachment["_links"]["download"]

    return download_link


def _process_image_attachment(
    confluence_client: "OnyxConfluence",
    attachment: dict[str, Any],
    raw_bytes: bytes,
    media_type: str,
) -> AttachmentProcessingResult:
    """Process an image attachment by saving it without generating a summary."""
    try:
        # Use the standardized image storage and section creation
        section, file_name = store_image_and_create_section(
            image_data=raw_bytes,
            file_id=Path(attachment["id"]).name,
            display_name=attachment["title"],
            media_type=media_type,
            file_origin=FileOrigin.CONNECTOR,
        )
        logging.info(f"Stored image attachment with file name: {file_name}")

        # Return empty text but include the file_name for later processing
        return AttachmentProcessingResult(text="", file_name=file_name, error=None)
    except Exception as e:
        msg = f"Image storage failed for {attachment['title']}: {e}"
        logging.error(msg, exc_info=e)
        return AttachmentProcessingResult(text=None, file_name=None, error=msg)


def process_attachment(
    confluence_client: "OnyxConfluence",
    attachment: dict[str, Any],
    parent_content_id: str | None,
    allow_images: bool,
) -> AttachmentProcessingResult:
    """
    Processes a Confluence attachment. If it's a document, extracts text,
    or if it's an image, stores it for later analysis. Returns a structured result.
    """
    try:
        # Get the media type from the attachment metadata
        media_type: str = attachment.get("metadata", {}).get("mediaType", "")
        # Validate the attachment type
        if not validate_attachment_filetype(attachment):
            return AttachmentProcessingResult(
                text=None,
                file_name=None,
                error=f"Unsupported file type: {media_type}",
            )

        attachment_link = _make_attachment_link(
            confluence_client, attachment, parent_content_id
        )
        if not attachment_link:
            return AttachmentProcessingResult(
                text=None, file_name=None, error="Failed to make attachment link"
            )

        attachment_size = attachment["extensions"]["fileSize"]

        if media_type.startswith("image/"):
            if not allow_images:
                return AttachmentProcessingResult(
                    text=None,
                    file_name=None,
                    error="Image downloading is not enabled",
                )
        else:
            if attachment_size > CONFLUENCE_CONNECTOR_ATTACHMENT_SIZE_THRESHOLD:
                logging.warning(
                    f"Skipping {attachment_link} due to size. "
                    f"size={attachment_size} "
                    f"threshold={CONFLUENCE_CONNECTOR_ATTACHMENT_SIZE_THRESHOLD}"
                )
                return AttachmentProcessingResult(
                    text=None,
                    file_name=None,
                    error=f"Attachment text too long: {attachment_size} chars",
                )

        logging.info(
            f"Downloading attachment: "
            f"title={attachment['title']} "
            f"length={attachment_size} "
            f"link={attachment_link}"
        )

        # Download the attachment
        resp: requests.Response = confluence_client._session.get(attachment_link)
        if resp.status_code != 200:
            logging.warning(
                f"Failed to fetch {attachment_link} with status code {resp.status_code}"
            )
            return AttachmentProcessingResult(
                text=None,
                file_name=None,
                error=f"Attachment download status code is {resp.status_code}",
            )

        raw_bytes = resp.content
        if not raw_bytes:
            return AttachmentProcessingResult(
                text=None, file_name=None, error="attachment.content is None"
            )

        # Process image attachments
        if media_type.startswith("image/"):
            return _process_image_attachment(
                confluence_client, attachment, raw_bytes, media_type
            )

        # Process document attachments
        try:
            text = extract_file_text(
                file=BytesIO(raw_bytes),
                file_name=attachment["title"],
            )

            # Skip if the text is too long
            if len(text) > CONFLUENCE_CONNECTOR_ATTACHMENT_CHAR_COUNT_THRESHOLD:
                return AttachmentProcessingResult(
                    text=None,
                    file_name=None,
                    error=f"Attachment text too long: {len(text)} chars",
                )

            return AttachmentProcessingResult(text=text, file_name=None, error=None)
        except Exception as e:
            return AttachmentProcessingResult(
                text=None, file_name=None, error=f"Failed to extract text: {e}"
            )

    except Exception as e:
        return AttachmentProcessingResult(
            text=None, file_name=None, error=f"Failed to process attachment: {e}"
        )


def convert_attachment_to_content(
    confluence_client: "OnyxConfluence",
    attachment: dict[str, Any],
    page_id: str,
    allow_images: bool,
) -> tuple[str | None, str | None] | None:
    """
    Facade function which:
      1. Validates attachment type
      2. Extracts content or stores image for later processing
      3. Returns (content_text, stored_file_name) or None if we should skip it
    """
    media_type = attachment.get("metadata", {}).get("mediaType", "")
    # Quick check for unsupported types:
    if media_type.startswith("video/") or media_type == "application/gliffy+json":
        logging.warning(
            f"Skipping unsupported attachment type: '{media_type}' for {attachment['title']}"
        )
        return None

    result = process_attachment(confluence_client, attachment, page_id, allow_images)
    if result.error is not None:
        logging.warning(
            f"Attachment {attachment['title']} encountered error: {result.error}"
        )
        return None

    # Return the text and the file name
    return result.text, result.file_name


def get_page_restrictions(
    confluence_client: OnyxConfluence,
    page_id: str,
    page_restrictions: dict[str, Any],
    ancestors: list[dict[str, Any]],
) -> ExternalAccess | None:
    """
    Get page access restrictions for a Confluence page.
    This functionality requires Enterprise Edition.

    Args:
        confluence_client: OnyxConfluence client instance
        page_id: The ID of the page
        page_restrictions: Dictionary containing page restriction data
        ancestors: List of ancestor pages with their restriction data

    Returns:
        ExternalAccess object for the page. None if EE is not enabled or no restrictions found.
    """
    # Fetch the EE implementation
    ee_get_all_page_restrictions = cast(
        Callable[
            [OnyxConfluence, str, dict[str, Any], list[dict[str, Any]]],
            ExternalAccess | None,
        ],
        fetch_versioned_implementation(
            "onyx.external_permissions.confluence.page_access", "get_page_restrictions"
        ),
    )

    return ee_get_all_page_restrictions(
        confluence_client, page_id, page_restrictions, ancestors
    )


def get_all_space_permissions(
    confluence_client: OnyxConfluence,
    is_cloud: bool,
) -> dict[str, ExternalAccess]:
    """
    Get access permissions for all spaces in Confluence.
    This functionality requires Enterprise Edition.

    Args:
        confluence_client: OnyxConfluence client instance
        is_cloud: Whether this is a Confluence Cloud instance

    Returns:
        Dictionary mapping space keys to ExternalAccess objects. Empty dict if EE is not enabled.
    """

    # Fetch the EE implementation
    ee_get_all_space_permissions = cast(
        Callable[
            [OnyxConfluence, bool],
            dict[str, ExternalAccess],
        ],
        fetch_versioned_implementation(
            "onyx.external_permissions.confluence.space_access",
            "get_all_space_permissions",
        ),
    )

    return ee_get_all_space_permissions(confluence_client, is_cloud)
