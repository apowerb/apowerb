# URLs and decoy secrets in tests

> Use when a test asserts on a URL or a host, or needs a fake secret or a decoy file.

Tests are scanned like the rest of the code (CodeQL). Two shapes have been reported here and are worth avoiding because the stricter form is also the better test.

## Assert a URL whole, never as a substring

`"api.example.com" in url` also passes for `https://api.example.com.evil.test/` and for a URL that only mentions the host in a query parameter (`py/incomplete-url-substring-sanitization`, PR #36, alert #77).

```python
# Don't
assert "http://mage.test" in record.getMessage()

# Do: compare the value itself, or the parsed parts
assert record.args == ("http://mage.test",)
parsed = urlparse(call_url)
assert parsed.scheme == "wss" and parsed.hostname == "api.deepgram.com"
```

## Name a decoy for what it is

A variable called `secret`, `password` or `token` is treated as sensitive data: writing it to a file or a log is reported as clear-text storage (`py/clear-text-storage-sensitive-data`, alerts #74 and #75). In a test it is usually a path or a made-up marker: call it `target`, `decoy`, `marker`.

```python
# Don't
secret = tmp_path / "secret.txt"

# Do
target = tmp_path / "secret.txt"   # the path the external entity points at
```

Never put a real credential, customer name or customer document in a test: this repository is public.
