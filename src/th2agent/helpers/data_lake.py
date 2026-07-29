from fastapi import logger
import pins
import os
from typing import Optional
from pydantic import BaseModel


class StorageBoardConfig(BaseModel):
    """Load configuration for storage board from environment variables or parameters.
    This class can be extended to support other storage backends in the future.
    Parameters
    ----------
    bucket_name: str
        The S3 bucket name to be used for the board.
    prefix: str
        Prefix (folder) inside the bucket. It will be joined
        with the bucket name as `bucket_name/prefix` for the pins board path.
    region: Optional[str] = None
        The region for S3 storage. If not provided, it will be resolved from environment variables.
    """

    bucket_name: str
    prefix: str
    region: Optional[str] = None
    endpoint_url: Optional[str] = None
    key: Optional[str] = None
    secret: Optional[str] = None

    def _get_s3_board(self, path: str, versioned=True, **kwargs):
        """
        Create and return a pins S3 board for S3-compatible storage (e.g., Scaleway).
        """
        # Read from S3_REGION instead of AWS_REGION
        region = self.region or kwargs.get("region") or os.getenv("S3_REGION")

        board_kwargs: dict = {"versioned": versioned}

        # Build storage_options for S3-compatible endpoints
        storage_options = {}

        # Use instance attributes first, fall back to env vars
        endpoint = self.endpoint_url or os.getenv("S3_ENDPOINT")
        if endpoint:
            storage_options["endpoint_url"] = endpoint

        key = self.key or os.getenv("S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
        if key:
            storage_options["key"] = key

        secret = (
            self.secret
            or os.getenv("S3_ACCESS_KEY_SECRET")
            or os.getenv("AWS_SECRET_ACCESS_KEY")
        )
        if secret:
            storage_options["secret"] = secret

        # CRITICAL: Pass storage_options to the board
        if storage_options:
            board_kwargs["storage_options"] = storage_options

        if region:
            board_kwargs["region"] = region

        return pins.board_s3(path, **storage_options)

    def _get_folder_board(self, folder_path: str):
        """Create and return a pins folder board."""
        return pins.board_folder(folder_path)


class StorageBoardFactory(StorageBoardConfig):
    """Factory class to create storage boards based on configuration.
    This can be extended to support multiple storage backends in the future."""

    def get_board(self, storage_source: str = "s3"):
        storage_source = (storage_source or "s3").lower()

        if storage_source == "s3":
            # Use full path with prefix
            board_path = f"{self.bucket_name.rstrip('/')}/{self.prefix.lstrip('/')}"
            return self._get_s3_board(board_path, region=self.region)
        elif storage_source == "folder":
            return self._get_folder_board(self.prefix)
        else:
            raise ValueError("storage_source must be 's3' or 'folder'")

    def _find_file_location(self, file_name: Optional[str] = None) -> str:
        # This method can be extended to check for file existence in different storage backends
        data_board = self.get_board()
        file_versions = data_board.pin_versions(file_name, as_df=True)
        if file_versions.empty:
            raise FileNotFoundError(f"File '{file_name}' not found in storage board.")
            # Get the latest version of the file
        latest_version = file_versions.sort_values("version", ascending=False).iloc[0]
        logger.info(
            "Found file '%s' in storage board with version '%s'",
            file_name,
            latest_version["version"],
        )
        return f"{self.bucket_name.rstrip('/')}/{self.prefix.lstrip('/')}/{file_name}/{latest_version['version']}"
