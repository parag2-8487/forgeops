import os
import re

files_to_fix = [
    "backend/tests/integration/test_cascade_integration.py",
    "backend/tests/property/test_p02_cascade.py",
    "backend/tests/property/test_p03_skip_invocation.py"
]

for fpath in files_to_fix:
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    
    if "create_redacted_chunk" not in content:
        content = content.replace("from __future__ import annotations", "from __future__ import annotations\nfrom src.secrets.redaction import create_redacted_chunk")
    
    # regex to match router.complete(..., request=...) and append prompt
    # we can just use a simple regex replacing "request=request)" -> "request=request, prompt=create_redacted_chunk('foo'))"
    # and "request=_make_request())" -> "request=_make_request(), prompt=create_redacted_chunk('foo'))"
    content = re.sub(r'request=request\)', 'request=request, prompt=create_redacted_chunk("foo"))', content)
    content = re.sub(r'request=_make_request\(\)\)', 'request=_make_request(), prompt=create_redacted_chunk("foo"))', content)
    # also for test_cascade_integration.py line 301 it's multi-line
    content = re.sub(r'request=CompletionRequest\((.*?)\),', r'request=CompletionRequest(\1),\n            prompt=create_redacted_chunk("foo"),', content, flags=re.DOTALL)
    
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)

print("Done")
