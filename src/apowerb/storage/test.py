from sys import prefix

from s3 import upload_file_to_s3, list_files_in_s3

#upload_file_to_s3("src/th2agent/storage/test.txt", "test/test.txt")
#print(list_files_in_s3("test/"))

from s3 import upload_bytes_to_s3

upload_bytes_to_s3(
    b"hello from bytes",
    "test/yacine.txt",
    "text/plain"
)
print(list_files_in_s3("test/"))

