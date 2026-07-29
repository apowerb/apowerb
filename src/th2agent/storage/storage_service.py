from th2agent.configs.settings import get_settings
from th2agent.configs.paths import uploads_dir

class StorageService:
    def __init__(self):
        self.settings=get_settings()
        self.storage_mode=self.settings.storage_mode

        if self.storage_mode=="S3":
            from .s3 import upload_file_to_s3, list_files_in_s3, upload_bytes_to_s3, download_file_from_s3, file_exists_in_s3
            self.upload_file_to_s3=upload_file_to_s3
            self.list_files_in_s3=list_files_in_s3
            self.upload_bytes_to_s3=upload_bytes_to_s3
            self.download_file_from_s3=download_file_from_s3
            self.file_exists_in_s3=file_exists_in_s3
        else:  
            import os
            
            def upload_bytes_to_local(content: bytes, local_path: str, content_type: str = "application/octet-stream") -> str:
                """Save bytes to local file system."""
                full_path = str(uploads_dir() / local_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "wb") as f:
                    f.write(content)
                return local_path
            
            def download_file_from_local(local_path: str) -> bytes:
                """Read file from local filesystem as bytes."""
                full_path = str(uploads_dir() / local_path)
                with open(full_path, "rb") as f:
                    return f.read()
            
            def file_exists_in_local(local_path: str) -> bool:
                """Check if file exists locally."""
                full_path = str(uploads_dir() / local_path)
                return os.path.exists(full_path)
            
            def list_files_in_local(prefix: str = "") -> list[str]:
                """List files in local directory."""
                full_path = str(uploads_dir() / prefix)
                if not os.path.exists(full_path):
                    return []
                files = []
                for root, dirs, filenames in os.walk(full_path):
                    for filename in filenames:
                        rel_path = os.path.relpath(os.path.join(root, filename), str(uploads_dir()))
                        files.append(rel_path)
                return files
            
            # Bind methods
            self.upload_bytes_to_storage = upload_bytes_to_local
            self.download_file_from_storage = download_file_from_local
            self.file_exists_in_storage = file_exists_in_local
            self.list_files_in_storage = list_files_in_local





